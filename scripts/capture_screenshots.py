"""Capture the README screenshots with Playwright (headless Chromium).

This is a developer utility, not part of the running app. It drives a real
browser through the main screens and saves PNGs to ``docs/screenshots/``.

Prerequisites (one-time)::

    pip install playwright
    python -m playwright install chromium

Usage::

    # 1. In one terminal, run the app with demo data:
    python seed.py && python run.py
    # 2. In another terminal:
    python scripts/capture_screenshots.py

Environment overrides: SHOT_BASE (default http://127.0.0.1:5000),
SHOT_TRACKING (a tracking number to feature on the public tracking page).
"""
import os

from playwright.sync_api import sync_playwright

BASE = os.environ.get("SHOT_BASE", "http://127.0.0.1:5000")
TRACKING = os.environ.get("SHOT_TRACKING", "SR-Q3AJKWN4")
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "screenshots")
os.makedirs(OUT, exist_ok=True)

ADMIN = ("admin@swiftroute.app", "admin12345")
MERCHANT = ("merchant1@swiftroute.app", "merchant123")
COURIER = ("courier1@swiftroute.app", "courier123")


def login(page, email, password):
    page.goto(f"{BASE}/auth/login", wait_until="load")
    page.fill("input[name=email]", email)
    page.fill("input[name=password]", password)
    page.click("input[type=submit]")
    page.wait_for_load_state("load")
    page.wait_for_timeout(600)
    if page.url.rstrip("/").endswith("/auth/login"):
        raise RuntimeError(f"Login failed for {email} (still on login page)")


def shot(page, name, full=True, wait=1500):
    page.wait_for_timeout(wait)
    page.screenshot(path=os.path.join(OUT, name), full_page=full)
    print("saved", name)


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
        page = ctx.new_page()

        # 1. Landing page
        page.goto(f"{BASE}/", wait_until="load")
        shot(page, "01-landing.png", wait=1600)

        # 2. Public tracking (populated)
        page.goto(f"{BASE}/track/", wait_until="load")
        page.fill("input[name=tracking_number]", TRACKING)
        page.press("input[name=tracking_number]", "Enter")
        page.wait_for_load_state("load")
        shot(page, "02-public-tracking.png", wait=1600)

        # 3. Admin dashboard (charts)
        login(page, *ADMIN)
        page.goto(f"{BASE}/admin/", wait_until="load")
        shot(page, "03-admin-dashboard.png", wait=2400)

        # 4. Admin live map (optimised routes on Leaflet)
        page.goto(f"{BASE}/admin/map", wait_until="load")
        shot(page, "04-admin-live-map.png", full=False, wait=4000)

        # 5. Merchant: create shipment (map pin picker)
        page.goto(f"{BASE}/auth/logout", wait_until="load")
        login(page, *MERCHANT)
        page.goto(f"{BASE}/merchant/shipments/new", wait_until="load")
        shot(page, "05-merchant-create.png", wait=3200)

        # 6. Courier dashboard
        page.goto(f"{BASE}/auth/logout", wait_until="load")
        login(page, *COURIER)
        page.goto(f"{BASE}/courier/", wait_until="load")
        shot(page, "06-courier-dashboard.png", wait=1600)

        browser.close()
        print("All screenshots saved to", OUT)


if __name__ == "__main__":
    main()
