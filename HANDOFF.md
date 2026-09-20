# HokieFlow integration handoff


**Schedule and dining update:** Schedule now provides an account-owned calendar and Home shows the next three upcoming entries. Nearby dining uses `/api/dining/places` with on-device location sorting; Gemini plain-text answers and file citations are supported. See the newest section of CONNECTING.md.

**Final UI update:** The app now defaults to backend mode, with no demo panels or sample content. Google sign-in UI is ready for a server connection. See [CONNECTING.md](CONNECTING.md) for the current integration checklist; it supersedes older preview and three-mode UI descriptions below.

## Authoritative references

The original functionality matrix governs data honesty. The teammate's newer `reply.txt` supersedes its older GIS limitations: VT GIS backend data is reported integrated, while interactive endpoint wiring and multimodal ranking remain pending. The actual Python repository was not provided, so those backend claims have not been independently verified here.

This package implements UI and a new local authentication service. It does not claim to implement the existing campus planner, GIS routing engine, GTFS ingestion, or dining source connectors.

## Account API (implemented by this package)

| Endpoint | Request / result |
|---|---|
| `POST /api/auth/register` | `{name,email,password}` → `{user,data,version}` + session cookie |
| `POST /api/auth/login` | `{email,password}` → `{user,data,version}` + new cookie |
| `POST /api/auth/logout` | `{}` → cookie cleared and session revoked |
| `GET /api/auth/me` | `{user,data,version}` or `{user:null}` |
| `PUT /api/account/data` | `{data,version}` → updated account state |

Write calls require same-origin JSON and `X-HokieFlow-Request: 1`. No CORS permission is granted. The service derives account ownership only from the session, never a caller's user ID. All responses containing account data use `Cache-Control: no-store`. Stale versions return 409 rather than overwriting another tab's save.

`data` is exactly:

```json
{
  "savedClass": {"code":"CS 2506","title":"Comp Org","building":"McBryde Hall","room":"100","time":"10:10 AM"},
  "reduceMotion": false,
  "plans": [{"id":"client-generated-id","query":"User request","answer":{"feasible":false}}]
}
```

`savedClass` may be null. Plans retain the original response including its provenance/replay flags. Saving is explicit, not automatic chat-history collection. Campus location is never written here. Email addresses are account identifiers, not verified university identities.

Do not overwrite your existing `hokieday` package when integrating: merge the new `accounts.py` module and mount equivalent framework routes. The standalone `serve.py` is a development adapter only. Preserve HTTPS/Secure cookies and implement shared rate limiting, email verification/recovery, account lifecycle and database backups for a public deployment. Setting `HOKIEDAY_SECURE_COOKIE=1` adds Secure locally but does not itself provide TLS.

Security references: https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html and https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html

## Campus planner API (existing team integration, unverified here)

`config.mode = 'preview'` uses synthetic design fixtures. Set to `backend` only when the teammate's `/api/ask`, `/api/time`, and `/api/status` are served on the same origin. Account APIs work independently of this switch.

- `POST /api/ask`: `{text}` → direct answer object with `feasible`, `itinerary`, `clarification`, `_time`, interpretation notes and re-plan information.
- `GET /api/time`: server-derived clock. Live display advances from the server epoch; replay remains pinned.
- `GET /api/status`: fetched once. Do not fabricate data from missing fields.
- Optional `homeEndpoint` → `{nextClass, routes}`. Class is an explicit new team contract, not existing timetable coverage. Manual account class overrides it.

## GIS adapter contract (proposed — confirm against actual backend)

The endpoint names come from `reply.txt`; the response/request structures below are the frontend adapter's assumptions. Map them to your actual backend in `ui/vt-gis.js`.

`GET /api/map/buildings` → GeoJSON FeatureCollection. Each building feature:
- `id` or `properties.id`: stable building ID used in route requests.
- `properties.name`: display name.
- `geometry`: Polygon, MultiPolygon or Point in standard `[longitude, latitude]` order.
- Optional `properties.accessible_entrances`: `[{name,coordinates:[longitude,latitude]}]`.

`GET /api/map/state` → object:
- `routes`: GTFS GeoJSON FeatureCollection, properties `source: "GTFS"`, `route_color`, `name`.
- `closures`: GeoJSON FeatureCollection; set `properties.kind: "closure"`.
- `vehicles`: array of `{bus_id,route_id,lat,lon,load_pct,sched_delta_min,observed_at,is_stale,route_color}`.
- `is_replay`, `observed_at`: source provenance.

`POST /api/route` request:

```json
{"origin":{"building_id":"START_ID"},"destination":{"building_id":"END_ID"},"mode":"ada_walking"}
```

Modes: `fastest`, `least_walking`, `ada_walking`. Response:
- `geometry`: GeoJSON FeatureCollection, each leg marked `properties.source` and color; `abandoned:true` for retained Plan A lines.
- `legs`: `[{label,type,source,minutes}]`.
- `feasible`, `message` / `reason`, `mode`.

Routing/ranking is the backend's responsibility. Selecting ADA does not make an unverified route accessible. No route is drawn until geometry arrives. Class/D2 shortcuts select a matching building when available, then the user chooses an origin.

## Map behavior and fallback

- Self-contained SVG canvas; no Apple/Google/Leaflet SDK and no map token.
- Building selection via pointer or keyboard, pan with drag/arrows, zoom buttons and +/-.
- BT snapshots refresh every 60 seconds; markers jump to observed points without interpolation. Failed refresh explicitly retains last data with freshness unconfirmed.
- User location is opt-in browser permission, with accuracy display and 5 km guard. Watching stops on disable, map teardown or page exit. Current device location is displayed only; route requests currently use selected building IDs.
- `config.gisFallbackUrl` may point to the same-origin SVG generated by the team's existing `app/mapview.py`. The SVG is loaded as an image rather than inserted as executable markup. The team's actual fallback source was not supplied; no substitute geometry is invented.

## Data honesty retained

D2-only dining, UNKNOWN allergens, missing nutrition as an em dash, explicit infeasibility, clarification instead of invented deadlines, no exact BT arrival countdown, no implied weather/events/timetable feed. Existing synthetic walking fixtures remain straight-line estimates; actual GIS route geometry is separately source-labelled.

## Verification boundary

Local account HTTP tests and UI helper tests pass. Login/signup form presentation, fixed chat and GIS unavailable states are browser-inspected. Actual VT GIS endpoints, ADA routing, map shapes and live BT updates require the team's running backend. Python model compatibility is tested with synthetic data. No claim is made that this package passes the team's 249-test suite or that its local server is production-ready.

## Update: supplied Python GIS module

The pasted module now supplies the concrete model contract. `buildingFeatures()` accepts a raw list of `Building.as_dict()` objects or `{buildings:[...]}`, including BuildingDetail wrappers. `normalizedRoute()` accepts `Route.as_dict()`, `NoRoute.as_dict()` and `Unavailable.as_dict()`. Model geometry is `[latitude,longitude]`; the renderer converts it in memory. Existing GeoJSON remains accepted.

`integration/gis_handlers.py` contains framework-neutral handlers that accept your GIS module as an argument. Mount them at the endpoints above, return JSON with `Cache-Control: no-store`, and map ValueError to HTTP 400. For building searches use the UI query parameter `q`. Your existing cache/config modules and HTTP framework were not supplied, so this package cannot run that module standalone. The local development server deliberately still returns unavailable for GIS endpoints.

Fastest and Least Walking both use Walking; ADA Walking uses `ada_walking`. Estimates retain `estimated_minutes_label`. The UI distinguishes unavailable from no_route, displays turn directions and attribution/disclaimer, and warns when closures were not considered. GIS results stay in memory and are excluded from account saving and ZIP fixtures. No export capability is fabricated or permission-gated helper invoked. The module's permission comments are project constraints, not independently verified legal conclusions.
