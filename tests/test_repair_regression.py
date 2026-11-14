"""Regression tests from the first real run (pipeline log: both Gemini rounds rejected, offline planner missed the wide cap).
Pure functions only: no media files, no network.  Run: python -m unittest discover -s tests -p "test_repair_regression.py" """
import copy
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import script_planner as sp   # noqa: E402
import timeline_core as core  # noqa: E402

TEMPLATE = ROOT / "config" / "master_rendering_template.json"
# mimics the real catalog: mostly wide, little close-up footage, lots of celebration / goal / face
MIX = ["wide"] * 9 + ["celebration"] * 4 + ["goal"] * 3 + ["face"] * 2 + ["near_miss_shot"] * 3 + ["good_pass_chance"] * 2 + \
      ["duel_1v1", "semi_close", "run_making", "close_skill", "penalty_goal", "fans_crowd", "trophy_ceremony",
       "player_exit_bus_or_tunnel"]


def real_like_ctx(target=171.5):
    tpl = core.load_template(TEMPLATE)
    sources = {f: {"duration": d, "fps": 60.0, "width": 3840, "height": 2160, "has_audio": True, "file": f}
               for f, d in (("s01.mp4", 43.0), ("s02.mp4", 1025.4), ("s03.mp4", 1025.4))}
    rng, cat = random.Random(4), []
    for f, info in sources.items():
        d, t = info["duration"], 1.0
        while t + 3.0 < d - 1:
            cat.append({"source_video_file": f, "start": round(t, 2), "end": round(t + rng.choice([3.0, 6.0, 8.0]), 2),
                        "shot_type": rng.choice(MIX), "quality": 4})
            t = cat[-1]["end"] + rng.choice([0.0, 0.0, 4.0, 12.0])
    return {"template": tpl, "sources": sources, "target_s": target, "catalog": cat}


def wide_share(plan):
    m = [s for s in plan["timeline"] if s["section"] == "main"]
    return sum(s["duration"] for s in m if s["shot_type"] == "wide") / sum(s["duration"] for s in m)


class TestOfflineRobustness(unittest.TestCase):
    def test_wide_heavy_catalog_valid_for_many_seeds(self):
        c = real_like_ctx()
        for seed in range(1, 13):
            plan = sp.local_plan_robust(c, seed)
            self.assertIsNotNone(plan, seed)
            self.assertEqual(core.validate(plan, c)[0], [], seed)
            self.assertLessEqual(wide_share(plan), 0.25, seed)

    def test_prompt_lists_allowed_types_per_section(self):
        _, user = sp.build_prompt(real_like_ctx())
        self.assertIn("ONLY shot_type values allowed", user)
        self.assertIn('"allowed_types"', user)


class TestRepairHandlesGeminiStyleMistakes(unittest.TestCase):
    def test_damaged_plans_are_repaired_to_valid(self):
        c = real_like_ctx()
        base = sp.local_plan_robust(c, 7)
        for trial in range(12):
            rng = random.Random(trial)
            tl = copy.deepcopy(base["timeline"])
            for s in rng.sample(tl, 6):
                r = rng.random()
                if r < 0.3:
                    s["shot_type"] = rng.choice(["face", "goal", "fans_crowd", "wide"])      # wrong label (face in intro...)
                elif r < 0.6:
                    s["trim_start"] = round(s["trim_start"] + rng.uniform(0.3, 2.0), 3)       # window drifts out of its segment
                elif r < 0.8:
                    s["duration"] = round(s["duration"] + rng.uniform(1, 3), 3)
                else:
                    s["trim_start"] = round(rng.uniform(0, 40), 3)                            # window in unlabelled footage
            for s in tl:
                if s["section"] == "main" and rng.random() < 0.25:
                    s["shot_type"] = "wide"
            fixed, _ = core.repair({"timeline": tl}, c)
            self.assertEqual(core.validate(fixed, c)[0], [], trial)

    def test_disallowed_type_is_dropped_not_kept(self):
        c = real_like_ctx()
        plan = sp.local_plan_robust(c, 7)
        tl = copy.deepcopy(plan["timeline"])
        intro_idx = next(i for i, s in enumerate(tl) if s["section"] == "intro")
        tl[intro_idx]["shot_type"] = "goal"
        fixed, notes = core.repair({"timeline": tl}, c)
        self.assertTrue(any("goal" in n and "intro" in n or "relabelled" in n for n in notes))
        self.assertEqual(core.validate(fixed, c)[0], [])

    def test_wide_cap_is_enforced_by_repair(self):
        c = real_like_ctx()
        plan = sp.local_plan_robust(c, 7)
        tl = copy.deepcopy(plan["timeline"])
        for s in tl:
            if s["section"] == "main" and s["shot_type"] in ("close_skill", "semi_close", "duel_1v1", "run_making"):
                s["shot_type"] = "wide"
        fixed, _ = core.repair({"timeline": tl}, c)
        self.assertEqual(core.validate(fixed, c)[0], [])
        self.assertLessEqual(wide_share(fixed), 0.25)


if __name__ == "__main__":
    unittest.main(verbosity=2)
