"""
plan_repair.py - the content-aware repair steps used by timeline_core.repair().

Principle: Gemini only proposes. The catalog (labels made by classify_shots.py) is the truth about what is in each
footage window. A shot that contradicts the catalog is relabelled, moved inside its segment, or dropped; a shot whose
type is not allowed in its section is dropped. Nothing is ever invented, and timeline_core.fit then refills the sections.
"""
from __future__ import annotations

import catalog_tools as ct
import timeline_core as core


def reclamp(shots: list[dict], cons: dict) -> None:
    """After drops/relabels the first main shot may have changed: re-apply clip limits and the zoom-punch rule."""
    first_main = next((k for k, s in enumerate(shots) if s["section"] == "main"), None)
    for i, s in enumerate(shots):
        lo, hi = core._clip_limits(shots, i, cons)
        if s["section"] == "main" and s["shot_type"] == "celebration":
            hi = min(hi, (cons.get("main_celebration") or {}).get("max_s", hi))
            lo = min(lo, hi)
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


def trim_overlaps(shots: list[dict], notes: list[str], cons: dict | None = None) -> None:
    """If a window runs into a later window of the same source, shorten it; if that would break the clip minimum,
    drop the later shot instead. Compares against the furthest end so far (a long window that swallows two
    later ones is caught for both)."""
    drop: set[int] = set()
    by_src: dict[str, list[int]] = {}
    for i, s in enumerate(shots):
        by_src.setdefault(s["source_video_file"], []).append(i)
    for idxs in by_src.values():
        idxs.sort(key=lambda k: shots[k]["trim_start"])
        cur = None                                   # index of the kept shot with the furthest end
        for ib in idxs:
            b = shots[ib]
            if cur is None:
                cur = ib
                continue
            a = shots[cur]
            end = a["trim_start"] + a["duration"] * a["speed_multiplier"]
            if end > b["trim_start"] - 0.02:
                new = round((b["trim_start"] - 0.02 - a["trim_start"]) / a["speed_multiplier"], 3)
                lo = core._clip_limits(shots, cur, cons)[0] if cons else 0.6
                if new >= max(0.6, lo):
                    notes.append(f"repair: shortened {a['shot_id']} by {a['duration'] - new:.2f}s to stop reusing footage")
                    a["duration"] = new
                else:
                    drop.add(ib)
                    notes.append(f"repair: dropped {b['shot_id']} (it reused footage of another shot)")
                    continue
            if b["trim_start"] + b["duration"] * b["speed_multiplier"] > shots[cur]["trim_start"] + shots[cur]["duration"] * shots[cur]["speed_multiplier"]:
                cur = ib
    if drop:
        shots[:] = [s for i, s in enumerate(shots) if i not in drop]


def drop_dups(shots: list[dict], ctx: dict, notes: list[str]) -> None:
    """Footage that exists twice is used once: a shot inside an excluded duplicate range is dropped (the fit step refills)."""
    dups = ctx.get("dups") or []
    if not dups:
        return
    keep = []
    for s in shots:
        d = ct.overlaps_dup(s["source_video_file"], s["trim_start"], s["trim_start"] + s["duration"] * s["speed_multiplier"], dups)
        if d:
            notes.append(f"repair: dropped {s['shot_id']} (same footage as {d.get('dup_of', 'another place')})")
        else:
            keep.append(s)
    shots[:] = keep


def fix_celebration(shots: list[dict], cons: dict, notes: list[str]) -> None:
    """Rule M14: a celebration in the main part stays only directly after a goal / penalty goal, once, at most max_s long."""
    cel = cons.get("main_celebration") or {}
    ok_prev = set(cel.get("only_directly_after", ["goal", "penalty_goal"]))
    keep: list[dict] = []
    for s in shots:
        if s["section"] == "main" and s["shot_type"] == "celebration":
            prev = keep[-1] if keep else None
            if not prev or prev["section"] != "main" or prev["shot_type"] not in ok_prev:
                notes.append(f"repair: dropped {s['shot_id']} (celebration is only allowed directly after a goal)")
                continue
            s["duration"] = round(min(s["duration"], cel.get("max_s", 3.0)), 3)
        keep.append(s)
    shots[:] = keep
