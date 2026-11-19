"""v3.1: the Ronaldo run labelled 0 of 197 windows and still mailed SUCCESS with random scenes.
Labelling now waits and asks again, says why it failed, and the mail says plainly when scenes were not checked.
Gemini is mocked, no media, no network except a local fake server.
Run: python -m unittest discover -s tests -p "test_v31_labelling.py" """
import contextlib
import http.server
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "autonomy"))
import classify_shots as cs      # noqa: E402
import label_health as lh        # noqa: E402
import plan_report               # noqa: E402
import timeline_core as core     # noqa: E402

TEMPLATE = ROOT / "config" / "master_rendering_template.json"


def make_rows(d: Path, n: int = 20) -> list[str]:
    rows = [{"id": f"s01-{i:02d}", "src": "s01", "shot_start": float(i * 10), "shot_end": float(i * 10 + 9),
             "s": i * 10 + 0.1, "e": i * 10 + 8.1, "motion": 5.0} for i in range(n)]
    (d / "s01_rows.json").write_text(json.dumps(rows))
    for k in range(0, n, cs.PER_SHEET):
        (d / f"s01_sheet_{k // cs.PER_SHEET:02d}.jpg").write_bytes(b"x")
    return [r["id"] for r in rows]


def answer_for(prompt: str) -> str:
    ids = json.loads(prompt[prompt.rindex("["):])
    return json.dumps({"rows": [{"id": i, "shot_type": "close_skill", "quality": 4} for i in ids]})


class TestLabellingRetries(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        make_rows(self.d)
        self.tpl = core.load_template(TEMPLATE)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_busy_spell_is_waited_out_and_every_sheet_gets_labelled(self):
        state = {"busy": True, "slept": []}

        def ask(role, prompt, images=(), json_mode=False, timeout=0):
            return (None, None) if state["busy"] else (answer_for(prompt), "fake")

        def sleep(sec):
            state["slept"].append(sec)
            state["busy"] = False      # the quota window passed while we waited
        segs, stats = cs.run(self.d, ["s01"], self.tpl, ask, retry_rounds=3, sleep=sleep, wait_hint=lambda: 12.0)
        self.assertEqual(stats["labelled"], 20)
        self.assertEqual(stats["retry_rounds"], 1)
        self.assertEqual(stats["sheets_unanswered"], 0)
        self.assertEqual(len(state["slept"]), 1)
        self.assertGreaterEqual(state["slept"][0], 30)
        self.assertIn("all_models_failed", stats["fail_reasons"])
        self.assertTrue(segs)

    def test_permanent_failure_stops_after_the_rounds_and_reports_why(self):
        slept = []
        segs, stats = cs.run(self.d, ["s01"], self.tpl, lambda *a, **k: (None, None), retry_rounds=3,
                             sleep=slept.append, wait_hint=lambda: 0.0)
        self.assertEqual((segs, stats["labelled"], stats["retry_rounds"]), ([], 0, 3))
        self.assertEqual(stats["sheets_unanswered"], stats["sheets"])
        self.assertEqual(len(slept), 3)
        self.assertGreater(stats["fail_reasons"]["all_models_failed"], 0)

    def test_no_tiers_at_all_does_not_wait(self):
        slept = []
        _, stats = cs.run(self.d, ["s01"], self.tpl, lambda *a, **k: (None, None), retry_rounds=3,
                          sleep=slept.append, wait_hint=lambda: None)
        self.assertEqual((slept, stats["retry_rounds"]), ([], 0))

    def test_default_run_never_waits(self):
        slept = []
        cs.run(self.d, ["s01"], self.tpl, lambda *a, **k: (None, None), sleep=slept.append)
        self.assertEqual(slept, [])

    def test_exceptions_are_counted_by_type(self):
        def boom(*a, **k):
            raise TimeoutError("slow")
        _, stats = cs.run(self.d, ["s01"], self.tpl, boom)
        self.assertGreater(stats["fail_reasons"]["TimeoutError"], 0)

    def test_main_prints_the_new_stats_and_retries_with_an_injected_sleep(self):
        out = self.d / "cat.json"
        buf = io.StringIO()
        slept = []
        with contextlib.redirect_stdout(buf):
            rc = cs.main(["--template", str(TEMPLATE), "--shots-dir", str(self.d), "--sources", "s01", "--out", str(out),
                          "--retry-rounds", "2"], ask=lambda *a, **k: (None, None), sleep=slept.append)
        self.assertEqual(rc, 0)
        stats = json.loads(buf.getvalue().split("CATALOG ", 1)[1])
        self.assertEqual((stats["retry_rounds"], len(slept)), (2, 2))      # an injected sleep switches the retry rounds on
        self.assertEqual(stats["sheets_unanswered"], stats["sheets"])
        self.assertEqual(json.loads(out.read_text()), [])

    def test_event_summary_counts_and_strips_time(self):
        ev = ["10:00:01 gemini-3.8-flash: quota/rate limit (429) -> next tier"] * 3 + ["10:00:09 x: HTTP 404 -> next tier"]
        s = cs.event_summary(ev)
        self.assertEqual(s[0], "3 x gemini-3.8-flash: quota/rate limit (429) -> next tier")
        self.assertEqual(len(s), 2)


class TestLabelHealth(unittest.TestCase):
    def test_zero_labels_is_an_unsolved_problem_with_a_suggestion(self):
        h = lh.assess({"rows": 197, "labelled": 0, "segments": 0, "retry_rounds": 5,
                       "fail_reasons": {"all_models_failed": 50}, "ai_events": ["9 x m: quota/rate limit (429) -> next tier"]})
        self.assertEqual(len(h["unsolved"]), 1)
        self.assertIn("0 of 197", h["unsolved"][0])
        self.assertIn("all_models_failed x50", h["unsolved"][0])
        self.assertIn("429", h["unsolved"][0])
        self.assertTrue(h["suggestions"])

    def test_half_labelled_is_unsolved_and_most_labelled_is_a_notice(self):
        self.assertTrue(lh.assess({"rows": 100, "labelled": 40, "segments": 5, "retry_rounds": 2})["unsolved"])
        h = lh.assess({"rows": 100, "labelled": 70, "segments": 9, "retry_rounds": 2})
        self.assertEqual((h["unsolved"], len(h["notices"])), ([], 1))

    def test_clean_run_says_nothing_and_a_rescued_run_only_notices(self):
        self.assertEqual(lh.assess({"rows": 100, "labelled": 99, "segments": 40, "retry_rounds": 0}),
                         {"notices": [], "unsolved": [], "suggestions": []})
        h = lh.assess({"rows": 100, "labelled": 98, "segments": 40, "retry_rounds": 1})
        self.assertEqual((h["unsolved"], len(h["notices"]), len(h["suggestions"])), ([], 1, 1))

    def test_crash_and_all_rejected(self):
        self.assertTrue(lh.assess(None, True)["unsolved"])
        h = lh.assess({"rows": 50, "labelled": 50, "segments": 0, "retry_rounds": 0})
        self.assertTrue(h["notices"])

    def test_report_suggestions_turn_shortages_and_repeats_into_advice(self):
        rep = {"shortages": ["No shot labels: scene choice was content-blind.", "INTRO: no usable bus arrival footage found (0s)."]}
        s = lh.report_suggestions(rep, {"ranges": 2, "seconds": 15.4, "files": ["s03.mp4"]})
        self.assertEqual(len(s), 2)
        self.assertIn("bus arrival", s[0])
        self.assertIn("s03.mp4", s[1])
        self.assertEqual(lh.report_suggestions({"shortages": []}, None), [])


class TestReportWarnsWhenBlind(unittest.TestCase):
    REP = {"supply_s": {}, "used": {"intro": {"bus_arrival": [1, 1.0]}, "main": {"close_skill": [32, 118.0]}},
           "budgets_s": {"intro": 19, "main": 138, "outro": 15}, "plan_line": "Main part: skills 86%.",
           "shortages": ["No shot labels: scene choice was content-blind."], "info": [], "planner": "p"}

    def test_blind_report_starts_with_a_warning_and_marks_used_lines(self):
        t = plan_report.text(self.REP, {"rows": 197, "labelled": 0})
        self.assertTrue(t.startswith("WARNING: shot labelling failed"))
        self.assertIn("INTRO USED (19s) [unverified]", t)

    def test_labelled_report_is_unchanged(self):
        rep = dict(self.REP, supply_s={"close_skill": 200.0}, shortages=[])
        t = plan_report.text(rep, {"rows": 100, "labelled": 95})
        self.assertNotIn("WARNING", t)
        self.assertNotIn("[unverified]", t)


class _H(http.server.BaseHTTPRequestHandler):
    hits = {"models": 0, "gen": 0}

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/v1beta/models"):
            _H.hits["models"] += 1
            if _H.hits["models"] == 1:
                return self._send(500, {})      # the first model listing fails
            return self._send(200, {"models": [{"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]}]})
        self._send(404, {})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        _H.hits["gen"] += 1
        self._send(200, {"candidates": [{"content": {"parts": [{"text": "{\"ok\":1}"}]}}]})


class TestRouter(unittest.TestCase):
    def setUp(self):
        import gemini_router as R
        self.R = R
        self.old = (R.BASE, os.environ.get("GEMINI_API_KEY"), dict(R._cool), R._cache["ids"])
        R._cool.clear()

    def tearDown(self):
        R, (base, key, cool, ids) = self.R, self.old
        R.BASE = base
        R._cool.clear(); R._cool.update(cool); R._cache["ids"] = ids
        if key is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = key

    def test_wait_hint(self):
        R = self.R
        R._cache["ids"] = ["gemini-2.5-flash", "gemini-3.6-flash"]
        self.assertEqual(R.wait_hint(), 0.0)
        R._cool["gemini-2.5-flash"] = time.time() + 100
        self.assertEqual(R.wait_hint(), 0.0)      # the other tier is ready
        R._cool["gemini-3.6-flash"] = time.time() + 40
        self.assertTrue(30 < R.wait_hint() <= 40)
        R._cache["ids"] = []
        self.assertIsNone(R.wait_hint())

    def test_a_failed_model_listing_is_retried_once_instead_of_poisoning_the_run(self):
        R = self.R
        _H.hits.update(models=0, gen=0)
        srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            R.BASE = f"http://127.0.0.1:{srv.server_address[1]}/v1beta"
            os.environ["GEMINI_API_KEY"] = "k" * 20
            R._cache["ids"] = None
            txt, model = R.ask("vision", "hi", json_mode=True)
            self.assertEqual((json.loads(txt), model), ({"ok": 1}, "gemini-2.5-flash"))
            self.assertEqual(_H.hits["models"], 2)
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
