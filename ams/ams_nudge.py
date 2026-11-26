#!/usr/bin/env python3
"""AMS job nudge: pick the next un-nudged job from the scored pool, track applications.

Subcommands
  next                 pick + mark the next job, print JSON for the cron prompt
  set <uuid> <status> [note]   record the answer (applied|blocked|skipped|interview|nudged)
  stage <uuid> <stage> [note]  move one application along the process pipeline
  note <uuid> <text...>        store the feedback note without moving the stage
unstage <uuid>               step back to the stage held before the last move
  track <uuid> [status] [note] record a job that was never nudged, seeded from AMS history
  list [n]             human-readable status table
  stats                counts per status

Stages are the process the application actually walks, and each one implies a
coarse status so the older status readers keep working:

  read -> generated -> checked -> sent -> confirmed -> waiting -> contact
    contact -> interview -> preparation -> awaiting -> success | rejected
  read/generated/checked -> new, sent/confirmed/waiting/contact -> applied
  interview/preparation/awaiting -> interview

State: JOBHUNTER_TRACKER_DIR/applications.json
Pool : JOBHUNTER_TRACKER_DIR/scored_jobs.json
"""
import json, os, sys
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE = os.path.abspath(os.environ.get("JOBHUNTER_WORKSPACE", ROOT))
BASE = os.path.abspath(os.environ.get("AMS_NUDGE_BASE") or os.environ.get(
    "JOBHUNTER_TRACKER_DIR", os.path.join(WORKSPACE, ".ai", "tracker")))
POOL = os.path.join(BASE, "scored_jobs.json")
STORE = os.path.join(BASE, "applications.json")
MD = os.path.join(BASE, "applications.md")
HISTORY = os.path.join(BASE, "state.json")

try:
    from zoneinfo import ZoneInfo
    NOW = datetime.now(ZoneInfo("Europe/Vienna"))
except Exception:
    NOW = datetime.now()

SCORE_MIN = float(os.environ.get("AMS_NUDGE_MIN_SCORE", "4.5"))
MAX_AGE_DAYS = int(os.environ.get("AMS_NUDGE_MAX_AGE", "21"))
FOLLOWUP_HOURS = 20            # a nudged job older than this is listed as unanswered
VALID = {"applied", "blocked", "skipped", "interview", "nudged", "expired",
         "rejected", "success", "unclear"}

# The pipeline a listing walks. The jobui gauge reads these keys, so this file
# stays the only place that decides where a record sits.
STAGES = ["unread", "read", "generated", "checked", "sent", "confirmed",
          "waiting", "contact", "interview", "preparation", "awaiting",
          "success", "rejected", "blocked", "unclear", "skipped"]
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
STAGE_WAIT_DAYS = 7            # waiting longer than this escalates to contact


def resolve(store, uid):
    """Find one record, accepting a unique short-uuid prefix."""
    jobs = store.setdefault("jobs", {})
    job = jobs.get(uid)
    if job:
        return uid, job
    matches = [key for key in jobs if key.startswith(uid)]
    if len(matches) == 1:
        return matches[0], jobs[matches[0]]
    return uid, None


def stamp(job, note=""):
    """Record when a stage changed, keeping a short auditable history."""
    at = NOW.isoformat(timespec="seconds")
    job["stage_at"] = at
    history = job.get("stage_history")
    if not isinstance(history, list):
        history = []
    history.append({"stage": job.get("stage"), "at": at, "note": note})
    job["stage_history"] = history[-40:]
    return job


def load(path, default):
    if os.path.exists(path):
        try:
            return json.load(open(path, encoding="utf-8"))
        except Exception:
            return default
    return default


def parse_date(s):
    try:
        return datetime.strptime((s or "").strip(), "%d.%m.%Y").date()
    except Exception:
        return None


def save(store):
    store["updated_at"] = NOW.isoformat(timespec="seconds")
    os.makedirs(BASE, exist_ok=True)
    json.dump(store, open(STORE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    rows = []
    order = {"success": 0, "interview": 1, "applied": 2, "unknown": 3,
             "unclear": 4, "blocked": 5, "rejected": 6, "nudged": 7,
             "skipped": 8, "expired": 9}
    items = sorted(store["jobs"].items(), key=lambda kv: (order.get(kv[1].get("status", ""), 9),
                                                          -float(kv[1].get("score") or 0)))
    icons = {"success": "🏆", "interview": "🎤", "applied": "✅", "blocked": "🚧",
             "rejected": "❌", "unclear": "❔", "nudged": "⏳",
             "skipped": "⏭", "expired": "🗑"}
    for uid, j in items:
        rows.append(f"- {icons.get(j.get('status'), '•')} **{j.get('title')}** — {j.get('company')} "
                    f"({j.get('status')}, score {j.get('score')}, gesendet {j.get('sent_at', '')[:10]})"
                    f"{' · ' + j['stage'] if j.get('stage') else ''}"
                    f"{(' · ' + j['note']) if j.get('note') else ''}\n  {j.get('url', '')}")
    open(MD, "w", encoding="utf-8").write("# AMS Bewerbungen\n\n" + "\n".join(rows) + "\n")


def cmd_next():
    pool = load(POOL, [])
    store = load(STORE, {"jobs": {}})
    store.setdefault("jobs", {})
    known = store["jobs"]
    cutoff = (NOW.date() - timedelta(days=MAX_AGE_DAYS)).isoformat()

    def fresh(j):
        d = parse_date(j.get("datum", ""))
        return (d is None) or (d.isoformat() >= cutoff)

    cands = [j for j in pool
             if j.get("uuid") and j["uuid"] not in known
             and float(j.get("score") or 0) >= SCORE_MIN and fresh(j)]
    cands.sort(key=lambda j: (-float(j.get("score") or 0), -(parse_date(j.get("datum", "")) or datetime(1900, 1, 1).date()).toordinal()))

    pending = []
    for u, j in known.items():
        if j.get("status") != "nudged":
            continue
        try:
            asked = datetime.fromisoformat(j.get("sent_at") or "")
        except ValueError:
            continue
        if (NOW - asked) > timedelta(hours=FOLLOWUP_HOURS):
            pending.append({"uuid": u, "title": j.get("title"),
                            "company": j.get("company"), "sent_at": j.get("sent_at")})

    counts = {}
    for j in known.values():
        counts[j.get("status", "?")] = counts.get(j.get("status", "?"), 0) + 1

    if not cands:
        print(json.dumps({"empty": True, "counts": counts, "pending": pending[:3],
                          "pool_size": len(pool), "min_score": SCORE_MIN}, ensure_ascii=False, indent=1))
        return 0

    job = cands[0]
    known[job["uuid"]] = {
        "title": job.get("title"), "company": job.get("company"), "ort": job.get("ort"),
        "url": job.get("url"), "score": job.get("score"), "datum": job.get("datum"),
        "status": "nudged", "sent_at": NOW.isoformat(timespec="seconds"), "note": "",
        "roles": job.get("roles_in_title") or job.get("matched_roles"),
        "skills": job.get("matched_skills"),
    }
    save(store)
    print(json.dumps({
        "empty": False,
        "uuid": job["uuid"], "title": job.get("title"), "company": job.get("company"),
        "ort": job.get("ort"), "datum": job.get("datum"), "score": job.get("score"),
        "url": job.get("url"), "snippet": (job.get("snippet") or "")[:400],
        "roles": job.get("roles_in_title") or job.get("matched_roles"),
        "skills": job.get("matched_skills"),
        "time": NOW.strftime("%H:%M"),
        "pending": pending[:2], "counts": counts,
        "remaining": len(cands) - 1,
    }, ensure_ascii=False, indent=1))
    return 0


def cmd_set(argv):
    if len(argv) < 2:
        print("usage: ams_nudge.py set <uuid> <status> [note]")
        return 2
    uid, status = argv[0], argv[1].lower()
    note = " ".join(argv[2:]).strip()
    if status not in VALID:
        print(f"invalid status {status}; use one of {sorted(VALID)}")
        return 2
    store = load(STORE, {"jobs": {}})
    store.setdefault("jobs", {})
    uid, j = resolve(store, uid)
    if not j:
        print(f"unknown job {uid}")
        return 1
    j["status"] = status
    j["responded_at"] = NOW.isoformat(timespec="seconds")
    if note:
        j["note"] = note
    # Keep the process stage in step with the coarse status.
    stage = STATUS_STAGE.get(status)
    if stage and j.get("stage") != stage:
        j["stage"] = stage
        stamp(j, note)
    store["jobs"][uid] = j
    save(store)
    print(json.dumps({"ok": True, "uuid": uid, "title": j.get("title"), "status": status,
                      "stage": j.get("stage", ""),
                      "note": j.get("note", "")}, ensure_ascii=False, indent=1))
    return 0


def cmd_stage(argv):
    """Move one application to a named process stage.

    The stage is the process truth; the coarse status is derived from it so
    every existing reader (feed, dashboard, nudge cron) keeps working.
    """
    if len(argv) < 2:
        print("usage: ams_nudge.py stage <uuid> <%s> [note]" % "|".join(STAGES))
        return 2
    uid, stage = argv[0], argv[1].lower()
    note = " ".join(argv[2:]).strip()
    if stage not in STAGE_STATUS:
        print(f"invalid stage {stage}; use one of {STAGES}")
        return 2
    store = load(STORE, {"jobs": {}})
    store.setdefault("jobs", {})
    uid, j = resolve(store, uid)
    if not j:
        print(f"unknown job {uid}")
        return 1
    j["stage"] = stage
    j["status"] = STAGE_STATUS[stage]
    j["responded_at"] = NOW.isoformat(timespec="seconds")
    if note:
        j["note"] = note
    stamp(j, note)
    store["jobs"][uid] = j
    save(store)
    print(json.dumps({"ok": True, "uuid": uid, "title": j.get("title"),
                      "stage": stage, "status": j["status"],
                      "stage_at": j.get("stage_at", "")},
                     ensure_ascii=False, indent=1))
    return 0


def cmd_unstage(argv):
    """Step one application back to the stage it held before the last move.

    A second press on the same stage key must not be a dead end: a block is
    often wrong the moment an answer arrives. stage_history already records
    every move, so the step to return to is read back from it instead of being
    guessed from the pipeline order.
    """
    if len(argv) < 1:
        print("usage: ams_nudge.py unstage <uuid>")
        return 2
    store = load(STORE, {"jobs": {}})
    store.setdefault("jobs", {})
    uid, j = resolve(store, argv[0])
    if not j:
        print(f"unknown job {uid}")
        return 1
    current = j.get("stage")
    earlier = [entry for entry in (j.get("stage_history") or [])
               if isinstance(entry, dict) and entry.get("stage") != current]
    if not earlier:
        print(f"no earlier stage recorded for {uid}; it stays at {current}")
        return 1
    previous = earlier[-1]["stage"]
    if previous not in STAGE_STATUS:
        print(f"recorded stage {previous} is not a stage any more; stage it by hand")
        return 1
    j["stage"] = previous
    j["status"] = STAGE_STATUS[previous]
    j["responded_at"] = NOW.isoformat(timespec="seconds")
    stamp(j, "undo")
    store["jobs"][uid] = j
    save(store)
    print(json.dumps({"ok": True, "uuid": uid, "title": j.get("title"),
                      "stage": previous, "status": j["status"],
                      "stage_at": j.get("stage_at", ""),
                      "undone": current}, ensure_ascii=False, indent=1))
    return 0


def cmd_note(argv):
    """Store the feedback note on one application; the stage stays where it is.

    Success and fail both want the employer's words kept, and a note should not
    have to pretend the stage moved to be written down.
    """
    if len(argv) < 2:
        print("usage: ams_nudge.py note <uuid> <text...>")
        return 2
    uid = argv[0]
    note = " ".join(argv[1:]).strip()
    if not note:
        print("usage: ams_nudge.py note <uuid> <text...>")
        return 2
    store = load(STORE, {"jobs": {}})
    store.setdefault("jobs", {})
    uid, j = resolve(store, uid)
    if not j:
        print(f"unknown job {uid}")
        return 1
    j["note"] = note
    at = NOW.isoformat(timespec="seconds")
    j["note_at"] = at
    history = j.get("stage_history")
    if not isinstance(history, list):
        history = []
    # A note is not a stage change, so stage_at stays where the last move left
    # it; the history keeps the words with the stage they were written at.
    history.append({"stage": j.get("stage"), "at": at, "note": note})
    j["stage_history"] = history[-40:]
    store["jobs"][uid] = j
    save(store)
    print(json.dumps({"ok": True, "uuid": uid, "title": j.get("title"),
                      "note": note, "note_at": j["note_at"],
                      "stage": j.get("stage", ""), "status": j.get("status", ""),
                      "stage_at": j.get("stage_at", "")},
                     ensure_ascii=False, indent=1))
    return 0


def cmd_bookmark(argv):
    """Flip the bookmark on one application, the tracker is its only home."""
    if not argv:
        print("usage: ams_nudge.py bookmark <uuid> [note]")
        return 2
    uid = argv[0]
    note = " ".join(argv[1:]).strip()
    store = load(STORE, {"jobs": {}})
    store.setdefault("jobs", {})
    uid, j = resolve(store, uid)
    if not j:
        print(f"unknown job {uid}")
        return 1
    j["bookmarked"] = not bool(j.get("bookmarked"))
    j["bookmarked_at"] = NOW.isoformat(timespec="seconds") if j["bookmarked"] else None
    if note:
        j["note"] = note
    store["jobs"][uid] = j
    save(store)
    print(json.dumps({"ok": True, "uuid": uid, "title": j.get("title"),
                      "bookmarked": j["bookmarked"],
                      "bookmarked_at": j.get("bookmarked_at") or ""},
                     ensure_ascii=False, indent=1))
    return 0


def cmd_track(argv):
    """Record a job the store has never seen, using AMS history as provenance.

    `set` can only answer a job that was nudged before. This command creates the
    record first, so a listing that was applied to without a nudge is not lost.
    It invents nothing: title, company, url and score come from state.json or
    the scored pool, and sent_at stays empty because no send date is known.
    """
    if len(argv) < 1:
        print("usage: ams_nudge.py track <uuid> [status] [note]")
        return 2
    uid = argv[0].strip()
    status = argv[1].lower() if len(argv) > 1 else "nudged"
    note = " ".join(argv[2:]).strip()
    if status not in VALID:
        print(f"invalid status {status}; use one of {sorted(VALID)}")
        return 2
    store = load(STORE, {"jobs": {}})
    store.setdefault("jobs", {})
    if uid in store["jobs"]:
        record = store["jobs"][uid]
        record["status"] = status
        record["responded_at"] = NOW.isoformat(timespec="seconds")
        if note:
            record["note"] = note
        save(store)
        print(json.dumps({"ok": True, "uuid": uid, "title": record.get("title"),
                          "status": status, "updated": True}, ensure_ascii=False, indent=1))
        return 0
    seen = (load(HISTORY, {}) or {}).get("seen", {}).get(uid) or {}
    pool = load(POOL, [])
    pooled = next((j for j in pool if isinstance(j, dict) and j.get("uuid") == uid), {})
    if not seen and not pooled:
        print(f"unknown job {uid}: not in applications, state.json history or the scored pool")
        return 1
    record = {
        "title": seen.get("title") or pooled.get("title"),
        "company": seen.get("company") or pooled.get("company"),
        "ort": seen.get("ort") or pooled.get("ort"),
        "url": seen.get("url") or pooled.get("url"),
        "score": seen.get("score") if seen.get("score") is not None else pooled.get("score"),
        "datum": pooled.get("datum") or "",
        "status": status,
        "sent_at": "",
        "responded_at": NOW.isoformat(timespec="seconds"),
        "note": note,
        "roles": pooled.get("roles_in_title") or pooled.get("matched_roles") or [],
        "skills": pooled.get("matched_skills") or [],
        "source": "state.json history" if seen else "scored_jobs.json pool",
    }
    store["jobs"][uid] = record
    save(store)
    print(json.dumps({"ok": True, "uuid": uid, "title": record["title"],
                      "company": record["company"], "status": status,
                      "source": record["source"], "in_pool": bool(pooled)},
                     ensure_ascii=False, indent=1))
    return 0


def cmd_list(argv):
    n = int(argv[0]) if argv and argv[0].isdigit() else 40
    store = load(STORE, {"jobs": {}})
    jobs = store.get("jobs", {})
    for uid, j in sorted(jobs.items(), key=lambda kv: kv[1].get("sent_at", ""), reverse=True)[:n]:
        print(f"{j.get('status'):9s} {j.get('sent_at','')[:10]} {j.get('score')} | {j.get('title')} — {j.get('company')} | {uid}")
        if j.get("note"):
            print(f"{'':9s}   note: {j['note']}")
    return 0


def cmd_stats():
    store = load(STORE, {"jobs": {}})
    counts = {}
    for j in store.get("jobs", {}).values():
        counts[j.get("status", "?")] = counts.get(j.get("status", "?"), 0) + 1
    print(json.dumps({"counts": counts, "total": sum(counts.values())}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    args = sys.argv[2:]
    if cmd == "next":
        sys.exit(cmd_next())
    if cmd == "set":
        sys.exit(cmd_set(args))
    if cmd == "stage":
        sys.exit(cmd_stage(args))
    if cmd == "unstage":
        sys.exit(cmd_unstage(args))
    if cmd == "note":
        sys.exit(cmd_note(args))
    if cmd == "bookmark":
        sys.exit(cmd_bookmark(args))
    if cmd == "track":
        sys.exit(cmd_track(args))
    if cmd == "list":
        sys.exit(cmd_list(args))
    if cmd == "stats":
        sys.exit(cmd_stats())
    print(__doc__)
    sys.exit(2)
