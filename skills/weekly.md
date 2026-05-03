**Name:** `weekly`

**Description:** Sunday review — volume, intensity, load trajectory, next-week plan, and macro targets

**Parameters:** none

---

## Instructions (paste below into the Instructions field)

Weekly training review. Denser than `/morning` — weekly patterns need a fuller picture — but still chat-formatted, not padded.

**Data to pull in parallel:**
1. `get_athlete_baseline` — pre-computed nightly (~300ms). Includes 90-day per-sport fitness trends, multi-method thresholds with CI + flags, key_session_counts, race predictions, staleness. Use for FITNESS TRAJECTORY section and all baseline references.
2. `get_activities` — this week (today - 7 → today) AND last week (today - 14 → today - 7).
3. `get_daily_summaries` — last 7 days with `[sleep, hrv, rhr, training_readiness, stats_and_body, stress, nutrition_food_log, nutrition_meals, body_battery_events]`.
4. `get_scheduled_workouts` — today → today + 7.
5. `get_weekly_summaries` — 4 weeks for `[intensity_minutes, stress, steps]`.
6. `analyze_training_period` — this week's totals.
7. For key sessions: `get_activity_details(activityId)` for `ambient_weather`.

**If `get_athlete_baseline` returns `{"error": "..."}`:** Note "⚠️ Baseline cache empty — trigger daily-refresh workflow" at the top, then complete the review from activities + daily summaries alone. Skip the FITNESS TRAJECTORY and multi-method sections.

**Output format** — markdown headings directly. **Do NOT wrap in triple-backticks or code blocks.**

### Format

```
📊 WEEK IN REVIEW — {Mon DD} to {Sun DD}

## Volume (this week vs last)
Cycling {km} ({±%}) · Running {km} ({±%}) · Swimming {km} ({±%}) · Strength {n} sessions · Total {h}h vs {h}h

## Intensity Mix
Z1-Z2 {h}h ({%}) · Z3 {h}h ({%}) · Z4-Z5 {h}h ({%}) · Anaerobic TE {sum}
Read: {base-heavy / tempo-heavy / VO2-heavy / too spiky}

## Load Trajectory
Acute {n} → {n prior} ({±%}), ACWR {ratio} ({sweet-spot/risk}), ramp {±%}/wk ({safe <10% / risk >15%}).

## Key Sessions
- {Session 1: 1-line verdict + conditions if notable}
- {Session 2: same}
- {Session 3: same}

## What Worked / What Drifted
- **Worked:** {2-3 specific observations}
- **Drifted:** {2-3 specific}
- **Weather context:** {1 line — hot/cool week effect on HR interpretation}

## 📅 Next Week
Scheduled: {summary}. Planned load: {TSS estimate}. Key session: {which + why}. Biggest risk: {sleep / volume spike / intensity stack / none}.

## 🍽️ Nutrition — next 7 days

Check project instructions for my current weight goal (maintain / lose Xkg by date / gain). If the goal is older than 4 weeks OR appears met/expired, ask me to confirm before using it. If no goal is set, ask.

**Adjustment from goal:**
- Maintain: baseline kcal (BMR × activity + predicted session kcal)
- Lose: baseline − 300-500 kcal/day. Never lower than BMR × 1.2. Protein stays at 1.8-2.0 g/kg (higher to preserve lean mass in deficit).
- Gain: baseline + 300-500 kcal/day. Carbs lead the surplus.

**Predicting session kcal — use history, not rules-of-thumb:**
For each scheduled workout this week, look at my actual recent sessions of the same type to estimate calorie burn rather than using generic TSS lookups:
1. From `get_activities` (last 90 days), filter to activities where the `activityName` matches the scheduled workout name (or same workout_id via `workoutId`), OR same `activityType.typeKey` + similar duration (±20%).
2. Compute median `calories / hour` across those matches (need ≥3 matches; use ≥5 if available).
3. Multiply by the scheduled workout's planned duration.
4. If <3 similar sessions exist, note that and fall back to: recovery ride ~500 kcal/h, Z2 ride ~650 kcal/h, Z2 run ~700 kcal/h, threshold/VO2 ~900-1000 kcal/h, long ride ~550-700 kcal/h depending on intensity.

Report the method inline: "Tuesday Sweet Spot Builder 90min: ~1020 kcal (median of 8 similar sessions last 90d at ~680 kcal/h)" OR "Wed Threshold Run 60min: ~900 kcal (estimated — only 1 similar session in cache)."

**Day-by-day:**

| Day | Session | Est kcal burn | Total kcal target | Protein (g) | Carbs (g) | Fat (g) | Timing |
|---|---|---|---|---|---|---|---|
| Mon | {workout or rest} | {from history} | {BMR×1.3 + burn + goal adj} | {weight × 1.7-2.0} | {weight × C} | {~25% of kcal / 9} | {pre/during/post if hard} |
| ... | ... | ... | ... | ... | ... | ... | ... |

Where C (carb ratio per kg) is:
- Rest day: 3 g/kg
- Easy/Z2: 4 g/kg
- Tempo/SST: 5-6 g/kg
- Threshold/VO2 or ride >2h: 7-8 g/kg

**Weekly totals:** {sum predicted burn · sum intake target · protein · carbs}. Verify sum aligns with goal (maintain/lose/gain weekly delta).

**Fueling window rules:** for any session ≥75min OR ≥Z3-intensity, pre: 40-60g carbs 60-90min prior; during: 30-60g carbs/hr; post: 20-30g protein + 50-70g carb within 60min.

## 📈 Fitness Trajectory (4 weeks)
- Endurance {prev → curr} ({±}), VO2max {prev → curr}, hill {prev → curr}
- Race: 5K {prev→curr}, 10K {prev→curr}, half {prev→curr}, mar {prev→curr}
- Multi-method check: {for each multi_method.*.flag that fires, a 1-line call-out. Also note consensus CI if tight (spread <3% of value) vs wide (spread >10% of value). Wide CI = uncertain baseline = a field test would materially improve precision.}
- Bike fitness drift: {from multi_method.bike_ftp.fitness_drift — if delta_pct ≥ 2%, report "efficiency rising {X}% (recent vs 90d baseline, N rides) — threshold capability likely up {X}%, no test needed." If ≤ -2%, flag fatigue/regression. If <2% absolute, "stable."}
- Power-duration spotlight: {from multi_method.bike_ftp.power_duration_curve_90d.curve — pick the duration where the best effort is most recent (smallest days_ago). "Strongest recent peak: {N}min @ {P}W from {activity_name} ({days_ago}d ago)."}
- Key session density: {from key_session_counts — "{run_key}/{run_total} key runs, {bike_key}/{bike_total} key rides, {swim_key}/{swim_total} key swims in last 90d". Flag if any sport <3 key sessions — baseline from that sport is under-supported.}
- {1-line verdict on where training is heading}
```

### Rules

- **Render as chat markdown. No wrapping code block.**
- Volumes in km, round to 0.1.
- Flag ACWR outside 0.8-1.3 with specific intervention.
- Next-week load from scheduled workout types: recovery=20 TSS, base=60, threshold=90, VO2=100, long=120+.
- Nutrition must reflect baseline.weight_kg (pulled dynamically) + current weight goal from project memory. Never hardcode weight.
- If nutrition data missing, say so and estimate from expenditure.
- **Never use Garmin's watch-reported ambient temperature.** The wrist sensor is distorted by body heat, sun, pavement — typically off by 5-15°F. Only use `ambient_weather.*` fields from `get_activity_details`, which come from Open-Meteo historical data. If `ambient_weather.skipped=true` (indoor activity), omit temperature entirely — don't substitute the watch reading.
- **Units check — Garmin `recoveryTime` is in MINUTES.** Always divide by 60 to report hours. 3056 min = 50.9h, NOT 3056h. This is a frequent mistake.
- Trajectory comparison: actual numbers vs 4 weeks ago, not vague "improving."
- Commit to a training emphasis for next week (base-build / threshold push / deload) — don't hedge.
- Prefer `ambient_weather` over Garmin watch. Use weekly weather to interpret HR/pace trends correctly.
- Target length: 35-50 lines. Depth where it matters.
