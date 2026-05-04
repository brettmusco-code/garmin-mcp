**Name:** `nutrition`

**Description:** Week-to-date nutrition — plan vs actual for each day, protein hit rate, deficit/surplus tracking, fueling recommendations

**Parameters:**
- **Name:** `days_back`
- **Type:** number
- **Required:** no
- **Description:** How many past days to show (1-14). Default 7.

---

## Instructions (paste below into the Instructions field)

Mid-week nutrition check-in. Shows how well I've matched my /weekly nutrition plan so far and what I need to adjust for the rest of the week.

**Data to pull:**
1. `nutrition_plan_vs_actual(days_back={days_back or 7})` — the key tool. Returns per-day rows with target/actual/delta for kcal + P/C/F, foods_logged count, Garmin adjusted goal, expenditure, net balance. Also returns `plan_source_weekly_snapshot` and `no_plan_available` flag.
2. `nutrition_trend(weeks=4)` — 4-week rollup of nutrition + weight. Returns per-week rows, weight_trajectory (start vs end kg, delta), summary (logging consistency %, intake rising/falling/stable, weight_trend).
3. `get_athlete_baseline` — for weight_kg and weight-goal context.

**If `no_plan_available=true`:** Note "⚠️ No nutrition plan from /weekly — either /weekly hasn't run yet, or the snapshot lacks a nutrition_plan field. Run /weekly on Sunday to generate one." Then fall back to expenditure-only analysis (actual intake vs expenditure, no target comparison).

**Output format** — use markdown directly. **Do NOT wrap in triple-backticks or code blocks.**

### Format

```
🍽️ NUTRITION CHECK — {start date} to {today}

Plan source: {plan_source_weekly_snapshot date} — {if >10 days old: "⚠️ plan is stale, rerun /weekly"}

## Daily Tracking

| Day | Session | Target kcal | Actual | Δ | P target / actual | Foods | Flag |
|---|---|---|---|---|---|---|---|
| Mon | {session} | {n} | {n or "—"} | {±n} | {n}/{n} | {count} | {⚠️ if delta <-500 or >+500 or foods=0} |
| Tue | ... | ... | ... | ... | ... | ... | ... |
| ... | | | | | | | |

## Week-to-Date Totals

- **Target intake:** {sum} kcal · P {n}g / C {n}g / F {n}g
- **Actual intake:** {sum} kcal · P {n}g / C {n}g / F {n}g (from {days_logged}/{N} days logged)
- **Expenditure:** {sum} kcal (BMR + active)
- **Net (intake − expenditure):** {±n}
- **Plan adherence:** {consistent / spotty — if days_logged < 4 of target days, call out}

## Protein hit rate
{M/N days at ≥1.6 g/kg of {weight_kg}kg = {target_per_day}g}. {Flag if <60% hit rate — chronic protein gap hurts recovery.}

## What to do for the rest of the week

- **Remaining days (today through Sunday):** sum of remaining targets = {sum target} kcal. Adjusted for actual-so-far: to stay on plan, you need to average {(sum target - sum actual) / remaining days} kcal/day for the rest of the week.
- **Goal alignment:** {pull weight goal from project memory. If lose 2kg by DATE: required weekly deficit = 500 kcal/day × 7 = 3500/wk. Current delta so far: {n}. Need {adjustment} the rest of the week to hit target.}
- **Hard days coming up:** {list any planned hard session in remaining days — remind to pre-fuel if target intake for that day is above average}
- **Under-fueled days to compensate:** {list any day from table with delta <-500 — suggest +carb tomorrow}

## 📉 4-Week Trend (from `nutrition_trend`)

| Week of | Avg kcal in | Avg kcal out | Daily Δ | Days logged | Avg weight | Protein ≥target |
|---|---|---|---|---|---|---|
| {week_start 3 weeks ago} | {n} | {n} | {±n} | {n}/7 | {kg} | {n}/7 |
| {week_start 2 weeks ago} | ... | ... | ... | ... | ... | ... |
| {last week} | ... | ... | ... | ... | ... | ... |
| {this week} | ... | ... | ... | ... | ... | ... |

- **Weight trajectory:** {start_weight_kg} → {end_weight_kg} ({+/− delta_kg}kg over 4w). Matches goal? {align with project-memory goal: losing on-pace / behind / ahead / stable against maintenance}.
- **Intake trend:** {rising / stable / falling}. {Interpretation — "declining intake tracks training deload" vs "unintentional drift".}
- **Logging consistency:** {logging_consistency_pct}% ({total_days_logged}/{total_window_days}). {Flag if <60% — can't trust the trend.}

## Recommended focus
{Single sentence tying together this-week adherence + the 4w trend. Examples: "Weight dropped 1.2kg in 3 weeks but intake rose — goal on track, keep logging 5+ days/wk" / "4w weight stable despite 500-kcal deficit — likely under-logging or TDEE higher than estimated" / "Protein hit <50% all 4 weeks — chronic gap, prioritize this."}
```

### Rules

- **Render as chat markdown. No wrapping code block.**
- Use `baseline.weight_kg` for protein target math. Target = weight × 1.6 for maintenance, × 1.8-2.0 for weight-loss phases.
- Flag days where foods_logged=0 in the table rather than showing zeros as actuals — under-logging is the #1 data quality issue.
- If a day's actual kcal < 1200 AND it was a training day, flag as likely under-logged.
- Rest of the week adjustment should be realistic — if the shortfall needs +1500 kcal/day to hit goal, note it's probably not achievable and suggest resetting expectations or extending the timeline.
- Target length: 20-35 lines.
