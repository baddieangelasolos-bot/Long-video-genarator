"""
offline_planner.py - deterministic safe-mode planner (no AI), used when no Gemini model produces a valid plan.
Follows the rulebook as closely as the catalog allows: intro scenes first, skills lead the main part, goals are built
from parts and followed by their celebration, outro ends on the tunnel clip. Split out of script_planner.py.
"""
from __future__ import annotations

import logging
import random
from typing import Callable

import catalog_tools as ct
import timeline_core as core

log = logging.getLogger("planner")

# --------------------------------------------------------------------------- #
class NeedsMoreSource(Exception):
    """The footage ran out before the video reached its target length (maps to exit code 3)."""


def supply_report(catalog: list[dict]) -> str:
    """Seconds of footage per shot type, for the 'NEEDS MORE SOURCE VIDEO' message."""
    tot: dict[str, float] = {}
    for g in catalog:
        tot[g["shot_type"]] = tot.get(g["shot_type"], 0.0) + g["end"] - g["start"]
    return ", ".join(f"{k} {v:.0f}s" for k, v in sorted(tot.items(), key=lambda kv: -kv[1]))

def auto_catalog(sources: dict) -> list[dict]:
    """With no catalog every source is one 'unclassified' segment (content-blind)."""
    return [{"source_video_file": k, "start": 0.0, "end": round(v["duration"] - 0.05, 3),
             "shot_type": "unclassified", "quality": 3} for k, v in sources.items()]


class _Footage:
    """Tracks used windows so the offline planner never reuses overlapping footage."""

    def __init__(self, catalog: list[dict], rng: random.Random):
        self.cat, self.rng = catalog, rng
        self.used: dict[str, list[tuple[float, float]]] = {}

    def _gaps(self, seg: dict) -> list[tuple[float, float]]:
        a, b = seg["start"], seg["end"]
        gaps, cur = [], a
        for u0, u1 in sorted(self.used.get(seg["source_video_file"], [])):
            if u1 <= cur or u0 >= b:
                continue
            if u0 - cur > 0:
                gaps.append((cur, min(u0, b)))
            cur = max(cur, u1)
        if b - cur > 0:
            gaps.append((cur, b))
        return [(x + 0.25, y - 0.0) for x, y in gaps if y - x >= 0.6]  # 0.25 s guard after used footage

    def take(self, types: list[str], want: float, lo: float, hi: float, avoid: str | None,
             section: str, shot_type_label: str | None = None, randomise: bool = False,
             after: tuple[str, float, float] | None = None) -> dict | None:
        """after=(file, t, within_s): only windows of that file starting 0..within_s after t (celebration of a goal)."""
        best = None
        for t_rank, typ in enumerate(types):
            cands = []
            for seg in self.cat:
                if seg["shot_type"] != typ:
                    continue
                if after and seg["source_video_file"] != after[0]:
                    continue
                for g0, g1 in self._gaps(seg):
                    if after:
                        g0 = max(g0, after[1])
                        if g0 > after[1] + after[2]:
                            continue
                    if g1 - g0 >= lo:
                        score = (t_rank, seg["source_video_file"] == avoid, -seg["quality"], self.rng.random())
                        cands.append((score, seg, g0, g1))
            if cands:
                best = min(cands, key=lambda c: c[0])
                break
        if best is None:
            return None
        _, seg, g0, g1 = best
        d = round(max(lo, min(want, hi, g1 - g0)), 3)
        slack = (g1 - g0) - d
        start = g0 + (self.rng.uniform(0, slack) if randomise and slack > 0 else 0.0)
        start = round(start, 3)
        self.used.setdefault(seg["source_video_file"], []).append((start, start + d))
        return {"shot_id": "S000", "section": section, "shot_type": shot_type_label or seg["shot_type"],
                "source_video_file": seg["source_video_file"], "trim_start": start, "duration": d,
                "transition": "hard_cut", "speed_multiplier": 1.0}

    def take_goal(self, types: list[str], section: str, lo: float, hi: float = 4.0) -> list[dict]:
        """A goal is built from parts (rule M15): build-up, then shot + finish, in time order from ONE segment."""
        best = None
        for rank, typ in enumerate(types):
            cands = [((rank, -seg["quality"], -(g1 - g0), self.rng.random()), seg, g0, g1)
                     for seg in self.cat if seg["shot_type"] == typ for g0, g1 in self._gaps(seg) if g1 - g0 >= lo]
            if cands:
                best = min(cands, key=lambda c: c[0])
                break
        if best is None:
            return []
        _, seg, g0, g1 = best
        span = g1 - g0
        if span >= 2 * lo + 1.4:
            a = min(2.4, max(lo, (span - 0.4) * 0.4))
            b = min(3.2, max(lo, span - a - 0.4))
            wins = [(g0, g0 + a), (g1 - b, g1)]
        else:
            wins = [(g1 - min(hi, span), g1)]
        out = []
        for x, y in wins:
            x, d = round(x, 3), round(y - x, 3)
            self.used.setdefault(seg["source_video_file"], []).append((x, x + d))
            out.append({"shot_id": "S000", "section": section, "shot_type": seg["shot_type"],
                        "source_video_file": seg["source_video_file"], "trim_start": x, "duration": d,
                        "transition": "hard_cut", "speed_multiplier": 1.0})
        return out


def local_plan(ctx: dict, seed: int = 7) -> dict | None:
    """
    Deterministic planner following the rulebook as closely as the catalog allows.
    With no catalog it is content-blind (all 'unclassified'): structure and pacing are right, scene choice is not.
    """
    tpl, target = ctx["template"], ctx["target_s"]
    rng = random.Random(seed)
    cat = ctx.get("catalog") or ct.subtract_ranges(auto_catalog(ctx["sources"]), ctx.get("dups") or [])
    ctx = {**ctx, "strict_comp": False}      # the safe-mode planner reports composition gaps instead of failing on them
    blind = all(g["shot_type"] == "unclassified" for g in cat)
    foot = _Footage(cat, rng)
    budgets = core.section_budgets(target, tpl)
    fp = tpl["pacing_fingerprint"]
    cons = tpl["constraints"]["sections"]
    ANY = ["unclassified"]
    shots: list[dict] = []
    capped = {"face", "goal", "penalty_goal", "near_miss_shot", "wide"}   # never used as generic filler

    def filler(section: str) -> list[str]:
        """Any other allowed type for this section, used only when the preferred pools are exhausted."""
        return [t for t in cons[section]["allowed_types"] if t not in capped]

    def pace(section: str, i: int) -> float:
        base = fp[section][i % len(fp[section])]
        return base * rng.uniform(0.9, 1.1)

    def fill(section: str, budget: float, pick: Callable[[float, int], dict | None]) -> None:
        got, i, misses = 0.0, 0, 0
        while got < budget - 0.5 and misses < 6:
            s = pick(budget - got, i)
            if s is None:
                misses += 1
                continue
            shots.append(s)
            got += s["duration"]
            i += 1

    # ---- intro: preference order, one celebration max, training last -----------------------
    intro_pref = tpl["planning_rulebook"]["intro"]["order_of_preference"]
    intro_cfg = cons["intro"]
    used_intro_types: list[str] = []

    def pick_intro(left: float, i: int) -> dict | None:
        want = min(pace("intro", i), left)
        want = max(want, intro_cfg["min_clip_s"])
        if blind:
            return foot.take(ANY, want, intro_cfg["min_clip_s"], intro_cfg["max_clip_s"],
                             shots[-1]["source_video_file"] if shots else None, "intro", randomise=True)
        order = [t for t in intro_pref if t not in used_intro_types or t in ("fans_crowd", "dressing_room", "stylized_atmosphere")
                 or (t in ("bus_arrival", "player_exit_bus_or_tunnel") and used_intro_types.count(t) < 2)]
        if sum(1 for s in shots if s["section"] == "intro" and s["shot_type"] == "celebration") >= 1:
            order = [t for t in order if t != "celebration"]
        prev = shots[-1]["source_video_file"] if shots else None
        # keep the preferred order moving forward (bus -> dressing room -> fans -> ...): try later types first
        last = max((intro_pref.index(t) for t in used_intro_types if t in intro_pref), default=-1)
        forward = [t for t in order if t in intro_pref and intro_pref.index(t) >= last]
        s = foot.take(forward, want, intro_cfg["min_clip_s"], intro_cfg["max_clip_s"], prev, "intro") \
            or foot.take(order + ["player_portrait"], want, intro_cfg["min_clip_s"], intro_cfg["max_clip_s"], prev, "intro") \
            or foot.take(filler("intro"), want, intro_cfg["min_clip_s"], intro_cfg["max_clip_s"], prev, "intro")
        if s:
            used_intro_types.append(s["shot_type"])
        return s

    fill("intro", budgets["intro"], pick_intro)

    # ---- main: wide opener, 2-4 close then 1-3 wide, goals / penalty / face placed by time ---
    main_cfg = cons["main"]
    close_pool = ["close_skill", "semi_close", "duel_1v1", "run_making", "ball_receive_pass"]
    wide_pool = ["wide"]
    elapsed = lambda: sum(s["duration"] for s in shots)  # noqa: E731
    state = {"goals": 0, "pen": 0, "face": 0, "wide_t": 0.0, "main_t": 0.0, "queue": [], "pending": []}
    cel_cfg = tpl["constraints"].get("main_celebration", {})

    def goal_event(types: list[str], avoid: str | None) -> dict | None:
        """Goal parts in order, then (if one exists right after it in the same source) the celebration of that goal."""
        parts = foot.take_goal(types, "main", main_cfg["min_clip_s"])
        if not parts:
            return None
        last = parts[-1]
        if last["shot_type"] in ("goal", "penalty_goal"):
            end_t = last["trim_start"] + last["duration"]
            cel = foot.take(["celebration"], 2.4, cel_cfg.get("min_s", 1.2), cel_cfg.get("max_s", 3.0), None, "main",
                            after=(last["source_video_file"], end_t, cel_cfg.get("same_source_within_s", 25.0)))
            if cel:
                parts.append(cel)
        state["pending"] = parts[1:]
        return add_main(parts[0])
    main_start = elapsed()

    def add_main(s: dict | None) -> dict | None:
        if s:
            state["main_t"] += s["duration"]
            if s["shot_type"] == "wide":
                state["wide_t"] += s["duration"]
        return s

    # opener: wide followed by the close-up of the same action
    if not blind:
        op = add_main(foot.take(wide_pool, 0.73, main_cfg["opener_wide_min_s"], main_cfg["opener_wide_max_s"],
                                shots[-1]["source_video_file"] if shots else None, "main"))
        if op:
            op["transition"] = "zoom_punch_cut" if any(s["section"] == "intro" for s in shots) else "hard_cut"
            shots.append(op)

    def pick_main(left: float, i: int) -> dict | None:
        t = elapsed()
        want = min(pace("main", i), left)
        avoid = shots[-1]["source_video_file"] if shots else None
        if blind:
            return add_main(foot.take(ANY, max(want, main_cfg["min_clip_s"]), main_cfg["min_clip_s"],
                                      main_cfg["max_clip_s"], avoid, "main", randomise=True))
        # time-triggered specials (rules M6, M8, M9, M10)
        if state["pending"]:
            return add_main(state["pending"].pop(0))
        if state["goals"] < 3 and t >= 35.0 and state["main_t"] > 0 and (i % 9 == 0):
            s = goal_event(["goal", "near_miss_shot"], avoid)
            if s:
                state["goals"] += 1
                return s
        if state["pen"] < 1 and t >= 90.0:
            s = goal_event(["penalty_goal"], avoid)
            state["pen"] += 1
            if s:
                return s
        if state["face"] < 1 and state["main_t"] > 0.4 * (budgets["main"]):
            s = foot.take(["face"], min(want, 2.5), main_cfg["min_clip_s"], 3.5, avoid, "main")
            state["face"] += 1
            if s:
                return add_main(s)
        # rhythm: a run of 2-4 close clips, then 1-3 wide clips
        if not state["queue"]:
            n_close, n_wide = rng.randint(2, 4), rng.randint(1, 3)
            state["queue"] = ["c"] * n_close + ["w"] * n_wide
        kind = state["queue"].pop(0)
        wide_ok = state["wide_t"] <= 0.22 * max(state["main_t"], 1.0)
        pools = wide_pool if (kind == "w" and wide_ok) else close_pool + ["good_pass_chance"]
        s = foot.take(pools, want, main_cfg["min_clip_s"], main_cfg["max_clip_s"], avoid, "main")
        if s is None and pools is not close_pool:  # pool empty -> try the other one
            s = foot.take(close_pool, want, main_cfg["min_clip_s"], main_cfg["max_clip_s"], avoid, "main")
        if s is None:                               # last resort: any other allowed type
            s = foot.take(filler("main"), want, main_cfg["min_clip_s"], main_cfg["max_clip_s"], avoid, "main")
        if s is None:                               # then the capped-but-legal types, never beyond the template caps
            gc = tpl["constraints"]
            for typ, capn in (("near_miss_shot", 99), ("goal", gc["goal_clips_max"] * 4),
                              ("penalty_goal", gc["penalty_goal_clips_max"] * 4), ("face", gc["face_clips_max"])):
                if sum(1 for x in shots if x["shot_type"] == typ) < capn:
                    s = foot.take([typ], min(want, 3.5), main_cfg["min_clip_s"], 4.5, avoid, "main")
                    if s:
                        break
        return add_main(s)

    fill("main", budgets["main"] - (state["main_t"]), pick_main)

    # ---- outro: after-match / celebration, ending on the tunnel clip when one exists -------
    out_cfg = cons["outro"]
    tunnel = None if (blind or budgets["outro"] < out_cfg["min_clip_s"]) else foot.take(["tunnel_entry"], 3.3, out_cfg["min_clip_s"], out_cfg["max_clip_s"],
                                          None, "outro")

    def pick_outro(left: float, i: int) -> dict | None:
        want = max(out_cfg["min_clip_s"], min(pace("outro", i), left))
        if blind:
            return foot.take(ANY, want, out_cfg["min_clip_s"], out_cfg["max_clip_s"],
                             shots[-1]["source_video_file"] if shots else None, "outro", randomise=True)
        prev = shots[-1]["source_video_file"] if shots else None
        return foot.take(["after_match", "celebration", "player_portrait"], want, out_cfg["min_clip_s"],
                         out_cfg["max_clip_s"], prev, "outro") \
            or foot.take(filler("outro"), want, out_cfg["min_clip_s"], out_cfg["max_clip_s"], prev, "outro")

    fill("outro", budgets["outro"] - (tunnel["duration"] if tunnel else 0.0), pick_outro)
    if tunnel:
        shots.append(tunnel)

    plan, notes = core.repair({"timeline": shots}, ctx)
    errs, warns = core.validate(plan, ctx)
    if errs:
        log.error("Offline planner could not satisfy the rules: %s", "; ".join(errs[:4]))
        got = sum(x["duration"] for x in plan["timeline"])
        if got < target - core.section_budgets(target, tpl)["intro"] * 0 - tpl["constraints"]["total_tolerance_s"]:
            raise NeedsMoreSource(f"planned only {got:.0f}s of {target:.0f}s; supply: {supply_report(cat)}")
        return None
    plan["_notes"] = notes + warns + (["content-blind safe mode: no shot labels were available"] if blind else [])
    return plan


def local_plan_robust(ctx: dict, seed: int = 7, tries: int = 8) -> dict | None:
    """The offline planner is seed-dependent; try a few seeds (deterministic order) before giving up."""
    last: NeedsMoreSource | None = None
    for k in range(tries):
        try:
            plan = local_plan(ctx, seed + k)
        except NeedsMoreSource as exc:
            last = exc
            continue
        if plan:
            if k:
                log.info("Offline planner: seed %d did not satisfy the rules, seed %d did", seed, seed + k)
            return plan
    if last:
        raise last
    return None


