"""
Run:  python -m unittest discover -s tests -v      (from the repository root)

Gemini is MOCKED here: these tests prove the control flow (fallback order, JSON handling, validation,
repair, offline planner, rendering). They do not prove that any specific model name exists on the live API.
Render tests use LVG_RENDER_PRESET=ultrafast to stay fast; every spec check (size, fps, frames, bars) is still real.
"""
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import render_video as rv          # noqa: E402
import script_planner as sp        # noqa: E402
import classify_shots as cs        # noqa: E402
import timeline_core as core       # noqa: E402

TEMPLATE = ROOT / "config" / "master_rendering_template.json"
MEDIA = Path(tempfile.gettempdir()) / "lvg_test_media"


def _ff(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def setUpModule():
    """Three source clips of different shapes (16:9 30fps, 9:16 25fps, 16:9 4:3-ish 60fps) + a 120 BPM music bed."""
    if (MEDIA / "music.m4a").exists():
        return
    (MEDIA / "src").mkdir(parents=True, exist_ok=True)
    enc = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-pix_fmt", "yuv420p"]
    _ff("-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=30:d=60", *enc, str(MEDIA / "src" / "s01.mp4"))
    _ff("-f", "lavfi", "-i", "testsrc2=s=1080x1920:r=25:d=50", *enc, str(MEDIA / "src" / "s02.mp4"))
    _ff("-f", "lavfi", "-i", "testsrc2=s=1440x1080:r=60:d=45", *enc, str(MEDIA / "src" / "s03.mp4"))
    _ff("-f", "lavfi", "-i", "aevalsrc='0.6*sin(2*PI*70*t)*exp(-9*mod(t,0.5))+0.1*sin(2*PI*330*t)':d=40.7:s=44100",
        "-c:a", "aac", "-b:a", "128k", str(MEDIA / "music.m4a"))


def sources():
    s, _ = core.scan_sources(MEDIA / "src")
    return s


def ctx(target=40.5, catalog=None):
    return {"template": core.load_template(TEMPLATE), "sources": sources(), "target_s": target, "catalog": catalog or []}


def good_plan(c):
    """A hand-built valid plan for target 40.5 s: intro 4.37, main 32.38.., outro 3.5 (budgets taken from the template)."""
    b = core.section_budgets(c["target_s"], c["template"])
    shots, files = [], ["s01.mp4", "s02.mp4", "s03.mp4"]

    def add(section, typ, dur, n, trans="hard_cut"):
        shots.append({"shot_id": f"S{len(shots) + 1:03d}", "section": section, "shot_type": typ,
                      "source_video_file": files[len(shots) % 3], "trim_start": float(4 * n), "duration": dur,
                      "transition": trans, "speed_multiplier": 1.0})
    add("intro", "unclassified", b["intro"] / 2, 0)
    add("intro", "unclassified", b["intro"] / 2, 1)
    add("main", "wide", 0.8, 2, "zoom_punch_cut")   # the short wide opener seen in the reference
    left = b["main"] - 0.8
    n = 3
    while left > 0.01:
        d = round(min(3.0, left), 3)
        if d < 1.2:                      # fold the remainder into the previous clip
            shots[-1]["duration"] = round(shots[-1]["duration"] + d, 3)
            break
        add("main", "unclassified", d, n)
        left = round(left - d, 3)
        n += 1
    add("outro", "unclassified", b["outro"], n + 1)
    # fix rounding dust so the total is exact
    shots[-1]["duration"] = round(shots[-1]["duration"] + (c["target_s"] - sum(s["duration"] for s in shots)), 3)
    # spread trim_start so windows never overlap inside one source
    seen = {}
    for s in shots:
        f = s["source_video_file"]
        s["trim_start"] = round(seen.get(f, 0.5), 3)
        seen[f] = s["trim_start"] + s["duration"] + 0.3
    return {"timeline": shots}


class TestBudgets(unittest.TestCase):
    def test_real_song_budget(self):
        tpl = core.load_template(TEMPLATE)
        t = core.target_length(171.54)
        self.assertEqual(t, 171.5)
        b = core.section_budgets(t, tpl)
        self.assertAlmostEqual(sum(b.values()), 171.5, places=3)
        self.assertTrue(15 <= b["intro"] <= 20)
        self.assertTrue(10 <= b["outro"] <= 18)

    def test_under_30s_is_main_only_and_plannable(self):
        tpl = core.load_template(TEMPLATE)
        self.assertEqual(core.section_budgets(12.0, tpl), {"intro": 0.0, "main": 12.0, "outro": 0.0})
        c = ctx(target=12.0)
        plan = sp.local_plan(c, seed=1)
        self.assertIsNotNone(plan)
        self.assertEqual({s["section"] for s in plan["timeline"]}, {"main"})
        self.assertEqual(core.validate(plan, c)[0], [])

    def test_short_video_has_no_forced_15s_intro(self):
        tpl = core.load_template(TEMPLATE)
        self.assertLess(core.section_budgets(60.0, tpl)["intro"], 15)


class TestValidator(unittest.TestCase):
    def setUp(self):
        self.c = ctx()
        self.plan = good_plan(self.c)

    def test_good_plan_is_valid(self):
        errs, _ = core.validate(self.plan, self.c)
        self.assertEqual(errs, [])

    def _errs(self, mutate):
        p = copy.deepcopy(self.plan)
        mutate(p["timeline"])
        return core.validate(p, self.c)[0]

    def test_hallucinated_file(self):
        e = self._errs(lambda t: t[1].update(source_video_file="ghost.mp4"))
        self.assertTrue(any(x.startswith("E-SRC") for x in e))

    def test_window_past_end_of_source(self):
        e = self._errs(lambda t: t[1].update(trim_start=59.0))
        self.assertTrue(any(x.startswith("E-WINDOW") for x in e))

    def test_bad_id_unknown_key_and_total(self):
        e = self._errs(lambda t: t[0].update(shot_id="S9"))
        self.assertTrue(any(x.startswith("E-ID") for x in e))
        e = self._errs(lambda t: t[0].update(bonus="x"))
        self.assertTrue(any("unknown keys" in x for x in e))
        e = self._errs(lambda t: t[-1].update(duration=t[-1]["duration"] + 1.0))
        self.assertTrue(any(x.startswith("E-TOTAL") for x in e))

    def test_zoom_punch_only_on_first_main_shot(self):
        e = self._errs(lambda t: t[4].update(transition="zoom_punch_cut"))
        self.assertTrue(any(x.startswith("E-TRANSITION") for x in e))

    def test_overlap_and_section_order(self):
        def mut(t):
            t[3]["source_video_file"], t[3]["trim_start"] = t[2]["source_video_file"], t[2]["trim_start"] + 0.1
        self.assertTrue(any(x.startswith("E-OVERLAP") for x in self._errs(mut)))
        def swap(t):
            t[0]["section"], t[-1]["section"] = "outro", "intro"
        self.assertTrue(any(x.startswith("E-ORDER") for x in self._errs(swap)))

    def test_wide_cap(self):
        def mut(t):
            for s in t:
                if s["section"] == "main":
                    s["shot_type"] = "wide"
        self.assertTrue(any(x.startswith("E-WIDE") for x in self._errs(mut)))

    def test_catalog_containment(self):
        cat = [{"source_video_file": "s01.mp4", "start": 0, "end": 5, "shot_type": "close_skill", "quality": 4}]
        c = ctx(catalog=cat)
        p = copy.deepcopy(self.plan)
        p["timeline"][2].update(shot_type="close_skill", source_video_file="s01.mp4", trim_start=20.0)
        errs, _ = core.validate(p, c)
        self.assertTrue(any(x.startswith("E-CATALOG") for x in errs))


class TestRepair(unittest.TestCase):
    def test_repair_drops_ghosts_fits_budget_and_renumbers(self):
        c = ctx()
        p = good_plan(c)
        p["timeline"][1]["source_video_file"] = p["timeline"][1]["source_video_file"].upper()   # case slip -> fixed (exact base-name match)
        p["timeline"].insert(3, {**p["timeline"][3], "source_video_file": "nope.mp4"})   # ghost -> dropped
        p["timeline"][5]["duration"] += 0.9                           # budget error -> fitted
        p["timeline"][2]["transition"] = "spin_zoom_9000"             # junk transition -> hard_cut
        fixed, notes = core.repair(p, c)
        errs, _ = core.validate(fixed, c)
        self.assertEqual(errs, [], errs)
        self.assertTrue(any("unknown source" in n for n in notes))
        self.assertEqual([s["shot_id"] for s in fixed["timeline"]],
                         [f"S{i + 1:03d}" for i in range(len(fixed["timeline"]))])


class FakeModel:
    def __init__(self, name):
        self.name = "models/" + name


class FakeClient:
    def __init__(self, served):
        self.models = self
        self._served = served

    def list(self):
        return [FakeModel(m) for m in self._served]


class FakeAPIError(Exception):
    def __init__(self, code, msg="x"):
        super().__init__(msg)
        self.code = code


class TestGeminiChain(unittest.TestCase):
    def setUp(self):
        self.c = ctx()
        self.valid = json.dumps(good_plan(self.c))
        self.sleeps = []

    def run_chain(self, behaviour, served=None):
        calls = []

        def caller(client, model, system, prompt):
            calls.append(model)
            return behaviour(model, len(calls), prompt)

        served = served if served is not None else sp.MODEL_FALLBACK_CHAIN
        plan, rep = sp.plan_with_gemini(self.c, FakeClient(served), sp.MODEL_FALLBACK_CHAIN, caller=caller,
                                        sleep=self.sleeps.append)
        return plan, rep, calls

    def test_chain_is_exactly_the_ten_requested_models_in_order(self):
        self.assertEqual(sp.MODEL_FALLBACK_CHAIN, [
            "gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
            "gemini-3-flash-preview", "gemini-1.5-flash", "gemini-1.5-pro", "gemini-2.5-flash-lite",
            "gemini-2.5-flash-preview", "gemini-2.0-flash-lite"])

    def test_primary_wins_when_healthy(self):
        plan, rep, calls = self.run_chain(lambda m, n, p: self.valid)
        self.assertEqual(calls, ["gemini-3.8-flash"])
        self.assertEqual(rep["model_used"], "gemini-3.8-flash")
        self.assertIsNotNone(plan)

    def test_walks_down_the_chain_on_errors(self):
        def behave(m, n, p):
            if m == "gemini-3.8-flash":
                raise FakeAPIError(503, "model overloaded")
            if m == "gemini-3.6-flash":
                raise TimeoutError("read timed out")
            if m == "gemini-3.5-flash-lite":
                raise FakeAPIError(429, "RESOURCE_EXHAUSTED")
            if m == "gemini-3.1-flash-lite":
                raise FakeAPIError(404, "model not found")
            return self.valid
        plan, rep, calls = self.run_chain(behave)
        self.assertEqual(calls[:5], ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite",
                                     "gemini-3.1-flash-lite", "gemini-3-flash-preview"])
        self.assertEqual(rep["model_used"], "gemini-3-flash-preview")

    def test_models_not_served_for_the_key_are_skipped_without_a_call(self):
        served = [m for m in sp.MODEL_FALLBACK_CHAIN if "1.5" not in m and "3.8" not in m]
        plan, rep, calls = self.run_chain(lambda m, n, p: self.valid, served=served)
        self.assertEqual(calls, ["gemini-3.6-flash"])
        self.assertIn("gemini-3.8-flash", rep["not_served"])

    def test_fixable_mistakes_are_repaired_without_another_call(self):
        bad = json.loads(self.valid)
        bad["timeline"][2]["source_video_file"] = "invented_clip.mp4"     # ghost file -> dropped
        bad["timeline"][4]["duration"] = 99.0                             # absurd length -> clamped and refitted
        plan, rep, calls = self.run_chain(lambda m, n, p: json.dumps(bad))
        self.assertEqual(calls, ["gemini-3.8-flash"])
        self.assertTrue(any("unknown source" in r for r in rep["repairs"]))
        self.assertEqual(core.validate(plan, self.c)[0], [])

    def test_unfixable_answer_gets_one_repair_round_with_the_errors_in_the_prompt(self):
        bad = json.loads(self.valid)
        for s in bad["timeline"]:
            if s["section"] == "main":
                s["shot_type"] = "wide"                                   # breaks the 25% wide cap; repair must not invent a fix

        def behave(m, n, prompt):
            if n == 1:
                return json.dumps(bad)
            self.assertIn("PREVIOUS ANSWER WAS REJECTED", prompt)
            self.assertIn("E-WIDE", prompt)
            return self.valid
        plan, rep, calls = self.run_chain(behave)
        self.assertEqual(calls, ["gemini-3.8-flash", "gemini-3.8-flash"])
        self.assertIsNotNone(plan)

    def test_garbage_json_then_next_model(self):
        plan, rep, calls = self.run_chain(lambda m, n, p: "I am sorry, here is your plan: not json at all"
                                          if m == "gemini-3.8-flash" else self.valid)
        self.assertEqual(rep["model_used"], "gemini-3.6-flash")

    def test_everything_fails_returns_none(self):
        def behave(m, n, p):
            raise FakeAPIError(503, "unavailable")
        plan, rep, calls = self.run_chain(behave)
        self.assertIsNone(plan)
        self.assertGreaterEqual(len(calls), 10)         # every model tried (plus the single transient retry pass)
        self.assertIn(20.0, self.sleeps)                # waited once before the retry pass

    def test_parse_json_tolerates_code_fences_and_bare_arrays(self):
        self.assertEqual(sp.parse_json('```json\n{"timeline": []}\n```'), {"timeline": []})
        self.assertEqual(sp.parse_json('[{"a": 1}]'), {"timeline": [{"a": 1}]})

    def test_error_classification(self):
        self.assertEqual(sp.classify_error(FakeAPIError(404)), "skip")
        self.assertEqual(sp.classify_error(FakeAPIError(429)), "quota")
        self.assertEqual(sp.classify_error(FakeAPIError(503)), "transient")
        self.assertEqual(sp.classify_error(TimeoutError()), "transient")
        class ConnectError(Exception):          # same class name the HTTP client raises when there is no route
            pass
        self.assertEqual(sp.classify_error(ConnectError("no route")), "transient")

    def test_real_sdk_objects_build_without_network(self):
        """Catches wrong parameter names against the installed google-genai package."""
        os.environ["GEMINI_API_KEY"] = "dummy-not-a-real-key"
        try:
            self.assertEqual(type(sp.make_client()).__name__, "Client")
            from google.genai import types
            cfg = types.GenerateContentConfig(system_instruction="x", response_mime_type="application/json",
                                              temperature=0.3, max_output_tokens=16384,
                                              http_options=types.HttpOptions(timeout=75000))
            self.assertEqual(cfg.response_mime_type, "application/json")
        finally:
            os.environ.pop("GEMINI_API_KEY", None)


class TestOfflinePlanner(unittest.TestCase):
    def test_blind_mode_is_valid_and_deterministic(self):
        c = ctx()
        a = sp.local_plan(c, seed=3)
        b = sp.local_plan(c, seed=3)
        self.assertIsNotNone(a)
        self.assertEqual(core.validate(a, c)[0], [])
        self.assertEqual(a["timeline"], b["timeline"])

    def test_catalog_mode_follows_the_rulebook(self):
        types = ["bus_arrival", "dressing_room", "wide", "close_skill", "semi_close", "wide", "duel_1v1", "goal",
                 "close_skill", "run_making", "wide", "face", "celebration", "tunnel_entry", "after_match",
                 "close_skill", "wide", "ball_receive_pass", "semi_close", "close_skill", "duel_1v1", "wide"]
        cat = []
        for k, (f, d) in enumerate((("s01.mp4", 60), ("s02.mp4", 50), ("s03.mp4", 45))):
            t, i = 0.5, k * 5                                   # rotate the type order per source
            while t + 3.2 < d:
                cat.append({"source_video_file": f, "start": t, "end": t + 3.0, "shot_type": types[i % len(types)],
                            "quality": 4})
                t, i = t + 3.2, i + 1
        c = ctx(catalog=cat)
        plan = sp.local_plan(c, seed=5)
        self.assertIsNotNone(plan)
        errs, _ = core.validate(plan, c)
        self.assertEqual(errs, [], errs)
        tl = plan["timeline"]
        self.assertEqual(tl[0]["section"], "intro")
        self.assertIn(tl[0]["shot_type"], ("bus_arrival", "player_exit_bus_or_tunnel"))
        first_main = next(s for s in tl if s["section"] == "main")
        self.assertEqual(first_main["shot_type"], "wide")
        self.assertEqual(first_main["transition"], "zoom_punch_cut")
        self.assertEqual(tl[-1]["shot_type"], "tunnel_entry")


class TestFilters(unittest.TestCase):
    """The owner's literal filter vs the corrected one, on all three aspect ratios."""

    def bars(self, vf, src):
        png = MEDIA / "f.png"
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", "1", "-i", str(MEDIA / "src" / src),
                            "-vf", vf, "-frames:v", "1", str(png)], capture_output=True, text=True)
        if r.returncode:
            return None
        import cv2
        im = cv2.imread(str(png))
        rows = im.mean(axis=(1, 2))
        top = int((rows[:200] < 6).argmin()) if rows[0] < 6 else 0
        return im.shape[1], im.shape[0], top

    def test_template_filter_gives_bars_on_every_shape(self):
        tpl = core.load_template(TEMPLATE)
        fixed = tpl["render_spec"]["letterbox"]["filter"]
        for src in ("s01.mp4", "s02.mp4", "s03.mp4"):
            w, h, top = self.bars(fixed, src)
            self.assertEqual((w, h, top), (1920, 1080, 60), src)

    def test_literal_filter_is_broken_as_documented(self):
        tpl = core.load_template(TEMPLATE)
        literal = tpl["render_spec"]["letterbox"]["filter_as_written"]
        w, h, top = self.bars(literal, "s01.mp4")
        self.assertEqual(top, 0)                      # 16:9 source: no bars at all
        self.assertIsNone(self.bars(literal, "s02.mp4"))   # vertical source: ffmpeg errors out


class TestEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = {"LVG_RENDER_PRESET": "ultrafast", "LVG_RENDER_CRF": "23"}
        os.environ.update(cls.env)

    @classmethod
    def tearDownClass(cls):
        for k in cls.env:
            os.environ.pop(k, None)

    def _plan(self, tmp, extra=()):
        out = Path(tmp) / "timeline.json"
        rc = sp.main(["--template", str(TEMPLATE), "--sources-dir", str(MEDIA / "src"), "--music", str(MEDIA / "music.m4a"),
                      "--out", str(out), "--no-ai", *extra])
        self.assertEqual(rc, 0)
        return out

    def test_plan_then_render_matches_the_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            tl = self._plan(tmp)
            doc = json.loads(tl.read_text())
            self.assertEqual(doc["target_duration_s"], 40.5)
            self.assertEqual(doc["_planner"]["planner"], "offline_safe_mode")
            out = Path(tmp) / "final.mp4"
            rc = rv.main(["--template", str(TEMPLATE), "--timeline", str(tl), "--sources-dir", str(MEDIA / "src"),
                          "--music", str(MEDIA / "music.m4a"), "--out", str(out), "--no-beats"])
            self.assertEqual(rc, 0)
            rep = json.loads(Path(str(out) + ".report.json").read_text())
            v = rep["verified"]
            self.assertEqual((v["width"], v["height"], v["fps"]), (1920, 1080, 60.0))
            self.assertEqual(v["frames"], round(40.5 * 60))
            self.assertAlmostEqual(v["duration_s"], 40.5, delta=0.1)
            # letterbox: bars really are black at first, middle and last frame; picture region is not
            import cv2
            cap = cv2.VideoCapture(str(out))
            for fidx in (5, 1200, v["frames"] - 3):
                cap.set(cv2.CAP_PROP_POS_FRAMES, fidx)
                ok, f = cap.read()
                self.assertTrue(ok)
                self.assertLess(f[:58].mean(), 8, f"top bar not black at frame {fidx}")
                self.assertLess(f[1022:].mean(), 8, f"bottom bar not black at frame {fidx}")
                self.assertGreater(f[200:900].mean(), 15, f"picture missing at frame {fidx}")
            # audio present and ends with the video
            self.assertTrue(core.probe_media(out)["has_audio"])

    def test_zoom_punch_blur_is_applied_on_the_tail_before_the_main_opener(self):
        import cv2
        c = ctx()
        plan = good_plan(c)
        with tempfile.TemporaryDirectory() as tmp:
            doc = {"target_duration_s": 40.5, "timeline": plan["timeline"]}
            tl = Path(tmp) / "t.json"
            tl.write_text(json.dumps(doc))
            out = Path(tmp) / "p.mp4"
            rc = rv.main(["--template", str(TEMPLATE), "--timeline", str(tl), "--sources-dir", str(MEDIA / "src"),
                          "--music", str(MEDIA / "music.m4a"), "--out", str(out), "--no-beats"])
            self.assertEqual(rc, 0)
            rep = json.loads(Path(str(out) + ".report.json").read_text())
            self.assertEqual(rep["punch_transitions"], 1)
            cut = round(sum(s["duration"] for s in plan["timeline"] if s["section"] == "intro") * 60)
            cap = cv2.VideoCapture(str(out))

            def sharp(i):
                cap.set(cv2.CAP_PROP_POS_FRAMES, i)
                ok, f = cap.read()
                return cv2.Laplacian(cv2.cvtColor(f[100:980], cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()
            normal, tail_last = sharp(cut - 40), sharp(cut - 2)
            self.assertLess(tail_last, 0.6 * normal, "last frames before the cut should be blurred")

    def test_render_refuses_a_bad_timeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            tl = self._plan(tmp)
            doc = json.loads(tl.read_text())
            doc["timeline"][1]["source_video_file"] = "../../etc/passwd"
            bad = Path(tmp) / "bad.json"
            bad.write_text(json.dumps(doc))
            rc = rv.main(["--template", str(TEMPLATE), "--timeline", str(bad), "--sources-dir", str(MEDIA / "src"),
                          "--music", str(MEDIA / "music.m4a"), "--out", str(Path(tmp) / "x.mp4")])
            self.assertEqual(rc, 2)
            self.assertFalse((Path(tmp) / "x.mp4").exists())

    def test_beat_snap_keeps_length_and_validity(self):
        with tempfile.TemporaryDirectory() as tmp:
            tl = self._plan(tmp)
            out = Path(tmp) / "snap.mp4"
            rc = rv.main(["--template", str(TEMPLATE), "--timeline", str(tl), "--sources-dir", str(MEDIA / "src"),
                          "--music", str(MEDIA / "music.m4a"), "--out", str(out), "--beat-snap"])
            self.assertEqual(rc, 0)
            rep = json.loads(Path(str(out) + ".report.json").read_text())
            self.assertIsNotNone(rep["beat_analysis"])
            self.assertEqual(rep["verified"]["frames"], round(40.5 * 60))
            self.assertGreater(rep["beat_snap_moves"], 0, "expected at least one cut to move onto a beat")

    def test_no_letterbox_switch_gives_full_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            tl = self._plan(tmp)
            out = Path(tmp) / "full.mp4"
            rc = rv.main(["--template", str(TEMPLATE), "--timeline", str(tl), "--sources-dir", str(MEDIA / "src"),
                          "--music", str(MEDIA / "music.m4a"), "--out", str(out), "--no-beats", "--no-letterbox"])
            self.assertEqual(rc, 0)
            import cv2
            cap = cv2.VideoCapture(str(out))
            cap.set(cv2.CAP_PROP_POS_FRAMES, 300)
            ok, f = cap.read()
            self.assertGreater(f[:40].mean(), 8)       # no black bar at the top

    def test_needs_more_source_halts_with_exit_3(self):
        with tempfile.TemporaryDirectory() as tmp:
            one = Path(tmp) / "src"
            one.mkdir()
            shutil.copy(MEDIA / "src" / "s03.mp4", one / "only.mp4")       # 45 s of footage...
            long_music = Path(tmp) / "long.m4a"
            _ff("-f", "lavfi", "-i", "sine=frequency=300:duration=120", "-c:a", "aac", str(long_music))  # ...for 120 s
            rc = sp.main(["--template", str(TEMPLATE), "--sources-dir", str(one), "--music", str(long_music),
                          "--out", str(Path(tmp) / "t.json"), "--no-ai"])
            self.assertEqual(rc, 3)

    def test_missing_api_key_falls_back_offline_and_require_ai_fails(self):
        os.environ.pop("GEMINI_API_KEY", None)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "t.json"
            base = ["--template", str(TEMPLATE), "--sources-dir", str(MEDIA / "src"), "--music", str(MEDIA / "music.m4a"),
                    "--out", str(out)]
            self.assertEqual(sp.main(base), 0)
            self.assertEqual(json.loads(out.read_text())["_planner"]["planner"], "offline_safe_mode")
            self.assertEqual(sp.main(base + ["--require-ai"]), 4)


class TestReferenceExcluded(unittest.TestCase):
    """Bug fix: ref.mp4 (the style reference saved by the fetch stage) must never become footage."""

    def test_ref_is_skipped_by_default(self):
        d = Path(tempfile.mkdtemp())
        try:
            shutil.copy(MEDIA / "src" / "s01.mp4", d / "s01.mp4")
            shutil.copy(MEDIA / "src" / "s01.mp4", d / "ref.mp4")
            found, skipped = core.scan_sources(d)
            self.assertEqual(list(found), ["s01.mp4"])
            self.assertTrue(any("ref.mp4" in s for s in skipped))
            found2, _ = core.scan_sources(d, exclude=[])          # explicit opt-out still possible
            self.assertEqual(sorted(found2), ["ref.mp4", "s01.mp4"])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_planner_never_uses_ref(self):
        d = Path(tempfile.mkdtemp())
        try:
            for n in ("s01.mp4", "s02.mp4", "ref.mp4"):
                shutil.copy(MEDIA / "src" / ("s01.mp4" if n == "ref.mp4" else n), d / n)
            out = d / "t.json"
            rc = sp.main(["--template", str(TEMPLATE), "--sources-dir", str(d), "--music", str(MEDIA / "music.m4a"),
                          "--out", str(out), "--no-ai"])
            self.assertEqual(rc, 0)
            used = {s["source_video_file"] for s in json.loads(out.read_text())["timeline"]}
            self.assertNotIn("ref.mp4", used)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestEncoderEnvCollision(unittest.TestCase):
    """Bug fix: the old workflow sets RENDER_PRESET=medium; the new renderer must keep the template's preset."""

    def test_generic_render_preset_is_ignored(self):
        spec = core.load_template(TEMPLATE)["render_spec"]
        saved = {k: os.environ.pop(k, None) for k in ("LVG_RENDER_PRESET", "LVG_RENDER_CRF")}
        os.environ["RENDER_PRESET"], os.environ["RENDER_CRF"] = "medium", "15"
        try:
            self.assertEqual(rv.encoder_settings(spec), (spec["video"]["preset"], str(spec["video"]["crf"])))
            args = rv.video_args(spec)
            self.assertEqual(args[args.index("-preset") + 1], "slow")
            self.assertEqual(args[args.index("-crf") + 1], "18")
        finally:
            os.environ.pop("RENDER_PRESET", None); os.environ.pop("RENDER_CRF", None)
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


class TestClassifyShots(unittest.TestCase):
    """The shot classifier validates every model answer (Gemini mocked)."""

    def _rows(self, d):
        rows = [{"id": f"s01-{i:02d}", "src": "s01", "shot_start": 0.0 if i < 3 else 30.0, "shot_end": 25.0 if i < 3 else 55.0,
                 "s": 0.1 + i * 8.0 if i < 3 else 30.1 + (i - 3) * 8.0, "e": 8.1 + i * 8.0 if i < 3 else 38.1 + (i - 3) * 8.0,
                 "motion": 5.0} for i in range(5)]
        (d / "s01_rows.json").write_text(json.dumps(rows))
        for k in range(0, len(rows), cs.PER_SHEET):
            (d / f"s01_sheet_{k // cs.PER_SHEET:02d}.jpg").write_bytes(b"x")
        return rows

    def test_merge_validate_and_drop(self):
        d = Path(tempfile.mkdtemp())
        try:
            self._rows(d)
            answer = {"rows": [{"id": "s01-00", "shot_type": "close_skill", "quality": 4},
                               {"id": "s01-01", "shot_type": "close_skill", "quality": 5},
                               {"id": "s01-02", "shot_type": "wide", "quality": 2},          # below min quality
                               {"id": "s01-03", "shot_type": "goal", "quality": 4},
                               {"id": "s01-04", "shot_type": "made_up_type", "quality": 9},   # -> unclassified, q clamped to 5
                               {"id": "zzz", "shot_type": "wide", "quality": 5}]}              # unknown id ignored
            calls = []

            def ask(role, prompt, images=(), json_mode=False, timeout=0):
                calls.append(role)
                return json.dumps(answer), "fake"
            segs, stats = cs.run(d, ["s01"], core.load_template(TEMPLATE), ask)
            self.assertEqual(calls, ["vision"])
            self.assertEqual([(s["shot_type"], s["start"], s["end"], s["quality"]) for s in segs],
                             [("close_skill", 0.1, 16.1, 4), ("goal", 30.1, 38.1, 4), ("unclassified", 38.1, 46.1, 5)])
            self.assertEqual(stats["labelled"], 5)
            out = d / "cat.json"          # the planner must be able to load what the classifier wrote
            out.write_text(json.dumps(segs))
            c = ctx(catalog=None)
            c["sources"] = {"s01.mp4": {"duration": 60.0}}
            self.assertEqual(len(sp.load_catalog(str(out), c)), 3)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_garbage_and_failures_give_empty_catalog_not_a_crash(self):
        d = Path(tempfile.mkdtemp())
        try:
            self._rows(d)
            segs, stats = cs.run(d, ["s01"], core.load_template(TEMPLATE), lambda *a, **k: ("not json", "fake"))
            self.assertEqual((segs, stats["labelled"]), ([], 0))

            def boom(*a, **k):
                raise RuntimeError("down")
            segs, _ = cs.run(d, ["s01"], core.load_template(TEMPLATE), boom)
            self.assertEqual(segs, [])
            out = d / "o.json"
            self.assertEqual(cs.main(["--template", str(TEMPLATE), "--shots-dir", str(d), "--sources", "s01",
                                      "--out", str(out)], ask=boom), 0)
            self.assertEqual(json.loads(out.read_text()), [])
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
