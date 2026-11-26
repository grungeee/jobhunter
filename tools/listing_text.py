"""Capture the full text of AMS job ads into a local cache.

The feed's `snippet` is the search card, which AMS cuts short. This tool reads
each listing detail page and stores the real ad body in
`.ai/feed/listing_text.json`, keyed by listing id. Nothing is inferred: a
listing without a captured body simply has no entry, and a page that answers
with the maintenance screen is reported as unreachable instead of being parsed.

Standard library only. Run it whenever the AMS pages are reachable:

    python3 -B tools/listing_text.py status
    python3 -B tools/listing_text.py fetch --limit 50
    python3 -B tools/listing_text.py fetch --id 00000000-0000-4000-8000-000000000001
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE = os.path.abspath(os.environ.get("JOBHUNTER_WORKSPACE", ROOT))
FEED = os.environ.get("JOBHUNTER_FEED", os.path.join(WORKSPACE, ".ai", "feed", "jobs.json"))
CACHE = os.environ.get("JOBHUNTER_LISTING_TEXT_CACHE", os.path.join(WORKSPACE, ".ai", "feed", "listing_text.json"))
USER_AGENT = "JobhunterDemo/1.0 (+https://example.invalid/jobhunter/)"
MAINTENANCE_MARKERS = ("/wartung/", "Wartungsarbeiten", "nicht verfügbar", "nicht verfuegbar")


class Maintenance(Exception):
    """AMS answered with the maintenance screen instead of the ad."""


def _strip_tags(fragment):
    """Turn a small HTML fragment into plain text, keeping block breaks."""
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", fragment)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|h[1-6]|tr|section|article)>", "\n", text)
    text = re.sub(r"(?i)<li\b[^>]*>", "- ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def _json_ld_description(page):
    """Use the schema.org JobPosting description when the page publishes one."""
    for blob in re.findall(r"(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>", page):
        try:
            data = json.loads(blob.strip())
        except (ValueError, TypeError):
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
                continue
            if not isinstance(node, dict):
                continue
            if str(node.get("@type", "")).lower() in ("jobposting", "jobposting "):
                description = node.get("description")
                if isinstance(description, str) and description.strip():
                    return _strip_tags(description), "json-ld"
            stack.extend(node.values())
    return "", ""


CONTAINERS = (
    ("main", r"(?is)<main\b[^>]*>(.*?)</main>"),
    ("article", r"(?is)<article\b[^>]*>(.*?)</article>"),
    ("job-container",
     r"(?is)<(?:div|section)[^>]+(?:id|class)=\"[^\"]*"
     r"(?:job-?detail|job-?ad|job-?description|stellenangebot|stellenbeschreibung)[^\"]*\"[^>]*>"
     r"(.*?)</(?:div|section)>"),
    ("role-main", r"(?is)<[^>]+role=\"main\"[^>]*>(.*?)</[^>]+>"),
)


def candidates(page):
    """Every extracted candidate for one page, largest first, for probing."""
    found = []
    text, extractor = _json_ld_description(page)
    if text:
        found.append({"extractor": extractor, "text": text})
    for name, pattern in CONTAINERS:
        match = re.search(pattern, page)
        if not match:
            continue
        body = _strip_tags(match.group(1))
        if len(body) > 200:
            found.append({"extractor": name, "text": body})
    return sorted(found, key=lambda entry: len(entry["text"]), reverse=True)


def extract_text(page):
    """Return (text, extractor) for one ad page; empty text means nothing usable.

    Structured data wins because it is the publisher's own copy of the ad body.
    The visible containers are ordered from the most specific to the most
    generic, and the longest result wins so a nested wrapper cannot beat the
    real description.
    """
    found = candidates(page)
    if not found:
        return "", ""
    best = max(found, key=lambda entry: len(entry["text"]))
    return best["text"], best["extractor"]


def is_maintenance(final_url, page):
    if any(marker in (final_url or "") for marker in MAINTENANCE_MARKERS[:1]):
        return True
    title = re.search(r"(?is)<title[^>]*>(.*?)</title>", page or "")
    if title and "wartung" in title.group(1).lower():
        return True
    return False


def fetch_detail(url, timeout=25):
    """Fetch one listing. Raises Maintenance when AMS serves the status page."""
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "de-AT,de;q=0.9,en;q=0.8",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        page = response.read().decode(charset, "replace")
        final_url = response.geturl()
    if is_maintenance(final_url, page):
        raise Maintenance(final_url)
    return page


def record_from_ams_api(payload, listing_id, page_url):
    """Map a real AMS detail response to the visible, named sections.

    AMS renders companyIntroduction, title, summary and generalEqualitiyInfo as
    Unternehmensbeschreibung, Beruf, Stellenbeschreibung and Allgemeine
    Informationen respectively. The short search card is never used here.
    """
    parsed = urlsplit(str(page_url))
    if parsed.scheme != "https" or parsed.netloc != "jobs.ams.at" or not \
            parsed.path.startswith("/public/emps/jobs/") or \
            parsed.path.rsplit("/", 1)[-1] != str(listing_id):
        raise ValueError("not the AMS detail page for this listing")
    if not isinstance(payload, dict) or str(payload.get("uuid")) != str(listing_id):
        raise ValueError("AMS detail uuid does not match the listing")
    sections = {
        "company_description": _strip_tags(payload.get("companyIntroduction") or "")
                               if isinstance(payload.get("companyIntroduction"), str) else "",
        "occupation": _strip_tags(payload.get("title") or "")
                      if isinstance(payload.get("title"), str) else "",
        "job_description": _strip_tags(payload.get("summary") or "")
                           if isinstance(payload.get("summary"), str) else "",
        "general_info": _strip_tags(payload.get("generalEqualitiyInfo") or "")
                        if isinstance(payload.get("generalEqualitiyInfo"), str) else "",
    }
    if not sections["job_description"]:
        raise ValueError("AMS detail has no Stellenbeschreibung")
    headings = (("Unternehmensbeschreibung", "company_description"),
                ("Beruf", "occupation"), ("Stellenbeschreibung", "job_description"),
                ("Allgemeine Informationen", "general_info"))
    combined = "\n\n".join("%s\n%s" % (label, sections[key])
                            for label, key in headings if sections[key])
    return {**sections, "text": combined,
            "source": "https://jobs.ams.at/public/emps/api/joboffer/uuid/" + str(listing_id),
            "page_url": page_url, "extractor": "ams-detail-api",
            "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


def import_browser_capture(rows, cache):
    """Validate browser-observed responses before merging; return outcome counts."""
    outcomes = {"captured": 0, "already_cached": 0, "unavailable": 0, "rejected": 0}
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            outcomes["rejected"] += 1
            continue
        if row["id"] in cache and cache[row["id"]].get("job_description"):
            outcomes["already_cached"] += 1
            continue
        if row.get("status") != 200 or not isinstance(row.get("data"), dict):
            outcomes["unavailable"] += 1
            continue
        try:
            record = record_from_ams_api(row["data"], row["id"], row.get("page") or "")
        except ValueError:
            outcomes["rejected"] += 1
            continue
        cache[row["id"]] = record
        outcomes["captured"] += 1
    return outcomes


def cmd_import_browser(args):
    """Merge a browser batch with verified ids, without storing response metadata."""
    with open(args.file, encoding="utf-8") as handle:
        rows = json.load(handle)
    if not isinstance(rows, list):
        raise ValueError("browser batch must be a JSON list")
    cache = load_cache(args.cache)
    outcomes = import_browser_capture(rows, cache)
    save_cache(cache, args.cache)
    print("browser capture: %s; cache total %d" %
          (", ".join("%s %d" % (key, value) for key, value in outcomes.items()), len(cache)))
    return 0 if outcomes["rejected"] == 0 else 1


def load_cache(path=CACHE):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_cache(cache, path=CACHE):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(cache, handle, ensure_ascii=False, indent=1, sort_keys=True)
        handle.write("\n")
    os.replace(temp, path)


def load_jobs(path=FEED):
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    jobs = data.get("jobs") if isinstance(data, dict) else data
    return [job for job in jobs or [] if isinstance(job, dict)]


def capture(job, cache, timeout=25):
    """Capture one job into the cache. Returns a short status string."""
    url = job.get("url") or ""
    job_id = job.get("id") or ""
    if not job_id or not url.startswith("https://jobs.ams.at/"):
        return "skipped"
    page = fetch_detail(url, timeout=timeout)
    text, extractor = extract_text(page)
    if len(text) <= len(job.get("snippet") or ""):
        return "too-short"
    cache[job_id] = {
        "text": text,
        "source": url,
        "extractor": extractor,
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return "captured"


def cmd_fetch(args):
    jobs = load_jobs(args.feed)
    cache = load_cache(args.cache)
    wanted = [job for job in jobs
              if (not args.id or job.get("id") in args.id)
              and (args.refresh or job.get("id") not in cache)]
    if args.limit:
        wanted = wanted[:args.limit]
    counts = {}
    maintenance = False
    for index, job in enumerate(wanted, 1):
        if maintenance:
            counts["unreachable"] = counts.get("unreachable", 0) + len(wanted) - index + 1
            break
        try:
            result = capture(job, cache, timeout=args.timeout)
        except Maintenance:
            maintenance = True
            counts["unreachable"] = counts.get("unreachable", 0) + 1
            print("AMS is serving the maintenance screen; stopping after %d job(s)." % index)
            continue
        except (urllib.error.URLError, OSError, ValueError) as problem:
            result = "error"
            print("  %s: %s" % (job.get("id"), problem))
        counts[result] = counts.get(result, 0) + 1
        if result == "captured":
            save_cache(cache, args.cache)
        if not args.quiet:
            print("[%d/%d] %s %s" % (index, len(wanted), result, (job.get("title") or "")[:60]))
        time.sleep(args.delay)
    save_cache(cache, args.cache)
    print("captured %d, unreachable %d, other %s, cache total %d" % (
        counts.get("captured", 0), counts.get("unreachable", 0),
        {key: value for key, value in counts.items()
         if key not in ("captured", "unreachable")} or "none", len(cache)))
    return 0


def cmd_status(args):
    jobs = load_jobs(args.feed)
    cache = load_cache(args.cache)
    captured = [job for job in jobs if (cache.get(job.get("id")) or {}).get("text")]
    print("listings: %d" % len(jobs))
    print("captured: %d" % len(captured))
    print("missing: %d" % (len(jobs) - len(captured)))
    print("cache file: %s" % args.cache)
    return 0


API_PATHS = (
    "https://jobs.ams.at/public/emps/api/joboffer/uuid/%s",
)


def _json_keys(payload, depth=0, prefix=""):
    """Flatten a JSON payload into dotted key paths, values shortened."""
    rows = []
    if depth > 2:
        return rows
    if isinstance(payload, dict):
        for key, value in list(payload.items())[:40]:
            path = prefix + str(key)
            if isinstance(value, str):
                rows.append((path, "%d chars" % len(value), value[:60].replace("\n", " ")))
            elif isinstance(value, (dict, list)):
                rows.extend(_json_keys(value, depth + 1, path + "."))
            else:
                rows.append((path, type(value).__name__, str(value)[:20]))
    elif isinstance(payload, list) and payload:
        rows.extend(_json_keys(payload[0], depth + 1, prefix))
    return rows


def probe_api(listing_id, timeout=15):
    """Try the ad's JSON endpoints and describe any body-carrying field."""
    reports = []
    for template in API_PATHS:
        url = template % listing_id
        request = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode(
                    response.headers.get_content_charset() or "utf-8", "replace")
                status = getattr(response, "status", 200)
        except urllib.error.HTTPError as problem:
            reports.append((url, problem.code, "http error", []))
            continue
        except (urllib.error.URLError, OSError, ValueError) as problem:
            reports.append((url, 0, str(problem), []))
            continue
        try:
            payload = json.loads(raw)
        except ValueError:
            reports.append((url, status, "not json", []))
            continue
        keys = [row for row in _json_keys(payload)
                if any(word in row[0].lower() for word in
                       ("desc", "text", "body", "content", "html", "aufgabe", "requirement"))]
        reports.append((url, status, "json", keys[:12]))
    return reports


def cmd_probe(args):
    """Explain what one listing page yields, so extraction can be tuned in minutes."""
    jobs = load_jobs(args.feed)
    if args.id:
        chosen = [job for job in jobs if job.get("id") in args.id]
    else:
        chosen = [job for job in jobs if job.get("snippet")][:1]
    if not chosen:
        print("no listing matched")
        return 2
    job = chosen[0]
    url = job.get("url") or ""
    print("listing: %s" % job.get("id"))
    print("title:   %s" % (job.get("title") or ""))
    print("url:     %s" % url)
    print("snippet: %d chars" % len(job.get("snippet") or ""))
    if not url.startswith("https://jobs.ams.at/"):
        print("result:  not an AMS url, nothing to probe")
        return 2
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "de-AT,de;q=0.9,en;q=0.8",
    })
    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            page = response.read().decode(charset, "replace")
            final_url = response.geturl()
            status = getattr(response, "status", 200)
            content_type = response.headers.get("Content-Type", "")
    except (urllib.error.URLError, OSError, ValueError) as problem:
        print("result:  request failed: %s" % problem)
        return 1
    print("http:    %s %s" % (status, content_type))
    print("final:   %s" % final_url)
    print("bytes:   %d" % len(page))
    maintenance = is_maintenance(final_url, page)
    found = [] if maintenance else candidates(page)
    if maintenance:
        print("html:    AMS maintenance page, no ad body in this response")
    elif not found:
        print("html:    no candidate container matched (probably rendered in JavaScript)")
    else:
        for entry in found:
            first = entry["text"].splitlines()[0][:70] if entry["text"] else ""
            print("  %-14s %6d chars  %s" % (entry["extractor"], len(entry["text"]), first))
    if found:
        text, extractor = extract_text(page)
        print("result:  best=%s, %d chars, %d times the card preview" % (
            extractor, len(text), len(text) // max(1, len(job.get("snippet") or ""))))
        print("         %s" % (text[:200].replace("\n", " / ")))
        return 0
    # Nothing usable in the HTML: ask the JSON endpoints the frontend calls.
    for api_url, api_status, note, keys in probe_api(job.get("id"), timeout=args.timeout):
        print("api:     %s %s %s" % (api_status, note, api_url))
        for path, size, sample in keys:
            print("           %-42s %-10s %s" % (path, size, sample))
    print("next:    rerun this probe once the page answers; if the JSON carries a")
    print("         body field, point the capture at that path instead of scraping")
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="Capture full AMS listing text.")
    parser.add_argument("--feed", default=FEED)
    parser.add_argument("--cache", default=CACHE)
    sub = parser.add_subparsers(dest="command", required=True)
    fetch = sub.add_parser("fetch", help="read ad pages and store the body text")
    fetch.add_argument("--id", action="append", help="only this listing id (repeatable)")
    fetch.add_argument("--limit", type=int)
    fetch.add_argument("--delay", type=float, default=0.6)
    fetch.add_argument("--timeout", type=float, default=25)
    fetch.add_argument("--refresh", action="store_true", help="refetch already cached ads")
    fetch.add_argument("--quiet", action="store_true")
    fetch.set_defaults(func=cmd_fetch)
    status = sub.add_parser("status", help="how many ads have a captured body")
    status.set_defaults(func=cmd_status)
    probe = sub.add_parser("probe", help="explain what one listing page yields")
    probe.add_argument("--id", action="append", help="listing id (repeatable)")
    probe.add_argument("--timeout", type=float, default=25)
    probe.set_defaults(func=cmd_probe)
    browser = sub.add_parser("import-browser", help="merge verified AMS browser detail responses")
    browser.add_argument("--file", required=True, help="JSON list of id/page/status/data rows")
    browser.set_defaults(func=cmd_import_browser)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
