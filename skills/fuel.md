**Name:** `fuel`

**Description:** Forward fueling plan — daily calories & macros plus per-workout fuel (pre / during / post + hydration) for your upcoming scheduled workouts, driven by your weight goal and current body stats.

**Parameters:**
- **Name:** `days`
- **Type:** number
- **Required:** no
- **Description:** Planning horizon in days ahead, 1-28. Default 7.

---

## Instructions (paste below into the Instructions field)

On-demand, **forward-looking** fueling plan. Where `/weekly` reviews the past week and saves the plan, and `/nutrition` checks plan-vs-actual mid-week, `/fuel` answers a single question: *"For each of my next N scheduled workouts, exactly what should I eat that day and how should I fuel that session?"* Its distinguishing output is a **per-workout fuel card** (pre/during/post grams + hydration) for the sessions that actually need one.

**Data to pull (in parallel where possible):**
1. `get_athlete_baseline` — `weight_kg`, per-sport fitness, `staleness_days`. Never hardcode weight.
2. `get_body_composition(startdate={today − 14}, enddate={today})` — latest **weight**, and the fields the other skills ignore: **body fat %** and **muscle / lean mass** (`dateWeightList[]` weight is in grams → ÷1000 for kg; surface bodyFat and muscleMass when present). Use these for recomposition context + a lean-mass protein sanity check.
3. `get_scheduled_workouts({today} → {today + days})` — the planned workouts (type, duration, intensity). For key days (≥Z3 or ≥75 min), optionally `get_workout_by_id(workoutId)` for the actual interval structure rather than inferring from the title.
4. `get_activities({today − 90} → {today})` — history for per-session kcal estimation from YOUR data, not generic tables.
5. `nutrition_trend(weeks=4)` — current weight trajectory + logging consistency, for goal-pacing context.

**Weight goal:** read the `## Weight goal` block from project memory (goal text, goal-set date, target date). Apply the project-instructions review triggers — if the goal is >4 weeks old, the target weight is already hit/passed, the target date has passed, or weight hasn't been logged in >14 days, **ask me to confirm/update before computing targets**. If no goal is set, ask: "maintain, lose (target + date), or gain?"

**BMR inputs:** Mifflin-St Jeor needs sex / height / age. Pull them from project memory if recorded; if unknown, use the endurance fallback `BMR ≈ weight_kg × 22` and note it once.

### Computation (do this per day in the window)

1. **BMR** = `weight_kg × 10 + height_cm × 6.25 − age × 5 + 5 (male) / −161 (female)`. Fallback if height/age unknown: `weight_kg × 22`.
2. **Goal adjustment** (from project-instructions weight-goal policy):
   - *Maintain:* 0.
   - *Lose X kg by DATE:* daily deficit = `(X × 7700 / weeks_remaining) / 7`, **capped at −500 kcal/day**, and the day's target **never below BMR × 1.2**. Protein **1.8–2.0 g/kg**.
   - *Gain:* **+300–500 kcal/day**, carb-led. Protein **1.6–1.8 g/kg**.
3. **Estimate session kcal from MY history, not generic lookups:** filter the last-90d activities to matches by `activityName` / `workoutId` / `activityType.typeKey` + duration (±20%). Use the **median kcal/hr from ≥3 matches × planned duration**. If <3 matches, mark the estimate low-confidence and use: recovery ride ~500 · Z2 ride ~650 · Z2 run ~700 · threshold/VO2 ~900–1000 · long ride ~550–700 kcal/hr.
4. **Daily target kcal** = `BMR × 1.3 (baseline day activity/NEAT) + session burn + goal adjustment`. (The ×1.3 already covers non-exercise activity — only add the *session* burn on top.)
5. **Macros:**
   - **Protein g** = `weight_kg × {1.6 maintain / 1.8–2.0 cut / 1.6–1.8 gain}`. If body fat is known, sanity-check against **~2.2 g/kg lean mass** as a floor on a cut.
   - **Carbs g** = `weight_kg × carb-ratio-by-session`: **3 rest · 4 easy · 5–6 tempo/SST · 7–8 threshold/VO2 or ride >2h**.
   - **Fat g** = `25% of target kcal / 9`, used as the balancer. Reconcile P+C+F back to target kcal; if a big-carb day already meets target on protein+carbs, hold fat near a floor (~0.6 g/kg) and let the day run a small surplus — **never cut protein to make fat fit**.
6. **Per-workout fuel card** — build one for **any session ≥75 min OR ≥Z3** (this is the "(if needed)" filter; skip easy/short days):
   - **Pre** (60–90 min before): **40–60 g carbs** (name 1–2 real foods). Add caffeine (~3 mg/kg) only on key/race-sim days.
   - **During** ({session duration}): **30–60 g carbs/hr**; scale to **60–90 g/hr** (mixed glucose:fructose) for efforts >2.5 h or races. Fluid **~500–750 ml/hr**, sodium **~500–800 mg/hr** (more in heat).
   - **Post** (within 60 min): **20–30 g protein + 50–70 g carbs**.

**Output format** — markdown headings directly. **Do NOT wrap in triple-backticks or code blocks.**

### Format

```
⛽ FUELING PLAN — {Mon DD} → {Mon DD} ({N} days)

Goal: {goal text} · {weeks_remaining} wk left · {maintain / −N kcal-day cut / +N kcal-day gain}
Body: {weight_kg}kg{ · body fat X% · lean mass Y kg}{  ⚠️ weight N days old if >7}
Weight trend (4wk): {start → end kg, Δ} — {on-pace / behind by N / ahead / stable}

## Daily targets

| Day | Session | Est burn | Target kcal | P / C / F (g) | Carb g/kg | Fuel |
|---|---|---|---|---|---|---|
| {Tue} | {workout or "rest"} | {kcal — "(median of N)" or "(est)"} | {n} | {p}/{c}/{f} | {x} | {⛽ / —} |
| ... | | | | | | |

## Per-workout fuel  (only the ⛽ days)

**{Day} — {Session} ({duration}, {intensity})**
- Pre (60–90m before): {40–60g carbs — example foods}{ + caffeine if key}
- During ({duration}): {g carbs/hr → ~total g}; fluid {ml/hr}, sodium {mg/hr}
- Post (within 60m): {20–30g protein + 50–70g carbs — example}

{repeat per qualifying session}

## Notes
- {goal pacing: "Required −N kcal/day; 4wk trend says on-pace / behind — do X"}
- {under-fueling / protein / stale-weight flags}
- {calendar-horizon note if Garmin has fewer than N days scheduled}
```

After the plan, offer once: *"Want me to save this as the week's plan? I'll write the `nutrition_plan` object via `save_weekly_snapshot` so `/nutrition` and `/morning` can track adherence against it."* Only call `save_weekly_snapshot` if I say yes — build the object in the same shape `/weekly` uses (per-day, keyed `YYYY-MM-DD`, with `session`, `target_kcal`, `expected_expenditure_kcal` = BMR + session burn, `protein_g`, `carbs_g`, `fat_g`, `notes`).

### Rules

- **Render as chat markdown. No wrapping code block.**
- Never hardcode weight — use `baseline.weight_kg` / `body_composition`. Flag any weight reading >7 days old.
- Estimate session burn from MY history; explicitly label low-confidence (`(est)`) estimates.
- Only emit a per-workout fuel card for sessions ≥75 min or ≥Z3 — don't clutter easy/short days.
- Reconcile P/C/F to target kcal; never shrink protein to make fat fit.
- Respect the goal guardrails: deficit capped at −500 kcal/day, day target never below BMR × 1.2. If hitting the target weight by the date would require more than that, say so plainly and suggest extending the timeline rather than prescribing an unsafe deficit.
- If `get_scheduled_workouts` returns fewer days than `days` (common with Garmin Coach adaptive plans that only populate ~1–2 weeks ahead), note: "Garmin calendar has workouts through {date}; days past that assume an easy/rest default — re-run when the plan extends."
- If no weight goal is set, or it's stale/passed per the review triggers, ask before computing — don't invent one.
- Target length: ~30–50 lines (scales with horizon).
