# Master template: architecture brief and usage

## What each file does
| File | Role |
|---|---|
| `config/master_rendering_template.json` | Permanent rulebook + schema + measured pacing from the reference edit. The only place numbers live. |
| `scripts/timeline_core.py` | The sandbox: probes media, computes section budgets, validates, repairs. Shared by planner and renderer. |
| `scripts/script_planner.py` | Gemini fills the timeline (10-model chain). Every answer is repaired + validated. Offline planner if all fail. |
| `scripts/classify_shots.py` | Gemini labels the pipeline's contact-sheet rows with the template's shot types and writes `analysis/shot_catalog.json`. |
| `scripts/comp_rules.py`, `catalog_tools.py`, `dup_scan.py`, `offline_planner.py`, `plan_report.py` | v3: composition rules, balanced prompt sample, repeated-footage scan, no-AI planner, footage and plan report mail. |
| `scripts/render_video.py` | Validates again, renders 1920x1080 / 60 fps / CRF 18 / slow, optional beat snap, verifies the MP4. |

## Run locally
```bash
pip install -r requirements.txt            # plus: apt install ffmpeg
export GEMINI_API_KEY=...                   # never commit it
python scripts/script_planner.py --sources-dir work/src --music work/music.m4a \
       [--catalog work/shot_catalog.json] --out work/timeline.json
python scripts/render_video.py  --sources-dir work/src --music work/music.m4a \
       --timeline work/timeline.json --out work/final.mp4 [--beat-snap] [--no-letterbox]
python -m unittest discover -s tests -p test_pipeline.py        # 39 tests, Gemini mocked (needs librosa for the beat-snap test)
python -m unittest discover -s tests -p test_repair_regression.py   # 5
python -m unittest discover -s tests -p test_v3_rules.py            # 14
```
Exit codes (both scripts): `0` ok, `2` setup/input error, `3` needs more source video (planner), `4` no valid plan / render failed.

## How it runs in the normal "Run workflow" (wired into `autonomy/run_pipeline.py`)
The master path is ON by default when `config/master_rendering_template.json` exists. `MASTER_RENDER=0` restores the old path.
```
prepare > fetch > inventory > identify > scan > plan > master_plan > master_render > qa > verify_output > export
```
- `plan`: `shots.py` + `shots2.py` (cuts and contact sheets), then `classify_shots.py` (Gemini labels, repo router with real model ids).
- `master_plan`: `script_planner.py` (10-model chain, validated, offline fallback). Exit 3 becomes a HALTED mail "NEEDS MORE SOURCE VIDEO".
- `master_render`: `render_video.py --no-beats`, writes `build/<Player>_Highlights.mp4` and `pipeline/edl.json` (for `qa.py`); config fps is set to 60.
- Skipped in master mode: beat grid, style, style_ai, known-good replay, edl, watermark, cards, grade, old render, assemble.
- The old `_guard` min-clip / wide-share checks are replaced by the template validator (the owner template allows a 0.6 s opener).
- `ref.mp4` is never used as footage. Encoder overrides are `LVG_RENDER_PRESET` / `LVG_RENDER_CRF` only (tests); the workflow's `RENDER_PRESET` is ignored here.
- Not in the new renderer: logo masking and per-source colour grading. No text is ever drawn.

- If Gemini labelled fewer than 90% of the shots and the labelled plan cannot be built, the run continues with a content-blind plan and the mail says so, instead of falsely asking for more footage.
- The render step logs `clip n/N done` lines, so the Actions log never looks frozen.

## Shot catalog
Without labels the planner is **content-blind**: structure and pacing follow the template but scene choice is random.
`classify_shots.py` writes `[{"source_video_file","start","end","shot_type","quality"}]` (quality 2 or better since v3, contiguous
same-type rows merged). If Gemini is unreachable the run continues content-blind and the mail says so.

## Security
Read-only token, `GEMINI_API_KEY` only in the pipeline step, owner-only job, public repo never uploads the MP4 or raw logs.

## v3 rules (skills lead, intro scenes, celebration after goals, no repeats, report)
- **Skills lead main**: `skill_types` (close_skill, semi_close, duel_1v1, run_making, ball_receive_pass, good_pass_chance) must fill at least `skill_share_min_of_main` = 0.5 of main (target about 65 percent). Goals never fill main. Checked by `comp_rules` (C-SKILL); an error for Gemini plans only when the footage allows it, a warning for the offline planner.
- **Intro scenes**: `intro_scene_types` (bus_arrival, player_exit_bus_or_tunnel, dressing_room) are used before fans, celebration or training; the intro opens with the bus when bus footage exists (C-INTRO). Quality 2 (phone footage) is accepted for these.
- **Celebration in main**: allowed only directly after its goal or penalty_goal, same source, within 25 s, 1.2 to 3.0 s, at most one per goal (E-CELEB). Repair drops any other main-part celebration. A goal with no celebration after it gets none.
- **Goals** are built from 2 to 3 short parts in order from one segment, then the optional celebration (M15).
- **No repeated footage** (G7, E-DUP): `dup_scan.py` compares 256-bit frame hashes (2 per second, written by `fullscan.py` as `hash_<src>.npz`) across and inside files. The first copy is kept; later copies go to `analysis/dup_ranges.json` and are cut out of the catalog. Thresholds: `MAX_BITS`, `MIN_RUN`, `SAME_FILE_GAP_S` in `scripts/dup_scan.py`. If the scan fails the run continues and a notice says repeats were not checked.
- **Overlap check** compares each window with the furthest end so far, so a long window that swallows two later ones is caught (E-OVERLAP).
- **Catalog changes**: quality floor 2 on the command line, shots from 1.4 s (was 2.6 s), sharper labelling guide. The prompt sample is balanced round-robin over shot types (up to 360 lines) and carries SUPPLY and MUST lines.
- **Report (G8)**: `plan_report.py` writes the mail "[Video pipeline] Footage and plan report" (footage by type, labelling stats, repeated footage, what each section used, main mix, MISSING / SHORT lines) and the same text goes into the final mail and the notices.

## v3.1 (labelling that survives a busy Gemini, honest mails)
- **Why**: a run labelled 0 of 197 windows (Gemini quota or busy) and still mailed SUCCESS with random scenes. The router cools a failing model for 1 to 15 minutes, so one busy spell could leave every remaining sheet without an answer.
- `classify_shots.py` now asks again for unanswered sheets after a wait (up to 5 rounds, 30 to 240 s, based on `gemini_router.wait_hint()`), counts why sheets failed (`fail_reasons`) and adds the router events of its own process (`ai_events`) to the CATALOG line. `gemini_router.ask` retries a failed model listing once.
- `label_health.py` judges the result: 0 labels or under 50 percent labelled = unsolved problem (mail status SUCCESS WITH WARNINGS), under 90 percent = notice. It also turns the footage report (shortages, repeated footage) into suggestions, so the "Suggestions after this run" mail is no longer empty in the master path.
- `plan_report.py` starts with a WARNING and marks the USED lines `[unverified]` when labelling failed.
- Test: `python -m unittest discover -s tests -p test_v31_labelling.py` (16).
- **Log mail after every run**: `autonomy/email_logs.py` (already the last workflow step, `if: always()`) now mails `pipeline-logs.zip` after every run, not only failures. Subject `[Video pipeline] Run logs attached (SUCCESS | SUCCESS WITH WARNINGS)` or `FAILED - logs attached`. The body lists the key lines (warnings, router failovers, labelling stats, plan report). The zip holds `build.log` (full output of every stage), `alerts.log` (every mail), `analysis/*.json`, `build/timeline.json`, the render report and `shots/*_rows.json`; secrets are replaced by `***`. Needs the `MAIL_USER` and `MAIL_APP_PASSWORD` secrets (the step prints "secrets missing" otherwise). `EMAIL_LOGS_ALWAYS=0` restores failures-only. Test: `tests/test_email_logs.py` (5).
