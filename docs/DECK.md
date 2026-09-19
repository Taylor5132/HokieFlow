# HokieFlow — 4-minute pitch deck

**8 slides.** Timings are targets for a 4:00 pitch with ~10s of slack.

> ⚠️ **Two rules before you present this.**
> 1. **Anything marked `TO MEASURE` must be replaced with your own measurement.** Do not read out a number you did not measure — the whole deck's credibility rests on the numbers being ours.
> 2. **Say "Free Edition" only where slide 5 does.** It explains a real constraint; it is not an apology.
>
> Every other figure on these slides was verified during the build and is recorded in `SDD.md`.

---

## Slide 1 — Title · 15s

> # HokieFlow
> ### Campus life, in one answer — and it re-plans when reality moves.
>
> Deloitte × Databricks · VTHacks 14 · [team names]

**Notes.** One sentence, then advance. Do not read the subtitle aloud; let them read it.

---

## Slide 2 — The problem · 30s

Three ordinary questions, each needing different systems:

| Question | Systems needed today |
|---|---|
| "Can I eat between these two classes?" | menu + hours + walking time |
| "Which bus, and is it full?" | schedule + live positions + crowding |
| "What's happening on campus now?" | events |

**Baseline, measured by us:** `TO MEASURE` — **___ apps, ~___ minutes** for one decision.
*Method (do this, 15 minutes): a teammate who hasn't seen the app answers "can I eat and still make my 1:25?" from scratch on their phone. Stopwatch the time, count the apps, screenshot them. That screenshot is your evidence.*

**The gap.** Each campus app is authoritative for **one** system and blind to all the others. No vendor sells the integration layer, because the value lands on the *student*, not on any single system owner. That is precisely the space an orchestration agent fills.

**Notes.** If asked "why hasn't someone done this?" — that last line *is* the answer. It's a strategy gap, not a technical one.

---

## Slide 3 — What we built · 30s

**Not another app. The orchestration layer over systems VT already runs.**

- **Mobile-first** — students don't open a laptop to ask whether they can make class
- **Uses your actual location** — distances measured from where you *are*
- **Re-plans when reality moves** — and states *why* the plan changed
- **Shows its sources** — every number tagged scheduled / estimated / from the menu API
- Composes existing systems; replaces none

**Notes.** Lead with the reframe, not the feature list: "we're not building the seventh app, we're building the thing that makes the other six agree."

---

## Slide 4 — LIVE DEMO · 75s

**This is the pitch. Everything else supports it.**

**Click path**
1. Judge asks a real question → **"Can I eat and still make my 1:25?"**
   → plan appears: walk → **eat** (a real dish, with kcal) → walk. Origin shown.
2. **"Same trip, but I want the bus."**
   → **PLAN A** built (bus route, boarding stop, wait)
3. The captured vehicle state is re-checked → **RE-PLANNED** banner with a real cause from the feed:
   *"Route CAS is 1.4 min early, leaving only 1.5 min to board at Tennis Courts; the plan requires a 2 min buffer."*
4. **PLAN B** appears beside PLAN A, dimmed, so the change is **inspectable, not asserted**

**Say this, not more:** *"Plan A was built, then reality was re-checked, and plan A died. Both plans are on screen — you can see exactly what changed and why."*

**Fallback if the room's Wi-Fi dies:** the app is running offline against a frozen snapshot. That is a feature, and worth saying out loud: *"this is running with networking off, from a snapshot we captured."*

---

## Slide 5 — How it's built on Databricks · 45s

[architecture diagram — `SDD.md` §4.2]

**All of this is live in the workspace, not mocked:**

| What | Verified |
|---|---|
| Unity Catalog `hokieday.gold` | 5 Delta tables: 297 stops · 34 departures · **13 live vehicles** · 470 menu items · hours |
| Agent tools as **governed UC functions** | `get_next_departures` · `get_live_bus` · `find_food` · `get_hours` |
| **The keystone join** | live vehicle → `gtfsTripId` → static schedule → schedule adherence, **13/13 matched, 0 unmatched** |
| Table & function COMMENTs | these are what the agent and Genie read — the documentation *is* the interface |
| Governance | no transcript, no grades, no GPA ever enters the lakehouse — **Unity Catalog enforces that, it isn't a policy promise** |

**Two things worth volunteering before you're asked:**

1. **Free Edition restricts outbound internet**, so ingestion runs **at the edge** (our pipeline fetches, the platform owns storage, governance and reasoning). In production that becomes a scheduled job with proper egress — **no change to the gold logic**.
2. **The keystone join is the proof the data is real.** Live vehicles carry `gtfsTripId`, which matches the static feed, so we can compute whether a bus is late *or early* — which is what makes the re-plan possible at all.

---

## Slide 6 — Trust · 45s · *your strongest differentiator*

Most teams will say "we filter allergens." We can say something better, and it came from checking a source instead of trusting our own reasoning.

**What we found**

- D2 at Dietrick Hall: **470 menu items. 188 of them state no allergens at all.**
- Naive handling treats a blank field as "safe" — which would have exposed a nut-allergic student to any of **140 items whose risk is simply unknown**.
- So we treat blank as **UNKNOWN** and exclude it…
- …**except** D2's **Viridian** kitchen, which VT documents as free from the top nine allergens with separate storage, preparation, cooking and serving space — and **all 48 Viridian items carry a blank field.**

A blanket "blank = exclude" rule would have hidden *exactly the food that student needs.*

**Result:** three-way policy — 470 items → **288 kept, 48 Viridian preserved, 0 leaks.**

**Also on this slide:** we surface **GPS accuracy** (±N m) and **reject an implausible position**. A laptop on Wi-Fi usually reports the ISP's location — ~350 km away — and accepting that would have produced a plan containing a multi-day walk, stated with total confidence. We reject it with a stated reason and fall back.

**Notes.** This slide is why a judge should trust the other slides. Lead with the *finding*, not the feature.

---

## Slide 7 — Value · 30s

```
Student minutes saved / week ──▶ student experience        (primary)
   ├─ decisions that need ≥2 apps   (today: most)
   ├─ re-checks after a bus moves
   └─ meals abandoned for lack of a reachable option

Service utilisation ──▶ dining + transit ROI               (secondary)
   ├─ off-peak dining shifted into capacity
   └─ fewer missed classes on transit delay
```

**The calculation:** `TO MEASURE` — minutes saved per decision **×** decisions per student per week **×** students.

*To fill this: multiply your measured baseline by a surveyed frequency. Ask 10 students on the floor, once: "how often do you skip eating or miss a bus because you couldn't work it out in time?" That is a real, defensible number and it takes 10 minutes.*

**Notes.** If challenged on precision: "we measured the single decision; the frequency is self-reported from 10 students. We'd rather show the method than a made-up figure."

---

## Slide 8 — Roadmap · 30s

| Phase | Window | Mode | Exit criteria |
|---|---|---|---|
| **Shadow** | 0–30 days | agent recommends, staff approve, students unaffected | ≥95% recommendation acceptance |
| **Assisted** | 30–90 days | pilot cohort, human escalation path | minutes saved per student per week |
| **Autonomous** | 90–180 days | full rollout, production ingestion | KPI trend sustained, source SLAs agreed |

**Shadow mode first is the point.** It is how real AI engagements de-risk, and it is the honest answer to *"what if it's wrong?"*

**Future:** parking · SafeRide · gyms · library hours · calendar write-back · production ingestion (UC Volume → Auto Loader) · **multi-university — the architecture is source-agnostic by design.**

---

# Rehearsal checklist

- [ ] Baseline measured, with a screenshot (`TO MEASURE` gone from every slide)
- [ ] Frequency surveyed with 10 students
- [ ] App running offline: `DEMO_MODE=cache python3 -u app/server.py`
- [ ] Demo clicked through **3 times**, out loud, with a timer
- [ ] **Backup video recorded while the app is known-good**
- [ ] Architecture diagram exported
- [ ] Every number on the slides traceable to `SDD.md` or your own measurement

# Q&A — see `SDD.md` §17.2 for the full set

| Question | Answer in one line |
|---|---|
| Different from Google Maps / the dining app? | We're the orchestration layer across systems, and we re-plan rather than inform. |
| Where's the Deloitte part? | Strategy framing: baseline, value case, phased roadmap, governance. |
| Is this real data? | Name the feeds: BT GTFS static schedule, a captured-live BT vehicle snapshot, and VT menu / nutrition / allergens / hours. The offline demo replays that frozen snapshot. Weather and events are roadmap, and there is no synthetic delay control — re-planning runs on the captured live state. |
| How do you handle student privacy? | No academic records enter the lakehouse; Unity Catalog enforces it. |
| Why an LLM rather than a script? | The offline demo uses a bounded parser today. The agent layer is for open-vocabulary clarification and tool choice; deterministic tools still own every exact fact. |
| What's the model predicting? | Not deployed yet. Today we act on observed schedule deviation; prediction is gated until the poller produces at least 2,000 labelled rows. |

# What we'd cut if we were short on time

In order: the Databricks App wrapper → the events feature → Slide 7's survey → the GPS picker fallback. **Never** slide 4 (the demo) or slide 6 (trust).