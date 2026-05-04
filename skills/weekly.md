**Name:** `weekly`

**Description:** Sunday review — execution, what changed, fitness trajectory, race countdown, nutrition review & plan, HRV-guided readiness forecast

**Parameters:** none

---

## Instructions (paste below into the Instructions field)

Weekly training review. Denser than `/morning` — weekly patterns need a fuller picture — but still chat-formatted, not padded.

**Data to pull in parallel:**
1. `get_athlete_baseline` — pre-computed nightly (~300ms). Current + prior-week snapshots for delta comparison. Includes 90-day per-sport fitness trends, multi-method thresholds with CI + flags, key_session_counts, race predictions, fitness_drift, staleness.
2. `get_activities` — this week (today - 7 → today) AND last week (today - 14 → today - 7). Also prior 3 weeks (today - 28 → today - 7) for trend context.
3. `get_daily_summaries` — last 14 days with `[sleep, hrv, rhr, training_readiness, stats_and_body, stress, nutrition_food_log, nutrition_meals, body_battery_events]`. 14 days so we can compare this week's nutrition/sleep/HRV averages to prior week.
4. `get_scheduled_workouts` — today → today + 7.
5. `get_weekly_summaries` — 4 weeks for `[intensity_minutes, stress, steps]`.
6. `analyze_training_period` — this week's totals.
7. For key sessions (top 3 hardest this week): `get_activity_details(activityId)` for `ambient_weather` + full splits.

**Prior-weekly reference:** Call `get_weekly_snapshots(weeks_back=1)` to retrieve last week's snapshot automatically from R2. If the list is non-empty, use snapshots[0] for the "WHAT CHANGED" deltas. If empty, note "first weekly run — trajectory starts from here."

**Race target:** Check project memory for current race target (event, date, distance). If none, ask what I'm training for. If set, compute weeks-remaining and use for the RACE COUNTDOWN section.

**If `get_athlete_baseline` returns `{"error": "..."}`:** Note "⚠️ Baseline cache empty" at top, complete what you can from activities + daily summaries, skip baseline-derived sections.

**Output format** — markdown headings directly. **Do NOT wrap in triple-backticks or code blocks.**

### Format

```
📊 WEEK IN REVIEW — {Mon DD} to {Sun DD}

## Training Week

**Volume** (miles — convert from Garmin meters: m ÷ 1609.344; swim stays in meters for context)

| Sport | This week | Last week | Δ | Sessions |
|---|---|---|---|---|
| Bike | {mi} | {mi} | {±%} | {n} |
| Run | {mi} | {mi} | {±%} | {n} |
| Swim | {m} | {m} | {±%} | {n} |
| Strength | — | — | — | {n} vs {n} |
| **Total hours** | {h}h | {h}h | {±%} | — |

**Intensity mix**

| Zone | Hours | % of total |
|---|---|---|
| Z1-Z2 (base) | {h} | {%} |
| Z3 (tempo) | {h} | {%} |
| Z4-Z5 (threshold+) | {h} | {%} |
| Anaerobic TE | — | {sum} |

Read: {base-heavy / tempo-heavy / VO2-heavy / spiky}

**Load & Form**

| Metric | Value | Prior week | Interpretation |
|---|---|---|---|
| Acute load (7d) | {n} | {n} | {±%} |
| ACWR | {ratio} | — | {sweet-spot 0.8-1.3 / risk zone} |
| Ramp rate | {±%}/wk | — | {safe <10% / risk >15%} |
| CTL (42d fitness) | {n} | {n} | {±} |
| ATL (7d fatigue) | {n} | {n} | {±} |
| TSB (freshness) | {n} | {n} | {fresh >+5 / neutral -10 to +5 / overloaded <-20 / high-risk <-30} |

{1-line interpretation of the form picture — fresh enough for key session? Or dig-out-of-fatigue week?}

## Execution
**Key sessions:**
- {Session 1 — planned vs executed verdict + 1-line why + ambient temp if outside 50-70°F}
- {Session 2 — same}
- {Session 3 — same}

- **Adherence:** {planned N sessions → executed M. Note any missed}.
- **Worked:** {2 specific observations}
- **Drifted:** {2 specific observations}

## What Changed vs Last Week
- {Delta 1: concrete number from prior /weekly snapshot. e.g., "Bike FTP consensus 323W → 325W (+2W)"}
- {Delta 2: same}
- {Delta 3: same}
- {If first run: "No prior snapshot — this week becomes the baseline."}

## 📈 Fitness Trajectory (4 weeks)
- Per-sport key metrics with 4-week prev → curr Δ:
  - **Run:** VDOT {prev → curr} ({±}), weekly miles avg {prev → curr}
  - **Bike:** FTP consensus {prev → curr} ({±W}), EF drift {±%}, best 20-min peak {prev → curr}
  - **Swim:** CSS {prev → curr} ({±s/100m}), best 1000m {prev → curr}
- **Race predictions:** 5K {prev→curr}, 10K {prev→curr}, half {prev→curr}, mar {prev→curr}
- **Multi-method flags:** (if any from `multi_method.*.flag`)
- 1-line verdict on trajectory.

## 🏁 Race Countdown — {Event Name} ({target distance}, {YYYY-MM-DD})
{If no race target in project memory: "No race target set. What are you training for?" — skip the rest of this section.}

- {N} weeks out.
- Current prediction from baseline: {time}. Goal time (from memory): {time}. Gap: {±Xs}.
- **Race-specific fitness needed:** {based on distance — for a half-mar: threshold pace + 90min endurance; for a half-IM: bike FTP + run 90min off bike; etc.}
- **What this week's training contributed to the goal:** {concrete}.
- **Next week's priority for race prep:** {single focus}.
- **Taper start:** {date — typically 2-3 weeks out for half, 3 weeks for marathon/half-IM}.

## 🔋 HRV-Guided Readiness Forecast
- **Avg HRV:** {n} (prior week avg: {n}, trajectory {±}).
- **Baseline weekly HRV:** {from baseline context or 30d avg}.
- **Key session prediction:** {hardest scheduled workout this week, and its projected readiness}. Based on HRV trajectory + last 2 nights' deep sleep + recovery time drift. "Project readiness {n}/100 by {day}. GO if it lands ≥60 AND HRV holds ≥{n}, MODIFY if 40-60, SWAP if <40."
- **Sleep-training coupling:** {correlation or note — "sleep duration dropped with load rise this week" is actionable}.

## 🍽️ Nutrition — Last Week Review + Next Week Plan

**Review of last 7 days** — read from `nutrition_food_log` at these exact paths:
- **Consumed:** `nutrition_food_log[date].dailyNutritionContent` → `.calories`, `.protein`, `.carbs`, `.fat` (all grams except kcal)
- **Garmin target:** `nutrition_food_log[date].dailyNutritionGoals.adjustedCalories` (adjusted for that day's activity)
- **Foods logged count:** `len(nutrition_food_log[date].loggedFoodsWithServingSizes)` — if 0, treat the day as unlogged even if dailyNutritionContent has zero values

For each day in the last 7: extract consumed kcal/P/C/F. Days with 0 foods logged OR missing dailyNutritionContent → flag as "not logged."

Report as a table:

| Date | Foods | Consumed kcal | Goal kcal | P / C / F (g) | Delta |
|---|---|---|---|---|---|
| Mon | {n} | {n or "—"} | {goal} | {P/C/F} | {consumed − goal} |
| ... | | | | | |

Then synthesize:
- **Days logged:** {n/7}. If <4, flag and note the plan relies on assumed intake for the remaining days.
- **Avg daily intake (logged days only):** {kcal} · P {g} / C {g} / F {g}
- **Avg daily expenditure:** {BMR + session kcal from activities}. BMR estimate: Mifflin-St Jeor (weight_kg × 10 + height_cm × 6.25 − age × 5 + 5 for male / −161 for female). If height/age unknown, use a default of BMR ≈ weight_kg × 22 for endurance athletes.
- **Avg daily delta:** {intake − expenditure}. Weekly total delta: {sum}.
- **Protein target hit:** {M/N logged days at ≥1.6 g/kg of baseline.weight_kg}.
- **Carb adequacy on hard days:** for each day with a hard session, did carbs meet 5-7 g/kg? List any that fell short.
- **Goal alignment:** from project memory weight goal. If "lose 2kg by DATE": required weekly deficit = (2 × 7700 / weeks_remaining). Compare to actual weekly delta. Report as: "Required: -N kcal/wk. Actual: -N kcal/wk. Off-pace by N — need +X kcal/day deficit OR +Y min extra Z2."
- **Under-logging suspicion:** if a day shows consumed kcal < 1200 AND ≥1hr of hard training that day, flag as "likely under-logged — missed meals/snacks." Don't let those days bias the avg downward.

**Next 7 days:**
Check project instructions for my current weight goal. If the goal is older than 4 weeks, appears met/expired, or target date passed, ask me to confirm before using it. If no goal is set, ask.

**Predict session kcal from MY history, not generic lookups:**
For each scheduled workout this week: filter `get_activities` (last 90d) to matches by `activityName`, `workoutId`, or `activityType.typeKey` + duration (±20%). Use median kcal/hr from ≥3 matches. Multiply by planned duration. If <3 matches, note estimate is low-confidence and use: recovery ride ~500 kcal/h, Z2 ride ~650 kcal/h, Z2 run ~700 kcal/h, threshold/VO2 ~900-1000 kcal/h, long ride ~550-700 kcal/h.

| Day | Session | Est burn (kcal) | Target intake | P / C / F (g) | Notes |
|---|---|---|---|---|---|
| Mon | {workout} | {from history, with "(median of N)" or "(est)" suffix} | {BMR×1.3 + burn + goal adj} | {weight_kg × 1.7-2.0} / {weight_kg × C} / {25% kcal / 9} | {fueling timing if hard day} |
| ... | ... | ... | ... | ... | ... |

Carb ratio C: 3 g/kg rest · 4 g/kg easy · 5-6 g/kg tempo/SST · 7-8 g/kg threshold/VO2 or ride >2h.

**Weekly totals:** {sum target intake · sum protein · sum carbs} vs goal-required sum. Flag if off by >10%.


**Fueling windows** (any session ≥75min or ≥Z3): pre 40-60g carbs 60-90min prior; during 30-60g carbs/hr; post 20-30g protein + 50-70g carb within 60min.

## 🎯 This Week's Priority
{Single sentence. "Base-build" / "threshold push" / "deload" / "race-week taper". Commit.}

## 💾 Snapshot saved
Call `save_weekly_snapshot` with this object (stored in R2, auto-retrieved by next week's /weekly — no manual paste needed):

```
{
  "date": "YYYY-MM-DD",   // Monday of the upcoming week (plan starts here)
  "bike_ftp_consensus": N,
  "run_vdot": N,
  "css_sec_per_100m": N,
  "weekly_miles": {"bike": N, "run": N, "swim_meters": N},
  "ctl": N, "atl": N, "tsb": N,
  "race_predictions": {"5k_sec": N, "10k_sec": N, "half_sec": N, "mar_sec": N},
  "hrv_avg": N,
  "avg_daily_kcal_intake": N,
  "avg_daily_kcal_expenditure": N,
  "weekly_kcal_delta": N,
  "protein_target_hit_days": N,
  "days_logged": N,
  "key_session_counts": {"run_key": N, "bike_key": N, "swim_key": N},
  "priority": "base-build | threshold push | deload | race taper",
  "nutrition_plan": {
    // one entry PER DAY of the upcoming week, keyed by YYYY-MM-DD.
    // These targets are what nutrition_plan_vs_actual will compare
    // against actual food-log entries during the week.
    "2026-05-05": {
      "session": "ROUVY Sweet Spot Builder 90min",
      "target_kcal": N, "protein_g": N, "carbs_g": N, "fat_g": N,
      "notes": "hard day — fuel pre/during/post"
    },
    "2026-05-06": { ... }
    // ... Mon–Sun
  }
}
```

After successful save, acknowledge inline: "Snapshot saved for {date}. Next Sunday's /weekly will auto-compute deltas from this."
```

### Rules

- **Render as chat markdown. No wrapping code block.**
- Volumes in **miles**, round to 0.1 (bike + run). Swim stays in meters — swimmers don't think in miles. Convert Garmin's meters: miles = m / 1609.344.
- Flag ACWR outside 0.8-1.3 with specific intervention.
- Next-week load from scheduled workout types: recovery=20 TSS, base=60, threshold=90, VO2=100, long=120+.
- Nutrition reflects baseline.weight_kg + current weight goal from project memory. Never hardcode weight.
- If nutrition data missing (<4 days logged), flag; don't fabricate.
- **Never use Garmin's watch-reported ambient temperature.** Use only `ambient_weather.*` fields. If `ambient_weather.skipped=true`, omit temperature.
- **Units check:** Garmin `recoveryTime` is in MINUTES. Divide by 60 for hours.
- Trajectory comparison uses actual numbers vs prior week (from project memory snapshot) OR vs 4 weeks ago. Never vague "improving."
- Commit to a weekly priority — don't hedge.
- Target length: 50-70 lines. Dense but readable.

### CTL / ATL / TSB calculation (if Garmin doesn't expose directly)

- Training Load (TL) per activity = `activityTrainingLoad` (or `trainingStressScore` for rides).
- CTL = exponentially-weighted 42-day average of daily TL. Approximate: 42d simple avg if needed.
- ATL = exponentially-weighted 7-day average of daily TL. Approximate: 7d simple avg.
- TSB = CTL - ATL. Interpretation: TSB > +5 = fresh/peaked, -10 to +5 = neutral, < -20 = overloaded/fatigued, < -30 = high injury/illness risk.
