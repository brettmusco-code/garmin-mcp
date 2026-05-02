## Skill config for claude.ai

**Name:** `session-review`

**Description:** Deep post-session analysis — pacing, zones, weather impact, fitness signals, next-session implications

**Parameters:**
- **Name:** `activity_id`
- **Type:** string
- **Required:** no
- **Description:** Garmin activity ID. Leave blank to analyze the most recent activity.

---

## Instructions (paste below into the Instructions field)

Generate a deep analysis of a single training session.

**Data to pull:**
1. If `activity_id` provided: `get_activity_details(activity_id)` directly.
   If not provided: `get_activities(limit=1)` to get the most recent activity, then `get_activity_details` on its `activityId`.
2. `get_activities(limit=20)` — to find similar past sessions for comparison
3. `get_daily_summaries` for the date of the session — recovery state that day

### Output format

```
🔍 SESSION REVIEW — {activity name}
{date, time, sport, duration, distance}

EXECUTION VS INTENT
  • Planned: {from scheduled workout or inferred from name}
  • Executed: {what actually happened}
  • Verdict: {nailed / slight over/under / blown / bailed}

PACING
  • {km or mile splits table if relevant, flag drift}
  • HR drift: {first half avg → second half avg, % drift}
  • Power/pace decoupling: {if endurance ride/run, Pa:HR ratio}

ZONES (actual vs intended)
  • Z1: {min / %}
  • Z2: {min / %}
  • Z3: {min / %}
  • Z4: {min / %}
  • Z5: {min / %}
  • {Flag if zone distribution missed the intent}

KEY METRICS
  • Avg HR / Max HR: {bpm}
  • Avg Power / Normalized Power / IF: {w / w / ratio}
  • TSS: {n}
  • Training Effect: Aerobic {n} / Anaerobic {n}
  • Calories: {n}

CONDITIONS (ambient_weather, not watch-reported)
  • Temp: {avg °F} (apparent {n}°F) · {"cold" / "ideal" / "warm" / "hot"}
  • Humidity: {n}% · Dewpoint: {n}°F
  • Wind: avg {n} mph, gusts to {n} mph
  • Conditions: {clear / cloudy / rain / etc.}
  • Thermal impact on today's metrics: {explain how weather shaped HR/pace}

COMPARISON TO RECENT SIMILAR
  • {Find 2-3 recent sessions of same type, compare}
  • {e.g., "Your last 4 threshold sessions: 268W, 272W, 275W, 278W (today) — clean upward trend"}

WHAT THIS TELLS US
  • {1-2 concrete fitness/fatigue signals}
  • {e.g., "HR drift of 6% over 90min at steady power = good aerobic fitness"}

NEXT SESSION IMPLICATIONS
  • {Recovery time estimate — how long to next hard session}
  • {Any adjustments to the plan this session suggests}
```

### Rules

- Commit to a verdict (nailed / slight miss / blown / bailed). No hedging.
- If no specific planned workout existed, infer intent from activity name + duration + intensity.
- HR drift >5% at steady power = aerobic fatigue signal. **BUT** first check weather — HR drift in heat is thermal, not aerobic fatigue.
- Power/pace decoupling (Pa:HR) >5% at Z2 = under-rested or dehydrated. Flag it.
- Compare to similar past sessions using actual numbers — not "looks good."
- If no comparison activities exist (new session type), say so explicitly.
- Use `ambient_weather` (Open-Meteo historical) not Garmin's watch `weather`. Garmin's on-watch temp is distorted by wrist heat, sun, pavement — typically off by 5-15°F. Always prefer ambient for analysis.
- Target length: 30-40 lines.

### Weather interpretation (apparent temperature = feels-like)

| Apparent °F | Effect on same-effort HR/pace |
|---|---|
| <40 | Cold — paces slow ~5-10s/km, HR may run low until warmed |
| 40-60 | Cool — ideal hard-effort conditions, expect PR-type performance |
| 60-70 | Neutral — baseline, no adjustment |
| 70-78 | Warming — HR +3-5 bpm at same pace |
| 78-85 | Warm — HR +5-10 bpm, pace slows ~3-5% at same RPE |
| 85-92 | Hot — HR +10-15 bpm, pace slows 5-10%, hydration critical |
| >92 | Extreme heat — reduce intensity 10-20%, shorten session |

Humidity modifier: add ~5°F to apparent-temp bucket if humidity >70%. Dewpoint >65°F indicates significant heat stress regardless of temp.

When interpreting a session, **distinguish thermal HR drift from fitness signals**:
- HR drift + cool conditions + steady power → aerobic fatigue or dehydration
- HR drift + warm/hot conditions → expected thermal effect, not fitness issue
- Low HR at expected pace + cold conditions → normal (cardio lag)
- High HR at expected pace + cool conditions → under-recovered
