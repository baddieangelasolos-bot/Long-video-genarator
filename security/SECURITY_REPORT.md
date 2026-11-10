# Security report (public-repo readiness)

## Findings fixed in code
| Item | Where | Fix |
|---|---|---|
| Owner e-mail hardcoded | autonomy/email_logs.py | removed; uses MAIL_TO secret |
| 5 Drive file ids hardcoded | autonomy/fetch_drive.py | now read from BUILTIN_FILES_JSON secret (default empty) |
| Drive folder id in docs | SETUP_GUIDE.md, docs/SETUP_GUIDE.md | replaced by placeholder |
| Old workflow copy with the same Drive ids | stray folder starting with an invisible character + "docs" | delete it (see steps) |
| No .gitignore | repo root | added (videos, audio, .env, keys, logs) |

No API keys, tokens, webhook URLs, chat ids or private keys were found in the current files.

## Workflow hardening
- Every job runs only if the actor is the repo owner (`github.actor == github.repository_owner`).
- Least privilege: default token is read-only; only the build job gets contents:write (releases).
- Secrets are given only to the steps that need them (not to apt, pip or third-party actions).
- Actions pinned to commit SHAs (checkout, setup-python, upload-artifact).
- No `pull_request_target`; fork pull requests get no secrets.
- Artifacts, release fallback and log summary are disabled on public repos (they are world-readable there). Logs reach you by e-mail with secrets redacted; the video goes to Drive.
- Untrusted inputs go through env vars, never inline in scripts; tag and flags are validated.

## Guard
security-guard.yml: scans current files and full git history on every push, pull request and weekly. Current-file finding = red check + alert e-mail with DONE button. History-only finding = warning e-mail (weekly/manual runs).
DONE opens security-remediate.yml (owner only, default branch only). It replaces the exposed value with your repository secret, checks python compiles, rescans, pushes.

## Limits (honest)
- Removing a value from code never removes it from old commits. Revoke the old key and use a new one.
- A hidden web link that pushes code would be a security hole, so DONE opens GitHub (your login and 2FA are the lock).
- Auto-fix handles plain string literals in .py/.yml/.md/.txt; other cases are reported for hand editing.
- Settings that code cannot change are listed in the setup steps (fork approvals, branch rules, secret scanning, 2FA).
- Dependencies: pip-audit found no known vulnerabilities in requirements.txt (checked today; not pinned to exact versions).

## History acknowledgements
security/history_acknowledged.txt lists old commits already reviewed. The guard stops warning about them. Drive ids and the owner e-mail in those commits are neutralised by: unsharing/re-uploading the Drive files (new ids) and keeping the live values only in the BUILTIN_FILES_JSON and MAIL_TO secrets.
