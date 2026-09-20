# Current campus navigation: Apple Maps

Run `python3 serve.py --port 3001` with Python 3.10+ and open http://127.0.0.1:3001.
This is a local development server. The production backend should serve the UI and its same-origin account/dining/transit APIs.

## Directions

`ui/buildings.js` contains only the UI owner's requested list. Grouped residence halls are expanded into separate wings, giving 69 building choices. The From menu also includes My location. Names are sent with “Virginia Tech, Blacksburg, VA” to Apple Maps. Apple performs destination lookup; this is not a verified-entrance coordinate catalog.

`ui/directions.js` constructs standard HTTPS Apple Maps links. Walk uses `dirflg=w`; Bus requests public transit with `dirflg=r`. Available transit routes and turn-by-turn directions are determined by Apple Maps. This app does not promise that Apple has transit coverage for every trip. No Apple developer key is required for these links.

With My location selected, the link omits `saddr` so Apple Maps uses its current-location flow. Find me optionally requests browser geolocation and places that coordinate in `saddr` only when the user opens the link. If permission is denied or location times out, current location can still be requested inside Apple Maps, or the user can select a building. Browser location requires HTTPS or localhost. Coordinates are held only in memory; they are not saved to accounts or sent to the HokieFlow server.

Dining and calendar directions can open Apple Maps directly for their named location, without adding destinations to the dropdown. The embedded map, GIS catalog search, GIS routing adapters, and `/api/map/*` and `/api/route` local endpoints have been removed. The group's independent GIS backend modules are not used or modified by this UI.

Apple specification: https://developer.apple.com/library/archive/featuredarticles/iPhoneURLScheme_Reference/MapLinks/MapLinks.html

## Dining and buses

`GET /api/dining/places` is backed by `hokieday/dining_places.py`. It lists ten campus dining centers with approximate location references from official VT building pages; each row includes `source_url`. Hitt Hall uses the address pin linked from its department's Directions button. These reference coordinates are maintained data, not real-time location feeds or confirmed entrances. There are no GIS calls. Directory sources were checked on September 19, 2026.

The browser computes straight-line distances from an enabled device location (or explicitly labelled central-campus reference). Home and the dining directory show feet. Distances are not walking-route lengths. Venue menus, opening hours, and open/closed status still require the group's dining service.

The BT stop and live-departure APIs are unchanged. They power Home bus chips independently of Apple Maps directions:

- `GET /api/transit/stops`
- `GET /api/transit/departures?stop=…`

## Team integrations still needed

- Production Google OAuth start/callback endpoints and `ui/config.js`'s `googleAuthUrl`.
- Gemini `/api/ask`, using the team's uploaded-source retrieval policy.
- Production account/session and schedule persistence using the existing UI account contract.
- Dining menus/hours/nutrition if those features are to be supplied.

The local account API, BT departures, dining location distances, and Apple Maps handoff work independently. No map API integration is needed for the directions link.

## Validation

Run `node --test tests/*.test.js` and `python3 -m unittest discover -s tests -p 'test_*.py'`.
The directions tests cover the exact curated-list count, current-location omission, building origins, bus/walk modes, bad inputs, encoded place names, and feet conversion.

## Settings

Light/dark mode is stored in browser localStorage. Notifications and reminders have been removed. The user icon opens the login view. Weather attribution is under About → Data credits.

## Integrated backend fixes

Run `python -m app.server --port 3001` for the combined app. Its dining endpoint uses the nine-place directory, and its transit endpoints use the live BT adapter with the UI's departure contract. BT outages return an unavailable state; no scheduled times are presented as live.

Install `requirements.txt` for Supabase authentication. The client uses HTTP/1.1 to avoid HTTP/2 stream resets, accepts current SDK response models, and validates the request's bearer token. Email-confirmation signups prompt the user to check their inbox instead of pretending they are logged in. Auth transport errors are not automatically retried, since a signup may already have reached the provider.

Verification: 23 frontend tests and targeted backend regressions; real BT stop/departure reads and browser rendering. Signup is verified with mocked provider responses, not a real production account. Production verification still needs the team's configured Supabase project and email confirmation flow.
