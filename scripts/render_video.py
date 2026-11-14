#!/usr/bin/env python3
"""
render_video.py - compiles a verified timeline.json + music into the final 1920x1080 / 60 fps MP4.

Pipeline
  1. Load the template + timeline and re-validate (a bad timeline is refused before any render time is spent).
  2. Resolve every source by BASE NAME inside --sources-dir (no free-form paths: no path traversal).
  3. Optional: librosa tempo/beat analysis; with --beat-snap, cut points move to a nearby beat.
  4. Convert durations to an exact frame schedule at 60 fps (no cumulative drift).
  5. Render each clip once (x264 CRF 18, preset slow, letterbox filter) to a transport-stream segment.
  6. Concatenate the segments WITHOUT re-encoding (so quality loss happens exactly once), then mux the music.
  7. Verify the output with ffprobe (size, fps, duration, frame count) and write render_report.json.

Exit codes: 0 ok | 2 setup/input error | 4 render or verification failed
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import timeline_core as core  # noqa: E402

log = logging.getLogger("render")
FPS = 60
PUNCH_S = 0.15      # length of the push-in + blur before a zoom_punch_cut
PUNCH_ZOOM = 0.10   # 10% push-in at the last frame of the punch


class RenderError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Filters
# --------------------------------------------------------------------------- #
def build_vf(spec: dict, duration: float, speed: float, punch: bool) -> str:
    """
    Per-clip video filter chain:
      speed -> constant 60 fps -> scale-to-cover -> (optional push-in + blur on the tail) -> crop -> pad -> sar 1
    The letterbox values come from the template. It is the owner's filter with one added `crop`, because
    `scale=...:increase` followed by `pad` alone leaves 16:9 footage un-barred and crashes on vertical footage.
    """
    lb, canvas = spec["letterbox"], spec["canvas"]
    if lb["enabled"]:
        bw, bh = lb["content_width"], lb["content_height"]      # the 2:1 cinematic block
    else:
        bw, bh = canvas["width"], canvas["height"]               # full frame
    parts: list[str] = []
    if abs(speed - 1.0) > 1e-6:
        parts.append(f"setpts=PTS/{speed:.6f}")                  # slow/fast the picture; clip audio is never used
    parts.append(f"fps={FPS}:round=near")                         # lower-fps sources get repeated frames, not new motion
    parts.append(f"scale={bw}:{bh}:force_original_aspect_ratio=increase:flags=lanczos")  # cover: never stretches
    if punch:
        t0 = max(0.0, duration - PUNCH_S)
        z = f"(1+{PUNCH_ZOOM}*clip((t-{t0:.4f})/{PUNCH_S},0,1))"   # 1.0 -> 1.1 over the last PUNCH_S seconds
        parts.append(f"scale=w='trunc(iw*{z}/2)*2':h='trunc(ih*{z}/2)*2':eval=frame:flags=bicubic")
        parts.append(f"crop={bw}:{bh}")
        parts.append(f"boxblur=luma_radius=5:luma_power=1:enable='gte(t,{max(0.0, duration - 0.1):.4f})'")
    else:
        parts.append(f"crop={bw}:{bh}")                           # centre-crop to the block
    if lb["enabled"]:
        parts.append(f"pad={canvas['width']}:{canvas['height']}:0:{lb['bar_height']}:color=black")
    parts += ["setsar=1", "format=yuv420p"]
    return ",".join(parts)


def encoder_settings(spec: dict) -> tuple[str, str]:
    """(preset, crf). Overrides exist ONLY for tests and use LVG_-prefixed names so a generic CI variable
    such as RENDER_PRESET (set by the old pipeline's workflow) can never change this renderer."""
    v = spec["video"]
    return os.environ.get("LVG_RENDER_PRESET", v["preset"]), os.environ.get("LVG_RENDER_CRF", str(v["crf"]))


def video_args(spec: dict) -> list[str]:
    """Encoder settings: the template wins unless LVG_RENDER_PRESET / LVG_RENDER_CRF are set (tests only)."""
    v = spec["video"]
    preset, crf = encoder_settings(spec)
    if preset != v["preset"] or crf != str(v["crf"]):
        log.warning("ENCODER OVERRIDE active: preset=%s crf=%s (template says %s / %s)",
                    preset, crf, v["preset"], v["crf"])
    return ["-c:v", v["codec"], "-preset", preset, "-crf", crf, "-profile:v", v["profile"],
            "-pix_fmt", v["pix_fmt"], "-r", str(FPS), "-g", str(FPS * 2), "-keyint_min", str(FPS),
            "-sc_threshold", "0", "-an"]


# --------------------------------------------------------------------------- #
# Beat analysis (optional)
# --------------------------------------------------------------------------- #
def analyse_beats(music: str) -> dict | None:
    """
    Tempo + beat times via librosa. Audio is decoded by FFmpeg first (librosa alone cannot open .m4a/.aac,
    the format the pipeline stores the music in). Returns None, with a log line, if anything is unavailable.
    """
    try:
        import librosa
        import numpy as np
    except ImportError:
        log.warning("librosa is not installed - skipping beat analysis (beat placement is optional)")
        return None
    try:
        sr = 22050
        proc = subprocess.run(["ffmpeg", "-v", "error", "-i", music, "-vn", "-ac", "1", "-ar", str(sr),
                               "-f", "f32le", "-"], capture_output=True, timeout=600)
        y = np.frombuffer(proc.stdout, dtype=np.float32)
        if proc.returncode != 0 or y.size < sr * 5:
            raise RuntimeError("could not decode the music (or it is shorter than 5 s)")
        tempo, beats = librosa.beat.beat_track(y=y, sr=sr, units="time")
        bpm = float(np.atleast_1d(tempo)[0])
        beats = [round(float(b), 4) for b in beats]
        log.info("Beat analysis: %.1f BPM, %d beats", bpm, len(beats))
        return {"bpm": round(bpm, 2), "beats": beats}
    except Exception as exc:  # noqa: BLE001 - optional feature must never break the render
        log.warning("Beat analysis failed (%s) - continuing without it", exc)
        return None


def _snap_overlaps(shots: list[dict], i: int, new_a: float, new_b: float, new_trim_b: float) -> bool:
    """True if the resized windows of shots i and i+1 would touch any other window of the same source."""
    a, b = shots[i], shots[i + 1]
    wins = {i: (a["trim_start"], a["trim_start"] + new_a * a["speed_multiplier"]),
            i + 1: (new_trim_b, new_trim_b + new_b * b["speed_multiplier"])}
    for k, w in wins.items():
        for j, o in enumerate(shots):
            if j == k or o["source_video_file"] != shots[k]["source_video_file"]:
                continue
            o0, o1 = wins.get(j, (o["trim_start"], o["trim_start"] + o["duration"] * o["speed_multiplier"]))
            if w[0] < o1 + 0.02 and o0 < w[1] + 0.02:
                return True
    return False


def snap_to_beats(shots: list[dict], beats: list[float], sources: dict, tol: float, min_clip: float) -> int:
    """
    Move interior cut points to the nearest beat if it is within `tol` seconds. The total length never changes:
    the clip before the cut grows/shrinks and the clip after does the opposite, keeping its own END footage.
    A move is skipped if it would break a minimum clip length or leave the source file.
    """
    if not beats:
        return 0
    moved, t = 0, 0.0
    for i in range(len(shots) - 1):
        a, b = shots[i], shots[i + 1]
        t += a["duration"]
        nearest = min(beats, key=lambda x: abs(x - t))
        shift = nearest - t                       # + = cut moves later, - = cut moves earlier
        if abs(shift) < 0.004 or abs(shift) > tol:
            continue
        new_a, new_b = a["duration"] + shift, b["duration"] - shift
        if new_a < min_clip or new_b < min_clip:
            continue
        new_trim_b = b["trim_start"] + shift * b["speed_multiplier"]   # b keeps the same last frame
        if shift > 0 and a["trim_start"] + new_a * a["speed_multiplier"] > sources[a["source_video_file"]]["duration"] - 0.05:
            continue
        if new_trim_b < 0:
            continue
        if _snap_overlaps(shots, i, new_a, new_b, new_trim_b):
            continue
        a["duration"], b["duration"], b["trim_start"] = round(new_a, 4), round(new_b, 4), round(new_trim_b, 4)
        t = nearest
        moved += 1
    return moved


# --------------------------------------------------------------------------- #
# Frame schedule
# --------------------------------------------------------------------------- #
def frame_schedule(shots: list[dict]) -> list[int]:
    """Frames per shot from cumulative rounding, so the sum is exact and error never accumulates."""
    edges, acc = [0], 0.0
    for s in shots:
        acc += s["duration"]
        edges.append(round(acc * FPS))
    frames = [b - a for a, b in zip(edges, edges[1:])]
    if any(f < 1 for f in frames):
        raise RenderError("a clip is shorter than one frame at 60 fps")
    return frames


# --------------------------------------------------------------------------- #
# FFmpeg steps
# --------------------------------------------------------------------------- #
def run_ffmpeg(cmd: list[str], what: str) -> None:
    log.debug("ffmpeg: %s", " ".join(cmd))
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        tail = "\n".join(res.stderr.strip().splitlines()[-8:])
        raise RenderError(f"ffmpeg failed while {what}:\n{tail}")


def render_clip(idx: int, shot: dict, frames: int, src_path: Path,
                punch: bool, spec: dict, workdir: Path) -> Path:
    """Render one clip. Exactly `frames` frames are written (-frames:v), so timing is frame-exact."""
    out = workdir / f"clip_{idx:04d}.ts"
    dur = frames / FPS
    speed = shot["speed_multiplier"]
    vf = build_vf(spec, dur, speed, punch)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-ss", f"{shot['trim_start']:.4f}", "-t", f"{dur * speed + 0.5:.4f}",  # +0.5 s: decoder head-room
           "-i", str(src_path), "-vf", vf, "-frames:v", str(frames), *video_args(spec),
           "-f", "mpegts", str(out)]
    run_ffmpeg(cmd, f"rendering {shot['shot_id']} ({shot['source_video_file']} @ {shot['trim_start']}s)")
    return out


def concat_and_mux(segments: list[Path], music: str, out_path: Path, target_s: float, spec: dict, workdir: Path) -> None:
    """Join the segments with stream copy, then add the music in the same pass (video is NOT re-encoded)."""
    listing = workdir / "concat.txt"
    listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in segments), encoding="utf-8")
    a = spec["audio"]
    fade_at = max(0.0, target_s - a["fade_out_s"])
    af = f"alimiter=limit={a['limiter']},afade=t=out:st={fade_at:.3f}:d={a['fade_out_s']}"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "concat", "-safe", "0", "-i", str(listing),
           "-i", music,
           "-map", "0:v:0", "-map", "1:a:0",
           "-c:v", "copy",
           "-c:a", a["codec"], "-b:a", a["bitrate"], "-ar", str(a["sample_rate"]), "-ac", str(a["channels"]),
           "-af", af, "-t", f"{target_s:.3f}",
           "-movflags", spec["movflags"], str(out_path)]
    run_ffmpeg(cmd, "joining clips and adding music")


def verify_output(path: Path, target_s: float, total_frames: int, spec: dict) -> dict:
    """ffprobe the result. Any violation of the render spec raises."""
    cmd = ["ffprobe", "-v", "error", "-count_packets", "-select_streams", "v:0",
           "-show_entries", "stream=width,height,r_frame_rate,nb_read_packets,duration", "-of", "json", str(path)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    try:
        st = json.loads(res.stdout)["streams"][0]
    except (json.JSONDecodeError, KeyError, IndexError) as exc:
        raise RenderError(f"output is not readable by ffprobe: {exc}") from exc
    info = core.probe_media(path)
    num, _, den = st["r_frame_rate"].partition("/")
    fps = float(num) / float(den or 1)
    problems = []
    if (st["width"], st["height"]) != (spec["canvas"]["width"], spec["canvas"]["height"]):
        problems.append(f"size {st['width']}x{st['height']}")
    if abs(fps - FPS) > 0.01:
        problems.append(f"fps {fps}")
    if abs(info["duration"] - target_s) > 0.15:
        problems.append(f"duration {info['duration']:.2f}s vs target {target_s:.2f}s")
    if int(st.get("nb_read_packets", 0)) != total_frames:
        problems.append(f"frames {st.get('nb_read_packets')} vs planned {total_frames}")
    if not info["has_audio"]:
        problems.append("no audio stream")
    if problems:
        raise RenderError("output verification failed: " + "; ".join(problems))
    return {"width": st["width"], "height": st["height"], "fps": fps, "duration_s": round(info["duration"], 3),
            "frames": int(st["nb_read_packets"]), "size_mb": round(path.stat().st_size / 1e6, 1)}


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Render the final highlight video from a verified timeline.")
    ap.add_argument("--template", default="config/master_rendering_template.json")
    ap.add_argument("--timeline", default="build/timeline.json")
    ap.add_argument("--sources-dir", required=True)
    ap.add_argument("--music", required=True)
    ap.add_argument("--out", default="build/final.mp4")
    ap.add_argument("--workdir", default=None, help="temp folder for clip segments (default: system temp)")
    ap.add_argument("--keep-temp", action="store_true")
    ap.add_argument("--beat-snap", action="store_true", help="optional: nudge cuts onto the nearest beat (librosa)")
    ap.add_argument("--no-beats", action="store_true", help="skip librosa analysis entirely")
    ap.add_argument("--jobs", type=int, default=1, help="clips rendered in parallel (x264 is already multi-threaded)")
    ap.add_argument("--edl-out", default=None,
                    help="write [{id, sec, frames}] of the rendered edit (the old pipeline's qa.py reads this format)")
    ap.add_argument("--no-letterbox", action="store_true", help="override the template: full-frame 16:9")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    t_start = time.time()
    try:
        tpl = core.load_template(args.template)
        spec = json.loads(json.dumps(tpl["render_spec"]))
        if args.no_letterbox:
            spec["letterbox"]["enabled"] = False
        doc = json.loads(Path(args.timeline).read_text(encoding="utf-8"))
        shots = doc["timeline"]
        sources_dir = Path(args.sources_dir)
        # resolve + probe only the sources the timeline really uses (by base name)
        names = sorted({s["source_video_file"] for s in shots})
        sources: dict[str, dict] = {}
        for n in names:
            p = sources_dir / os.path.basename(n)
            if not p.is_file():
                raise core.PlanError(f"source '{n}' is not in {sources_dir}")
            sources[n] = core.probe_media(p)
        target = float(doc.get("target_duration_s") or sum(s["duration"] for s in shots))
        ctx = {"template": tpl, "sources": sources, "target_s": target, "catalog": []}
        errs, warns = core.validate({"timeline": shots}, ctx)
        for w in warns:
            log.warning("%s", w)
        if errs:
            for e in errs[:12]:
                log.error("%s", e)
            raise core.PlanError(f"timeline failed validation ({len(errs)} errors) - refusing to render")
    except (core.PlanError, OSError, KeyError, json.JSONDecodeError) as exc:
        log.error("%s", exc)
        return 2

    # ---- optional beat work ------------------------------------------------------------
    beat_info, snapped = None, 0
    if not args.no_beats:
        beat_info = analyse_beats(args.music)
    if args.beat_snap and beat_info:
        backup = json.loads(json.dumps(shots))
        snapped = snap_to_beats(shots, beat_info["beats"], sources,
                                tpl["constraints"]["beat_sync"]["snap_tolerance_s"],
                                tpl["constraints"]["sections"]["main"]["min_clip_s"])
        snap_errs, _ = core.validate({"timeline": shots}, ctx)
        if snap_errs:  # snapping must never make a valid plan invalid
            log.warning("Beat snap reverted (%s)", snap_errs[0])
            shots[:] = backup
            snapped = 0
        else:
            log.info("Beat snap moved %d cut points", snapped)
    elif args.beat_snap:
        log.warning("--beat-snap requested but no beat data available; cuts stay where the planner put them")

    try:
        frames = frame_schedule(shots)
    except RenderError as exc:
        log.error("%s", exc)
        return 2
    total_frames = sum(frames)
    target_frames = round(target * FPS)
    if total_frames != target_frames:
        log.error("Frame schedule is %d frames, target needs %d", total_frames, target_frames)
        return 2

    workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="lvg_"))
    workdir.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    log.info("Rendering %d clips, %d frames (%.2fs) at %d fps, letterbox=%s",
             len(shots), total_frames, total_frames / FPS, FPS, spec["letterbox"]["enabled"])

    try:
        segs: dict[int, Path] = {}
        # a zoom_punch_cut on shot i+1 puts the push-in on the TAIL of shot i
        punch_tail = {i for i in range(len(shots) - 1) if shots[i + 1]["transition"] == "zoom_punch_cut"}
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            futs = {pool.submit(render_clip, i, s, frames[i], Path(args.sources_dir) / os.path.basename(s["source_video_file"]),
                                i in punch_tail, spec, workdir): i
                    for i, s in enumerate(shots)}
            for n, fut in enumerate(as_completed(futs), 1):
                i = futs[fut]
                segs[i] = fut.result()  # raises RenderError on failure
                log.info("clip %d/%d done (%s)", n, len(shots), shots[i]["shot_id"])  # progress: never a silent log
        concat_and_mux([segs[i] for i in range(len(shots))], args.music, out_path, target, spec, workdir)
        verified = verify_output(out_path, target, total_frames, spec)
    except RenderError as exc:
        log.error("%s", exc)
        return 4
    finally:
        if not args.keep_temp and not args.workdir:
            shutil.rmtree(workdir, ignore_errors=True)

    report = {"output": str(out_path), "verified": verified, "clips": len(shots), "punch_transitions": len(punch_tail),
              "beat_analysis": {"bpm": beat_info["bpm"], "beats": len(beat_info["beats"])} if beat_info else None,
              "beat_snap_moves": snapped, "render_seconds": round(time.time() - t_start, 1),
              "encoder": dict(zip(("preset", "crf"), encoder_settings(spec)))}
    Path(str(out_path) + ".report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    if args.edl_out:
        Path(args.edl_out).write_text(json.dumps([{"id": s["shot_id"], "sec": s["section"], "frames": frames[i]}
                                                  for i, s in enumerate(shots)]), encoding="utf-8")
    log.info("DONE %s - %s", out_path, verified)
    return 0


if __name__ == "__main__":
    sys.exit(main())
