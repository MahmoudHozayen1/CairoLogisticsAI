# Design Decisions & Engineering Trade-offs

This document records the hard problems we faced building **SwiftRoute**, the options we
considered, what we chose, and *why*. It is meant to be read alongside the code and the
presentation.

---

## 1. From prototype to product

**The problem.** The starting point was a single-file Flask prototype (`shipment_tracker/`) that
held all data in Python lists in memory, had no users or authentication, and recomputed routes on
every page load. It demonstrated the idea but lost all data on restart and could never run "for
real".

**What we changed and why.**

| Prototype | Production system | Reason |
|-----------|-------------------|--------|
| In-memory lists | SQLAlchemy models + a real database | Data must survive restarts and support many users. |
| One `app.py` | Application factory + blueprints | Separation of concerns, testability, scalability. |
| No users | Role-based auth (admin/courier/merchant) | A real logistics network has distinct actors. |
| Status as a string set on the fly | A modelled lifecycle + immutable event log | Auditability and an accurate customer-facing timeline. |
| Routes recomputed per request | Optimiser persists assignments & geometry | Performance and a stable source of truth. |

---

## 2. Database: SQLite vs PostgreSQL

**The tension.** We wanted a "real-world" database (PostgreSQL) *and* a system that runs flawlessly
on any examiner's machine with zero setup.

**What we did.** We built everything on **SQLAlchemy**, which is database-agnostic, and made the
database a single environment variable (`DATABASE_URL`):

- **Default → SQLite**: a file-based DB that needs no server. Clone, run, done.
- **Production → PostgreSQL**: set `DATABASE_URL` and the same code runs on Postgres. We ship a
  `docker-compose.yml` for a one-command Postgres and pin the **psycopg3** driver (chosen over
  psycopg2 for first-class Python 3.14 wheel support). `postgres://` / `postgresql://` URLs are
  auto-normalised to the psycopg3 dialect.

**A subtle bug we hit (and fixed).** A global `~/.env` on the development machine defined a
`DATABASE_URL` pointing at a cloud Postgres. Flask's dotenv auto-loader walks *up* the directory
tree, so the app silently tried to connect to that remote database and hung when it was asleep. We
made the project **self-contained** by loading only a *project-local* `.env`, which shadows any
global one. Lesson: configuration precedence must be explicit, not accidental.

---

## 3. The route-optimisation engine

This is the "AI" heart of the system and the hardest part. Vehicle routing is NP-hard, so we
decomposed it into tractable sub-problems.

**Step 1 — Assignment.** Each parcel belongs to the hub it physically sits in.

**Step 2 — Clustering (which courier?).** We split a hub's parcels into one balanced group per
available courier using **k-means** on geographic coordinates. Couriers get geographically coherent
zones instead of crisscrossing the city.

**Step 3 — Sequencing (what order?).** Ordering stops to minimise distance is the **Travelling
Salesman Problem**. Exact solutions are infeasible beyond a handful of stops, so we use a
**nearest-neighbour heuristic** for a fast first tour, then refine it with **2-opt** local search
(repeatedly un-crossing the route). This gives near-optimal routes in milliseconds.

**Step 4 — Geometry & ETA.** By default we draw straight lines between stops (fast, no
dependencies). When `ENABLE_STREET_ROUTING=1`, we download a real street graph with **OSMnx** and
compute shortest driving paths with **Dijkstra on travel-time** (NetworkX), so routes follow actual
roads. ETAs come from distance ÷ average courier speed + per-stop service time.

**The dependency dilemma.** scikit-learn, OSMnx, NumPy and NetworkX are heavy, slow to install, and
on bleeding-edge Python (3.14) may lack pre-built wheels. We refused to let the core feature depend
on them.

**What we chose.** The optimiser is **pure-Python by default** with optional acceleration:
- Haversine distance — pure Python.
- k-means — uses scikit-learn *if present*, otherwise a tiny built-in implementation.
- Street routing — uses OSMnx *if enabled and installed*, otherwise straight lines.

The result always runs; the heavy libraries only ever make it *nicer*, never *necessary*.

---

## 4. Modelling the shipment lifecycle

**The problem.** A parcel is not just a row with a "status" field — customers expect a Bosta/DHL
style timeline of everything that happened.

**What we chose.** Two tables:
- `Shipment` holds the *current* state.
- `ShipmentEvent` is an **append-only audit log**; every status change writes a new event.

This gives a truthful history, supports the public timeline, and means we never lose information by
overwriting a status. Statuses (`pending → at_warehouse → out_for_delivery → delivered`, plus
`failed`, `returned`, `cancelled`) are centralised so labels and colours stay consistent everywhere.

---

## 5. Authentication, roles and security

**Decisions.**
- **One `User` table** with a `role` column rather than three tables — simpler relationships, one
  login flow. Role-specific fields (hub, vehicle, business name) live on the same row.
- **Self-service registration creates merchants only.** Couriers are created by admins; the first
  admin comes from a CLI command or the seed script. This mirrors how real platforms onboard.
- **Defence in depth.** Werkzeug password hashing, CSRF tokens on every form, a `role_required`
  decorator for authorisation, server-side WTForms validation, and open-redirect protection on the
  login `next` parameter.

---

## 6. Maps & "drop a pin" addressing

**The problem.** Egyptian addresses are landmark-based and hard to geocode reliably.

**What we chose.** Instead of fighting geocoding, merchants and admins **click a Leaflet map** to set
exact coordinates, and add a free-text landmark ("near the pharmacy"). Couriers get a one-tap link to
external turn-by-turn directions. This is both more accurate and far simpler than address parsing.

---

## 7. Front-end approach

**Decision.** Server-rendered **Jinja2 + Bootstrap 5**, with **Leaflet** for maps and **Chart.js**
for the dashboard, rather than a separate React SPA.

**Why.** It keeps the whole project in one language/stack (Python-first, as required), removes a
build pipeline, is easy to grade and demo, and is perfectly adequate for the interaction model.
A JSON API still exists for anyone who wants to build a richer client later.

---

## 8. Testing strategy

**Decision.** A focused **end-to-end** test suite using Flask's test client over an in-memory SQLite
database, exercising the real lifecycle: register → create shipment → optimise → deliver → track,
plus role-protection checks. We favoured a few high-value integration tests over many brittle unit
tests, because the value of this system is in the *flow* working together.

---

## 9. What we deliberately left out (scope control)

To keep the project focused and finishable, we consciously deferred:
- Real SMS/email notifications (the event log is the notification substrate).
- Payment-gateway integration for COD settlement (amounts are tracked, not charged).
- A native mobile app (the courier UI is mobile-friendly web).
- Live GPS streaming of couriers (we show planned routes and ETAs).

Each is a natural next step rather than a gap in the core system.

---

## 10. Summary of key choices

| Decision | Chosen | Main reason |
|----------|--------|-------------|
| Web framework | Flask | Python-first, lightweight, well understood. |
| Persistence | SQLAlchemy, SQLite→Postgres | Runs anywhere, scales to real DB via one variable. |
| Postgres driver | psycopg3 | Python 3.14 wheels. |
| Optimiser | k-means + NN + 2-opt, pure-Python core | Good routes, zero hard heavy deps. |
| Street routing | OSMnx (optional) | Realism without fragility. |
| Lifecycle | Status + append-only events | Auditable, customer-facing timeline. |
| Auth | Single user table + roles | Simplicity with clear permissions. |
| Addressing | Map pin + landmark | Accuracy without geocoding. |
| Front-end | Jinja + Bootstrap + Leaflet | One stack, easy to demo. |
