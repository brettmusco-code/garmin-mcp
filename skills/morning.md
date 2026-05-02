## Skill config for claude.ai

**Name:** `morning`

**Description:** Daily training summary with recovery, today's plan, fueling, and fitness trajectory

**Parameters:** none

---

## Instructions (paste below into the Instructions field)

Generate a detailed daily training summary. Be generous with depth — insight, not brevity.

**Data to pull (in parallel where possible):**
1. Core metrics: `get_daily_summaries` for the last 2 days with `[training_readiness, hrv, rhr, sleep, stats_and_body, training_status, steps, stress, intensity_minutes, max_metrics, respiration]`
2. Optional metrics (may be empty on some days): same tool with `[morning_readiness, body_battery_events, nutrition_food_log, nutrition_meals]` — if these return errors, omit from summary and note "not available."
3. Last 7 days of activities: `get_activities` (today - 7 → today)
4. Scheduled workouts: `get_scheduled_workouts` (today → today + 7)
5. Workout structure for today: once step 4 returns today's `workoutId`, call `get_workout_by_id(workoutId)`. Do NOT infer from title if actual structure is available.
6. Race predictions / lactate threshold / training scores if it's been >14 days since last pulled in conversation
7. Sleep trend analysis if HRV or sleep is flagged as concerning
8. For yesterday's activity: `get_activity_details(activityId)` to get `ambient_weather` — use this to contextualize HR/pace

**Metric notes:**
- Use `stats_and_body` (pre-warmed) not plain `stats` (not pre-warmed).
- Nutrition metrics only have data if I've logged meals. Empty responses are normal.
- Prefer `ambient_weather` (Open-Meteo) over Garmin's on-watch `weather` — watch temp is typically distorted by 5-15°F.

### Output format (follow exactly)

```
🌅 MORNING — {weekday, Mon DD}

RECOVERY: {🟢 READY / 🟡 CAUTION / 🔴 REST} — readiness {score}/100
  • HRV: {value} ({interpretation vs baseline})
  • Sleep: {h}h / score {n} ({deep/REM split if notable})
  • RHR: {bpm} ({vs baseline})
  • Recovery time remaining: {hours}h ({what it means for today})
  • Body battery: {trend}
  • Limiting factor: {which readiness factor is lowest and why}

YESTERDAY:
  • {Activity 1}: duration, key metrics, TE, ambient weather (temp/humidity/wind), 1-line coach take
  • {Activity 2 if any}: same
  • Total TL added: {n} (cumulative impact)
  • Fueling: {kcal consumed vs burned · P/C/F grams if logged, OR "not logged" — don't fabricate}
  • Thermal context: {if apparent temp >75°F or <45°F, note how this affected the session — expected HR/pace impact}

TODAY'S PLAN: {scheduled workout} — {sport}
  Intent: {what the workout is FOR in the training arc}
  Structure: {interval/set breakdown}
  Targets (from my physiology):
    • HR: {bpm zones from LT HR 181}
    • Power: {watts from FTP 438 run / ~330 bike inferred}
    • Pace (if run): {min/km from VDOT 60}
  Verdict: {GO / MODIFY / SWAP / REST}. {2-sentence reasoning tying readiness to demands}

7-DAY LOAD: {acute} ({trend})
  • ACWR: {ratio} ({sweet-spot interpretation})
  • Aerobic-low/high/anaerobic mix: {appropriate for goals?}

WATCH LIST:
  • {Item trending wrong + why it matters}
  • {Item 2 or "no other flags"}

WEEK OUTLOOK:
  • Key session: {hardest planned workout + purpose}
  • Next quality window: {soonest day readiness + plan enable a hard session}
  • Strategic note: {priority given current state}

BASELINE: VO2max 60 (run) / 59 (bike) · run FTP 438W @ 72.6kg (6.04 W/kg) · LT HR 181 · endurance 9908 (Elite) · hill 39 (low)

FITNESS PROGRESSION:
  • {Trend 1: metric + direction + magnitude + timeframe + driver}
  • {Trend 2: same}
  • {Trend 3: include one gap/risk alongside positives}
```

### Rules

- Commit to a verdict. No hedging.
- Derive pacing targets from my physiology — LT HR, FTP, VO2max/VDOT. Don't echo Garmin's generic zones.
- Every bullet adds information. Skip boring/normal metrics.
- Tie recovery to session demands in verdict reasoning — "readiness X, session asks Y, therefore Z."
- Coach takes explain *why* not *what* — e.g., "pairing compressed recovery windows," not "you did 2 sessions."
- Quantify drags on readiness — "Sleep factor 61% is the single largest drag — one 8h night flips yellow to green."
- Progression trends compare to 2-4 weeks ago with actual numbers. Not vague "trending up."
- Always include one gap/risk alongside positive trends. Don't be a cheerleader.
- Cycling FTP is inferred (~330W) — measured FTP is for running. Flag when using the inferred value.
- Independent pacing and predictions. Don't just relay Garmin — generate own using VDOT tables, Jack Daniels formulas, W/kg power-to-pace, LT-based models.
- Use `ambient_weather` (Open-Meteo) not Garmin's on-watch `weather` for yesterday's session context. Report actual conditions.
- Target length: 30-40 lines.
