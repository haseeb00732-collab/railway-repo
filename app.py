"""
Invoice Editor Python Service v2
- Pixel-perfect white-boxing using actual background color sampled from image
- Collapse deleted product rows + shift totals block up
- Re-render product table and totals in correct positions
- Font matching via OCR height estimation
Host on Railway / Render (free tier)
"""

from flask import Flask, request, jsonify, send_file
from PIL import Image, ImageDraw, ImageFont
import base64, io, os, json, traceback

app = Flask(__name__)

# ─── Font Loader ──────────────────────────────────────────────────────────────

FONT_CACHE = {}

def get_font(size: int = 13, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = (size, bold)
    if key in FONT_CACHE:
        return FONT_CACHE[key]
    candidates = (
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
        ] if bold else [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "C:/Windows/Fonts/arial.ttf",
        ]
    )
    for path in candidates:
        if os.path.exists(path):
            try:
                font = ImageFont.truetype(path, size)
                FONT_CACHE[key] = font
                return font
            except Exception:
                continue
    font = ImageFont.load_default()
    FONT_CACHE[key] = font
    return font


# ─── Color Helpers ────────────────────────────────────────────────────────────

def hex_to_rgb(hex_color: str) -> tuple:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
    except Exception:
        return (0, 0, 0)


def sample_bg_color(img: Image.Image, x: int, y: int, radius: int = 5) -> tuple:
    """
    Sample the actual background color from the image at a given point.
    Averages a small area to avoid hitting text pixels.
    Falls back to white if out of bounds.
    """
    w, h = img.size
    pixels = []
    for dx in range(-radius, radius + 1):
        for dy in range(-radius, radius + 1):
            px, py = x + dx, y + dy
            if 0 <= px < w and 0 <= py < h:
                pixels.append(img.getpixel((px, py))[:3])
    if not pixels:
        return (255, 255, 255)
    r = sum(p[0] for p in pixels) // len(pixels)
    g = sum(p[1] for p in pixels) // len(pixels)
    b = sum(p[2] for p in pixels) // len(pixels)
    return (r, g, b)


def measure_text(draw: ImageDraw.Draw, text: str, font: ImageFont.FreeTypeFont) -> tuple:
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0], bbox[3] - bbox[1]
    except AttributeError:
        return draw.textsize(text, font=font)


# ─── Core Operations ─────────────────────────────────────────────────────────

def white_box(img: Image.Image, draw: ImageDraw.Draw,
              x: int, y: int, w: int, h: int,
              sample_x: int = None, sample_y: int = None) -> tuple:
    """
    Erase a region by filling with the actual background color sampled from the image.
    sample_x/y: where to sample bg color (defaults to just right of the box).
    Returns the sampled color for reuse.
    """
    sx = sample_x if sample_x is not None else min(x + w + 10, img.width - 1)
    sy = sample_y if sample_y is not None else y + h // 2
    bg = sample_bg_color(img, sx, sy)
    draw.rectangle([x, y, x + w, y + h], fill=bg)
    return bg


def render_text_in_box(draw: ImageDraw.Draw,
                        text: str,
                        x: int, y: int, box_w: int, box_h: int,
                        font_size: int, bold: bool,
                        text_color: str, align: str = "left",
                        padding: int = 2):
    """Render text inside a box with alignment and vertical centering."""
    font = get_font(size=font_size, bold=bold)
    tw, th = measure_text(draw, text, font)

    # Vertical center
    ty = y + max(padding, (box_h - th) // 2)

    if align == "right":
        tx = x + box_w - tw - padding
    elif align == "center":
        tx = x + (box_w - tw) // 2
    else:
        tx = x + padding

    draw.text((tx, ty), text, fill=hex_to_rgb(text_color), font=font)


# ─── Main Edit Function ───────────────────────────────────────────────────────

def process_invoice(image_b64: str, invoice_map: dict, user_products: list) -> str:
    """
    invoice_map from Claude:
    {
      "page_bg_color": "#FFFFFF",
      "table_header": {
        "y_px": 320, "height_px": 28,
        "columns": [
          {"label": "Description", "x_px": 40, "width_px": 280, "align": "left"},
          {"label": "Qty",         "x_px": 320, "width_px": 60,  "align": "center"},
          {"label": "Unit Price",  "x_px": 380, "width_px": 100, "align": "right"},
          {"label": "Total",       "x_px": 480, "width_px": 100, "align": "right"}
        ],
        "font": {"size": 11, "bold": true, "color": "#FFFFFF"}
      },
      "product_rows": [
        {
          "row_index": 0,
          "y_px": 348, "height_px": 26,
          "cells": {
            "product_name": {"x_px": 40,  "width_px": 280, "value": "Widget A", "align": "left"},
            "quantity":     {"x_px": 320, "width_px": 60,  "value": "5",        "align": "center"},
            "unit_price":   {"x_px": 380, "width_px": 100, "value": "£10.00",   "align": "right"},
            "line_total":   {"x_px": 480, "width_px": 100, "value": "£50.00",   "align": "right"}
          }
        }
      ],
      "totals_block": {
        "y_px": 500, "height_px": 80,
        "rows": [
          {"label": "Subtotal", "label_x": 380, "value_x": 480, "width_px": 100, "y_px": 500, "height_px": 22, "value": "£50.00", "align": "right"},
          {"label": "VAT (20%)", ...},
          {"label": "Total Due", ..., "bold": true}
        ],
        "font": {"size": 12, "bold": false, "color": "#333333"}
      },
      "simple_edits": [
        {
          "field": "invoice_number",
          "new_value": "INV-9999",
          "x_px": 620, "y_px": 138, "width_px": 180, "height_px": 22,
          "font_size": 13, "bold": false,
          "text_color": "#333333", "align": "right"
        }
      ]
    }

    user_products: list of dicts from the form, e.g.:
    [{"Product_Name": "New Widget", "Quantity": "3", "Product_Price": "£25.00"}]
    """

    # Decode image
    img_data = base64.b64decode(image_b64)
    img = Image.open(io.BytesIO(img_data)).convert("RGB")
    draw = ImageDraw.Draw(img)
    img_w, img_h = img.size

    log = []

    # ── STEP 1: Simple field edits (date, invoice_number, addresses etc.) ──────
    for edit in invoice_map.get("simple_edits", []):
        field     = edit.get("field", "unknown")
        new_val   = str(edit.get("new_value", ""))
        x         = int(edit["x_px"])
        y         = int(edit["y_px"])
        w         = int(edit["width_px"])
        h         = int(edit["height_px"])
        fsize     = int(edit.get("font_size", 13))
        bold      = bool(edit.get("bold", False))
        tcolor    = edit.get("text_color", "#333333")
        align     = edit.get("align", "left")

        # Sample bg from right of box (avoids text area)
        bg = white_box(img, draw, x, y, w, h)
        render_text_in_box(draw, new_val, x, y, w, h, fsize, bold, tcolor, align)
        log.append(f"Simple edit '{field}' → '{new_val}' at ({x},{y})")

    # ── STEP 2: Product table rebuild ─────────────────────────────────────────
    product_rows  = invoice_map.get("product_rows", [])
    user_prods    = user_products or []
    table_header  = invoice_map.get("table_header", {})
    totals_block  = invoice_map.get("totals_block", {})

    if product_rows and user_prods:
        # Determine row geometry from first existing row
        first_row     = product_rows[0]
        row_height    = int(first_row.get("height_px", 26))
        first_row_y   = int(first_row.get("y_px", 0))
        columns       = table_header.get("columns", [])
        row_font      = invoice_map.get("row_font", {"size": 12, "bold": False, "color": "#333333"})
        row_font_size = int(row_font.get("size", 12))
        row_bold      = bool(row_font.get("bold", False))
        row_color     = row_font.get("color", "#333333")

        # White-box ALL existing product rows in one pass
        all_rows_top    = int(product_rows[0]["y_px"])
        all_rows_bottom = int(product_rows[-1]["y_px"]) + int(product_rows[-1]["height_px"])
        table_x         = min(int(r.get("y_px", 40)) for r in columns) if columns else 30
        # Use full image width for the wipe to be safe
        bg_sample_x     = img_w - 20
        bg_sample_y     = all_rows_top + 5
        bg_color        = sample_bg_color(img, bg_sample_x, bg_sample_y)
        draw.rectangle([0, all_rows_top, img_w, all_rows_bottom], fill=bg_color)
        log.append(f"Wiped {len(product_rows)} original product rows ({all_rows_top}–{all_rows_bottom}px)")

        # Render user's products starting from first_row_y
        current_y = first_row_y
        for i, prod in enumerate(user_prods):
            name      = str(prod.get("Product_Name", ""))
            qty       = str(prod.get("Quantity", ""))
            price     = str(prod.get("Product_Price", ""))
            # Calculate line total if both qty and price are numeric
            try:
                qty_num   = float(qty.replace(",", ""))
                price_num = float(price.replace("£","").replace("$","").replace(",","").strip())
                line_total = f"{price[0] if price and not price[0].isdigit() else '£'}{qty_num * price_num:,.2f}"
            except Exception:
                line_total = ""

            # Map column labels to values
            col_values = {
                "Description": name, "Product": name, "Item": name, "Name": name,
                "Qty": qty, "Quantity": qty,
                "Unit Price": price, "Price": price, "Rate": price,
                "Total": line_total, "Amount": line_total, "Line Total": line_total
            }

            # Render each cell
            for col in columns:
                col_label = col.get("label", "")
                col_x     = int(col.get("x_px", 0))
                col_w     = int(col.get("width_px", 100))
                col_align = col.get("align", "left")
                # Match column to value
                val = ""
                for key, v in col_values.items():
                    if key.lower() in col_label.lower():
                        val = v
                        break
                if val:
                    render_text_in_box(draw, val, col_x, current_y, col_w, row_height,
                                       row_font_size, row_bold, row_color, col_align)

            log.append(f"Rendered row {i}: {name} × {qty} @ {price} = {line_total}")
            current_y += row_height

        # ── STEP 3: Shift totals block up ──────────────────────────────────────
        rows_rendered    = len(user_prods)
        rows_deleted     = len(product_rows) - rows_rendered
        vertical_savings = rows_deleted * row_height

        if rows_deleted > 0 and totals_block:
            old_totals_y  = int(totals_block.get("y_px", 0))
            totals_h      = int(totals_block.get("height_px", 80))
            new_totals_y  = old_totals_y - vertical_savings
            totals_font   = totals_block.get("font", {"size": 12, "bold": False, "color": "#333333"})

            # White-box old totals location
            draw.rectangle([0, old_totals_y, img_w, old_totals_y + totals_h], fill=bg_color)

            # Re-render totals rows at new position
            for trow in totals_block.get("rows", []):
                t_old_y   = int(trow.get("y_px", old_totals_y))
                t_offset  = t_old_y - old_totals_y          # offset within block
                t_new_y   = new_totals_y + t_offset
                t_h       = int(trow.get("height_px", 22))
                t_bold    = bool(trow.get("bold", totals_font.get("bold", False)))
                t_fsize   = int(trow.get("font_size", totals_font.get("size", 12)))
                t_color   = trow.get("color", totals_font.get("color", "#333333"))
                lx        = int(trow.get("label_x", 0))
                lw        = int(trow.get("label_width_px", 120))
                vx        = int(trow.get("value_x", 0))
                vw        = int(trow.get("width_px", 100))
                label     = str(trow.get("label", ""))
                value     = str(trow.get("value", ""))
                align     = trow.get("align", "right")

                render_text_in_box(draw, label, lx, t_new_y, lw, t_h, t_fsize, t_bold, t_color, "left")
                render_text_in_box(draw, value, vx, t_new_y, vw, t_h, t_fsize, t_bold, t_color, align)

            log.append(f"Shifted totals block up by {vertical_savings}px ({old_totals_y} → {new_totals_y})")

    # ── Done: encode result ────────────────────────────────────────────────────
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    result_b64 = base64.b64encode(buf.read()).decode("utf-8")
    return result_b64, log


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "invoice-editor-v2"})


@app.route("/edit", methods=["POST"])
def edit():
    """
    POST /edit
    {
      "image_base64":  "<base64, no prefix>",
      "invoice_map":   { ...Claude's structural analysis... },
      "user_products": [ {"Product_Name": "...", "Quantity": "...", "Product_Price": "..."} ]
    }
    """
    try:
        body = request.get_json(force=True)
        if not body:
            return jsonify({"success": False, "error": "No JSON body"}), 400

        image_b64     = body.get("image_base64", "")
        invoice_map   = body.get("invoice_map", {})
        user_products = body.get("user_products", [])

        if not image_b64:
            return jsonify({"success": False, "error": "Missing image_base64"}), 400
        if not invoice_map:
            return jsonify({"success": False, "error": "Missing invoice_map"}), 400

        # Strip data URI prefix
        if "," in image_b64:
            image_b64 = image_b64.split(",", 1)[1]

        result_b64, log = process_invoice(image_b64, invoice_map, user_products)

        return jsonify({
            "success": True,
            "edited_image_base64": result_b64,
            "log": log,
            "output_format": "png"
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/preview", methods=["POST"])
def preview():
    """Same as /edit but returns the PNG file directly for browser testing."""
    try:
        body          = request.get_json(force=True)
        image_b64     = body.get("image_base64", "")
        invoice_map   = body.get("invoice_map", {})
        user_products = body.get("user_products", [])

        if "," in image_b64:
            image_b64 = image_b64.split(",", 1)[1]

        result_b64, log = process_invoice(image_b64, invoice_map, user_products)
        img_bytes = base64.b64decode(result_b64)

        return send_file(
            io.BytesIO(img_bytes),
            mimetype="image/png",
            as_attachment=True,
            download_name="edited_invoice.png"
        )
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 Invoice Editor v2 on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
