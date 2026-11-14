#!/usr/bin/env python3
"""
classify_shots.py - label the pipeline's contact-sheet rows with the MASTER TEMPLATE's shot types and write
the catalog that script_planner.py reads (--catalog).

Input  (made by the existing stages): <shots-dir>/<src>_rows.json   rows: {id, s, e, shot_start, shot_end, motion}
                                      <shots-dir>/<src>_sheet_<kk>.jpg   8 rows per sheet (rows[kk*8:(kk+1)*8])
Output: JSON list [{source_video_file, start, end, shot_type, quality}] (contiguous same-type rows are merged).

Gemini only LABELS. Every answer is validated here: unknown ids are ignored, unknown types become
"unclassified", quality is clamped to 1-5, and rows Gemini did not answer stay unlabelled (they are dropped,
never guessed). The model chain is the repository's own router (autonomy/gemini_router.py: real model ids are
discovered from the API, tiers, cooldowns, circuit breaker).

Exit codes: 0 catalog written (may be empty: planner then runs content-blind and says so), 2 bad input.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import timeline_core as core  # noqa: E402

log = logging.getLogger("classify_shots")
PER_SHEET = 8          # must equal `per` in pipeline/shots2.py

GUIDE = {
    "wide": "broad stadium/pitch view, players small",
    "close_skill": "tight or medium-close on the featured player dribbling / doing skills",
    "semi_close": "medium shot, featured player clearly visible with the ball, less tight than close_skill",
    "duel_1v1": "featured player against one defender",
    "run_making": "featured player running or carrying the ball at pace, off-ball runs",
    "ball_receive_pass": "receiving or playing a pass",
    "good_pass_chance": "key pass or chance creation",
    "goal": "a goal being scored (ball in the net)",
    "penalty_goal": "a penalty kick being scored",
    "near_miss_shot": "shot saved, blocked or off target",
    "face": "close-up of a face",
    "celebration": "celebrating (after a goal or at the end)",
    "bus_arrival": "team bus or arrival at the stadium",
    "player_exit_bus_or_tunnel": "someone stepping off a bus or walking out through a tunnel",
    "dressing_room": "dressing room",
    "fans_crowd": "fans, supporters, a crowd",
    "player_portrait": "posed or portrait shot of one person",
    "stylized_atmosphere": "aerial view, crest, artistic stadium atmosphere",
    "trophy_ceremony": "trophy, medals, ceremony",
    "training": "training or warm-up",
    "after_match": "post-match scenes",
    "tunnel_entry": "entering or leaving through the tunnel",
}


def taxonomy(template: dict) -> list[str]:
    enum = template["timeline_schema"]["properties"]["timeline"]["items"]["properties"]["shot_type"]["enum"]
    return [t for t in enum if t != "unclassified"]


def build_prompt(ids: list[str], types: list[str]) -> str:
    lines = "\n".join(f"- {t}: {GUIDE.get(t, t.replace('_', ' '))}" for t in types)
    return (
        "You label football (soccer) highlight footage. The image is a contact sheet. Each ROW is one clip window: "
        "the left column shows the row id (yellow), its time range, duration and a motion number; the four pictures "
        "are frames from that window in time order.\n"
        "Label EVERY row with exactly one shot_type from this list:\n" + lines + "\n"
        "- reject: referee, bench or staff close-ups, replays or graphics overlays, scoreboards, black or blurred "
        "frames, anything unusable\n"
        "Also give quality 1-5 (5 = sharp, steady, clearly framed; 1 = blurry, shaky, obstructed). "
        "Judge only what is visible. Do not identify anyone by face. If unsure use \"reject\" with quality 1.\n"
        "Return JSON only, exactly: {\"rows\":[{\"id\":\"<row id>\",\"shot_type\":\"<type>\",\"quality\":<1-5>}]} "
        "with exactly these ids, once each: " + json.dumps(ids)
    )


def parse_answer(text: str | None, ids: list[str], types: set[str]) -> dict[str, tuple[str, int]]:
    """Validate a model answer. Returns {row_id: (shot_type, quality)} for rows that were answered sensibly."""
    if not text:
        return {}
    try:
        data = json.loads(re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip())
        items = data["rows"] if isinstance(data, dict) else data
    except (ValueError, KeyError, TypeError):
        return {}
    out: dict[str, tuple[str, int]] = {}
    for it in items if isinstance(items, list) else []:
        try:
            rid = str(it["id"])
            if rid not in ids or rid in out:
                continue
            typ = str(it.get("shot_type", "reject"))
            typ = typ if typ in types or typ == "reject" else "unclassified"
            q = max(1, min(5, int(round(float(it.get("quality", 3))))))
            out[rid] = (typ, q)
        except (KeyError, TypeError, ValueError):
            continue
    return out


def classify_sheet(ask: Callable[..., Any], sheet: Path, ids: list[str], types: list[str]) -> dict[str, tuple[str, int]]:
    prompt = build_prompt(ids, types)
    for attempt in (1, 2):
        try:
            text, model = ask("vision", prompt, images=[str(sheet)], json_mode=True, timeout=120)
        except Exception as exc:  # never let one sheet kill the run
            log.warning("sheet %s attempt %d failed: %s", sheet.name, attempt, type(exc).__name__)
            continue
        got = parse_answer(text, ids, set(types))
        if got:
            return got
        log.warning("sheet %s attempt %d: no usable answer", sheet.name, attempt)
    return {}


def merge_rows(rows: list[dict], labels: dict[str, tuple[str, int]], min_quality: int) -> list[dict]:
    """Rows -> catalog segments. Contiguous rows of one shot with the same type are merged (quality = minimum)."""
    segs: list[dict] = []
    for r in sorted(rows, key=lambda r: (r["src"], r["s"])):
        lab = labels.get(r["id"])
        if not lab or lab[0] == "reject" or lab[1] < min_quality:
            continue
        typ, q = lab
        last = segs[-1] if segs else None
        if (last and last["source_video_file"] == f"{r['src']}.mp4" and last["shot_type"] == typ
                and last["_shot"] == r["shot_start"] and abs(r["s"] - last["end"]) <= 0.06):
            last["end"] = round(r["e"], 3)
            last["quality"] = min(last["quality"], q)
        else:
            segs.append({"source_video_file": f"{r['src']}.mp4", "start": round(r["s"], 3), "end": round(r["e"], 3),
                         "shot_type": typ, "quality": q, "_shot": r["shot_start"]})
    for s in segs:
        s.pop("_shot", None)
    return segs


def run(shots_dir: Path, srcs: list[str], template: dict, ask: Callable[..., Any],
        min_quality: int = 3, workers: int = 3) -> tuple[list[dict], dict]:
    types = taxonomy(template)
    jobs, all_rows = [], []
    for src in srcs:
        rp = shots_dir / f"{src}_rows.json"
        if not rp.exists():
            log.warning("no rows file for %s", src)
            continue
        rows = json.loads(rp.read_text(encoding="utf-8"))
        all_rows += rows
        for k in range(0, len(rows), PER_SHEET):
            sheet = shots_dir / f"{src}_sheet_{k // PER_SHEET:02d}.jpg"
            chunk = rows[k:k + PER_SHEET]
            if sheet.exists():
                jobs.append((sheet, [r["id"] for r in chunk]))
            else:
                log.warning("missing sheet %s", sheet.name)
    labels: dict[str, tuple[str, int]] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for res in ex.map(lambda j: classify_sheet(ask, j[0], j[1], types), jobs):
            labels.update(res)
    segs = merge_rows(all_rows, labels, min_quality)
    seconds: dict[str, float] = {}
    for s in segs:
        seconds[s["shot_type"]] = round(seconds.get(s["shot_type"], 0.0) + s["end"] - s["start"], 1)
    stats = {"rows": len(all_rows), "labelled": len(labels), "sheets": len(jobs), "segments": len(segs),
             "seconds_by_type": dict(sorted(seconds.items()))}
    return segs, stats


def _router_ask() -> Callable[..., Any] | None:
    sys.path.insert(0, str(HERE.parent / "autonomy"))
    try:
        import gemini_router as G  # type: ignore
        return G.ask
    except Exception as exc:  # noqa: BLE001
        log.warning("gemini_router unavailable (%s)", type(exc).__name__)
        return None


def main(argv: list[str] | None = None, ask: Callable[..., Any] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--template", default="config/master_rendering_template.json")
    ap.add_argument("--shots-dir", default="/home/claude/work/shots")
    ap.add_argument("--sources", nargs="+", required=True, help="source stems, e.g. s01 s02")
    ap.add_argument("--out", default="/home/claude/work/analysis/shot_catalog.json")
    ap.add_argument("--min-quality", type=int, default=3)
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    try:
        tpl = core.load_template(args.template)
    except core.PlanError as exc:
        log.error("%s", exc)
        return 2
    ask = ask or _router_ask()
    if ask is None:
        segs, stats = [], {"rows": 0, "labelled": 0, "sheets": 0, "segments": 0, "seconds_by_type": {}}
        log.warning("No AI available: writing an EMPTY catalog (planner will be content-blind)")
    else:
        segs, stats = run(Path(args.shots_dir), args.sources, tpl, ask, args.min_quality, args.workers)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(segs, indent=1), encoding="utf-8")
    print("CATALOG " + json.dumps(stats), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
