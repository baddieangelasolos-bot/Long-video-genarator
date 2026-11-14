"""
plan_repair.py - the content-aware repair steps used by timeline_core.repair().

Principle: Gemini only proposes. The catalog (labels made by classify_shots.py) is the truth about what is in each
footage window. A shot that contradicts the catalog is relabelled, moved inside its segment, or dropped; a shot whose
type is not allowed in its section is dropped. Nothing is ever invented, and timeline_core.fit then refills the sections.
"""
from __future__ import annotations

import timeline_core as core


def reclamp(shots: list[dict], cons: dict) -> None:
    """After drops/relabels the first main shot may have changed: re-apply clip limits and the zoom-punch rule."""
    first_main = next((k for k, s in enumerate(shots) if s["section"] == "main"), None)
    for i, s in enumerate(shots):
        lo, hi = core._clip_limits(shots, i, cons)
        s["duration"] = round(min(hi, max(lo, s["duration"])), 3)
        if s["transition"] == "zoom_punch_cut" and i != first_main:
            s["transition"] = "hard_cut"


def drop_disallowed(shots: list[dict], cons: dict, notes: list[str]) -> None:
    keep = []
    for s in shots:
        if s["shot_type"] in cons["sections"][s["section"]]["allowed_types"]:
            keep.append(s)
        else:
            notes.append(f"repair: dropped {s['shot_id']} ('{s['shot_type']}' is not allowed in {s['section']})")
    shots[:] = keep


def apply_catalog(shots: list[dict], ctx: dict, notes: list[str]) -> None:
    """With a catalog: every window must lie inside a catalogued segment. Same-type segment -> shift/shorten the window
    into it; only another type overlaps -> relabel to that type; nothing overlaps (invented footage) -> drop."""
    idx = core._catalog_index(ctx.get("catalog"))
    if not idx:
        return
    cons = ctx["template"]["constraints"]
    keep = []
    for s in shots:
        segs = idx.get(s["source_video_file"], [])
        sp_ = s["speed_multiplier"]
        a, need = s["trim_start"], s["duration"] * s["speed_multiplier"]
        b = a + need
        ov = lambda g: min(b, g["end"]) - max(a, g["start"])        # noqa: E731
        same = [g for g in segs if g["shot_type"] == s["shot_type"] and ov(g) > 0.3]
        if same:
            g = max(same, key=ov)
        else:
            anyg = [g for g in segs if ov(g) >= 0.5 * need]
            if not anyg:
                notes.append(f"repair: dropped {s['shot_id']} (window is not inside any catalogued segment)")
                continue
            g = max(anyg, key=ov)
            if s["shot_type"] != g["shot_type"]:
                notes.append(f"repair: {s['shot_id']} relabelled {s['shot_type']} -> {g['shot_type']} (catalog)")
                s["shot_type"] = g["shot_type"]
        if not (g["start"] - 0.05 <= a and b <= g["end"] + 0.05):
            seg_len = g["end"] - g["start"]
            if seg_len >= need:
                s["trim_start"] = round(min(max(a, g["start"]), g["end"] - need), 3)
            else:
                s["trim_start"], s["duration"] = round(g["start"], 3), round(seg_len / sp_, 3)
            notes.append(f"repair: {s['shot_id']} moved inside its catalogued segment")
        cfg = cons["sections"][s["section"]]
        lo = cfg["opener_wide_min_s"] if (s["section"] == "main" and s["shot_type"] == "wide") else cfg["min_clip_s"]
        if s["duration"] < lo - 1e-6:
            notes.append(f"repair: dropped {s['shot_id']} (catalogued segment too short)")
            continue
        keep.append(s)
    shots[:] = keep


def shed_wide(shots: list[dict], target: float, tpl: dict, notes: list[str]) -> bool:
    """If wide clips exceed the cap: shrink them to their minimum, then drop trailing wides. True if anything changed."""
    cons = tpl["constraints"]
    limit = core._wide_limit(target, tpl)
    wides = [i for i, s in enumerate(shots) if s["section"] == "main" and s["shot_type"] == "wide"]
    total = sum(shots[i]["duration"] for i in wides)
    if total <= limit + 0.01:
        return False
    for i in wides:
        lo, _ = core._clip_limits(shots, i, cons)
        cut = min(shots[i]["duration"] - lo, (total - limit) * (shots[i]["duration"] - lo) / max(1e-6, sum(
            shots[k]["duration"] - core._clip_limits(shots, k, cons)[0] for k in wides)))
        shots[i]["duration"] = round(shots[i]["duration"] - max(0.0, cut), 3)
    total = sum(shots[i]["duration"] for i in wides)
    first_main = next((k for k, s in enumerate(shots) if s["section"] == "main"), None)
    for i in reversed(wides):
        if total <= limit + 0.01:
            break
        if i == first_main:
            continue
        total -= shots[i]["duration"]
        notes.append(f"repair: dropped {shots[i]['shot_id']} (wide clips would exceed the {cons['wide_share_max_of_main']:.0%} cap)")
        shots.pop(i)
    notes.append("repair: wide clips reduced to respect the wide-share cap")
    return True
