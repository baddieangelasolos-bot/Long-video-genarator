"""email_logs.py: the log mail goes out after EVERY run (it used to be failures only, so a SUCCESS run that was
really content-blind sent no logs). Dry run, no SMTP. Run: python -m unittest discover -s tests -p "test_email_logs.py" """
import email
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from email import policy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "autonomy"))
import email_logs as el  # noqa: E402

SECRET = "AIzaSyFAKEKEY1234567890"


class TestEmailLogs(unittest.TestCase):
    def setUp(self):
        self.w = Path(tempfile.mkdtemp())
        (self.w / "build").mkdir(); (self.w / "analysis").mkdir(); (self.w / "shots").mkdir()
        (self.w / "build.log").write_text(
            "[10:00:01] $ python prepare\nnoise line\n"
            "[router] gemini-3.8-flash: quota/rate limit (429) -> next tier\n"
            "WARNING sheet s01_sheet_03.jpg attempt 1: no usable answer\n"
            "WARNING 22 sheet(s) unanswered: waiting 30s, retry round 1/5\n"
            f"[10:20:00] shot catalog: {{\"rows\": 197, \"labelled\": 0}} key={SECRET}\nmore noise\n")
        (self.w / "alerts.log").write_text(
            json.dumps({"subject": "[Video pipeline] Footage and plan report", "body": "x"}) + "\n"
            + json.dumps({"subject": "[Video pipeline] SUCCESS WITH WARNINGS", "body": "y"}) + "\n")
        (self.w / "analysis" / "shot_catalog.json").write_text("[]")
        (self.w / "build" / "timeline.json").write_text("{}")
        (self.w / "shots" / "s01_rows.json").write_text("[]")
        self.old = (el.W, el.SECRETS, dict(os.environ))
        el.W = str(self.w); el.SECRETS = [SECRET]
        for k in ("EMAIL_LOGS_ALWAYS", "JOB_STATUS", "PIPELINE_OUTCOME"):
            os.environ.pop(k, None)
        os.environ.update(EMAIL_LOGS_DRYRUN="1", MAIL_USER="me@example.com", RUN_URL="https://example.com/run/1")
        Path("/tmp/email_logs.eml").unlink(missing_ok=True)

    def tearDown(self):
        el.W, el.SECRETS = self.old[0], self.old[1]
        os.environ.clear(); os.environ.update(self.old[2])
        shutil.rmtree(self.w, ignore_errors=True)

    def sent(self):
        p = Path("/tmp/email_logs.eml")
        return email.message_from_bytes(p.read_bytes(), policy=policy.default) if p.exists() else None

    def test_clean_run_still_sends_logs_with_key_lines_and_the_real_status(self):
        os.environ.update(JOB_STATUS="success", PIPELINE_OUTCOME="success")
        el.main()
        m = self.sent()
        self.assertIsNotNone(m)
        self.assertEqual(m["Subject"], "[Video pipeline] Run logs attached (SUCCESS WITH WARNINGS)")
        body = m.get_body().get_content()
        self.assertIn("quota/rate limit (429)", body)
        self.assertIn("retry round 1/5", body)
        self.assertIn("shot catalog:", body)
        self.assertNotIn("noise line", body)
        self.assertNotIn(SECRET, body)

    def test_zip_has_the_logs_and_plan_files_and_no_secret(self):
        os.environ.update(JOB_STATUS="success", PIPELINE_OUTCOME="success")
        el.main()
        att = [p for p in self.sent().iter_attachments()][0]
        self.assertEqual(att.get_filename(), "pipeline-logs.zip")
        z = zipfile.ZipFile(io.BytesIO(att.get_content()))
        names = set(z.namelist())
        self.assertTrue({"build.log", "alerts.log", "analysis/shot_catalog.json", "build/timeline.json", "shots/s01_rows.json"} <= names)
        self.assertNotIn(SECRET.encode(), z.read("build.log"))
        self.assertIn(b"***", z.read("build.log"))

    def test_failed_run_keeps_the_failure_mail(self):
        os.environ.update(JOB_STATUS="failure", PIPELINE_OUTCOME="failure")
        el.main()
        m = self.sent()
        self.assertEqual(m["Subject"], "[Video pipeline] FAILED - logs attached")
        self.assertIn("Pipeline problem detected", m.get_body().get_content())

    def test_can_be_switched_back_to_failures_only(self):
        os.environ.update(JOB_STATUS="success", PIPELINE_OUTCOME="success", EMAIL_LOGS_ALWAYS="0")
        el.main()
        self.assertIsNone(self.sent())

    def test_plain_success_without_alert_log_and_key_line_helpers(self):
        (self.w / "alerts.log").unlink()
        os.environ.update(JOB_STATUS="success", PIPELINE_OUTCOME="success")
        el.main()
        self.assertEqual(self.sent()["Subject"], "[Video pipeline] Run logs attached (SUCCESS)")
        self.assertEqual(el.run_status(""), "")
        self.assertEqual(el.key_lines("a\nb\n"), "")
        long = "\n".join(f"WARNING line {i}" for i in range(2000))
        self.assertTrue(el.key_lines(long, 500).startswith("...(older lines cut)"))
        self.assertIn("WARNING line 1999", el.key_lines(long, 500))


if __name__ == "__main__":
    unittest.main()
