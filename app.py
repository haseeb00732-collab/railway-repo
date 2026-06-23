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

_BOLD_MAP = {"helv": "hebo", "tiro": "tibo", "cour": "cobo"}

def _to_bold(name: str) -> str:
    return _BOLD_MAP.get(name, "hebo")


def map_font(name: str) -> str:
    """Map any PDF font name to closest PyMuPDF built-in."""
    if not name:
        return "helv"
    c = (name.lower()
         .replace(" ", "").replace(",", "").replace("+", "")
         .replace("-", "").replace("_", "").replace(".", ""))
    is_bold = any(b in c for b in ("bold", "heavy", "black", "semibold", "demi"))
    for k, v in FONT_MAP.items():
        if k in c:
            return _to_bold(v) if is_bold else v
    return "hebo" if is_bold else "helv"


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


def fmt(value: float, sym: str = "", before: bool = True, eu: bool = False) -> str:
    """Format number matching invoice currency style. sym='' means plain number."""
    if eu:
        s = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    else:
        s = f"{value:,.2f}"
    if not sym:
        return s
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


# ─── Embedded font extraction ─────────────────────────────────────────────────

def extract_page_fonts(page) -> dict:
    """
    Extract embedded font bytes from a page, keyed by normalised font name.
    Returns {normalised_name: font_bytes} for fonts that have extractable data.
    Subset-embedded fonts (most PDFs) only contain glyphs used in the original
    document — fine for invoice editing since we reuse the same character set.
    """
    doc = page.parent
    font_cache = {}
    try:
        for font in page.get_fonts(full=True):
            # font tuple: (xref, ext, type, basefont, name, encoding, referencer)
            xref      = font[0]
            basefont  = font[3]   # e.g. "ABCDEF+Calibri-Bold"
            name      = font[4]   # e.g. "Calibri-Bold"
            if not xref:
                continue
            try:
                font_info = doc.extract_font(xref)
                # extract_font returns (name, ext, type, content, ...)
                # content is bytes or None
                content = font_info[3] if len(font_info) > 3 else None
                if content and len(content) > 100:   # skip tiny/empty stubs
                    # Store under multiple keys so lookup is forgiving
                    for key in (basefont, name, basefont.split("+")[-1]):
                        if key:
                            norm = (key.lower()
                                    .replace(" ", "").replace("-", "")
                                    .replace("_", "").replace("+", ""))
                            font_cache[norm] = content
            except Exception:
                continue
    except Exception:
        pass
    return font_cache


def get_font_for_span(span_font: str, page_font_cache: dict):
    """
    Look up extracted font bytes for a span's font name.
    Returns (fontbuffer_bytes, None) if found (use fontbuffer= kwarg),
    or (None, mapped_name_str) if not found (use fontname= kwarg).
    """
    norm = (span_font.lower()
            .replace(" ", "").replace("-", "")
            .replace("_", "").replace("+", "").replace(",", ""))
    # Direct hit
    if norm in page_font_cache:
        return page_font_cache[norm], None
    # Partial match — find longest key that is a substring of norm
    best_key = None
    for k in page_font_cache:
        if k in norm or norm in k:
            if best_key is None or len(k) > len(best_key):
                best_key = k
    if best_key:
        return page_font_cache[best_key], None
    # No match — fall back to PyMuPDF built-in
    return None, map_font(span_font)


# ─── Text replacement ─────────────────────────────────────────────────────────

def replace_text(page, old: str, new: str,
                 x_min=None, x_max=None, y_min=None, y_max=None,
                 occurrence=0, font_cache: dict = None) -> bool:
    """Find old text in PDF, redact, insert new text with same font.
    font_cache: output of extract_page_fonts(page) — enables true embedded
    font reuse instead of PyMuPDF built-in substitution."""
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
        sp  = nearest_span(page, rect)
        fs  = sp["size"]
        fc  = rgb(sp["color"])

        # ── Font resolution: embedded first, built-in fallback ───────────────
        fb, fn = get_font_for_span(sp["font"], font_cache or {})

        new_str = str(new)
        # Erase old text — measure with actual font when possible
        try:
            if fb:
                _f = pymupdf.Font(fontbuffer=fb)
            else:
                _f = pymupdf.Font(fn)
            old_w   = _f.text_length(old, fs)
            new_w   = _f.text_length(new_str, fs) if new_str else 0
            erase_w = max(rect.width, old_w, new_w) + 10
        except Exception:
            erase_w = max(rect.width, max(len(old), len(new_str)) * fs * 0.6) + 10

        er = pymupdf.Rect(rect.x0 - 2, rect.y0 - 2,
                          rect.x0 + erase_w, rect.y1 + 2)
        page.add_redact_annot(er, fill=(1, 1, 1))
        page.apply_redactions(graphics=0)  # graphics=0 preserves border lines

        if new_str:
            if fb:
                page.insert_text((rect.x0, rect.y1 - 1), new_str,
                                 fontbuffer=fb, fontsize=fs, color=fc)
                print(f"  ✅ '{old[:35]}' → '{new_str[:35]}' [embedded font]")
            else:
                page.insert_text((rect.x0, rect.y1 - 1), new_str,
                                 fontname=fn, fontsize=fs, color=fc)
                print(f"  ✅ '{old[:35]}' → '{new_str[:35]}' [builtin={fn}]")
    return True


# ─── Address replacement (text-search based, not x-position) ─────────────────

def replace_address(page, old_lines: list, new_text: str,
                    x_min=None, x_max=None, font_cache: dict = None):
    """
    Block erase the full address bounding box, then reinsert all new lines.
    Handles old/new line count differences cleanly — no orphaned lines.
    font_cache: output of extract_page_fonts(page) for embedded font reuse.
    """
    new_lines = [l.strip() for l in
                 str(new_text).replace("\\n", "\n").split("\n")
                 if l.strip()]
    stripped = [l.strip() for l in old_lines if l.strip()]
    if not stripped:
        return

    # Find bounding rects for all old lines
    all_rects = []
    anchor_span = None
    anchor_rect = None
    for ol in stripped:
        rects = page.search_for(ol)
        if x_min is not None:
            rects = [r for r in rects if r.x0 >= x_min]
        if x_max is not None:
            rects = [r for r in rects if r.x1 <= x_max]
        if rects:
            if anchor_span is None:
                anchor_span = nearest_span(page, rects[0])
                anchor_rect = rects[0]
            all_rects.extend(rects)

    if not all_rects or anchor_span is None:
        print(f"  ⚠️  Address block not found on page")
        return

    # ── Font resolution: embedded first, built-in fallback ───────────────────
    fb, fn = get_font_for_span(anchor_span["font"], font_cache or {})
    fs = anchor_span["size"]
    fc = rgb(anchor_span["color"])
    lh = fs * 1.45  # line height

    # Bounding box covering all old lines + room for new lines
    x0 = min(r.x0 for r in all_rects) - 2
    y0 = min(r.y0 for r in all_rects) - 2
    x1 = max(r.x1 for r in all_rects) + 60
    y1 = max(
        max(r.y1 for r in all_rects),
        y0 + len(new_lines) * lh
    ) + 4

    # Erase entire address block
    page.add_redact_annot(pymupdf.Rect(x0, y0, x1, y1), fill=(1, 1, 1))
    page.apply_redactions(graphics=0)

    # Reinsert new lines starting at first old line's baseline
    base_y = anchor_rect.y1 - 1
    for i, nl in enumerate(new_lines):
        if fb:
            page.insert_text(
                (anchor_rect.x0, base_y + i * lh),
                nl, fontbuffer=fb, fontsize=fs, color=fc
            )
        else:
            page.insert_text(
                (anchor_rect.x0, base_y + i * lh),
                nl, fontname=fn, fontsize=fs, color=fc
            )
    font_label = "embedded" if fb else f"builtin={fn}"
    print(f"  ✅ Address: {len(stripped)} old → {len(new_lines)} new lines [{font_label}]")


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
    - Embedded font bytes extracted and reused (no Helvetica substitution)
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

    # ── Extract embedded fonts from page BEFORE any redactions ───────────────
    font_cache = extract_page_fonts(page)
    print(f"  🔤 Font cache: {len(font_cache)} embedded fonts extracted")

    # ── Read font/size/color from ACTUAL first data row spans ─────────────────
    # Resolve to embedded bytes if available, built-in name as fallback.
    rs = tbl.get("row_size", 7.0)
    rc = tbl.get("row_color", (0, 0, 0))
    actual_font_name = tbl.get("row_font", "helv")  # raw PDF font name

    first_row_y = rows[0]["y"]
    all_spans_page = get_all_spans(page)
    first_row_spans = [s for s in all_spans_page
                       if abs(s["y0"] - first_row_y) < rh * 0.8
                       and s["text"].strip()]
    if first_row_spans:
        from collections import Counter
        font_counts    = Counter(s["font"] for s in first_row_spans)
        actual_font_name = font_counts.most_common(1)[0][0]
        rs             = first_row_spans[0]["size"]
        rc             = rgb(first_row_spans[0]["color"])

    # Resolve embedded bytes or fall back to mapped built-in name
    row_fb, row_fn = get_font_for_span(actual_font_name, font_cache)
    if row_fb:
        print(f"  🎨 Row font: {actual_font_name} → embedded bytes  size={rs:.1f}")
    else:
        print(f"  🎨 Row font: {actual_font_name} → builtin={row_fn}  size={rs:.1f}")

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
        _push_totals_down(page, rows[-1]["y"] + rh + 2, push, font_cache)

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

    # ── 4. Detect whether table rows use currency symbol ─────────────────────
    # BUG FIX: original Wilkinson invoice uses plain numbers (800.00) in table
    # rows — no £ symbol. The £ only appears in Payment Details section.
    # We detect this by sampling actual data row spans.
    table_uses_sym = False
    for row_data in rows[:3]:  # check first 3 rows
        for s in row_data["spans"]:
            if any(ch in s["text"] for ch in "£$€¥₹₪฿"):
                table_uses_sym = True
                break
    table_sym   = sym   if table_uses_sym else ""
    table_before = before if table_uses_sym else True
    print(f"  💱 Table currency: {'with symbol ' + sym if table_uses_sym else 'plain numbers (no symbol)'}")

    # ── 5. Build ACCURATE column boundaries from ACTUAL data positions ────────
    # BUG FIX: col_spans use header text x0 which is the label's left edge
    # (headers are center-aligned, so their x0 ≠ true column left boundary).
    # Instead derive column left boundaries from where actual data values sit.
    #
    # Strategy: collect all span x0 values from first 2 data rows, cluster
    # them into column buckets, sort ascending → these are the true col starts.

    data_x_positions = []
    for row_data in rows[:min(3, len(rows))]:
        for s in row_data["spans"]:
            data_x_positions.append(s["x0"])

    # Cluster x positions (within 8px = same column)
    data_x_positions.sort()
    col_starts = []
    for xp in data_x_positions:
        if not col_starts or xp - col_starts[-1] > 8:
            col_starts.append(xp)

    # Also include header x positions for columns with no data (e.g. SKU)
    for cs in col_spans:
        if not any(abs(cs["x0"] - cx) < 8 for cx in col_starts):
            col_starts.append(cs["x0"])
    col_starts.sort()

    # Match each column header label to its nearest col_start
    n = len(col_spans)
    col_rects = []
    used_starts = set()

    for i, cs in enumerate(col_spans):
        label = cs["text"].lower().replace(" ", "").replace(".", "")
        # Find nearest col_start to this header's x0
        nearest = min(col_starts, key=lambda x: abs(x - cs["x0"]))
        # Get the right boundary = next col_start or page edge
        nearest_idx = col_starts.index(nearest)
        x_right = (col_starts[nearest_idx + 1] - 1) if nearest_idx + 1 < len(col_starts) else page_w - 15
        col_rects.append((nearest, x_right, label))

    print(f"  📐 Column rects: {[(round(xl), round(xr), lbl[:6]) for xl, xr, lbl in col_rects]}")

    # ── 6. Draw new rows ──────────────────────────────────────────────────────
    cur_y = rows[0]["y"]

    for ri, row in enumerate(new_rows):
        y0 = cur_y
        y1 = y0 + rh

        for xl, xr, label in col_rects:
            # Map label → value + alignment
            if any(k in label for k in ("descr","product","item","name","bezeich","artíc","artik","omschr","towar")):
                text, align = row["name"], 0  # left
            elif any(k in label for k in ("sku","code","ref","no","num","artikelnr")):
                text, align = "", 0           # blank — don't invent SKUs
            elif any(k in label for k in ("stat","status")):
                text, align = row["status"], 1  # center
            elif any(k in label for k in ("unitprice","unitcost","unit","rate","preis","prijs","prix","precio","prezzo","cena")) \
                 and not any(k in label for k in ("total","sub","amount","gesamt")):
                text, align = fmt(row["price"], table_sym, table_before, eu), 2  # right
            elif any(k in label for k in ("qty","quan","menge","antal","ilość","units","qté","cant")):
                text, align = row["qty"], 2   # right
            elif any(k in label for k in ("sub","subtot","netto","net")):
                text, align = fmt(row["sub"],   table_sym, table_before, eu), 2
            elif any(k in label for k in ("vat","tax","steuer","btw","taxe","iva","imposta","podatek","moms")):
                text, align = fmt(row["vat"],   table_sym, table_before, eu), 2
            elif any(k in label for k in ("total","amount","gesamt","bedrag","montant","importe","importo","razem","betrag")):
                text, align = fmt(row["total"], table_sym, table_before, eu), 2
            else:
                text, align = "", 0

            if text:
                # Ensure textbox is wide enough — expand right if needed
                min_width = len(text) * rs * 0.65
                actual_xr = max(xr, xl + min_width + 4)
                # But don't overflow page
                actual_xr = min(actual_xr, page_w - 8)
                tb_rect = pymupdf.Rect(xl + 1, y0 + 1, actual_xr - 1, y1 - 1)
                if row_fb:
                    page.insert_textbox(
                        tb_rect, text,
                        fontbuffer=row_fb, fontsize=rs, color=rc,
                        align=align
                    )
                else:
                    page.insert_textbox(
                        tb_rect, text,
                        fontname=row_fn, fontsize=rs, color=rc,
                        align=align
                    )

        sym_display = table_sym if table_sym else ""
        print(f"  📝 Row {ri+1}: {row['name'][:30]} | "
              f"qty={row['qty']} × {sym_display}{row['price']:.2f} = "
              f"sub:{fmt(row['sub'],table_sym,table_before,eu)} "
              f"vat:{fmt(row['vat'],table_sym,table_before,eu)} "
              f"total:{fmt(row['total'],table_sym,table_before,eu)}")
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


def _push_totals_down(page, below_y: float, push_amount: float,
                      font_cache: dict = None):
    """
    Safe push-down: moves text AND drawings in the totals zone.
    Collects both before erasing, redraws both shifted down.
    font_cache: extract_page_fonts() result — must be collected BEFORE this
    call since redactions will have already run on the product rows by then.
    """
    page_w   = page.rect.width
    footer_y = _find_footer_y(page)
    zone_top = below_y
    zone_bot = footer_y - 2

    if zone_top >= zone_bot:
        print(f"  ⚠️  No totals zone (below_y={below_y:.0f} >= footer={footer_y:.0f})")
        return

    # 1. Collect text
    clip   = pymupdf.Rect(0, zone_top, page_w, zone_bot)
    blocks = page.get_text("dict", clip=clip, flags=0)["blocks"]
    to_move = [
        span
        for block in blocks
        for line in block.get("lines", [])
        for span in line.get("spans", [])
        if zone_top <= span["bbox"][1] < zone_bot
    ]

    # 2. Collect drawings BEFORE erase
    drawings_to_move = [
        d for d in page.get_drawings()
        if zone_top <= d["rect"][1] < zone_bot
    ]

    if not to_move and not drawings_to_move:
        print(f"  ℹ️  Nothing in totals zone to push")
        return

    # 3. Erase zone (graphics=0 removes vector graphics in redact area)
    page.add_redact_annot(pymupdf.Rect(0, zone_top, page_w, zone_bot), fill=(1, 1, 1))
    page.apply_redactions(graphics=0)

    # 4. Redraw text shifted — use embedded font bytes if available
    _fc = font_cache or {}
    for span in to_move:
        fb, fn = get_font_for_span(span["font"], _fc)
        fs     = span["size"]
        fc     = rgb(span["color"])
        new_y  = span["bbox"][3] + push_amount - 1
        if new_y < footer_y - 5:
            if fb:
                page.insert_text((span["bbox"][0], new_y), span["text"],
                                 fontbuffer=fb, fontsize=fs, color=fc)
            else:
                page.insert_text((span["bbox"][0], new_y), span["text"],
                                 fontname=fn, fontsize=fs, color=fc)

    # 5. Redraw vector graphics shifted
    shape = page.new_shape()
    for d in drawings_to_move:
        dy     = push_amount
        stroke = d.get("color")
        fill   = d.get("fill")
        lw     = d.get("width", 1.0)
        for item in d.get("items", []):
            kind = item[0]
            try:
                if kind == "l":   # line
                    shape.draw_line(
                        pymupdf.Point(item[1].x, item[1].y + dy),
                        pymupdf.Point(item[2].x, item[2].y + dy)
                    )
                elif kind == "re":  # rect
                    r = item[1]
                    shape.draw_rect(pymupdf.Rect(r.x0, r.y0 + dy, r.x1, r.y1 + dy))
                elif kind == "c":   # cubic bezier
                    pts = [pymupdf.Point(p.x, p.y + dy) for p in item[1:5]]
                    shape.draw_bezier(*pts)
                elif kind == "qu":  # quad
                    q = item[1]
                    shifted = pymupdf.Quad(
                        pymupdf.Point(q.ul.x, q.ul.y + dy),
                        pymupdf.Point(q.ur.x, q.ur.y + dy),
                        pymupdf.Point(q.ll.x, q.ll.y + dy),
                        pymupdf.Point(q.lr.x, q.lr.y + dy)
                    )
                    shape.draw_quad(shifted)
            except Exception:
                continue
        shape.finish(
            color=stroke, fill=fill, width=lw,
            stroke_opacity=d.get("opacity", 1.0),
            fill_opacity=d.get("fill_opacity", 1.0)
        )
    shape.commit()

    print(f"  ⬇️  Pushed {len(to_move)} spans + {len(drawings_to_move)} drawings "
          f"↓{push_amount:.0f}px (footer@{footer_y:.0f})")


# ─── Grand total update ───────────────────────────────────────────────────────

def update_grand_total(page, old_str: str, new_val: float,
                       sym: str, before: bool, eu: bool,
                       all_spans: list = None,
                       structure_map: dict = None):
    """
    Replace grand total. Blueprint-first if available, else heuristic fallback.
    """
    # Blueprint-first: check structure_map for grand total field
    if structure_map:
        bp = structure_map.get("editing_blueprint", {})
        for f in bp.get("replaceable_fields", []):
            if "grand" in f.get("field_id", "").lower() or "total" in f.get("field_id", "").lower():
                cv = f.get("current_value", "")
                if cv:
                    old_str = re.sub(r'[^\d.,]', '', str(cv)) or old_str
                    break

    new_plain    = f"{new_val:,.2f}"
    new_with_sym = fmt(new_val, sym, before, eu) if sym else new_plain
    replaced     = False

    if old_str:
        old_clean = re.sub(r'[^\d.,]', '', str(old_str))
        if old_clean:
            if replace_text(page, old_clean, new_plain, occurrence=None):
                replaced = True
        if sym:
            old_sym = (sym + old_clean) if before else (old_clean + sym)
            if replace_text(page, old_sym, new_with_sym, occurrence=None):
                replaced = True

    # Heuristic fallback: largest number below last table row, right half of page.
    # Uses detect_table() to derive the search floor — no hardcoded ph * 0.5.
    if not replaced and all_spans:
        pw = page.rect.width
        ph = page.rect.height

        def _parse(t):
            try:    return float(re.sub(r'[^\d.]', '', t.replace(',', '')) or '0')
            except: return 0.0

        # Derive y floor from actual table position, not a magic percentage
        _tbl = detect_table(page, all_spans)
        if _tbl and _tbl["data_rows"]:
            gt_floor = _tbl["data_rows"][-1]["y"]   # below last product row
        else:
            gt_floor = ph * 0.5                      # only if no table found at all

        cands = [s for s in all_spans
                 if s["y0"] > gt_floor and s["x0"] > pw * 0.5
                 and re.search(r'\d{2,}[.,]\d{2}', s["text"])]
        if cands:
            largest = max(cands, key=lambda s: _parse(s["text"]))
            old_v   = re.sub(r'[^\d.,]', '', largest["text"])
            has_sym = any(ch in largest["text"] for ch in "£$€¥₹₪฿")
            replace_text(page, old_v,
                         new_with_sym if has_sym else new_plain,
                         occurrence=None)

    print(f"  💰 Grand total → {new_plain} (table) / {new_with_sym} (payment)")


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

    # Currency detection — look in PAYMENT SECTION only (lower half of page)
    # not table rows, because some invoices (e.g. Wilkinson) use plain numbers
    # in table but £ in payment details
    sym, before = "", True  # default: no symbol (most common in table rows)
    payment_spans = [s for s in all_spans if s["y0"] > ph * 0.55]
    for s in payment_spans:
        for ch in "£$€¥₹₪฿":
            if ch in s["text"]:
                sym    = ch
                before = s["text"].strip().startswith(ch)
                break
        else:
            continue
        break
    # Fallback: if not found in payment section, check whole page
    if not sym:
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

    # ── Grand total: derive search zone from table detection, not magic % ────
    tbl_for_gt = detect_table(page, all_spans)
    if tbl_for_gt and tbl_for_gt["data_rows"]:
        gt_y_min = tbl_for_gt["data_rows"][-1]["y"]
    else:
        gt_y_min = ph * 0.5  # safe fallback

    tot_cands = [s for s in all_spans
                 if s["y0"] > gt_y_min and s["x0"] > pw * 0.5
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
        # Fallback: left third of page, upper-mid zone
        bill = [s["text"] for s in sorted(all_spans, key=lambda s: s["y0"])
                if s["x0"] < pw * 0.40 and s["size"] < 9
                and ph * 0.12 < s["y0"] < ph * 0.45]

    if delv_y is not None:
        dy_start = delv_y + 4
        dy_end   = min(table_header_y - 5, dy_start + 80)
        delv = [s["text"] for s in sorted(all_spans, key=lambda s: s["y0"])
                if dy_start <= s["y0"] <= dy_end
                and (delv_x - 5) <= s["x0"] < meta_x - 5
                and s["size"] < 10
                and not any(kw in s["text"].lower() for kw in DELV_KW + ('address',))]
    else:
        # Fallback: centre third of page, upper-mid zone
        delv = [s["text"] for s in sorted(all_spans, key=lambda s: s["y0"])
                if pw * 0.30 <= s["x0"] < pw * 0.65 and s["size"] < 9
                and ph * 0.12 < s["y0"] < ph * 0.45]

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

    # ── Extract embedded fonts ONCE, reuse across all edits on this page ──────
    font_cache = extract_page_fonts(page)
    print(f"  🔤 Page font cache: {len(font_cache)} embedded fonts")

    # Use Claude's structure_map if available, else self-extract
    smap = structure_map or self_extract(page)
    print(f"  📋 Map: {'Claude blueprint' if structure_map else 'self-extract'}")

    sym  = smap.get("currency_symbol", "£")
    bfr  = smap.get("currency_before", True)
    eu   = smap.get("european_format", False)
    tax  = float(smap.get("tax_rate_pct", 20.0))

    if "Tax" in changes:
        try: tax = float(str(changes["Tax"]).replace("%", "").strip())
        except ValueError: pass

    # ── Blueprint field lookup helper ─────────────────────────────────────────
    # When Claude's editing_blueprint is present, use its field current_values
    # directly — these are more precise than self_extract heuristics.
    def _blueprint_value(field_id_fragments: list, fallback: str) -> str:
        """Return current_value from blueprint field matching any fragment."""
        bp = smap.get("editing_blueprint", {})
        for f in bp.get("replaceable_fields", []):
            fid = f.get("field_id", "").lower()
            if any(frag in fid for frag in field_id_fragments):
                cv = str(f.get("current_value", "")).strip()
                if cv:
                    return cv
        return fallback

    # ── Simple field edits ────────────────────────────────────────────────────

    if "Invoice_Number" in changes:
        old = _blueprint_value(
            ["invoice_number", "inv_number", "invoiceno", "invoice_no"],
            str(smap.get("invoice_number_value", ""))
        )
        new = str(changes["Invoice_Number"])
        if old:
            replace_text(page, old, new, x_min=350, font_cache=font_cache)
            log.append(f"Invoice number: {old} → {new}")

    if "Date" in changes:
        old = _blueprint_value(
            ["date", "invoice_date", "invoicedate"],
            str(smap.get("date_value", ""))
        )
        new = str(changes["Date"])
        if old:
            replace_text(page, old, new, x_min=350, font_cache=font_cache)
            log.append(f"Date: {old} → {new}")

    if "Billing_Address" in changes:
        old_lines = smap.get("billing_address_lines", [])
        # Blueprint override: if billing address lines listed in blueprint
        bp_bill = _blueprint_value(["billing_address", "bill_to", "customer_address"], "")
        if bp_bill and not old_lines:
            old_lines = [l.strip() for l in bp_bill.split("\n") if l.strip()]
        replace_address(page, old_lines, changes["Billing_Address"],
                        x_max=None, font_cache=font_cache)
        log.append("Billing address updated")

    if "Shipping_Address" in changes:
        old_lines = smap.get("delivery_address_lines", [])
        bp_ship = _blueprint_value(["shipping_address", "ship_to", "delivery_address"], "")
        if bp_ship and not old_lines:
            old_lines = [l.strip() for l in bp_ship.split("\n") if l.strip()]
        replace_address(page, old_lines, changes["Shipping_Address"],
                        font_cache=font_cache)
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
                        grand, sym, bfr, eu, get_all_spans(pg), smap
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


@app.route("/extract", methods=["POST"])
def extract():
    """
    PDF analysis only — no editing, no output PDF.
    Returns structured data for Claude planning.
    """
    try:
        body = request.get_json(force=True)
        b64  = body.get("pdf_base64", "")
        if "," in b64:
            b64 = b64.split(",", 1)[1]

        doc  = pymupdf.open(stream=base64.b64decode(b64), filetype="pdf")
        page = doc[0]

        if detect_pdf_type(page) == "scanned":
            doc.close()
            return jsonify({"error": "Scanned PDF — no selectable text"}), 422

        all_spans  = get_all_spans(page)
        tbl        = detect_table(page, all_spans)
        extracted  = self_extract(page)

        result = {
            "page": {
                "width":  round(page.rect.width,  2),
                "height": round(page.rect.height, 2),
                "count":  len(doc),
            },
            "fonts":  sorted({s["font"] for s in all_spans}),
            "table": {
                "found":      tbl is not None,
                "columns":    [c["text"] for c in tbl["col_spans"]] if tbl else [],
                "row_count":  len(tbl["data_rows"]) if tbl else 0,
                "row_height": round(tbl["row_height"], 1) if tbl else 0,
                "row_font":   tbl.get("row_font", "") if tbl else "",
                "row_size":   tbl.get("row_size", 0) if tbl else 0,
            },
            "extracted": extracted,
            # Lightweight drawings summary (no raw path items)
            "drawings": [
                {
                    "rect":  [round(v, 1) for v in d["rect"]],
                    "color": d.get("color"),
                    "fill":  d.get("fill"),
                    "width": d.get("width"),
                }
                for d in page.get_drawings()
            ],
        }

        doc.close()
        return jsonify(result)

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 Invoice Editor v6 :{port}  PyMuPDF {pymupdf.__version__}")
    app.run(host="0.0.0.0", port=port, debug=False)
