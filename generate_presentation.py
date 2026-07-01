"""Generate the project presentation (docs/presentation.pptx) with python-pptx.

Run::

    python generate_presentation.py

The deck mirrors docs/PRESENTATION_OUTLINE.md. Edit the SLIDES list below (or the
outline) and regenerate.
"""
import os

from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN

# --- Brand palette (matches the web app) ---
PRIMARY = RGBColor(0x0D, 0x3B, 0x66)
ACCENT = RGBColor(0xEE, 0x6C, 0x4D)
LIGHT = RGBColor(0xF4, 0xF7, 0xFB)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
DARK = RGBColor(0x1F, 0x29, 0x33)

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "docs", "presentation.pptx")
BENCH_DIR = os.path.join(HERE, "docs", "benchmarks")

# (title, [bullets], subtitle_or_None)
SLIDES = [
    ("SwiftRoute", [
        "AI-Assisted Logistics & Parcel Delivery Platform",
        "Graduation Project — inspired by Bosta & DHL",
        "Built with Python, Flask, SQLAlchemy & Leaflet",
    ], "TITLE"),
    ("The Problem", [
        "E-commerce is booming; last-mile delivery is the bottleneck.",
        "Manual courier dispatch is slow, uneven and error-prone.",
        "Customers expect real-time visibility: \u201cwhere is my parcel?\u201d",
        "Operations teams lack tools to plan and monitor the fleet.",
    ], None),
    ("Project Goal", [
        "Deliver a complete, real-world system \u2014 not a prototype.",
        "Merchants ship \u2192 AI plans routes \u2192 couriers deliver \u2192 customers track.",
        "Python-first, web-based, and runnable on any machine.",
        "Database-agnostic: SQLite out of the box, PostgreSQL for production.",
    ], None),
    ("Inspiration: Bosta & DHL", [
        "Bosta: merchant onboarding, cash-on-delivery, status timeline, courier app.",
        "DHL: hub-and-spoke network, tracking numbers, proof of delivery.",
        "We replicate these core ideas at graduation-project scale.",
    ], None),
    ("System Overview", [
        "Four actors: Admin, Courier, Merchant, Public customer.",
        "Flow: Hub \u2192 Optimise \u2192 Courier \u2192 Delivery \u2192 Tracking.",
        "One Flask application with role-based portals and a JSON API.",
    ], None),
    ("Architecture", [
        "Application factory + blueprints (auth, admin, courier, merchant, tracking, api).",
        "SQLAlchemy ORM; SQLite by default, PostgreSQL via one env variable.",
        "Front-end: Jinja2 + Bootstrap 5 + Leaflet maps + Chart.js.",
        "psycopg3 driver for modern Python; Docker Compose for Postgres.",
    ], None),
    ("Data Model", [
        "User \u2014 admins, couriers, merchants in one table, separated by role.",
        "Hub \u2014 a warehouse with a fleet of couriers.",
        "Shipment \u2014 a parcel with a full lifecycle and coordinates.",
        "ShipmentEvent \u2014 append-only timeline; RouteStop \u2014 map geometry + ETA.",
    ], None),
    ("The AI Route Optimiser", [
        "Vehicle routing is NP-hard \u2014 we decompose it:",
        "1) k-means clustering decides which courier serves which parcels.",
        "2) A capacity-aware pass assigns parcels without overloading a vehicle.",
        "3) A selectable technique sequences each route (the TSP sub-problem).",
        "4) OSRM draws real road paths; ETA from distance, speed & traffic.",
        "Pure-Python fallbacks mean it always runs \u2014 heavy ML is optional.",
    ], None),
    ("Six Optimisation Techniques", [
        "FIFO \u2014 as received: the baseline to beat.",
        "Nearest Neighbour \u2014 fast greedy heuristic.",
        "2-opt \u2014 local search removing crossing/expensive edges (default).",
        "Or-opt \u2014 adds segment relocation; highest quality.",
        "Christofides (NetworkX) \u2014 proven 1.5\u00d7-optimal guarantee for metric TSP.",
        "Simulated Annealing (NetworkX) \u2014 metaheuristic escaping local optima.",
    ], None),
    ("Benchmark: How Much Does It Save?", [
        "Optimised routes cut total distance ~60\u201362% vs. naive FIFO dispatch.",
        "40 random scenarios per size \u00d7 sizes {8, 15, 25, 40}, fixed seed.",
    ], "improvement_by_strategy.png"),
    ("Benchmark: Optimality & Speed", [
        "On solvable instances, Or-opt is ~0.1% above optimal; 2-opt & SA < 1%.",
        "2-opt gives the best quality/speed balance; Christofides scales gently.",
    ], "runtime_scaling.png"),
    ("Shipment Lifecycle", [
        "pending \u2192 at_warehouse \u2192 out_for_delivery \u2192 delivered.",
        "Plus failed, returned and cancelled states.",
        "Every change is recorded as an immutable event.",
        "The customer-facing timeline is built from these events.",
    ], None),
    ("Difficult Choices & Trade-offs", [
        "SQLite vs PostgreSQL \u2192 support both via one DATABASE_URL.",
        "Heavy ML libraries \u2192 made optional, never required.",
        "Global vs project .env \u2192 self-contained config (a real bug we fixed).",
        "Geocoding vs \u201cdrop a pin\u201d \u2192 map pin + landmark text.",
        "SPA vs server-rendered \u2192 server-rendered, single stack.",
    ], None),
    ("Security", [
        "Passwords hashed with Werkzeug (PBKDF2).",
        "CSRF protection on every browser form.",
        "Role-based access control via a decorator.",
        "Server-side validation and open-redirect protection.",
    ], None),
    ("Live Demo Flow", [
        "Merchant creates a shipment by dropping a pin on the map.",
        "Admin marks it At-Warehouse and runs the optimiser.",
        "Courier sees the route, delivers, captures proof of delivery.",
        "Anyone tracks the parcel publicly by its number.",
    ], None),
    ("Testing & Quality", [
        "End-to-end tests over the real create \u2192 optimise \u2192 deliver \u2192 track flow.",
        "Role-protection tests ensure permissions hold.",
        "Optimiser test verifies routes are assigned and measured.",
        "All tests pass on the in-memory database.",
    ], None),
    ("Future Work", [
        "SMS / email notifications on status changes.",
        "Cash-on-delivery settlement and merchant payouts.",
        "Live GPS tracking and a native courier mobile app.",
        "ML-driven demand forecasting and dynamic re-routing.",
    ], None),
    ("Thank You", [
        "Questions & discussion.",
        "Demo: admin@swiftroute.app / courier1@swiftroute.app / merchant1@swiftroute.app",
        "Repository: see the project README.",
    ], "TITLE"),
]


def _set_bg(slide, color):
    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = color


def _title_slide(prs, title, bullets):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _set_bg(slide, PRIMARY)
    box = slide.shapes.add_textbox(Inches(0.8), Inches(2.0), Inches(11.7), Inches(3.5))
    tf = box.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    run = p.add_run(); run.text = title
    run.font.size = Pt(54); run.font.bold = True; run.font.color.rgb = WHITE
    p.alignment = PP_ALIGN.CENTER
    for b in bullets:
        para = tf.add_paragraph()
        r = para.add_run(); r.text = b
        r.font.size = Pt(20); r.font.color.rgb = RGBColor(0xCF, 0xDD, 0xEC)
        para.alignment = PP_ALIGN.CENTER
        para.space_before = Pt(10)
    # accent bar
    bar = slide.shapes.add_shape(1, Inches(5.4), Inches(1.7), Inches(2.5), Inches(0.08))
    bar.fill.solid(); bar.fill.fore_color.rgb = ACCENT; bar.line.fill.background()


def _content_slide(prs, title, bullets):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _set_bg(slide, LIGHT)
    # header band
    band = slide.shapes.add_shape(1, Inches(0), Inches(0), Inches(13.333), Inches(1.25))
    band.fill.solid(); band.fill.fore_color.rgb = PRIMARY; band.line.fill.background()
    tbox = band.text_frame; tbox.word_wrap = True
    tbox.margin_left = Inches(0.6)
    p = tbox.paragraphs[0]
    r = p.add_run(); r.text = title
    r.font.size = Pt(30); r.font.bold = True; r.font.color.rgb = WHITE

    body = slide.shapes.add_textbox(Inches(0.9), Inches(1.7), Inches(11.5), Inches(5.4))
    tf = body.text_frame; tf.word_wrap = True
    for i, b in enumerate(bullets):
        para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        bullet = para.add_run(); bullet.text = "\u25B8  "
        bullet.font.size = Pt(20); bullet.font.color.rgb = ACCENT; bullet.font.bold = True
        run = para.add_run(); run.text = b
        run.font.size = Pt(20); run.font.color.rgb = DARK
        para.space_after = Pt(14)


def _image_slide(prs, title, bullets, image_filename):
    """A content slide whose lower half showcases a benchmark chart image."""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _set_bg(slide, LIGHT)
    band = slide.shapes.add_shape(1, Inches(0), Inches(0), Inches(13.333), Inches(1.25))
    band.fill.solid(); band.fill.fore_color.rgb = PRIMARY; band.line.fill.background()
    tbox = band.text_frame; tbox.word_wrap = True
    tbox.margin_left = Inches(0.6)
    p = tbox.paragraphs[0]
    r = p.add_run(); r.text = title
    r.font.size = Pt(30); r.font.bold = True; r.font.color.rgb = WHITE

    body = slide.shapes.add_textbox(Inches(0.9), Inches(1.45), Inches(11.5), Inches(1.4))
    tf = body.text_frame; tf.word_wrap = True
    for i, b in enumerate(bullets):
        para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        bullet = para.add_run(); bullet.text = "\u25B8  "
        bullet.font.size = Pt(16); bullet.font.color.rgb = ACCENT; bullet.font.bold = True
        run = para.add_run(); run.text = b
        run.font.size = Pt(16); run.font.color.rgb = DARK
        para.space_after = Pt(6)

    path = os.path.join(BENCH_DIR, image_filename)
    if os.path.exists(path):
        pic_w = Inches(7.47)  # keeps the 8:4.5 chart aspect ratio at 4.2" tall
        left = (Inches(13.333) - pic_w) / 2
        slide.shapes.add_picture(path, left, Inches(2.9), height=Inches(4.2))


def build():
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    for title, bullets, kind in SLIDES:
        if kind == "TITLE":
            _title_slide(prs, title, bullets)
        elif isinstance(kind, str) and kind.endswith(".png"):
            _image_slide(prs, title, bullets, kind)
        else:
            _content_slide(prs, title, bullets)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    prs.save(OUT)
    print(f"Presentation written to {OUT} ({len(SLIDES)} slides).")


if __name__ == "__main__":
    build()
