"""
Invoice Editor Service - FINAL VERSION
Method: PyMuPDF (direct PDF editing) + Pillow fallback (for image inputs)
Output: Always PDF
Features:
  - Reads exact font name/size/color from original PDF
  - Redacts old text using PyMuPDF redaction API (pixel-perfect white box)
  - Re-renders new text with same font metadata
  - Collapses deleted product rows + shifts totals block up
  - Converts image input to PDF if needed
  - Always outputs PDF
"""

from flask import Flask, request, jsonify, send_file
import pymupdf
from PIL import Image, ImageDraw, ImageFont
import base64, io, os, json, traceback, re

app = Flask(__name__)

# ─── Color Helpers ────────────────────────────────────────────────────────────

def hex_to_rgb_float(hex_color: str) -> tuple:
    """Convert #RRGGBB to (r, g, b) floats 0-1 for PyMuPDF."""
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        r, g, b = int(h[0:2],16), int(h[2:4],16), int(h[4:6],16)
        return (r/255, g/255, b/255)
    except Exception:
        return (0, 0, 0)

def int_color_to_hex(color_int: int) -> str:
    """Convert PyMuPDF integer color to hex string."""
    r = (color_int >> 16) & 0xFF
    g = (color_int >> 8)  & 0xFF
    b =  color_int        & 0xFF
    return f"#{r:02x}{g:02x}{b:02x}"

def pymupdf_color(color_int: int) -> tuple:
    """Convert PyMuPDF integer color to (r,g,b) floats."""
    r = ((color_int >> 16) & 0xFF) / 255
    g = ((color_int >> 8)  & 0xFF) / 255
    b = ( color_int        & 0xFF) / 255
    return (r, g, b)

# ─── Font Mapping ─────────────────────────────────────────────────────────────

# Map PDF font names to PyMuPDF built-in names
FONT_MAP = {
    "helvetica":        "helv",
    "helvetica-bold":   "hebo",
    "helvetica-oblique":"heit",
    "helveticaneue":    "helv",
    "arial":            "helv",
    "arial-bold":       "hebo",
    "arialmt":          "helv",
    "arial-boldmt":     "hebo",
    "times":            "tiro",
    "times-roman":      "tiro",
    "times-bold":       "tibo",
    "timesnewroman":    "tiro",
    "timesnewroman-bold":"tibo",
    "courier":          "cour",
    "courier-bold":     "cobo",
    "calibri":          "helv",
    "calibri-bold":     "hebo",
    "verdana":          "helv",
    "verdana-bold":     "hebo",
    "trebuchet":        "helv",
    "garamond":         "tiro",
    "georgia":          "tiro",
    "georgia-bold":     "tibo",
}

def map_font(pdf_font_name: str) -> str:
    """Map a PDF font name to the closest PyMuPDF built-in."""
    clean = pdf_font_name.lower().replace(" ", "").replace(",", "")
    # Try direct match
    if clean in FONT_MAP:
        return FONT_MAP[clean]
    # Try partial match
    for key, val in FONT_MAP.items():
        if key in clean:
            return val
    # Default
    return "helv"

# ─── Number Formatting ────────────────────────────────────────────────────────

def format_currency(value: float, template: str) -> str:
    """
    Format a number to match the original invoice's currency format.
    template: e.g. "£1,250.00" or "$1.250,00"
    """
    # Detect currency symbol
    symbol = ""
    symbol_after = False
    for ch in template:
        if ch in "£$€¥₹₪₩฿":
            symbol = ch
            break
    if template and template[-1] in "£$€¥₹₪₩฿":
        symbol_after = True

    # Detect decimal separator style
    # European: 1.234,56  |  Standard: 1,234.56
    if re.search(r'\d\.\d{3},\d{2}', template):
        # European format
        formatted = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    else:
        # Standard format
        formatted = f"{value:,.2f}"

    if symbol_after:
        return formatted + symbol
    return symbol + formatted

# ─── PDF Editing (PyMuPDF) ────────────────────────────────────────────────────

def edit_pdf(pdf_bytes: bytes, invoice_map: dict, user_products: list) -> bytes:
    """
    Edit a PDF using PyMuPDF.
    - Reads exact font metadata from original
    - Redacts old text regions
    - Re-renders new text with same font/size/color
    - Collapses rows + shifts totals
    Returns edited PDF bytes.
    """
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    page = doc[0]  # Invoice is always page 1
    page_rect = page.rect

    log = []

    # ── Build a font metadata index from the original PDF ─────────────────────
    # Key: approximate y position → span metadata
    # This lets us find the exact font used at any location
    font_index = {}
    blocks = page.get_text("dict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE)["blocks"]
    for block in blocks:
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                y_key = round(span["bbox"][1])
                font_index[y_key] = {
                    "font":  span["font"],
                    "size":  span["size"],
                    "color": span["color"],
                    "bbox":  span["bbox"]
                }

    def get_font_at(y_px: float, fallback_size: float = 12) -> dict:
        """Find the closest font metadata for a given y position."""
        if not font_index:
            return {"font": "helv", "size": fallback_size, "color": 0}
        closest = min(font_index.keys(), key=lambda k: abs(k - y_px))
        if abs(closest - y_px) < 30:
            return font_index[closest]
        return {"font": "helv", "size": fallback_size, "color": 0}

    def redact_rect(rect: pymupdf.Rect, bg_color=(1,1,1)):
        """Redact a rectangle with background color."""
        annot = page.add_redact_annot(rect, fill=bg_color)
        return annot

    def render_text(x: float, y: float, text: str,
                    fontname: str, fontsize: float,
                    color: tuple, align: str = "left",
                    box_width: float = None):
        """Render text at position. Handles alignment within box_width."""
        mapped_font = map_font(fontname)
        if align == "right" and box_width:
            # Measure text width to right-align
            try:
                tw = pymupdf.get_text_length(text, fontname=mapped_font, fontsize=fontsize)
                x = x + box_width - tw
            except Exception:
                pass
        elif align == "center" and box_width:
            try:
                tw = pymupdf.get_text_length(text, fontname=mapped_font, fontsize=fontsize)
                x = x + (box_width - tw) / 2
            except Exception:
                pass
        page.insert_text(
            (x, y + fontsize),  # PyMuPDF y is baseline
            text,
            fontname=mapped_font,
            fontsize=fontsize,
            color=color
        )

    # ── STEP 1: Simple field edits ─────────────────────────────────────────────
    for edit in invoice_map.get("simple_edits", []):
        field    = edit.get("field", "unknown")
        new_val  = str(edit.get("new_value", ""))
        x        = float(edit.get("x_px", 0))
        y        = float(edit.get("y_px", 0))
        w        = float(edit.get("width_px", 200))
        h        = float(edit.get("height_px", 20))
        align    = edit.get("align", "left")

        if not new_val:
            continue

        # Get exact font from original PDF at this location
        font_meta = get_font_at(y)
        fontname  = font_meta["font"]
        fontsize  = font_meta["size"]
        color     = pymupdf_color(font_meta["color"])

        # Redact old text
        rect = pymupdf.Rect(x - 2, y - 2, x + w + 4, y + h + 4)
        redact_rect(rect)
        page.apply_redactions()

        # Re-render new text
        render_text(x, y, new_val, fontname, fontsize, color, align, w)
        log.append(f"✅ Simple edit '{field}' → '{new_val}'")

    # ── STEP 2: Product table rebuild ─────────────────────────────────────────
    product_rows  = invoice_map.get("product_rows", [])
    table_header  = invoice_map.get("table_header", {})
    totals_block  = invoice_map.get("totals_block", {})
    row_font_meta = invoice_map.get("row_font", {})

    if product_rows and user_products:
        first_row    = product_rows[0]
        row_height   = float(first_row.get("height_px", 26))
        first_row_y  = float(first_row.get("y_px", 0))
        last_row     = product_rows[-1]
        all_rows_end = float(last_row.get("y_px", 0)) + float(last_row.get("height_px", 26))
        columns      = table_header.get("columns", [])

        # Get row font from original PDF
        sample_y   = first_row_y + row_height / 2
        font_meta  = get_font_at(sample_y)
        fontname   = font_meta["font"]
        fontsize   = font_meta["size"]
        row_color  = pymupdf_color(font_meta["color"])

        # Wipe ALL original product rows in one redaction
        wipe_rect = pymupdf.Rect(0, first_row_y - 2, page_rect.width, all_rows_end + 2)
        page.add_redact_annot(wipe_rect, fill=(1, 1, 1))
        page.apply_redactions()
        log.append(f"🗑️ Wiped {len(product_rows)} original rows ({first_row_y:.0f}–{all_rows_end:.0f}px)")

        # Re-render user products
        current_y = first_row_y
        for i, prod in enumerate(user_products):
            name  = str(prod.get("Product_Name", "")).strip()
            qty   = str(prod.get("Quantity", "")).strip()
            price = str(prod.get("Product_Price", "")).strip()

            # Auto-calculate line total
            try:
                qty_num   = float(re.sub(r'[^\d.]', '', qty))
                price_str = re.sub(r'[^\d.]', '', price)
                price_num = float(price_str)
                line_total = format_currency(qty_num * price_num, price)
            except Exception:
                line_total = ""

            # Map values to column labels
            col_values = {
                "description": name, "product": name, "item": name,
                "name": name, "details": name,
                "qty": qty, "quantity": qty, "units": qty,
                "unit price": price, "price": price, "rate": price,
                "unit cost": price, "cost": price,
                "total": line_total, "amount": line_total,
                "line total": line_total, "subtotal": line_total
            }

            for col in columns:
                col_label = col.get("label", "").lower()
                col_x     = float(col.get("x_px", 0))
                col_w     = float(col.get("width_px", 100))
                col_align = col.get("align", "left")

                val = ""
                for key, v in col_values.items():
                    if key in col_label:
                        val = v
                        break

                if val:
                    render_text(col_x, current_y, val, fontname, fontsize,
                                row_color, col_align, col_w)

            log.append(f"✅ Rendered row {i+1}: {name} × {qty} @ {price} = {line_total}")
            current_y += row_height

        # ── STEP 3: Shift totals block up ────────────────────────────────────
        rows_rendered    = len(user_products)
        rows_deleted     = len(product_rows) - rows_rendered
        vertical_savings = rows_deleted * row_height

        if rows_deleted > 0 and totals_block:
            old_totals_y = float(totals_block.get("y_px", 0))
            totals_h     = float(totals_block.get("height_px", 100))
            new_totals_y = old_totals_y - vertical_savings

            # Snapshot totals content BEFORE wiping
            totals_rows = totals_block.get("rows", [])

            # Wipe old totals location
            wipe_totals = pymupdf.Rect(0, old_totals_y - 4,
                                       page_rect.width, old_totals_y + totals_h + 4)
            page.add_redact_annot(wipe_totals, fill=(1, 1, 1))
            page.apply_redactions()

            # Re-render each totals row at shifted position
            for trow in totals_rows:
                t_old_y  = float(trow.get("y_px", old_totals_y))
                t_offset = t_old_y - old_totals_y
                t_new_y  = new_totals_y + t_offset
                t_h      = float(trow.get("height_px", 22))

                # Get font from original (by old y position)
                t_meta   = get_font_at(t_old_y)
                t_font   = t_meta["font"]
                t_size   = t_meta["size"]
                t_color  = pymupdf_color(t_meta["color"])

                lx    = float(trow.get("label_x", 0))
                lw    = float(trow.get("label_width_px", 120))
                vx    = float(trow.get("value_x", 0))
                vw    = float(trow.get("width_px", 100))
                label = str(trow.get("label", ""))
                value = str(trow.get("value", ""))
                align = trow.get("align", "right")

                if label:
                    render_text(lx, t_new_y, label, t_font, t_size, t_color, "left", lw)
                if value:
                    render_text(vx, t_new_y, value, t_font, t_size, t_color, align, vw)

            log.append(f"⬆️ Shifted totals up {vertical_savings:.0f}px "
                       f"({old_totals_y:.0f} → {new_totals_y:.0f})")

    # ── Save as PDF ────────────────────────────────────────────────────────────
    buf = io.BytesIO()
    doc.save(buf, garbage=4, deflate=True)
    doc.close()
    buf.seek(0)
    return buf.read()


# ─── Image → PDF fallback (Pillow + ReportLab-style via PyMuPDF) ─────────────

def image_to_pdf_with_edits(image_b64: str, invoice_map: dict, user_products: list) -> bytes:
    """
    For image inputs: apply edits using Pillow white-box method,
    then convert result to PDF using PyMuPDF.
    Always outputs PDF.
    """
    from PIL import Image, ImageDraw, ImageFont

    img_data = base64.b64decode(image_b64)
    img = Image.open(io.BytesIO(img_data)).convert("RGB")
    draw = ImageDraw.Draw(img)
    img_w, img_h = img.size
    log = []

    def hex_to_rgb(hex_color):
        h = hex_color.lstrip("#")
        if len(h) == 3: h = "".join(c*2 for c in h)
        try: return tuple(int(h[i:i+2],16) for i in (0,2,4))
        except: return (0,0,0)

    def sample_bg(x, y, radius=5):
        pixels = []
        for dx in range(-radius, radius+1):
            for dy in range(-radius, radius+1):
                px, py = x+dx, y+dy
                if 0 <= px < img_w and 0 <= py < img_h:
                    pixels.append(img.getpixel((px,py))[:3])
        if not pixels: return (255,255,255)
        return tuple(sum(p[i] for p in pixels)//len(pixels) for i in range(3))

    def get_font(size=13, bold=False):
        candidates = ([
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ] if bold else [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ])
        for p in candidates:
            if os.path.exists(p):
                try: return ImageFont.truetype(p, size)
                except: continue
        return ImageFont.load_default()

    def render_text_pil(x, y, w, h, text, fsize, bold, tcolor, align):
        font = get_font(fsize, bold)
        try:
            bb = draw.textbbox((0,0), text, font=font)
            tw, th = bb[2]-bb[0], bb[3]-bb[1]
        except: tw, th = len(text)*fsize*0.6, fsize
        ty = y + max(2, (h-th)//2)
        if align == "right": tx = x + w - tw - 2
        elif align == "center": tx = x + (w-tw)//2
        else: tx = x + 2
        draw.text((tx, ty), text, fill=hex_to_rgb(tcolor), font=font)

    # Simple edits
    for edit in invoice_map.get("simple_edits", []):
        new_val = str(edit.get("new_value",""))
        if not new_val: continue
        x,y,w,h = int(edit["x_px"]),int(edit["y_px"]),int(edit["width_px"]),int(edit["height_px"])
        fsize = int(edit.get("font_size",13))
        bold  = bool(edit.get("bold",False))
        tcolor= edit.get("text_color","#333333")
        align = edit.get("align","left")
        bg = sample_bg(min(x+w+10, img_w-1), y+h//2)
        draw.rectangle([x-2,y-2,x+w+4,y+h+4], fill=bg)
        render_text_pil(x,y,w,h,new_val,fsize,bold,tcolor,align)
        log.append(f"✅ Image edit '{edit.get('field')}' → '{new_val}'")

    # Product rows
    product_rows = invoice_map.get("product_rows",[])
    table_header = invoice_map.get("table_header",{})
    totals_block = invoice_map.get("totals_block",{})
    columns = table_header.get("columns",[])
    row_font = invoice_map.get("row_font",{"size":12,"bold":False,"color":"#333333"})

    if product_rows and user_products:
        first_row   = product_rows[0]
        row_height  = int(first_row.get("height_px",26))
        first_row_y = int(first_row.get("y_px",0))
        last_row    = product_rows[-1]
        all_end     = int(last_row["y_px"]) + int(last_row["height_px"])

        bg = sample_bg(img_w-20, first_row_y+5)
        draw.rectangle([0,first_row_y-2,img_w,all_end+2], fill=bg)

        current_y = first_row_y
        for i, prod in enumerate(user_products):
            name  = str(prod.get("Product_Name","")).strip()
            qty   = str(prod.get("Quantity","")).strip()
            price = str(prod.get("Product_Price","")).strip()
            try:
                q = float(re.sub(r'[^\d.]','',qty))
                p = float(re.sub(r'[^\d.]','',price))
                line_total = format_currency(q*p, price)
            except: line_total = ""

            col_values = {
                "description":name,"product":name,"item":name,"name":name,
                "qty":qty,"quantity":qty,"units":qty,
                "unit price":price,"price":price,"rate":price,"cost":price,
                "total":line_total,"amount":line_total,"line total":line_total
            }
            for col in columns:
                lbl = col.get("label","").lower()
                cx,cw,ca = int(col.get("x_px",0)),int(col.get("width_px",100)),col.get("align","left")
                val = next((v for k,v in col_values.items() if k in lbl), "")
                if val:
                    render_text_pil(cx,current_y,cw,row_height,val,
                                    int(row_font.get("size",12)),
                                    bool(row_font.get("bold",False)),
                                    row_font.get("color","#333333"),ca)
            log.append(f"✅ Row {i+1}: {name}")
            current_y += row_height

        rows_deleted = len(product_rows) - len(user_products)
        if rows_deleted > 0 and totals_block:
            old_y  = int(totals_block.get("y_px",0))
            t_h    = int(totals_block.get("height_px",100))
            new_y  = old_y - rows_deleted * row_height
            t_font = totals_block.get("font",{"size":12,"bold":False,"color":"#333333"})

            draw.rectangle([0,old_y-4,img_w,old_y+t_h+4], fill=bg)
            for trow in totals_block.get("rows",[]):
                t_old = int(trow.get("y_px",old_y))
                t_new = new_y + (t_old - old_y)
                th    = int(trow.get("height_px",22))
                lx,lw = int(trow.get("label_x",0)),int(trow.get("label_width_px",120))
                vx,vw = int(trow.get("value_x",0)),int(trow.get("width_px",100))
                bold  = bool(trow.get("bold", t_font.get("bold",False)))
                fsize = int(trow.get("font_size", t_font.get("size",12)))
                color = trow.get("color", t_font.get("color","#333333"))
                if trow.get("label"):
                    render_text_pil(lx,t_new,lw,th,trow["label"],fsize,bold,color,"left")
                if trow.get("value"):
                    render_text_pil(vx,t_new,vw,th,trow["value"],fsize,bold,color,trow.get("align","right"))
            log.append(f"⬆️ Shifted totals up {rows_deleted*row_height}px")

    # Convert edited image to PDF using PyMuPDF
    img_buf = io.BytesIO()
    img.save(img_buf, format="PNG")
    img_buf.seek(0)

    pdf_doc = pymupdf.open()
    img_doc = pymupdf.open(stream=img_buf.read(), filetype="png")
    pdfbytes = img_doc.convert_to_pdf()
    img_doc.close()

    result_doc = pymupdf.open("pdf", pdfbytes)
    pdf_buf = io.BytesIO()
    result_doc.save(pdf_buf, garbage=4, deflate=True)
    result_doc.close()
    pdf_buf.seek(0)
    return pdf_buf.read(), log


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "invoice-editor-final", "engine": "PyMuPDF"})


@app.route("/edit", methods=["POST"])
def edit():
    """
    POST /edit
    {
      "pdf_base64":    "<base64 PDF bytes>",       // for PDF inputs
      "image_base64":  "<base64 image bytes>",     // for image inputs
      "file_type":     "pdf" | "image",            // which one to use
      "invoice_map":   { ...Claude's structural analysis... },
      "user_products": [ {Product_Name, Quantity, Product_Price}, ... ]
    }
    Returns: { success, pdf_base64, log }
    """
    try:
        body = request.get_json(force=True)
        if not body:
            return jsonify({"success": False, "error": "No JSON body"}), 400

        file_type     = body.get("file_type", "image")
        invoice_map   = body.get("invoice_map", {})
        user_products = body.get("user_products", [])

        if not invoice_map:
            return jsonify({"success": False, "error": "Missing invoice_map"}), 400

        print(f"\n📄 Processing {file_type} invoice with {len(user_products)} product(s)...")

        if file_type == "pdf":
            pdf_b64 = body.get("pdf_base64", "")
            if "," in pdf_b64:
                pdf_b64 = pdf_b64.split(",",1)[1]
            if not pdf_b64:
                return jsonify({"success": False, "error": "Missing pdf_base64"}), 400

            pdf_bytes = base64.b64decode(pdf_b64)
            result_bytes = edit_pdf(pdf_bytes, invoice_map, user_products)
            log = ["PDF edited with PyMuPDF — exact font matching"]

        else:
            # Image input → Pillow edit → convert to PDF
            img_b64 = body.get("image_base64", "")
            if "," in img_b64:
                img_b64 = img_b64.split(",",1)[1]
            if not img_b64:
                return jsonify({"success": False, "error": "Missing image_base64"}), 400

            result_bytes, log = image_to_pdf_with_edits(img_b64, invoice_map, user_products)

        result_b64 = base64.b64encode(result_bytes).decode("utf-8")
        print(f"✅ Done. Output: {len(result_bytes)} bytes PDF")

        return jsonify({
            "success":    True,
            "pdf_base64": result_b64,
            "log":        log,
            "output_format": "pdf"
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/preview", methods=["POST"])
def preview():
    """Same as /edit but returns PDF file directly for browser testing."""
    try:
        body          = request.get_json(force=True)
        file_type     = body.get("file_type","image")
        invoice_map   = body.get("invoice_map",{})
        user_products = body.get("user_products",[])

        if file_type == "pdf":
            pdf_b64 = body.get("pdf_base64","")
            if "," in pdf_b64: pdf_b64 = pdf_b64.split(",",1)[1]
            result_bytes = edit_pdf(base64.b64decode(pdf_b64), invoice_map, user_products)
        else:
            img_b64 = body.get("image_base64","")
            if "," in img_b64: img_b64 = img_b64.split(",",1)[1]
            result_bytes, _ = image_to_pdf_with_edits(img_b64, invoice_map, user_products)

        return send_file(
            io.BytesIO(result_bytes),
            mimetype="application/pdf",
            as_attachment=True,
            download_name="edited_invoice.pdf"
        )
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 Invoice Editor FINAL on port {port} | Engine: PyMuPDF {pymupdf.__version__}")
    app.run(host="0.0.0.0", port=port, debug=False)
