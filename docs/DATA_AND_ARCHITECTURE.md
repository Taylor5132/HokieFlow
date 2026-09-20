# HokieFlow — Locked Data, Stack & Architecture

**Verified live on 2026-09-19 ~11:20 ET.** All endpoints below were actually called and returned usable data.
Submission deadline: **Sun 2026-09-20 08:00 ET.** Judging 10:30–13:00, 4-minute pitch.

> **Attribution / use.** Unaffiliated student hackathon project. FoodPro data is
> Virginia Tech's; transit data is BT's; weather is NWS's. No endorsement by any
> of them is implied. The committed `fixtures/` are a frozen demo/test snapshot,
> not an official distribution — re-fetch upstream under their terms. See
> `README.md` → "Data sources, attribution, and use".

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
| 7 | **Campus events** | `https://events.vt.edu/sitemap.xml` + public detail HTML | ✅ **Integrated (Sept 2026, Blacksburg)** | AEM/Ensemble CMS, **no JSON/RSS/ICS**. Normalized by `hokieday.events`; snapshot of 27 Blacksburg events; tags `free-food`, `in-person`/`hybrid`, `paid`, `blacksburg-va-24061`, category, department |
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

## 2. TARGET TECH STACK AND SHIPPED STATUS

This section records the **production target**, not a claim that every box is
implemented. The shipped demo currently uses edge ingestion, five Unity Catalog
gold tables, four governed UC functions, deterministic `plan_day`, and a local
stdlib web app. It does **not** yet ship an LLM tool loop, trained ML model,
weather/events ingestion, Lakebase state, Genie, or a Databricks App deployment.

| Layer | Decision | Why (say this to judges) |
|-------|----------|--------------------------|
| Platform | **Databricks Free Edition** (serverless) unless the VTHacks Discord gives a sponsor workspace + token | Free Edition still includes **Lakebase, Model Serving, Apps**; quotas are the only limiter |
| Storage | **Delta Lake** in **Unity Catalog**, catalog `hokieday`, schemas `bronze` / `silver` / `gold` | Medallion = the consulting-standard data architecture; lineage is auto-captured for the deck |
| Ingestion | **Shipped:** laptop edge scripts fetch and upload JSONL because Free Edition blocks required outbound hosts. **Target:** scheduled egress-capable job → UC Volume → Auto Loader / Structured Streaming | Constraint-driven demo scaffold with an explicit production path |
| Query compute | **Serverless SQL warehouse** | Chat needs sub-second reads, not cluster startup |
| ML | **Not shipped.** Target: small bus-lateness model after ≥2,000 labelled observations, tracked in MLflow and batch-scored into gold | Current demo acts on observed schedule deviation; `predict_bus_delay` returns `basis=no_model` |
| Agent | **Shipped locally.** Provider-independent bounded tool loop + strict allowlist; Gemini REST adapter is opt-in in live mode; replay/provider failure retains the bounded parser | Deterministic tools own all facts and arithmetic. Virginia Tech HokieAI is a separate multi-model platform, not the name of this app agent |
| Semantic search | **Vector Search** (Delta-synced index) + Foundation Model API embeddings — **Tier 2 only** | Justified use: "something warm and filling" over dish descriptions. Skip if structured filters suffice |
| Serving UI | **Shipped:** local stdlib HTTP app. **Target:** Databricks App or another HTTPS host | Keep deployment claims separate from the working demo |
| State / OLTP | **Not shipped.** Target: Lakebase (Postgres) for profile, preferences, and session state | State must live outside the model |
| Staff analytics | **Not shipped.** Target: Genie over governed gold tables | Roadmap capability only |
| LLM | Runtime-configured provider/model; no hardcoded model. Gemini GenerateContent is the first adapter; a future documented HokieAI/ARC adapter can use the same protocol | Portability; provider choice never changes deterministic campus tools |
| Demo safety | Committed `fixtures/` replay store + `DEMO_MODE=cache`; live `cache/` remains separate and ignored | **Non-negotiable.** Expo = walk-up judging on unknown Wi-Fi |

---

## 3. ARCHITECTURE

The diagram below is the **target architecture**. Weather, model scores, the LLM
agent runtime, Lakebase, Genie, and Databricks App hosting are roadmap boxes.

```
                 EXTERNAL SOURCES (source endpoints verified)
  ┌────────────┬───────────────┬─────────────┬──────────────┬──────────┐
  │ BT GTFS    │ BT live buses │ VT dining   │ VT nutrition │ NWS      │
  │ (static)   │ (15-min job)  │ menu API    │ + hours API  │ weather  │
  └─────┬──────┴───────┬───────┴──────┬──────┴──────┬───────┴────┬─────┘
        │              │              │             │            │
        ▼              ▼              ▼             ▼            ▼
  ╔══════════════════════════════════════════════════════════════════════╗
  ║  BRONZE  · raw landing (Unity Catalog Volumes + Delta)               ║
  ║  gtfs_stops · gtfs_trips · gtfs_stop_times · gtfs_shapes             ║
  ║  bus_pings · menu_items_snapshot · hours_snapshot · weather_raw [future] ║
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
  ║  delay_model_scores [future; no model today] · open_now              ║
  ╚═══════┬═══════════════════════════════════════════════════┬══════════╝
          │                                                   │
          ▼                                                   ▼
  ╔═══════════════════════════════╗                 ╔═════════════════╗
  ║ Mosaic AI AGENT [target]      ║                 ║ GENIE [target]  ║
  ║ UC functions as tools:        ║                 ║ NL analytics    ║
  ║  get_next_departures          ║                 ╚═════════════════╝
  ║  get_live_bus (+adherence)    ║
  ║  walk_time                    ║                 ╔═════════════════╗
  ║  find_food (diet/allergen/kcal)║                ║ LAKEBASE        ║
  ║  get_hours · get_events          ║◀───────────────▶║ profile/prefs   ║
  ║  predict_bus_delay(no_model)  ║                 ║ chat state      ║
  ║  plan_day  (orchestrator)     ║                 ╚═════════════════╝
  ╚═══════════┬═══════════════════╝
              ▼
  ╔═══════════════════════════════╗
  ║ DATABRICKS APP [target]       ║
  ╚═══════════════════════════════╝
```

### The agentic loop (what makes it an *agent*, not a chatbot)
`plan_day` **drafts → checks against live reality → revises**. Specifically: propose an itinerary → call `get_live_bus` → if the bus is early/late or `percentOfCapacity` is high, **re-plan and explain the change**. Demo the diff explicitly: "here's plan A, here's why it died, here's plan B." Judges score that, not the itinerary.

### Tool interface contract (freeze this — it's what lets 4 people work in parallel)
```
get_next_departures(stop_id:str, route:str|None, horizon_min:int=180) -> [{route, dep_time, in_min}]
get_live_bus(route:str|None) -> [{bus_id, route, lat, lon, load_pct, at_stop, sched_delta_min}]
walk_time(from_place:str, to_place:str) -> {minutes, meters}          # haversine x1.30 @1.35 m/s
find_food(location_num:str, date:str, diet:str|None, avoid:[str], min_kcal:int|None,
          max_kcal:int|None) -> [{name, section, kcal, protein_g, allergens, diet_tags}]
get_hours(foodpro_id:str, date:str) -> [{open_time, close_time}]
get_events(date:str, tags:[str]|None) -> {state, events:[...], reason, coverage, fetched_at}
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