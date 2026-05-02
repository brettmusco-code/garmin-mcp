## Skill config for claude.ai

**Name:** `weekly`

**Description:** Sunday review — volume, intensity, load trajectory, next-week plan, and macro targets

**Parameters:** none

---

## Instructions (paste below into the Instructions field)

Weekly training review. Denser than `/morning` — weekly patterns need a fuller picture — but still chat-formatted, not padded.

**Data to pull in parallel:**
1. `get_athlete_baseline` — fresh physiology and per-sport fitness trends from the last 60 days. Use for the FITNESS TRAJECTORY section and all baseline references.
2. `get_activities` — this week (today - 7 → today) AND last week (today - 14 → today - 7).
3. `get_daily_summaries` — last 7 days with `[sleep, hrv, rhr, training_readiness, stats_and_body, stress, nutrition_food_log, nutrition_meals, body_battery_events]`.
4. `get_scheduled_workouts` — today → today + 7.
5. `get_weekly_summaries` — 4 weeks for `[intensity_minutes, stress, steps]`.
6. `analyze_training_period` — this week's totals.
7. For key sessions: `get_activity_details(activityId)` for `ambient_weather`.

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

## 🍽️ Nutrition
(Use baseline.weight_kg. Protein 1.6-1.8 g/kg, carbs 3.5-4.5 g/kg base / up to 7 g/kg on long-hard days.)

**Baseline (easy/rest day):** ~{kcal estimate: BMR × 1.4 + session kcal} · P {weight_kg × 1.7}g / C {weight_kg × 4}g / F {~25% of kcal / 9}g
**Hard day (+300-500 kcal):** add {+50-80g carbs pre/during}, {+10g protein post} within 60min
**Rest day (-200-300 kcal):** lower carbs (~3 g/kg), maintain protein
**Week-specific:** {call out hard days from schedule, e.g. "Tues VO2 + Sat long ride → +carb days"}

## 📈 Fitness Trajectory (4 weeks)
- Endurance {prev → curr} ({±}), VO2max {prev → curr}, hill {prev → curr}
- Race: 5K {prev→curr}, 10K {prev→curr}, half {prev→curr}, mar {prev→curr}
- Multi-method check: {for any multi_method.*.flag that fires, mention it in 1 line — e.g., "Garmin VO2max N but M methods suggest N±Δ. Field test or accept Garmin is lagging."}
- {1-line verdict on where training is heading}
```

### Rules

- **Render as chat markdown. No wrapping code block.**
- Volumes in km, round to 0.1.
- Flag ACWR outside 0.8-1.3 with specific intervention.
- Next-week load from scheduled workout types: recovery=20 TSS, base=60, threshold=90, VO2=100, long=120+.
- Nutrition must reflect baseline.weight_kg (pulled dynamically) + endurance profile. Never hardcode weight.
- If nutrition data missing, say so and estimate from expenditure.
- Trajectory comparison: actual numbers vs 4 weeks ago, not vague "improving."
- Commit to a training emphasis for next week (base-build / threshold push / deload) — don't hedge.
- Prefer `ambient_weather` over Garmin watch. Use weekly weather to interpret HR/pace trends correctly.
- Target length: 35-50 lines. Depth where it matters.
