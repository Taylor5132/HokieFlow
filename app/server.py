#!/usr/bin/env python3
"""HokieDay demo server — stdlib only, no dependencies, works offline.

WHY STDLIB, NOT STREAMLIT
Streamlit/pandas are not installed on this machine, and more importantly the
expo demo must survive unknown Wi-Fi and a possible Databricks quota shutdown.
`http.server` needs nothing installed and reads the frozen fixtures, so the demo
depends on nothing but Python.

Run:
    DEMO_MODE=cache python3 app/server.py            # offline, deterministic
    DEMO_MODE=cache python3 app/server.py --port 8080

Endpoints:
    GET  /                  the UI
    GET  /api/status        clock, mode, data freshness, provenance
    GET  /api/scenarios     the preset demo buttons
    POST /api/ask           {"text": "..."} -> plan_day(...) result
    GET  /api/raw?n=1       the raw JSON for scenario n (debugging / slides)
"""
from __future__ import annotations

import argparse
import errno
import json
import re
import socket
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from hokieday import cache, config, tools  # noqa: E402

# Absolute, not relative: this file is run as a SCRIPT (`python3 app/server.py`),
# where `from . import x` raises "attempted relative import with no known parent
# package". REPO is on sys.path just above, so `app` resolves either way -- as a
# namespace package when run as a script, and as app.server when tests import it.
from app import mapview  # noqa: E402

TZ = ZoneInfo(config.CAMPUS_TZ)

# Preset scenarios. Each is a deterministic call into the SAME plan_day the agent
# uses -- the demo is not a re-enactment.
SCENARIOS: list[dict] = [
    {
        "id": "eat",
        "label": "Can I eat and still make my 1:25?",
        "text": "I've got from 11:22 to 13:00, I'm hungry, and I need to get from Burruss to McBryde",
        "student_ref": "demo-student-1", "start": "11:22", "end": "13:00", "prefs": {},
    },
    {
        "id": "bus_replan",
        "label": "Same trip, but I want the bus",
        "text": "Same trip but I'd rather take the bus than walk",
        "student_ref": "demo-student-1", "start": "11:22", "end": "13:00",
        "prefs": {"prefer": "bus"},
    },
    {
        "id": "vegan",
        "label": "Vegan, and no sesame",
        "text": "I'm vegan and I can't have sesame. Same window.",
        "student_ref": "demo-student-2", "start": "11:22", "end": "13:00", "prefs": {},
    },
    {
        "id": "tight",
        "label": "Tighter window (11:22 to 12:05)",
        "text": "Only have until 12:05",
        "student_ref": "demo-student-1", "start": "11:22", "end": "12:05", "prefs": {},
    },
]

TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")

# Served inline so the app has no asset files to lose.
MANIFEST = {
    "name": "HokieDay",
    "short_name": "HokieDay",
    "description": "Campus-life agent: one question across dining, transit and hours.",
    "start_url": "/",
    "display": "standalone",
    "orientation": "portrait",
    "background_color": "#0f1115",
    "theme_color": "#0f1115",
    "icons": [{"src": "/icon.svg", "sizes": "any",
               "type": "image/svg+xml", "purpose": "any maskable"}],
}

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <rect width="512" height="512" rx="112" fill="#861f41"/>
  <text x="256" y="330" font-family="-apple-system,Helvetica,Arial,sans-serif"
        font-size="210" font-weight="700" text-anchor="middle" fill="#ffffff">HD</text>
  <circle cx="388" cy="124" r="36" fill="#e87722"/>
</svg>
"""


def lan_ips() -> list[str]:
    """Best-effort LAN addresses, so a phone on the same network can connect.

    The UDP connect sends nothing; it just asks the routing table which local
    address would be used to reach the outside world.
    """
    ips: set[str] = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ips.add(s.getsockname()[0])
        finally:
            s.close()
    except Exception:                                      # noqa: BLE001
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except Exception:                                      # noqa: BLE001
        pass
    return sorted(ips)


def parse_free_text(text: str) -> dict:
    """A deliberately SMALL rule-based parser.

    It is NOT presented as the language model. With a Databricks workspace the
    text->parameters step is the agent's job (Mosaic AI calls these same tools);
    this exists so the behaviour is demonstrable with no network at all. The UI
    labels it as such rather than implying an LLM is running.
    """
    t = (text or "").lower()
    call = dict(SCENARIOS[0])
    call["prefs"] = {}
    if any(w in t for w in ("bus", "transit", "ride ")):
        call["prefs"]["prefer"] = "bus"
    if "vegan" in t:
        call["student_ref"] = "demo-student-2"
    elif "vegetarian" in t:
        call["student_ref"] = "demo-student-1"
    times = TIME_RE.findall(text or "")
    if len(times) >= 2:
        call["start"] = f"{int(times[0][0]):02d}:{times[0][1]}"
        call["end"] = f"{int(times[1][0]):02d}:{times[1][1]}"
    elif len(times) == 1:
        call["end"] = f"{int(times[0][0]):02d}:{times[0][1]}"
    return call


def _static_or_none(payload: dict, info: dict) -> tuple[str | None, dict]:
    """Fall back to a selected campus place (or the planner default).

    Used whenever a device position is absent or rejected, so the caller always
    gets a usable origin and the reason travels with it.
    """
    sel = payload.get("from_place")
    if sel and str(sel) not in ("auto", "default"):
        row = config.PLACES.get(str(sel))
        if row and not row.get("dynamic"):
            if info.get("note"):
                info["note"] += f" \u2014 using {sel} instead"
            info["source"] = "selected"
            info["label"] = str(sel)
            return str(sel), info
    return None, info


def resolve_origin(payload: dict) -> tuple[str | None, dict]:
    """Turn an optional device position into a place key.

    NEVER raises and never silently accepts a bad origin: a wrong origin yields a
    confidently wrong plan, so every rejection carries a stated reason.

    The rejection that matters in practice: a LAPTOP's Wi-Fi geolocation often
    resolves to the ISP, hundreds of km from campus. Accepting that would produce
    a plan with a multi-day walk, so anything beyond MAX_ORIGIN_KM falls back.
    """
    info: dict = {"source": "default", "label": None, "accuracy_m": None,
                  "note": None}
    if payload.get("lat") is None or payload.get("lon") is None:
        return _static_or_none(payload, info)
    try:
        lat, lon = float(payload["lat"]), float(payload["lon"])
    except (TypeError, ValueError):
        info["note"] = "device position was not numeric; using the default origin"
        return _static_or_none(payload, info)
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        info["note"] = (f"device position {lat}, {lon} is out of range; "
                        f"using the default origin")
        return _static_or_none(payload, info)

    km = tools._haversine_m((lat, lon), config.CAMPUS_REFERENCE) / 1000.0
    if km > config.MAX_ORIGIN_KM:
        info["note"] = (f"your device reports a position {km:,.0f} km from campus — "
                        f"that is Wi-Fi/network geolocation rather than GPS, so the "
                        f"plan uses the default origin instead")
        info["rejected_km_from_campus"] = round(km, 1)
        return _static_or_none(payload, info)

    try:
        acc = float(payload["accuracy"]) if payload.get("accuracy") is not None else None
    except (TypeError, ValueError):
        acc = None

    key = config.register_dynamic_place(lat, lon, "your location", acc)
    info.update({"source": "device", "label": "your location",
                 "lat": round(lat, 6), "lon": round(lon, 6),
                 "accuracy_m": acc, "km_from_campus": round(km, 3)})
    if acc is not None and acc > 100:
        info["note"] = (f"device accuracy is only ±{acc:,.0f} m, so walk times "
                        f"are approximate")
    return key, info


def deep_links(result: dict) -> list[dict]:
    """Keyless handoff URLs — "we plan it, your maps app navigates it".

    Verified: Google states "You don't need a Google API key to use Maps URLs",
    and Apple map links need no developer account either. Turn-by-turn is the
    part we deliberately do NOT build, so we hand off to the app that does.
    `dirflg`: d=car, w=foot, r=public transit. `travelmode`: walking|transit.
    """
    legs = ((result.get("itinerary") or {}).get("legs")) or []
    origin = next((l.get("from_coords") for l in legs if l.get("from_coords")), None)
    dest = next((l.get("to_coords") for l in reversed(legs) if l.get("to_coords")), None)
    if not (origin and dest):
        return []
    # Commas must be percent-encoded in Maps URLs.
    o = f"{origin[0]:.6f}%2C{origin[1]:.6f}"
    d = f"{dest[0]:.6f}%2C{dest[1]:.6f}"
    return [
        {"label": "Walk it", "app": "Apple Maps",
         "url": f"https://maps.apple.com/?saddr={o}&daddr={d}&dirflg=w"},
        {"label": "Transit", "app": "Apple Maps",
         "url": f"https://maps.apple.com/?saddr={o}&daddr={d}&dirflg=r"},
        {"label": "Walk it", "app": "Google Maps",
         "url": ("https://www.google.com/maps/dir/?api=1"
                 f"&origin={o}&destination={d}&travelmode=walking")},
        {"label": "Transit", "app": "Google Maps",
         "url": ("https://www.google.com/maps/dir/?api=1"
                 f"&origin={o}&destination={d}&travelmode=transit")},
    ]


def run_plan(call: dict, origin: dict | None = None) -> dict:
    result = tools.plan_day(call["student_ref"], call["start"], call["end"],
                            call.get("prefs") or {})
    result["_request"] = {"student_ref": call["student_ref"],
                          "start": call["start"], "end": call["end"],
                          "prefs": call.get("prefs") or {}}
    result["_origin"] = origin or {"source": "default", "label": None,
                                    "accuracy_m": None, "note": None}
    # The map is drawn from OUR data (GTFS shapes) and inlined, so it renders with
    # networking off. Deep links are the keyless handoff for real navigation.
    try:
        plan_a = next((a.get("itinerary") for a in (result.get("alternatives") or [])
                       if a.get("type") == "previous_itinerary_a"), None)
        result["_map_svg"] = mapview.build_map_svg(result.get("itinerary"),
                                                   overlay=plan_a)
    except Exception as exc:                               # noqa: BLE001
        result["_map_svg"] = ""
        result["_map_error"] = f"{type(exc).__name__}: {exc}"
    try:
        result["_links"] = deep_links(result)
    except Exception:                                      # noqa: BLE001
        result["_links"] = []
    return result


def status() -> dict:
    import time as _time
    fixtures = cache.stats()
    ages = {}
    for name, params in (("bt_buses", {}), ("dining_menu",
                                            {"location_num": "15",
                                             "dtdate": "09/19/2026"})):
        a = cache.age_seconds(name, params)
        ages[name] = None if a is None else round(a / 3600.0, 1)
    live = tools.get_live_bus(source=None)
    return {
        "mode": config.DEMO_MODE,
        "offline": config.CACHE_ONLY,
        "clock": config.now().isoformat(timespec="seconds"),
        "clock_pinned_to_snapshot": config.CACHE_ONLY,
        "wall_clock": datetime.now(TZ).isoformat(timespec="seconds"),
        "campus_now": config.now(TZ).strftime("%a %d %b %Y %H:%M"),
        "fixtures": fixtures["count"],
        "fixture_age_hours": ages,
        "live_vehicles": len(live.get("buses", live) or []),
        "live_stale": live.get("stale"),
        "generated_in_ms": round(_time.perf_counter() * 0 + 0, 1),
        "assumptions": {
            "eat_minutes": tools.EAT_MINUTES,
            "meal_ranking": tools.MEAL_RANKING_RULE,
            "meal_ranking_note": "a demo convenience, not dietary advice",
            "min_board_buffer_min": tools.MIN_BOARD_BUFFER_MIN,
            "walk_speed_mps": config.WALK_SPEED_MPS,
            "walk_path_factor": config.WALK_PATH_FACTOR,
            "bus_full_pct": config.BUS_FULL_PCT,
        },
        "caveats": [
            "blank allergen field means UNKNOWN, except in a documented "
            "allergen-free kitchen (Viridian) -- see venue_allergen_free",
            "live bus positions are a replayed snapshot; sched_delta_min is "
            "computed against the pinned snapshot clock",
            "walk times use a straight-line path factor, not a routed path",
        ],
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):        # quieter console
        if "/api/" not in (args[0] if args else ""):
            return

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, default=str, indent=1).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self) -> None:                 # noqa: N802
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            html = (Path(__file__).parent / "index.html").read_bytes()
            self._send(200, html, "text/html; charset=utf-8")
            return
        if path == "/api/status":
            self._json(status())
            return
        if path == "/api/scenarios":
            self._json([{k: s[k] for k in ("id", "label", "text")} for s in SCENARIOS])
            return
        if path == "/api/origins":
            # Static campus places, for when the device will not give a position
            # (geolocation needs a SECURE CONTEXT: localhost or HTTPS).
            self._json([{"key": k, "verified": bool(v.get("verified"))}
                        for k, v in config.PLACES.items()
                        if not v.get("dynamic")])
            return
        if path == "/manifest.webmanifest":
            self._send(200, json.dumps(MANIFEST).encode("utf-8"),
                       "application/manifest+json; charset=utf-8")
            return
        if path == "/icon.svg":
            self._send(200, ICON_SVG.encode("utf-8"), "image/svg+xml")
            return
        if path == "/favicon.ico":        # keep the console clean
            self._send(204, b"", "image/x-icon")
            return
        if path == "/api/raw":
            from urllib.parse import parse_qs, urlparse
            n = int((parse_qs(urlparse(self.path).query).get("n", ["1"])[0]))
            idx = max(0, min(len(SCENARIOS) - 1, n - 1))
            try:
                self._json(run_plan(dict(SCENARIOS[idx])))
            except Exception as exc:                      # noqa: BLE001
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:                # noqa: N802
        if self.path.split("?")[0] != "/api/ask":
            self._json({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:                                  # noqa: BLE001
            payload = {}
        try:
            origin_key, origin = resolve_origin(payload)
            if payload.get("scenario_id"):
                match = next((s for s in SCENARIOS
                              if s["id"] == payload["scenario_id"]), None)
                if match is None:
                    self._json({"error": "unknown scenario_id"}, 400)
                    return
                call = dict(match)
            else:
                call = parse_free_text(payload.get("text", ""))
            # Copy prefs rather than mutating the shared SCENARIOS entry.
            if origin_key:
                call["prefs"] = {**(call.get("prefs") or {}),
                                 "from_place": origin_key}
            self._json(run_plan(call, origin))
        except Exception as exc:                           # noqa: BLE001
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8321)
    ap.add_argument("--host", default=None,
                    help="bind address; defaults to 127.0.0.1, or 0.0.0.0 with --lan")
    ap.add_argument("--lan", action="store_true",
                    help="bind 0.0.0.0 so a phone on the same Wi-Fi can open it")
    args = ap.parse_args()
    if args.host is None:
        args.host = "0.0.0.0" if args.lan else "127.0.0.1"

    st = status()
    print("=" * 68)
    print("HokieDay demo server")
    print("=" * 68)
    print(f"  mode        : {st['mode']}   offline={st['offline']}")
    print(f"  campus now  : {st['campus_now']}  (pinned to the snapshot)")
    print(f"  fixtures    : {st['fixtures']}")
    print(f"  live buses  : {st['live_vehicles']}   stale={st['live_stale']}")

    # Bind BEFORE announcing a URL, so we never print an address we did not get.
    # Binding can fail because a previous instance is still running -- easy on
    # demo day, and a raw traceback is a bad look. Step to the next free port and
    # say plainly which one we actually bound.
    httpd = None
    for port in range(args.port, args.port + 6):
        try:
            httpd = ThreadingHTTPServer((args.host, port), Handler)
            break
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
            print(f"  port {port} is busy (an older HokieDay server still "
                  f"running?), trying {port + 1}")
    if httpd is None:
        print(f"\n  Could not bind any port in {args.port}-{args.port + 5}.\n"
              f"  Stop the other instance, or run: python3 app/server.py --port 9000")
        return 1

    bound = httpd.server_address[1]
    print(f"\n  open on this machine : http://127.0.0.1:{bound}/")
    if args.lan or args.host == "0.0.0.0":
        ips = lan_ips()
        if ips:
            print("  open on a phone     : " +
                  "  ".join(f"http://{ip}:{bound}/" for ip in ips))
            print("                        (same Wi-Fi; if it will not load, the")
            print("                         network is blocking device-to-device traffic)")
        else:
            print("  --lan: could not determine a LAN address")
    else:
        print("  (add --lan to reach it from a phone on the same Wi-Fi)")
    print("  Ctrl-C to stop\n")
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())