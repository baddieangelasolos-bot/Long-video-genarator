#!/usr/bin/env python3
"""
script_planner.py - turns source clips + music + the master template into a verified timeline.json.

Flow
  1. Load config/master_rendering_template.json (the permanent rulebook + schema).
  2. Probe the raw clips and the music (target length = audible end rounded DOWN to 0.5 s).
  3. Ask Gemini (modern `google-genai` SDK) for a JSON edit decision list, walking the
     10-model free-tier fallback chain. Output is forced to JSON with response_mime_type.
  4. Every answer is repaired + validated by timeline_core (the sandbox). Gemini can never
     put an invented file or an impossible window on the timeline.
  5. If every model fails, a deterministic offline planner builds the timeline, so a run never
     stops just because the free tier is busy.

Exit codes: 0 ok | 2 setup/usage error | 3 NEEDS MORE SOURCE VIDEO | 4 no valid plan could be made
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import timeline_core as core  # noqa: E402

log = logging.getLogger("planner")

# Ordered free-tier chain, exactly as specified by the owner. Names that the API does not
# know for this key are skipped in milliseconds (see discover_models + classify_error).
MODEL_FALLBACK_CHAIN = [
    "gemini-3.8-flash",         # primary
    "gemini-3.6-flash",         # backup 1
    "gemini-3.5-flash-lite",    # backup 2
    "gemini-3.1-flash-lite",    # backup 3
    "gemini-3-flash-preview",   # backup 4
    "gemini-1.5-flash",         # backup 5
    "gemini-1.5-pro",           # backup 6
    "gemini-2.5-flash-lite",    # backup 7
    "gemini-2.5-flash-preview", # backup 8
    "gemini-2.0-flash-lite",    # backup 9
]
REQUEST_TIMEOUT_S = 75     # per request; a hanging model must not eat the whole run
MAX_CATALOG_LINES = 320    # keeps the prompt small enough for lite models


# --------------------------------------------------------------------------- #
# Catalog (optional shot labels from an earlier classification stage)
# --------------------------------------------------------------------------- #
def load_catalog(path: str | None, ctx: dict) -> list[dict]:
    """
    Catalog format: JSON list of {"source_video_file","start","end","shot_type","quality"(1-5, optional)}.
    Bad rows are dropped with a log line, never trusted.
    """
    if not path:
        return []
    try:
        rows = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Catalog %s unreadable (%s) - continuing without shot labels", path, exc)
        return []
    taxonomy = set(ctx["template"]["timeline_schema"]["properties"]["timeline"]["items"]
                   ["properties"]["shot_type"]["enum"])
    good, dropped = [], 0
    for r in rows if isinstance(rows, list) else []:
        try:
            name = core._match_source(r["source_video_file"], ctx["sources"])
            a, b = float(r["start"]), float(r["end"])
            typ = r.get("shot_type", "unclassified")
            if name is None or not (0 <= a < b <= ctx["sources"][name]["duration"] + 0.05):
                raise ValueError("bad source or window")
            if typ == "reject":
                continue
            good.append({"source_video_file": name, "start": round(a, 3), "end": round(b, 3),
                         "shot_type": typ if typ in taxonomy else "unclassified",
                         "quality": int(r.get("quality", 3))})
        except (KeyError, ValueError, TypeError):
            dropped += 1
    if dropped:
        log.warning("Catalog: dropped %d invalid rows", dropped)
    log.info("Catalog: %d usable segments", len(good))
    return good


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #
def build_prompt(ctx: dict, errors: list[str] | None = None, previous: str | None = None) -> tuple[str, str]:
    """Returns (system_instruction, user_prompt). The rulebook text comes from the template, never hard-coded."""
    tpl, sources, target = ctx["template"], ctx["sources"], ctx["target_s"]
    budgets = core.section_budgets(target, tpl)
    cons = tpl["constraints"]
    system = (
        "You are the edit planner for an automated football highlight generator. "
        "Reply with ONE JSON object of the form {\"timeline\": [...]} and nothing else: no prose, no markdown. "
        "You are a structured populator, not a writer: use only the schema keys, only the listed source files, "
        "and only windows that exist. Obey the RULEBOOK exactly. If a rule cannot be met with the footage "
        "provided, skip the optional clip type instead of inventing footage."
    )
    lines = [
        f"TARGET_DURATION_S: {target}",
        f"SECTION_BUDGET_S (sum must equal target): {json.dumps(budgets)}",
        "SOURCES (the only files that exist): " + json.dumps(
            [{"file": k, "duration_s": round(v["duration"], 2), "fps": v["fps"]} for k, v in sources.items()]),
    ]
    cat = ctx.get("catalog") or []
    if cat:
        top = sorted(cat, key=lambda g: -g["quality"])[:MAX_CATALOG_LINES]
        lines.append("CATALOG (file|start|end|shot_type|quality). A shot's whole window must sit inside ONE catalog "
                     "segment of the same shot_type:")
        lines += [f"{g['source_video_file']}|{g['start']}|{g['end']}|{g['shot_type']}|{g['quality']}" for g in top]
    else:
        lines.append("CATALOG: none. Shot types are unknown, so use shot_type \"unclassified\" for every shot and "
                     "spread clips across all sources and across each source's length.")
    lines += [
        "RULEBOOK: " + json.dumps(tpl["planning_rulebook"], separators=(",", ":")),
        "LIMITS: " + json.dumps({"sections": {k: {kk: vv for kk, vv in v.items() if kk != "allowed_types"}
                                              for k, v in cons["sections"].items()},
                                 "wide_share_max_of_main": cons["wide_share_max_of_main"],
                                 "speed": cons["speed"], "transitions": cons["transitions"]}),
        "PACING (measured clip lengths, seconds, from the reference edit): " + json.dumps(
            {k: tpl["pacing_fingerprint"][k] for k in SECTIONS_TUPLE}),
        "SCHEMA: " + json.dumps(core.runtime_schema(tpl, list(sources)), separators=(",", ":")),
        "Rules of arithmetic: shots play back to back; the sum of duration over all shots equals TARGET_DURATION_S; "
        "source seconds used = duration x speed_multiplier; window = [trim_start, trim_start + duration x speed_multiplier].",
    ]
    if errors:
        lines.append("YOUR PREVIOUS ANSWER WAS REJECTED. Fix every item and answer again in full:\n- "
                     + "\n- ".join(errors[:25]))
        if previous:
            lines.append("PREVIOUS_ANSWER: " + previous[:6000])
    return system, "\n\n".join(lines)


SECTIONS_TUPLE = core.SECTIONS


# --------------------------------------------------------------------------- #
# Gemini access (google-genai SDK)
# --------------------------------------------------------------------------- #
def make_client() -> Any:
    """Initialise the client from the environment. The key is never logged or written anywhere."""
    from google import genai
    from google.genai import types
    key = os.environ["GEMINI_API_KEY"]  # KeyError is handled by the caller
    return genai.Client(api_key=key, http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_S * 1000))


def discover_models(client: Any) -> set[str] | None:
    """Names the API really serves for this key, or None if listing fails (then every name is tried)."""
    try:
        names = set()
        for m in client.models.list():
            names.add(str(getattr(m, "name", "")).removeprefix("models/"))
        return names or None
    except Exception as exc:  # discovery is an optimisation, never a requirement
        log.warning("Model discovery failed (%s); trying the full chain", type(exc).__name__)
        return None


def classify_error(exc: BaseException) -> str:
    """'skip' = this model is unusable for this key | 'quota' | 'transient' | 'other'."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    msg = str(exc).lower()
    if code in (400, 401, 403, 404) or "not found" in msg or "not supported" in msg:
        return "skip"
    if code == 429 or "resource_exhausted" in msg or "quota" in msg:
        return "quota"
    kind = type(exc).__name__.lower()   # httpx.ConnectError, ReadTimeout, RemoteProtocolError ...
    if (isinstance(code, int) and code >= 500) or isinstance(exc, (TimeoutError, ConnectionError)) or \
            any(w in kind for w in ("timeout", "connect", "network", "transport", "protocol")) or \
            any(w in msg for w in ("timeout", "timed out", "deadline", "unavailable", "overloaded", "connection")):
        return "transient"
    return "other"


def call_gemini(client: Any, model: str, system: str, prompt: str) -> str:
    """One generate_content call with JSON-only output. Returns raw text."""
    from google.genai import types
    resp = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",   # forces syntactically valid JSON
            temperature=0.3,                          # low: we want rule-following, not creativity
            max_output_tokens=16384,
            http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_S * 1000),
        ),
    )
    text = getattr(resp, "text", None)
    if not text:
        raise ValueError("empty response")
    return text


def parse_json(text: str) -> dict:
    """json.loads with a tolerance for stray code fences; anything else is an error."""
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t[4:] if t.lower().startswith("json") else t
    try:
        obj = json.loads(t)
    except json.JSONDecodeError:
        a, b = t.find("{"), t.rfind("}")
        if a < 0 or b <= a:
            raise
        obj = json.loads(t[a:b + 1])
    if isinstance(obj, list):  # model returned the bare array
        obj = {"timeline": obj}
    if not isinstance(obj, dict):
        raise ValueError("top-level JSON is not an object")
    return obj


def plan_with_gemini(ctx: dict, client: Any, models: list[str], max_repairs: int = 1,
                     caller: Callable[[Any, str, str, str], str] = call_gemini,
                     sleep: Callable[[float], None] = time.sleep) -> tuple[dict | None, dict]:
    """
    Walk the fallback chain. Per model: ask once; if the answer fails validation, ask once more
    with the error list (max_repairs). Returns (plan or None, report).
    """
    report: dict[str, Any] = {"attempts": [], "model_used": None}
    available = discover_models(client) if client is not None else None
    chain = [m for m in models if available is None or m in available]
    skipped = [m for m in models if m not in chain]
    if skipped:
        log.info("Not served for this key, skipped: %s", ", ".join(skipped))
        report["not_served"] = skipped
    if not chain:
        log.warning("None of the chain models is served for this key")
        return None, report

    for pass_no in (1, 2):  # a second pass only retries models that failed for transient reasons
        retry_later: list[str] = []
        for model in (chain if pass_no == 1 else report.get("_retry", [])):
            errors: list[str] | None = None
            previous: str | None = None
            for attempt in range(1 + max_repairs):
                system, prompt = build_prompt(ctx, errors, previous)
                t0 = time.time()
                try:
                    raw = caller(client, model, system, prompt)
                except Exception as exc:  # noqa: BLE001 - classified below, never swallowed silently
                    kind = classify_error(exc)
                    log.warning("%s -> %s (%s: %s) in %.1fs", model, kind, type(exc).__name__,
                                str(exc)[:120], time.time() - t0)
                    report["attempts"].append({"model": model, "result": kind})
                    if kind in ("quota", "transient"):
                        retry_later.append(model)
                        sleep(1.0)
                    break  # next model
                try:
                    plan = parse_json(raw)
                except (ValueError, json.JSONDecodeError) as exc:
                    errors, previous = [f"E-JSON: answer was not valid JSON ({exc})"], raw
                    report["attempts"].append({"model": model, "result": "bad_json"})
                    continue
                fixed, notes = core.repair(plan, ctx)
                errs, warns = core.validate(fixed, ctx)
                if not errs:
                    log.info("%s produced a valid plan (%d shots, %d repairs, %d warnings)",
                             model, len(fixed["timeline"]), len(notes), len(warns))
                    report.update(model_used=model, repairs=notes, warnings=warns)
                    report["attempts"].append({"model": model, "result": "ok"})
                    report.pop("_retry", None)
                    return fixed, report
                log.warning("%s answer rejected: %d errors, e.g. %s", model, len(errs), errs[0])
                report["attempts"].append({"model": model, "result": "invalid", "errors": errs[:5]})
                errors, previous = errs, json.dumps(fixed)
        report["_retry"] = [m for m in dict.fromkeys(retry_later)]
        if pass_no == 1 and report["_retry"]:
            log.info("Waiting 20 s, then retrying %d busy model(s) once", len(report["_retry"]))
            sleep(20.0)
        else:
            break
    report.pop("_retry", None)
    return None, report


# --------------------------------------------------------------------------- #
# Offline safe-mode planner (no AI)
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
             section: str, shot_type_label: str | None = None, randomise: bool = False) -> dict | None:
        best = None
        for t_rank, typ in enumerate(types):
            cands = []
            for seg in self.cat:
                if seg["shot_type"] != typ:
                    continue
                for g0, g1 in self._gaps(seg):
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


def local_plan(ctx: dict, seed: int = 7) -> dict | None:
    """
    Deterministic planner following the rulebook as closely as the catalog allows.
    With no catalog it is content-blind (all 'unclassified'): structure and pacing are right, scene choice is not.
    """
    tpl, target = ctx["template"], ctx["target_s"]
    rng = random.Random(seed)
    cat = ctx.get("catalog") or auto_catalog(ctx["sources"])
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
        order = [t for t in intro_pref if t not in used_intro_types or t in ("fans_crowd", "dressing_room",
                                                                            "stylized_atmosphere")]
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
    state = {"goals": 0, "pen": 0, "face": 0, "wide_t": 0.0, "main_t": 0.0, "queue": []}
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
        if state["goals"] < 3 and t >= 35.0 and state["main_t"] > 0 and (i % 9 == 0):
            s = foot.take(["goal", "near_miss_shot"], min(want, 3.0), main_cfg["min_clip_s"], 4.0, avoid, "main")
            if s:
                state["goals"] += 1
                return add_main(s)
        if state["pen"] < 1 and t >= 90.0:
            s = foot.take(["penalty_goal"], min(want, 3.0), main_cfg["min_clip_s"], 4.0, avoid, "main")
            state["pen"] += 1
            if s:
                return add_main(s)
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


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Plan a long highlight video timeline (Gemini + validation).")
    ap.add_argument("--template", default="config/master_rendering_template.json")
    ap.add_argument("--sources-dir", required=True, help="folder with the raw source clips")
    ap.add_argument("--music", required=True, help="background music file")
    ap.add_argument("--catalog", default=None, help="optional shot catalog JSON (labels from a classifier stage)")
    ap.add_argument("--out", default="build/timeline.json")
    ap.add_argument("--no-ai", action="store_true", help="skip Gemini, use the offline planner only")
    ap.add_argument("--require-ai", action="store_true", help="fail (exit 4) instead of using the offline planner")
    ap.add_argument("--max-repairs", type=int, default=1, help="re-asks per model after a rejected answer")
    ap.add_argument("--exclude", nargs="*", default=list(core.DEFAULT_EXCLUDE),
                    help="file names in --sources-dir that are NOT footage (default: ref.mp4, the reference video)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None, client_factory: Callable[[], Any] = make_client) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    try:
        tpl = core.load_template(args.template)
        sources, skipped = core.scan_sources(Path(args.sources_dir), tpl["constraints"]["min_source_duration_s"], args.exclude)
        for s in skipped:
            log.warning("Skipped source - %s", s)
        if not sources:
            log.error("No usable source videos in %s", args.sources_dir)
            return 2
        audible = core.audible_end(args.music)
    except core.PlanError as exc:
        log.error("%s", exc)
        return 2

    target = core.target_length(audible)
    ctx: dict[str, Any] = {"template": tpl, "sources": sources, "target_s": target, "catalog": []}
    ctx["catalog"] = load_catalog(args.catalog, ctx)
    log.info("Music audible end %.2fs -> target %.1fs | %d sources", audible, target, len(sources))

    # Halt rule: not enough footage at all (checked before spending any API call)
    usable = sum(g["end"] - g["start"] for g in ctx["catalog"]) if ctx["catalog"] else \
        sum(v["duration"] for v in sources.values())
    if usable < tpl["constraints"]["min_usable_fraction"] * target:
        log.error("NEEDS MORE SOURCE VIDEO: %.0fs usable footage for a %.0fs video (need at least %.0fs). "
                  "Add more clips and run again.", usable, target,
                  tpl["constraints"]["min_usable_fraction"] * target)
        return 3

    plan, report, planner_name = None, {}, "none"
    if not args.no_ai:
        try:
            client = client_factory()
        except KeyError:
            log.warning("GEMINI_API_KEY is not set")
            client = None
        except Exception as exc:  # SDK import problems, bad key format, etc.
            log.warning("Could not create the Gemini client (%s: %s)", type(exc).__name__, str(exc)[:120])
            client = None
        if client is not None:
            plan, report = plan_with_gemini(ctx, client, MODEL_FALLBACK_CHAIN, max_repairs=args.max_repairs)
            if plan:
                planner_name = f"gemini:{report['model_used']}"
    if plan is None:
        if args.require_ai:
            log.error("No Gemini model produced a valid plan and --require-ai is set")
            return 4
        log.warning("Using the offline safe-mode planner")
        try:
            plan = local_plan(ctx, args.seed)
        except NeedsMoreSource as exc:
            log.error("NEEDS MORE SOURCE VIDEO: %s. Add more close-up / skill footage and run again.", exc)
            return 3
        planner_name = "offline_safe_mode"
        if plan is None:
            return 4

    out = {
        "_planner": {"planner": planner_name, "model_chain_report": {k: v for k, v in report.items()
                                                                        if k in ("attempts", "not_served", "repairs", "warnings")},
                     "notes": plan.pop("_notes", [])},
        "target_duration_s": target,
        "music_audible_end_s": round(audible, 3),
        "section_budgets_s": core.section_budgets(target, tpl),
        "timeline": plan["timeline"],
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2), encoding="utf-8")
    log.info("Wrote %s (%d shots, planner=%s)", args.out, len(out["timeline"]), planner_name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
