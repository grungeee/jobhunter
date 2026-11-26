#!/usr/bin/env python3
"""Normalized job feed for the job-search project.

Reads the AMS scan data read-only and produces one flat, deterministic feed of
job listings with their tracking status. The feed is what the dashboard renders.

CLI
  python3 -B tools/feed.py                 build and print a human table
  python3 -B tools/feed.py --json          build and print the feed as JSON
  python3 -B tools/feed.py --write         build and write .ai/feed/jobs.json
  python3 -B tools/feed.py --status new    only listings in the given status
  python3 -B tools/feed.py --min-score 8   floor filter
  python3 -B tools/feed.py --limit 20      truncate the printed table

Sources (read-only, never modified here)
  ../ams/state.json          every listing id the scanner has ever seen
  ../ams/raw_jobs.json       the most recent full scan
  ../ams/scored_jobs.json    current scores
  ../ams/applications.json   tracking status written by the nudge pipeline

Degraded mode: a missing or unreadable source produces a warning and an empty
contribution instead of a crash, and the feed records which sources were used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WORKSPACE = os.path.abspath(os.environ.get("JOBHUNTER_WORKSPACE", ROOT))
# AMS_FEED_SOURCE lets a sandbox or deployment read a private tracker source.
# Unset means the sibling ams directory in a local checkout.
DEFAULT_SOURCE = (os.environ.get("AMS_FEED_SOURCE")
                  or os.path.normpath(os.path.join(WORKSPACE, "..", "ams")))
FEED_DIR = os.environ.get("JOBHUNTER_FEED_DIR", os.path.join(WORKSPACE, ".ai", "feed"))
FEED_FILE = os.path.join(FEED_DIR, "jobs.json")

STATUS_ORDER = ["success", "interview", "applied", "new", "nudged", "unclear",
                "blocked", "rejected", "skipped", "expired"]
STATUS_LABEL = {
    "success": "Success",
    "interview": "Interview",
    "applied": "Applied",
    "unclear": "Unclear",
    "blocked": "Blocked",
    "rejected": "Rejected",
    "new": "New",
    "nudged": "Asked",
    "skipped": "Skipped",
    "expired": "Expired",
}
OPEN_STATUSES = {"new", "nudged"}
TERMINAL_STATUSES = {"interview", "success", "rejected", "blocked", "unclear",
                     "skipped", "expired"}

# Process stages. The tracker stores the stage; the coarse status above is
# derived from it, so every older reader keeps working. Keep this map in step
# with ams_nudge.STAGE_STATUS (tests/test_stages.py enforces that).
STAGE_FLOW = ["unread", "read", "generated", "checked", "sent", "confirmed",
              "waiting", "contact"]
STAGE_OUTCOME = ["interview", "preparation", "awaiting", "success", "rejected",
                 "blocked", "unclear", "skipped"]
STAGE_ORDER = STAGE_FLOW + STAGE_OUTCOME
STAGE_LABEL = {
    "unread": "Not read",
    "read": "Reading",
    "generated": "Generating application",
    "checked": "Control application",
    "sent": "Sending out",
    "confirmed": "Confirming",
    "waiting": "Waiting",
    "contact": "Call/Contact",
    "interview": "Interview",
    "preparation": "Interview preparation",
    "awaiting": "Waiting for outcome",
    "success": "Success",
    "rejected": "Fail",
    "blocked": "Blocked",
    "unclear": "Unclear",
    "skipped": "Skipped",
}
STAGE_SHORT = {
    "unread": "Unread", "read": "Read", "generated": "Gen", "checked": "Ctrl",
    "sent": "Sent", "confirmed": "Conf", "waiting": "Wait", "contact": "Call",
    "preparation": "Prep", "awaiting": "Await",
}
STAGE_STATUS = {
    "unread": "new", "read": "new", "generated": "new", "checked": "new",
    "sent": "applied", "confirmed": "applied", "waiting": "applied",
    "contact": "applied",
    "interview": "interview", "preparation": "interview", "awaiting": "interview",
    "success": "success", "rejected": "rejected",
    "blocked": "blocked", "unclear": "unclear", "skipped": "skipped",
}
STATUS_STAGE = {
    "new": "unread", "nudged": "read", "applied": "sent",
    "interview": "interview", "blocked": "blocked", "skipped": "skipped",
    "expired": "unclear", "rejected": "rejected", "success": "success",
    "unclear": "unclear",
}
STAGE_WAIT_DAYS = 7        # waiting longer than this escalates to call/contact


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_source(path):
    """Return (payload, meta). A missing or corrupt file degrades to empty."""
    meta = {"path": path, "present": os.path.exists(path)}
    if not meta["present"]:
        meta["error"] = "missing"
        return None, meta
    try:
        with open(path, "rb") as handle:
            blob = handle.read()
        meta["sha256"] = hashlib.sha256(blob).hexdigest()
        meta["bytes"] = len(blob)
        return json.loads(blob.decode("utf-8")), meta
    except (ValueError, OSError) as exc:
        meta["error"] = "%s: %s" % (type(exc).__name__, exc)
        return None, meta


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(value):
    return "" if value is None else str(value).strip()


def _iso_date(value):
    """Normalize a date to YYYY-MM-DD. AMS writes DD.MM.YYYY, scoring writes ISO."""
    text = _text(value)
    if not text:
        return ""
    parts = text.split("T", 1)[0].split(" ", 1)[0]
    bits = parts.split(".")
    if len(bits) == 3 and len(bits[2]) == 4 and bits[0].isdigit():
        return "%s-%s-%s" % (bits[2], bits[1].zfill(2), bits[0].zfill(2))
    bits = parts.replace("/", "-").split("-")
    if len(bits) == 3 and len(bits[0]) == 4 and bits[0].isdigit():
        return "%s-%s-%s" % (bits[0], bits[1].zfill(2), bits[2].zfill(2))
    return ""


def _days_between(earlier, later):
    try:
        start = datetime.strptime(earlier, "%Y-%m-%d")
        end = datetime.strptime(later, "%Y-%m-%d")
    except (TypeError, ValueError):
        return None
    return (end - start).days


def _plus_days(iso_date, days):
    """Return iso_date shifted forward, or "" when the date is unusable."""
    try:
        base = datetime.strptime(iso_date, "%Y-%m-%d")
    except (TypeError, ValueError):
        return ""
    return (base + timedelta(days=int(days))).strftime("%Y-%m-%d")


_PAY_AMOUNT = re.compile(r"(?<![\w.])(\d{1,3}(?:[. ]\d{3})+(?:,\d{1,2})?|\d{4,5}(?:,\d{1,2})?)(?!\d)")
_PAY_MONTH = re.compile(r"monatlich|pro\s+Monat|/\s*Monat|je\s+Monat|Monatsbrutto\w*"
                        r"|Monats(?:lohn|gehalt|entgelt|entlohnung|bezug|einkommen)\w*"
                        r"|Bruttomonats\w*", re.I)
_PAY_CONTEXT = re.compile(r"Gehalt|Lohn|Entgelt|Entlohnung|Vergütung|€|\bEUR\b", re.I)
_PAY_ANNUAL = re.compile(r"Jahresgehalt|Jahresbrutto\w*|Jahresentgelt|Jahreslohn"
                         r"|pro\s+Jahr|jährlich|p\.a\.", re.I)
# A monthly figure above the ceiling is a yearly figure typed with a monthly
# word next to it, and a yearly figure below the floor is a monthly one.
_PAY_MONTHLY_CEILING = 50000.0
_PAY_ANNUAL_FLOOR = 1000.0
_PAY_ANNUAL_CEILING = 1000000.0


def _pay_amount(value):
    """Parse an Austrian-formatted amount: 2.920,50 -> 2920.5, else None."""
    try:
        return float(str(value).replace(".", "").replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def _pay_window(text, match, reach=55):
    """Text around a number, wide enough to carry the basis and the currency."""
    start = max(0, match.start() - reach)
    return text[start:match.end() + reach], (match.start() + match.end()) / 2.0 - start


def _pay_basis(context, centre):
    """Which basis word sits closest to the amount: 'month', 'year', or ''.

    Ads mix the two ("Jahresgehalt 60.000, monatlich wechselnde Dienste"), so
    the nearest marker decides instead of any match in the whole window.
    """
    marks = []
    for label, pattern in (("year", _PAY_ANNUAL), ("month", _PAY_MONTH)):
        for found in pattern.finditer(context):
            marks.append((abs((found.start() + found.end()) / 2.0 - centre), label))
    if not marks:
        return ""
    marks.sort(key=lambda item: (item[0], item[1] != "year"))
    return marks[0][1]


def _pay_monthly_min(snippet):
    """Only extract an amount when the ad explicitly says it is monthly."""
    text = _text(snippet)
    for match in _PAY_AMOUNT.finditer(text):
        context, centre = _pay_window(text, match)
        if not _PAY_CONTEXT.search(context):
            continue
        if _pay_basis(context, centre) != "month":
            continue
        amount = _pay_amount(match.group(1))
        if amount is not None and 100 <= amount <= _PAY_MONTHLY_CEILING:
            return amount
    return None


def _pay_yearly_min(text_source):
    """Extract an amount the ad states on an explicit yearly basis."""
    text = _text(text_source)
    for match in _PAY_AMOUNT.finditer(text):
        context, centre = _pay_window(text, match)
        if not _PAY_CONTEXT.search(context):
            continue
        if _pay_basis(context, centre) != "year":
            continue
        amount = _pay_amount(match.group(1))
        if amount is not None and _PAY_ANNUAL_FLOOR <= amount <= _PAY_ANNUAL_CEILING:
            return amount
    return None


# Austrian gross pay is quoted in 14 monthly payments a year: twelve salary
# months plus holiday and Christmas pay, which arrive with the June and
# November salary. A yearly figure is therefore divided by 14, never by 12.
PAYMENTS_PER_YEAR_AT = 14


def monthly_from_yearly(yearly):
    """Monthly gross from an Austrian yearly figure: yearly / 14, in cents."""
    if not isinstance(yearly, (int, float)) or isinstance(yearly, bool):
        return None
    if not math.isfinite(yearly) or yearly <= 0:
        return None
    return round(yearly / PAYMENTS_PER_YEAR_AT, 2)


def _pay_terms(*sources):
    """Monthly and yearly figures from the first sources that state either.

    Sources are searched in priority order (search card, AMS general info,
    captured ad body). Both bases are kept so the UI can show the monthly
    figure when the ad gives one and the yearly figure when that is all
    there is. Nothing is converted between the two.
    """
    monthly = yearly = None
    for source in sources:
        text = _text(source)
        if not text:
            continue
        if monthly is None:
            monthly = _pay_monthly_min(text)
        if yearly is None:
            yearly = _pay_yearly_min(text)
        if monthly is not None and yearly is not None:
            break
    return monthly, yearly


CAPTURED_TEXT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             ".ai", "feed", "listing_text.json")


def captured_details(path=CAPTURED_TEXT):
    """Structured AMS detail cache, keyed by exact listing id."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: value for key, value in data.items()
            if isinstance(key, str) and isinstance(value, dict)}


def captured_texts(path=CAPTURED_TEXT):
    """Legacy read interface for ad bodies captured from listing pages."""
    return {key: value["text"] for key, value in captured_details(path).items()
            if isinstance(value.get("text"), str) and value["text"].strip()}


DETAIL_FIELDS = ("company_description", "occupation", "job_description", "general_info")


def enrich_with_cached_details(feed, cache):
    """Add verified detail sections without discarding a prior scan snapshot."""
    result = dict(feed)
    result["jobs"] = []
    for old in feed.get("jobs") or []:
        row = dict(old)
        detail = cache.get(row.get("id")) or {}
        if not isinstance(detail, dict):
            detail = {}
        for key in DETAIL_FIELDS:
            stored = detail.get(key)
            if isinstance(stored, str) and stored.strip() and not row.get(key):
                row[key] = stored.strip()
            else:
                row.setdefault(key, "")
        stored_text = detail.get("text")
        if isinstance(stored_text, str) and stored_text.strip() and not row.get("full_text"):
            row["full_text"] = stored_text.strip()
        else:
            row.setdefault("full_text", "")
        result["jobs"].append(row)
    return result


def build_feed(source_dir=DEFAULT_SOURCE, text_cache=None):
    """Build the feed dict from the AMS sources. Pure function of the inputs."""
    warnings = []
    sources = {}
    captured = captured_details(text_cache) if text_cache else captured_details()
    state, state_meta = read_source(os.path.join(source_dir, "state.json"))
    raw, raw_meta = read_source(os.path.join(source_dir, "raw_jobs.json"))
    scored, scored_meta = read_source(os.path.join(source_dir, "scored_jobs.json"))
    apps, apps_meta = read_source(os.path.join(source_dir, "applications.json"))
    for name, meta in (("state", state_meta), ("raw", raw_meta),
                       ("scored", scored_meta), ("applications", apps_meta)):
        sources[name] = meta
        if meta.get("error"):
            warnings.append("source %s unavailable (%s)" % (name, meta["error"]))

    seen = {}
    if isinstance(state, dict):
        seen = state.get("seen") or {}
    raw_jobs = {}
    if isinstance(raw, dict):
        raw_jobs = {job.get("uuid"): job for job in raw.get("jobs") or [] if job.get("uuid")}
    scores = {}
    if isinstance(scored, list):
        scores = {job.get("uuid"): job for job in scored if isinstance(job, dict) and job.get("uuid")}
    tracked = {}
    if isinstance(apps, dict):
        tracked = apps.get("jobs") or {}

    scan_date = _iso_date((raw or {}).get("scraped_at")) if isinstance(raw, dict) else ""
    ids = set(seen) | set(raw_jobs) | set(tracked) | set(scores)
    jobs = []
    for job_id in ids:
        history = seen.get(job_id) or {}
        current = raw_jobs.get(job_id) or {}
        score_rec = scores.get(job_id) or {}
        track = tracked.get(job_id) or {}

        score = _num(score_rec.get("score"))
        score_source = "scored_jobs.json" if score is not None else ""
        if score is None:
            score = _num(history.get("score"))
            if score is not None:
                score_source = "state.json (historical)"
        if score is None:
            score = _num(track.get("score"))
            if score is not None:
                score_source = "applications.json"

        posted_raw = (current.get("datum") or score_rec.get("datum_iso")
                      or score_rec.get("datum") or track.get("datum"))
        posted_iso = _iso_date(posted_raw)
        posted_basis = "posting" if posted_iso else ""
        if not posted_iso:
            # No posting date survived in any source: fall back to when we saw it.
            posted_iso = _iso_date(history.get("last_seen") or history.get("first_seen")
                                   or scan_date)
            posted_basis = "extraction" if posted_iso else ""

        status = _text(track.get("status"))
        raw_stage = _text(track.get("stage")).lower()
        if status not in STATUS_ORDER:
            # An untracked listing has no record at all: a job that never made
            # it into applications.json has never been opened either.
            status = "new"
        if raw_stage in STAGE_STATUS:
            stage = raw_stage
            # A flow stage outranks the coarse word it was written from, so a
            # sent application stops reading as only nudged. Terminal branches
            # keep their own word: reached, refused, or unreadable.
            if status not in TERMINAL_STATUSES:
                status = STAGE_STATUS.get(raw_stage, status)
        else:
            stage = STATUS_STAGE.get(status, "unread")
        stage_at = _iso_date(track.get("stage_at") or track.get("responded_at")
                             or track.get("sent_at"))
        stage_due = ""
        if stage_at and stage in ("sent", "confirmed", "waiting"):
            stage_due = _plus_days(stage_at, STAGE_WAIT_DAYS)
        roles = score_rec.get("matched_roles") or track.get("roles") or []
        skills = score_rec.get("matched_skills") or track.get("skills") or []
        snippet = _text(current.get("snippet") or score_rec.get("snippet"))
        # A search card is not a full ad. Only carry text captured from a
        # listing detail source, never promote a truncated card preview.
        detail = current.get("full_text") or score_rec.get("full_text")
        full_text = detail.strip() if isinstance(detail, str) else ""
        if not full_text:
            cached_text = (captured.get(job_id) or {}).get("text")
            full_text = cached_text.strip() if isinstance(cached_text, str) else ""
        general_info = _text(current.get("general_info")
                             or (captured.get(job_id) or {}).get("general_info"))
        # Pay is often only in the AMS general-info sentence or the ad body,
        # never in the search card, so all three are searched in that order.
        pay_monthly, pay_yearly = _pay_terms(snippet, general_info, full_text)

        jobs.append({
            "id": job_id,
            "title": _text(current.get("title") or history.get("title") or track.get("title")),
            "company": _text(current.get("company") or history.get("company")
                             or track.get("company")),
            "location": _text(current.get("ort") or history.get("ort") or track.get("ort")),
            "url": _text(current.get("url") or history.get("url") or track.get("url")),
            "score": score,
            "score_source": score_source,
            "pay_monthly_min": pay_monthly,
            "pay_yearly_min": pay_yearly,
            # A yearly figure is turned into a comparable monthly one with the
            # Austrian 14 payments a year; the ad's own wording stays in
            # pay_yearly_min and the UI labels the figure as calculated.
            "pay_monthly_calc": (None if pay_monthly is not None
                                 else monthly_from_yearly(pay_yearly)),
            "pay_basis": ("month" if pay_monthly is not None
                          else ("year" if pay_yearly is not None else "")),
            "posted_date": posted_iso,
            "posted_basis": posted_basis,
            "posted_age_days": _days_between(posted_iso, scan_date) if posted_basis == "posting" else None,
            "employment_type": _text(current.get("zeit") or score_rec.get("zeit")
                                     or track.get("zeit")),
            "occupation_group": _text(current.get("berufsgruppe")
                                      or score_rec.get("berufsgruppe")),
            "snippet": snippet,
            "full_text": full_text,
            "company_description": _text(current.get("company_description")
                                         or (captured.get(job_id) or {}).get("company_description")),
            "occupation": _text(current.get("occupation")
                                or (captured.get(job_id) or {}).get("occupation")),
            "job_description": _text(current.get("job_description")
                                     or (captured.get(job_id) or {}).get("job_description")),
            "general_info": _text(current.get("general_info")
                                  or (captured.get(job_id) or {}).get("general_info")),
            "first_seen": _text(history.get("first_seen")),
            "last_seen": _text(history.get("last_seen")),
            "in_latest_scan": job_id in raw_jobs,
            "status": status,
            "status_label": STATUS_LABEL.get(status, status),
            "stage": stage,
            "stage_label": STAGE_LABEL.get(stage, stage),
            "stage_short": STAGE_SHORT.get(stage) or STAGE_LABEL.get(stage, stage),
            "stage_at": stage_at,
            "stage_due": stage_due,
            "tracked_date": _text(track.get("sent_at")),
            "note": _text(track.get("note")),
            "bookmarked": bool(track.get("bookmarked")),
            "bookmarked_at": _text(track.get("bookmarked_at")),
            "roles": list(roles) if isinstance(roles, (list, tuple)) else [],
            "skills": list(skills) if isinstance(skills, (list, tuple)) else [],
        })

    jobs.sort(key=lambda job: (
        STATUS_ORDER.index(job["status"]) if job["status"] in STATUS_ORDER else len(STATUS_ORDER),
        -(job["score"] if job["score"] is not None else -1.0),
        job["title"].casefold(),
        job["id"],
    ))

    counts = {"total": len(jobs)}
    for status in STATUS_ORDER:
        counts[status] = sum(1 for job in jobs if job["status"] == status)
    counts["in_latest_scan"] = sum(1 for job in jobs if job["in_latest_scan"])
    counts["with_score"] = sum(1 for job in jobs if job["score"] is not None)

    scan = {
        "scraped_at": _text((raw or {}).get("scraped_at")) if isinstance(raw, dict) else "",
        "period": _text((raw or {}).get("period")) if isinstance(raw, dict) else "",
        "location": _text((raw or {}).get("location")) if isinstance(raw, dict) else "",
        "keyword_count": (raw or {}).get("keywords") if isinstance(raw, dict) else None,
        "history_updated_at": _text((state or {}).get("updated_at")) if isinstance(state, dict) else "",
        "tracker_updated_at": _text((apps or {}).get("updated_at")) if isinstance(apps, dict) else "",
    }

    return {
        "schema_version": 1,
        "generated_at": utc_now(),
        "source_dir": os.path.abspath(source_dir),
        "sources": sources,
        "scan": scan,
        "counts": counts,
        "warnings": warnings,
        "jobs": jobs,
    }


def select(feed, statuses=None, min_score=None, limit=None, open_only=False):
    """Filter a feed without mutating it."""
    jobs = list(feed.get("jobs") or [])
    if open_only:
        jobs = [job for job in jobs if job["status"] in OPEN_STATUSES]
    if statuses:
        wanted = {str(item).strip().lower() for item in statuses if str(item).strip()}
        jobs = [job for job in jobs if job["status"] in wanted]
    if min_score is not None:
        jobs = [job for job in jobs if job["score"] is not None and job["score"] >= min_score]
    if limit is not None and limit >= 0:
        jobs = jobs[:limit]
    return jobs


def format_score(score):
    if score is None:
        return "  -  "
    return "%5.2f" % score


def render_table(jobs, counts=None):
    lines = []
    header = "%-7s %-8s %-5s %-38s %-30s %s" % ("STATUS", "SCAN", "SCORE", "TITLE",
                                               "COMPANY", "LOCATION")
    lines.append(header)
    lines.append("-" * len(header))
    for job in jobs:
        lines.append("%-7s %-8s %-5s %-38s %-30s %s" % (
            job["status"][:7],
            "yes" if job["in_latest_scan"] else "no",
            format_score(job["score"]).strip(),
            job["title"][:38],
            job["company"][:30],
            job["location"][:24],
        ))
    lines.append("")
    if counts:
        lines.append("total %d | new %d | nudged %d | applied %d | interview %d | blocked %d"
                     % (counts.get("total", 0), counts.get("new", 0), counts.get("nudged", 0),
                        counts.get("applied", 0), counts.get("interview", 0),
                        counts.get("blocked", 0)))
        lines.append("in latest scan %d | with score %d"
                     % (counts.get("in_latest_scan", 0), counts.get("with_score", 0)))
    lines.append("showing %d" % len(jobs))
    return "\n".join(lines)


def write_feed(feed, path=None):
    path = FEED_FILE if path is None else path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".pending-%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(feed, handle, ensure_ascii=False, indent=1, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the normalized job feed.")
    parser.add_argument("--source", default=DEFAULT_SOURCE, help="AMS data directory")
    parser.add_argument("--json", action="store_true", help="print the feed as JSON")
    parser.add_argument("--write", action="store_true", help="write .ai/feed/jobs.json")
    parser.add_argument("--enrich-existing", action="store_true",
                        help="merge cached detail into the existing feed without rebuilding an empty scan")
    parser.add_argument("--feed-file", default=FEED_FILE, help="existing feed for --enrich-existing")
    parser.add_argument("--status", action="append", help="filter by status, repeatable")
    parser.add_argument("--min-score", type=float, help="minimum score")
    parser.add_argument("--open-only", action="store_true", help="new and nudged only")
    parser.add_argument("--limit", type=int, help="truncate the printed rows")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if args.enrich_existing:
        with open(args.feed_file, encoding="utf-8") as handle:
            existing = json.load(handle)
        if not isinstance(existing, dict) or not isinstance(existing.get("jobs"), list):
            parser.error("--enrich-existing requires an existing feed with a jobs array")
        merged = enrich_with_cached_details(existing, captured_details())
        write_feed(merged, args.feed_file)
        print("enriched %d existing listings with cached AMS detail" % len(merged["jobs"]))
        return 0

    feed = build_feed(args.source)
    for warning in feed["warnings"]:
        print("warn: %s" % warning, file=sys.stderr)

    if args.write:
        path = write_feed(feed)
        print("wrote %s jobs=%d" % (os.path.relpath(path, ROOT), feed["counts"]["total"]),
              file=sys.stderr)

    if args.json:
        print(json.dumps(feed, ensure_ascii=False, indent=1, sort_keys=True))
        return 0

    jobs = select(feed, statuses=args.status, min_score=args.min_score,
                  limit=args.limit, open_only=args.open_only)
    print(render_table(jobs, feed["counts"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
