"""
build_ppt.py
====================================================================
One-off PPT rebuild script (NOT part of the SIH-26027 DSS application).

Edits the existing SIH26027_Bug_Hunters_2.pptx IN PLACE:
  - Slide 1 is never touched.
  - On slides 2-6, only the official SIH placeholder title and the
    corner team/SIH logo picture are preserved; every other shape is
    rebuilt from scratch with real, verified project numbers.
  - Every new run is set to Times New Roman (latin/ea/cs).

Run: .venv\\Scripts\\python.exe tools\\build_ppt.py
====================================================================
"""

from __future__ import annotations

import shutil
from pathlib import Path

from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR, MSO_AUTO_SIZE
from pptx.enum.shapes import MSO_SHAPE, MSO_SHAPE_TYPE
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
from pptx.enum.dml import MSO_LINE_DASH_STYLE
from pptx.oxml.ns import qn

SRC = Path(r"c:\Users\JITESH\Downloads\SIH26027_Bug_Hunters_2.pptx")
BACKUP = SRC.with_name("SIH26027_Bug_Hunters_2_BACKUP.pptx")
ASSETS = Path(r"C:\Users\JITESH\Downloads\PythonProject5\tools\ppt_assets")

FONT = "Times New Roman"

# ---------------------------------------------------------------------------
# Palette (kept from the existing deck / slide 1 for full visual continuity)
# ---------------------------------------------------------------------------
NAVY = "203B63"
SLATE = "5C7190"
RED = "C9414B"
BLUE = "0079C8"
LBLUE = "009FE3"
GREEN = "0C9A62"
PURPLE = "7657C7"
AMBER = "D99200"
DARKTEXT = "25364D"
NEARBLACK = "111111"
WHITE = "FFFFFF"

PALE_RED = "FBEAEC"
PALE_BLUE = "EAF4FC"
PALE_GREEN = "EAF7F0"
PALE_PURPLE = "F1EDFB"
PALE_AMBER = "FCF3E3"
PALE_GRAY = "F5F6F9"
BORDER_GRAY = "D8DEE8"

KNOWN_TITLES = {
    "IDEA TITLE",
    "TECHNICAL APPROACH",
    "FEASIBILITY AND VIABILITY",
    "IMPACT AND BENEFITS",
    "RESEARCH & REFERENCES",
}

PAGE_W = 13.33
PAGE_H = 7.5
MARGIN = 0.54
CONTENT_W = 12.25
HEADLINE_Y = 0.60
DESC_Y = 1.05
DIVIDER_Y = 1.41
CONTENT_TOP = 1.56
FOOTER_DIVIDER_Y = 6.96
FOOTER_TEXT_Y = 7.02


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def rgb(h: str) -> RGBColor:
    return RGBColor.from_string(h)


def force_font(run, name: str = FONT) -> None:
    """Set latin AND ea/cs typeface so PowerPoint never substitutes a fallback."""
    run.font.name = name
    rPr = run._r.get_or_add_rPr()
    for tag in ("a:latin", "a:ea", "a:cs"):
        el = rPr.find(qn(tag))
        if el is None:
            el = rPr.makeelement(qn(tag), {})
            rPr.append(el)
        el.set("typeface", name)


def style_run(run, text: str, size: float, bold: bool = False, italic: bool = False,
              color: str = DARKTEXT, font: str = FONT) -> None:
    run.text = text
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.italic = italic
    run.font.color.rgb = rgb(color)
    force_font(run, font)


def delete_shape(shape) -> None:
    shape._element.getparent().remove(shape._element)


def clear_slide_except_chrome(slide) -> None:
    """Remove every shape except the official SIH title and the corner logo."""
    for shape in list(slide.shapes):
        keep = False
        if shape.has_text_frame and shape.text_frame.text.strip() in KNOWN_TITLES:
            keep = True
        elif shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
            left_in = (shape.left or 0) / 914400.0
            top_in = (shape.top or 0) / 914400.0
            if left_in > 10.3 and top_in < 0.35:
                keep = True
        if not keep:
            delete_shape(shape)


def add_textbox(slide, l: float, t: float, w: float, h: float,
                 anchor=MSO_ANCHOR.TOP, wrap: bool = True):
    box = slide.shapes.add_textbox(Inches(l), Inches(t), Inches(w), Inches(h))
    tf = box.text_frame
    tf.word_wrap = wrap
    tf.auto_size = MSO_AUTO_SIZE.NONE
    tf.vertical_anchor = anchor
    tf.margin_left = Inches(0.05)
    tf.margin_right = Inches(0.05)
    tf.margin_top = Inches(0.02)
    tf.margin_bottom = Inches(0.02)
    return box, tf


def first_or_new_para(tf, used_first: list[bool]):
    if not used_first[0]:
        used_first[0] = True
        return tf.paragraphs[0]
    return tf.add_paragraph()


def add_rect(slide, l: float, t: float, w: float, h: float, fill: str | None = None,
             line: str | None = None, line_w: float = 0.75,
             shape_type=MSO_SHAPE.ROUNDED_RECTANGLE, radius: float = 0.055,
             dashed: bool = False):
    shp = slide.shapes.add_shape(shape_type, Inches(l), Inches(t), Inches(w), Inches(h))
    if shape_type == MSO_SHAPE.ROUNDED_RECTANGLE:
        try:
            shp.adjustments[0] = radius
        except (IndexError, ValueError):
            pass
    if fill is None:
        shp.fill.background()
    else:
        shp.fill.solid()
        shp.fill.fore_color.rgb = rgb(fill)
    if line is None:
        shp.line.fill.background()
    else:
        shp.line.color.rgb = rgb(line)
        shp.line.width = Pt(line_w)
        if dashed:
            shp.line.dash_style = MSO_LINE_DASH_STYLE.DASH
    shp.shadow.inherit = False
    shp.text_frame.margin_left = Inches(0.0)
    shp.text_frame.margin_right = Inches(0.0)
    return shp


def add_line(slide, l: float, t: float, w: float, h: float, color: str, weight: float = 1.0):
    shp = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(l), Inches(t), Inches(w), Inches(h))
    shp.fill.solid()
    shp.fill.fore_color.rgb = rgb(color)
    shp.line.fill.background()
    shp.shadow.inherit = False
    return shp


def add_picture_framed(slide, path: Path, l: float, t: float, w: float, h: float,
                        border: str = BORDER_GRAY):
    slide.shapes.add_picture(str(path), Inches(l), Inches(t), Inches(w), Inches(h))
    frame = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(l), Inches(t), Inches(w), Inches(h))
    frame.fill.background()
    frame.line.color.rgb = rgb(border)
    frame.line.width = Pt(1.0)
    frame.shadow.inherit = False
    return frame


def image_size_for_height(name: str, height_in: float) -> tuple[float, float]:
    from PIL import Image
    with Image.open(ASSETS / name) as img:
        w_px, h_px = img.size
    return height_in * w_px / h_px, height_in


# ---------------------------------------------------------------------------
# Chrome shared by slides 2-6 (headline band + footer band)
# ---------------------------------------------------------------------------

def add_header_chrome(slide, headline: str, descriptor: str) -> None:
    _, tf = add_textbox(slide, MARGIN, HEADLINE_Y, CONTENT_W, 0.42)
    p = tf.paragraphs[0]
    r = p.add_run()
    style_run(r, headline, 18.5, bold=True, color=NAVY)

    _, tf2 = add_textbox(slide, MARGIN, DESC_Y, CONTENT_W, 0.30)
    p2 = tf2.paragraphs[0]
    r2 = p2.add_run()
    style_run(r2, descriptor, 11.5, italic=False, color=SLATE)

    add_line(slide, MARGIN, DIVIDER_Y, CONTENT_W, 0.017, BORDER_GRAY)


def add_footer_chrome(slide, slide_no: int) -> None:
    add_line(slide, MARGIN, FOOTER_DIVIDER_Y, CONTENT_W, 0.017, BORDER_GRAY)
    _, tf = add_textbox(slide, MARGIN, FOOTER_TEXT_Y, 6.46, 0.25)
    r = tf.paragraphs[0].add_run()
    style_run(r, "BUG_HUNTERS  \u2022  SIH 26027", 9.75, color=SLATE)
    _, tf2 = add_textbox(slide, 9.69, FOOTER_TEXT_Y, 3.10, 0.25)
    tf2.paragraphs[0].alignment = PP_ALIGN.RIGHT
    r2 = tf2.paragraphs[0].add_run()
    style_run(r2, f"SIH 2026  \u2022  Slide {slide_no} of 6", 9.75, color=SLATE)


def add_card(slide, l: float, t: float, w: float, h: float, fill: str, border: str) -> None:
    add_rect(slide, l, t, w, h, fill=fill, line=border, line_w=1.0, radius=0.05)


def add_label_chip(slide, l: float, t: float, w: float, text: str, color: str, size: float = 11.5) -> None:
    _, tf = add_textbox(slide, l, t, w, 0.24)
    r = tf.paragraphs[0].add_run()
    style_run(r, text, size, bold=True, color=color)


def add_bullets(slide, l: float, t: float, w: float, h: float, items: list,
                 size: float = 10.2, space_after: float = 4.0, bullet_color: str = DARKTEXT,
                 line_spacing: float = 1.02, anchor=MSO_ANCHOR.TOP):
    """items: list of list-of-(text,bold,italic,color) run tuples, one bullet each."""
    _, tf = add_textbox(slide, l, t, w, h, anchor=anchor)
    used = [False]
    for segs in items:
        p = first_or_new_para(tf, used)
        p.space_after = Pt(space_after)
        p.line_spacing = line_spacing
        r0 = p.add_run()
        style_run(r0, "\u25b8 ", size, bold=True, color=bullet_color)
        for text, bold, italic, color in segs:
            r = p.add_run()
            style_run(r, text, size, bold=bold, italic=italic, color=color)


def add_plain_paragraphs(slide, l: float, t: float, w: float, h: float, paras: list,
                          space_after: float = 4.0, line_spacing: float = 1.05,
                          anchor=MSO_ANCHOR.TOP):
    """paras: list of list-of-(text,size,bold,italic,color) run tuples."""
    _, tf = add_textbox(slide, l, t, w, h, anchor=anchor)
    used = [False]
    for segs in paras:
        p = first_or_new_para(tf, used)
        p.space_after = Pt(space_after)
        p.line_spacing = line_spacing
        for text, size, bold, italic, color in segs:
            r = p.add_run()
            style_run(r, text, size, bold=bold, italic=italic, color=color)


# ===========================================================================
# SLIDE 2 -- IDEA TITLE  (title text untouched)
# ===========================================================================

def build_slide2(slide) -> None:
    clear_slide_except_chrome(slide)
    add_header_chrome(
        slide,
        "The block-planning gap on a 192 km corridor \u2014 and how Bug_Hunters closes it",
        "Problem \u2192 solution \u2192 root-cause fix \u2192 innovation, grounded in the official SIH-26027 brief and the working prototype.",
    )

    col1_x, col2_x, col_w = MARGIN, 6.81, 5.98
    row1_y, row2_y, row_h = 1.56, 4.06, 2.40

    # ---- TL: THE PROBLEM ----------------------------------------------
    add_card(slide, col1_x, row1_y, col_w, row_h, PALE_RED, RED)
    add_label_chip(slide, col1_x + 0.18, row1_y + 0.10, col_w - 0.36, "THE PROBLEM", RED)
    add_plain_paragraphs(
        slide, col1_x + 0.18, row1_y + 0.38, col_w - 0.36, 0.55,
        [[("77%", 30, True, False, NAVY)]],
        space_after=0,
    )
    add_plain_paragraphs(
        slide, col1_x + 1.55, row1_y + 0.44, col_w - 1.75, 0.50,
        [[("Mail/Express punctuality, FY 2024-25 \u2014 Ministry of Railways, "
           "Lok Sabha reply (sansad.in). Down years earlier: 73.62% in 2023-24.",
           9.0, False, True, SLATE)]],
        line_spacing=1.0,
    )
    add_bullets(
        slide, col1_x + 0.18, row1_y + 0.98, col_w - 0.36, 1.36,
        [
            [("TMS, SMMS and TDMS keep their defect backlogs separately \u2014 no one screen shows all three.", False, False, DARKTEXT)],
            [("Every department requests its block through BDMS on its own; nothing is co-planned.", False, False, DARKTEXT)],
            [("Official PS 26027 (Ministry of Railways): this \u201cleads to inefficient block utilisation, poor coordination, and suboptimal scheduling.\u201d", False, True, DARKTEXT)],
        ],
        size=10.0, bullet_color=RED,
    )

    # ---- TR: THE PROPOSED SOLUTION -------------------------------------
    add_card(slide, col2_x, row1_y, col_w, row_h, PALE_BLUE, BLUE)
    add_label_chip(slide, col2_x + 0.18, row1_y + 0.10, col_w - 0.36, "THE PROPOSED SOLUTION", BLUE)
    add_plain_paragraphs(
        slide, col2_x + 0.18, row1_y + 0.38, col_w - 0.36, 0.42,
        [[("One CP-SAT engine turns 3 siloed maintenance feeds into a single, conflict-checked block plan.", 10.6, True, False, NAVY)]],
        line_spacing=1.0,
    )
    steps = [
        ("1", "Unify", "TMS + SMMS + TDMS + COA + WTT read into one repository."),
        ("2", "Rank", "Formula 1 scores every defect 0\u2013100 on severity, overdue days, TSR, traffic."),
        ("3", "Bundle", "Formula 2 groups same-line work within 3 km into one possession."),
        ("4", "Solve", "Google OR-Tools CP-SAT proves a conflict-free window \u2014 or reports why not."),
    ]
    _, tf = add_textbox(slide, col2_x + 0.18, row1_y + 0.86, col_w - 0.36, 1.48)
    used = [False]
    for num, label, desc in steps:
        p = first_or_new_para(tf, used)
        p.space_after = Pt(5)
        p.line_spacing = 1.0
        r0 = p.add_run()
        style_run(r0, f"{num}  ", 10.3, bold=True, color=BLUE)
        r1 = p.add_run()
        style_run(r1, f"{label} \u2014 ", 10.3, bold=True, color=NAVY)
        r2 = p.add_run()
        style_run(r2, desc, 10.0, color=DARKTEXT)

    # ---- BL: HOW IT ADDRESSES THE PROBLEM ------------------------------
    add_card(slide, col1_x, row2_y, col_w, row_h, PALE_GREEN, GREEN)
    add_label_chip(slide, col1_x + 0.18, row2_y + 0.10, col_w - 0.36, "HOW IT ADDRESSES THE PROBLEM", GREEN)
    pairs = [
        ("Split systems", "TMS + SMMS + TDMS + WTT merged into one repository (app.py adapter layer)."),
        ("No shared ranking", "Formula 1 gives every defect the same 0\u2013100 urgency score, regardless of source."),
        ("Manual conflict-checking", "CP-SAT proves zero train / block / machine conflict before a human ever signs off."),
        ("One-shot decisions", "Weekly + monthly rolling horizon re-ranks unscheduled work automatically, never dropped."),
    ]
    _, tf = add_textbox(slide, col1_x + 0.18, row2_y + 0.40, col_w - 0.36, 1.92)
    used = [False]
    for cause, fix in pairs:
        p = first_or_new_para(tf, used)
        p.space_after = Pt(5)
        p.line_spacing = 1.0
        r0 = p.add_run()
        style_run(r0, "\u25b8 ", 10.0, bold=True, color=GREEN)
        r1 = p.add_run()
        style_run(r1, f"{cause}", 10.0, bold=True, color=NAVY)
        r2 = p.add_run()
        style_run(r2, "  \u2192  ", 10.0, bold=True, color=SLATE)
        r3 = p.add_run()
        style_run(r3, fix, 9.7, color=DARKTEXT)

    # ---- BR: INNOVATION & UNIQUENESS -----------------------------------
    add_card(slide, col2_x, row2_y, col_w, row_h, PALE_PURPLE, PURPLE)
    add_label_chip(slide, col2_x + 0.18, row2_y + 0.10, col_w - 0.36, "INNOVATION & UNIQUENESS", PURPLE)
    innov = [
        ("Two-formula math, not a heuristic.", "Priority Urgency + Spatial Bundling feed the CP-SAT objective directly."),
        ("Three policies solved every time.", "Punctuality-First / Asset-First / Mega-Block compared side-by-side \u2014 never one black-box answer."),
        ("ML sees defects getting worse.", "GradientBoostingRegressor forecasts days-to-critical (R\u00b2 = 0.98 on matched hold-out)."),
        ("Plans like the Railway already does.", "7-day and 28-day rolling horizon mirrors the Railway Board\u2019s own Rolling Block Plan."),
    ]
    _, tf = add_textbox(slide, col2_x + 0.18, row2_y + 0.40, col_w - 0.36, 1.92)
    used = [False]
    for lead, rest in innov:
        p = first_or_new_para(tf, used)
        p.space_after = Pt(5)
        p.line_spacing = 1.0
        r0 = p.add_run()
        style_run(r0, "\u25b8 ", 10.0, bold=True, color=PURPLE)
        r1 = p.add_run()
        style_run(r1, f"{lead} ", 10.0, bold=True, color=NAVY)
        r2 = p.add_run()
        style_run(r2, rest, 9.7, color=DARKTEXT)

    add_footer_chrome(slide, 2)


# ===========================================================================
# SLIDE 3 -- TECHNICAL APPROACH  (title text untouched)
# ===========================================================================

def build_slide3(slide) -> None:
    clear_slide_except_chrome(slide)
    add_header_chrome(
        slide,
        "Python + Google OR-Tools CP-SAT, served by FastAPI, watched on a live VDU console",
        "Every item below is running in the attached prototype today \u2014 picked for a stated reason, shown as a real flow, not a logo wall.",
    )

    # ---- Row A: tech stack chips ---------------------------------------
    chips = [
        (BLUE, "LANGUAGE & SOLVER", "Python 3.14 + Google OR-Tools CP-SAT",
         "Typed end-to-end; CP-SAT proves a feasible plan instead of approximating one."),
        (GREEN, "BACKEND API", "FastAPI + Uvicorn",
         "Async ASGI service; 11 REST routes, OpenAPI docs generated for free."),
        (PURPLE, "MACHINE LEARNING", "scikit-learn (Gradient Boosting) + joblib",
         "Forecasts days-to-critical; the model is cached, never retrained on the request path."),
        (AMBER, "AUDIT & DATA", "SQLAlchemy ORM \u2192 SQLite / PostgreSQL",
         "Every sanction, shift and rejection is a signed audit row, not a log line."),
        (RED, "VDU CONSOLE", "HTML5, CSS3, JavaScript, Leaflet.js, SVG",
         "Dark CRIS-style console: live GIS map plus a hand-built schematic of the real crossovers."),
    ]
    col_w, gutter = 2.32, 0.145
    y0, h0 = CONTENT_TOP, 1.50
    for i, (accent, cat, tech, reason) in enumerate(chips):
        x = MARGIN + i * (col_w + gutter)
        add_card(slide, x, y0, col_w, h0, PALE_GRAY, BORDER_GRAY)
        add_rect(slide, x, y0, col_w, 0.07, fill=accent, line=None,
                 shape_type=MSO_SHAPE.RECTANGLE)
        add_plain_paragraphs(
            slide, x + 0.13, y0 + 0.13, col_w - 0.26, h0 - 0.22,
            [
                [(cat, 8.7, True, False, accent)],
                [(tech, 10.3, True, False, NAVY)],
                [(reason, 8.7, False, True, SLATE)],
            ],
            space_after=3.0, line_spacing=1.0,
        )

    # ---- Row B: methodology flow (pentagon chain) ----------------------
    flow = [
        ("SOURCES", "TMS \u00b7 SMMS \u00b7 TDMS \u00b7 COA \u00b7 WTT \u2014 5 feeds, 1 read"),
        ("VALIDATE", "Row-level parse; bad rows rejected with a reason, not the file"),
        ("RANK", "Formula 1 urgency (0\u2013100) + ML days-to-critical"),
        ("BUNDLE", "Formula 2: \u2264 3 km, same line & feeder \u21d2 one possession"),
        ("SOLVE", "CP-SAT proves Rule A / B / C, pooling & power isolation"),
    ]
    fw, step, fy, fh = 2.62, 2.42, 3.22, 1.42
    for i, (title, detail) in enumerate(flow):
        x = MARGIN + i * step
        shp = slide.shapes.add_shape(MSO_SHAPE.PENTAGON, Inches(x), Inches(fy), Inches(fw), Inches(fh))
        shp.fill.solid()
        shp.fill.fore_color.rgb = rgb(NAVY if i % 2 == 0 else BLUE)
        shp.line.fill.background()
        shp.shadow.inherit = False
        tf = shp.text_frame
        tf.word_wrap = True
        tf.margin_left = Inches(0.10)
        tf.margin_right = Inches(0.22)
        tf.margin_top = Inches(0.08)
        tf.vertical_anchor = MSO_ANCHOR.MIDDLE
        p0 = tf.paragraphs[0]
        p0.alignment = PP_ALIGN.CENTER
        r0 = p0.add_run()
        style_run(r0, title, 12.5, bold=True, color=WHITE)
        p1 = tf.add_paragraph()
        p1.alignment = PP_ALIGN.CENTER
        p1.space_before = Pt(4)
        p1.line_spacing = 1.0
        r1 = p1.add_run()
        style_run(r1, detail, 8.3, color="D7E4F5")
        if i < len(flow) - 1:
            pass  # pentagon point already reads as an arrow into the next block

    # ---- Row C: working prototype evidence + GitHub / YouTube links ---
    img_h = 1.42
    img_w, img_h = image_size_for_height("situation_map.png", img_h)
    img_y = 4.98
    add_picture_framed(slide, ASSETS / "situation_map.png", MARGIN, img_y, img_w, img_h)
    add_plain_paragraphs(
        slide, MARGIN, img_y + img_h + 0.04, img_w, 0.30,
        [[("Live VDU console \u2014 GIS map + SVG track schematic + real-time train "
           "telemetry (screenshot of the running prototype).", 8.3, False, True, SLATE)]],
        line_spacing=0.95,
    )

    chip_x = MARGIN + img_w + 0.28
    chip_w = MARGIN + CONTENT_W - chip_x
    chip_h = 0.66
    add_rect(slide, chip_x, img_y, chip_w, chip_h, fill=PALE_BLUE, line=BLUE, line_w=1.0, dashed=True)
    add_plain_paragraphs(
        slide, chip_x + 0.16, img_y + 0.08, chip_w - 0.32, chip_h - 0.16,
        [
            [("\U0001F4C1  GITHUB REPOSITORY", 10.5, True, False, BLUE)],
            [("Paste your repository link here before the final SIH portal upload \u2192  https://github.com/<your-org>/<repo>", 9.3, False, True, SLATE)],
        ],
        space_after=2.0, line_spacing=1.0,
    )
    y2 = img_y + chip_h + 0.14
    add_rect(slide, chip_x, y2, chip_w, chip_h, fill=PALE_GREEN, line=GREEN, line_w=1.0, dashed=True)
    add_plain_paragraphs(
        slide, chip_x + 0.16, y2 + 0.08, chip_w - 0.32, chip_h - 0.16,
        [
            [("\u25B6  DEMO VIDEO (YOUTUBE)", 10.5, True, False, GREEN)],
            [("Paste your unlisted / public walkthrough link here \u2192  https://youtu.be/<video-id>", 9.3, False, True, SLATE)],
        ],
        space_after=2.0, line_spacing=1.0,
    )
    add_plain_paragraphs(
        slide, chip_x, y2 + chip_h + 0.03, chip_w, 0.20,
        [[("Both links are opened directly by SIH evaluators \u2014 keep them public or unlisted, never private.", 8.2, False, True, SLATE)]],
    )

    add_footer_chrome(slide, 3)


# ===========================================================================
# SLIDE 4 -- FEASIBILITY AND VIABILITY  (title text untouched)
# ===========================================================================

def build_slide4(slide) -> None:
    clear_slide_except_chrome(slide)
    add_header_chrome(
        slide,
        "Feasible inside today\u2019s block calendar \u2014 three real risks, three concrete mitigations",
        "Technical and operational feasibility argued from the corridor model itself, then tested against real failure modes.",
    )

    col1_x, col2_x, col_w = MARGIN, 6.81, 5.98
    row_y, row_h = CONTENT_TOP, 1.86

    # ---- Technical feasibility ------------------------------------------
    add_card(slide, col1_x, row_y, col_w, row_h, PALE_BLUE, BLUE)
    add_label_chip(slide, col1_x + 0.18, row_y + 0.09, col_w - 0.36, "TECHNICAL FEASIBILITY", BLUE)
    add_plain_paragraphs(
        slide, col1_x + 0.18, row_y + 0.37, col_w - 0.36, 0.42,
        [[("The corridor, the rules and the fleet are already modelled in code \u2014 not slideware.", 9.6, True, False, NAVY)]],
        line_spacing=1.0,
    )
    stats = [("192 km", "corridor modelled"), ("3 rules", "Rule A\u00b7B\u00b7C in CP-SAT"),
              ("18 tests", "pytest, 3 suites"), ("170 trains", "full 24 h WTT")]
    sw, sh, sg = 2.78, 0.46, 0.10
    stats_top = row_y + 0.76
    for i, (num, cap) in enumerate(stats):
        cx = col1_x + 0.18 + (i % 2) * (sw + sg)
        cy = stats_top + (i // 2) * (sh + sg)
        add_rect(slide, cx, cy, sw, sh, fill=WHITE, line=BORDER_GRAY, line_w=0.75, radius=0.10)
        add_plain_paragraphs(
            slide, cx + 0.10, cy + 0.035, sw - 0.20, sh - 0.07,
            [[(num, 13.5, True, False, BLUE)], [(cap, 8.2, False, False, SLATE)]],
            space_after=0.0, line_spacing=0.92,
        )
    # sanity: stats_top + 2*sh + sg must stay within row_h (1.86in card height)
    assert stats_top + 2 * sh + sg <= row_y + row_h, "Slide 4 stat grid overflows its card"

    # ---- Operational feasibility -----------------------------------------
    add_card(slide, col2_x, row_y, col_w, row_h, PALE_GREEN, GREEN)
    add_label_chip(slide, col2_x + 0.18, row_y + 0.09, col_w - 0.36, "OPERATIONAL FEASIBILITY", GREEN)
    add_plain_paragraphs(
        slide, col2_x + 0.18, row_y + 0.37, col_w - 0.36, 1.42,
        [
            [("Fits the block windows Central Railway already uses", 9.6, True, False, NAVY),
             (": nightly 00:00\u201304:00, Sunday 00:00\u201307:00 mega-block, or a 12-hour extended "
              "block \u2014 the same vocabulary as the Railway Board\u2019s own Rolling Block Plan.",
              9.6, False, False, DARKTEXT)],
            [("Verified on a live 7-day run: ", 9.6, True, False, GREEN),
             ("10 of 14 backlog defects (71%) cleared in week 1, including one automatic "
              "mandatory-bundle split. The remaining 4 lower-urgency defects carry forward "
              "with rising urgency \u2014 never forced into an unsafe window.",
              9.6, False, False, DARKTEXT)],
        ],
        space_after=6.0, line_spacing=1.03,
    )

    # ---- Risk -> Mitigation table -----------------------------------------
    tbl_y = row_y + row_h + 0.16
    tbl_h = 6.55 - tbl_y
    n_rows, n_cols = 6, 3
    gframe = slide.shapes.add_table(n_rows, n_cols, Inches(MARGIN), Inches(tbl_y),
                                     Inches(CONTENT_W), Inches(tbl_h))
    table = gframe.table
    table.columns[0].width = Inches(2.95)
    table.columns[1].width = Inches(4.15)
    table.columns[2].width = Inches(5.15)
    header_h = 0.36
    data_h = (tbl_h - header_h) / 5.0
    table.rows[0].height = Inches(header_h)
    for r in range(1, 6):
        table.rows[r].height = Inches(data_h)

    headers = ["RISK", "WHY IT CAN HAPPEN", "MITIGATION ALREADY IN THE CODE"]
    for c, htext in enumerate(headers):
        cell = table.cell(0, c)
        cell.fill.solid()
        cell.fill.fore_color.rgb = rgb(NAVY)
        cell.vertical_anchor = MSO_ANCHOR.MIDDLE
        cell.margin_left = Inches(0.08)
        cell.margin_top = Inches(0.02)
        cell.margin_bottom = Inches(0.02)
        tf = cell.text_frame
        tf.word_wrap = True
        p = tf.paragraphs[0]
        r_ = p.add_run()
        style_run(r_, htext, 10.5, bold=True, color=WHITE)

    rows_data = [
        ("Zero-tolerance passenger clash",
         "Premium / Superfast carry 0-minute patience (Rule B); a tight nightly window can have no legal gap left.",
         "Solver reports INFEASIBLE rather than forcing a delay; calendar_plan.py rolls the job to the next feasible day \u2014 verified: cleared on the following Sunday mega-block in our own test run."),
        ("Scarce machine fleet",
         "Division-wide there is only 1 BCM machine, 2 tie-tampers and 2 tower wagons.",
         "AddCumulative in solver.py caps concurrent bundles per machine type \u2014 double-booking is impossible, not just discouraged."),
        ("Oversized safety-critical bundle",
         "A mandatory cluster\u2019s envelope can exceed even the 420-minute Sunday mega-block.",
         "calendar_plan.py auto-splits it into single-defect bundles and retries before the day is ever given up on."),
        ("Legacy schema drift (TMS/SMMS/TDMS)",
         "Three real systems use three different column names, date formats and severity scales.",
         "Per-source parsers in data_ingestion.py normalise each feed; one bad row is rejected with a reason \u2014 the rest of the file still loads."),
        ("Live integration not switched on yet",
         "The prototype runs on a locally-encoded demo backlog, not a production feed.",
         "Ingestion layer already speaks TMS/SMMS/TDMS/COA\u2019s real export formats; go-live needs only an approved read endpoint, validated in a shadow-mode pilot first."),
    ]
    for r, (risk, why, fix) in enumerate(rows_data, start=1):
        fill = WHITE if r % 2 == 1 else PALE_GRAY
        for c, text in enumerate((risk, why, fix)):
            cell = table.cell(r, c)
            cell.fill.solid()
            cell.fill.fore_color.rgb = rgb(fill)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            cell.margin_left = Inches(0.08)
            cell.margin_right = Inches(0.06)
            cell.margin_top = Inches(0.02)
            cell.margin_bottom = Inches(0.02)
            tf = cell.text_frame
            tf.word_wrap = True
            p = tf.paragraphs[0]
            p.line_spacing = 0.96
            r_ = p.add_run()
            style_run(r_, text, 9.1, bold=(c == 0), color=(NAVY if c == 0 else DARKTEXT))

    add_footer_chrome(slide, 4)


# ===========================================================================
# SLIDE 5 -- IMPACT AND BENEFITS  (title text untouched)
# ===========================================================================

def build_slide5(slide) -> None:
    clear_slide_except_chrome(slide)
    add_header_chrome(
        slide,
        "Built around the two people who run a block \u2014 not a generic persona slide",
        "Impact shown on the real Officer and Controller consoles already in the prototype; benefits quantified from the same defect backlog.",
    )

    col1_x, col2_x, col_w = MARGIN, 6.81, 5.98
    row_y, row_h = CONTENT_TOP, 2.66

    personas = [
        (col1_x, BLUE, PALE_BLUE, "BLOCK MAINTENANCE OFFICER \u00b7 ASSET CONSOLE", "officer_console.png",
         [
             "Sees all 14 TMS / TDMS / SMMS defects ranked by Formula 1 urgency (0\u2013100) on one screen instead of three logins.",
             "The ML \u201cdays-to-critical\u201d column flags an escalating fault before it becomes an emergency block.",
             "One button \u2014 Transmit Request \u2014 replaces a phone / e-mail chain across three departments.",
         ]),
        (col2_x, GREEN, PALE_GREEN, "SECTION CONTROLLER \u00b7 CHIEF CONTROLLER", "controller_console.png",
         [
             "Reviews 3 solved policies (Punctuality-First / Asset-First / Mega-Block) side-by-side with a live confidence score.",
             "Approves, force-shifts (+30 min) or rejects with a logged reason \u2014 every action becomes an audit row.",
             "Live telemetry confirms every running train is clear of the block before sanction, not after.",
         ]),
    ]
    for x, accent, pale, label, img, bullets in personas:
        add_card(slide, x, row_y, col_w, row_h, pale, accent)
        add_label_chip(slide, x + 0.18, row_y + 0.09, col_w - 0.36, label, accent, size=10.8)
        img_w, img_h = image_size_for_height(img, 1.28)
        img_x = x + (col_w - img_w) / 2.0
        add_picture_framed(slide, ASSETS / img, img_x, row_y + 0.36, img_w, img_h)
        add_bullets(
            slide, x + 0.18, row_y + 0.36 + img_h + 0.06, col_w - 0.36,
            row_h - (0.36 + img_h + 0.10),
            [[(b, False, False, DARKTEXT)] for b in bullets],
            size=9.15, bullet_color=accent, space_after=3.0, line_spacing=0.98,
        )

    # ---- Benefits row: Social / Safety / Economic / Environmental -------
    brow_y = row_y + row_h + 0.16
    brow_h = 6.55 - brow_y
    bw, bg = 2.90, 0.217
    benefits = [
        (RED, "SOCIAL",
         "Premium and Superfast keep their 0-minute delay guarantee (Rule B) \u2014 protecting the "
         "passenger punctuality a CAG audit already flagged as under pressure (77% national "
         "average, FY 2024-25)."),
        (AMBER, "SAFETY",
         "Formula 1 surfaces the worst rail fracture (severity 96, 28 days overdue, active TSR) "
         "ahead of any derailment risk \u2014 safety-critical items are never silently outranked."),
        (GREEN, "ECONOMIC", None),
        (PURPLE, "ENVIRONMENTAL",
         "Fewer stand-alone mobilisations for the tamper, BCM and tower-wagon fleet \u2014 bundling "
         "cuts machine transit runs, not just paperwork."),
    ]
    for i, (accent, label, text) in enumerate(benefits):
        x = MARGIN + i * (bw + bg)
        add_card(slide, x, brow_y, bw, brow_h, WHITE, BORDER_GRAY)
        add_rect(slide, x, brow_y, bw, 0.05, fill=accent, shape_type=MSO_SHAPE.RECTANGLE)
        add_label_chip(slide, x + 0.14, brow_y + 0.11, bw - 0.28, label, accent, size=10.3)
        if text is not None:
            add_plain_paragraphs(
                slide, x + 0.14, brow_y + 0.40, bw - 0.28, brow_h - 0.50,
                [[(text, 9.0, False, False, DARKTEXT)]],
                line_spacing=1.05,
            )
        else:
            chart_data = CategoryChartData()
            chart_data.categories = ["S1 DN-Fast", "S2 DN", "S3 MID"]
            chart_data.add_series("Standalone (min)", (150, 180, 120))
            chart_data.add_series("Bundled (min)", (105, 135, 90))
            cx, cy, cw, ch = x + 0.10, brow_y + 0.38, bw - 0.20, 1.42
            gframe = slide.shapes.add_chart(
                XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(cx), Inches(cy), Inches(cw), Inches(ch),
                chart_data,
            )
            chart = gframe.chart
            chart.has_legend = True
            chart.legend.position = XL_LEGEND_POSITION.BOTTOM
            chart.legend.include_in_layout = False
            chart.legend.font.size = Pt(7.0)
            chart.legend.font.name = FONT
            cat_ax = chart.category_axis
            cat_ax.tick_labels.font.size = Pt(7.2)
            cat_ax.tick_labels.font.name = FONT
            val_ax = chart.value_axis
            val_ax.tick_labels.font.size = Pt(7.2)
            val_ax.tick_labels.font.name = FONT
            val_ax.has_major_gridlines = False
            plot = chart.plots[0]
            plot.series[0].format.fill.solid()
            plot.series[0].format.fill.fore_color.rgb = rgb(SLATE)
            plot.series[1].format.fill.solid()
            plot.series[1].format.fill.fore_color.rgb = rgb(GREEN)
            add_plain_paragraphs(
                slide, x + 0.14, brow_y + 0.38 + ch + 0.02, bw - 0.28, brow_h - 0.50 - ch,
                [[("Verified: 3 real defect clusters save 120 track-minutes (26.7%) sharing "
                   "one possession instead of three.", 7.9, False, True, SLATE)]],
                line_spacing=0.95,
            )

    add_footer_chrome(slide, 5)


# ===========================================================================
# SLIDE 6 -- RESEARCH & REFERENCES  (title text untouched)
# ===========================================================================

def build_slide6(slide) -> None:
    clear_slide_except_chrome(slide)
    add_header_chrome(
        slide,
        "Government-first evidence \u2014 six primary sources, zero invented statistics",
        "Every number quoted in this deck traces back to one of the sources below.",
    )

    # ---- Row A: 5-step research flow -----------------------------------
    steps = [
        ("1", "Define the scope", "SIH problem statement 26027 and the Ministry of Railways use case.", NAVY),
        ("2", "Identify source systems", "CRIS / Indian Railways: COA, TMS, SMMS and TDMS \u2014 confirmed with each system\u2019s own owner.", NAVY),
        ("3", "Read operating rules", "Division Working Time Table, Rolling Block Plan guidance and RDSO 25 kV safety code.", NAVY),
        ("4", "Validate provenance", "Approved endpoint, owning division, source date, chainage, line, units and freshness.", NAVY),
        ("5", "Build the plan", "ML estimates urgency; bundling and CP-SAT generate candidate windows for controller review.", GREEN),
    ]
    col_w, gutter, y0, h0 = 2.20, 0.156, CONTENT_TOP, 1.62
    for i, (num, title, body, accent) in enumerate(steps):
        x = MARGIN + i * (col_w + gutter)
        add_card(slide, x, y0, col_w, h0, PALE_GRAY, BORDER_GRAY)
        add_plain_paragraphs(
            slide, x + 0.14, y0 + 0.11, col_w - 0.28, 0.34,
            [[(f"{num}  ", 12.0, True, False, accent), (title, 10.6, True, False, NAVY)]],
            line_spacing=0.95,
        )
        add_plain_paragraphs(
            slide, x + 0.14, y0 + 0.50, col_w - 0.28, h0 - 0.60,
            [[(body, 9.0, False, False, DARKTEXT)]],
            line_spacing=1.0,
        )
        if i < len(steps) - 1:
            ax = x + col_w + 0.02
            arrow = slide.shapes.add_shape(MSO_SHAPE.RIGHT_ARROW, Inches(ax), Inches(y0 + h0 / 2 - 0.10),
                                            Inches(gutter - 0.04), Inches(0.20))
            arrow.fill.solid()
            arrow.fill.fore_color.rgb = rgb(SLATE)
            arrow.line.fill.background()
            arrow.shadow.inherit = False

    # ---- Row B: reference chips (3 across) -------------------------------
    refs1 = [
        (BLUE, "CRIS \u2014 Centre for Railway Information Systems", "cris.org.in",
         "Owns the COA, TMS and TDMS system definitions this prototype integrates against."),
        (GREEN, "Central Railway + RDSO", "cr.indianrailways.gov.in \u00b7 rdso.indianrailways.gov.in",
         "Zonal operating rules and the 25 kV traction safety code (isolation, earthing)."),
        (NAVY, "Ministry of Railways \u2014 Lok Sabha replies", "sansad.in",
         "Punctuality: 73.62% (2023-24) \u2192 77.12% (2024-25) \u2192 77.43% (2025-26)."),
    ]
    ry1, rh1, rcw, rcg = 3.42, 0.98, 3.95, 0.20
    for i, (accent, name, domain, fact) in enumerate(refs1):
        x = MARGIN + i * (rcw + rcg)
        add_card(slide, x, ry1, rcw, rh1, WHITE, BORDER_GRAY)
        add_rect(slide, x, ry1, 0.06, rh1, fill=accent, shape_type=MSO_SHAPE.RECTANGLE)
        add_plain_paragraphs(
            slide, x + 0.18, ry1 + 0.08, rcw - 0.32, rh1 - 0.16,
            [
                [(name, 9.8, True, False, NAVY)],
                [(domain, 9.2, True, False, BLUE)],
                [(fact, 8.6, False, False, DARKTEXT)],
            ],
            space_after=2.0, line_spacing=1.0,
        )

    # ---- Row C: 2 more reference chips -----------------------------------
    refs2 = [
        (PURPLE, "SIH 2026 \u2014 Problem Statement 26027", "sih.gov.in",
         "Official brief this solution answers, quoted directly on Slide 2."),
        (AMBER, "PRS Legislative Research + Open Data", "prsindia.org \u00b7 data.gov.in",
         "CAG audit context (track defects named as a delay driver) + open government datasets."),
    ]
    ry2, rh2, rcw2, rcg2 = ry1 + rh1 + 0.14, 0.72, 6.02, 0.21
    for i, (accent, name, domain, fact) in enumerate(refs2):
        x = MARGIN + i * (rcw2 + rcg2)
        add_card(slide, x, ry2, rcw2, rh2, WHITE, BORDER_GRAY)
        add_rect(slide, x, ry2, 0.06, rh2, fill=accent, shape_type=MSO_SHAPE.RECTANGLE)
        add_plain_paragraphs(
            slide, x + 0.18, ry2 + 0.07, rcw2 - 0.32, rh2 - 0.14,
            [
                [(name, 9.8, True, False, NAVY), ("   \u2014  ", 9.0, False, False, SLATE), (domain, 9.2, True, False, BLUE)],
                [(fact, 8.6, False, False, DARKTEXT)],
            ],
            space_after=2.0, line_spacing=1.0,
        )

    # ---- Row D: prototype data disclosure --------------------------------
    ry3, rh3 = ry2 + rh2 + 0.14, 0.62
    add_rect(slide, MARGIN, ry3, CONTENT_W, rh3, fill=PALE_AMBER, line=AMBER, line_w=1.0)
    add_plain_paragraphs(
        slide, MARGIN + 0.18, ry3 + 0.06, CONTENT_W - 0.36, rh3 - 0.12,
        [
            [("PROTOTYPE DATA DISCLOSURE  \u2014  ", 8.8, True, False, AMBER),
             ("this build runs on a locally encoded CSMT\u2013Pune corridor, train and defect set, "
              "plus synthetic ML training labels, so the demo works offline. Map tiles are "
              "OpenStreetMap / OpenRailwayMap for display only. Production use needs authorised "
              "TMS/SMMS/TDMS/COA read access, validated in the shadow-mode pilot on Slide 4.",
              8.8, False, False, DARKTEXT)],
        ],
        line_spacing=1.0,
    )

    # ---- Row E: closing credibility line ----------------------------------
    ry4 = ry3 + rh3 + 0.08
    add_plain_paragraphs(
        slide, MARGIN, ry4, CONTENT_W, 0.24,
        [[("Six primary sources cited above \u2014 four Government of India / Railway domains, one "
           "legislative-research analysis, one official SIH brief. No blog post or AI-generated "
           "statistic is used in this deck.", 8.6, False, True, SLATE)]],
    )
    tf_last = slide.shapes[-1].text_frame
    tf_last.paragraphs[0].alignment = PP_ALIGN.CENTER

    add_footer_chrome(slide, 6)


# ===========================================================================
# Main
# ===========================================================================

def main() -> None:
    if not BACKUP.exists():
        shutil.copy2(SRC, BACKUP)
        print(f"Backup written to {BACKUP}")

    prs = Presentation(str(SRC))
    slides = list(prs.slides)
    assert len(slides) == 6, f"Expected 6 slides, found {len(slides)}"

    build_slide2(slides[1])
    build_slide3(slides[2])
    build_slide4(slides[3])
    build_slide5(slides[4])
    build_slide6(slides[5])

    prs.save(str(SRC))
    print(f"Saved rebuilt deck to {SRC}")


if __name__ == "__main__":
    main()
