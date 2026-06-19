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

**Step 4 — Geometry & ETA.** Routes must *follow real streets*, not cut across buildings. We chose
**OSRM** (the public Open Source Routing Machine) reached over plain HTTP: it returns road-following
geometry, needs **no API key**, and is far lighter than downloading a street graph. Responses are
cached on disk so re-optimising is instant, and a small **circuit breaker** falls back to straight
lines if OSRM is unreachable (so the system never hangs offline). An optional local **OSMnx** graph
is still supported via `ROUTING_PROVIDER=osmnx`. ETAs come from street distance ÷ average courier
speed + per-stop service time.

**The dependency dilemma.** scikit-learn, OSMnx, NumPy and NetworkX are heavy, slow to install, and
on bleeding-edge Python (3.14) may lack pre-built wheels. We refused to let the core feature depend
on them.

**What we chose.** The optimiser is **pure-Python by default** with optional acceleration:
- Haversine distance — pure Python.
- k-means — uses scikit-learn *if present*, otherwise a tiny built-in implementation.
- Street geometry — OSRM over HTTP (standard library only), straight-line fallback, OSMnx optional.

The result always runs; the heavy libraries only ever make it *nicer*, never *necessary*.

---

## 3b. Traffic visualisation and road closures

**The ask.** Show "how busy the roads are" and "if there are any closures" — like Google Maps.

**The honest constraint.** There is no free, key-less live-traffic feed for Cairo. Rather than fake
a data source, we built an explicit, defensible **traffic *model*** and label it as simulated in the
UI:
- Each road segment gets a congestion level (clear / moderate / busy / heavy) from a **deterministic,
  time-of-day function**: a per-road hash (some roads are consistently busier) combined with a
  rush-hour curve that peaks around 09:00 and 18:00. The same street therefore looks calmer at
  night and congested at rush hour, and the colours are stable and explainable.
- Levels map to Google-Maps-style colours and a travel-time multiplier; the route polyline is split
  into coloured segments on every map, with a legend.

**Road closures — making them *do* something.** Closures are a first-class, admin-managed entity
(`RoadClosure`: a centre, radius, reason, active flag). They are not just markers:
- The optimiser **re-routes the drawn geometry around** active closures by asking OSRM for a detour
  via a point offset perpendicular to the blocked leg.
- If no detour clears it (e.g. the destination's own road is closed), the leg is returned
  `blocked=True` and rendered as a **dashed red line with a warning** — an honest "we couldn't avoid
  this" rather than a silent wrong route.
- Closures are computed at **render time** for the overlay (so a newly added closure immediately
  shows as a warning) and at **optimise time** for the geometry (so re-optimising actually re-routes).

**Why this split (store geometry, compute traffic live).** Street geometry is expensive (an OSRM
call) so we **persist** it on the `RouteStop`. Traffic colour and closure flags are cheap and
*time-dependent*, so we compute them **freshly on each page load** from the stored geometry — the map
reflects "now" without re-running the optimiser.

---

## 3c. Dispatch-time planning and comparing optimisation techniques

**The problem.** The optimiser answered *who* delivers *what* in *which order*, but ignored *when*.
ETAs were static (distance ÷ a fixed speed), traffic was only ever modelled for the current moment,
and the dispatcher had no way to ask "is Friday morning faster than Tuesday at 6 pm?" or to see how
much a smarter algorithm actually saves. For a real dispatcher, choosing the right **day and time** to
send a courier — and knowing which technique to trust — is the whole job.

**What we changed.**

1. **Day-of-week-aware traffic.** The congestion model already had a rush-hour curve; we added a
   per-weekday multiplier reflecting Cairo's working week (Sunday–Thursday busy, **Friday lightest**,
   Saturday light). Congestion is now a function of *both* the hour and the day, so the same street is
   calm on a Friday morning and gridlocked on a Tuesday evening.

2. **Time-scaled ETAs.** A new `traffic_factor_at(lat, lon, when)` turns a planned moment into a
   travel-time multiplier. The optimiser walks each leg forward in simulated time (departure + minutes
   already driven) and stretches or shrinks the ETA by the traffic expected *when the courier actually
   reaches that leg* — not one global guess.

3. **A dispatch-time resolver.** `resolve_departure(day, hour, minute)` turns a weekday name/index +
   time into a concrete future `datetime` (rolling forward to the next occurrence of that weekday), or
   "right now" when no time is given.

4. **Multiple, selectable techniques.** Sequencing is now a small registry of strategies, each with a
   label and a one-line description:
   - **As received (FIFO)** — no optimisation; the baseline to beat.
   - **Nearest Neighbour** — greedy "always drive to the closest remaining stop".
   - **Nearest Neighbour + 2-opt** — greedy tour refined by edge-swaps (the previous default).
   - **2-opt + Or-opt** — adds segment relocation for the highest-quality tour.

5. **An honest comparison + recommendation.** `compare_strategies(hub, departure)` estimates every
   technique for the chosen day/time and reports, per technique, the **total fleet distance** and the
   **estimated completion time**, flagging the best as *recommended*. Leaving the choice on **Auto**
   dispatches with that recommendation.

**Two decisions worth calling out.**

- **Completion time = makespan, not the sum.** Couriers deliver *in parallel*, so the figure that
  matters to a dispatcher is "when does the **last** parcel land" — the longest single courier route,
  not the summed driving time of the whole fleet. We rank techniques (and pick the recommendation) on
  makespan, then total distance, then simplicity as tie-breakers.

- **Comparison must be cheap; only dispatch persists.** Previewing many day/time/technique
  combinations has to be instant and work offline, so `compare_strategies` is **pure estimation** —
  haversine distances scaled by the traffic model, with **no OSRM calls and no database writes**. Only
  when the admin clicks *Optimise & dispatch* do we run the real persistence path (road geometry,
  closure avoidance, `RouteStop` rows). The preview reloads the map (GET) for the selected time;
  dispatch commits it (POST).

**Backwards compatibility.** `optimize_and_persist`, `build_overlay` and `congestion_for` all gained
*optional* parameters (`departure` / `strategy` / `when`), so every existing caller and test keeps
working unchanged; "now" and the previous default technique remain the defaults.

---

## 3d. Vehicle capacity & admin shipment operations

**The problem.** The optimiser happily assigned *every* parcel in a courier's cluster to that one
courier — a bicycle could be told to deliver 30 boxes. There was also no way for an **admin** to
create a shipment on a merchant's behalf (phone orders, walk-ins), and the admin shipment table
listed thousands of parcels with no way to find a specific merchant's or zone's orders.

**What we changed.**

1. **Per-vehicle capacity caps.** A `Vehicle` registry defines how many parcels each vehicle type
   can carry on one route — **Bicycle 3, Motorcycle 5, Car 10, Van 15** — exposed as
   `courier.route_capacity`. The optimiser now does **capacity-aware assignment**: k-means still
   gives each courier a coherent zone, then a greedy pass fills each courier up to their cap, spilling
   to the next-nearest courier when one is full. Parcels beyond the *whole fleet's* capacity are left
   **unassigned** for the next dispatch round (and the admin is told), rather than silently
   overloading a vehicle.

2. **Admin-created shipments.** Admins get the same map/search/pin picker the merchant uses, plus a
   **merchant selector** (a shipment must belong to a merchant). Implemented as an `AdminShipmentForm`
   subclass so the two forms never drift apart.

3. **Find-by-merchant / zone / search.** The admin shipments table gained a **Merchant** column and a
   filter bar: free-text search (tracking number, receiver name or phone) plus dropdowns for merchant,
   zone/district and status. Filters compose and survive pagination.

**A decision worth calling out — biggest vehicles pick first.** When assigning clusters to couriers we
sort couriers by descending capacity so dense zones land on high-capacity vehicles. This minimises the
number of parcels that overflow the fleet, which is the figure a dispatcher actually cares about.

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
- **Self-service registration supports merchants and couriers**, with a role selector on the signup
  form; admin accounts are never created publicly (only via a CLI command or the seed script). A new
  courier signs up unassigned and an admin later gives them a hub. Email is unique (DB constraint +
  case-insensitive form validation), so two people can't register the same address.
- **Defence in depth.** Werkzeug password hashing, CSRF tokens on every form, a `role_required`
  decorator for authorisation, server-side WTForms validation, and open-redirect protection on the
  login `next` parameter.

---

## 6. Maps & "drop a pin" addressing

**The problem.** Egyptian addresses are landmark-based and hard to geocode reliably.

**What we chose.** Instead of fighting geocoding, merchants and admins **click a Leaflet map** to set
exact coordinates, and add a free-text landmark ("near the pharmacy"). Couriers get a one-tap link to
external turn-by-turn directions. This is both more accurate and far simpler than address parsing.

**A usability gap we closed.** Dropping a pin assumes the merchant can *find* the address on the map.
In practice many couldn't — panning around Cairo to locate a street, with no idea what to type into
raw latitude/longitude boxes, was a real friction point. We kept the map pin as the source of truth
but added three easier ways to set it, all of which fill the coordinates automatically:
- **Address search** — type an address/area/landmark and pick from a result list, geocoded with
  **OpenStreetMap Nominatim** (free, no API key — the same key-less philosophy as our OSRM choice).
- **"Use my current location"** — one click via the browser's Geolocation API.
- **Draggable pin** — click or drag to fine-tune the exact spot.

The lat/lon fields became **read-only** (set by the tools, not hand-typed), and the search degrades
gracefully: if Nominatim is unreachable it tells the user to click the map instead, so the offline
map-click path always works. No backend change was needed — the form still submits the same two
fields.

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

## 9b. Bugs and gotchas we hit (and fixed)

A short field guide to the non-obvious problems that cost us real time — recorded so they don't bite
again. Several were "works on my machine" traps where tests passed but the running system failed.

| Problem (symptom) | Root cause | Fix |
|---|---|---|
| App **hung on startup** trying to reach a remote DB | Flask-dotenv walks *up* the directory tree and loaded a global `~/.env` with a stale `DATABASE_URL` | Load only the **project-local** `.env` (see §2); configuration is now self-contained and explicit |
| Production deploy (Railway) returned **500 on login**, yet worked locally | `AUTO_INIT_DB=true` was compared against the literal string `"1"`, so it evaluated *false* → tables were never created | Parse booleans tolerantly — accept `1/true/yes/on` (any case). First-boot table creation is wrapped in try/except so parallel gunicorn workers can't crash each other |
| Login/registration **rejected demo accounts** with "Invalid email address" | WTForms' `Email()` validator rejects reserved/special-use TLDs (`.test`, `.example`, `.localhost`) per RFC 2606/6761 | Use a real TLD for seed/demo accounts (`@swiftroute.app`) |
| **Tests passed yet demo logins failed** | Tests used a valid TLD (`@test.io`) while the seed data used `.test` | Keep seed + test domains consistent; added a regression test asserting a demo email yields a `302`, not a 200 re-render |
| Leaflet map threw **"Identifier 'path' has already been declared"** | `const path` was emitted twice in one JS scope inside a Jinja `{% for %}` loop | Block-scope each loop iteration with `{ … }` so every `const` is fresh |
| Automated screenshots **lost auth / used the wrong viewport** | VS Code's built-in browser CDP doesn't persist cookies (`Storage.getCookies` missing) and `setViewportSize` doesn't stick across navigations | Capture with **standalone Playwright** (`chromium.launch()` + `new_context(viewport=…)`) |
| Admin **dashboard 500'd on PostgreSQL** (worked on local SQLite) | The 7-day chart used `func.date(created_at) == day.isoformat()`; Postgres rejects comparing a `date` to a text string (`operator does not exist: date = text`) | Compare against a portable half-open range (`created_at >= day_start AND < next_day`) instead of `func.date(...) == str` |
| Merchants **couldn't fill latitude/longitude** when creating a shipment | The only input was clicking the right spot on the map; raw lat/lon boxes meant nothing to users | Added address search (Nominatim), "use my location", and a draggable pin; the lat/lon fields are now **read-only** and filled by those tools |

---

## 10. Summary of key choices

| Decision | Chosen | Main reason |
|----------|--------|-------------|
| Web framework | Flask | Python-first, lightweight, well understood. |
| Persistence | SQLAlchemy, SQLite→Postgres | Runs anywhere, scales to real DB via one variable. |
| Postgres driver | psycopg3 | Python 3.14 wheels. |
| Optimiser | k-means + NN + 2-opt, pure-Python core | Good routes, zero hard heavy deps. |
| Dispatch planning | Day/time picker + technique comparison, ranked by makespan | Send couriers at the right time with the right algorithm; estimate before committing. |
| Fleet capacity | Per-vehicle caps (bike 3 / moto 5 / car 10 / van 15) | Never overload a vehicle; overflow waits for the next round. |
| Street routing | OSMnx (optional) | Realism without fragility. |
| Lifecycle | Status + append-only events | Auditable, customer-facing timeline. |
| Auth | Single user table + roles | Simplicity with clear permissions. |
| Addressing | Map pin + landmark | Accuracy without geocoding. |
| Front-end | Jinja + Bootstrap + Leaflet | One stack, easy to demo. |
