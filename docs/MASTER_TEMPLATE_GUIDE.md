# Master template: architecture brief and usage

## What each file does
| File | Role |
|---|---|
| `config/master_rendering_template.json` | Permanent rulebook + schema + measured pacing from the reference edit. The only place numbers live. |
| `scripts/timeline_core.py` | The sandbox: probes media, computes section budgets, validates, repairs. Shared by planner and renderer. |
| `scripts/script_planner.py` | Gemini fills the timeline (10-model chain). Every answer is repaired + validated. Offline planner if all fail. |
| `scripts/classify_shots.py` | Gemini labels the pipeline's contact-sheet rows with the template's shot types and writes `analysis/shot_catalog.json`. |
| `scripts/render_video.py` | Validates again, renders 1920x1080 / 60 fps / CRF 18 / slow, optional beat snap, verifies the MP4. |

## Run locally
```bash
pip install -r requirements.txt            # plus: apt install ffmpeg
export GEMINI_API_KEY=...                   # never commit it
python scripts/script_planner.py --sources-dir work/src --music work/music.m4a \
       [--catalog work/shot_catalog.json] --out work/timeline.json
python scripts/render_video.py  --sources-dir work/src --music work/music.m4a \
       --timeline work/timeline.json --out work/final.mp4 [--beat-snap] [--no-letterbox]
python -m unittest discover -s tests -v     # 39 tests, Gemini mocked
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
`classify_shots.py` writes `[{"source_video_file","start","end","shot_type","quality"}]` (quality 3 or better, contiguous
same-type rows merged). If Gemini is unreachable the run continues content-blind and the mail says so.

## Security
Read-only token, `GEMINI_API_KEY` only in the pipeline step, owner-only job, public repo never uploads the MP4 or raw logs.
