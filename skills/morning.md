## Skill config for claude.ai

**Name:** `morning`

**Description:** Daily training summary with recovery, today's plan, fueling, and fitness trajectory

**Parameters:** none

---

## Instructions (paste below into the Instructions field)

Generate a daily training summary. Depth where it matters, tight everywhere else.

**Data to pull (in parallel where possible):**
1. `get_daily_summaries` for the last 2 days with metrics `[training_readiness, hrv, rhr, sleep, stats_and_body, training_status, morning_readiness, body_battery_events, nutrition_food_log, nutrition_meals]`. If any metric returns an error, omit and note "not available" inline — don't let it fail the whole summary.
2. `get_activities` for today - 3 → today (catches yesterday + today).
3. `get_scheduled_workouts` today → today + 7.
4. For today's scheduled workout: call `get_workout_by_id(workoutId)` to get actual interval structure — don't infer from title.
5. For yesterday's activity: call `get_activity_details(activityId)` to get `ambient_weather` (Open-Meteo, not Garmin watch reading).

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
{Activity 1 — sport, duration, key metric, TE, 1-line coach take. Include ambient temp/humidity if outside 50-70°F ideal range.}
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

Baseline: VO2max 60 run / 59 bike · run FTP 438W (6.04 W/kg) · LT HR 181 · endurance 9908 (Elite) · hill 39 (low)
```

### Rules

- **Render as chat with markdown headings, NOT a code block. No wrapping triple-backticks.**
- Commit to a verdict. No hedging.
- Skip boring/normal metrics — only include what moves the read.
- Derive pacing targets from my physiology (LT HR 181, FTP 438W run, VDOT 60) — don't echo Garmin zones.
- Prefer `ambient_weather` over Garmin's watch weather.
- Coach takes explain *why*, not *what*.
- Quantify drags: "sleep factor 61% is the biggest drag — one 8h night flips to green."
- Progression: actual numbers vs 2-4 weeks ago. Not "trending up."
- Always include one gap/risk alongside positives.
- Cycling FTP is inferred (~330W); flag when used.
- Target length: 20-30 lines. Tight. Every bullet earns its spot.
