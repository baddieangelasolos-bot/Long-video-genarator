"""plan_report.py - the footage and plan report that goes into the run mail (what was wanted, found, used, missing)."""
from __future__ import annotations

import catalog_tools as ct
import timeline_core as core


def _fmt(d: dict[str, float]) -> str:
    return ", ".join(f"{k} {v:.0f}s" for k, v in d.items()) or "none"


def build(ctx: dict, timeline: list[dict], planner: str) -> dict:
    tpl, target = ctx["template"], ctx["target_s"]
    cons = tpl["constraints"]
    cat = ctx.get("catalog") or []
    sup = ct.supply_by_type(cat)
    bud = core.section_budgets(target, tpl)
    used: dict[str, dict[str, list]] = {}
    for s in timeline:
        u = used.setdefault(s["section"], {}).setdefault(s["shot_type"], [0, 0.0])
        u[0] += 1
        u[1] = round(u[1] + s["duration"], 2)
    short: list[str] = []
    info: list[str] = []
    if not cat:
        short.append("No shot labels: scene choice was content-blind (skills, intro scenes and goals could not be chosen on purpose).")
    else:
        mc = cons["sections"]["intro"]["min_clip_s"]
        for typ, label in (("bus_arrival", "bus arrival"), ("player_exit_bus_or_tunnel", "player leaving the bus or tunnel"),
                           ("dressing_room", "dressing room")):
            have = sup.get(typ, 0.0)
            if have < mc:
                short.append(f"INTRO: no usable {label} footage found ({have:.0f}s).")
            elif have < 4.0:
                short.append(f"INTRO: only {have:.0f}s of {label} footage.")
        intro_have = sum(v for k, v in sup.items() if k in cons["sections"]["intro"]["allowed_types"])
        if intro_have < bud["intro"] * 1.3:
            short.append(f"INTRO: {intro_have:.0f}s of intro-type footage for a {bud['intro']:.0f}s intro (fillers like training or fans were needed).")
        skills = set(cons.get("skill_types", []))
        skill_have = sum(v for k, v in sup.items() if k in skills)
        skill_need = cons.get("skill_share_min_of_main", 0.5) * bud["main"]
        if skill_have < skill_need * 1.1:
            short.append(f"MAIN: only {skill_have:.0f}s of skill footage (dribbling, close-ups, passes, runs, 1v1) found; "
                         f"about {skill_need * 1.1:.0f}s is needed, so the main part is short on skills.")
        if sup.get("goal", 0.0) < 1.2 and sup.get("penalty_goal", 0.0) < 1.2:
            info.append("MAIN: no goal footage found, so the main part has no goal clips.")
        if sup.get("wide", 0.0) < 0.1 * bud["main"]:
            info.append(f"MAIN: little wide footage ({sup.get('wide', 0.0):.0f}s).")
        out_have = sum(sup.get(k, 0.0) for k in ("after_match", "celebration", "tunnel_entry", "player_portrait"))
        if out_have < bud["outro"]:
            short.append(f"OUTRO: {out_have:.0f}s of after-match/celebration footage for a {bud['outro']:.0f}s outro.")
        if sup.get("tunnel_entry", 0.0) < cons["sections"]["outro"]["min_clip_s"]:
            short.append("OUTRO: no player-into-the-tunnel footage, so the video cannot end on it.")
    mains = used.get("main", {})
    main_s = sum(v[1] for v in mains.values()) or 1.0
    skill_used = sum(v[1] for k, v in mains.items() if k in set(cons.get("skill_types", [])))
    goal_s = sum(v[1] for k, v in mains.items() if k in ("goal", "penalty_goal", "near_miss_shot"))
    cel = mains.get("celebration", [0, 0.0])[0]
    plan_line = (f"Main part: skills {skill_used / main_s:.0%}, goals/penalty {goal_s / main_s:.0%}, "
                 f"{cel} celebration clip(s) after goals, wide {mains.get('wide', [0, 0.0])[1] / main_s:.0%}.")
    return {"planner": planner, "target_s": target, "budgets_s": bud, "supply_s": sup,
            "used": {sec: {t: v for t, v in d.items()} for sec, d in used.items()},
            "shortages": short, "info": info, "plan_line": plan_line,
            "skill_share_main": round(skill_used / main_s, 3)}


def text(rep: dict, cat_stats: dict | None = None, dups: dict | None = None) -> str:
    L = [f"FOOTAGE FOUND (seconds by type): {_fmt(rep['supply_s'])}"]
    if cat_stats:
        L.append(f"Labelling: {cat_stats.get('labelled', 0)} of {cat_stats.get('rows', 0)} windows labelled; "
                 f"{cat_stats.get('rejected_rows', 0)} rejected as unusable, {cat_stats.get('too_low_quality_rows', 0)} too poor in quality.")
    if dups:
        L.append(f"Repeated footage: {dups.get('ranges', 0)} repeated stretch(es), {dups.get('seconds', 0)}s, excluded so nothing shows twice.")
    for sec in ("intro", "main", "outro"):
        u = rep["used"].get(sec, {})
        L.append(f"{sec.upper()} USED ({rep['budgets_s'].get(sec, 0):.0f}s): " + (", ".join(f"{t} x{v[0]} ({v[1]:.0f}s)" for t, v in u.items()) or "nothing"))
    L.append(rep["plan_line"])
    L += ["MISSING / SHORT: " + x for x in rep["shortages"]] or []
    if not rep["shortages"]:
        L.append("MISSING / SHORT: nothing, every wanted clip type had enough footage.")
    L += ["NOTE: " + x for x in rep["info"]]
    L.append(f"Planner: {rep['planner']}")
    return "\n".join(L)
