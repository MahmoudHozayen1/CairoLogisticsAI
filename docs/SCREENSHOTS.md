# 📸 SwiftRoute — Screenshot Gallery

A visual tour of **every screen, role and use case** in the platform. Each image links to the
full-resolution file. Regenerate the whole set anytime by running the app and then
[`scripts/capture_screenshots.py`](../scripts/capture_screenshots.py) (headless Playwright).

> Demo logins used for these captures:
> `admin@swiftroute.app` · `courier1@swiftroute.app` · `merchant1@swiftroute.app`

---

## 1. Public & authentication

| Landing page | Public parcel tracking |
| --- | --- |
| [![Landing](screenshots/01-landing.png)](screenshots/01-landing.png) | [![Public tracking](screenshots/02-public-tracking.png)](screenshots/02-public-tracking.png) |
| Marketing landing with role entry points. | No-login tracking by number — status timeline, ETA and a delivery-confirmation (GIS) badge. |

| Sign in | Register |
| --- | --- |
| [![Login](screenshots/10-login.png)](screenshots/10-login.png) | [![Register](screenshots/11-register.png)](screenshots/11-register.png) |
| Role-aware login with open-redirect-safe `next` handling. | Merchant/courier self-registration with server-side validation. |

---

## 2. Admin — operations

| Operations dashboard | Live map & AI route optimisation |
| --- | --- |
| [![Admin dashboard](screenshots/03-admin-dashboard.png)](screenshots/03-admin-dashboard.png) | [![Live map](screenshots/04-admin-live-map.png)](screenshots/04-admin-live-map.png) |
| KPI cards + charts across the whole network. | Plan by day/time/strategy; routes drawn on real streets with simulated traffic. |

| Traffic colours & road closures | Shipments table |
| --- | --- |
| [![Traffic and closures](screenshots/07-admin-closures.png)](screenshots/07-admin-closures.png) | [![Admin shipments](screenshots/12-admin-shipments.png)](screenshots/12-admin-shipments.png) |
| Admins mark closures; the optimiser re-routes around them. | Filterable table of every parcel in the network. |

| Shipment detail (+ AI predictions) | Shipment chain-of-custody audit |
| --- | --- |
| [![Shipment detail](screenshots/13-admin-shipment-detail.png)](screenshots/13-admin-shipment-detail.png) | [![Shipment audit](screenshots/14-admin-shipment-audit.png)](screenshots/14-admin-shipment-audit.png) |
| Status, timeline, ETA predictions with reasoning, GIS map and handling-note analysis. | Per-parcel hash-chained handoff ledger with GIS delivery confirmation. |

| Couriers & fleet | Edit courier |
| --- | --- |
| [![Couriers](screenshots/15-admin-couriers.png)](screenshots/15-admin-couriers.png) | [![Courier edit](screenshots/08-admin-courier-edit.png)](screenshots/08-admin-courier-edit.png) |
| Manage the fleet and hub assignments. | Create/edit courier profiles. |

---

## 3. Admin — AI / ML suite

| AI overview (predictors + forecast) | Feedback loop & drift monitor |
| --- | --- |
| [![AI overview](screenshots/16-admin-ai-overview.png)](screenshots/16-admin-ai-overview.png) | [![AI feedback](screenshots/17-admin-ai-feedback.png)](screenshots/17-admin-ai-feedback.png) |
| Drop-off/pick-up/late scorecards with cross-validated scores and naive-baseline comparisons, demand forecast with a 95% band, and feature importances. | Back-tested MAE/bias/R², weekly drift, residuals and late-risk calibration. |

| Learning-to-route (neural router) | Courier behaviour — fleet |
| --- | --- |
| [![Neural router](screenshots/18-admin-ai-router.png)](screenshots/18-admin-ai-router.png) | [![Behaviour fleet](screenshots/19-admin-ai-behavior-fleet.png)](screenshots/19-admin-ai-behavior-fleet.png) |
| A pure-numpy pointer policy vs. nearest-neighbour, with per-pick reasoning and a learning curve. | Persona-mix across the fleet, ranked roster and productivity scores. |

| Courier behaviour — detail | Operations assistant (chatbot) |
| --- | --- |
| [![Behaviour detail](screenshots/20-admin-ai-behavior-detail.png)](screenshots/20-admin-ai-behavior-detail.png) | [![Assistant](screenshots/21-admin-assistant.png)](screenshots/21-admin-assistant.png) |
| State-coded GPS map (driving/delivery/break/idle), time budget and z-score reasoning. | Natural-language Q&A grounded in live data — every answer lists its sources. |

| Network audit integrity | |
| --- | --- |
| [![Audit overview](screenshots/22-admin-audit.png)](screenshots/22-admin-audit.png) | |
| Chain-of-custody integrity across every shipment. | |

---

## 4. Merchant portal

| Dashboard | My shipments |
| --- | --- |
| [![Merchant dashboard](screenshots/23-merchant-dashboard.png)](screenshots/23-merchant-dashboard.png) | [![Merchant shipments](screenshots/24-merchant-shipments.png)](screenshots/24-merchant-shipments.png) |
| The merchant's own KPIs and recent parcels. | Every parcel the merchant created, with status. |

| Create shipment (pin on map) | Shipment detail |
| --- | --- |
| [![Create shipment](screenshots/05-merchant-create.png)](screenshots/05-merchant-create.png) | [![Merchant shipment detail](screenshots/25-merchant-shipment-detail.png)](screenshots/25-merchant-shipment-detail.png) |
| Drop a pin, set COD and add handling notes. | Timeline and status for a single parcel. |

---

## 5. Courier portal

| My deliveries | Street route with live traffic |
| --- | --- |
| [![Courier deliveries](screenshots/06-courier-dashboard.png)](screenshots/06-courier-dashboard.png) | [![Courier route traffic](screenshots/09-courier-route-traffic.png)](screenshots/09-courier-route-traffic.png) |
| Optimised stop list with COD totals. | Real-road route coloured by congestion. |

| Shipment detail (proof of delivery) | |
| --- | --- |
| [![Courier shipment detail](screenshots/26-courier-shipment-detail.png)](screenshots/26-courier-shipment-detail.png) | |
| One-tap Delivered/Failed, GPS capture and handling-note guidance. | |

---

## 6. Quality — automated tests

| Full test suite |
| --- |
| [![Test results](screenshots/27-test-results.png)](screenshots/27-test-results.png) |
| The complete `pytest` run — **74 tests, all passing** — grouped by module. |
