# VT GIS client contract

`hokieday.vtgis` is a stdlib-only, anonymous, read-only client for Virginia
Tech's public ArcGIS services. Every result is attributed to **Virginia Tech
Campus Planning, Infrastructure, and Facilities (CPIF) GIS** and carries the
source URL and wayfinding disclaimer.

## Supported runtime flow

1. On a building click or search, call `search_buildings(query)` or
   `building_by_id(id)`.
2. Call `building_detail(id)` to obtain the official building and its entrances.
   Building and entrance IDs are joined after normalizing the services' different
   zero-padding widths.
3. Treat the selected official building as the destination. Call
   `route_between_buildings(origin_id, destination_id, mode=...)` when both ends
   are buildings, or `solve_route(origin, destination, mode=...)` for a device or
   other coordinate.
4. Use `MODE_WALKING` for Walking or `MODE_ADA_WALKING` for ADA Walking. The ADA
   solve sends the service's complete `ADA Routes Only` travel-mode object. It
   may return a typed `NoRoute`; never silently retry it as ordinary Walking.
5. Render `Route.geometry`, `Route.directions`, and `Route.distance_m`. Minutes
   are explicitly estimated from the configured walking speed because this
   network has no time cost. `Total_Time=0` is unavailable, not a zero-minute
   trip.

Raw caller coordinates are always marked non-authoritative. Official building
and entrance features are authoritative only as VT GIS feature coordinates.
`Unavailable` is a typed degraded result. `walk_fallback()` is a labelled
haversine × path-factor estimate with a straight segment, not routed GIS
geometry.

## Closures

`closures()`, `effective_closures()`, and `closures_for_day()` parse and filter
published closure intervals. Closures are **informational only** in this build:
they are not submitted as ArcGIS barriers, `Route.closures_considered` is false,
and the UI must not claim that a route avoids a closure.

## Permission and persistence

The services are anonymously readable, but public access is not a redistribution
grant. Runtime requests only use ArcGIS query/solve operations; no public API
edits the upstream GIS. The shared cache may keep operational responses in the
gitignored `cache/` directory, but no real VT GIS response belongs in committed
`fixtures/`, tests, docs, exports, or snapshots.

`route_to_geojson()`, `buildings_to_geojson()`, and `snapshot_payload()` refuse
by default. Enabling one requires a `RedistributionPermit` made with
`authorize_redistribution(<written-permission reference>)`. Use that capability
only after VT has granted written redistribution permission and retain the
referenced record. There is intentionally no boolean override.

`tests/test_vtgis.py` uses synthetic ArcGIS-shaped payloads only. The optional
live check is explicit and leaves no repository files:

```bash
python3 scripts/vtgis_smoke.py --live
```

It uses an OS temporary cache, makes anonymous reads, prints summaries, and
removes the temporary responses before exit.

## Deliberate parent-work boundary

This module supplies only GIS Walking/ADA edges. `Fastest` and `Least Walking`
are walking-only descriptors today and must not be presented as multimodal
results.

The later parent integration must:

1. map official GIS origins/destinations and entrances to nearby GTFS stops;
2. generate candidate itineraries with GIS access/egress walks plus GTFS trips,
   transfers, schedules, and live-bus state;
3. compute one comparable end-to-end arrival time for **Fastest** and total
   walked distance for **Least Walking**;
4. rank candidates deterministically, preserve ADA/wheelchair constraints, and
   expose stale/unavailable transit states; and
5. update frontend labels only after those GTFS multimodal candidates are
   actually evaluated.

Until then, neither label implies a bus comparison.