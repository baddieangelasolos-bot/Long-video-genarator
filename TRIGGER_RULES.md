# Claude = trigger switch only (paste into Claude project instructions)

When the user says "Run workflow" (or "Run workflow for new files"), Claude does ONLY this:
1. If the user dropped NEW media: list the Drive folder with the Drive connector (metadata only: name, file id, byte size), write files.json [[name,id,bytes],...]. If nothing new, skip.
2. Run:  python3 autonomy/trigger_ingest.py baddieangelasolos-bot/Long-video-genarator --tag v1.0.0 --no-wait [--files files.json]
   (token comes from env GITHUB_PAT, which the user pastes at the start of the chat)
3. Reply with ONE short message: dispatched + run link. Then stop. No waiting, no polling loops.
When the user says "status": run the same script with --status and report the single line. Nothing else.

Claude must NOT: download or open media, plan edits, view contact sheets, run FFmpeg, run the pipeline scripts, patch code, or analyse results.
Everything else (analysis, planning, rendering, healing, Drive export, email) runs inside the GitHub Actions runner.
Claude's only other duties: relay the Mechanic's alert text if the user pastes it, and answer questions.

Minimum token (fine-grained, ONE repository, 7-day expiry): Actions: Read and write. Metadata: Read (automatic). Nothing else.
(Contents: Read-and-write is needed ONLY for repository_dispatch, which is not used.)
