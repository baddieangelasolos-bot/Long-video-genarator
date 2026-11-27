#!/usr/bin/env python3
"""
brain_guard.py - the "brain rule" for the master planning job (v4). Pure functions, stdlib only.

Owner rule: the video script/plan is made ONLY by the Gemini brain chain (v5: the whole planner chain in
script_planner.MODEL_FALLBACK_CHAIN, primary first). If no model of the chain can make a plan that passes the master template
rules (quota used up, overloaded, not served, no key), the workflow STOPS and mails the owner when to run it again. No
offline / low-quality fallback video is made. The other Gemini jobs (identify, labelling) keep their own router chains.
Times in the mails are shown in Dhaka time (the owner's time zone, UTC+6, no daylight saving) first, then UTC and Pacific.

What this module decides:
  * describe_error()  - turn one failed Gemini call into a record with HTTP code, short message, retry hint, daily flag and
                        the time the model should be usable again (with how that time was obtained).
  * summarize()       - turn all attempts of a run into one halt report: per-model lines, earliest time to run again, text.
Honesty rule: a time is labelled EXACT only when it comes from Google (a retry hint in the error, or the documented daily
quota reset at midnight Pacific time). Overload errors (503, timeouts) have no published end time: the time shown for them is
a SUGGESTION and says so.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any

BRAIN_SIZE = 3                   # v4 value, kept for old callers; v5 plans with the whole chain (len(brain) is used everywhere)
OVERLOAD_SUGGEST_MIN = 30        # suggested wait after 503 / timeouts (a suggestion, not a Google time)
UNKNOWN_QUOTA_TRY_MIN = 10       # earliest sensible retry when Google names no quota type and no retry time

PACIFIC = "America/Los_Angeles"
DHAKA = timezone(timedelta(hours=6))     # Bangladesh: UTC+6 all year (no daylight saving since 2009)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Pacific time (Gemini daily quotas reset at midnight Pacific time)
# --------------------------------------------------------------------------- #
def _nth_sunday(year: int, month: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(6 - d.weekday()) % 7)      # first Sunday
    return d + timedelta(weeks=n - 1)


def _us_dst(utc_dt: datetime) -> bool:
    """US daylight saving: second Sunday of March 02:00 local standard (10:00 UTC) to first Sunday of November 02:00 daylight (09:00 UTC)."""
    y = utc_dt.year
    start = datetime.combine(_nth_sunday(y, 3, 2), dtime(10, 0), tzinfo=timezone.utc)
    end = datetime.combine(_nth_sunday(y, 11, 1), dtime(9, 0), tzinfo=timezone.utc)
    return start <= utc_dt < end


def _pacific_offset_manual(utc_dt: datetime) -> timedelta:
    return timedelta(hours=-7 if _us_dst(utc_dt) else -8)


def to_pacific(utc_dt: datetime) -> tuple[datetime, str]:
    """(aware datetime in Pacific time, abbreviation PST/PDT)."""
    try:
        from zoneinfo import ZoneInfo
        loc = utc_dt.astimezone(ZoneInfo(PACIFIC))
        return loc, loc.tzname() or "PT"
    except Exception:                                # no tz database on this machine: use the rule above
        off = _pacific_offset_manual(utc_dt)
        return utc_dt.astimezone(timezone(off)), ("PDT" if off.total_seconds() == -7 * 3600 else "PST")


def daily_reset(now_utc: datetime) -> datetime:
    """The next 00:00 Pacific time after now_utc, as an aware UTC datetime."""
    loc, _ = to_pacific(now_utc)
    next_day = loc.date() + timedelta(days=1)
    try:
        from zoneinfo import ZoneInfo
        return datetime.combine(next_day, dtime(0, 0), tzinfo=ZoneInfo(PACIFIC)).astimezone(timezone.utc)
    except Exception:
        local_mid = datetime.combine(next_day, dtime(0, 0), tzinfo=timezone.utc)      # same wall time, treated as UTC
        for hours in (7, 8):                                                           # midnight is never ambiguous
            cand = local_mid + timedelta(hours=hours)
            if _pacific_offset_manual(cand) == timedelta(hours=-hours):
                return cand
        return local_mid + timedelta(hours=8)


def to_dhaka(utc_dt: datetime) -> datetime:
    return utc_dt.astimezone(DHAKA)


def fmt_when(utc_dt: datetime, now_utc: datetime | None = None) -> str:
    """'2026-11-22 14:00 Dhaka time = 2026-11-22 08:00 UTC = 2026-11-22 00:00 PST (Pacific), in 5 h 12 min'."""
    loc, abbr = to_pacific(utc_dt)
    s = (f"{to_dhaka(utc_dt).strftime('%Y-%m-%d %H:%M')} Dhaka time = {utc_dt.strftime('%Y-%m-%d %H:%M')} UTC = "
         f"{loc.strftime('%Y-%m-%d %H:%M')} {abbr} (Pacific)")
    if now_utc is not None:
        secs = int((utc_dt - now_utc).total_seconds())
        if secs > 0:
            h, rem = divmod(secs, 3600)
            s += f", in {h} h {rem // 60:02d} min" if h else f", in {rem // 60} min"
        else:
            s += ", already passed: run it now"
    return s


def short_when(utc_dt: datetime) -> str:
    """'2026-11-22 14:00 Dhaka (08:00 UTC)' - used in the mail subject and the per-model lines."""
    return f"{to_dhaka(utc_dt).strftime('%Y-%m-%d %H:%M')} Dhaka ({utc_dt.astimezone(timezone.utc).strftime('%H:%M')} UTC)"


# --------------------------------------------------------------------------- #
# Reading one Gemini error
# --------------------------------------------------------------------------- #
_RETRY_RX = (re.compile(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)\s*s", re.I),
             re.compile(r"retry in\s+(\d+(?:\.\d+)?)\s*s", re.I))
_DAILY_RX = re.compile(r"PerDay|per day|requests per day|RequestsPerDay|TokensPerDay|daily", re.I)
_MINUTE_RX = re.compile(r"PerMinute|per minute", re.I)
_QUOTAID_RX = re.compile(r"quotaId['\"]?\s*[:=]\s*['\"]([^'\"]+)['\"]", re.I)
_KEY_RX = re.compile(r"(key=|api[_-]?key['\"]?\s*[:=]\s*['\"]?)[A-Za-z0-9_\-]{12,}", re.I)


def _text(exc: Any) -> str:
    parts = [str(exc)]
    for attr in ("details", "response_json", "message", "status"):
        v = getattr(exc, attr, None)
        if v:
            parts.append(repr(v) if not isinstance(v, str) else v)
    return " ".join(parts)


def scrub(text: str) -> str:
    """Never let anything key-like into a mail or a log."""
    return _KEY_RX.sub(r"\1***", text)


def quota_info(text: str) -> dict:
    """{'daily': bool, 'minute': bool, 'retry_after_s': float|None, 'quota_id': str|None} read from an error text."""
    retry = None
    for rx in _RETRY_RX:
        m = rx.search(text)
        if m:
            retry = float(m.group(1))
            break
    qid = _QUOTAID_RX.search(text)
    return {"daily": bool(_DAILY_RX.search(text)), "minute": bool(_MINUTE_RX.search(text)),
            "retry_after_s": retry, "quota_id": qid.group(1) if qid else None}


def describe_error(kind: str, exc: BaseException, now_utc: datetime) -> dict:
    """
    Extra fields for one failed attempt. kind is script_planner.classify_error(): skip | quota | transient | other.
    Keys: http, message, state, and (when a time is known) at (ISO UTC), basis (exact | safe | suggested), plus hint fields.
    """
    text = _text(exc)
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    out: dict[str, Any] = {"http": code if isinstance(code, int) else None, "message": scrub(str(exc))[:200]}
    if kind == "quota":
        q = quota_info(text)
        out.update(retry_after_s=q["retry_after_s"], quota_id=q["quota_id"])
        if q["daily"]:        # a daily limit wins over any retry hint Google attaches to the same error
            out.update(state="quota_daily", at=daily_reset(now_utc).isoformat(), basis="exact")
        elif q["retry_after_s"] is not None:
            out.update(state="quota_minute", at=(now_utc + timedelta(seconds=q["retry_after_s"] + 2)).isoformat(), basis="exact")
        else:                 # Google named neither: the daily reset is the safe time, a per-minute limit may clear sooner
            out.update(state="quota_unknown", at=daily_reset(now_utc).isoformat(), basis="safe",
                       try_at=(now_utc + timedelta(minutes=UNKNOWN_QUOTA_TRY_MIN)).isoformat())
    elif kind == "transient":
        out.update(state="overload", at=(now_utc + timedelta(minutes=OVERLOAD_SUGGEST_MIN)).isoformat(), basis="suggested")
    elif kind == "skip":
        out.update(state="key_or_model")        # 400/401/403/404: model not served for this key or key refused; waiting does not help
    else:
        out.update(state="other_error")
    return out


def retry_wait(attempts: list[dict], pass_no: int) -> float:
    """Seconds to wait before retry pass `pass_no` (2 or 3): 20 s then 60 s, longer (max 90 s) if Google's hint asks for it."""
    base = 20.0 if pass_no == 2 else 60.0
    hints = [a["retry_after_s"] for a in attempts if a.get("retry_after_s") is not None and a.get("state") == "quota_minute"]
    if hints:
        return float(min(90.0, max(base, min(hints) + 2.0)))
    return base


# --------------------------------------------------------------------------- #
# The halt report
# --------------------------------------------------------------------------- #
_STATE_TEXT = {
    "quota_daily": "daily quota used up",
    "quota_minute": "rate limit hit",
    "quota_unknown": "quota/rate limit hit (Google named no type or time)",
    "overload": "busy / overloaded (server error or timeout)",
    "key_or_model": "not usable with this API key (model not served or key refused)",
    "invalid_plan": "answered, but the plan broke the template rules",
    "other_error": "unexpected error",
    "not_served": "not served for this API key",
    "not_tried": "not tried (no Gemini client, or the planning time budget was used up first)",
}
_BASIS_TEXT = {
    "exact": "EXACT: taken from Google (retry hint in the error, or the documented daily reset at midnight Pacific time).",
    "safe": ("SAFE TIME: Google gave no retry time and no quota type. A per-minute limit clears within minutes, a daily limit at "
             "midnight Pacific time. This is the later, safe time."),
    "suggested": ("SUGGESTION, NOT A GOOGLE TIME: overload (server error / timeout) has no published end time. "
                  f"Try again after about {OVERLOAD_SUGGEST_MIN} minutes."),
}


def _row(model: str, a: dict | None, state: str | None = None) -> dict:
    if a is None:
        return {"model": model, "state": state or "not_tried"}
    res = a.get("result")
    st = a.get("state") or ("invalid_plan" if res in ("invalid", "bad_json") else state or "other_error")
    row = {"model": model, "state": st, "http": a.get("http"), "message": a.get("message", ""), "at": a.get("at"),
           "basis": a.get("basis"), "try_at": a.get("try_at"), "retry_after_s": a.get("retry_after_s")}
    return row


def summarize(attempts: list[dict], brain: list[str], not_served: list[str], now_utc: datetime, key_missing: bool = False) -> dict:
    """One halt report for the brain chain. Returns a dict with 'text' (the mail body part), 'subject_when', 'rows'."""
    last: dict[str, dict] = {}
    for a in attempts:
        if a.get("model") in brain and a.get("result") != "ok":
            last[a["model"]] = a
    rows = []
    for m in brain:
        if m in not_served:
            rows.append(_row(m, None, "not_served"))
        elif m in last:
            rows.append(_row(m, last[m]))
        else:
            rows.append(_row(m, None, "not_tried"))
    timed = [r for r in rows if r.get("at")]
    pick = min(timed, key=lambda r: r["at"]) if timed else None      # ISO UTC strings sort in time order
    states = {r["state"] for r in rows}
    if key_missing:
        cause, reason = "no_key", "the GEMINI_API_KEY secret is missing or the Gemini client could not start"
    elif pick is not None:
        cause = {"quota_daily": "quota", "quota_minute": "quota", "quota_unknown": "quota", "overload": "overload"}[pick["state"]]
        reason = _STATE_TEXT[pick["state"]]
    elif "invalid_plan" in states:
        cause, reason = "invalid_plan", "every model that answered broke the master template rules (a new run asks again)"
    else:
        cause, reason = "key_or_model", "none of the brain models is usable with this API key (waiting will not fix this)"

    n = len(brain)
    L = ["THE GEMINI BRAIN IS UNAVAILABLE. NO VIDEO WAS MADE, ON PURPOSE (your rule: no low-quality fallback video).",
         f"These {n} models plan the video script with the master template, in this order (primary + {n - 1} fallbacks):"]
    skipped = []
    for i, r in enumerate(rows, 1):
        if r["state"] == "not_served":
            skipped.append(r["model"])
            continue
        line = f" {i}. {r['model']}: {_STATE_TEXT.get(r['state'], r['state'])}"
        if r.get("http"):
            line += f" [HTTP {r['http']}]"
        if r.get("at"):
            lead = {"exact": "usable again at", "safe": "safe to retry after", "suggested": "suggested retry after (not a Google time)"}.get(r.get("basis"), "usable again about")
            line += f"; {lead} {short_when(datetime.fromisoformat(r['at']))}"
        L.append(line)
    if skipped:
        L.append(" Not served for this API key (skipped, waiting will not change this): " + ", ".join(skipped))
    out: dict[str, Any] = {"cause": cause, "reason": reason, "brain_models": list(brain), "rows": rows, "key_missing": key_missing,
                           "created_utc": now_utc.isoformat(), "retry_at": None, "basis": None, "subject_when": "see the mail"}
    if pick is not None:
        at = datetime.fromisoformat(pick["at"])
        out.update(retry_at=at.isoformat(), basis=pick["basis"], subject_when=short_when(at))
        L += ["", "RUN THE WORKFLOW AGAIN AFTER: " + fmt_when(at, now_utc), _BASIS_TEXT[pick["basis"]]]
        if pick["basis"] == "safe" and pick.get("try_at"):
            L.append("Earliest worth trying (only if it was a per-minute limit): " + short_when(datetime.fromisoformat(pick["try_at"])) + ".")
        L.append("Model used for the time above: " + pick["model"] + ". Any one of these models being back is enough to plan.")
        sure = [r for r in timed if r["basis"] in ("exact", "safe")]
        if pick["basis"] == "suggested" and sure:
            s0 = min(sure, key=lambda r: r["at"])
            L.append("If it is still busy then, the next reliable time is " + fmt_when(datetime.fromisoformat(s0["at"]), now_utc) + " (" + s0["model"] + ").")
    else:
        L += ["", "WAITING ALONE WILL NOT FIX THIS: " + reason + "."]
        if cause in ("no_key", "key_or_model"):
            L.append("Check the GEMINI_API_KEY secret in GitHub and which models your key can use in Google AI Studio.")
        elif cause == "invalid_plan":
            L.append("Run the workflow again: Gemini gives a new answer each time. If it repeats, send me the logs.")
    L += ["", "Google resets daily Gemini quotas at midnight Pacific time. Identify and labelling share the same Gemini quota, so a",
          "run that labels many shots can use up what the planner needs."]
    out["text"] = "\n".join(L)
    return out
