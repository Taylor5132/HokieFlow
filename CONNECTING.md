# Final UI — remaining team connections

The final UI defaults to backend mode. No synthetic plan, class, clock or bus is shown. Missing services use ordinary empty/error states. Developer notes and roadmap panels are not rendered.

## Google sign-in

The login and signup sheets both include Continue with Google. Set `googleAuthUrl` in `ui/config.js` to a same-origin route such as `/api/auth/google/start` once implemented. Clicking it navigates to the server; the server handles Google's authorization redirect and callback. No Google token, client secret or fake authentication is implemented in the frontend.

After completing sign-in, the server should establish the same account session used by email login and redirect to `/`. The UI then calls `GET /api/auth/me` and loads `{user, data, version}`. Map a provider identity into the team's user table; Google-only users need a schema that does not require a local password. Implement callback errors/cancellation, account linking, and provider validation in the authentication backend. Until configured the button shows an email-login fallback message. Verify the final Google button branding with your chosen Google integration before release.

## Connection checklist

| Area | Partner work |
|---|---|
| Google | Provider setup, server start/callback routes, shared account session; set `googleAuthUrl`. |
| Email accounts & storage | Merge `hokieday/accounts.py` and account HTTP routes into the main backend/database. Local SQLite version already works. Keep user ownership and conflict/version handling. |
| Planner/chat | Serve `POST /api/ask` returning the itinerary/clarification contract in HANDOFF.md. |
| Campus time | Serve `GET /api/time` and `GET /api/status`. No phone-derived time substitutes for the planner clock. |
| Class schedule | Manual saved classes work. For imported classes, supply `config.homeEndpoint` with `{nextClass}` and implement the timetable source. |
| Building search | Mount `GET /api/map/buildings?q=...` using the supplied GIS module; see `integration/gis_handlers.py`. |
| Walking & ADA routes | Mount `POST /api/route`; retain the module's geometry, directions, estimate and provenance fields. UI presents Walking and ADA Walking because multimodal ranking is absent. |
| Transit/map | Mount `GET /api/map/state` with GTFS routes, observed BT vehicles, timestamps/replay flags and closures. Polling is already implemented. |
| Dining | Return D2 meal, nutrition and allergen fields in planner results. Broader menus and operating hours have no active UI integration. |
| Optional schematic | Set `gisFallbackUrl` only if the team's schematic endpoint is available. |
| Deployment | Serve UI and APIs on the same origin with HTTPS, production sessions/database, shared rate limits and account recovery/lifecycle. The included Python server is for local development. |

No API credentials belong in `ui/config.js`. No account database, provider tokens, or actual GIS snapshots are included in the ZIP.

## Final verification

JavaScript helper suite: 13 tests. Browser inspection covers the clean home screen, Google button fallback, email/signup forms and More screen. Google OAuth itself cannot be tested until the partner supplies its server routes and credentials. Existing email authentication HTTP suite previously passed seven tests; this UI pass does not alter its implementation.

## Schedule, nearby dining, and Gemini update (current contract)

This section supersedes the earlier manual-class/D2-only presentation. Schedule replaces Plan in navigation; chat results now appear on Home. Existing saved plans remain under More. Imported university timetables are not required for the personal calendar.

### Account schedule — implemented locally

`data.events` is an array (maximum 200) saved through the existing authenticated `PUT /api/account/data` with version checks. Old accounts default to an empty schedule. Each entry:

```json
{"id":"uuid","title":"CS 2506","kind":"class","location":"McBryde 100","start":"2026-09-21T14:10:00.000Z","end":"2026-09-21T15:00:00.000Z","repeat":"weekly","repeatUntil":"2026-12-11"}
```

`kind`: class/event; `repeat`: none/weekly; repeatUntil is null for nonrepeating events. UI supports add/edit/delete, month/day selection, and next-three chronological occurrences on Home, including an event currently in progress. Weekly recurrence uses the browser's local wall time and requires an end date (up to five years). The UI displays its time zone; changing device time zones changes recurrence interpretation, so cross-time-zone scheduling would require storing an IANA zone per event and a backend recurrence engine. No daily/monthly/custom weekday recurrence or single-occurrence exceptions are implemented. Editing/deleting affects the series. Each entry is account-owned; never accept caller-provided user IDs for ownership.

### Dining directory — partner endpoint required

`GET /api/dining/places` returns:

```json
{"places":[{"id":"source-building-id","name":"Dining hall name","building":"Official building name","lat":37.0,"lon":-80.0,"hours_text":"Hours from supplied source","description":"Source description","updated_at":"2026-09-19"}]}
```

Example coordinates above are schema placeholders, not real campus data. Return actual geocoded records from the curated VT files/GIS. Do not infer an open-now badge from stale files. Missing fields stay absent. The frontend requests location only on a button click, computes straight-line distances locally, sorts nearest first and retains no location across a reload/logout. Coordinates are not sent to the dining API or Gemini. Hours and description are displayed as plain text. Directions use the existing building picker/GIS route flow. Permission-denied, empty and service-error states are implemented. The partner must supply the actual directory; no fabricated campus listings ship.

### Gemini, uploaded files only — partner backend required

Reuse `POST /api/ask` with `{text}`. For conversational answers return:

```json
{"answer":"Answer supported by approved source files.","sources":[{"file_id":"approved-file-id","title":"VT dining source","updated_at":"2026-09-19","page":2}]}
```

The UI supports plain text answers and expandable file citations, and also retains the existing structured itinerary contract. Keep API keys, file ingestion and Gemini calls on the server. Use a controlled File Search store containing only the approved uploaded VT files; omit Google Search, URL Context, Maps grounding and arbitrary network tools from model calls. Replacing a source must remove/supersede its old indexed version. Keep file IDs, source dates and citations tied to the returned claims. Check for sufficient retrieved evidence and return a clear insufficient-information response when it is absent. Disabling web search alone does not guarantee the model uses only files or that files are current; server retrieval, grounding checks and source maintenance are required. Do not let uploaded document instructions override the application policy.

Official references: [Gemini File Search](https://ai.google.dev/gemini-api/docs/file-search) and [Google Search grounding](https://ai.google.dev/gemini-api/docs/google-search).

A frontend configuration flag cannot enforce model tools or freshness. No Gemini integration, file upload/admin pipeline, live source refresh or API keys are included in this frontend handoff. The team needs integration tests for these exact contracts; merely copying frontend files alongside backend files cannot guarantee every feature works.

## Home bus departures and weather (implemented)

Home now contains compact route/stop rows with up to three future departure chips per row; the large GIS map is on the Bus tab. Direction shortcuts open that tab. The calendar, dining and account contracts remain unchanged.

- `GET /api/transit/stops`: implemented in `serve.py` via `hokieday/transit.py`. Reads official Town of Blacksburg transit-stop coordinates, caches in memory for six hours. Browser selects three nearest stops within 1.5 km. Before location permission, reference point is central campus and the heading says Campus buses. Near me requests location, retains it only in browser memory, and sends only stop IDs to the server.
- `GET /api/transit/departures?stop=STOP_CODE`: implemented via the HTTPS JSON endpoint used by the official BT map (`getNextDeparturesForStop`). Preserves the provider's timezone-aware `adjustedDepartureTime` and route names, caches for 30 seconds and refreshes every minute while visible. Chips are adjusted departure estimates, not guarantees or deductions from bus positions. Past trips are omitted; unavailable data does not become invented arrivals. The legacy bt4u.org SOAP host was unreachable during verification, so this adapter uses ridebt.org's current map service. This website-backed endpoint may change; monitor its response contract.
- Weather: `ui/home-live.js` calls Open-Meteo directly for fixed campus coordinates, Fahrenheit temperature, weather code and hourly precipitation probability. Refreshes every ten minutes. The orange rain note appears only when a future hour in the next 12 hours has at least 50% probability. Conditions are weather-model estimates. No API key or user location is needed; attribution is in the header. Free Open-Meteo access is for non-commercial use; review provider terms for a commercial release.
- Production integration: mount both transit endpoints in the team's server with the same contracts. A static-only Netlify upload can show weather but needs a backend/reverse proxy for BT endpoints. Existing GIS and dining backend dependencies still apply.

Verified the current BT response and weather response over the network and in the browser. 20 JavaScript tests and 11 Python tests pass. No real provider response fixtures or account data are bundled.

References: https://ridebt.org/developers · https://ridebt.org/media/mod_bt_map/js/lib.js · https://tobmaps.blacksburg.gov/server/rest/services/transportation/Blacksburg_Transit/FeatureServer/0 · https://open-meteo.com/en/docs

### Nearby bus location fix

Near me now enables an opt-in geolocation watch, with a visible locating state and distinct denied/unavailable/timeout recovery messages. It updates the selected stops as the user moves; tapping Location on disables tracking. Departure refresh is every 30 seconds, on return to the app, and on network reconnection. An in-flight campus request cannot overwrite a newer location: requests are versioned and location changes queue a follow-up refresh. Permission messages are retained through background departure refreshes. Live data still depends on BT availability and browser/OS location permission; HTTPS or localhost is required. Coordinates stay on the device. Watchers stop at logout/page exit. Location/watch tests use synthetic coordinates and no real location is stored.

### Bus abbreviations and loop colors

Rows display BT's `routeShortName` (for example CAS, HXS, SME). The first departure chip uses maroon for Maroon Loop and orange for Orange Loop; later chips stay neutral. The adapter preserves `patternName`, prioritizes explicit directional pattern names such as CAS to Orange, and otherwise checks the last bus stop in BT's pattern geometry. Unknown destinations stay neutral. Opposite-loop departures are grouped separately, and text labels accompany the colors. These colors indicate the destination loop, not the boarding stop or route branding color.
