# SwiftRoute — AI-Assisted Logistics & Parcel Delivery Platform

> Graduation project — a full-stack, multi-role logistics system inspired by **Bosta** and **DHL**.
> Merchants create shipments, an AI optimiser plans courier routes, couriers deliver with proof
> of delivery, and customers track parcels in real time.

![Python](https://img.shields.io/badge/Python-3.11%E2%80%933.14-blue)
![Flask](https://img.shields.io/badge/Flask-3.x-black)
![Database](https://img.shields.io/badge/DB-SQLite%20%7C%20PostgreSQL-336791)
![License](https://img.shields.io/badge/License-MIT-green)

---

## ✨ Features

| Area | What it does |
|------|--------------|
| **Multi-role auth** | Admin, Courier and Merchant portals with role-based access control. |
| **Merchant portal** | Create shipments by dropping a pin on a map, set COD, track every parcel. |
| **AI route optimisation** | k-means clustering per courier + nearest-neighbour & 2-opt TSP sequencing. Optional OSMnx street-level routing. |
| **Courier portal** | Optimised stop list, live route map, one-tap *Delivered/Failed*, photo proof of delivery. |
| **Admin operations** | Manage hubs & fleet, run the optimiser, KPI dashboard with charts, full shipment table. |
| **Public tracking** | No-login tracking by number with a Bosta-style status timeline + ETA. |
| **REST API** | JSON endpoints for tracking, shipments and KPIs. |
| **Cash on delivery** | Per-parcel COD amounts, surfaced to couriers and reconciled per route. |

---

## 🏗️ Architecture

```
run.py / wsgi.py          → entry points
config.py                 → environment-driven config (SQLite default, Postgres-ready)
app/
├── __init__.py           → application factory, blueprints, CLI commands
├── extensions.py         → db, login, migrate, csrf
├── models.py             → User, Hub, Shipment, ShipmentEvent, RouteStop
├── forms.py              → WTForms (validation + CSRF)
├── utils.py              → role decorators, tracking numbers, uploads
├── routing/optimizer.py  → the AI route-optimisation engine
├── blueprints/           → main, auth, admin, courier, merchant, tracking, api
├── templates/            → Jinja2 + Bootstrap 5 + Leaflet
└── static/               → CSS + proof-of-delivery uploads
seed.py                   → realistic demo data
tests/test_app.py         → end-to-end lifecycle tests
docs/                     → design decisions, user guide, presentation
```

See [`docs/DESIGN_DECISIONS.md`](docs/DESIGN_DECISIONS.md) for the engineering trade-offs and
[`docs/USER_GUIDE.md`](docs/USER_GUIDE.md) for a walkthrough of every screen.

---

## 🚀 Quick start (zero-config, SQLite)

```powershell
# 1. Create a virtual environment & install dependencies
python -m venv .venv
.\.venv\Scripts\Activate.ps1            # Windows PowerShell
pip install -r requirements.txt

# 2. Create the database and load demo data
python seed.py

# 3. Run
python run.py
```

Open <http://127.0.0.1:5000>.

### Demo accounts (after seeding)

| Role | Email | Password |
|------|-------|----------|
| Admin | `admin@swiftroute.test` | `admin12345` |
| Courier | `courier1@swiftroute.test` | `courier123` |
| Merchant | `merchant1@swiftroute.test` | `merchant123` |

> First time? **Sign in as the merchant** and create a shipment, **switch to admin** to mark it
> *At Warehouse* and **Optimise**, then **sign in as the courier** to deliver it. Finally, track it
> publicly on the **Track** page.

---

## 🐘 Using PostgreSQL (the "real-world" target)

The app is database-agnostic. To run on PostgreSQL:

```powershell
# Start a local Postgres (Docker)
docker compose up -d

# Point the app at it (psycopg3 driver is already in requirements.txt)
# Edit .env and set:
#   DATABASE_URL=postgresql+psycopg://swiftroute:swiftroute@localhost:5432/swiftroute

python seed.py        # creates tables + demo data in Postgres
python run.py
```

Any managed Postgres (Neon, Render, Supabase, Heroku) works — just set `DATABASE_URL`.
`postgres://` and `postgresql://` URLs are automatically upgraded to the psycopg3 dialect.

### Database migrations (optional)

```powershell
flask --app run db init      # once
flask --app run db migrate -m "initial"
flask --app run db upgrade
```

---

## 🧠 The route optimiser

For each hub the engine:

1. **Clusters** at-warehouse parcels into one group per available courier (*k-means*).
2. **Sequences** each courier's stops with **nearest-neighbour + 2-opt** (a TSP heuristic).
3. **Draws** the route — straight lines by default, or real road geometry when
   `ENABLE_STREET_ROUTING=1` (uses OSMnx + a cached street graph; see optional deps in
   `requirements.txt`).
4. **Estimates** an ETA per stop from distance and average courier speed.

Heavy scientific libraries are optional — the optimiser ships pure-Python fallbacks for distance,
clustering and routing, so it never fails to run.

---

## 🔌 REST API

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/track/<tracking_number>` | public | Status + full timeline |
| GET | `/api/shipments` | session | Caller's shipments |
| GET | `/api/stats` | admin | Network KPIs |

```bash
curl http://127.0.0.1:5000/api/track/SR-7F3K9Q2A
```

---

## 🧪 Tests

```powershell
pytest -q
```

Covers registration, role protection, the full create → optimise → deliver → track lifecycle, and
the optimiser.

---

## ⚙️ CLI commands

```powershell
flask --app run init-db        # create tables
flask --app run seed           # demo data
flask --app run create-admin   # interactive admin creation
```

---

## 🔐 Security notes

- Passwords hashed with Werkzeug (PBKDF2).
- CSRF protection on all browser forms (Flask-WTF).
- Role-based access control via a `role_required` decorator.
- Open-redirect protection on login `next`.
- Server-side validation on every form (WTForms).
- Secrets and the local database are kept out of git (`.gitignore`).

---

## 📦 Tech stack

Flask · SQLAlchemy · Flask-Login · Flask-Migrate · Flask-WTF · Bootstrap 5 · Leaflet ·
Chart.js · scikit-learn/OSMnx (optional) · PostgreSQL / SQLite.

## 📄 License

MIT — see [`LICENSE`](LICENSE).
