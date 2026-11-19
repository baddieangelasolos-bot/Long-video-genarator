"""label_health.py - turns the labelling statistics and the plan report into mail text:
notices, unsolved problems (-> status SUCCESS WITH WARNINGS) and improvement suggestions.
Pure functions, no I/O, so they are unit-tested without a run."""
from __future__ import annotations


def _reasons(stats: dict) -> str:
    bits = []
    fr = stats.get("fail_reasons") or {}
    if fr:
        bits.append(", ".join(f"{k} x{v}" for k, v in sorted(fr.items(), key=lambda kv: -kv[1])))
    ev = stats.get("ai_events") or []
    if ev:
        bits.append("router: " + "; ".join(ev[:3]))
    return " | ".join(bits) or "no reason recorded"


def assess(stats: dict | None, classify_crashed: bool = False) -> dict[str, list[str]]:
    """Judge how well the shot labelling went. Returns {"notices": [...], "unsolved": [...], "suggestions": [...]}."""
    out: dict[str, list[str]] = {"notices": [], "unsolved": [], "suggestions": []}
    again = ("Run the workflow again later (an hour, or after your Gemini daily quota resets). "
             "If it keeps happening, check the quota in Google AI Studio or add billing.")
    if classify_crashed or not stats:
        out["unsolved"].append("Shot labelling crashed: the video was planned content-blind (scene types are random picks, "
                               "not checked). Run again for a properly labelled video.")
        out["suggestions"].append(again)
        return out
    rows, lab = int(stats.get("rows", 0)), int(stats.get("labelled", 0))
    if not rows:
        return out
    cov = lab / rows
    rounds = int(stats.get("retry_rounds", 0))
    if lab == 0:
        out["unsolved"].append(f"Shot labelling got NO answers (0 of {rows} windows, {rounds} retry round(s)). The video was planned "
                               f"content-blind: scene types are random picks, not checked footage. Cause: {_reasons(stats)}.")
        out["suggestions"].append(again)
    elif cov < 0.5:
        out["unsolved"].append(f"Shot labelling answered only {lab} of {rows} windows after {rounds} retry round(s); most of the video "
                               f"was planned blind. Cause: {_reasons(stats)}.")
        out["suggestions"].append(again)
    elif cov < 0.9:
        out["notices"].append(f"Shot labelling answered {lab} of {rows} windows after {rounds} retry round(s); unanswered windows were left out.")
        out["suggestions"].append("Gemini was busy during labelling: a later run may label more of your footage.")
    elif rounds:
        out["notices"].append(f"Shot labelling needed {rounds} retry round(s) (Gemini was busy) but finished: {lab} of {rows} windows labelled.")
        out["suggestions"].append("Gemini was busy during labelling and the retry wait fixed it. Runs at quieter times are faster.")
    if lab and not stats.get("segments"):
        out["notices"].append(f"All {lab} labelled windows were rejected or too poor in quality: the footage may be unusable (replays, graphics, blur).")
        out["suggestions"].append("Check that the uploaded clips are match footage; every window was rejected or too poor.")
    return out


def report_suggestions(rep: dict, dups: dict | None = None) -> list[str]:
    """Suggestions from the footage and plan report: what to upload or clean up for a better next video."""
    sug: list[str] = []
    for line in rep.get("shortages", []):
        if line.startswith("No shot labels"):
            continue
        sug.append("Upload more footage for the next video: " + line)
    if dups and dups.get("ranges"):
        sug.append(f"{dups.get('seconds', 0)}s of repeated footage in {', '.join(dups.get('files', []))} was skipped: "
                   "upload each moment once, so more unique footage is available.")
    return sug
