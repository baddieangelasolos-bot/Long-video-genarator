"""
v4 brain rule tests.  Run:  python -m unittest discover -s tests -p test_brain_rule.py

Gemini is MOCKED. What is proven: only the first 3 chain models plan; a daily 429 / 503 / no key in strict mode stops with exit 5
and a halt report (no offline video); times are labelled exact / safe / suggested honestly; the log mail names the planning model.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import brain_guard as bg          # noqa: E402
import script_planner as sp       # noqa: E402
import test_pipeline as tp        # noqa: E402  (fixtures only: ctx(), good_plan(), MEDIA)

UTC = timezone.utc
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)          # winter: Pacific = UTC-8


def setUpModule():
    tp.setUpModule()                                       # builds the small shared test media once


class Err(Exception):
    def __init__(self, code, msg="x"):
        super().__init__(msg)
        self.code = code


DAILY_429 = ("429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your current quota. Please retry in 34.5s.', "
             "'details': [{'@type': 'type.googleapis.com/google.rpc.QuotaFailure', 'violations': [{'quotaMetric': 'x', "
             "'quotaId': 'GenerateRequestsPerDayPerProjectPerModel-FreeTier'}]}, "
             "{'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '34s'}]}}")
MINUTE_429 = ("429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'Quota exceeded. Please retry in 41.2s.', 'details': "
              "[{'violations': [{'quotaId': 'GenerateRequestsPerMinutePerProjectPerModel-FreeTier'}]}, {'retryDelay': '41s'}]}}")
BARE_429 = "429 RESOURCE_EXHAUSTED. resource exhausted"
OVERLOAD_503 = "503 UNAVAILABLE. This model is currently experiencing high demand."


class TestChainStays(unittest.TestCase):
    def test_full_chain_unchanged_and_brain_is_its_first_three(self):
        self.assertEqual(len(sp.MODEL_FALLBACK_CHAIN), 10)
        self.assertEqual(sp.MODEL_FALLBACK_CHAIN[:4], ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"])
        self.assertEqual(sp.BRAIN_MODELS, ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"])


class TestTimes(unittest.TestCase):
    def test_daily_reset_is_midnight_pacific_winter_and_summer(self):
        self.assertEqual(bg.daily_reset(NOW), datetime(2026, 11, 22, 8, 0, tzinfo=UTC))                 # PST = UTC-8
        self.assertEqual(bg.daily_reset(datetime(2026, 7, 1, 3, 0, tzinfo=UTC)), datetime(2026, 7, 1, 7, 0, tzinfo=UTC))   # PDT = UTC-7

    def test_daily_reset_across_dst_changes(self):
        self.assertEqual(bg.daily_reset(datetime(2026, 3, 8, 5, 0, tzinfo=UTC)), datetime(2026, 3, 8, 8, 0, tzinfo=UTC))
        self.assertEqual(bg.daily_reset(datetime(2026, 3, 9, 5, 0, tzinfo=UTC)), datetime(2026, 3, 9, 7, 0, tzinfo=UTC))
        self.assertEqual(bg.daily_reset(datetime(2026, 11, 1, 5, 0, tzinfo=UTC)), datetime(2026, 11, 1, 7, 0, tzinfo=UTC))
        self.assertEqual(bg.daily_reset(datetime(2026, 11, 2, 5, 0, tzinfo=UTC)), datetime(2026, 11, 2, 8, 0, tzinfo=UTC))

    def test_manual_fallback_agrees_with_the_tz_database(self):
        for d in (datetime(2026, 1, 15, 9, 0, tzinfo=UTC), datetime(2026, 6, 15, 9, 0, tzinfo=UTC), datetime(2026, 11, 21, 12, 0, tzinfo=UTC)):
            with_tz = bg.daily_reset(d)
            with mock.patch.dict(sys.modules, {"zoneinfo": None}):          # import zoneinfo now fails -> manual rule
                self.assertEqual(bg.daily_reset(d), with_tz)
                self.assertEqual(bg.to_pacific(d)[1], "PDT" if 4 <= d.month <= 10 else "PST")

    def test_fmt_when_shows_utc_pacific_and_countdown(self):
        s = bg.fmt_when(datetime(2026, 11, 22, 8, 0, tzinfo=UTC), NOW)
        self.assertIn("2026-11-22 08:00 UTC", s)
        self.assertIn("2026-11-22 00:00 PST", s)
        self.assertIn("in 20 h 00 min", s)


class TestErrorReading(unittest.TestCase):
    def d(self, kind, text, code=429):
        return bg.describe_error(kind, Err(code, text), NOW)

    def test_daily_quota_wins_over_the_retry_hint(self):
        r = self.d("quota", DAILY_429)
        self.assertEqual((r["state"], r["basis"]), ("quota_daily", "exact"))
        self.assertEqual(r["at"], "2026-11-22T08:00:00+00:00")

    def test_per_minute_quota_uses_googles_retry_hint(self):
        r = self.d("quota", MINUTE_429)
        self.assertEqual((r["state"], r["basis"]), ("quota_minute", "exact"))
        self.assertEqual(datetime.fromisoformat(r["at"]), NOW + timedelta(seconds=41 + 2))

    def test_unknown_quota_gets_the_safe_daily_time_and_an_earlier_try_time(self):
        r = self.d("quota", BARE_429)
        self.assertEqual((r["state"], r["basis"]), ("quota_unknown", "safe"))
        self.assertEqual(r["at"], "2026-11-22T08:00:00+00:00")
        self.assertEqual(datetime.fromisoformat(r["try_at"]), NOW + timedelta(minutes=10))

    def test_overload_is_only_a_suggestion(self):
        r = self.d("transient", OVERLOAD_503, 503)
        self.assertEqual((r["state"], r["basis"]), ("overload", "suggested"))
        self.assertEqual(datetime.fromisoformat(r["at"]), NOW + timedelta(minutes=30))

    def test_not_served_has_no_time(self):
        r = self.d("skip", "404 NOT_FOUND model not found", 404)
        self.assertEqual(r["state"], "key_or_model")
        self.assertNotIn("at", r)

    def test_key_like_text_is_scrubbed(self):
        r = bg.describe_error("other", Err(500, "boom key=AIzaSyA1234567890abcdefghij end"), NOW)
        self.assertNotIn("AIzaSyA1234567890", r["message"])


def run_chain(behaviour, models=None, sleeps=None, clock=lambda: NOW):
    c = tp.ctx()
    valid = json.dumps(tp.good_plan(c))
    calls = []

    def caller(client, model, system, prompt):
        calls.append(model)
        return behaviour(model, valid)
    sl = sleeps if sleeps is not None else []
    plan, rep = sp.plan_with_gemini(c, tp.FakeClient(sp.MODEL_FALLBACK_CHAIN), models or sp.BRAIN_MODELS, caller=caller,
                                    sleep=sl.append, clock=clock)
    return plan, rep, calls, sl


class TestBrainChain(unittest.TestCase):
    def test_only_three_models_are_ever_asked(self):
        def behave(m, valid):
            raise Err(503, OVERLOAD_503)
        plan, rep, calls, sleeps = run_chain(behave)
        self.assertIsNone(plan)
        self.assertEqual(set(calls), set(sp.BRAIN_MODELS))              # never a 4th model
        self.assertEqual(calls.count("gemini-3.8-flash"), 3)           # three passes
        self.assertIn(20.0, sleeps)
        self.assertIn(60.0, sleeps)

    def test_daily_quota_model_is_not_asked_again(self):
        def behave(m, valid):
            raise Err(429, DAILY_429)
        plan, rep, calls, sleeps = run_chain(behave)
        self.assertIsNone(plan)
        self.assertEqual(calls, sp.BRAIN_MODELS)                        # one call each, no retry pass
        self.assertNotIn(20.0, sleeps)

    def test_minute_limit_waits_for_googles_hint_then_succeeds(self):
        seen = {"n": 0}

        def behave(m, valid):
            seen["n"] += 1
            if seen["n"] <= 3:
                raise Err(429, MINUTE_429)
            return valid
        plan, rep, calls, sleeps = run_chain(behave)
        self.assertIsNotNone(plan)
        self.assertIn(43.0, sleeps)                                      # retryDelay 41 s + 2 s
        self.assertEqual(rep["model_used"], "gemini-3.8-flash")

    def test_fallback_to_second_model_is_recorded_with_details(self):
        def behave(m, valid):
            if m == "gemini-3.8-flash":
                raise Err(503, OVERLOAD_503)
            return valid
        plan, rep, calls, _ = run_chain(behave)
        self.assertEqual(rep["model_used"], "gemini-3.6-flash")
        first = rep["attempts"][0]
        self.assertEqual((first["model"], first["result"], first["http"], first["state"]), ("gemini-3.8-flash", "transient", 503, "overload"))


class TestHalt(unittest.TestCase):
    def test_all_three_overloaded_gives_suggested_time(self):
        def behave(m, valid):
            raise Err(503, OVERLOAD_503)
        plan, rep, *_ = run_chain(behave)
        info = bg.summarize(rep["attempts"], sp.BRAIN_MODELS, rep.get("not_served", []), NOW)
        self.assertEqual((info["cause"], info["basis"]), ("overload", "suggested"))
        self.assertIn("SUGGESTION, NOT A GOOGLE TIME", info["text"])
        self.assertIn("gemini-3.5-flash-lite", info["text"])

    def test_daily_quota_everywhere_gives_exact_midnight_pacific(self):
        def behave(m, valid):
            raise Err(429, DAILY_429)
        plan, rep, *_ = run_chain(behave)
        info = bg.summarize(rep["attempts"], sp.BRAIN_MODELS, [], NOW)
        self.assertEqual((info["cause"], info["basis"]), ("quota", "exact"))
        self.assertEqual(info["retry_at"], "2026-11-22T08:00:00+00:00")
        self.assertEqual(info["subject_when"], "2026-11-22 08:00 UTC")
        self.assertIn("RUN THE WORKFLOW AGAIN AFTER: 2026-11-22 08:00 UTC = 2026-11-22 00:00 PST (Pacific), in 20 h 00 min", info["text"])

    def test_earliest_model_time_is_used(self):
        att = [{"model": "gemini-3.8-flash", "result": "quota", **bg.describe_error("quota", Err(429, DAILY_429), NOW)},
               {"model": "gemini-3.6-flash", "result": "quota", **bg.describe_error("quota", Err(429, MINUTE_429), NOW)},
               {"model": "gemini-3.5-flash-lite", "result": "quota", **bg.describe_error("quota", Err(429, DAILY_429), NOW)}]
        info = bg.summarize(att, sp.BRAIN_MODELS, [], NOW)
        self.assertEqual(datetime.fromisoformat(info["retry_at"]), NOW + timedelta(seconds=43))
        self.assertIn("Model used for the time above: gemini-3.6-flash", info["text"])

    def test_not_served_everywhere_says_waiting_will_not_help(self):
        info = bg.summarize([], sp.BRAIN_MODELS, list(sp.BRAIN_MODELS), NOW)
        self.assertIsNone(info["retry_at"])
        self.assertEqual(info["cause"], "key_or_model")
        self.assertIn("WAITING ALONE WILL NOT FIX THIS", info["text"])

    def test_no_key(self):
        info = bg.summarize([], sp.BRAIN_MODELS, [], NOW, key_missing=True)
        self.assertEqual(info["cause"], "no_key")
        self.assertIn("GEMINI_API_KEY", info["text"])

    def test_all_invalid_answers(self):
        att = [{"model": m, "result": "invalid", "errors": ["E-X"]} for m in sp.BRAIN_MODELS]
        info = bg.summarize(att, sp.BRAIN_MODELS, [], NOW)
        self.assertEqual(info["cause"], "invalid_plan")
        self.assertIsNone(info["retry_at"])


class TestMainStrict(unittest.TestCase):
    def base(self, tmp):
        return ["--template", str(tp.TEMPLATE), "--sources-dir", str(tp.MEDIA / "src"), "--music", str(tp.MEDIA / "music.m4a"),
                "--out", str(Path(tmp) / "build" / "timeline.json"), "--brain-report", str(Path(tmp) / "analysis" / "brain.json")]

    def test_strict_all_unavailable_exits_5_writes_report_and_no_timeline(self):
        def boom(client, model, system, prompt):
            raise Err(429, DAILY_429)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sp, "call_gemini", boom), \
                mock.patch.object(sp.time, "sleep", lambda s: None):
            rc = sp.main(self.base(tmp) + ["--strict-brain"], client_factory=lambda: tp.FakeClient(sp.MODEL_FALLBACK_CHAIN))
            self.assertEqual(rc, 5)
            info = json.loads((Path(tmp) / "analysis" / "brain.json").read_text())
            self.assertEqual(info["cause"], "quota")
            self.assertEqual(info["brain_models"], sp.BRAIN_MODELS)
            self.assertFalse((Path(tmp) / "build" / "timeline.json").exists())

    def test_strict_without_key_exits_5(self):
        def no_key():
            raise KeyError("GEMINI_API_KEY")
        with tempfile.TemporaryDirectory() as tmp:
            rc = sp.main(self.base(tmp) + ["--strict-brain"], client_factory=no_key)
            self.assertEqual(rc, 5)
            self.assertEqual(json.loads((Path(tmp) / "analysis" / "brain.json").read_text())["cause"], "no_key")

    def test_not_strict_still_has_the_offline_planner(self):
        def no_key():
            raise KeyError("GEMINI_API_KEY")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(sp.main(self.base(tmp), client_factory=no_key), 0)

    def test_strict_success_records_model_and_slot(self):
        c = tp.ctx()
        valid = json.dumps(tp.good_plan(c))

        def fake(client, model, system, prompt):
            if model == "gemini-3.8-flash":
                raise Err(503, OVERLOAD_503)
            return valid
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sp, "call_gemini", fake), \
                mock.patch.object(sp.time, "sleep", lambda s: None):
            rc = sp.main(self.base(tmp) + ["--strict-brain"], client_factory=lambda: tp.FakeClient(sp.MODEL_FALLBACK_CHAIN))
            self.assertEqual(rc, 0)
            pl = json.loads((Path(tmp) / "build" / "timeline.json").read_text())["_planner"]
            self.assertEqual((pl["planner"], pl["model_used"], pl["slot"]), ("gemini:gemini-3.6-flash", "gemini-3.6-flash", 2))
            self.assertEqual(pl["brain_models"], sp.BRAIN_MODELS)
            self.assertEqual(pl["model_chain_report"]["attempts"][0]["state"], "overload")


class TestLogMail(unittest.TestCase):
    def run_mail(self, files, job="success", outcome="success"):
        with tempfile.TemporaryDirectory() as tmp:
            for rel, content in files.items():
                p = Path(tmp) / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content if isinstance(content, str) else json.dumps(content))
            env = dict(os.environ, WORK_DIR=tmp, EMAIL_LOGS_DRYRUN="1", JOB_STATUS=job, PIPELINE_OUTCOME=outcome, RUN_URL="u")
            Path("/tmp/email_logs.eml").unlink(missing_ok=True)
            subprocess.run([sys.executable, str(ROOT / "autonomy" / "email_logs.py")], env=env, check=True, capture_output=True, cwd=tmp)
            import email
            from email import policy
            raw = Path("/tmp/email_logs.eml").read_bytes()
            m = email.message_from_bytes(raw, policy=policy.default)
            return m["Subject"], m.get_body(preferencelist=("plain",)).get_content()

    def test_success_mail_names_the_planning_model(self):
        tl = {"_planner": {"planner": "gemini:gemini-3.6-flash", "model_used": "gemini-3.6-flash", "slot": 2, "brain_models": sp.BRAIN_MODELS,
                           "model_chain_report": {"attempts": [{"model": "gemini-3.8-flash", "result": "transient", "http": 503},
                                                               {"model": "gemini-3.6-flash", "result": "ok"}]}}}
        subj, body = self.run_mail({"build/timeline.json": tl, "build.log": "x", "alerts.log": ""})
        self.assertEqual(subj, "[Video pipeline] Run logs attached (SUCCESS)")
        self.assertIn("Model that planned this video: gemini-3.6-flash (planning slot 2 of 3", body)
        self.assertIn("gemini-3.8-flash: busy or timed out (HTTP 503) -> gemini-3.6-flash: valid plan", body)

    def test_old_timeline_without_new_fields_still_works(self):
        tl = {"_planner": {"planner": "gemini:gemini-3.6-flash", "model_chain_report": {"attempts": []}}}
        subj, body = self.run_mail({"build/timeline.json": tl, "build.log": "x", "alerts.log": ""})
        self.assertIn("Model that planned this video: gemini-3.6-flash", body)

    def test_brain_halt_changes_subject_and_leads_the_body(self):
        info = bg.summarize([{"model": m, "result": "quota", **bg.describe_error("quota", Err(429, DAILY_429), NOW)} for m in sp.BRAIN_MODELS],
                            sp.BRAIN_MODELS, [], NOW)
        subj, body = self.run_mail({"analysis/brain_unavailable.json": info, "build.log": "x", "alerts.log": ""}, job="failure", outcome="failure")
        self.assertEqual(subj, "[Video pipeline] BRAIN UNAVAILABLE - run again after 2026-11-22 08:00 UTC - logs attached")
        self.assertTrue(body.startswith("THE GEMINI BRAIN IS UNAVAILABLE"))

    def test_offline_planner_is_called_out(self):
        tl = {"_planner": {"planner": "offline_safe_mode", "model_chain_report": {}}}
        _, body = self.run_mail({"build/timeline.json": tl, "build.log": "x", "alerts.log": ""})
        self.assertIn("OFFLINE planner", body)


if __name__ == "__main__":
    unittest.main()
