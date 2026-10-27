# -*- coding: utf-8 -*-
"""Build the Avishkar presentation and poster. Does not touch the SIH deck or the application."""

from __future__ import annotations

from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Cm, Emu, Inches, Pt

OUT_DIR_NAME = r"C:\Users\JITESH\Downloads\PythonProject5\deliverables\avishkar_2026"

NAVY = "1E3A5F"
BLUE = "1D4E89"
TEAL = "0F6E6B"
GREEN = "1E6B45"
SLATE = "5C6B7A"
TEXT = "243140"
MUTED = "5E6A75"
LINE = "D5DEE8"
WHITE = "FFFFFF"
PALE_BLUE = "EAF2F8"
PALE_TEAL = "E7F4F2"
PALE_GREEN = "E8F6EE"
PALE_AMBER = "FBF6EA"
PALE_RED = "F8EEEE"
SERIF = "Times New Roman"
SANS = "Arial"

TITLE = (
    "Integrated Railway Maintenance Prioritization and "
    "Constraint-Based Block Planning for Asset Availability"
)
CATEGORY = "5 — Engineering and Technology"
LEVEL = "________________"
SLOT = "________________"


def rgb(value: str) -> RGBColor:
    return RGBColor.from_string(value)


def font_run(run, name: str, size: float, color: str, bold: bool = False, italic: bool = False) -> None:
    run.font.name = name
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.italic = italic
    run.font.color.rgb = rgb(color)
    rpr = run._r.get_or_add_rPr()
    for tag in ("latin", "ea", "cs"):
        node = rpr.find(qn(f"a:{tag}"))
        if node is None:
            node = rpr.makeelement(qn(f"a:{tag}"), {})
            rpr.append(node)
        node.set("typeface", name)


def fill_shape(shape, color: str) -> None:
    shape.fill.solid()
    shape.fill.fore_color.rgb = rgb(color)
    shape.line.fill.background()


def box(slide, x, y, w, h, fill: str, line: str | None = None, weight: float = 1.0):
    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, x, y, w, h)
    shape.fill.solid()
    shape.fill.fore_color.rgb = rgb(fill)
    if line is None:
        shape.line.fill.background()
    else:
        shape.line.color.rgb = rgb(line)
        shape.line.width = Pt(weight)
    shape.shadow.inherit = False
    return shape


def write(slide, text: str, x, y, w, h, font: str, size: float, color: str, bold: bool = False,
          italic: bool = False, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP) -> None:
    shape = slide.shapes.add_textbox(x, y, w, h)
    frame = shape.text_frame
    frame.word_wrap = True
    frame.auto_size = None
    frame.margin_left = Emu(0)
    frame.margin_right = Emu(0)
    frame.margin_top = Emu(0)
    frame.margin_bottom = Emu(0)
    try:
        frame._txBody.bodyPr.set("anchor", {MSO_ANCHOR.TOP: "t", MSO_ANCHOR.MIDDLE: "ctr", MSO_ANCHOR.BOTTOM: "b"}[anchor])
    except Exception:
        pass
    paragraph = frame.paragraphs[0]
    paragraph.alignment = align
    paragraph.space_before = Pt(0)
    paragraph.space_after = Pt(0)
    run = paragraph.add_run()
    run.text = text
    font_run(run, font, size, color, bold, italic)
    return shape


def write_lines(slide, lines: list[str], x, y, w, h, font: str, size: float, color: str,
                bold: bool = False, align=PP_ALIGN.LEFT, spacing: float = 6) -> None:
    shape = slide.shapes.add_textbox(x, y, w, h)
    frame = shape.text_frame
    frame.word_wrap = True
    frame.auto_size = None
    frame.margin_left = Emu(0)
    frame.margin_right = Emu(0)
    frame.margin_top = Emu(0)
    frame.margin_bottom = Emu(0)
    for index, line in enumerate(lines):
        paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        paragraph.alignment = align
        paragraph.space_before = Pt(0)
        paragraph.space_after = Pt(spacing)
        run = paragraph.add_run()
        run.text = line
        font_run(run, font, size, color, bold)
    return shape


def blank(prs: Presentation):
    return prs.slides.add_slide(prs.slide_layouts[6])


def notes(slide, text: str) -> None:
    slide.notes_slide.notes_text_frame.text = text


def footer(slide, page: str) -> None:
    write(slide, "Prototype evidence. Not a live railway deployment.", Inches(0.48), Inches(7.16),
          Inches(8.6), Inches(0.24), SANS, 11, MUTED)
    write(slide, page, Inches(10.4), Inches(7.16), Inches(2.45), Inches(0.24), SANS, 11, MUTED, align=PP_ALIGN.RIGHT)


def slide_title(slide, kicker: str, title: str) -> None:
    write(slide, kicker, Inches(0.48), Inches(0.22), Inches(12.3), Inches(0.24), SANS, 12, TEAL, True)
    write(slide, title, Inches(0.48), Inches(0.44), Inches(12.3), Inches(0.48), SERIF, 28, NAVY, True)
    rule = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.48), Inches(1.02), Inches(12.36), Pt(1.25))
    fill_shape(rule, LINE)


def add_column_chart(slide, x, y, w, h) -> None:
    data = CategoryChartData()
    data.categories = ["S1  DN Fast", "S2  DN", "S3  Mid"]
    data.add_series("Separate jobs", (150, 180, 120))
    data.add_series("One bundle", (105, 135, 90))
    graphic = slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, x, y, w, h, data)
    chart = graphic.chart
    chart.has_title = False
    chart.has_legend = True
    chart.legend.position = XL_LEGEND_POSITION.BOTTOM
    chart.legend.include_in_layout = False
    chart.font.size = Pt(12)
    chart.font.name = SANS
    plot = chart.plots[0]
    plot.gap_width = 60
    chart.value_axis.minimum_scale = 0
    chart.value_axis.maximum_scale = 200
    chart.value_axis.has_major_gridlines = True
    chart.value_axis.has_title = True
    chart.value_axis.axis_title.text_frame.paragraphs[0].text = "Job minutes"
    chart.value_axis.axis_title.text_frame.paragraphs[0].font.size = Pt(11)
    chart.value_axis.axis_title.text_frame.paragraphs[0].font.name = SANS
    for series, color in zip(chart.series, ("8AA0B8", "1E6B45")):
        series.format.fill.solid()
        series.format.fill.fore_color.rgb = rgb(color)


def build_presentation(path: str) -> None:
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    prs.core_properties.title = TITLE
    prs.core_properties.author = ""
    prs.core_properties.last_modified_by = ""
    prs.core_properties.subject = "Aavishkar research presentation"
    s1(prs)
    s2(prs)
    s3(prs)
    s4(prs)
    s5(prs)
    s6(prs)
    prs.save(path)


def s1(prs: Presentation) -> None:
    slide = blank(prs)
    band = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, prs.slide_width, Inches(1.35))
    fill_shape(band, NAVY)
    write(slide, "AAVISHKAR", Inches(0.6), Inches(0.32), Inches(12), Inches(0.46), SERIF, 32, WHITE, True)
    write(slide, "Research Convention", Inches(0.62), Inches(0.84), Inches(10), Inches(0.28), SANS, 16, "D6E2EE")
    write(slide, "Title of the research project", Inches(0.6), Inches(1.7), Inches(12), Inches(0.28), SANS, 14, TEAL, True)
    write(slide, TITLE, Inches(0.6), Inches(2.05), Inches(12.1), Inches(1.7), SERIF, 32, NAVY, True)
    fields = [
        ("Category", CATEGORY),
        ("Level", LEVEL),
        ("Slot / Code No.", SLOT),
    ]
    for index, (label, value) in enumerate(fields):
        left = Inches(0.6 + index * 4.15)
        box(slide, left, Inches(4.15), Inches(3.9), Inches(1.85), PALE_BLUE, LINE, 1.0)
        write(slide, label, left + Inches(0.2), Inches(4.32), Inches(3.5), Inches(0.3), SANS, 13, TEAL, True)
        write(slide, value, left + Inches(0.2), Inches(4.75), Inches(3.5), Inches(0.9), SERIF, 22, NAVY, True)
    notes(slide, SLIDE_NOTES[1])


def s2(prs: Presentation) -> None:
    slide = blank(prs)
    slide_title(slide, "PROBLEM AND OBJECTIVE", "Maintenance is still requested department by department")
    panels = [
        (PALE_RED, "8C3A3A", "What is fragmented",
         "Engineering, traction, and signalling keep defect records in TMS, SMMS, and TDMS. Each department asks for its own block through BDMS. COA holds corridor availability. The timetable is a separate document. [1]"),
        (PALE_AMBER, "8A5A12", "Why the gap matters",
         "Mail/Express punctuality on Indian Railways was 73.62% in 2023–24 and 77.12% in 2024–25. Asset maintenance is one stated cause of delay. [2] In a 2017 audit of selected heavy-traffic sections, non-availability of blocks was the stated reason deep screening fell overdue. [4]"),
        (PALE_TEAL, TEAL, "Research objective",
         "Can separate maintenance records and a working timetable be turned into one explainable priority, a separate escalation forecast, shared possessions, and a feasible block a person still has to approve?"),
    ]
    for index, (fill, accent, heading, body) in enumerate(panels):
        left = Inches(0.48 + index * 4.2)
        box(slide, left, Inches(1.28), Inches(4.0), Inches(4.55), fill, LINE, 1.0)
        bar = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, left, Inches(1.28), Inches(0.08), Inches(4.55))
        fill_shape(bar, accent)
        write(slide, heading, left + Inches(0.24), Inches(1.44), Inches(3.6), Inches(0.7), SERIF, 20, NAVY, True)
        write(slide, body, left + Inches(0.24), Inches(2.25), Inches(3.58), Inches(3.3), SANS, 15, TEXT)
    write(slide, "Rolling Block, notified on 30 November 2023, already asks for integrated maintenance to be planned ahead. This prototype tests one way to do that on a modelled corridor. [3]",
          Inches(0.48), Inches(6.0), Inches(12.3), Inches(0.95), SANS, 14, TEXT)
    footer(slide, "2  /  6")
    notes(slide, SLIDE_NOTES[2])


def s3(prs: Presentation) -> None:
    slide = blank(prs)
    slide_title(slide, "METHOD", "One pipeline. The numbers are not merged.")
    write(slide, "TMS, SMMS, TDMS, COA, and WTT are the intended feeds. In this build they are file readers plus an encoded CSMT–Pune model, not live railway servers.",
          Inches(0.48), Inches(1.16), Inches(12.3), Inches(0.55), SANS, 14, TEXT)
    sources = ["TMS\nTrack", "SMMS\nSignalling", "TDMS\nTraction", "COA\nCorridor", "WTT\nTimetable"]
    for index, label in enumerate(sources):
        left = Inches(0.48 + index * 2.52)
        box(slide, left, Inches(1.78), Inches(2.35), Inches(0.78), PALE_BLUE, "B7C9DC", 1.0)
        write(slide, label, left + Inches(0.08), Inches(1.86), Inches(2.18), Inches(0.62), SANS, 13, NAVY, True, align=PP_ALIGN.CENTER)
    steps = [
        ("1", "Read and check", "Parse each export. A bad row is rejected with a reason."),
        ("2", "AI-1 review", "Local rules propose findings. A quote must be on the page. A person applies ready rows."),
        ("3", "Formula 1", "Explainable urgency from severity, overdue days, TSR, and traffic."),
        ("4", "Days to critical", "A separate forecast. It does not replace Formula 1."),
        ("5", "Formula 2", "Same line and feeder, next job within 3 km, one possession."),
        ("6", "CP-SAT", "OR-Tools searches a legal window under Rules A, B, and C."),
        ("7", "Dashboard", "Three policies are shown. A person sanctions, shifts, or rejects."),
    ]
    for index, (num, title, body) in enumerate(steps):
        row, col = divmod(index, 4)
        if row == 1:
            left = Inches(1.74 + col * 3.15)
        else:
            left = Inches(0.48 + col * 3.2)
        top = Inches(2.8 + row * 1.95)
        box(slide, left, top, Inches(3.02), Inches(1.78), WHITE, LINE, 1.0)
        write(slide, num, left + Inches(0.12), top + Inches(0.1), Inches(0.4), Inches(0.36), SERIF, 18, TEAL, True)
        write(slide, title, left + Inches(0.5), top + Inches(0.12), Inches(2.4), Inches(0.36), SERIF, 16, NAVY, True)
        write(slide, body, left + Inches(0.14), top + Inches(0.56), Inches(2.74), Inches(1.1), SANS, 13, TEXT)
    footer(slide, "3  /  6")
    notes(slide, SLIDE_NOTES[3])


def s4(prs: Presentation) -> None:
    slide = blank(prs)
    slide_title(slide, "CONTRIBUTION", "The combination is the research claim")
    items = [
        ("Shared records", "Department exports are normalised into one defect shape. Source severity is kept as reported. It is not rewritten as the urgency score."),
        ("Explainable priority", "Formula 1 is written down: safety, overdue days, an active speed restriction, and how busy the line is. The screen shows 0–100. The solver can use the unclipped score."),
        ("A separate forecast", "Gradient boosting estimates days until severity reaches 80. Training labels are synthetic. The forecast is shown beside Formula 1, not inside it."),
        ("One possession", "Formula 2 packs compatible jobs on the same line and feeder within 3 km. Work time becomes the longest job plus 15 minutes, instead of a fresh visit for each job."),
        ("A feasible window", "CP-SAT must keep trains and the gang apart, protect Premium and Superfast trains from induced delay, cap other delay, and respect the machine pool and power isolation."),
        ("A human decision", "Punctuality-First, Asset-First, and Mega-Block reweight the same constraints. AI-1 only prepares evidence-linked rows. It does not choose the block."),
    ]
    for index, (title, body) in enumerate(items):
        row, col = divmod(index, 3)
        left = Inches(0.48 + col * 4.2)
        top = Inches(1.25 + row * 2.75)
        box(slide, left, top, Inches(4.02), Inches(2.55), PALE_BLUE if row == 0 else PALE_TEAL, LINE, 1.0)
        write(slide, title, left + Inches(0.18), top + Inches(0.14), Inches(3.66), Inches(0.42), SERIF, 18, NAVY, True)
        write(slide, body, left + Inches(0.18), top + Inches(0.64), Inches(3.66), Inches(1.75), SANS, 14, TEXT)
    footer(slide, "4  /  6")
    notes(slide, SLIDE_NOTES[4])


def s5(prs: Presentation) -> None:
    slide = blank(prs)
    slide_title(slide, "PROTOTYPE EVALUATION", "What was tested, and what it showed")
    write_lines(
        slide,
        [
            "Encoded CSMT–Pune corridor model: 192 km.",
            "24-hour working timetable: 170 modelled services.",
            "Encoded demonstration backlog: 14 defects, 11 bundles.",
            "One 7-day run from 24 September 2026 placed 10 of those 14 defects. Four stayed for a later day. An unsafe window was not forced.",
            "Three multi-defect clusters: 450 job-minutes if done separately, 330 when bundled. Saved: 120 minutes (26.7%).",
        ],
        Inches(0.5), Inches(1.22), Inches(6.35), Inches(4.7),
        SANS, 16, TEXT, spacing=10,
    )
    box(slide, Inches(7.0), Inches(1.22), Inches(5.85), Inches(4.85), WHITE, LINE, 1.0)
    write(slide, "Bundled work minutes on the encoded backlog", Inches(7.15), Inches(1.32), Inches(5.55), Inches(0.4), SERIF, 15, NAVY, True)
    add_column_chart(slide, Inches(7.15), Inches(1.75), Inches(5.55), Inches(4.05))
    write(slide, "These are prototype simulations on local demo data. They are not divisional operating results. The live console backlog starts empty until a reviewed report is applied.",
          Inches(0.48), Inches(6.25), Inches(12.3), Inches(0.75), SANS, 14, TEXT)
    footer(slide, "5  /  6")
    notes(slide, SLIDE_NOTES[5])


def s6(prs: Presentation) -> None:
    slide = blank(prs)
    slide_title(slide, "CONCLUSION", "What this demonstrates, and what it does not")
    box(slide, Inches(0.48), Inches(1.22), Inches(6.2), Inches(2.55), PALE_GREEN, LINE, 1.0)
    write(slide, "Contribution", Inches(0.68), Inches(1.34), Inches(5.8), Inches(0.34), SERIF, 18, NAVY, True)
    write(slide, "The prototype shows a full path from separate maintenance records to a conflict-checked block: validate, review evidence, score urgency, forecast escalation separately, bundle nearby work, then let CP-SAT search a legal window for a person to sanction.",
          Inches(0.68), Inches(1.78), Inches(5.8), Inches(1.85), SANS, 15, TEXT)
    box(slide, Inches(6.88), Inches(1.22), Inches(5.95), Inches(2.55), PALE_AMBER, LINE, 1.0)
    write(slide, "Limits and next evidence", Inches(7.08), Inches(1.34), Inches(5.55), Inches(0.34), SERIF, 18, NAVY, True)
    write(slide, "Inputs are local and simulated. AI-1’s ordinary reader is rule-based and misses unfamiliar wording. The forecast is trained on synthetic labels, not real defect histories. A field claim needs authorised feeds and a shadow-mode trial.",
          Inches(7.08), Inches(1.78), Inches(5.55), Inches(1.85), SANS, 15, TEXT)
    write_lines(
        slide,
        [
            "[1] Ministry of Railways, SIH 2026 problem statement SIH26027.",
            "[2] Lok Sabha Unstarred Question 3976, answered 12 August 2026. Mail/Express punctuality on Indian Railways.",
            "[3] Rajya Sabha Starred Question 62, answered 5 December 2025. Rolling Block, Gazette notification 30 November 2023.",
            "[4] CAG Report No. 45 of 2017, tabled 13 March 2018. Selected heavy-traffic sections. Historical.",
        ],
        Inches(0.48), Inches(4.05), Inches(12.35), Inches(2.85),
        SANS, 16, TEXT, spacing=6,
    )
    footer(slide, "6  /  6")
    notes(slide, SLIDE_NOTES[6])


SLIDE_NOTES = {}  # filled below after the strings exist


def build_poster(path: str) -> None:
    prs = Presentation()
    prs.slide_width = Cm(100)
    prs.slide_height = Cm(100)
    prs.core_properties.title = TITLE
    prs.core_properties.author = ""
    prs.core_properties.last_modified_by = ""
    slide = blank(prs)
    frame = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Cm(0.6), Cm(0.6), Cm(98.8), Cm(98.8))
    frame.fill.background()
    frame.line.color.rgb = rgb(NAVY)
    frame.line.width = Pt(2.25)

    write(slide, "AAVISHKAR", Cm(2), Cm(1.2), Cm(96), Cm(1.6), SERIF, 34, NAVY, True, align=PP_ALIGN.CENTER)
    write(slide, "Title of the research project", Cm(2), Cm(2.9), Cm(96), Cm(0.8), SANS, 16, TEAL, True, align=PP_ALIGN.CENTER)
    write(slide, TITLE, Cm(3), Cm(3.7), Cm(94), Cm(2.8), SERIF, 26, NAVY, True, align=PP_ALIGN.CENTER)
    fields = [("Category", CATEGORY), ("Level", LEVEL), ("Slot / Code No.", SLOT)]
    for index, (label, value) in enumerate(fields):
        left = 2.2 + index * 31.8
        box(slide, Cm(left), Cm(6.7), Cm(30.8), Cm(2.8), PALE_BLUE, LINE, 1.0)
        write(slide, label, Cm(left + 0.4), Cm(6.85), Cm(30), Cm(0.7), SANS, 14, TEAL, True, align=PP_ALIGN.CENTER)
        write(slide, value, Cm(left + 0.4), Cm(7.55), Cm(30), Cm(1.6), SERIF, 18, NAVY, True, align=PP_ALIGN.CENTER)

    write(slide, "Problem", Cm(2.2), Cm(10.1), Cm(46), Cm(0.9), SERIF, 20, NAVY, True)
    write(slide, "Track, traction, and signalling request blocks separately. Defects sit in TMS, SMMS, and TDMS. Requests go through BDMS. COA holds the corridor, and the working timetable is separate. [1] Mail/Express punctuality on Indian Railways was 73.62% in 2023–24 and 77.12% in 2024–25. Asset maintenance is one stated cause of delay. [2]",
          Cm(2.2), Cm(11.0), Cm(47), Cm(5.4), SANS, 17, TEXT)
    write(slide, "Research objective", Cm(51), Cm(10.1), Cm(46), Cm(0.9), SERIF, 20, NAVY, True)
    write(slide, "Show one inspectable path from those records to a feasible possession. Urgency, the escalation forecast, and the block decision stay separate. A person still sanctions the window. Rolling Block, notified on 30 November 2023, is the operating idea this lines up with. [3]",
          Cm(51), Cm(11.0), Cm(46.5), Cm(5.4), SANS, 17, TEXT)

    band = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Cm(1.6), Cm(17.0), Cm(96.8), Cm(27.4))
    band.fill.solid()
    band.fill.fore_color.rgb = rgb("F7F9FB")
    band.line.color.rgb = rgb(LINE)
    band.line.width = Pt(1)
    write(slide, "Workflow  ·  source records to a window a person can accept", Cm(2.4), Cm(17.5), Cm(94), Cm(1.3), SERIF, 24, NAVY, True)
    sources = [("TMS", "Track defects"), ("SMMS", "Signalling"), ("TDMS", "Traction"), ("COA", "Corridor"), ("WTT", "Timetable")]
    for index, (name, detail) in enumerate(sources):
        left = 3.4 + index * 18.8
        box(slide, Cm(left), Cm(19.3), Cm(17.6), Cm(4.2), PALE_BLUE, "B7C9DC", 1.25)
        write(slide, name, Cm(left), Cm(19.5), Cm(17.6), Cm(1.8), SERIF, 22, NAVY, True, align=PP_ALIGN.CENTER)
        write(slide, detail, Cm(left), Cm(21.4), Cm(17.6), Cm(1.5), SANS, 15, SLATE, align=PP_ALIGN.CENTER)
    write(slide, "Intended feeds. This build uses file readers and an encoded 192 km CSMT–Pune corridor, not live servers.",
          Cm(2.4), Cm(23.8), Cm(94), Cm(1.0), SANS, 15, MUTED, italic=True)
    steps = [
        ("1  Read and check", "A bad row is rejected with a reason. The rest of the file still loads."),
        ("2  AI-1 review", "Local rules propose a finding. The quote must appear on the page. A person applies ready rows."),
        ("3  Formula 1", "Explainable urgency from severity, overdue days, a speed restriction, and traffic."),
        ("4  Days to critical", "A separate forecast of days until severity 80. It does not change Formula 1."),
        ("5  Formula 2", "Same line and feeder, next defect within 3 km, one possession."),
        ("6  CP-SAT", "OR-Tools searches a legal window, or it reports that the window is infeasible."),
        ("7  Dashboard", "Punctuality-First, Asset-First, and Mega-Block. A person sanctions, shifts, or rejects."),
    ]
    for index, (title, body) in enumerate(steps):
        row, col = divmod(index, 4)
        left = 2.6 + col * 24.0
        top = 25.3 + row * 8.6
        if row == 1:
            left = 14.6 + col * 24.0
        box(slide, Cm(left), Cm(top), Cm(22.8), Cm(7.8), WHITE, LINE, 1.15)
        write(slide, title, Cm(left + 0.5), Cm(top + 0.4), Cm(21.8), Cm(2.4), SERIF, 16, NAVY, True)
        write(slide, body, Cm(left + 0.5), Cm(top + 3.0), Cm(21.8), Cm(4.3), SANS, 14, TEXT)
    write(slide, "Severity, Formula 1, and the forecast are not added together. AI-1 does not rank the job and does not choose the block.",
          Cm(2.4), Cm(42.4), Cm(94), Cm(1.4), SANS, 16, TEXT)

    box(slide, Cm(1.8), Cm(45.2), Cm(31.2), Cm(22.5), PALE_TEAL, LINE, 1.0)
    write(slide, "What each step adds", Cm(2.4), Cm(45.7), Cm(30), Cm(1.2), SERIF, 22, NAVY, True)
    write(slide, "Formula 1 is the priority a controller can read: severity, overdue days, a speed restriction, and traffic. The forecast only estimates days until severity reaches 80, and its labels are synthetic. Formula 2 replaces repeated visits with the longest job plus 15 minutes. CP-SAT keeps a train and a gang apart, gives Premium and Superfast trains no induced delay, and caps the model fleet at 1 ballast cleaner, 2 tampers, and 2 tower wagons.",
          Cm(2.4), Cm(47.3), Cm(30), Cm(19.5), SANS, 18, TEXT)

    box(slide, Cm(34.2), Cm(45.2), Cm(31.6), Cm(22.5), WHITE, LINE, 1.0)
    write(slide, "Encoded-backlog result", Cm(34.8), Cm(45.7), Cm(30), Cm(1.2), SERIF, 22, NAVY, True)
    write(slide, "Job minutes. Not measured train delay.", Cm(34.8), Cm(47.1), Cm(30), Cm(1.0), SANS, 15, MUTED, italic=True)
    bars = [("S1 DN Fast", 150, 105), ("S2 DN", 180, 135), ("S3 Mid", 120, 90)]
    for index, (name, alone, bundled) in enumerate(bars):
        top = 48.6 + index * 4.2
        write(slide, name, Cm(34.8), Cm(top), Cm(8.5), Cm(1.5), SANS, 15, NAVY, True)
        box(slide, Cm(43.6), Cm(top), Cm(14.8 * alone / 200), Cm(1.25), "8AA0B8")
        box(slide, Cm(43.6), Cm(top + 1.5), Cm(14.8 * bundled / 200), Cm(1.25), GREEN)
        write(slide, str(alone), Cm(43.6 + 14.8 * alone / 200 + 0.3), Cm(top - 0.1), Cm(5), Cm(1.3), SANS, 14, SLATE)
        write(slide, str(bundled), Cm(43.6 + 14.8 * bundled / 200 + 0.3), Cm(top + 1.4), Cm(5), Cm(1.3), SANS, 14, GREEN)
    write(slide, "Grey: each job alone. Green: one bundle. 450 minutes became 330.",
          Cm(34.8), Cm(61.4), Cm(30), Cm(2.2), SANS, 16, TEXT)

    box(slide, Cm(67), Cm(45.2), Cm(31.2), Cm(22.5), PALE_AMBER, LINE, 1.0)
    write(slide, "Limits and next step", Cm(67.6), Cm(45.7), Cm(30), Cm(1.2), SERIF, 22, NAVY, True)
    write(slide, "This is not a live deployment. The ordinary AI-1 reader matches known phrases and misses unfamiliar wording. The forecast has not been tested on real defect histories: about 1 day of error on a matched synthetic hold-out, and about 3.8 days when that synthetic process changes. Next evidence is authorised feeds, a shadow-mode trial, and a held-out set of real reports.",
          Cm(67.6), Cm(47.3), Cm(29.8), Cm(19.5), SANS, 18, TEXT)

    facts = [
        ("192 km", "Encoded CSMT–Pune corridor"),
        ("170", "Services in the 24-hour model"),
        ("10 of 14", "Defects placed in one 7-day run"),
        ("120 min", "Saved on three clusters, 26.7%"),
    ]
    for index, (value, label) in enumerate(facts):
        left = 1.8 + index * 24.3
        box(slide, Cm(left), Cm(68.6), Cm(23.4), Cm(7.6), PALE_BLUE, LINE, 1.0)
        write(slide, value, Cm(left + 0.4), Cm(69.1), Cm(22.6), Cm(3.0), SERIF, 32, NAVY, True, align=PP_ALIGN.CENTER)
        write(slide, label, Cm(left + 0.6), Cm(72.3), Cm(22.2), Cm(3.2), SANS, 16, TEXT, align=PP_ALIGN.CENTER)

    write(slide, "References", Cm(2.2), Cm(77.2), Cm(30), Cm(1.0), SERIF, 20, NAVY, True)
    write_lines(
        slide,
        [
            "[1] Ministry of Railways, SIH 2026 problem statement SIH26027. Separate TMS, SMMS, and TDMS records; BDMS requests; COA corridor availability.",
            "[2] Lok Sabha Unstarred Question 3976, answered 12 August 2026. Mail/Express punctuality on Indian Railways: 73.62% (2023–24), 77.12% (2024–25), 77.43% (2025–26).",
            "[3] Rajya Sabha Starred Question 62, answered 5 December 2025. Asset maintenance is one stated cause of delay. Rolling Block: Gazette notification 30 November 2023.",
            "[4] CAG Report No. 45 of 2017, tabled 13 March 2018. Selected heavy-traffic sections. On 30 August 2017, Railway Board stated that non-availability of blocks was the main reason for overdue deep screening in those observations. Historical.",
        ],
        Cm(2.2), Cm(78.4), Cm(95.5), Cm(14),
        SANS, 16, TEXT, spacing=8,
    )
    write(slide, "The four figures above were recomputed from the encoded demonstration backlog and the 24-hour timetable model. They are not field operating results. The 7-day run starts on 24 September 2026.",
          Cm(2.2), Cm(93.2), Cm(95.5), Cm(3.6), SANS, 16, TEXT)
    prs.save(path)


SLIDE_NOTES[1] = """Open with the title only. Say that the level and the slot or code number will be the ones issued at registration, and that you will introduce yourself by that number. Do not say your name, college, department, or team. Give the title once, slowly: integrated railway maintenance prioritisation and constraint-based block planning for asset availability. Then move on. About 20 seconds."""

SLIDE_NOTES[2] = """The problem is coordination, not a claim that the railway has no maintenance process. Track, traction, and signalling each keep their own defect lists — TMS, SMMS, and TDMS — and each asks for a block through BDMS. The control office application holds the corridor, and the working timetable is another document. Smart India Hackathon problem statement 26027, from the Ministry of Railways, describes that split as a cause of poor block use.

Put the punctuality figures in their population and years. For Mail and Express trains on Indian Railways, Lok Sabha Unstarred Question 3976, answered on 12 August 2026, gives 73.62 percent in 2023–24 and 77.12 percent in 2024–25. The same reply gives 77.43 percent for 2025–26. Asset maintenance is one of the causes the Ministry lists, alongside fog, path constraints, alarm-chain pulling, and other events. Do not say punctuality is falling. It moved up between those two full years.

The older audit is historical. CAG Report 45 of 2017, tabled in March 2018, looked at selected heavy-traffic sections. At the exit conference in August 2017, Railway Board said non-availability of blocks was the main reason deep screening in those observations was overdue. That is not a figure for today’s network.

The research question is whether one prototype can keep three judgements separate: an explainable urgency score, a separate forecast of days to a critical severity, and a block window that a person still approves. Mention Rolling Block, notified on 30 November 2023, as the operating idea this work lines up with: integrated maintenance planned ahead, not invented on the day. About 70 seconds."""

SLIDE_NOTES[3] = """Walk the diagram from the top. The five names are the feeds the problem statement asks for. In this build they are readers for export files and a locally encoded CSMT to Pune corridor of 192 kilometres. Say clearly that production endpoints are not connected.

Then the seven steps. Reading and checking rejects a bad row without dropping the whole file. AI-1 is the report reader. On an ordinary upload it uses local sentence rules. It proposes a finding only when the wording matches a known defect phrase, and a second check keeps the finding only when the quoted words are actually on the page. A person applies the complete rows. It is not a trained generative model, and it does not score urgency.

Formula 1 then scores the open job. Beside it, a gradient-boosting model estimates days until severity reaches 80. Those two numbers stay separate. Formula 2 groups jobs on the same running line and the same power feeder when the next defect is within 3 kilometres. Google OR-Tools CP-SAT searches for a window that satisfies the hard rules. The dashboard shows the result for a person to sanction. About 80 seconds."""

SLIDE_NOTES[4] = """This slide is the contribution, so do not read every box. Say that a scheduling screen by itself is not the claim. The claim is that the steps stay inspectable and still fit together.

Formula 1 is written down. Safety comes from severity, then overdue days, then whether a temporary speed restriction is already on, then a traffic index for that line. The console shows a score from 0 to 100. The solver can keep the unclipped score so two severe jobs do not look identical.

The machine-learning piece does not replace that score. It forecasts days to criticality. Say, if asked, that the labels are synthetic. Do not quote R-squared as field accuracy; that belongs with the limits.

Formula 2 is how repeated mobilisation is reduced: one possession instead of one visit per job. CP-SAT is how the possession is allowed onto the timetable. Rule A keeps a train and a gang out of the same place at the same time. Rule B gives Premium and Superfast trains no induced delay. Rule C caps delay for other types. The machine pool in the model is one ballast cleaner, two tie tampers, and two tower wagons. A power block also requires isolation.

The three policies — Punctuality-First, Asset-First, and Mega-Block — change the weights. They do not hide a single answer. AI-1’s job ends when a reviewed record enters the backlog. About 70 seconds."""

SLIDE_NOTES[5] = """Say the test conditions before the percentages. The corridor is a 192 kilometre encoded model from CSMT to Pune Junction. The timetable is a 24-hour model of 170 services, not a live control-office feed. The backlog is 14 encoded demonstration defects, grouped into 11 bundles. The console’s live list actually starts empty until someone applies a reviewed report. These 14 defects are the demonstration catalog used for the test.

The chart is Formula 2 on the three clusters that contain more than one defect. If each job kept its own duration, the minutes are 150, 180, and 120. Bundled, using the longest job plus 15 minutes, they are 105, 135, and 90. Together that is 450 minutes against 330, so 120 minutes, or 26.7 percent, of those job minutes. These are maintenance-work minutes in the model. They are not measured minutes of train delay on the ground.

The week result is one simulation, from 24 September 2026 through seven days. Ten of the 14 defects were placed in a feasible window. Four were left for later. Where a night window could not hold a job, the solver reported infeasible and the plan moved or split the work rather than forcing the train conflict. Do not call this a field week. About 80 seconds."""

SLIDE_NOTES[6] = """Close on four sentences. The prototype demonstrates a path from separate records to a block a person can defend. The contribution is the split between evidence-backed reading, explainable urgency, a separate forecast, spatial bundling, and constraint solving. The important limits are local demo inputs, a rule-based report reader, and a forecast that has not seen real defect histories. The next evidence is authorised access, a shadow-mode comparison against today’s block decisions, and a held-out set of real reports.

If there is time, point at the four references and stop. Do not add a name or a team. Leave the last seconds for the judges. About 60 seconds. The whole talk should finish inside seven minutes, with the warning bell in mind at six."""


def main() -> None:
    from pathlib import Path
    out = Path(OUT_DIR_NAME)
    out.mkdir(parents=True, exist_ok=True)
    build_presentation(str(out / "avishkar_research_presentation.pptx"))
    build_poster(str(out / "avishkar_research_poster.pptx"))
    print("built")


if __name__ == "__main__":
    main()
