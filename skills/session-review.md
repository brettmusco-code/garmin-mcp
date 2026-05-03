**Name:** `session-review`

**Description:** Deep post-session analysis — pacing, zones, weather impact, fitness signals, next-session implications

**Parameters:**
- **Name:** `activity_id`
- **Type:** string
- **Required:** no
- **Description:** Garmin activity ID. Leave blank to analyze the most recent activity.

---

## Instructions (paste below into the Instructions field)

Deep analysis of one training session. Depth on signals, not on restating metrics.

**Data to pull:**
1. `get_athlete_baseline` — pre-computed nightly (~300ms). Use `multi_method.{run_lt_hr|run_ftp|bike_ftp|swim_css}.consensus` and `.confidence_interval_80pct` for zone/target comparisons.
2. If `activity_id` provided: `get_activity_details(activity_id)` directly.
   If blank: `get_activities(limit=1)` → get its `activityId` → then `get_activity_details`.
3. `get_activities(limit=20)` — for similar-session comparison.
4. `get_daily_summaries` for the session date — recovery state that day.

**If `get_athlete_baseline` returns `{"error": "..."}`:** Note "⚠️ Baseline cache empty" at the top and skip the CI-band comparison in the Signals section. Still complete the session analysis from activity details + comparison sessions.

**Output format** — use markdown directly. **Do NOT wrap the response in triple-backticks or code blocks.** Chat output, not a document.

### Format

```
🔍 {activity name} — {date, sport, duration, distance}

## Execution
- Planned: {from scheduled or inferred}
- Executed: {what actually happened}
- **Verdict: {nailed / slight miss / blown / bailed}** — {1-line why}

## Pacing & Zones
- {Splits narrative if relevant — e.g., "4:01 / 4:03 / 3:58 / 3:55 — negative split, held form"}
- HR drift: {first half → second half, % drift} — {thermal vs fatigue interpretation}
- Zone distribution: {Z1/Z2/Z3/Z4/Z5 split in one line, flag if off-intent}

## Key Metrics
Avg HR {bpm} / max {bpm} · Pwr {W avg, NP, IF if ride} · TSS {n} · TE aerobic {n} / anaerobic {n}

## Conditions
{If ambient_weather.skipped=true: "Indoor session — no weather context." and skip the rest of this section.}
{If outdoor: temp °F, apparent °F, humidity %, wind mph, clear/cloudy/rain. Add a 1-sentence thermal impact note on HR/pace.}

## vs Recent Similar
{2-3 sessions of same type with actual numbers — "last 4 thresholds: 268W, 272W, 275W, 278W today — clean upward"}. If new session type, say so.

## Signals
- {1-2 concrete fitness or fatigue signals with numbers}
- {If session was at/above threshold, compare observed HR/power to multi_method.{run_vo2max|run_lt_hr|run_ftp|bike_ftp}.consensus and confidence_interval_80pct. Note whether observed is inside/above/below the CI band.}
- {If multi_method.*.flag is non-null AND this session contributed to the disagreement, call that out specifically.}

## Next
Recovery: {h hours to next hard session}. {Plan adjustment if any.}
```

### Rules

- **Render as chat with markdown. No wrapping code block.**
- Commit to a verdict. No hedging.
- HR drift >5% at steady effort = flag — but check weather first. Drift in heat = thermal, not fitness loss.
- Pa:HR decoupling >5% at Z2 = under-rested/dehydrated.
- Use `ambient_weather` (Open-Meteo), not Garmin's watch weather. Watch is off by 5-15°F.
- Compare to similar past with actual numbers.
- Target length: 20-25 lines. Every line earns its spot.

### Weather interpretation

Apparent temp effect on same-effort HR/pace:
- <40°F: paces slow 5-10s/km, HR low until warmed
- 40-60°F: ideal hard-effort conditions
- 60-70°F: neutral baseline
- 70-78°F: HR +3-5 bpm at same pace
- 78-85°F: HR +5-10 bpm, pace slows 3-5% at same RPE
- 85-92°F: HR +10-15 bpm, pace slows 5-10%, hydration critical
- \>92°F: reduce intensity 10-20%

Humidity >70% adds ~5°F to the apparent bucket. Dewpoint >65°F = significant heat stress regardless of temp.

**Distinguish thermal drift from fitness:**
- Drift + cool + steady power → fatigue/dehydration
- Drift + warm/hot → thermal, expected
- High HR at expected pace + cool → under-recovered
- Low HR at expected pace + cold → normal cardio lag
