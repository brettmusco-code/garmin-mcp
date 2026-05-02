## Skill config for claude.ai

**Name:** `weekly`

**Description:** Sunday review — volume, intensity, load trajectory, next-week plan, and macro targets

**Parameters:** none

---

## Instructions (paste below into the Instructions field)

Generate a comprehensive weekly training review using MCP tools in parallel:

1. `get_activities` — this week (today - 7 → today) and last week (today - 14 → today - 7) for comparison
2. `get_daily_summaries` — last 7 days with `[sleep, hrv, rhr, training_readiness, stats_and_body, stress, nutrition_food_log, nutrition_meals, body_battery_events]`
3. `get_scheduled_workouts` — today → today + 7 days
4. `get_weekly_summaries` — 4 weeks for `[intensity_minutes, stress, steps]`
5. `get_race_predictions`, `get_training_score` (hill + endurance)
6. `analyze_training_period` — this week's aggregated totals
7. For key sessions: `get_activity_details(activityId)` to get `ambient_weather`

### Output format

```
📊 WEEK IN REVIEW — {Mon DD} to {Sun DD}

VOLUME (sport — this week vs last)
  • Cycling: {km} ({±%})
  • Running: {km} ({±%})
  • Swimming: {km} ({±%})
  • Strength: {sessions} ({±n})
  • Total hours: {n} vs {n} prior week

INTENSITY MIX
  • Z1-Z2 (base): {hours} / {%}
  • Z3 (tempo): {hours} / {%}
  • Z4-Z5 (threshold+): {hours} / {%}
  • Anaerobic TE total: {sum}
  • Read: {base-heavy / tempo-heavy / VO2-heavy / too spiky}

LOAD TRAJECTORY
  • Acute load (7d): {n} → {n} last week ({±%})
  • ACWR: {ratio} ({sweet-spot 0.8-1.3 / danger / too low})
  • Ramp rate: {±%}/week ({safe <10% / risk >15%})

KEY SESSIONS
  • {Top 1-3 hardest/most important sessions with 1-line verdict each + ambient temp/humidity if notable}

WHAT WORKED
  • {2-3 specific observations — e.g., "held target watts on threshold Tues"}

WHAT DRIFTED
  • {2-3 specific — e.g., "easy days averaged Z2 not Z1"}

WEATHER CONTEXT
  • {1-2 lines on weekly thermal load — hot week, cool week, day-to-day swings}
  • {Adjust interpretation of HR/pace data accordingly — hot week = elevated HR is thermal, not fitness regression}

📅 NEXT WEEK PLAN
SCHEDULED: {summary of next 7 days' scheduled workouts}
PLANNED LOAD (est): {TSS estimate}
KEY SESSION: {the hardest/most important + why}
BIGGEST RISK: {sleep debt / volume spike / intensity stack / nothing flagged}

🍽️ NUTRITION PLAN FOR NEXT WEEK
(Base: 73kg, endurance athlete, 1.6-1.8 g/kg protein target)

DAILY BASELINE (rest/easy day)
  • kcal: {~2,200-2,500}
  • Protein: {120-130g}
  • Carbs: {260-320g} (3.5-4.5 g/kg)
  • Fat: {70-85g}

HARD DAY (Z4-Z5 session or >2h session)
  • kcal: {+300-500 over baseline}
  • Carbs: {+50-80g, emphasize pre/during}
  • Protein: same or +10g if session >90min
  • Pre-session: {specific timing + food}
  • During: {if session >75min, gel/drink every 30min}
  • Post: {20-30g protein + 50-70g carb within 60min}

REST DAY
  • kcal: baseline minus ~200-300
  • Carbs: lower end (~250g)
  • Protein: do NOT reduce — recovery priority

WEEK-SPECIFIC CALL: {based on scheduled sessions, e.g. "Tues VO2 run + Sat long ride both need +carb days"}

📈 FITNESS TRAJECTORY (last 4 weeks)
  • Endurance score: {prev} → {curr} ({±})
  • VO2max (run): {prev} → {curr}
  • Race predictions: 5K {prev}→{curr}, 10K {prev}→{curr}, half {prev}→{curr}, marathon {prev}→{curr}
  • {1-line overall trajectory verdict}
```

### Rules

- Volumes in km (not meters). Round to 0.1.
- Flag ACWR outside 0.8-1.3 with specific intervention.
- Derive next-week load from scheduled workout types (recovery=20 TSS, base=60, threshold=90, VO2=100, long=120+).
- Nutrition targets must reflect my 73kg weight and VO2max 60 endurance profile.
- If nutrition data is missing, flag that and estimate from expenditure instead.
- Compare trajectory vs 4 weeks ago using actual numbers, not vague "improving."
- Commit to a specific training emphasis for next week ("base-build," "threshold push," "deload") — don't hedge.
- Prefer `ambient_weather` (Open-Meteo) over Garmin watch `weather`. Use weekly weather to interpret HR/pace trends (hot week = HR elevation is thermal, not fitness loss).
- Target length: 50-70 lines. Depth is the point.
