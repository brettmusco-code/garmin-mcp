**Name:** `fuel`

**Description:** Forward fueling plan — daily calories & macros plus per-workout fuel (pre / during / post + hydration) for your upcoming scheduled workouts, driven by your stored weight goal and current body stats.

**Parameters:**
- **Name:** `days`
- **Type:** number
- **Required:** no
- **Description:** Planning horizon in days ahead, 1-28. Default 7.

---

## Instructions (paste below into the Instructions field)

On-demand, **forward-looking** fueling plan. Where `/weekly` reviews the past week and saves the plan, and `/nutrition` checks plan-vs-actual mid-week, `/fuel` answers a single question: *"For each of my next N scheduled workouts, exactly what should I eat that day and how should I fuel that session?"* Its distinguishing output is a **per-workout fuel card** (pre/during/post grams + hydration) for the sessions that actually need one.

The math runs server-side in the MCP tool `generate_fueling_plan`, so this skill mostly orchestrates and renders. The tool mirrors the formulas in `skills/weekly.md` + `skills/project-instructions.md` (Mifflin-St Jeor BMR, the capped-deficit weight-goal policy, carb periodization 3–8 g/kg, fueling windows), calibrating each session's calorie burn from your own 90-day history.

Related tools: **`get_adaptive_tdee`** (measured maintenance from intake vs weight change), **`get_race_fueling`** (race-day calculator), **`push_nutrition_targets_to_garmin`** (write goals into the Garmin app). The plan output also carries a **`projection`** (forward weight curve + finish date), a per-day **`meals[]`** split, **`energy_base`** (bmr_x1.3 or adaptive_tdee), and heat-aware / swim-aware fuel cards. Swims never get pre/during fuel (post only); outdoor sessions get a fluid/sodium bump on hot days (needs `home_lat`/`home_lon` on the goal, or a `heat_c` override).

### Flow

0. **Start today** — leave `start_date` unset (defaults to today) so the plan includes today and the next 6 days; today's logged intake shows in `today_actuals` alongside today's target.

1. **`get_fueling_goal`** — pull the stored goal + live progress.
   - **If `goal` is null:** onboard first. Ask for: goal_type (lose / gain / maintain), and for lose/gain the `target_weight_kg` + `target_date`. Also ask sex / `height_cm` / age once (Garmin doesn't expose them — without them BMR falls back to weight × 22). Then call **`set_fueling_goal`** with those and continue.
   - **If `progress.review_flags` is non-empty** (goal >4 weeks old, target date passed, target already hit, weight not logged >14 days): surface the flag and ask me to confirm or update the goal before planning. Don't silently plan against a stale goal.

2. **`generate_fueling_plan(days = {days or 7}, rebalance = true)`** — the engine. `rebalance=true` self-corrects from the week so far: the net intake error on logged days (vs their expenditure-adjusted targets) is spread across this window, so over-eating Tuesday tightens Wed–Sun and a banked deficit loosens them. Returns everything needed:
   - `goal`, `goal_progress` (current vs target weight, `weeks_remaining`, `required_daily_kcal_change`, `kg_to_target`, `pace_flag`)
   - `body` (weight, `body_fat_pct`, `lean_mass_kg`, `muscle_mass_kg`, `staleness_days` — from Renpho→Garmin), `fat_free_mass_kg`
   - `bmr` (`value`, `source`), `daily_kcal_adjustment`, `protein_g_per_kg` (base)
   - `days[]` — per day: `sessions[]` (each with `kcal_per_hour` + `burn_source`), `primary_intensity`, `est_burn_kcal`, `target_kcal`, **`target_deficit_kcal`** (expenditure − target; >0 = deficit), `protein_g` / `carbs_g` / `fat_g`, **`protein_g_per_kg`** (that day's periodized value), `carb_g_per_kg`, `energy_availability_kcal_per_kg_ffm`, `needs_fuel`, and `fuel[]` cards
   - `config` — the resolved knobs: `deficit_cap_kcal` (null = uncapped), `max_loss_lb_per_week`, `ea_floor_kcal_per_kg_ffm` (warning), `ea_min_kcal_per_kg_ffm` (**enforced** EA floor: each day's target ≥ ea_min × FFM + that day's burn, so the floor scales with training), `min_kcal` (absolute daily floor), `fuel_min_minutes`, `bmr_floor_mult` (null = floor dropped), `periodize_deficit`, `front_load`, `skip_breakfast_weekdays`
   - `today_actuals` — today's logged intake + the actual foods eaten (falls back to the most recent logged day, with `is_today`); `recent_days` — the last 2 days' consumed vs planned vs burned. Surface both, and if logging is sparse, say so — it's the main thing blocking adaptive TDEE. Each day also carries a `meals[]` split that reconciles with the day's targets and fuel: on fueled days a single **`Workout fuel (pre/during/post)`** line carries exactly the carbs/protein the fuel cards prescribe (pre + during + post, summed across every fueled session), so the meal plan and the fuel timeline agree. The remaining energy is weighted toward lunch/dinner (**not** spread evenly) with a hard **breakfast cap (~650 kcal)** so a big training day doesn't produce a 1,500-kcal breakfast. If `skip_breakfast_weekdays` is on, weekday plans drop the Breakfast meal (weekends keep it) and shift its calories later; each day carries `skip_breakfast`. Session duration comes from the calendar/workout estimate (`estimatedDurationInSecs`, which TrainingPeaks→Garmin fills for every structured workout), then the workout's own **step durations** (repeat groups × iterations), then a **distance ÷ pace** estimate, then the title, then a type default — so a 20-min brick run reads as 20 min and a 3h38m ride as 3.63 h, not a flat 1 h; `sessions[].hours_source` tells you which. During-carb rates are **training** numbers (25–45 g/hr); 60–90 g/hr is a race target — use `get_race_fueling` for events.
   - **Deficit periodization** (default for lose goals): the weekly deficit is banked on rest/easy days while tempo/threshold/VO2/long days never take a deeper cut than the flat per-day amount — each day reports its own `kcal_adjustment`. `periodize_deficit=false` restores a flat daily deficit. When floors bind and part of the weekly deficit can't be absorbed, a shortfall note fires — surface it.
   - `totals` (incl. `target_deficit_kcal`), `notes[]` (low-EA / RED-S warning fires below the configured floor)
   - Guard flags: `no_goal_available`, `error` (e.g. `no_weight`).
   - **How macros work** (explain if asked): protein = bodyweight × a per-kg factor — deliberately near-constant because protein need tracks lean mass, not energy — nudged up +0.2 g/kg on threshold/VO2/long days, +0.1 on tempo, +0.1 in a steep (≥500) deficit; carbs = bodyweight × a factor periodized by session type (3→8 g/kg), and carbs are the **flex** macro that absorbs the extra on a big training day; fat fills the gap but is **capped at ~30% of calories** (`carbs_trimmed` flips only on the opposite case — a deficit so steep the target can't hold the training carbs). Session burn is calibrated from the athlete's own history — same sport **and** similar duration first (`burn_source: history_similar`), then sport median, then a generic table.
   - Fuel cards are emitted only for sessions **≥ `fuel_min_minutes` (default 90)**.
   - Pass `carb_load=true` for race week — suspends the deficit and raises carbs to ~9 g/kg. Config overrides (all optional): `max_deficit_kcal` (0 removes the cap), `ea_floor` (0 disables the warning), `ea_min` (enforced EA floor — the best guardrail when cutting hard, since it protects training days automatically), `min_kcal` (absolute floor), `bmr_floor_mult` (0 drops the BMR floor), `periodize_deficit`, `front_load` (0–0.9: steeper deficit early, tapering as weight nears target — recomputed from each weigh-in so it self-tapers), `fuel_min_minutes`, `skip_breakfast_weekdays` (time-restricted eating: drop the weekday breakfast, keep weekends). These also persist on the goal via `set_fueling_goal`. When the goal pace exceeds what the floors allow, the shortfall note projects the realistic landing date — always surface it.

3. **Render** using the format below. Fold every entry in `notes[]` into the Notes section verbatim (they carry horizon/BMR/deficit/pacing caveats). If `error` = `no_weight`, tell me to log a weigh-in (or pass `start_weight_kg` when setting the goal) and stop.

4. **Offer to persist:** after showing the plan, ask *"Save this as the week's plan so `/nutrition` and `/morning` track adherence?"* If yes, re-call `generate_fueling_plan(days, save=true)` — it merges the per-day targets into the weekly snapshot's `nutrition_plan` without touching other snapshot fields.

4b. **Race week:** if I name a target event, call `get_race_fueling(sport, duration_hours, ...)` and present the pre-race meal, carb-load days, and hour-by-hour carbs/fluid/sodium/caffeine + gel count. Combine with `generate_fueling_plan(carb_load=true)` for the loading days.

5. **Optional Garmin push:** if I ask to see targets in the Garmin Connect app, call `push_nutrition_targets_to_garmin` (EXPERIMENTAL — only works from the live/cron env, not the read-only web MCP; the nightly cron can do it automatically with `FUEL_PUSH_GARMIN=true`). Garmin then applies its own activity adjustment on top of the pushed base goal — in-app auto-adjust after each workout. Report the per-endpoint diagnostics honestly if it fails. Independently of the push, adherence tracking always auto-adjusts: `/nutrition` and `/morning` compare intake to `adjusted_target_kcal`, and `rebalance=true` folds the error into the next plan.

6. **Optional visual dashboard:** if I ask for a dashboard / visual / shareable page, render one as an Artifact from the repo template `web/fuel-dashboard.html` — replace the object assigned to `const PLAN` in its `<script>` with the exact `generate_fueling_plan` JSON, and publish. The template is self-contained, theme-aware, and renders the goal header, today card, week table, energy-availability panel, weight-to-target bar, per-workout fuel timeline, and flags with no code changes needed.

**Output format** — markdown headings directly. **Do NOT wrap in triple-backticks or code blocks.**

### Format

```
⛽ FUELING PLAN — {Mon DD} → {Mon DD} ({N} days)

Goal: {goal_type} to {target_weight_kg}kg by {target_date} · {weeks_remaining} wk left · {daily_kcal_adjustment:+d} kcal/day
Body: {weight_kg}kg{ · body fat {body_fat_pct}% · lean {lean_mass_kg}kg}{  ⚠️ weight {staleness_days}d old if >7}
Pace: {required_daily_kcal_change and kg_to_target → "need −N kcal/day to lose Xkg in time"}. {pace_flag if present}
BMR {bmr.value} ({"measured" if source=mifflin_st_jeor else "est weight×22 — add sex/height/age"})

## Daily targets

| Day | Session | Est burn | Target | Deficit | P / C / F (g) | Carb g/kg | EA | Fuel |
|---|---|---|---|---|---|---|---|---|
| {Tue} | {sessions or "rest"} | {est_burn_kcal} | {target_kcal} | {target_deficit_kcal} | {p}/{c}/{f} | {carb_g_per_kg} | {ea} | {⛽ if needs_fuel else —} |
| ... | | | | | | | | |

## Per-workout fuel  (only ⛽ days, sessions ≥90 min)

**{Day} — {session} ({hours}h, {intensity})**
- Pre (60–90m before): {pre_carbs_g}g carbs — {name 1–2 real foods}{ + {caffeine_mg}mg caffeine}
- During ({hours}h): {during_carbs_g_per_hr}g/hr → ~{during_carbs_g_total}g; fluid {fluid_ml_per_hr}ml/hr, sodium {sodium_mg_per_hr}mg/hr{ · {note}}
- Post (within 60m): {post_protein_g}g protein + {post_carbs_g}g carbs — {example}

{repeat per fuel card}

## Notes
- {every notes[] entry}
- {goal pacing read; under-fueling / stale-weight flags}
```

### Rules

- **Render as chat markdown. No wrapping code block.**
- The tool owns the numbers — don't recompute targets/macros by hand; render what `generate_fueling_plan` returns. You only add real-food examples to the fuel cards and the prose reads.
- Only show a per-workout fuel card for days where `needs_fuel` is true (the tool applies the ≥90-min `fuel_min_minutes` filter).
- If `config.deficit_cap_kcal` is null (cap removed), the EA floor was lowered/disabled, or `bmr_floor_mult` is null (floor dropped), say so plainly — and always surface the below-BMR, macro-conflict, and shortfall notes verbatim. Removed guardrails don't make a steep deficit safe; the numbers still have to tell the truth.
- Never invent a goal. If none is set, onboard via `set_fueling_goal` first; if `review_flags` fire, confirm before planning.
- Surface `pace_flag` honestly — if the timeline needs more than the 500 kcal/day cap, say the target date is aggressive and suggest extending it rather than implying an unsafe deficit is fine.
- If `bmr.source` is `weight_x22_fallback`, add one line inviting me to set sex/height/age for a precise BMR.
- Body-fat / lean-mass come from Renpho→Garmin; if absent, just omit those fields (don't fabricate).
- Target length: ~30–50 lines (scales with horizon).
