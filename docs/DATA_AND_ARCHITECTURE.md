# HokieDay — Locked Data, Stack & Architecture

**Verified live on 2026-09-19 ~11:20 ET.** All endpoints below were actually called and returned usable data.
Submission deadline: **Sun 2026-09-20 08:00 ET.** Judging 10:30–13:00, 4-minute pitch.

---

## 1. DATA VERDICT — what we can actually use

| # | Pillar | Endpoint | Status | What we get |
|---|--------|----------|--------|-------------|
| 1 | **BT static GTFS** | `http://www.bt4uclassic.org/gtfs/google_transit.zip` | ✅ **Real, current** | 297 stops · 24 routes · 3,658 trips · 74,301 stop_times · 7,351 shape points. `feed_version=FY27 Blacksburg 1.6A`, valid 2026-09-12 → 2026-10-31 |
| 2 | **BT live vehicles** | `https://ridebt.org/index.php?option=com_ajax&module=bt_map&method=getBuses&format=json&Itemid=101` | ✅ **Live** | 13 vehicles on road. Per bus: `routeId`, `gtfsTripId`, lat/lon, speed, `passengers`, **`percentOfCapacity`**, `isBusAtStop`, `states[]` history |
| 3 | **VT dining menu** | `https://foodpro.students.vt.edu/menus/API/MenuAtLocation.aspx?locationNum=15&dtdate=09/19/2026` | ✅ **Real** | 470 recipes today at D2, 3 meals, named sections. Per item: `recipeId`, name, description, portionSize/Unit, **allergens**, `legendImages` (vegetarian/vegan/halal) |
| 4 | **VT nutrition** | `.../menus/API/NutritiveReport.aspx?items=<id>*<portion>*<qty>,...` | ✅ **Real** | calories, protein, fat, carbs, fiber, sugar, added sugar, sodium, calcium, iron, vit A/C/D, potassium + %DV |
| 5 | **VT dining hours** | `https://apps.students.vt.edu/hours/Api/NonRestricted/FoodProCentersOpen/readByFoodPro/15/2026-09-19` | ✅ **Real** | open_time/close_time windows, `announcements`, logo. `extra_data[foodpro_id]` **joins to the menu API** |
| 6 | **Weather** | `https://api.weather.gov/points/37.2296,-80.4139` | ✅ **Real, no key** | forecast + hourly; no API key required |
| 7 | **Campus events** | `https://events.vt.edu/events` | ⚠️ **Scrape only** | Server-rendered cards, richly tagged: `free-food`, `in-person`/`hybrid`, `paid`, `blacksburg-va-24061`, category, department. **No API** — BeautifulSoup it |
| 8 | **FoodPro locations** | `.../menus/API/Locations.aspx` | ✅ **Real** | 12 locations with `locationNum` + `name` |
| 9 | **Allergen taxonomy** | `.../menus/API/Allergens.aspx?locationNum=15` | ✅ **Real** | 10 allergens (Milk, Eggs, Fish, Crustacean Shellfish, Tree Nuts, Peanuts, Wheat, Soybeans, Gluten, Sesame) + diet codes (wcveg/wcvtn/wcha/wcal) |

### ⚠️ Correction to an earlier assumption
`locationNum=09` is **Hokie Grill at Owens**, not D2. **D2 at Dietrick Hall = `locationNum=15`**. Locations query:
`09 Hokie Grill at Owens · 15 D2 at Dietrick Hall · 72 Deet's Place · 01 Ducky's at GLC · 71 DX · 39 Owens Food Court` (+6 more).

### The keystone join (proven working)
Every live bus carries `gtfsTripId`, which matches `trips.txt` in the static feed. Joining gives **schedule adherence**:

```
bus 6413 route SME  trip 9aa9176e stop 1635 sched 11:20:23 -> ON TIME       (load 30%)
bus 6412 route NMG  trip 0a2ebd26 stop 1411 sched 11:25:00 -> 3 min early   (load  4%)
bus 6411 route UCB  trip 771cf92b stop 1323 sched 11:30:00 -> 8 min early   (load  4%)
... 13/13 live vehicles joined successfully
```

**This is why the live re-planning demo is real, not staged.** No delay injection needed — you can show an actually-late or actually-early bus.

### Data QA caveats — say these out loud, they buy credibility
1. **`capacity` is unreliable.** One bus reported `capacity=24, passengers=24, percentOfCapacity=30` — inconsistent denominator. **Use `percentOfCapacity` only.**
2. **No GTFS-RT protobuf feed exists publicly.** BT's `/d-archive` page only *defines* GTFS-RT and links Google's spec; Transitland's catalog registers **only** a static URL. Our live feed is BT's **internal Joomla AJAX endpoint** (`com_ajax&module=bt_map`) — undocumented, no SLA. **Must have a schedule-only fallback.**
3. **No ETA/TripUpdates feed.** We get *positions only*. Any "arrives in 4 min" must be **computed by us** (project onto pattern shape / decay by speed). Label it as our inference.
4. **Buses run early, not late** (3–8 min observed). So the static schedule **overstates** wait time — prefer realtime-derived estimates.
5. **Menus are per-date and thin on weekends.** 470 recipes at D2 today is plenty, but Friday/Saturday differ hugely. Always pass an explicit `dtdate`.
6. **Events are scraped HTML** → brittle. Cache aggressively; degrade gracefully.

---

## 2. LOCKED TECH STACK

No options below — decisions made. Change only if blocked.

| Layer | Decision | Why (say this to judges) |
|-------|----------|--------------------------|
| Platform | **Databricks Free Edition** (serverless) unless the VTHacks Discord gives a sponsor workspace + token | Free Edition still includes **Lakebase, Model Serving, Apps**; quotas are the only limiter |
| Storage | **Delta Lake** in **Unity Catalog**, catalog `hokieday`, schemas `bronze` / `silver` / `gold` | Medallion = the consulting-standard data architecture; lineage is auto-captured for the deck |
| Ingestion | Python notebooks run as **Databricks Jobs** (15-min realtime job, daily static job). **Append snapshots with `INSERT INTO`**, no Kafka | ⚠️ *Architectural honesty:* you can't cleanly stream an HTTP JSON endpoint into Databricks without a landing zone. Notebook-append is reliable in 20 hours. **Production path (slide it):** write JSON to a **UC Volume** → **Auto Loader** picks it up → Structured Streaming |
| Query compute | **Serverless SQL warehouse** | Chat needs sub-second reads, not cluster startup |
| ML | **scikit-learn** GBM for dining wait-time + arrival correction; **MLflow** tracking & registry; **batch scoring into gold** | Batch scoring is enough for a demo. **Model Serving** as the production path |
| Agent | **Mosaic AI Agent Framework** (ResponsesAgent ChatAgent) with **Unity Catalog functions as tools** | UC functions = governed, reusable, versioned tools. This is the single most "Databricks-native" choice available |
| Semantic search | **Vector Search** (Delta-synced index) + Foundation Model API embeddings — **Tier 2 only** | Justified use: "something warm and filling" over dish descriptions. Skip if structured filters suffice |
| Serving UI | **Databricks Apps** (Streamlit or FastAPI+static) | Sponsor-native, appears in the architecture story |
| State / OLTP | **Lakebase** (Postgres) for student profile, preferences, chat session state | Textbook Lakebase justification: real writes, not analytics |
| Staff analytics | **Genie** space over gold tables | Cheap to build (point at tables + sample questions), huge on the slide |
| LLM | Whatever the workspace serves via **Foundation Model APIs** — **do not hardcode a model name** | Portability; also lets you swap to **Gemini via AI Gateway** to double-enter the MLH Gemini track |
| Demo safety | `cache/*.json` for every external call + `DEMO_MODE=cache` env flag | **Non-negotiable.** Expo = walk-up judging on unknown Wi-Fi |

---

## 3. ARCHITECTURE

```
                 EXTERNAL SOURCES (all verified live)
  ┌────────────┬───────────────┬─────────────┬──────────────┬──────────┐
  │ BT GTFS    │ BT live buses │ VT dining   │ VT nutrition │ NWS      │
  │ (static)   │ (15-min job)  │ menu API    │ + hours API  │ weather  │
  └─────┬──────┴───────┬───────┴──────┬──────┴──────┬───────┴────┬─────┘
        │              │              │             │            │
        ▼              ▼              ▼             ▼            ▼
  ╔══════════════════════════════════════════════════════════════════════╗
  ║  BRONZE  · raw landing (Unity Catalog Volumes + Delta)               ║
  ║  gtfs_stops · gtfs_trips · gtfs_stop_times · gtfs_shapes             ║
  ║  bus_pings · menu_items_snapshot · hours_snapshot · weather_raw      ║
  ╚═══════════════════════════════┬══════════════════════════════════════╝
                                  │  Lakeflow / notebooks
  ╔═══════════════════════════════▼══════════════════════════════════════╗
  ║  SILVER  · cleaned, conformed, joined                                ║
  ║  stop_departures (stop × trip × time)                                ║
  ║  bus_live  ──gtfsTripId JOIN──▶ SCHEDULE ADHERENCE (late/early)       ║
  ║  dining_locations ══foodpro_id══▶ hours  |  recipes ══recipeId══▶ nutrition
  ╚═══════════════════════════════┬══════════════════════════════════════╝
                                  │
  ╔═══════════════════════════════▼══════════════════════════════════════╗
  ║  GOLD  · decision-ready                                              ║
  ║  next_departures · bus_reliability · crowding_now                    ║
  ║  eat_options (diet + allergen + macro filters)                       ║
  ║  wait_forecast (MLflow model) · open_now                             ║
  ╚═══════┬═══════════════════════════════════════════════════┬══════════╝
          │                                                   │
          ▼                                                   ▼
  ╔═══════════════════════════════╗                 ╔═════════════════╗
  ║ Mosaic AI AGENT               ║                 ║ GENIE (staff)   ║
  ║ UC functions as tools:        ║                 ║ NL analytics    ║
  ║  get_next_departures          ║                 ╚═════════════════╝
  ║  get_live_bus (+adherence)    ║
  ║  walk_time                    ║                 ╔═════════════════╗
  ║  find_food (diet/allergen/kcal)║                ║ LAKEBASE        ║
  ║  get_hours · get_events       ║◀───────────────▶║ profile/prefs   ║
  ║  predict_dining_wait          ║                 ║ chat state      ║
  ║  plan_day  (orchestrator)     ║                 ╚═════════════════╝
  ╚═══════════┬═══════════════════╝
              ▼
  ╔═══════════════════════════════╗
  ║ DATABRICKS APP (chat UI)      ║
  ╚═══════════════════════════════╝
```

### The agentic loop (what makes it an *agent*, not a chatbot)
`plan_day` **drafts → checks against live reality → revises**. Specifically: propose an itinerary → call `get_live_bus` → if the bus is early/late or `percentOfCapacity` is high, **re-plan and explain the change**. Demo the diff explicitly: "here's plan A, here's why it died, here's plan B." Judges score that, not the itinerary.

### Tool interface contract (freeze this — it's what lets 4 people work in parallel)
```
get_next_departures(stop_id:str, route:str|None, horizon_min:int=180) -> [{route, dep_time, in_min}]
get_live_bus(route:str|None) -> [{bus_id, route, lat, lon, load_pct, at_stop, sched_delta_min}]
walk_time(from_place:str, to_place:str) -> {minutes, meters}          # haversine x1.25 @1.35 m/s
find_food(location_num:str, date:str, diet:str|None, avoid:[str], min_kcal:int|None,
          max_kcal:int|None) -> [{name, section, kcal, protein_g, allergens, diet_tags}]
get_hours(foodpro_id:str, date:str) -> [{open_time, close_time}]
get_events(date:str, tags:[str]|None) -> [{title, start, place, tags}]
predict_dining_wait(location_num:str, ts:str) -> {wait_min, confidence}
plan_day(student_id:str, start:str, end:str, prefs:dict) -> {itinerary, rationale, alternatives}
```

### Governance (the FERPA slide — 10 minutes of work, huge payoff)
- **No academic records, ever.** No transcript, no grades, no GPA enters the lakehouse. Self-declared skills and preferences only.
- Unity Catalog **column masks** on any student identifier; **row filters** so the app can only read the current student's row.
- **No PII in prompts.** Student id is a surrogate key; the agent sees `student_ref`, never a name.
- One-line framing: *"Governance is a platform capability here, not a policy promise — Unity Catalog enforces it."*

---

## 4. RISK REGISTER

| Risk | Mitigation |
|------|-----------|
| **No Databricks workspace / quota exhausted** | Check Discord first. Fall back to Free Edition. Tier 2 (Vector Search, Lakebase) drops before Tier 1 |
| **BT internal endpoint changes or rate-limits** | Cache every response; schedule-only fallback. **Don't hammer it** — poll ≤ every 60s |
| **Expo Wi-Fi dies** | `DEMO_MODE=cache` + recorded backup video captured by 06:00 |
| **Menu has no data for the demo date** | Pass an explicit known-good date (Sat 09/19 had 470 items) |
| **Over-scoping the agent** | Freeze the tool contract above. No new tools after 20:00 tonight |
| **Nobody sleeps** | Rotate 3-hour sleep shifts. A broken demo at 10:30 loses to a simpler one that runs |

---

## 5. FIVE THINGS THAT WIN THIS SPECIFIC CHALLENGE

1. **Use the `gtfsTripId` join on camera.** It's the proof your data is real and your agent reasons over live truth.
2. **Show one re-plan.** Draft → reality check → revised plan, with the reason stated.
3. **Say "Unity Catalog enforces it, not policy."** Answers FERPA, "realism", and "efficient use of Databricks" in one sentence.
4. **Ship the roadmap slide.** Judging explicitly rewards it and it costs one hour.
5. **Frame the whole thing as an engagement**: baseline KPI → future state → value case → 0–30/30–90/90–180 roadmap → risks. Deloitte judges grade their own reflection.