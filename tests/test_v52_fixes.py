"""
v5.2 fixes found in the logs of the 27 Nov 2026 run (the first successful run of the v5 code):
1. The planner (brain) talks to the same Gemini API version as the router (v1beta). The google-genai SDK 3.x defaults to v1,
   where this key's listing had no gemini-3-flash-preview and no rolling aliases, so 3 names of the brain chain were skipped as "not served".
2. The final audio limiter keeps its ceiling: FFmpeg's alimiter auto level (on by default) scaled the limited music back up
   to full scale, so QA found the audio at 0.0 dB.
Run: python -m unittest tests.test_v52_fixes
"""
import json
import os
import re
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

TEMPLATE = ROOT / "config" / "master_rendering_template.json"


class TestPlannerApiVersion(unittest.TestCase):
    def test_default_is_v1beta(self):
        self.assertEqual(sp.API_VERSION, "v1beta")

    def test_listing_and_calls_go_to_v1beta(self):
        """The real SDK builds the URLs (listing and generateContent); the network call itself is blocked."""
        try:
            from google.genai import _api_client as ac
        except ImportError:
            self.skipTest("google-genai not installed")
        if not hasattr(getattr(ac, "BaseApiClient", None), "_request"):
            self.skipTest("this google-genai version has no BaseApiClient._request to intercept")
        urls = []

        def blocked(self_, http_request, *args, **kwargs):
            urls.append(str(http_request.url).split("?")[0])
            raise ConnectionError("network blocked in test")

        original = ac.BaseApiClient._request
        os.environ["GEMINI_API_KEY"] = "dummy-not-a-real-key"
        try:
            ac.BaseApiClient._request = blocked
            client = sp.make_client()
            self.assertIsNone(sp.discover_models(client))      # listing failed -> None (full chain is tried)
            with self.assertRaises(Exception):
                sp.call_gemini(client, "gemini-3.6-flash", "system", "prompt")
        finally:
            ac.BaseApiClient._request = original
            os.environ.pop("GEMINI_API_KEY", None)
        self.assertTrue(urls)
        for u in urls:
            self.assertIn("/v1beta/models", u)
            self.assertNotIn("/v1/models", u)
        self.assertTrue(any(u.endswith("/v1beta/models/gemini-3.6-flash:generateContent") for u in urls), urls)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg not installed")
class TestAudioCeiling(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="v52audio_"))
        self.spec = json.loads(TEMPLATE.read_text(encoding="utf-8"))["render_spec"]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ff(self, *args):
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)

    def test_limiter_auto_level_is_off(self):
        seen = []
        original = rv.run_ffmpeg
        rv.run_ffmpeg = lambda cmd, what: seen.append(cmd)
        try:
            rv.concat_and_mux([self.tmp / "a.ts"], str(self.tmp / "m.wav"), self.tmp / "o.mp4", 4.0, self.spec, self.tmp)
        finally:
            rv.run_ffmpeg = original
        af = seen[0][seen[0].index("-af") + 1]
        self.assertIn(f"alimiter=limit={self.spec['audio']['limiter']}:level=disabled", af)

    def test_full_scale_music_keeps_headroom(self):
        """Music that peaks at -0.1 dBFS must leave the final mux below QA's clipping line (it used to end at 0.0 dB)."""
        seg, music, out = self.tmp / "a.ts", self.tmp / "m.wav", self.tmp / "o.mp4"
        self._ff("-f", "lavfi", "-i", "testsrc2=s=320x180:r=60", "-t", "4", "-c:v", "libx264", "-preset", "ultrafast",
                 "-pix_fmt", "yuv420p", "-f", "mpegts", str(seg))
        self._ff("-f", "lavfi", "-i", "aevalsrc=0.99*sin(2*PI*220*t)|0.99*sin(2*PI*221*t):d=5:s=44100",
                 "-c:a", "pcm_f32le", str(music))
        rv.concat_and_mux([seg], str(music), out, 4.0, self.spec, self.tmp)
        vd = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(out), "-af", "volumedetect", "-f", "null", "-"],
                            capture_output=True, text=True).stderr
        peak = float(re.search(r"max_volume: (-?[\d.]+) dB", vd).group(1))
        self.assertLess(peak, -0.2, f"final audio peak {peak} dB")
        self.assertNotIn("full scale", self._qa(out, 240), "QA must not flag a mux that kept the limiter's headroom")
        self._ff("-i", str(out), "-c:v", "copy", "-af", "volume=3", "-c:a", "aac", str(self.tmp / "loud.mp4"))
        self.assertIn("audio at full scale (clipping risk)", self._qa(self.tmp / "loud.mp4", 240))

    def _qa(self, video: Path, frames: int) -> str:
        """Run the real pipeline/qa.py (it reads edl.json and config.json from its own folder) and return its last line."""
        qa_dir = self.tmp / "qa"
        qa_dir.mkdir(exist_ok=True)
        shutil.copy(ROOT / "pipeline" / "qa.py", qa_dir / "qa.py")
        (qa_dir / "edl.json").write_text(json.dumps([{"id": "S001", "sec": "main", "frames": frames}]))
        (qa_dir / "config.json").write_text(json.dumps({"fps": 60}))
        res = subprocess.run([sys.executable, str(qa_dir / "qa.py"), str(video), str(qa_dir / "sheets")],
                             capture_output=True, text=True, timeout=300)
        lines = [ln for ln in res.stdout.splitlines() if ln.startswith("QA RESULT")]
        self.assertTrue(lines, res.stdout[-800:] + res.stderr[-800:])
        return lines[-1]


if __name__ == "__main__":
    unittest.main()
