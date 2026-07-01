"""Capture the full SwiftRoute screenshot set with Playwright (headless Chromium).

Developer utility (not part of the running app). It drives a real browser
through *every* screen — public, admin, merchant, courier and the whole AI/ML
suite — plus a rendered test-results report, saving PNGs to ``docs/screenshots/``.

Prerequisites (one-time)::

    pip install playwright
    python -m playwright install chromium

Usage::

    # 1. In one terminal, seed + run the app:
    python seed.py && python run.py
    # 2. In another terminal:
    python scripts/capture_screenshots.py

Environment overrides: SHOT_BASE (default http://127.0.0.1:5000).
"""
import os
import re
import subprocess
import sys
import tempfile

from playwright.sync_api import sync_playwright

BASE = os.environ.get("SHOT_BASE", "http://127.0.0.1:5000")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)  # so ``import app`` works when run as a script
OUT = os.path.join(ROOT, "docs", "screenshots")
os.makedirs(OUT, exist_ok=True)

ADMIN = ("admin@swiftroute.app", "admin12345")
MERCHANT = ("merchant1@swiftroute.app", "merchant123")
COURIER = ("courier1@swiftroute.app", "courier123")


# --------------------------------------------------------------------------- #
#  Dynamic ids straight from the seeded database (read-only)
# --------------------------------------------------------------------------- #
def discover():
    """Pull real tracking numbers / ids so deep-link screenshots are populated."""
    from app import create_app
    from app.extensions import db  # noqa: F401
    from app.models import User, Shipment, ShipmentStatus

    app = create_app()
    data = {}
    with app.app_context():
        delivered = (Shipment.query
                     .filter_by(status=ShipmentStatus.DELIVERED)
                     .order_by(Shipment.id).first())
        ofd = (Shipment.query
               .filter_by(status=ShipmentStatus.OUT_FOR_DELIVERY)
               .order_by(Shipment.id).first())
        courier = User.query.filter_by(email=COURIER[0]).first()
        merchant = User.query.filter_by(email=MERCHANT[0]).first()
        courier_ship = None
        if courier:
            courier_ship = (Shipment.query
                            .filter_by(courier_id=courier.id)
                            .filter(Shipment.status == ShipmentStatus.OUT_FOR_DELIVERY)
                            .order_by(Shipment.id).first())
        merchant_ship = None
        if merchant:
            merchant_ship = (Shipment.query
                             .filter_by(merchant_id=merchant.id)
                             .order_by(Shipment.id.desc()).first())
        data["tracking"] = (delivered or ofd).tracking_number if (delivered or ofd) else ""
        data["delivered_id"] = delivered.id if delivered else None
        data["ofd_id"] = (ofd or delivered).id if (ofd or delivered) else None
        data["courier_id"] = courier.id if courier else None
        data["courier_ship_id"] = (courier_ship or ofd).id if (courier_ship or ofd) else None
        data["merchant_ship_id"] = merchant_ship.id if merchant_ship else None
    return data


# --------------------------------------------------------------------------- #
#  Test-results report (rendered to an HTML page, then screenshotted)
# --------------------------------------------------------------------------- #
def build_test_report():
    """Run the suite and render a styled pass/fail table; return a file:// URL."""
    env = dict(os.environ, COLUMNS="200")  # wide output so verbose names don't wrap
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=line", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, env=env,
    )
    run_out = run.stdout + "\n" + run.stderr
    # Full, untruncated node-id list (one per line: tests/test_x.py::test_y).
    coll = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, env=env,
    )
    node_re = re.compile(r"^(tests/\S+?)::(.+)$")  # param ids may contain spaces
    failed = set(re.findall(r"^FAILED\s+(tests/\S+?::\S+)", run_out, re.M))
    skipped = set(re.findall(r"^SKIPPED\s+(tests/\S+?::\S+)", run_out, re.M))

    groups = {}
    counts = {"PASSED": 0, "FAILED": 0, "SKIPPED": 0}
    for line in coll.stdout.splitlines():
        m = node_re.match(line.strip())
        if not m:
            continue
        f, name = m.group(1), m.group(2)
        node = f"{f}::{name}"
        status = "FAILED" if node in failed else "SKIPPED" if node in skipped else "PASSED"
        groups.setdefault(f, []).append((name, status))
        counts[status] += 1
    total = sum(counts.values())
    summary = re.search(r"(\d+ passed[^\n]*)", run_out)
    summary_line = summary.group(1).strip() if summary else f"{total} collected"

    def badge(status):
        color = {"PASSED": "#2a9d8f", "FAILED": "#e76f51",
                 "SKIPPED": "#e9c46a"}.get(status, "#94a3b8")
        return f'<span class="badge" style="background:{color}">{status}</span>'

    rows = []
    for f in sorted(groups):
        rows.append(f'<tr class="file"><td colspan="2">{f} '
                    f'<span style="opacity:.7">({len(groups[f])})</span></td></tr>')
        for name, status in groups[f]:
            rows.append(f"<tr><td>{name}</td><td>{badge(status)}</td></tr>")

    html = f"""<!doctype html><html><head><meta charset="utf-8">
<style>
  body {{ font-family: 'Segoe UI', system-ui, sans-serif; background:#f4f7fb; margin:0; padding:32px; color:#0d1b2a; }}
  .wrap {{ max-width: 920px; margin: 0 auto; }}
  h1 {{ color:#0d3b66; margin:0 0 4px; }}
  .sub {{ color:#5b6b7b; margin-bottom:20px; }}
  .cards {{ display:flex; gap:14px; margin-bottom:22px; flex-wrap:wrap; }}
  .card {{ background:#fff; border-radius:14px; padding:16px 22px; box-shadow:0 6px 20px rgba(13,59,102,.06); }}
  .card .n {{ font-size:30px; font-weight:800; }}
  .card .l {{ font-size:12px; text-transform:uppercase; letter-spacing:.5px; color:#5b6b7b; }}
  table {{ width:100%; border-collapse:collapse; background:#fff; border-radius:14px; overflow:hidden; box-shadow:0 6px 20px rgba(13,59,102,.06); }}
  td {{ padding:9px 16px; border-bottom:1px solid #eef2f6; font-size:14px; }}
  tr.file td {{ background:#0d3b66; color:#fff; font-weight:700; font-family: ui-monospace, monospace; }}
  .badge {{ color:#fff; padding:2px 10px; border-radius:999px; font-size:12px; font-weight:700; }}
  .ok {{ color:#2a9d8f; font-weight:800; }}
</style></head><body><div class="wrap">
  <h1>SwiftRoute &mdash; Automated Test Suite</h1>
  <div class="sub">pytest &middot; <span class="ok">{summary_line}</span></div>
  <div class="cards">
    <div class="card"><div class="n">{total}</div><div class="l">Total</div></div>
    <div class="card"><div class="n" style="color:#2a9d8f">{counts.get('PASSED',0)}</div><div class="l">Passed</div></div>
    <div class="card"><div class="n" style="color:#e76f51">{counts.get('FAILED',0)}</div><div class="l">Failed</div></div>
    <div class="card"><div class="n" style="color:#e9c46a">{counts.get('SKIPPED',0)}</div><div class="l">Skipped</div></div>
  </div>
  <table><tbody>{''.join(rows)}</tbody></table>
</div></body></html>"""
    path = os.path.join(tempfile.gettempdir(), "swiftroute_test_report.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return "file:///" + path.replace("\\", "/"), counts


# --------------------------------------------------------------------------- #
#  Browser helpers
# --------------------------------------------------------------------------- #
def login(page, email, password):
    page.goto(f"{BASE}/auth/logout", wait_until="load")
    page.goto(f"{BASE}/auth/login", wait_until="load")
    page.fill("input[name=email]", email)
    page.fill("input[name=password]", password)
    page.click("input[type=submit], button[type=submit]")
    page.wait_for_load_state("load")
    page.wait_for_timeout(600)
    if page.url.rstrip("/").endswith("/auth/login"):
        raise RuntimeError(f"Login failed for {email}")


def shot(page, name, full=True, wait=1400):
    page.wait_for_timeout(wait)
    page.screenshot(path=os.path.join(OUT, name), full_page=full)
    print("saved", name)


def safe(fn, label):
    try:
        fn()
    except Exception as exc:  # keep going even if one screen misbehaves
        print(f"  !! skipped {label}: {exc}")


def _assistant_shot(page):
    def run():
        page.goto(f"{BASE}/admin/assistant", wait_until="load")
        page.wait_for_timeout(600)
        page.fill("#chatInput", "How many shipments are late risk today?")
        page.click("#chatSend")
        # Wait for the server's answer, then for the "thinking" bubble to be replaced.
        try:
            page.wait_for_response(lambda r: "assistant/ask" in r.url, timeout=30000)
        except Exception:
            pass
        try:
            page.wait_for_function(
                "!document.querySelector('#chatLog').innerText.toLowerCase().includes('thinking')",
                timeout=30000)
        except Exception:
            pass
        shot(page, "21-admin-assistant.png", wait=1200)
    return run


def main():
    ids = {}
    safe(lambda: ids.update(discover()), "discover ids")
    print("discovered:", ids)
    report_url, counts = "", {}
    safe(lambda: globals().__setitem__("_rpt", build_test_report()), "test report")
    if "_rpt" in globals():
        report_url, counts = globals()["_rpt"]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
        page = ctx.new_page()

        # ---- Public / auth ------------------------------------------------- #
        safe(lambda: (page.goto(f"{BASE}/", wait_until="load"),
                      shot(page, "01-landing.png", wait=1600)), "landing")
        safe(lambda: (page.goto(f"{BASE}/track/", wait_until="load"),
                      page.fill("input[name=tracking_number]", ids.get("tracking", "")),
                      page.press("input[name=tracking_number]", "Enter"),
                      page.wait_for_load_state("load"),
                      shot(page, "02-public-tracking.png", wait=1800)), "public tracking")
        safe(lambda: (page.goto(f"{BASE}/auth/login", wait_until="load"),
                      shot(page, "10-login.png", full=False, wait=800)), "login")
        safe(lambda: (page.goto(f"{BASE}/auth/register", wait_until="load"),
                      shot(page, "11-register.png", wait=1000)), "register")

        # ---- Admin --------------------------------------------------------- #
        safe(lambda: login(page, *ADMIN), "admin login")
        safe(lambda: (page.goto(f"{BASE}/admin/", wait_until="load"),
                      shot(page, "03-admin-dashboard.png", wait=2400)), "admin dashboard")
        safe(lambda: (page.goto(f"{BASE}/admin/map", wait_until="load"),
                      shot(page, "04-admin-live-map.png", full=False, wait=4200)), "admin map")
        safe(lambda: (page.goto(f"{BASE}/admin/shipments", wait_until="load"),
                      shot(page, "12-admin-shipments.png", wait=1400)), "admin shipments")
        if ids.get("ofd_id"):
            safe(lambda: (page.goto(f"{BASE}/admin/shipments/{ids['ofd_id']}", wait_until="load"),
                          shot(page, "13-admin-shipment-detail.png", wait=2200)), "admin ship detail")
        if ids.get("delivered_id"):
            safe(lambda: (page.goto(f"{BASE}/admin/shipments/{ids['delivered_id']}", wait_until="load"),
                          shot(page, "14-admin-shipment-audit.png", wait=2200)), "admin ship audit")
        safe(lambda: (page.goto(f"{BASE}/admin/couriers", wait_until="load"),
                      shot(page, "15-admin-couriers.png", wait=1400)), "admin couriers")
        safe(lambda: (page.goto(f"{BASE}/admin/closures", wait_until="load"),
                      shot(page, "07-admin-closures.png", wait=1600)), "admin closures")

        # ---- Admin · AI / ML suite ---------------------------------------- #
        safe(lambda: (page.goto(f"{BASE}/admin/ai", wait_until="load"),
                      shot(page, "16-admin-ai-overview.png", wait=2600)), "ai overview")
        safe(lambda: (page.goto(f"{BASE}/admin/ai/feedback", wait_until="load"),
                      shot(page, "17-admin-ai-feedback.png", wait=2600)), "ai feedback")
        safe(lambda: (page.goto(f"{BASE}/admin/ai/router", wait_until="load"),
                      shot(page, "18-admin-ai-router.png", wait=4200)), "ai router")
        safe(lambda: (page.goto(f"{BASE}/admin/ai/behavior", wait_until="load"),
                      shot(page, "19-admin-ai-behavior-fleet.png", wait=2600)), "ai behavior fleet")
        if ids.get("courier_id"):
            safe(lambda: (page.goto(f"{BASE}/admin/ai/behavior?courier_id={ids['courier_id']}", wait_until="load"),
                          shot(page, "20-admin-ai-behavior-detail.png", wait=3200)), "ai behavior detail")
        safe(_assistant_shot(page), "assistant")
        safe(lambda: (page.goto(f"{BASE}/admin/audit", wait_until="load"),
                      shot(page, "22-admin-audit.png", wait=1800)), "admin audit")

        # ---- Merchant ------------------------------------------------------ #
        safe(lambda: login(page, *MERCHANT), "merchant login")
        safe(lambda: (page.goto(f"{BASE}/merchant/", wait_until="load"),
                      shot(page, "23-merchant-dashboard.png", wait=1600)), "merchant dashboard")
        safe(lambda: (page.goto(f"{BASE}/merchant/shipments", wait_until="load"),
                      shot(page, "24-merchant-shipments.png", wait=1400)), "merchant shipments")
        safe(lambda: (page.goto(f"{BASE}/merchant/shipments/new", wait_until="load"),
                      shot(page, "05-merchant-create.png", wait=3200)), "merchant create")
        if ids.get("merchant_ship_id"):
            safe(lambda: (page.goto(f"{BASE}/merchant/shipments/{ids['merchant_ship_id']}", wait_until="load"),
                          shot(page, "25-merchant-shipment-detail.png", wait=1800)), "merchant ship detail")

        # ---- Courier ------------------------------------------------------- #
        safe(lambda: login(page, *COURIER), "courier login")
        safe(lambda: (page.goto(f"{BASE}/courier/", wait_until="load"),
                      shot(page, "06-courier-dashboard.png", wait=1600)), "courier dashboard")
        safe(lambda: (page.goto(f"{BASE}/courier/route", wait_until="load"),
                      shot(page, "09-courier-route-traffic.png", full=False, wait=4200)), "courier route")
        if ids.get("courier_ship_id"):
            safe(lambda: (page.goto(f"{BASE}/courier/shipment/{ids['courier_ship_id']}", wait_until="load"),
                          shot(page, "26-courier-shipment-detail.png", wait=2000)), "courier ship detail")

        # ---- Test results -------------------------------------------------- #
        if report_url:
            safe(lambda: (page.goto(report_url, wait_until="load"),
                          shot(page, "27-test-results.png", wait=800)), "test results")

        browser.close()
        print("All screenshots saved to", OUT)
        if counts:
            print("test counts:", counts)


if __name__ == "__main__":
    main()
