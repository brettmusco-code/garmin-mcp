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
1. `nutrition_plan_vs_actual(days_back={days_back or 7})` — the key tool. Returns per-day rows with target/actual/delta for kcal + P/C/F, foods_logged count, Garmin adjusted goal, expenditure, net balance. Also returns `plan_source_weekly_snapshot` (which /weekly the plan came from) and `no_plan_available` flag.
2. `get_athlete_baseline` — for weight_kg (protein target math) and current weight goal context.

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

## Recommended focus
{Single sentence. "Hit protein target 3 more days" / "Close 1400 kcal deficit by Sunday" / "Log every meal — only 2/5 days tracked" / "On plan — keep it up."}
```

### Rules

- **Render as chat markdown. No wrapping code block.**
- Use `baseline.weight_kg` for protein target math. Target = weight × 1.6 for maintenance, × 1.8-2.0 for weight-loss phases.
- Flag days where foods_logged=0 in the table rather than showing zeros as actuals — under-logging is the #1 data quality issue.
- If a day's actual kcal < 1200 AND it was a training day, flag as likely under-logged.
- Rest of the week adjustment should be realistic — if the shortfall needs +1500 kcal/day to hit goal, note it's probably not achievable and suggest resetting expectations or extending the timeline.
- Target length: 20-35 lines.
