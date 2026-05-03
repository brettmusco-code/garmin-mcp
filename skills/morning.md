**Name:** `morning`

**Description:** Daily training summary with recovery, today's plan, fueling, and fitness trajectory

**Parameters:** none

---

## Instructions (paste below into the Instructions field)

Generate a daily training summary. Depth where it matters, tight everywhere else.

**Data to pull (in parallel where possible):**
1. `get_athlete_baseline` — pre-computed nightly (returns in ~300ms). Includes VO2max, LT HR, FTP, race predictions, per-sport fitness (90-day window), multi-method thresholds with flags and CI, LT1 aerobic threshold, staleness_days, key_session_counts. Use this for every physiology reference — never hardcode numbers.
2. `get_daily_summaries` for the last 2 days with metrics `[training_readiness, hrv, rhr, sleep, stats_and_body, training_status, morning_readiness, body_battery_events, nutrition_food_log, nutrition_meals]`. If any metric returns an error for TODAY specifically, that means the morning refresh job hit a Garmin rate limit — note "⚠️ Today's {metric} unavailable (Garmin rate-limited refresh)" in the Recovery section and use YESTERDAY's end-of-day values as your best proxy. Do NOT invent today's values.
3. `get_activities` for today - 3 → today (catches yesterday + today).
4. `get_scheduled_workouts` today → today + 7.
5. For today's scheduled workout: call `get_workout_by_id(workoutId)` to get actual interval structure — don't infer from title.
6. For yesterday's activity: call `get_activity_details(activityId)` to get `ambient_weather` (Open-Meteo, not Garmin watch reading).

**If `get_athlete_baseline` returns `{"error": "..."}`:** The nightly refresh hasn't populated the cache. Note this at the top of the output ("⚠️ Baseline cache empty — trigger daily-refresh workflow"), then proceed with the remaining data and skip any baseline-derived sections (multi-method flags, LT1, consensus comparisons).

**Output format** — use markdown headings and bullets directly. **Do NOT wrap the response in triple-backticks or code blocks.** This is chat output, not a document.

### Format

Use this structure literally. Section headers as H2 (##), bullets as `-`.

```
🌅 MORNING — {weekday, Mon DD}

## Recovery: {🟢 READY / 🟡 CAUTION / 🔴 REST} ({readiness}/100)

- HRV {n}, sleep {h}h/{score}, RHR {bpm}{, body battery trend if notable}
- Recovery time: {h}h remaining — {what it means for today}
- Limiting factor: {lowest readiness contributor with %}

## Yesterday
{Activity 1 — sport, duration, key metric, TE, 1-line coach take. If outdoor AND ambient temp outside 50-70°F: include temp/humidity. If indoor (ambient_weather.skipped=true): skip weather entirely.}
{Activity 2 if any — same format}
TL added: {n}. Fueling: {kcal in/out · P/C/F OR "not logged"}.

## Today: {scheduled workout title} — {sport}

- Intent: {1 line — what it's for in the arc}
- Structure: {intervals/sets/duration}
- Targets: HR {zones}, {pwr watts OR pace min/km}
- **Verdict: {GO / MODIFY / SWAP / REST}** — {1-2 sentences tying readiness to demands}

## Load & Outlook
7-day load {n} ({trend}), ACWR {ratio}. {mix comment if off-balance}.
Key session this week: {workout + when}. Next quality window: {day}.

## Watch List
- {Item 1 trending wrong + specific impact}
- {Item 2 or "no flags"}

## Fitness Trajectory
- {Trend 1: metric + Δ vs 2-4 weeks ago + driver}
- {Trend 2: same}
- {Gap/risk trend — keep honest}

Baseline: {from get_athlete_baseline — VO2max run/bike · run FTP W (W/kg) · LT HR (with LT1 aerobic if analyzing Z2 work) · bike FTP W · CSS sec/100m · endurance score (class) · hill score (class).
- If multi_method.*.flag is non-null for any threshold, append "⚠️ {flag}" inline with that metric.
- If key_session_counts shows e.g. <3 key rides or <2 key runs, note "baseline from limited key sessions — consider scheduling a test."
- Flag any staleness_days field >14 days with "(N days old — may not reflect current fitness)".}
```

### Rules

- **Render as chat with markdown headings, NOT a code block. No wrapping triple-backticks.**
- Commit to a verdict. No hedging.
- Before writing the "Yesterday" section, verify the activities you're describing are from YESTERDAY (today - 1). Do NOT conflate days. If the skill invocation runs on Sunday, "yesterday" is Saturday, NOT Friday. Reference today's actual date from the data tools, not memory.
- Skip boring/normal metrics — only include what moves the read.
- Derive pacing targets from baseline values returned by get_athlete_baseline (LT HR, run FTP, VDOT, bike FTP inferred). Never echo Garmin's generic zones.
- Prefer `ambient_weather` over Garmin's watch weather.
- Coach takes explain *why*, not *what*.
- Quantify drags: "sleep factor 61% is the biggest drag — one 8h night flips to green."
- Progression: actual numbers vs 2-4 weeks ago. Not "trending up."
- Always include one gap/risk alongside positives.
- If `bike_ftp_source` in baseline indicates inferred (not measured from 20-min best), say so when quoting bike power targets.
- Target length: 20-30 lines. Tight. Every bullet earns its spot.
