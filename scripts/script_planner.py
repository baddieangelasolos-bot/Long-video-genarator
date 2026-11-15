#!/usr/bin/env python3
"""
script_planner.py - source clips + music + master template -> a verified timeline.json.
Gemini (google-genai, 10-model chain, JSON-only output) proposes; timeline_core repairs and validates every answer;
if every model fails a deterministic offline planner builds the timeline.
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
import catalog_tools as ct  # noqa: E402
import plan_report  # noqa: E402
import timeline_core as core  # noqa: E402

log = logging.getLogger("planner")

# Owner's ordered chain; names the API does not serve for this key are skipped (discover_models).
MODEL_FALLBACK_CHAIN = [
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3-flash-preview",
    "gemini-1.5-flash",
    "gemini-1.5-pro",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash-preview",
    "gemini-2.0-flash-lite",
]
REQUEST_TIMEOUT_S = 75     # per request; a hanging model must not eat the whole run
MAX_CATALOG_LINES = 360    # keeps the prompt small enough for lite models; the sample keeps every type and source (balanced_sample)


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
        top = ct.balanced_sample(cat, MAX_CATALOG_LINES)
        lines.append("SUPPLY_SECONDS_BY_TYPE (whole catalog): " + json.dumps(ct.supply_by_type(cat)))
        lines.append("MUST (checked by code, a plan that breaks one is rejected): "
                     "(1) skill clips (close_skill, semi_close, duel_1v1, run_making, ball_receive_pass, good_pass_chance) are at least "
                     f"{int(cons.get('skill_share_min_of_main', 0.5) * 100)}% of the main part and goals never fill it; "
                     "(2) if bus_arrival, player_exit_bus_or_tunnel or dressing_room exist, the intro opens with bus_arrival (else player_exit) "
                     "and then uses the others before fans or celebration; "
                     "(3) a celebration clip in main comes ONLY directly after a goal or penalty_goal clip (same source, within 25 s after it, "
                     "at most 3 s); (4) build each goal from 2-3 short parts in time order; (5) quality 2 segments only when a section would be short.")
        lines.append("CATALOG (file|start|end|shot_type|quality). A shot's whole window must sit inside ONE catalog "
                     "segment of the same shot_type:")
        lines += [f"{g['source_video_file']}|{g['start']}|{g['end']}|{g['shot_type']}|{g['quality']}" for g in top]
    else:
        lines.append("CATALOG: none. Shot types are unknown, so use shot_type \"unclassified\" for every shot and "
                     "spread clips across all sources and across each source's length.")
    lines += [
        "RULEBOOK: " + json.dumps(tpl["planning_rulebook"], separators=(",", ":")),
        "LIMITS (allowed_types = the ONLY shot_type values allowed in that section): " + json.dumps({"sections": cons["sections"],
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


from offline_planner import (NeedsMoreSource, _Footage, auto_catalog, local_plan, local_plan_robust,  # noqa: E402,F401
                             supply_report)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Plan a long highlight video timeline (Gemini + validation).")
    ap.add_argument("--template", default="config/master_rendering_template.json")
    ap.add_argument("--sources-dir", required=True, help="folder with the raw source clips")
    ap.add_argument("--music", required=True, help="background music file")
    ap.add_argument("--catalog", default=None, help="optional shot catalog JSON (labels from a classifier stage)")
    ap.add_argument("--dups", default=None, help="excluded duplicate ranges JSON (from dup_scan.py)")
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
    ctx["dups"] = ct.load_dups(args.dups)
    ctx["catalog"] = ct.subtract_ranges(load_catalog(args.catalog, ctx), ctx["dups"])
    if ctx["dups"]:
        log.info("Duplicate footage: %d excluded range(s)", len(ctx["dups"]))
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
            plan = local_plan_robust(ctx, args.seed)
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
        "_report": plan_report.build(ctx, plan["timeline"], planner_name),
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2), encoding="utf-8")
    log.info("Wrote %s (%d shots, planner=%s)", args.out, len(out["timeline"]), planner_name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
