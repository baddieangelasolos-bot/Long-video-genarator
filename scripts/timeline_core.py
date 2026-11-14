#!/usr/bin/env python3
"""
timeline_core.py - shared sandbox logic for the Long Video Generator.

Used by BOTH scripts/script_planner.py (to validate what Gemini wrote) and
scripts/render_video.py (to refuse a bad timeline before spending render time).

Design rule: Gemini only *proposes*. Everything in this file is plain Python that
*checks and repairs*. If a proposal cannot be made valid, it is rejected, never
"best-effort rendered".
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
from pathlib import Path
from typing import Any

FPS_GRID = 60  # durations are snapped to this grid by the renderer
SECTIONS = ("intro", "main", "outro")
CORE_KEYS = ("shot_id", "section", "shot_type", "source_video_file",
             "trim_start", "duration", "transition", "speed_multiplier")
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}


class PlanError(Exception):
    """Raised when a timeline cannot be validated or repaired."""


# --------------------------------------------------------------------------- #
# Template + media helpers
# --------------------------------------------------------------------------- #
def load_template(path: str | os.PathLike) -> dict:
    """Load the master template. A missing or broken template is fatal on purpose."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            tpl = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanError(f"Cannot read master template {path}: {exc}") from exc
    for key in ("render_spec", "timeline_schema", "constraints", "planning_rulebook"):
        if key not in tpl:
            raise PlanError(f"Master template is missing required block '{key}'")
    return tpl


def _run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def probe_media(path: str | os.PathLike) -> dict:
    """ffprobe one file. Returns duration, fps, width, height (video fields are None for audio)."""
    cmd = ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    try:
        res = _run(cmd)
        data = json.loads(res.stdout or "{}")
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
        raise PlanError(f"ffprobe failed on {path}: {exc}") from exc
    if res.returncode != 0 or "format" not in data:
        raise PlanError(f"ffprobe could not read {path}: {res.stderr.strip()[:200]}")
    info: dict[str, Any] = {"file": Path(path).name,
                            "duration": float(data["format"].get("duration") or 0.0),
                            "fps": None, "width": None, "height": None, "has_audio": False}
    for st in data.get("streams", []):
        if st.get("codec_type") == "video" and info["width"] is None:
            info["width"], info["height"] = st.get("width"), st.get("height")
            num, _, den = (st.get("avg_frame_rate") or st.get("r_frame_rate") or "0/1").partition("/")
            try:
                info["fps"] = round(float(num) / float(den or 1), 3) if float(den or 1) else None
            except ValueError:
                info["fps"] = None
        if st.get("codec_type") == "audio":
            info["has_audio"] = True
    return info


DEFAULT_EXCLUDE = ("ref.mp4", "ref.mov", "ref.mkv")   # the reference edit is style input, never footage


def scan_sources(sources_dir: str | os.PathLike, min_duration: float = 4.0,
                 exclude: tuple[str, ...] | list[str] | None = None) -> tuple[dict, list[str]]:
    """Probe every video in a folder. Clips shorter than min_duration and names in `exclude`
    (default: ref.mp4, the reference video) are skipped and reported."""
    skip_names = {n.lower() for n in (DEFAULT_EXCLUDE if exclude is None else exclude)}
    sources: dict[str, dict] = {}
    skipped: list[str] = []
    for p in sorted(Path(sources_dir).iterdir()):
        if p.suffix.lower() not in VIDEO_EXT or not p.is_file():
            continue
        if p.name.lower() in skip_names:
            skipped.append(f"{p.name}: excluded (reference video, not footage)")
            continue
        try:
            info = probe_media(p)
        except PlanError as exc:
            skipped.append(f"{p.name}: unreadable ({exc})")
            continue
        if info["width"] is None:
            skipped.append(f"{p.name}: no video stream")
        elif info["duration"] < min_duration:
            skipped.append(f"{p.name}: shorter than {min_duration}s")
        else:
            sources[p.name] = info
    return sources, skipped


def audible_end(music_path: str | os.PathLike, floor_db: float = -55.0) -> float:
    """
    Seconds until the music is last audible (ignores a silent tail). Falls back to the
    container duration if numpy is missing or decoding fails.
    """
    total = probe_media(music_path)["duration"]
    try:
        import numpy as np
        sr = 8000
        proc = subprocess.run(["ffmpeg", "-v", "error", "-i", str(music_path), "-vn", "-ac", "1",
                               "-ar", str(sr), "-f", "s16le", "-"], capture_output=True, timeout=300)
        pcm = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32)
        if pcm.size < sr:
            return total
        win = sr // 20  # 50 ms windows
        n = pcm.size // win
        rms = np.sqrt((pcm[: n * win].reshape(n, win) ** 2).mean(axis=1))
        loud = np.where(rms > 32768.0 * (10 ** (floor_db / 20.0)))[0]
        if loud.size == 0:
            return total
        return float(min(total, (loud[-1] + 1) * 0.05))
    except Exception:  # decoding is best-effort; the container duration is a safe answer
        return total


def target_length(audible: float) -> float:
    """Final length = floor(audible_end x 2) / 2, i.e. rounded DOWN to 0.5 s."""
    return math.floor(audible * 2.0) / 2.0


def section_budgets(target_s: float, tpl: dict) -> dict[str, float]:
    """Split the target length into intro/main/outro seconds using the template's shares and clamps."""
    sec = tpl["constraints"]["sections"]
    if target_s < 30:   # too short for an intro + outro: the whole video is "main" (validate() agrees)
        return {"intro": 0.0, "main": round(target_s, 4), "outro": 0.0}
    intro = target_s * sec["intro"]["share"]
    if target_s >= 120:
        intro = min(sec["intro"]["max_s"], max(sec["intro"]["min_s_if_video_over_120s"], intro))
    else:
        intro = min(sec["intro"]["max_s"], intro)
    outro = target_s * sec["outro"]["share"]
    if target_s >= 60:
        outro = min(sec["outro"]["max_s"], max(sec["outro"]["min_s"], outro))
    main = target_s - intro - outro
    snap = lambda x: round(x * FPS_GRID) / FPS_GRID  # noqa: E731
    intro, outro = snap(intro), snap(outro)
    return {"intro": intro, "main": round(target_s - intro - outro, 4), "outro": outro}


def runtime_schema(tpl: dict, available_sources: list[str]) -> dict:
    """Copy of the template schema with the real file names injected as an enum (the sandbox)."""
    schema = json.loads(json.dumps(tpl["timeline_schema"]))
    item = schema["properties"]["timeline"]["items"]["properties"]["source_video_file"]
    item.pop("x-runtime-enum", None)
    item["enum"] = list(available_sources)
    return schema


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def _is_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _catalog_index(catalog: list[dict] | None) -> dict[str, list[dict]]:
    idx: dict[str, list[dict]] = {}
    for seg in catalog or []:
        idx.setdefault(seg["source_video_file"], []).append(seg)
    return idx


def validate(plan: dict, ctx: dict) -> tuple[list[str], list[str]]:
    """
    Check a timeline against the template. Returns (errors, warnings).
    ctx = {"template", "sources": {name: probe}, "target_s", "catalog": optional list}
    Errors block rendering. Warnings are reported and never block.
    """
    tpl, sources, target = ctx["template"], ctx["sources"], ctx["target_s"]
    cons = tpl["constraints"]
    errors: list[str] = []
    warns: list[str] = []

    shots = plan.get("timeline") if isinstance(plan, dict) else None
    if not isinstance(shots, list) or not shots:
        return ["E-SCHEMA: 'timeline' must be a non-empty array"], warns

    schema_item = tpl["timeline_schema"]["properties"]["timeline"]["items"]
    props = schema_item["properties"]
    allowed_types = set(props["shot_type"]["enum"])
    allowed_trans = set(props["transition"]["enum"])
    id_re = re.compile(cons["id_pattern"])

    # ---- per-shot structural checks --------------------------------------
    for i, s in enumerate(shots):
        tag = f"shot[{i}]"
        if not isinstance(s, dict):
            errors.append(f"E-SCHEMA {tag}: not an object")
            continue
        extra = set(s) - set(CORE_KEYS)
        missing = [k for k in CORE_KEYS if k not in s]
        if missing:
            errors.append(f"E-SCHEMA {tag}: missing keys {missing}")
            continue
        if extra:
            errors.append(f"E-SCHEMA {tag}: unknown keys {sorted(extra)}")
        if not (isinstance(s["shot_id"], str) and id_re.match(s["shot_id"])):
            errors.append(f"E-ID {tag}: shot_id must match {cons['id_pattern']}")
        if s["section"] not in SECTIONS:
            errors.append(f"E-SECTION {tag}: section '{s['section']}' invalid")
        if s["shot_type"] not in allowed_types:
            errors.append(f"E-TYPE {tag}: shot_type '{s['shot_type']}' not in taxonomy")
        if s["transition"] not in allowed_trans:
            errors.append(f"E-TRANSITION {tag}: '{s['transition']}' not allowed")
        for k in ("trim_start", "duration", "speed_multiplier"):
            if not _is_num(s[k]):
                errors.append(f"E-SCHEMA {tag}: {k} must be a number")
        if any(not _is_num(s[k]) for k in ("trim_start", "duration", "speed_multiplier")):
            continue
        smin, smax = props["speed_multiplier"]["minimum"], props["speed_multiplier"]["maximum"]
        if not smin <= s["speed_multiplier"] <= smax:
            errors.append(f"E-SPEED {tag}: speed_multiplier {s['speed_multiplier']} outside {smin}-{smax}")
        elif not cons["speed"]["min"] <= s["speed_multiplier"] <= cons["speed"]["max"]:
            warns.append(f"W-SPEED {tag}: speed {s['speed_multiplier']} is outside the reference range "
                         f"{cons['speed']['min']}-{cons['speed']['max']}")
        if s["trim_start"] < 0:
            errors.append(f"E-WINDOW {tag}: trim_start < 0")
        # source existence + window inside the file (G3)
        src = sources.get(s["source_video_file"])
        if src is None:
            errors.append(f"E-SRC {tag}: '{s['source_video_file']}' is not an available source")
        elif s["trim_start"] + s["duration"] * s["speed_multiplier"] > src["duration"] - 0.05 + 1e-6:
            errors.append(f"E-WINDOW {tag}: window ends at "
                          f"{s['trim_start'] + s['duration'] * s['speed_multiplier']:.2f}s but "
                          f"{s['source_video_file']} is {src['duration']:.2f}s long")

    if errors and any(e.startswith(("E-SCHEMA", "E-ID", "E-SECTION")) for e in errors):
        return errors, warns  # structure is too broken for the semantic checks below

    # ---- ids sequential (V01) --------------------------------------------
    for i, s in enumerate(shots):
        if s["shot_id"] != f"S{i + 1:03d}":
            errors.append(f"E-ID: shot[{i}] is {s['shot_id']}, expected S{i + 1:03d}")
            break

    # ---- section order + contiguity (V06) --------------------------------
    order = [SECTIONS.index(s["section"]) for s in shots]
    if order != sorted(order):
        errors.append("E-ORDER: sections must run intro -> main -> outro without interleaving")
    present = {s["section"] for s in shots}
    need = SECTIONS if target >= 30 else ("main",)
    for sec in need:
        if sec not in present:
            errors.append(f"E-ORDER: section '{sec}' is missing")

    # ---- per-section clip lengths + types + budgets ----------------------
    budgets = section_budgets(target, tpl)
    sec_cfg = cons["sections"]
    first_main = next((i for i, s in enumerate(shots) if s["section"] == "main"), None)
    for i, s in enumerate(shots):
        cfg = sec_cfg.get(s["section"])
        if not cfg or not _is_num(s["duration"]):
            continue
        lo, hi = cfg["min_clip_s"], cfg["max_clip_s"]
        if i == first_main and s["shot_type"] == "wide":  # the 0.73 s opener seen in the reference
            lo, hi = cfg["opener_wide_min_s"], cfg["opener_wide_max_s"]
        if not lo - 1e-6 <= s["duration"] <= hi + 1e-6:
            errors.append(f"E-CLIP {s['shot_id']}: {s['section']} clip {s['duration']:.2f}s outside {lo}-{hi}s")
        if s["shot_type"] not in cfg["allowed_types"]:
            errors.append(f"E-TYPE {s['shot_id']}: '{s['shot_type']}' is not allowed in {s['section']}")
    for sec in SECTIONS:
        got = sum(s["duration"] for s in shots if s["section"] == sec)
        want = budgets[sec]
        if want and sec in present and abs(got - want) > max(0.35 * want, 1.0):
            errors.append(f"E-BUDGET: {sec} is {got:.1f}s, budget {want:.1f}s (more than 35% off)")
        elif want and sec in present and abs(got - want) > 0.05:
            warns.append(f"W-BUDGET: {sec} is {got:.2f}s, budget {want:.2f}s")

    # ---- total (V05) ------------------------------------------------------
    total = sum(s["duration"] for s in shots)
    if abs(total - target) > cons["total_tolerance_s"]:
        errors.append(f"E-TOTAL: timeline is {total:.2f}s, target {target:.2f}s")

    # ---- transitions (V08) ------------------------------------------------
    zooms = [i for i, s in enumerate(shots) if s["transition"] == "zoom_punch_cut"]
    if len(zooms) > cons["transitions"]["zoom_punch_cut_max_uses"] or any(i != first_main for i in zooms):
        errors.append("E-TRANSITION: zoom_punch_cut is allowed once, only on the first main shot")

    # ---- no overlapping footage (G4) --------------------------------------
    by_src: dict[str, list[tuple[float, float, str]]] = {}
    for s in shots:
        by_src.setdefault(s["source_video_file"], []).append(
            (s["trim_start"], s["trim_start"] + s["duration"] * s["speed_multiplier"], s["shot_id"]))
    for name, wins in by_src.items():
        wins.sort()
        for a, b in zip(wins, wins[1:]):
            if b[0] < a[1] - 1e-6:
                errors.append(f"E-OVERLAP: {a[2]} and {b[2]} reuse the same footage of {name}")

    # ---- special clip caps and wide share ---------------------------------
    types = [s["shot_type"] for s in shots]
    main_shots = [s for s in shots if s["section"] == "main"]
    main_dur = sum(s["duration"] for s in main_shots) or 1.0
    wide_share = sum(s["duration"] for s in main_shots if s["shot_type"] == "wide") / main_dur
    if wide_share > cons["wide_share_max_of_main"] + 0.01:
        errors.append(f"E-WIDE: wide clips are {wide_share:.0%} of main, cap is {cons['wide_share_max_of_main']:.0%}")
    if types.count("face") > cons["face_clips_max"]:
        errors.append(f"E-CAP: {types.count('face')} face clips, max {cons['face_clips_max']}")
    # NOTE: a long goal is meant to be cut into parts (rule M7), so the cap is on distinct goal
    # *events* only when the catalog can tell them apart; here it is on clip count x 4 parts.
    if types.count("goal") > cons["goal_clips_max"] * 4:
        errors.append(f"E-CAP: {types.count('goal')} goal clips (max {cons['goal_clips_max']} goals, up to 4 parts each)")
    if types.count("penalty_goal") > cons["penalty_goal_clips_max"] * 4:
        errors.append("E-CAP: too many penalty_goal clips (one penalty, up to 4 parts)")
    intro_cel = sum(1 for s in shots if s["section"] == "intro" and s["shot_type"] == "celebration")
    if intro_cel > 1:
        errors.append("E-CAP: more than one celebration clip in the intro (rule I3)")

    # ---- catalog containment (content sandbox) ----------------------------
    cidx = _catalog_index(ctx.get("catalog"))
    if cidx:
        for s in shots:
            if s["shot_type"] == "unclassified":
                continue
            segs = [g for g in cidx.get(s["source_video_file"], []) if g["shot_type"] == s["shot_type"]]
            a, b = s["trim_start"], s["trim_start"] + s["duration"] * s["speed_multiplier"]
            if not any(g["start"] - 0.05 <= a and b <= g["end"] + 0.05 for g in segs):
                errors.append(f"E-CATALOG {s['shot_id']}: window {a:.1f}-{b:.1f}s of {s['source_video_file']} "
                              f"is not inside a catalogued '{s['shot_type']}' segment")

    # ---- soft rules (warnings only) ---------------------------------------
    intro_shots = [s for s in shots if s["section"] == "intro"]
    pref = tpl["planning_rulebook"]["intro"]["order_of_preference"]
    if intro_shots and intro_shots[0]["shot_type"] not in ("bus_arrival", "player_exit_bus_or_tunnel") \
            and any(c["shot_type"] in ("bus_arrival", "player_exit_bus_or_tunnel") for c in (ctx.get("catalog") or [])):
        warns.append("W-I1: intro does not open with a bus/tunnel scene although one exists")
    ranks = [pref.index(s["shot_type"]) for s in intro_shots if s["shot_type"] in pref]
    if ranks and ranks != sorted(ranks):
        warns.append("W-I-ORDER: intro clips are not in the preferred order")
    if intro_shots and "training" in {s["shot_type"] for s in intro_shots} and \
            any(c["shot_type"] != "training" and c["shot_type"] in pref for c in (ctx.get("catalog") or [])):
        warns.append("W-I4: training clips used in the intro while better intro footage exists")
    if first_main is not None and shots[first_main]["shot_type"] != "wide":
        warns.append("W-T3: first main shot is not a wide clip")
    t0 = 0.0
    first_goal = None
    for s in shots:
        if s["shot_type"] in ("goal", "penalty_goal", "near_miss_shot") and first_goal is None:
            first_goal = t0
        t0 += s["duration"]
    lo, hi = cons["first_goal_window_s"]
    if first_goal is not None and target >= hi and not lo <= first_goal <= hi:
        warns.append(f"W-M6: first goal at {first_goal:.0f}s, wanted {lo:.0f}-{hi:.0f}s")
    for a, b in zip(shots, shots[1:]):
        if a["source_video_file"] == b["source_video_file"] and len(sources) > 1:
            warns.append(f"W-SRC: {a['shot_id']} and {b['shot_id']} come from the same source back to back")
            break
    outro_shots = [s for s in shots if s["section"] == "outro"]
    if outro_shots and outro_shots[-1]["shot_type"] != "tunnel_entry" and \
            any(c["shot_type"] == "tunnel_entry" for c in (ctx.get("catalog") or [])):
        warns.append("W-O2: tunnel_entry footage exists but the video does not end on it")
    return errors, warns


# --------------------------------------------------------------------------- #
# Deterministic repair (safe fixes only)
# --------------------------------------------------------------------------- #
def _match_source(name: Any, sources: dict) -> str | None:
    """Exact name, else case-insensitive base-name match. Never fuzzy: a wrong guess is a hallucination."""
    if not isinstance(name, str):
        return None
    if name in sources:
        return name
    base = os.path.basename(name.replace("\\", "/")).lower()
    hits = [k for k in sources if k.lower() == base]
    return hits[0] if len(hits) == 1 else None


def repair(plan: dict, ctx: dict) -> tuple[dict, list[str]]:
    """
    Fix what can be fixed without inventing content: strip unknown keys, coerce numbers,
    drop shots with unknown sources, clamp windows, renumber ids, then stretch or shrink
    clip lengths inside their limits so each section hits its budget.
    Returns (new_plan, notes). Never raises for bad content; validate() decides afterwards.
    """
    tpl, sources, target = ctx["template"], ctx["sources"], ctx["target_s"]
    cons, notes = tpl["constraints"], []
    props = tpl["timeline_schema"]["properties"]["timeline"]["items"]["properties"]
    raw = plan.get("timeline") if isinstance(plan, dict) else None
    if not isinstance(raw, list):
        return {"timeline": []}, ["repair: no timeline array found"]

    shots: list[dict] = []
    for s in raw:
        if not isinstance(s, dict):
            notes.append("repair: dropped a non-object entry")
            continue
        s = {k: s.get(k) for k in CORE_KEYS}
        for k in ("trim_start", "duration", "speed_multiplier"):
            try:
                s[k] = round(float(s[k]), 3) if s[k] is not None else None
            except (TypeError, ValueError):
                s[k] = None
        if s["speed_multiplier"] is None:
            s["speed_multiplier"] = 1.0
        s["speed_multiplier"] = min(props["speed_multiplier"]["maximum"],
                                    max(props["speed_multiplier"]["minimum"], s["speed_multiplier"]))
        name = _match_source(s["source_video_file"], sources)
        if name is None:
            notes.append(f"repair: dropped shot with unknown source {s['source_video_file']!r}")
            continue
        s["source_video_file"] = name
        if s["section"] not in SECTIONS or s["duration"] is None or s["trim_start"] is None:
            notes.append(f"repair: dropped incomplete shot ({s['shot_id']})")
            continue
        if s["shot_type"] not in props["shot_type"]["enum"]:
            s["shot_type"] = "unclassified"
        if s["transition"] not in props["transition"]["enum"]:
            s["transition"] = cons["transitions"]["default"]
        shots.append(s)

    # stable sort so a model that shuffled sections does not lose the whole plan
    shots.sort(key=lambda s: SECTIONS.index(s["section"]))

    first_main = next((i for i, s in enumerate(shots) if s["section"] == "main"), None)
    for i, s in enumerate(shots):
        if s["transition"] == "zoom_punch_cut" and i != first_main:
            s["transition"] = "hard_cut"
            notes.append(f"repair: {s['shot_id']} zoom_punch_cut -> hard_cut (only allowed on first main shot)")
        cfg = cons["sections"][s["section"]]
        lo, hi = cfg["min_clip_s"], cfg["max_clip_s"]
        if i == first_main and s["shot_type"] == "wide":
            lo, hi = cfg["opener_wide_min_s"], cfg["opener_wide_max_s"]
        s["duration"] = round(min(hi, max(lo, s["duration"])), 3)
        src = sources[s["source_video_file"]]
        room = src["duration"] - 0.05
        need = s["duration"] * s["speed_multiplier"]
        if need > room:  # source shorter than the clip: shorten the clip
            s["duration"] = round(max(0.0, room / s["speed_multiplier"]), 3)
            need = s["duration"] * s["speed_multiplier"]
        s["trim_start"] = round(min(max(0.0, s["trim_start"]), max(0.0, room - need)), 3)

    _trim_overlaps(shots, notes)
    _fit_to_budget(shots, sources, target, tpl, ctx.get("catalog"), notes)
    for i, s in enumerate(shots):
        s["shot_id"] = f"S{i + 1:03d}"
    return {"timeline": shots}, notes


def _trim_overlaps(shots: list[dict], notes: list[str]) -> None:
    """If a window runs into the next window of the same source, shorten it (never below the section minimum)."""
    by_src: dict[str, list[dict]] = {}
    for s in shots:
        by_src.setdefault(s["source_video_file"], []).append(s)
    for wins in by_src.values():
        wins.sort(key=lambda w: w["trim_start"])
        for a, b in zip(wins, wins[1:]):
            end = a["trim_start"] + a["duration"] * a["speed_multiplier"]
            if end > b["trim_start"] - 0.02:
                new = round((b["trim_start"] - 0.02 - a["trim_start"]) / a["speed_multiplier"], 3)
                if new >= 0.6:
                    notes.append(f"repair: shortened {a['shot_id']} by {a['duration'] - new:.2f}s to stop reusing footage")
                    a["duration"] = new


def _room_after(s: dict, sources: dict, cat_idx: dict[str, list[dict]], shots: list[dict]) -> float:
    """
    Extra seconds (output time) a clip may grow forward without leaving its source, leaving its catalogued
    segment, or running into the next window planned from the same source (rule G4: no reused footage).
    """
    end = sources[s["source_video_file"]]["duration"] - 0.05
    for o in shots:
        if o is not s and o["source_video_file"] == s["source_video_file"] and o["trim_start"] >= s["trim_start"] + 1e-6:
            end = min(end, o["trim_start"] - 0.02)
    if cat_idx and s["shot_type"] != "unclassified":
        for g in cat_idx.get(s["source_video_file"], []):
            if g["shot_type"] == s["shot_type"] and g["start"] - 0.05 <= s["trim_start"] <= g["end"]:
                end = min(end, g["end"])
                break
    return max(0.0, (end - s["trim_start"]) / s["speed_multiplier"] - s["duration"])


def _fit_to_budget(shots: list[dict], sources: dict, target: float, tpl: dict,
                   catalog: list[dict] | None, notes: list[str]) -> None:
    """Spread each section's length error over its clips, proportionally, inside every limit."""
    cons = tpl["constraints"]
    budgets = section_budgets(target, tpl)
    cat_idx = _catalog_index(catalog)
    first_main = next((i for i, s in enumerate(shots) if s["section"] == "main"), None)
    for sec in SECTIONS:
        idxs = [i for i, s in enumerate(shots) if s["section"] == sec]
        if not idxs:
            continue
        cfg = cons["sections"][sec]
        for _ in range(8):  # converges in 2-3 passes; the cap prevents any infinite loop
            delta = budgets[sec] - sum(shots[i]["duration"] for i in idxs)
            if abs(delta) < 0.004:
                break
            caps = []
            for i in idxs:
                s = shots[i]
                lo, hi = cfg["min_clip_s"], cfg["max_clip_s"]
                if i == first_main and s["shot_type"] == "wide":
                    lo, hi = cfg["opener_wide_min_s"], cfg["opener_wide_max_s"]
                if delta > 0:
                    caps.append(max(0.0, min(hi - s["duration"], _room_after(s, sources, cat_idx, shots))))
                else:
                    caps.append(max(0.0, s["duration"] - lo))
            tot = sum(caps)
            if tot < 1e-6:
                break
            take = min(abs(delta), tot)
            for i, c in zip(idxs, caps):
                shots[i]["duration"] = round(shots[i]["duration"] + math.copysign(take * c / tot, delta), 3)
        got = sum(shots[i]["duration"] for i in idxs)
        if abs(got - budgets[sec]) > 0.05:
            notes.append(f"repair: {sec} ends {got - budgets[sec]:+.2f}s from budget (limits reached)")
    # absorb rounding dust (< 1 frame-ish) on the last main shot so the total is exact
    total = sum(s["duration"] for s in shots)
    resid = round(target - total, 3)
    if shots and 0 < abs(resid) <= 0.25:
        mains = [s for s in shots if s["section"] == "main"] or shots
        s = mains[-1]
        new = round(s["duration"] + resid, 3)
        cfg = cons["sections"][s["section"]]
        if cfg["min_clip_s"] <= new <= cfg["max_clip_s"] and _room_after(s, sources, cat_idx, shots) >= resid:
            s["duration"] = new
