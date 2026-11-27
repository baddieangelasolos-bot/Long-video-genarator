# Master template: architecture brief and usage

## What each file does
| File | Role |
|---|---|
| `config/master_rendering_template.json` | Permanent rulebook + schema + measured pacing from the reference edit. The only place numbers live. |
| `scripts/timeline_core.py` | The sandbox: probes media, computes section budgets, validates, repairs. Shared by planner and renderer. |
| `scripts/script_planner.py` | Gemini fills the timeline with the whole master template in the prompt. v5: the brain is the whole 17-name chain (see v5). Every answer is repaired + validated. In the workflow (`--strict-brain`) there is no offline fallback: see v4. |
| `scripts/brain_guard.py` | v4: reads Gemini errors, computes when a model is usable again, builds the BRAIN UNAVAILABLE report. |
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
- `master_plan`: `script_planner.py --strict-brain` (brain chain, validated, no offline fallback). Exit 3 becomes a HALTED mail "NEEDS MORE SOURCE VIDEO"; exit 5 becomes a HALTED mail "Gemini brain unavailable - run again after <time>" (v4).
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

## v4 (brain rule: only the 3 best models plan, otherwise stop and wait)
- **Owner rule**: the video script and plan are made only by `script_planner.BRAIN_MODELS` = the first 3 models of `MODEL_FALLBACK_CHAIN` (today `gemini-3.8-flash` primary, `gemini-3.6-flash`, `gemini-3.5-flash-lite`). The full 10-name chain and its order are unchanged and kept; identify and shot labelling keep their own router chains (`gemini_router.py`). The master template (`config/master_rendering_template.json`) and the prompt (`build_prompt`) were NOT changed.
- **Patience before giving up**: a busy (5xx, timeout) or rate-limited (429) brain model gets up to 2 more passes, after 20 s then 60 s (longer, max 90 s, if Google's own retry hint says so). A model whose DAILY quota is used up is not asked again.
- **If none of the 3 can plan** (quota, overload, not served for the key, no key): `script_planner.py --strict-brain` writes `analysis/brain_unavailable.json` and exits **5**. `run_pipeline.py` turns that into the HALTED mail `[Video pipeline] HALTED: Gemini brain unavailable - run again after <time>`. No offline video is made. `ALLOW_OFFLINE_PLANNER=1` in the runner environment restores the old offline fallback (the workflow file does not set it).
- **The time in the mail is labelled honestly**: EXACT = Google's retry hint or the documented daily reset at midnight Pacific time (08:00 UTC in winter, 07:00 UTC in summer); SAFE = Google named no quota type (the daily reset is the safe time); SUGGESTION = overload (503, timeout) has no published end time, so about 30 minutes is only a suggestion. The mail shows UTC, Pacific time and a countdown.
- **Log mail names the planner**: `email_logs.py` starts the body with a PLANNING BRAIN block (model that planned the video and its slot, the attempts before it, models not served, player-identification model, labelling result and router events). After a brain halt the subject is `[Video pipeline] BRAIN UNAVAILABLE - run again after <time> - logs attached` and the body starts with the halt report. `build/timeline.json` `_planner` now also holds `model_used`, `brain_models`, `slot`, and each failed attempt carries `http`, `message`, `state`, `at`, `basis`.
- **Limits worth knowing**: identify and labelling use the same Gemini project quota as the planner, so a run that labels many shots on 3.8-flash / 3.6-flash can use up what the planner needs. A run with failed labelling still ends as SUCCESS WITH WARNINGS (content-blind); only the planner is strict.
- Exit codes of `script_planner.py`: 0 ok, 2 setup, 3 needs more source, 4 no valid plan (non-strict), 5 brain unavailable (strict).
- Test: `python -m unittest discover -s tests -p test_brain_rule.py` (29).

## v5 (owner, 26 Nov 2026: master template in the prompt, longer fallback chains, Dhaka time)
- **Master template to the brain**: `build_prompt` now starts the user prompt with `MASTER_TEMPLATE`: the complete `config/master_rendering_template.json` (compact JSON, unchanged), and the system instruction tells the model to follow it. The template file itself was not changed. One line was added to `SECTION_BUDGET_S`: code rejects a section more than 35% off its budget (the v4 test run failed on that).
- **Planner (brain) chain** = `MODEL_FALLBACK_CHAIN`, all 17 names, in order; `BRAIN_MODELS` is the whole chain. The v4 names keep their order; new names: `gemini-4-hydrogen` (slot 4), `gemini-2.5-pro`, `gemini-2.5-flash`, `gemma-4-31b-it`, `gemma-4-26b-a4b-it`, `gemini-flash-latest`, `gemini-flash-lite-latest`. Names the key does not serve are skipped without a call. Every answer must still pass the template rules; if none does, the run stops (exit 5) as in v4.
- **Planner call**: 180 s per request (v4: 75 s, and Google answered 504 DEADLINE_EXCEEDED right at that limit), up to 32768 output tokens. Gemma models get the system text on top of the prompt and no JSON mode; `parse_json` reads the JSON out of their text. The whole planning step has a time budget: `PLANNER_BUDGET_MIN` (default 45); models not reached in time are reported as not tried.
- **Router** (identify, style, labelling, Mechanic consult): up to 16 tiers (`GEMINI_MAX_TIERS`). Order: `USER_PRIORITY` (v4 order kept, `gemini-4-hydrogen` added at slot 4, then 3.1-flash-lite, 2.5-flash-lite, the two Gemma 4 models and the two rolling aliases), then other discovered models newest first, then paid-only models (`PAID_ONLY`: gemini-4-carbon/argon/neon, 3.1-pro, pro-latest, customtools) last. A 429 with `limit: 0` (not in the free tier) cools that model for 6 h. Gemma 4 is allowed (older Gemma, TTS, live, decision, embedding models stay excluded).
- **Dhaka time**: every time in the halt mail and its subject is shown in Dhaka time (UTC+6) first, then UTC and Pacific, e.g. `2026-11-26 13:56 Dhaka (07:56 UTC)`.
- Test: `python -m unittest tests.test_brain_rule` (35).

