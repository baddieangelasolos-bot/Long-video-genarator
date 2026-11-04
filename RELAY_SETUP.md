# GitHub private-repo relay: setup (about 10 minutes, no software to install)

## A. Google Drive (1 min)
Select the 5 files > Share > General access > **Anyone with the link** > Viewer. (The GitHub runner downloads anonymously.)
Switch back to Restricted when the relay has finished.

## B. GitHub repo + workflow (4 min)
1. github.com > New repository > name `video-relay` > **Private** > tick "Add a README" > Create.
2. Add file > Create new file > type the name `.github/workflows/ingest_drive.yml` (typing the slashes makes the folders)
   > paste the contents of ingest_drive.yml > Commit changes.
3. Actions tab (enable workflows if asked) > **ingest-drive** > Run workflow > keep tag `v1.0.0` > Run.
4. Wait for a green check (roughly 5-15 min). Then the repo's Releases section shows `v1.0.0` with 5 files + manifest.json.
   Red X? Open the run and send me the failing step's text. Typical causes: a file is not shared as "Anyone with the link",
   or Drive's "too many downloads" limit (re-run later).

## C. Read-only token for me (3 min)
GitHub > profile photo > Settings > Developer settings > Personal access tokens > **Fine-grained tokens** > Generate new token.
- Expiration: 7 days. Repository access: **Only select repositories** > video-relay.
- Repository permissions > **Contents: Read-only** (Metadata read is added automatically). Nothing else.
- Generate, copy the `github_pat_...` value.
Send me: `owner/video-relay` and the token. It only lets the holder read that one private repo, for 7 days. Delete it afterwards.

## D. After ingestion
Delete the token, delete the release (or repo), and set the Drive files back to Restricted.
