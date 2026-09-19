"""Draw a plan as an SVG map — no basemap, no API key, no network.

WHY OUR OWN MAP RATHER THAN GOOGLE/APPLE
The demo must survive with networking off, and an embedded Google/Apple map needs
tile fetches to render anything. We also already hold the real geometry: the bus
leg's trip carries a GTFS shape (67 shapes, 23-367 points, median 97), so the
orange line below is *the route the bus actually drives* — our own ingested data
rather than a screenshot.

HONESTY RULES BAKED IN
  * Bus legs are drawn from GTFS shape geometry  -> real
  * Walk legs are drawn as STRAIGHT dashed lines -> our walk model is
    haversine x 1.30, NOT a routed path, so drawing a path would be a lie.
    The caption says so, on the image.
  * There are no street names or imagery. This is a schematic, and it is labelled
    as one rather than passed off as a basemap.
"""
from __future__ import annotations

import html
import math

from hokieday import gtfs

W, H = 660, 430
PAD = 30
METRES_PER_DEG_LAT = 111_320.0

INK = "#e9edf3"
DIM = "#98a2b3"
MUTED = "#6b7280"
WALK = "#8892a4"
BUS = "#e87722"
BUS_HALO = "#3a2a18"
ORIGIN = "#5aa9e6"
DEST = "#861f41"
EAT = "#d29922"


def _project(points, lat0):
    """lon/lat -> planar units, with cos(lat) correction so the shape isn't skewed."""
    k = math.cos(math.radians(lat0))
    return [(lon * k, lat) for lat, lon in points]


def _fmt_m(m: float) -> str:
    return f"{m / 1000:.1f} km" if m >= 1000 else f"{int(round(m / 10.0) * 10)} m"


def build_map_svg(itinerary: dict | None, overlay: dict | None = None,
                  stops_lookup: dict | None = None,
                  width: int = W, height: int = H) -> str:
    """Render an itinerary as a self-contained SVG string (empty string if nothing
    drawable). Embeds directly via innerHTML — no external references.

    `overlay` draws a second itinerary's BUS geometry in grey behind the chosen
    plan. That is what makes a re-plan legible in ONE image: the route we planned
    sits visibly under the route we are using instead, and the bbox still covers
    both so the abandoned loop is not cropped away.
    """
    if not itinerary or not itinerary.get("legs"):
        return ""

    legs = itinerary["legs"]

    # ---------------------------------------------------------------- geometry
    bus_paths: list[list[tuple[float, float]]] = []
    overlay_paths: list[list[tuple[float, float]]] = []
    walk_lines: list[tuple[tuple[float, float], tuple[float, float]]] = []
    markers: list[dict] = []
    all_pts: list[tuple[float, float]] = []

    try:
        g = gtfs.load_gtfs()
    except Exception:                                        # noqa: BLE001
        g = None

    def _shape_for(leg: dict):
        if g is None or not leg.get("trip_id"):
            return None
        try:
            return gtfs.shape_for_trip(g, leg["trip_id"])
        except Exception:                                    # noqa: BLE001
            return None

    for leg in (overlay or {}).get("legs", []):
        if leg.get("type") == "bus":
            shape = _shape_for(leg)
            if shape:
                overlay_paths.append(list(shape))
                all_pts.extend(shape)

    for leg in legs:
        fc, tc = leg.get("from_coords"), leg.get("to_coords")
        if leg["type"] == "bus":
            shape = _shape_for(leg)
            if shape:
                bus_paths.append(list(shape))
                all_pts.extend(shape)
            if fc and tc:
                markers.append({"kind": "stop", "pt": tc,
                                "label": f"get off (stop {leg.get('to_stop')})"})
        elif leg["type"] == "walk" and fc and tc:
            walk_lines.append((fc, tc))
            all_pts.extend([fc, tc])
        elif leg["type"] == "eat" and leg.get("coords"):
            markers.append({"kind": "eat", "pt": tuple(leg["coords"]),
                            "label": str(leg.get("place") or "eat")})
            all_pts.append(tuple(leg["coords"]))

    if not all_pts:
        return ""

    # origin = first walk's from, destination = last walk's to
    origin = next((l["from_coords"] for l in legs if l.get("from_coords")), None)
    dest = next((l["to_coords"] for l in reversed(legs) if l.get("to_coords")), None)
    if origin:
        markers.append({"kind": "origin", "pt": tuple(origin), "label": "start"})
    if dest:
        markers.append({"kind": "dest", "pt": tuple(dest), "label": "destination"})

    lats = [p[0] for p in all_pts]
    lons = [p[1] for p in all_pts]
    lat0 = (min(lats) + max(lats)) / 2.0
    proj = _project(all_pts, lat0)
    xs = [p[0] for p in proj]
    ys = [p[1] for p in proj]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    spanx = max(maxx - minx, 1e-6)
    spany = max(maxy - miny, 1e-6)

    # fit into the drawing box, preserving aspect ratio
    box_w, box_h = width - 2 * PAD, height - 2 * PAD
    scale = min(box_w / spanx, box_h / spany)
    off_x = PAD + (box_w - spanx * scale) / 2.0
    off_y = PAD + (box_h - spany * scale) / 2.0

    def xy(pt: tuple[float, float]) -> tuple[float, float]:
        lon_c = pt[1] * math.cos(math.radians(lat0))
        px = off_x + (lon_c - minx) * scale
        py = off_y + (maxy - pt[0]) * scale
        return px, py

    def lg(v: float) -> float:
        return max(1.0, min(9.0, v))

    parts: list[str] = []
    parts.append(
        f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
        f'aria-label="route map" xmlns="http://www.w3.org/2000/svg" '
        f'style="display:block;border-radius:14px;background:#12151b">'
    )

    # ---- quiet context: nearby stops, so the map reads as a place
    if g is not None:
        pad_deg_lat = 0.0012
        pad_deg_lon = 0.0015
        near = [(s.lat, s.lon) for s in g.stops.values()
                if min(lats) - pad_deg_lat <= s.lat <= max(lats) + pad_deg_lat
                and min(lons) - pad_deg_lon <= s.lon <= max(lons) + pad_deg_lon]
        for lat, lon in near[:120]:
            px, py = xy((lat, lon))
            # Clip to the drawing box. Stops inside the padded lat/lon window can
            # still project outside the fitted area (seen at cy=-22.6 and
            # cy=459.4 in a 430-tall viewBox), which wastes elements on dots that
            # are invisible anyway.
            if not (0 <= px <= width and 0 <= py <= height):
                continue
            parts.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="2" fill="{MUTED}" '
                         f'opacity="0.55"/>')

    # ---- abandoned plan A route, drawn first so it sits UNDER the live plan
    for path in overlay_paths:
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in (xy(p) for p in path))
        parts.append(f'<polyline points="{pts}" fill="none" stroke="#4b5563" '
                     f'stroke-width="5" stroke-linejoin="round" stroke-linecap="round" '
                     f'stroke-dasharray="10 7" opacity="0.9"/>')

    # ---- walk legs: dashed, straight. Labelled as an estimate in the caption.
    for a, b in walk_lines:
        (x1, y1), (x2, y2) = xy(a), xy(b)
        parts.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                     f'stroke="{WALK}" stroke-width="2.5" stroke-dasharray="7 6" '
                     f'stroke-linecap="round" opacity="0.85"/>')

    # ---- bus legs: the real GTFS shape, with a halo so it reads over the dots
    for path in bus_paths:
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in (xy(p) for p in path))
        parts.append(f'<polyline points="{pts}" fill="none" stroke="{BUS_HALO}" '
                     f'stroke-width="11" stroke-linejoin="round" stroke-linecap="round"/>')
        parts.append(f'<polyline points="{pts}" fill="none" stroke="{BUS}" '
                     f'stroke-width="5.5" stroke-linejoin="round" stroke-linecap="round"/>')

    # ---- markers
    for m in markers:
        px, py = xy(m["pt"])
        if m["kind"] == "origin":
            parts.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="9" fill="{ORIGIN}" '
                         f'stroke="#0f1115" stroke-width="3"/>')
        elif m["kind"] == "dest":
            parts.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="9" fill="{DEST}" '
                         f'stroke="#0f1115" stroke-width="3"/>')
            parts.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="15" fill="none" '
                         f'stroke="{DEST}" stroke-width="2" opacity="0.5"/>')
        elif m["kind"] == "eat":
            parts.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="7" fill="{EAT}" '
                         f'stroke="#0f1115" stroke-width="3"/>')
        else:                                    # a stop to get off at
            parts.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="5" fill="{BUS}" '
                         f'stroke="#0f1115" stroke-width="2"/>')

    # ---- scale bar: a round distance that fits about a quarter of the width
    metres_per_unit = METRES_PER_DEG_LAT / scale
    target = (width * 0.25) * metres_per_unit
    nice = next((n for n in (50, 100, 200, 400, 800, 1600, 3200) if n >= target), 3200)
    bar_px = nice / metres_per_unit
    by = height - PAD + 6
    parts.append(f'<line x1="{PAD}" y1="{by}" x2="{PAD + bar_px:.1f}" y2="{by}" '
                 f'stroke="{DIM}" stroke-width="2"/>')
    parts.append(f'<text x="{PAD + bar_px + 8:.1f}" y="{by + 4}" fill="{DIM}" '
                 f'font-size="11" font-family="sans-serif">{html.escape(_fmt_m(nice))}</text>')

    # ---- legend: says plainly what is real and what is an estimate
    ly = 16
    legend = [("walk (straight-line estimate)", WALK, True),
              ("bus (GTFS shape geometry)", BUS, False)]
    if overlay_paths:
        legend.append(("plan A route (abandoned)", "#4b5563", True))
    legend += [("start", ORIGIN, False), ("eat", EAT, False),
               ("destination", DEST, False)]
    parts.append('<g font-size="11" font-family="sans-serif">')
    for i, (label, colour, dashed) in enumerate(legend):
        x = 12 + i * 0
        y = ly + i * 15
        if dashed:
            parts.append(f'<line x1="{x}" y1="{y}" x2="{x + 16}" y2="{y}" '
                         f'stroke="{colour}" stroke-width="2.5" stroke-dasharray="5 4"/>')
        else:
            parts.append(f'<line x1="{x}" y1="{y}" x2="{x + 16}" y2="{y}" '
                         f'stroke="{colour}" stroke-width="4" stroke-linecap="round"/>')
        parts.append(f'<text x="{x + 22}" y="{y + 4}" fill="{DIM}">'
                     f'{html.escape(label)}</text>')
    parts.append('</g>')

    parts.append('</svg>')
    return "".join(parts)