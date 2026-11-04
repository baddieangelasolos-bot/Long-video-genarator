# Workflow rule changes (v2) - paste over the matching parts of the project blueprint

## CHANGED: wide shots are now allowed (a few)
Old rule removed: "reject extreme wide-angle tactical shots where the player is tiny".
New rule: wide tactical shots MAY be used, at most ~15-25% of body beats (reference ~25%).
A wide shot qualifies only if it is a continuous ball-tracking shot of the target player that ends in a shot, goal or assist.
Keep it unzoomed (zoom base 1.0), 8-11 beats, speed 1.0. Still banned: blur, referee/bench/foul/injury, the player losing the ball,
goal-net mesh covering the view, hidden internal cuts (check with exact scan AND the 0.5 s verification sheet; high-motion shots hide cuts).

## NEW: reference style adaptation (mandatory step before planning)
1. python3 analyze_reference.py REF.mp4          -> style_profile.json (shot lengths, flashes, effect bursts, grade, own tempo)
2. python3 measure_grade.py REF.mp4 20 110        -> letterbox bars + grade stats of the ACTIVE picture area (bars excluded)
3. View the reference at full resolution (not only contact sheets) to read letterbox, text, overlays, shot mix.
4. Encode what was measured, never assumed:
   - Letterbox: bars on every clip (reference: 5% each, 55 px at 1080) until the end-card clip, which is full-frame.
     (Earlier blueprint said "bars only in the intro" - WRONG.)
   - Grade: run grade_fit.py after the EDL exists; it fits eq + vignette per source to the reference's measured
     saturation / luma / contrast / vignette. Do not use fixed eq/vignette values.
   - Pacing: real shots (median ~4 s ~ 9 beats at 138 BPM). Sub-0.5 s "cuts" in the reference are effect bursts.
   - Speed: real-time by default. Slow-mo only to reach the 3 s minimum or for hero dribbles (>=0.8; 0.6-0.75 only on 60 fps).
   - Effects: ~7 flashes/min (white / light-leak flash, RGB glitch, zoom punch); a short burst of fast light-leak transitions ~2/3 through.
   - Title: player name tracked caps + small club line fading in at ~2 s. End card full-frame.
5. Shot mix target: ~50% close, ~25% medium, ~15-25% wide.
6. Report to the user which measured values were applied and which could not be matched.

## Measured on the supplied reference (Lamine Yamal edit, 2:12, 720p)
Bars 5% top/bottom until ~2:00. Saturation 0.51, luma mean 81, contrast (std) 42, p95 luma 197, vignette ratio 0.91.
Real shot median ~4 s (2.4-8.5 s). 16 flash events (7.3/min). Slow-motion share small. Body mix ~50/25/25 close/medium/wide.
Its own music is ~88 BPM and its cuts are NOT tightly beat-locked (13% within 40 ms); our beat-locking to the supplied music is kept.
