"""catalog_tools.py - helpers on the shot catalog: remove duplicate footage, pick a balanced prompt sample, count supply."""
from __future__ import annotations

import json
from pathlib import Path


def load_dups(path: str | None) -> list[dict]:
    """Excluded ranges [{file,start,end,...}] written by dup_scan.py. Missing or bad file -> no exclusions."""
    if not path:
        return []
    try:
        rows = json.loads(Path(path).read_text(encoding="utf-8"))
        return [r for r in rows if isinstance(r, dict) and {"file", "start", "end"} <= set(r)]
    except (OSError, ValueError):
        return []


def subtract_ranges(catalog: list[dict], dups: list[dict], min_len: float = 0.8) -> list[dict]:
    """Cut every excluded range out of the catalog segments (a segment may split in two; pieces under min_len vanish)."""
    if not dups:
        return catalog
    by_file: dict[str, list[tuple[float, float]]] = {}
    for d in dups:
        by_file.setdefault(d["file"], []).append((float(d["start"]), float(d["end"])))
    out: list[dict] = []
    for g in catalog:
        pieces = [(g["start"], g["end"])]
        for a, b in sorted(by_file.get(g["source_video_file"], [])):
            nxt = []
            for x, y in pieces:
                if b <= x or a >= y:
                    nxt.append((x, y))
                    continue
                if a - x >= min_len:
                    nxt.append((x, a))
                if y - b >= min_len:
                    nxt.append((b, y))
            pieces = nxt
        for x, y in pieces:
            if y - x >= min_len:
                out.append({**g, "start": round(x, 3), "end": round(y, 3)})
    return out


def overlaps_dup(file: str, a: float, b: float, dups: list[dict]) -> dict | None:
    for d in dups:
        if d["file"] == file and min(b, d["end"]) - max(a, d["start"]) > 0.15:
            return d
    return None


def supply_by_type(catalog: list[dict]) -> dict[str, float]:
    tot: dict[str, float] = {}
    for g in catalog:
        tot[g["shot_type"]] = tot.get(g["shot_type"], 0.0) + g["end"] - g["start"]
    return {k: round(v, 1) for k, v in sorted(tot.items(), key=lambda kv: -kv[1])}


def balanced_sample(catalog: list[dict], max_lines: int) -> list[dict]:
    """
    Prompt sample that keeps EVERY shot type and EVERY source represented. Taking the top-quality lines overall
    (the old behaviour) let sharp goal / celebration footage push out skills and phone-quality intro scenes.
    Round-robin over types; inside a type the best quality first, spread over the sources.
    """
    if len(catalog) <= max_lines:
        return sorted(catalog, key=lambda g: (g["shot_type"], g["source_video_file"], g["start"]))
    pools: dict[str, list[dict]] = {}
    for g in catalog:
        pools.setdefault(g["shot_type"], []).append(g)
    for typ, lst in pools.items():
        per_src: dict[str, list[dict]] = {}
        for g in sorted(lst, key=lambda g: (-g["quality"], -(g["end"] - g["start"]))):
            per_src.setdefault(g["source_video_file"], []).append(g)
        ordered: list[dict] = []
        while any(per_src.values()):
            for k in list(per_src):
                if per_src[k]:
                    ordered.append(per_src[k].pop(0))
        pools[typ] = ordered
    picked: list[dict] = []
    while len(picked) < max_lines and any(pools.values()):
        for typ in list(pools):
            if pools[typ] and len(picked) < max_lines:
                picked.append(pools[typ].pop(0))
    return sorted(picked, key=lambda g: (g["shot_type"], g["source_video_file"], g["start"]))
