"""
comp_rules.py - composition rules that decide WHAT is in the video (timeline_core.validate calls check()).
 C-SKILL  main part must be led by skill clips (rule M13)          error when `strict` and the footage allows it
 C-INTRO  intro must use bus / player-exit / dressing-room footage when it exists (I1, I2, I7)   same
 C-CELEB  a celebration in main only directly after a goal or penalty goal (M14)                always an error
 C-DUP    no window inside an excluded duplicate range (G7)                                       always an error
"""
from __future__ import annotations

import catalog_tools as ct


def check(shots: list[dict], ctx: dict, strict: bool = True) -> tuple[list[str], list[str]]:
    cons = ctx["template"]["constraints"]
    cat = ctx.get("catalog") or []
    errs: list[str] = []
    warns: list[str] = []
    soft = errs if strict else warns
    sup = ct.supply_by_type(cat)

    for s in shots:                                   # C-DUP
        d = ct.overlaps_dup(s["source_video_file"], s["trim_start"],
                            s["trim_start"] + s["duration"] * s["speed_multiplier"], ctx.get("dups") or [])
        if d:
            errs.append(f"E-DUP {s['shot_id']}: {s['source_video_file']} {s['trim_start']:.1f}s repeats footage already "
                        f"used from {d.get('dup_of', 'another place')} (G7)")

    cel = cons.get("main_celebration", {})            # C-CELEB
    goal_types = set(cel.get("only_directly_after", ["goal", "penalty_goal"]))
    for i, s in enumerate(shots):
        if s["section"] != "main" or s["shot_type"] != "celebration":
            continue
        prev = shots[i - 1] if i else None
        if not prev or prev["section"] != "main" or prev["shot_type"] not in goal_types:
            errs.append(f"E-CELEB {s['shot_id']}: a main-part celebration must come directly after a goal clip (M14)")
            continue
        if s["duration"] > cel.get("max_s", 3.0) + 0.01:
            errs.append(f"E-CELEB {s['shot_id']}: celebration is {s['duration']:.1f}s, max {cel.get('max_s', 3.0)}s")
        if s["source_video_file"] != prev["source_video_file"]:
            warns.append(f"W-CELEB {s['shot_id']}: celebration is from another source than its goal")
        elif not 0 <= s["trim_start"] - (prev["trim_start"] + prev["duration"]) <= cel.get("same_source_within_s", 25.0):
            warns.append(f"W-CELEB {s['shot_id']}: celebration is not close in time after its goal")
    for i, s in enumerate(shots[1:], 1):
        if s["section"] == "main" and s["shot_type"] == "celebration" and shots[i - 1]["shot_type"] == "celebration":
            errs.append(f"E-CELEB {s['shot_id']}: two celebration clips in a row")

    mains = [s for s in shots if s["section"] == "main"]
    if mains and cat:                                 # C-SKILL
        skills = set(cons.get("skill_types", []))
        main_dur = sum(s["duration"] for s in mains)
        share = sum(s["duration"] for s in mains if s["shot_type"] in skills) / max(main_dur, 1e-6)
        need = cons.get("skill_share_min_of_main", 0.5)
        have = sum(v for k, v in sup.items() if k in skills)
        if share < need - 0.005:
            msg = f"C-SKILL: skill clips are {share:.0%} of main, need at least {need:.0%} (rule M13)"
            (soft if have >= need * main_dur * 1.1 else warns).append(msg)

    intro = [s for s in shots if s["section"] == "intro"]
    scenes = set(cons.get("intro_scene_types", []))
    if intro and cat:                                 # C-INTRO
        have = sum(v for k, v in sup.items() if k in scenes)
        min_clip = cons["sections"]["intro"]["min_clip_s"]
        if have >= min_clip and not any(s["shot_type"] in scenes for s in intro):
            soft.append("C-INTRO: the catalog has bus / player-exit / dressing-room footage but the intro uses none (I1, I2)")
        bus = sup.get("bus_arrival", 0.0)
        if bus >= min_clip and intro[0]["shot_type"] != "bus_arrival":
            soft.append("C-INTRO: bus footage exists, so the intro must open with it (I1)")
    return errs, warns
