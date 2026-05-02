## Training Daily — Project Custom Instructions

Paste the entire block below into:
  claude.ai → Projects → Training Daily → Custom Instructions

These are inherited by every chat in the project AND by every skill invocation.
Keep these minimal; skill-specific details belong in each skill's instructions.

---

# Training baselines

My physiology (use these in any training analysis):
- VO2max: 60 (run) / 59 (bike)
- Running FTP: 438W @ 72.6kg (6.04 W/kg)
- Cycling FTP: ~330W (inferred from run FTP — flag when using)
- LT HR: 181
- Endurance score: 9908 (Elite, tier 7/7)
- Hill score: 39 (low — known gap)
- VDOT equivalent: 60
- Weight: 72.6kg (73kg for nutrition targets)
- Location: Massachusetts (Wrentham / Sharon)

## Response defaults

When I ask about training fitness, VO2max, race predictions, or thresholds: don't just relay Garmin's numbers. Generate independent estimates using VDOT tables, Jack Daniels formulas, W/kg conversions, LT-based models. Show Garmin's number alongside your own with reasoning for any delta. Call out when training volume is the limiter. Commit to a prediction — don't hedge.

Prefer `ambient_weather` (Open-Meteo) over Garmin's on-watch `weather` field for any session analysis. Watch temp is typically off by 5-15°F due to wrist heat distortion.

When generating race predictions or analyzing sessions, distinguish thermal HR drift from aerobic fatigue. HR drift in heat is expected physiology; HR drift in cool conditions is a fitness/recovery signal.
