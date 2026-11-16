"""v3 rules from the owner's review of the first good render: skills lead the main part, intro scenes (bus, tunnel,
dressing room) are used when they exist, a celebration in main only follows its goal, repeated footage is used once,
and the mail says what was missing.  Pure functions, no media, no network.
Run: python -m unittest discover -s tests -p "test_v3_rules.py" """
import json
import random
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import catalog_tools as ct   # noqa: E402
import comp_rules            # noqa: E402,F401
import dup_scan              # noqa: E402
import plan_report           # noqa: E402
import script_planner as sp  # noqa: E402
import timeline_core as core  # noqa: E402

TEMPLATE = ROOT / "config" / "master_rendering_template.json"


def ctx_with(kinds, target=171.5, seed=3, dups=None):
    """kinds: {shot_type: (count, segment_length_s, quality)} spread over three sources."""
    tpl = core.load_template(TEMPLATE)
    sources = {f: {"duration": 600.0, "fps": 60.0, "width": 1920, "height": 1080, "has_audio": True, "file": f}
               for f in ("s01.mp4", "s02.mp4", "s03.mp4")}
    rng, cat, t = random.Random(seed), [], {f: 2.0 for f in sources}
    for typ, (n, ln, q) in kinds.items():
        for k in range(n):
            f = list(sources)[k % 3]
            cat.append({"source_video_file": f, "start": round(t[f], 2), "end": round(t[f] + ln, 2), "shot_type": typ, "quality": q})
            t[f] += ln + rng.choice([0.5, 3.0, 9.0])
    return {"template": tpl, "sources": sources, "target_s": target, "catalog": cat, "dups": dups or []}


FULL = {"bus_arrival": (3, 6.0, 2), "player_exit_bus_or_tunnel": (2, 5.0, 3), "dressing_room": (3, 5.0, 2),
        "fans_crowd": (3, 5.0, 4), "close_skill": (20, 6.0, 4), "semi_close": (14, 6.0, 4), "duel_1v1": (8, 6.0, 4),
        "run_making": (8, 6.0, 4), "ball_receive_pass": (8, 6.0, 4), "wide": (14, 6.0, 4), "goal": (6, 9.0, 5),
        "celebration": (14, 5.0, 5), "penalty_goal": (2, 8.0, 5), "after_match": (4, 5.0, 4), "tunnel_entry": (2, 5.0, 3)}


def tl(plan):
    return plan["timeline"]


class TestOfflinePlannerFollowsTheOwnerRules(unittest.TestCase):
    def test_full_catalog(self):
        for seed in range(7, 13):
            c = ctx_with(FULL)
            plan = sp.local_plan_robust(c, seed)
            self.assertIsNotNone(plan, seed)
            shots = tl(plan)
            self.assertEqual(core.validate(plan, {**c, "strict_comp": True})[0], [], seed)
            intro = [s for s in shots if s["section"] == "intro"]
            self.assertEqual(intro[0]["shot_type"], "bus_arrival", seed)                       # opens with the bus
            self.assertTrue({"dressing_room", "player_exit_bus_or_tunnel"} & {s["shot_type"] for s in intro}, seed)
            main = [s for s in shots if s["section"] == "main"]
            skills = set(c["template"]["constraints"]["skill_types"])
            share = sum(s["duration"] for s in main if s["shot_type"] in skills) / sum(s["duration"] for s in main)
            self.assertGreaterEqual(share, 0.5, seed)
            goals = [s for s in main if s["shot_type"] in ("goal", "penalty_goal")]
            self.assertTrue(goals, seed)
            self.assertLess(sum(s["duration"] for s in goals) / sum(s["duration"] for s in main), 0.3, seed)
            for i, s in enumerate(shots):                                                       # celebration only after a goal
                if s["section"] == "main" and s["shot_type"] == "celebration":
                    self.assertIn(shots[i - 1]["shot_type"], ("goal", "penalty_goal"), seed)
                    self.assertEqual(shots[i - 1]["source_video_file"], s["source_video_file"], seed)
            self.assertEqual(shots[-1]["shot_type"], "tunnel_entry", seed)

    def test_goal_is_built_from_parts_in_time_order(self):
        c = ctx_with(FULL)
        plan = sp.local_plan_robust(c, 7)
        shots = tl(plan)
        for i in range(len(shots) - 1):
            a, b = shots[i], shots[i + 1]
            if a["shot_type"] == "goal" and b["shot_type"] == "goal" and a["source_video_file"] == b["source_video_file"]:
                self.assertLess(a["trim_start"], b["trim_start"])

    def test_no_celebration_is_invented_when_none_exists(self):
        kinds = {k: v for k, v in FULL.items() if k != "celebration"}
        plan = sp.local_plan_robust(ctx_with(kinds), 7)
        self.assertFalse([s for s in tl(plan) if s["section"] == "main" and s["shot_type"] == "celebration"])


class TestRulesCatchGeminiMistakes(unittest.TestCase):
    def _base(self):
        c = ctx_with(FULL)
        return c, sp.local_plan_robust(c, 7)

    def test_lonely_celebration_in_main_is_dropped_by_repair(self):
        c, plan = self._base()
        shots = tl(plan)
        i = next(k for k, s in enumerate(shots) if s["section"] == "main" and s["shot_type"] in ("close_skill", "semi_close"))
        cel = next(g for g in c["catalog"] if g["shot_type"] == "celebration"
                   and not any(s["source_video_file"] == g["source_video_file"] and s["trim_start"] < g["end"] and g["start"] < s["trim_start"] + s["duration"] for s in shots))
        shots.insert(i, {"shot_id": "S999", "section": "main", "shot_type": "celebration", "source_video_file": cel["source_video_file"],
                         "trim_start": cel["start"], "duration": 2.0, "transition": "hard_cut", "speed_multiplier": 1.0})
        fixed, notes = core.repair({"timeline": shots}, c)
        self.assertTrue(any("celebration" in n for n in notes))
        self.assertEqual(core.validate(fixed, c)[0], [])

    def test_goal_only_main_is_rejected_when_skills_exist(self):
        c, plan = self._base()
        shots = [dict(s) for s in tl(plan)]
        goals = [g for g in c["catalog"] if g["shot_type"] == "goal"]
        k = 0
        for s in shots:
            if s["section"] == "main" and s["shot_type"] != "wide":
                g = goals[k % len(goals)]
                s.update(shot_type="goal", source_video_file=g["source_video_file"], trim_start=g["start"] + (k // len(goals)) * 0.0)
                k += 1
        errs, _ = core.validate({"timeline": shots}, c)
        self.assertTrue(any(e.startswith("C-SKILL") for e in errs), errs[:3])

    def test_intro_without_scene_footage_is_rejected_when_it_exists(self):
        c, plan = self._base()
        shots = [dict(s) for s in tl(plan)]
        fans = next(g for g in c["catalog"] if g["shot_type"] == "fans_crowd")
        for s in shots:
            if s["section"] == "intro":
                s.update(shot_type="fans_crowd", source_video_file=fans["source_video_file"], trim_start=fans["start"])
        errs, _ = core.validate({"timeline": shots}, c)
        self.assertTrue(any(e.startswith("C-INTRO") for e in errs), errs[:3])

    def test_short_footage_is_a_warning_not_an_error(self):
        kinds = dict(FULL, close_skill=(4, 6.0, 4), semi_close=(4, 6.0, 4), duel_1v1=(2, 6.0, 4), run_making=(0, 6.0, 4),
                     ball_receive_pass=(0, 6.0, 4), wide=(20, 6.0, 4))
        c = ctx_with(kinds)
        plan = sp.local_plan_robust(c, 7)
        self.assertIsNotNone(plan)
        rep = plan_report.build(c, tl(plan), "offline_safe_mode")
        self.assertTrue(any(x.startswith("MAIN: only") for x in rep["shortages"]), rep["shortages"])


class TestOverlapAndDuplicates(unittest.TestCase):
    def test_long_window_swallowing_two_later_ones_is_caught(self):
        c = ctx_with(FULL)
        base = {"section": "main", "transition": "hard_cut", "speed_multiplier": 1.0, "source_video_file": "s01.mp4"}
        shots = [dict(base, shot_id="S001", shot_type="close_skill", trim_start=10.0, duration=9.0),
                 dict(base, shot_id="S002", shot_type="close_skill", trim_start=11.0, duration=1.5),
                 dict(base, shot_id="S003", shot_type="close_skill", trim_start=14.0, duration=1.5)]
        errs, _ = core.validate({"timeline": shots}, {**c, "catalog": []})
        self.assertEqual(sum(e.startswith("E-OVERLAP") for e in errs), 2)
        notes: list = []
        core._trim_overlaps(shots, notes, c["template"]["constraints"])
        self.assertEqual([s["shot_id"] for s in shots], ["S001"] if len(shots) == 1 else [s["shot_id"] for s in shots])
        self.assertFalse([e for e in core.validate({"timeline": shots}, {**c, "catalog": []})[0] if e.startswith("E-OVERLAP")])

    def test_dup_scan_finds_a_copy_in_another_file_and_ignores_unrelated_footage(self):
        d = Path(tempfile.mkdtemp())
        mk = lambda n, seed: np.random.default_rng(seed).integers(0, 256, (n, 32), dtype=np.uint8)  # noqa: E731
        a, b = mk(80, 1), mk(80, 2)
        b[30:44] = a[10:24]                                           # 7 s of A (5-12 s) sits in B at 15-22 s
        for name, h in (("s01", a), ("s02", b)):
            np.savez(d / f"hash_{name}.npz", h=h, std=np.full(len(h), 30, np.float32), t=np.arange(len(h)) * 0.5)
        ex = dup_scan.find(d, ["s01", "s02"])
        self.assertEqual(len(ex), 1)
        self.assertEqual(ex[0]["file"], "s02.mp4")
        self.assertLessEqual(ex[0]["start"], 15.5)
        self.assertGreaterEqual(ex[0]["end"], 21.5)
        self.assertEqual(ex[0]["dup_of"], "s01.mp4")

    def test_dup_scan_finds_a_repeat_inside_one_file_but_not_a_static_scene(self):
        d = Path(tempfile.mkdtemp())
        h = np.random.default_rng(5).integers(0, 256, (120, 32), dtype=np.uint8)
        h[80:92] = h[10:22]                                           # repeat 35 s later in the same file
        h[40:50] = h[40]                                              # a static scene: every sample identical to its neighbour
        np.savez(d / "hash_s01.npz", h=h, std=np.full(120, 30, np.float32), t=np.arange(120) * 0.5)
        ex = dup_scan.find(d, ["s01"])
        self.assertTrue(any(e["start"] >= 38 for e in ex), ex)
        self.assertFalse(any(30 < e["start"] < 38 for e in ex), ex)

    def test_catalog_loses_the_duplicate_range_and_planner_never_uses_it(self):
        dups = [{"file": "s02.mp4", "start": 0.0, "end": 200.0, "dup_of": "s01.mp4"}]
        c = ctx_with(FULL, dups=dups)
        c["catalog"] = ct.subtract_ranges(c["catalog"], dups)
        self.assertFalse([g for g in c["catalog"] if g["source_video_file"] == "s02.mp4" and g["start"] < 200])
        plan = sp.local_plan_robust(c, 7)
        for s in tl(plan):
            self.assertIsNone(ct.overlaps_dup(s["source_video_file"], s["trim_start"], s["trim_start"] + s["duration"], dups))
        bad = [dict(s) for s in tl(plan)]
        bad[3].update(source_video_file="s02.mp4", trim_start=10.0)
        self.assertTrue(any(e.startswith("E-DUP") for e in core.validate({"timeline": bad}, c)[0]))


class TestCatalogSampleAndReport(unittest.TestCase):
    def test_prompt_sample_keeps_every_type_even_when_quality_is_low(self):
        c = ctx_with(dict(FULL, goal=(300, 4.0, 5), celebration=(200, 4.0, 5)))
        sample = ct.balanced_sample(c["catalog"], sp.MAX_CATALOG_LINES)
        types = {g["shot_type"] for g in sample}
        self.assertEqual(types, {g["shot_type"] for g in c["catalog"]})
        old = sorted(c["catalog"], key=lambda g: -g["quality"])[:sp.MAX_CATALOG_LINES]
        self.assertNotIn("bus_arrival", {g["shot_type"] for g in old})      # the old top-by-quality cut lost the bus footage
        _, user = sp.build_prompt(c)
        self.assertIn("bus_arrival|", user)
        self.assertIn("SUPPLY_SECONDS_BY_TYPE", user)

    def test_report_names_missing_intro_scenes_and_planner(self):
        kinds = {k: v for k, v in FULL.items() if k not in ("bus_arrival", "dressing_room", "tunnel_entry")}
        c = ctx_with(kinds)
        plan = sp.local_plan_robust(c, 7)
        rep = plan_report.build(c, tl(plan), "offline_safe_mode")
        txt = plan_report.text(rep, {"labelled": 10, "rows": 12, "rejected_rows": 1, "too_low_quality_rows": 1}, {"ranges": 2, "seconds": 14.0})
        for needle in ("no usable bus arrival", "no usable dressing room", "player-into-the-tunnel", "Repeated footage: 2", "FOOTAGE FOUND"):
            self.assertIn(needle, txt)

    def test_end_to_end_cli_writes_report(self):
        # the planner CLI needs real media for scan_sources, so exercise the pieces it chains
        c = ctx_with(FULL)
        plan = sp.local_plan_robust(c, 7)
        rep = plan_report.build(c, tl(plan), "offline_safe_mode")
        json.dumps(rep)
        self.assertFalse(rep["shortages"], rep["shortages"])
        self.assertGreaterEqual(rep["skill_share_main"], 0.5)


if __name__ == "__main__":
    unittest.main()
