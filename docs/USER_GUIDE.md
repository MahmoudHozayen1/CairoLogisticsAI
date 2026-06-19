# SwiftRoute — User Guide

A walkthrough of how to use the system, screen by screen, for each kind of user.

> **Tip:** run `python seed.py` first so you have demo data and the accounts below.

| Role | Email | Password |
|------|-------|----------|
| Admin | `admin@swiftroute.app` | `admin12345` |
| Courier | `courier1@swiftroute.app` | `courier123` |
| Merchant | `merchant1@swiftroute.app` | `merchant123` |

---

## 0. The 60-second demo

1. **Merchant** signs in → *New Shipment* → click the map, fill details → **Create**.
2. **Admin** signs in → *Shipments* → open it → set status **At Warehouse**.
3. **Admin** → *Live Map* → **Optimise all hubs**. The parcel is assigned to a courier and a route
   is drawn.
4. **Courier** signs in → *My Deliveries* → open the stop → **Mark delivered** (optionally attach a
   photo).
5. **Anyone** → *Track* → paste the tracking number → see the full timeline.

---

## 1. Public visitor

### Landing page (`/`)
Overview of the platform with entry points to sign up, sign in, or track a parcel.

### Track a parcel (`/track`)
Enter a tracking number (e.g. `SR-7F3K9Q2A`). You'll see:
- current status and an **ETA** (if the parcel is out for delivery),
- recipient and sender summary,
- the assigned courier,
- a **timeline** of every milestone.

No account is needed — this is what you'd share with end customers.

---

## 2. Merchant (the business shipping parcels)

Sign up at `/auth/register` (choose the **Merchant** role) or use the demo merchant. Each email can
only be registered once.

### Dashboard (`/merchant`)
KPI cards (total / delivered / in-transit / pending) and your most recent shipments.

### Create a shipment (`/merchant/shipments/new`)
1. Fill the **receiver** details (name, phone, district, landmark, optional street address).
2. Enter **parcel** info (description, weight, **cash-on-delivery** amount).
3. **Click the map** to drop a pin on the delivery location — this sets the coordinates.
4. Choose an origin hub or leave **Auto-assign nearest hub**.
5. **Create** → you get a unique tracking number immediately.

### My shipments (`/merchant/shipments`)
A paginated table of everything you've sent. Click any row for the detail page, where you can watch
the timeline and **cancel** a parcel while it's still pending.

---

## 3. Courier (the driver)

Sign in with a courier account — either self-register at `/auth/register` (choose the **Courier**
role and pick a vehicle; an admin assigns your hub afterwards), or use one created by an admin / seed.

### My deliveries (`/courier`)
- KPI cards: active stops, lifetime deliveries, **COD to collect**.
- Your stops **in optimised order**, each showing the receiver, phone, area and COD.
- Toggle **availability** (off duty removes you from the optimiser).

### My route (`/courier/route`)
A map of your hub and all your stops with the drawn street route and ETAs. The route is **coloured
by live traffic** and shows any **road closure** on your way as a red zone with a dashed-red
detour, plus a warning banner.

### Delivering (`/courier/shipment/<id>`)
- Tap **call** to phone the recipient or **Open directions** for turn-by-turn navigation.
- **Mark delivered**: add a note and optionally upload a **proof-of-delivery photo**.
- **Failed attempt**: record a reason (e.g. "no answer"); the attempt counter increments.

---

## 4. Admin (operations team)

Sign in with the admin account.

### Dashboard (`/admin`)
- KPI cards including **success rate**.
- A **status doughnut** chart and a **7-day** new-shipments bar chart.
- Network counts (hubs, couriers, merchants, failed) and recent shipments.

### Live map & optimisation (`/admin/map`)
- See every hub and active parcel on one map.
- **Optimise all hubs** clusters at-warehouse parcels per courier and draws each route along real
  streets, with numbered stops.
- Route lines are **coloured by traffic** (green = clear → red = heavy). A **legend** explains the
  colours. Active **road closures** appear as red zones, and any route leg passing through one is
  drawn as a dashed red line with a warning banner.

### Shipments (`/admin/shipments`)
Filter by status, page through all parcels, and open any one to:
- **update status** (with a note that's written to the timeline),
- **assign a courier** manually,
- view the map, proof-of-delivery photo and full history.

### Hubs (`/admin/hubs`)
Add warehouses by clicking the map; edit or delete existing ones. A hub with parcels or couriers
can't be deleted (data-integrity guard).

### Couriers (`/admin/couriers`)
Create couriers (name, email, hub, vehicle, password), **edit** any courier inline (click **Edit** to
change name, email, phone, hub, vehicle, or reset the password), and **activate/deactivate** them.

### Traffic & road closures (`/admin/closures`)
- Add a **closure** by clicking the map to drop a pin and setting a radius and reason.
- The optimiser routes deliveries **around active closures** the next time you press *Optimise*.
- **Lift** a closure to re-open the road, or **Delete** it. Closures show on every map in red.

---

## 5. REST API (for developers)

| Endpoint | Auth | Returns |
|----------|------|---------|
| `GET /api/track/<tracking_number>` | public | status + timeline JSON |
| `GET /api/shipments` | signed-in | your shipments |
| `GET /api/stats` | admin | network KPIs |

Example:
```bash
curl http://127.0.0.1:5000/api/track/SR-7F3K9Q2A
```

---

## 6. Troubleshooting

| Symptom | Fix |
|---------|-----|
| `No module named psycopg` when using Postgres | `pip install "psycopg[binary]"` (already in venv). |
| App seems to hang on first DB hit | A global `~/.env` may set `DATABASE_URL`; the project `.env` shadows it — ensure it exists. |
| Optimiser drew straight lines, not roads | That's the default. Set `ENABLE_STREET_ROUTING=1` and install the optional deps. |
| Want a fresh database | Delete `instance/swiftroute.db` and re-run `python seed.py`. |
| Maps don't load | They use public CDNs (Leaflet/OpenStreetMap); check your internet connection. |
