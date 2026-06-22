"""
Invoice Editor Service - v6 PRODUCTION
========================================
What's new vs v5:
  - SMART PAGE FIT: calculates available space before push-down, returns
    clear error if new products won't fit instead of silently wiping footer
  - SAFE PUSH-DOWN: only moves the totals zone (text between last product
    row and footer), never touches drawings/images/footer boxes
  - FONT MATCHING: reads actual font+size from existing product rows and
    reuses them exactly — no more Helvetica substitution on non-Helvetica invoices
  - GRAND TOTAL FIX: picks LARGEST number in lower half (not rightmost),
    fixes wrong detection when product row totals appear further right
  - ADDRESS FIX: anchor-based extraction off Customer/Delivery label
    positions — no more supplier header text bleeding into billing address
  - ERASE ZONE FIX: stops erase rect before grand total row to prevent
    wiping it before update runs
  - /validate endpoint: pre-flight check returns page fit info without editing
  - /health shows all v6 capabilities
"""

from flask import Flask, request, jsonify, send_file
import pymupdf
import base64, io, os, re, traceback
from collections import defaultdict

app = Flask(__name__)

# ─── Comprehensive font map ────────────────────────────────────────────────────
# Maps PDF/OTF/TTF font name fragments → PyMuPDF built-in name
FONT_MAP = {
    # Helvetica / Arial family
    "helvetica":        "helv",
    "helveticaneue":    "helv",
    "helveticabd":      "hebo",
    "helvetica-bold":   "hebo",
    "helvetica-oblique":"heit",
    "arial":            "helv",
    "arialmt":          "helv",
    "arial-bold":       "hebo",
    "arial-boldmt":     "hebo",
    "arialnarrow":      "helv",
    "arialhebrew":      "helv",
    # Times / Serif family
    "times":            "tiro",
    "timesnewroman":    "tiro",
    "times-roman":      "tiro",
    "timesbold":        "tibo",
    "times-bold":       "tibo",
    "timesnewromanbd":  "tibo",
    "timesnewroman-bold":"tibo",
    "timesnewromanps":  "tiro",
    "georgia":          "tiro",
    "georgia-bold":     "tibo",
    "garamond":         "tiro",
    "bookman":          "tiro",
    "palatino":         "tiro",
    # Courier / Mono family
    "courier":          "cour",
    "courier-bold":     "cobo",
    "couriernew":       "cour",
    "couriernewbd":     "cobo",
    "lucidaconsole":    "cour",
    "consolasbold":     "cobo",
    "consolas":         "cour",
    # Office / Web fonts
    "calibri":          "helv",
    "calibri-bold":     "hebo",
    "calibribd":        "hebo",
    "calibril":         "helv",
    "cambria":          "tiro",
    "cambriabold":      "tibo",
    "verdana":          "helv",
    "verdanabold":      "hebo",
    "verdanabd":        "hebo",
    "tahoma":           "helv",
    "tahomabd":         "hebo",
    "trebuchet":        "helv",
    "trebuchetbd":      "hebo",
    "segoeui":          "helv",
    "segoeuibd":        "hebo",
    "segoeuibold":      "hebo",
    "franklingothic":   "helv",
    "centuryschoolbook":"tiro",
    "bookantiqua":      "tiro",
    # Google Fonts common
    "roboto":           "helv",
    "robotobold":       "hebo",
    "opensans":         "helv",
    "opensansbold":     "hebo",
    "lato":             "helv",
    "latobold":         "hebo",
    "montserrat":       "helv",
    "montserratbold":   "hebo",
    "sourcesanspro":    "helv",
    "notoserif":        "tiro",
    "notosans":         "helv",
    "ptsans":           "helv",
    "ptserif":          "tiro",
    "raleway":          "helv",
    "oswald":           "helv",
    "ubuntu":           "helv",
    "nunito":           "helv",
    # Adobe / CID fonts
    "myriadpro":        "helv",
    "myriadprobd":      "hebo",
    "myriadprobold":    "hebo",
    "futurapt":         "helv",
    "futura":           "helv",
    "gillsans":         "helv",
    "gillsansbd":       "hebo",
    "trajanpro":        "tiro",
    "minion":           "tiro",
    "minionpro":        "tiro",
    # CID embedded (common in supplier invoices)
    "cidfont":          "helv",
    "cidfonts":         "helv",
    # Symbol / Dingbats
    "symbol":           "helv",
    "zapfdingbats":     "helv",
}

def map_font(name: str) -> str:
    """Map any PDF font name to closest PyMuPDF built-in."""
    if not name:
        return "helv"
    c = (name.lower()
         .replace(" ", "").replace(",", "").replace("+", "")
         .replace("-", "").replace("_", "").replace(".", ""))
    # Bold check first
    if any(b in c for b in ("bold", "heavy", "black", "semibold", "demi")):
        # Check if it's a known bold mapping
        for k, v in FONT_MAP.items():
            if k in c:
                return v  # already mapped to bold variant above
        return "hebo"
    for k, v in FONT_MAP.items():
        if k in c:
            return v
    return "helv"


# ─── Table header keywords (multilingual) ────────────────────────────────────
HEADER_KW = {
    # English
    "sku","product","description","desc","item","name","details",
    "status","unit","price","qty","quantity","units","subtotal",
    "sub","vat","tax","total","amount","rate","cost","charge",
    # German
    "artikel","bezeichnung","menge","betrag","steuer","preis","gesamt",
    # French
    "article","désignation","quantité","montant","taxe","prix","total",
    # Spanish
    "artículo","descripción","cantidad","importe","impuesto","precio",
    # Italian
    "articolo","descrizione","quantità","importo","imposta","prezzo",
    # Dutch
    "artikel","omschrijving","aantal","bedrag","btw","prijs","totaal",
    # Polish
    "towar","opis","ilość","wartość","podatek","cena","razem",
}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def nearest_span(page, rect) -> dict:
    cy = (rect.y0 + rect.y1) / 2
    best, best_d = {"font": "helv", "size": 8.0, "color": 0}, 9999
    for block in page.get_text("dict", flags=0)["blocks"]:
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                sy = (span["bbox"][1] + span["bbox"][3]) / 2
                d  = abs(sy - cy)
                if d < best_d:
                    best_d = d
                    best   = span
    return best


def rgb(color_int: int) -> tuple:
    return (((color_int >> 16) & 0xFF) / 255,
            ((color_int >>  8) & 0xFF) / 255,
            ( color_int        & 0xFF) / 255)


def fmt(value: float, sym: str = "£", before: bool = True, eu: bool = False) -> str:
    """Format number matching invoice currency style."""
    if eu:
        s = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    else:
        s = f"{value:,.2f}"
    return (sym + s) if before else (s + sym)


def detect_pdf_type(page) -> str:
    """Returns 'digital', 'scanned', or 'minimal'."""
    text = page.get_text("text").strip()
    if len(text) < 50:
        return "scanned"
    if len(text) < 200:
        return "minimal"
    return "digital"


def get_all_spans(page) -> list:
    spans = []
    for block in page.get_text("dict", flags=0)["blocks"]:
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                t = span["text"].strip()
                if t:
                    spans.append({
                        "text":  t,
                        "x0":    span["bbox"][0],
                        "y0":    span["bbox"][1],
                        "x1":    span["bbox"][2],
                        "y1":    span["bbox"][3],
                        "size":  span["size"],
                        "font":  span["font"],
                        "color": span["color"],
                    })
    return spans


# ─── Text replacement ─────────────────────────────────────────────────────────

def replace_text(page, old: str, new: str,
                 x_min=None, x_max=None, y_min=None, y_max=None,
                 occurrence=0) -> bool:
    """Find old text in PDF, redact, insert new text with same font."""
    if not old or not old.strip():
        return False

    rects = page.search_for(old)

    # Fallback: try first 4 words if full string not found
    if not rects:
        short = " ".join(old.split()[:4])
        if short != old:
            rects = page.search_for(short)

    # Fallback: try first 3 words
    if not rects:
        short = " ".join(old.split()[:3])
        if short and short != old:
            rects = page.search_for(short)

    if not rects:
        print(f"  ⚠️  Not found: '{old[:50]}'")
        return False

    # Filter by position
    if x_min is not None: rects = [r for r in rects if r.x0 >= x_min]
    if x_max is not None: rects = [r for r in rects if r.x0 <= x_max]
    if y_min is not None: rects = [r for r in rects if r.y0 >= y_min]
    if y_max is not None: rects = [r for r in rects if r.y0 <= y_max]

    if not rects:
        print(f"  ⚠️  Not found in bounds: '{old[:50]}'")
        return False

    targets = ([rects[-1]] if occurrence == -1 else
               rects       if occurrence is None else
               [rects[min(occurrence, len(rects) - 1)]])

    for rect in targets:
        sp = nearest_span(page, rect)
        fn = map_font(sp["font"])
        fs = sp["size"]
        fc = rgb(sp["color"])

        new_str = str(new)
        # Erase old text — wide enough for new value too
        erase_w = max(rect.width, len(new_str) * fs * 0.65) + 8
        er = pymupdf.Rect(rect.x0 - 1, rect.y0 - 1,
                          rect.x0 + erase_w, rect.y1 + 1)
        page.add_redact_annot(er, fill=(1, 1, 1))
        page.apply_redactions(graphics=0)  # graphics=0 preserves border lines

        if new_str:
            page.insert_text((rect.x0, rect.y1 - 1), new_str,
                             fontname=fn, fontsize=fs, color=fc)
        print(f"  ✅ '{old[:35]}' → '{new_str[:35]}'")
    return True


# ─── Address replacement (text-search based, not x-position) ─────────────────

def replace_address(page, old_lines: list, new_text: str,
                    x_min=None, x_max=None):
    """
    Replace address lines by searching for each old line text.
    Uses optional x bounds only to disambiguate billing vs shipping.
    """
    new_lines = [l.strip() for l in
                 str(new_text).replace("\\n", "\n").split("\n")
                 if l.strip()]

    for i, old_line in enumerate(old_lines):
        ol = old_line.strip()
        if not ol:
            continue
        nl = new_lines[i] if i < len(new_lines) else ""
        replace_text(page, ol, nl, x_min=x_min, x_max=x_max, occurrence=0)


# ─── Table detection ──────────────────────────────────────────────────────────

def detect_table(page, all_spans=None) -> dict | None:
    """
    Detect product table. Strategy order:
    1. Span-cluster with keyword header detection
    2. Numeric-column fallback (for tables without standard headers)
    3. PyMuPDF find_tables() for bordered/grid tables
    Returns table_info dict or None.
    """
    if all_spans is None:
        all_spans = get_all_spans(page)

    result = _span_cluster_detect(page, all_spans)
    if result:
        return result

    result = _numeric_column_detect(page, all_spans)
    if result:
        return result

    return _find_tables_fallback(page, all_spans)


def _span_cluster_detect(page, all_spans) -> dict | None:
    """Primary: find header row by keyword density, then walk data rows."""
    header_candidates = [
        s for s in all_spans
        if any(kw in s["text"].lower().replace(" ", "")
               for kw in HEADER_KW)
        and len(s["text"]) < 30
    ]

    if len(header_candidates) < 2:
        return None

    # Group by y (5px buckets)
    y_groups = defaultdict(list)
    for hc in header_candidates:
        y_groups[round(hc["y0"] / 5) * 5].append(hc)

    best_y_key = max(y_groups, key=lambda k: len(y_groups[k]))
    if len(y_groups[best_y_key]) < 2:
        return None

    header_y   = y_groups[best_y_key][0]["y0"]
    col_spans  = sorted(y_groups[best_y_key], key=lambda s: s["x0"])

    # Walk data rows below header
    data_spans = [
        s for s in all_spans
        if s["y0"] > header_y + 5
        and "Customer Order Reference" not in s["text"]
        and "order reference" not in s["text"].lower()
    ]

    rows_by_y = defaultdict(list)
    for s in data_spans:
        rows_by_y[round(s["y0"] / 4) * 4].append(s)

    sorted_ys = sorted(rows_by_y.keys())
    if not sorted_ys:
        return None

    # Row height from early gaps
    early_gaps = [sorted_ys[i+1] - sorted_ys[i]
                  for i in range(min(6, len(sorted_ys)-1))
                  if 4 < sorted_ys[i+1] - sorted_ys[i] < 50]
    row_height = (sum(early_gaps) / len(early_gaps)) if early_gaps else 18.0

    # Walk rows, stop on big gap or column collapse
    product_ys = []
    for i, yk in enumerate(sorted_ys):
        row    = rows_by_y[yk]
        n_cols = len(set(round(s["x0"] / 5) * 5 for s in row))
        gap    = (sorted_ys[i+1] - yk) if i+1 < len(sorted_ys) else 999

        if n_cols < 3:
            if product_ys:
                break
            continue

        if product_ys and gap > row_height * 2.5:
            break

        product_ys.append(yk)

    if not product_ys:
        return None

    return _build_table_dict(page, col_spans, rows_by_y, product_ys, row_height, all_spans)


def _numeric_column_detect(page, all_spans) -> dict | None:
    """
    Fallback: find rows that have 3+ numeric values.
    Works for invoices without standard column headers.
    """
    rows_by_y = defaultdict(list)
    for s in all_spans:
        rows_by_y[round(s["y0"] / 4) * 4].append(s)

    NUM_RE = re.compile(r'^\d+([.,]\d+)*$')

    def has_nums(row, threshold=3):
        return sum(1 for s in row if NUM_RE.match(s["text"].replace(",","").replace(".","").replace(" ",""))) >= threshold

    sorted_ys = sorted(rows_by_y.keys())
    product_ys = [yk for yk in sorted_ys if has_nums(rows_by_y[yk])]

    if len(product_ys) < 2:
        return None

    # Find the y just above first numeric row to use as header
    header_y_idx = sorted_ys.index(product_ys[0]) - 1
    header_y     = sorted_ys[header_y_idx] if header_y_idx >= 0 else product_ys[0] - 20
    col_spans    = sorted(rows_by_y.get(header_y, []), key=lambda s: s["x0"])

    if not col_spans:
        # Infer columns from first data row
        col_spans = sorted(rows_by_y[product_ys[0]], key=lambda s: s["x0"])

    early_gaps = [product_ys[i+1] - product_ys[i]
                  for i in range(min(4, len(product_ys)-1))
                  if product_ys[i+1] - product_ys[i] < 50]
    row_height  = (sum(early_gaps) / len(early_gaps)) if early_gaps else 18.0

    print(f"  📊 Numeric-column detect: {len(product_ys)} rows")
    return _build_table_dict(page, col_spans, rows_by_y, product_ys, row_height, all_spans)


def _find_tables_fallback(page, all_spans) -> dict | None:
    """Last resort: PyMuPDF's find_tables() for bordered/grid tables."""
    try:
        tabs = page.find_tables()
        if not tabs.tables:
            return None

        tbl        = max(tabs.tables, key=lambda t: t.row_count * t.col_count)
        row_height = 18.0
        if tbl.row_count > 1 and tbl.rows[0].cells and tbl.rows[0].cells[0]:
            c          = tbl.rows[0].cells[0]
            row_height = c[3] - c[1]

        def cell_text(cell):
            if not cell: return ""
            words = page.get_text("words", clip=pymupdf.Rect(cell))
            return " ".join(w[4] for w in words)

        col_spans = [
            {"text": cell_text(c), "x0": c[0], "y0": c[1],
             "x1": c[2], "y1": c[3], "size": 7.0, "font": "helv", "color": 0}
            for c in tbl.rows[0].cells if c
        ]

        rows_by_y = defaultdict(list)
        data_rows_raw = []
        for row in tbl.rows[1:]:
            for c in row.cells:
                if c:
                    t = cell_text(c)
                    if t.strip():
                        yk = round(c[1] / 4) * 4
                        rows_by_y[yk].append({
                            "text": t, "x0": c[0], "y0": c[1],
                            "x1": c[2], "y1": c[3],
                            "size": 7.0, "font": "helv", "color": 0
                        })

        product_ys = sorted(rows_by_y.keys())
        if not product_ys:
            return None

        print(f"  📊 find_tables fallback: {len(product_ys)} rows")
        return _build_table_dict(page, col_spans, rows_by_y, product_ys, row_height, all_spans)

    except Exception as e:
        print(f"  ⚠️  find_tables error: {e}")
        return None


def _build_table_dict(page, col_spans, rows_by_y, product_ys, row_height, all_spans):
    """Shared structure builder for all detection methods."""
    # Sample font from first data row
    sample_spans = rows_by_y.get(product_ys[0], [])
    sample = next((s for s in sample_spans if s.get("font")), None)
    row_font  = map_font(sample["font"]) if sample else "helv"
    row_size  = sample["size"]           if sample else 7.0
    row_color = rgb(sample["color"])     if sample else (0, 0, 0)

    # Detect original status value from first row
    orig_status = ""
    for s in sample_spans:
        if s["text"].isupper() and 3 < len(s["text"]) < 15:
            orig_status = s["text"]
            break

    print(f"  📊 Table: header_y={col_spans[0]['y0'] if col_spans else 0:.0f}, "
          f"{len(product_ys)} rows, row_h≈{row_height:.1f}, "
          f"cols={[c['text'][:8] for c in col_spans]}")

    return {
        "header_y":    col_spans[0]["y0"] if col_spans else 0,
        "col_spans":   col_spans,
        "row_font":    row_font,
        "row_size":    row_size,
        "row_color":   row_color,
        "row_height":  row_height,
        "data_rows":   [{"y": yk, "spans": rows_by_y[yk]} for yk in product_ys],
        "orig_status": orig_status,
        "all_spans":   all_spans,
    }


# ─── Table rebuild ────────────────────────────────────────────────────────────

def rebuild_table(page, tbl: dict, user_products: list,
                  tax_rate: float, sym: str, before: bool, eu: bool,
                  structure_map: dict = None):
    """
    v6: Wipe original data rows, redraw with user's products.
    - Font/size read from ACTUAL existing row spans (pixel-perfect match)
    - Page fit check before push-down (returns error if won't fit)
    - Safe erase zone (stops before grand total row)
    - Safe push-down (only moves totals zone, protects footer)
    Returns (new_rows, grand_subtotal, grand_vat, grand_total) or
            (None, 0, 0, 0) with error logged on fit failure.
    """
    if not tbl or not tbl["data_rows"]:
        return None, 0, 0, 0

    rows      = tbl["data_rows"]
    rh        = tbl["row_height"]
    col_spans = tbl["col_spans"]
    orig_stat = tbl.get("orig_status", "")
    page_w    = page.rect.width

    # ── v6 FIX: read font/size/color from ACTUAL first data row spans ─────────
    # This ensures we exactly match whatever font the invoice uses in its rows
    # (Helvetica, CIDFont, Calibri, etc.) — not a hardcoded fallback.
    rf = tbl.get("row_font", "helv")
    rs = tbl.get("row_size", 7.0)
    rc = tbl.get("row_color", (0, 0, 0))

    first_row_y = rows[0]["y"]
    all_spans_page = get_all_spans(page)
    first_row_spans = [s for s in all_spans_page
                       if abs(s["y0"] - first_row_y) < rh * 0.8
                       and s["text"].strip()]
    if first_row_spans:
        # Use the most common font in the first data row
        from collections import Counter
        font_counts = Counter(s["font"] for s in first_row_spans)
        actual_font = font_counts.most_common(1)[0][0]
        actual_size = first_row_spans[0]["size"]
        actual_color = rgb(first_row_spans[0]["color"])
        rf = map_font(actual_font)
        rs = actual_size
        rc = actual_color
        print(f"  🎨 Row font: {actual_font} → {rf}  size={rs:.1f}")

    # ── 1. Calculate math ─────────────────────────────────────────────────────
    new_rows   = []
    grand_sub  = 0.0
    grand_vat  = 0.0

    for prod in user_products:
        name    = str(prod.get("Product_Name",  "")).strip()
        qty_s   = str(prod.get("Quantity",       "1")).strip()
        price_s = str(prod.get("Product_Price",  "0")).strip()
        try:
            qty   = float(re.sub(r'[^\d.]', '', qty_s)   or 0)
            price = float(re.sub(r'[^\d.]', '', price_s) or 0)
        except ValueError:
            qty, price = 1.0, 0.0
        sub   = round(qty * price, 2)
        vat   = round(sub * (tax_rate / 100), 2)
        total = round(sub + vat, 2)
        grand_sub += sub
        grand_vat += vat
        new_rows.append({
            "name": name, "qty": qty_s, "qty_num": qty,
            "price": price, "sub": sub, "vat": vat, "total": total,
            "status": orig_stat
        })

    grand_total = round(grand_sub + grand_vat, 2)

    # ── 2. Page fit check BEFORE any edits ───────────────────────────────────
    extra = len(user_products) - len(rows)
    if extra > 0:
        fit = page_fit_check(page, len(rows), len(user_products),
                             rh, rows[-1]["y"])
        print(f"  📐 {fit['message']}")
        if not fit["fits"]:
            # Return error — caller will log this and skip table edit
            return None, 0, 0, 0

        push = extra * rh
        _push_totals_down(page, rows[-1]["y"] + rh + 2, push)

    # ── 3. Erase original data rows (stop before grand total row) ────────────
    y0_erase = rows[0]["y"] - 2
    # v6 FIX: use rh - 5 not rh + 4 so we stop before the grand total row
    # which can sit immediately below the last product row
    y1_erase = rows[-1]["y"] + rh - 5
    page.add_redact_annot(
        pymupdf.Rect(20, y0_erase, page_w - 20, y1_erase),
        fill=(1, 1, 1)
    )
    page.apply_redactions(graphics=0)
    print(f"  🧹 Erased {len(rows)} rows (y={y0_erase:.0f}→{y1_erase:.0f})")

    # ── 4. Build column bounding boxes ────────────────────────────────────────
    n = len(col_spans)
    col_rects = []
    for i, cs in enumerate(col_spans):
        x_left  = cs["x0"]
        x_right = (col_spans[i+1]["x0"] - 1) if i+1 < n else page_w - 20
        label   = cs["text"].lower().replace(" ", "").replace(".", "")
        col_rects.append((x_left, x_right, label))

    # ── 5. Draw new rows ──────────────────────────────────────────────────────
    cur_y = rows[0]["y"]

    for ri, row in enumerate(new_rows):
        y0 = cur_y
        y1 = y0 + rh

        for xl, xr, label in col_rects:
            # Map label → value + alignment
            if   any(k in label for k in ("descr","product","item","name","bezeich","artíc","artik","artíc","omschr","towar")):
                text, align = row["name"], 0
            elif any(k in label for k in ("sku","code","ref","no","num","artikelnr")):
                text, align = "", 0         # leave blank — don't invent SKUs
            elif any(k in label for k in ("stat","taken","status")):
                text, align = row["status"], 1  # center
            elif any(k in label for k in ("unit","price","rate","preis","prijs","prix","precio","prezzo","cena")) \
                 and not any(k in label for k in ("total","sub","amount")):
                text, align = fmt(row["price"], sym, before, eu), 2
            elif any(k in label for k in ("qty","quan","menge","antal","ilość","amount","units","qté","cant")):
                text, align = row["qty"], 2
            elif any(k in label for k in ("sub","subtot","netto","net")):
                text, align = fmt(row["sub"],   sym, before, eu), 2
            elif any(k in label for k in ("vat","tax","steuer","btw","taxe","iva","imposta","podatek","moms")):
                text, align = fmt(row["vat"],   sym, before, eu), 2
            elif any(k in label for k in ("total","amount","gesamt","bedrag","montant","importe","importo","razem","betrag")):
                text, align = fmt(row["total"], sym, before, eu), 2
            else:
                text, align = "", 0

            if text:
                tb_rect = pymupdf.Rect(xl + 2, y0 + 1, xr - 2, y1 - 1)
                page.insert_textbox(
                    tb_rect, text,
                    fontname=rf, fontsize=rs, color=rc,
                    align=align
                )

        print(f"  📝 Row {ri+1}: {row['name'][:35]} | "
              f"qty={row['qty']} × £{row['price']:.2f} = "
              f"sub:{fmt(row['sub'],sym,before,eu)} "
              f"vat:{fmt(row['vat'],sym,before,eu)} "
              f"total:{fmt(row['total'],sym,before,eu)}")
        cur_y += rh

    return new_rows, grand_sub, grand_vat, grand_total


def _find_footer_y(page) -> float:
    """
    Find the y-coordinate where the footer begins (first drawing/image/text
    that is clearly NOT part of the totals section).
    Returns page height if no footer detected.
    """
    page_h = page.rect.height
    page_w = page.rect.width

    # Drawings below y=400 that span the full width are footer borders
    drawings = page.get_drawings()
    footer_draws = [d for d in drawings
                    if d["rect"][1] > page_h * 0.6
                    and d["rect"][2] - d["rect"][0] > page_w * 0.5]
    if footer_draws:
        return min(d["rect"][1] for d in footer_draws)

    # Images (logos, QR codes) in lower 40% of page
    images = page.get_image_info()
    footer_imgs = [img for img in images
                   if img["bbox"][1] > page_h * 0.6]
    if footer_imgs:
        return min(img["bbox"][1] for img in footer_imgs)

    return page_h  # no footer found — whole page available


def page_fit_check(page, current_row_count: int, new_row_count: int,
                   row_height: float, last_row_y: float) -> dict:
    """
    Before pushing: calculate if new rows fit without hitting the footer.
    Returns dict with 'fits' bool, 'available_rows', 'message'.
    """
    footer_y   = _find_footer_y(page)
    extra_rows = new_row_count - current_row_count
    push_need  = extra_rows * row_height

    # Space between current last row and footer
    space_after_table = footer_y - (last_row_y + row_height)
    max_extra          = int(space_after_table / row_height)

    fits = extra_rows <= max_extra
    return {
        "fits":            fits,
        "extra_rows":      extra_rows,
        "max_extra_rows":  max_extra,
        "footer_y":        footer_y,
        "push_needed_px":  push_need,
        "space_px":        space_after_table,
        "message": (
            f"✅ Fits: {extra_rows} extra rows need {push_need:.0f}px, "
            f"{space_after_table:.0f}px available before footer."
        ) if fits else (
            f"❌ Won't fit: {extra_rows} extra rows need {push_need:.0f}px "
            f"but only {space_after_table:.0f}px available before footer "
            f"(max {max_extra} extra rows on this invoice)."
        )
    }


def _push_totals_down(page, below_y: float, push_amount: float):
    """
    v6: SAFE push-down — only moves the totals zone between the last product
    row and the footer. Never erases drawings, images, or footer content.
    Only text in the totals zone is moved.
    """
    page_w   = page.rect.width
    footer_y = _find_footer_y(page)

    # Only operate on the gap between last product row and footer
    zone_top = below_y
    zone_bot = footer_y - 2  # stay above footer

    if zone_top >= zone_bot:
        print(f"  ⚠️  No totals zone found (below_y={below_y:.0f} >= footer_y={footer_y:.0f})")
        return

    # Collect only text spans inside the totals zone
    clip   = pymupdf.Rect(0, zone_top, page_w, zone_bot)
    blocks = page.get_text("dict", clip=clip, flags=0)["blocks"]

    to_move = []
    for block in blocks:
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                if zone_top <= span["bbox"][1] < zone_bot:
                    to_move.append(span)

    if not to_move:
        print(f"  ℹ️  No text in totals zone to push")
        return

    # Erase ONLY the totals zone text (graphics=0 keeps borders/lines)
    page.add_redact_annot(
        pymupdf.Rect(0, zone_top, page_w, zone_bot),
        fill=(1, 1, 1)
    )
    page.apply_redactions(graphics=0)

    # Re-render at shifted positions
    for span in to_move:
        fn    = map_font(span["font"])
        fs    = span["size"]
        fc    = rgb(span["color"])
        new_y = span["bbox"][3] + push_amount - 1
        # Safety: don't push text past the footer
        if new_y < footer_y - 5:
            page.insert_text(
                (span["bbox"][0], new_y),
                span["text"],
                fontname=fn, fontsize=fs, color=fc
            )

    print(f"  ⬇️  Pushed {len(to_move)} totals-zone elements down {push_amount:.0f}px "
          f"(footer protected at y={footer_y:.0f})")


# ─── Grand total update ───────────────────────────────────────────────────────

def update_grand_total(page, old_str: str, new_val: float,
                       sym: str, before: bool, eu: bool,
                       all_spans: list = None):
    """
    Replace ALL occurrences of grand total (table footer + payment section).
    Tries multiple formats: with/without currency symbol, with/without commas.
    """
    new_fmt   = fmt(new_val, sym, before, eu)
    new_clean = re.sub(r'[^\d.,]', '', new_fmt)

    replaced = False

    if old_str:
        old_clean = re.sub(r'[^\d.,]', '', old_str)
        if old_clean:
            if replace_text(page, old_clean, new_clean, occurrence=None):
                replaced = True
        # Also try with currency symbol
        old_with_sym = (sym + old_clean) if before else (old_clean + sym)
        if replace_text(page, old_with_sym, new_fmt, occurrence=None):
            replaced = True

    # If old value not found, search for large numbers in lower half of page
    if not replaced and all_spans:
        ph = page.rect.height
        pw = page.rect.width
        cands = [
            s for s in all_spans
            if s["y0"] > ph * 0.5
            and re.search(r'\d{2,}[.,]\d{2}', s["text"])
        ]
        if cands:
            largest = max(cands, key=lambda s: float(
                re.sub(r'[^\d]', '', s["text"].split(".")[0] or "0") or "0"))
            old_v = re.sub(r'[^\d.,]', '', largest["text"])
            replace_text(page, old_v, new_clean, occurrence=None)

    print(f"  💰 Grand total → {new_fmt}")


# ─── Self-extractor (no Claude fallback) ──────────────────────────────────────

def self_extract(page) -> dict:
    """Extract invoice structure from PDF without AI. v6: anchor-based
    addresses, largest-value grand total, robust field detection."""
    all_spans = get_all_spans(page)
    ph = page.rect.height
    pw = page.rect.width

    def val_near(label, x_min=350):
        hits = [s for s in all_spans if label.lower() in s["text"].lower()]
        if not hits: return ""
        ly = hits[0]["y0"]
        vs = [s for s in all_spans
              if abs(s["y0"] - ly) < 8 and s["x0"] >= x_min
              and label.lower() not in s["text"].lower()]
        return vs[0]["text"] if vs else ""

    # Invoice fields
    inv_num = (val_near("InvoiceNumber") or val_near("Invoice Number")
               or val_near("Invoice#")   or val_near("Invoice No")
               or val_near("Inv No")     or val_near("InvNo"))
    date    = (val_near("Date") or val_near("Invoice Date") or val_near("Datum"))

    # Currency detection
    sym, before = "£", True
    for s in all_spans:
        for ch in "£$€¥₹₪฿":
            if ch in s["text"]:
                sym    = ch
                before = s["text"].strip().startswith(ch)
                break
        else:
            continue
        break

    # European format detection
    eu = bool(re.search(r'\d\.\d{3},\d{2}', " ".join(s["text"] for s in all_spans)))

    # ── Grand total: LARGEST number in lower-right quadrant ──────────────────
    # "Rightmost" fails when a product row total sits further right than the
    # grand total. "Largest value" is always correct.
    def _parse_num(t):
        try: return float(re.sub(r'[^\d.]', '', t.replace(',', '')) or '0')
        except: return 0.0

    tot_cands = [s for s in all_spans
                 if s["y0"] > ph * 0.5 and s["x0"] > pw * 0.5
                 and re.search(r'\d{2,}[.,]\d{2}', s["text"])]
    grand = re.sub(r'[^\d.,]', '',
                   max(tot_cands, key=lambda s: _parse_num(s["text"]))["text"]) \
            if tot_cands else ""

    # Tax rate
    tax = 20.0
    for s in all_spans:
        m = re.search(r'(\d+)\s*%', s["text"])
        if m:
            tax = float(m.group(1))
            break

    # ── Addresses: anchor off Customer/Delivery label positions ──────────────
    # Avoids pulling in supplier header, table headers, or product names.
    CUST_KW = ('customer', 'bill to', 'billed to', 'sold to', 'client')
    DELV_KW = ('delivery', 'ship to', 'shipping', 'deliver to', 'deliver address')
    META_KW = ('invoicenumber', 'invoice number', 'salesperson', 'pagenumber',
               'page number', 'vatnumber', 'vat number', 'ordernumber', 'order number')

    table_header_y = next(
        (s["y0"] for s in all_spans
         if s["text"].lower() in ("sku","product","description","item","article","artikel")),
        ph * 0.45)

    def _find_label(keywords):
        for s in sorted(all_spans, key=lambda x: x["y0"]):
            if any(kw in s["text"].lower() for kw in keywords):
                return s["y0"], s["x0"]
        return None, None

    meta_fields = [s for s in all_spans
                   if any(kw in s["text"].lower().replace(' ','') for kw in META_KW)]
    meta_x = min((s["x0"] for s in meta_fields), default=pw * 0.65)

    cust_y, cust_x = _find_label(CUST_KW)
    delv_y, delv_x = _find_label(DELV_KW)

    if cust_y is not None:
        cy_start = cust_y + 4
        cy_end   = min(table_header_y - 5, cy_start + 80)
        bill = [s["text"] for s in sorted(all_spans, key=lambda s: s["y0"])
                if cy_start <= s["y0"] <= cy_end
                and s["x0"] < (delv_x if delv_x else pw * 0.4) - 5
                and s["size"] < 10
                and not any(kw in s["text"].lower() for kw in CUST_KW)]
    else:
        bill = [s["text"] for s in sorted(all_spans, key=lambda s: s["y0"])
                if s["x0"] < pw * 0.35 and s["size"] < 9
                and ph * 0.15 < s["y0"] < ph * 0.35]

    if delv_y is not None:
        dy_start = delv_y + 4
        dy_end   = min(table_header_y - 5, dy_start + 80)
        delv = [s["text"] for s in sorted(all_spans, key=lambda s: s["y0"])
                if dy_start <= s["y0"] <= dy_end
                and (delv_x - 5) <= s["x0"] < meta_x - 5
                and s["size"] < 10
                and not any(kw in s["text"].lower() for kw in DELV_KW + ('address',))]
    else:
        delv = [s["text"] for s in sorted(all_spans, key=lambda s: s["y0"])
                if pw * 0.35 <= s["x0"] < pw * 0.65 and s["size"] < 9
                and ph * 0.15 < s["y0"] < ph * 0.35]

    return {
        "invoice_number_value":   inv_num,
        "date_value":             date,
        "billing_address_lines":  bill[:8],
        "delivery_address_lines": delv[:8],
        "tax_rate_pct":           tax,
        "currency_symbol":        sym,
        "currency_before":        before,
        "european_format":        eu,
        "grand_total_value":      grand,
    }


# ─── Multi-page support ───────────────────────────────────────────────────────

def get_table_pages(doc) -> list:
    """Find all pages that contain product tables."""
    pages_with_tables = []
    for i, page in enumerate(doc):
        tbl = detect_table(page)
        if tbl and tbl["data_rows"]:
            pages_with_tables.append((i, page, tbl))
    return pages_with_tables


# ─── Main editor ──────────────────────────────────────────────────────────────

def edit_invoice(pdf_bytes: bytes, changes: dict,
                 user_products: list, structure_map: dict) -> tuple:

    doc  = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    page = doc[0]
    log  = []

    # Scanned PDF detection
    if detect_pdf_type(page) == "scanned":
        doc.close()
        return pdf_bytes, ["⚠️ Scanned PDF detected. Please upload a digital PDF with selectable text."]

    all_spans = get_all_spans(page)

    # Use Claude's structure_map if available, else self-extract
    smap = structure_map or self_extract(page)
    print(f"  📋 Map: {'Claude' if structure_map else 'self-extract'}")

    sym  = smap.get("currency_symbol", "£")
    bfr  = smap.get("currency_before", True)
    eu   = smap.get("european_format", False)
    tax  = float(smap.get("tax_rate_pct", 20.0))

    if "Tax" in changes:
        try: tax = float(str(changes["Tax"]).replace("%", "").strip())
        except ValueError: pass

    # ── Simple field edits ────────────────────────────────────────────────────

    if "Invoice_Number" in changes:
        old = str(smap.get("invoice_number_value", ""))
        new = str(changes["Invoice_Number"])
        if old:
            replace_text(page, old, new, x_min=350)
            log.append(f"Invoice number: {old} → {new}")

    if "Date" in changes:
        old = str(smap.get("date_value", ""))
        new = str(changes["Date"])
        if old:
            replace_text(page, old, new, x_min=350)
            log.append(f"Date: {old} → {new}")

    if "Billing_Address" in changes:
        old_lines = smap.get("billing_address_lines", [])
        replace_address(page, old_lines, changes["Billing_Address"], x_max=None)
        log.append("Billing address updated")

    if "Shipping_Address" in changes:
        old_lines = smap.get("delivery_address_lines", [])
        replace_address(page, old_lines, changes["Shipping_Address"])
        log.append("Shipping address updated")

    # ── Product table rebuild ─────────────────────────────────────────────────

    if user_products:
        all_pages_with_tables = []
        for i, pg in enumerate(doc):
            tbl = detect_table(pg, get_all_spans(pg))
            if tbl and tbl["data_rows"]:
                all_pages_with_tables.append((i, pg, tbl))

        if not all_pages_with_tables:
            log.append("⚠️ No product table detected on any page")
        else:
            for pg_idx, pg, tbl in all_pages_with_tables:
                # Pre-flight page fit check — log result before editing
                orig_count = len(tbl["data_rows"])
                new_count  = len(user_products)
                if new_count > orig_count:
                    fit = page_fit_check(
                        pg, orig_count, new_count,
                        tbl["row_height"], tbl["data_rows"][-1]["y"]
                    )
                    log.append(fit["message"])
                    if not fit["fits"]:
                        log.append(f"⛔ Edit aborted on page {pg_idx+1}: "
                                   f"add max {fit['max_extra_rows']} extra rows "
                                   f"or use an invoice with more space.")
                        continue

                rd, sub_t, vat_t, grand = rebuild_table(
                    pg, tbl, user_products, tax, sym, bfr, eu, smap)
                if rd:
                    update_grand_total(
                        pg,
                        str(smap.get("grand_total_value", "")),
                        grand, sym, bfr, eu, get_all_spans(pg)
                    )
                    log.append(f"Page {pg_idx+1}: {len(rd)} rows rebuilt "
                               f"({orig_count} → {new_count})")
                    log.append(f"Grand total: {fmt(grand, sym, bfr, eu)}")
                else:
                    log.append(f"⚠️ Page {pg_idx+1}: table rebuild failed "
                               f"(table detected but could not write rows)")

    buf = io.BytesIO()
    doc.save(buf, garbage=4, deflate=True)
    doc.close()
    buf.seek(0)
    return buf.read(), log


# ─── Flask routes ─────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status":   "ok",
        "service":  "invoice-editor-v6",
        "engine":   f"PyMuPDF {pymupdf.__version__}",
        "features": [
            "v6: font read from actual row spans (pixel-perfect match)",
            "v6: smart page fit check before push-down",
            "v6: safe push-down (totals zone only, footer protected)",
            "v6: grand total = largest value not rightmost",
            "v6: anchor-based address extraction (no supplier bleed)",
            "v6: erase zone stops before grand total row",
            "v6: /validate endpoint for pre-flight checks",
            "50+ font mappings",
            "multi-page table support",
            "address text-search replacement",
            "scanned PDF detection",
            "multilingual table headers (DE/ES/FR/NL/IT/PL)",
            "numeric-column table fallback",
            "graphics=0 redaction (border lines preserved)",
            "insert_textbox L/C/R alignment per column",
        ]
    })


@app.route("/edit", methods=["POST"])
def edit():
    try:
        body = request.get_json(force=True)
        if not body:
            return jsonify({"success": False, "error": "No JSON body"}), 400

        b64 = body.get("pdf_base64", "")
        if not b64:
            return jsonify({"success": False, "error": "Missing pdf_base64"}), 400
        if "," in b64:
            b64 = b64.split(",", 1)[1]

        changes       = body.get("changes", {})
        prods         = body.get("user_products", [])
        smap          = body.get("structure_map", None)
        pdf_bytes     = base64.b64decode(b64)

        print(f"\n📄 {len(pdf_bytes):,}B | "
              f"changes={list(changes.keys())} | "
              f"products={len(prods)} | "
              f"map={'yes' if smap else 'no'}")

        out, log = edit_invoice(pdf_bytes, changes, prods, smap)
        print(f"✅ {len(out):,}B output | {len(log)} log entries")

        return jsonify({
            "success":    True,
            "pdf_base64": base64.b64encode(out).decode(),
            "log":        log
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/validate", methods=["POST"])
def validate():
    """
    Pre-flight check: tells you if new products will fit without editing.
    Send same body as /edit. Returns fit info and invoice metadata — no PDF changes.
    """
    try:
        body = request.get_json(force=True)
        b64  = body.get("pdf_base64", "")
        if "," in b64:
            b64 = b64.split(",", 1)[1]
        pdf_bytes = base64.b64decode(b64)
        prods     = body.get("user_products", [])
        smap      = body.get("structure_map", None)

        doc  = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        page = doc[0]

        if detect_pdf_type(page) == "scanned":
            doc.close()
            return jsonify({"valid": False,
                            "error": "Scanned PDF — no selectable text"})

        extracted = smap or self_extract(page)
        tbl       = detect_table(page, get_all_spans(page))

        result = {
            "valid":          True,
            "pages":          len(doc),
            "invoice_number": extracted.get("invoice_number_value", ""),
            "date":           extracted.get("date_value", ""),
            "currency":       extracted.get("currency_symbol", "£"),
            "tax_rate":       extracted.get("tax_rate_pct", 20.0),
            "grand_total":    extracted.get("grand_total_value", ""),
            "table_found":    tbl is not None,
            "original_rows":  len(tbl["data_rows"]) if tbl else 0,
            "new_rows":       len(prods),
        }

        if tbl and prods:
            orig_count = len(tbl["data_rows"])
            new_count  = len(prods)
            if new_count > orig_count:
                fit = page_fit_check(
                    page, orig_count, new_count,
                    tbl["row_height"], tbl["data_rows"][-1]["y"]
                )
                result["fit_check"] = fit
            else:
                result["fit_check"] = {
                    "fits": True,
                    "message": f"✅ {new_count} rows fit (≤ original {orig_count})"
                }

        doc.close()
        return jsonify(result)

    except Exception as e:
        traceback.print_exc()
        return jsonify({"valid": False, "error": str(e)}), 500


@app.route("/preview", methods=["POST"])
def preview():
    try:
        body  = request.get_json(force=True)
        b64   = body.get("pdf_base64", "")
        if "," in b64:
            b64 = b64.split(",", 1)[1]
        out, _ = edit_invoice(
            base64.b64decode(b64),
            body.get("changes", {}),
            body.get("user_products", []),
            body.get("structure_map", None)
        )
        return send_file(io.BytesIO(out), mimetype="application/pdf",
                         as_attachment=True, download_name="edited_invoice.pdf")
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 Invoice Editor v6 :{port}  PyMuPDF {pymupdf.__version__}")
    app.run(host="0.0.0.0", port=port, debug=False)
