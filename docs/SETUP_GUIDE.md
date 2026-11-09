# Setup guide: autonomous highlight pipeline (v3)

v3 was tested locally on synthetic media with a mock Gemini only. Nothing here has run on real GitHub Actions, real Gemini, Apps Script or email yet. Expect the first run to need small fixes; the Mechanic will tell you what.

## 0. Security first (5 min)
1. Revoke every GitHub token and Gemini key ever pasted in chat (GitHub > Settings > Developer settings > Tokens; Google AI Studio > API keys). Create a NEW Gemini key.
2. Keep the repo PRIVATE (it holds copyrighted match footage while a run is active; the pipeline purges old releases at the start of each run).

## 1. Put the repo on GitHub
Repo: baddieangelasolos-bot/Long-video-genarator (private).
- Easiest: unzip autonomous_repo_final.zip on a computer, open the repo on github.com > Add file > Upload files, drag EVERYTHING including the hidden `.github` folder (if the folder does not upload, use GitHub Desktop or `git push`).
- Required result: `.github/workflows/ingest_drive.yml`, `autonomy/`, `pipeline/`, `known_good/`, `requirements.txt` on the default branch (main).
- This replaces the old relay workflow (same name and inputs, so trigger_ingest.py still works).

## 2. Apps Script (alerts + Drive export)
1. script.google.com > New project > delete the sample > paste docs/Code_v2_upload.gs > Save.
2. Run `initialSetup` (grant permission: Advanced > Go to project). Copy WEBHOOK_TOKEN from View > Execution log.
3. Optional: pin the export folder: run `setOutputsFolder('1Sdko5QqZAnuKujGMCZYqiqeGjLG5oIUv')` (edit the id in the call, run once). `showOutputsFolder` confirms.
4. Deploy > New deployment > Web app > Execute as: Me; Who has access: Anyone > copy the /exec URL.
5. After any later code edit: Deploy > Manage deployments > Edit > New version.

## 3. GitHub secrets
Repo > Settings > Secrets and variables > Actions > New repository secret:
| Name | Value |
|---|---|
| GEMINI_API_KEY | new AI Studio key |
| APPS_SCRIPT_URL | the /exec URL |
| WEBHOOK_TOKEN | token from step 2 |
| MAIL_USER, MAIL_APP_PASSWORD, MAIL_TO | optional SMTP fallback (Gmail app password: Google Account > Security > 2-Step Verification > App passwords) |

Optional Variables / workflow env (Variables tab or the workflow file): USE_AI (0 skips Gemini), MAX_FPS (default 60), FPS_OVERRIDE (24/25/30/50/60), RENDER_PRESET (default slow), RENDER_CRF (default 15), FETCH_MAX_SOURCES (default 12), FINAL_LEN, USE_BUILTIN_FILES, SKIP_FETCH. PLAYER_NAME and CLUB_NAME are legacy: v3 only trusts PLAYER_NAME when a source file name contains that person.
Settings > Actions > General: allow actions; workflow permissions can stay default (the file requests contents: write).

## 4. Drive (v3: dynamic sources)
Folder tree (created by Apps Script setup): `resources/source video`, `resources/audio file`, `resources/reference videos` (optional), `outputs`.
- Every run uses what is IN these folders: all source videos (newest 12 if more, FETCH_MAX_SOURCES), the newest audio file, the newest reference video. Any player, any number of clips. Nothing is hard-coded any more.
- Run `shareResourceFolders` ONCE in the Apps Script editor (makes ONLY the three resource folders view-by-link so the runner can download; outputs is never shared). Then `testListSources` and read the log: your files must be listed.
- Old fixed files: USE_BUILTIN_FILES=1 (on purpose only). Explicit list: RELAY_FILES_JSON (must be empty/unset for Drive discovery).
- Player/club title: Gemini reads visible text cues only (never faces); backups: `autonomy/overrides.json` (fields player, club; empty = no override, wins over everything), file names, neutral title HIGHLIGHTS.

## 5. First run
1. Check the workflow: job `timeout-minutes` at least 120 (v3 renderer is slower; expect ~15-25 min for 148 s at 30 fps, 40+ min at 60 fps; faster: RENDER_PRESET=medium, MAX_FPS=30, RENDER_CRF=17). Then Actions tab > ingest-drive > Run workflow (leave defaults). Or tell Claude "Run workflow" (needs a fine-grained token: this repo only, Actions: Read and write, 7 days; set it as env var only).
2. Expect roughly 15-60 minutes (estimate, not yet measured on the real runner). Watch the step summary. Downloads of big public Drive files can hit Drive quotas; the Mechanic waits and retries.
3. Result: email from Apps Script, video in the outputs folder, artifacts `final-video` (7 days) and `pipeline-logs` (14 days). If Drive export fails, a release asset is created as fallback.

## 6. Reading the email
- SUCCESS: all good. SUCCESS WITH WARNINGS: read "could NOT fix".
- HALTED: a protected goal (1920x1080, adaptive fps 24/25/30/50/60 matching the footage, length, minimum clip length, 25% wide shots, letterbox) or an unverifiable tempo; the email says why. Nothing was loosened.
- Gemini patches are temporary. If one helped, its diff is in the email/artifact; commit it yourself if you approve.
- Upgrade suggestions arrive as a separate notice; nothing is applied automatically.

## 6b. Email logs on failure (workflow step, paste by hand)
At the END of the steps in `.github/workflows/ingest_drive.yml` (the pipeline step must have `id: pipeline`):
```
      - name: Email logs on any failure
        if: always()
        env:
          RUN_URL: ${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}
          JOB_STATUS: ${{ job.status }}
          PIPELINE_OUTCOME: ${{ steps.pipeline.outcome }}
        run: python3 autonomy/email_logs.py
```
Check in the run log: `[fetch] Drive folders: N source video(s)`, `[director] title:`, `style:`, `export preflight: PREFLIGHT OK`.

## 7. After the first real run
Watch the video yourself (only measurements and sheets were ever reviewed). Add approved plans to known_good/ (a plan only replays for the exact same input set). Tell Claude your feedback on v2.

## 8. Known limits and risks
- Free private-repo Actions minutes are limited per month: check your plan. A full run is long on 2 vCPUs.
- Planner quality with real Gemini vision is untested; DEGRADED plans use a heuristic. For these exact sources the known-good plan reproduces v2.
- The Drive resumable session URL is used without an Authorization header (documented Drive behaviour) but was only tested against a mock.
- Delete tokens and revert Drive sharing when finished.
