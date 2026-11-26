#!/usr/bin/env python3
"""One structure file, two navigation surfaces.

`ui/interface.json` is the single source of truth for what the app shows, how
it nests, and what an element does. Nothing here decides look and feel beyond
density: this module only validates the structure and projects it onto a
terminal menu (tui) or a browser navigation (gui).

CLI
  python3 -B tools/ui.py validate
  python3 -B tools/ui.py flatten --surface gui
  python3 -B tools/ui.py tui --context ui/examples/context.sample.json
  python3 -B tools/ui.py gui --context ui/examples/context.sample.json --out ui/out/gui.html
  python3 -B tools/ui.py context --feed .ai/feed/jobs.json --out ui/examples/context.sample.json
  python3 -B tools/ui.py build

Structure, in short:
  facts       typed values a renderer may read (counts, scan, session)
  vocabulary  closed lists (statuses, sort keys) shared by feed and UI
  actions     what can happen, with one transport per surface
  tree        nodes nested to any depth; a node is group, view, item, field,
              meter, filter, sort, query, action, status, message, separator
  states      loading, empty, degraded, error: which nodes show or hide

Python 3 standard library only. Every value that comes from a listing is
untrusted text and is escaped before it reaches markup.
"""
from __future__ import annotations

import argparse
import base64
import colorsys
import hashlib
import html
import json
import math
import os
import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
import threading
import webbrowser
from concurrent.futures import Future
from datetime import date, datetime
from urllib.parse import urlsplit

from generate import (DEFAULT_GENERATION_MODEL, DEFAULT_LANGUAGE, GENERATION_LANGUAGES,
                      GENERATION_MODELS, letter_review)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WORKSPACE = os.path.abspath(os.environ.get("JOBHUNTER_WORKSPACE", ROOT))
UI_FILE = os.path.join(ROOT, "ui", "interface.json")
OUT_DIR = os.path.join(ROOT, "ui", "out")

SCHEMA_ID = "hermes-ui-structure/1"
KINDS = {
    "group", "view", "item", "field", "meter", "status",
    "filter", "sort", "query", "action", "message", "separator",
}
TRANSPORT_KINDS = {"hermes.send", "http", "cli", "url", "local"}
FACT_TYPES = {"int", "float", "string", "bool", "date", "url", "enum"}
OPS = {"is", "is_not", "gt", "lt", "gte", "lte", "in", "not_in",
       "truthy", "falsy", "present"}
ACTION_SCOPES = {"global", "selection", "row"}
EDITABLE_OVERRIDES = {"label", "hint", "role", "hidden", "component", "icon", "keys"}


class ValidationError(Exception):
    """Raised only when the structure cannot be read at all."""


# --------------------------------------------------------------------------
# loading and validation
# --------------------------------------------------------------------------

def load(path=UI_FILE):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            spec = json.load(handle)
    except FileNotFoundError:
        raise ValidationError("structure file not found: %s" % path)
    except json.JSONDecodeError as exc:
        raise ValidationError("structure file is not valid JSON: %s" % exc)
    if not isinstance(spec, dict):
        raise ValidationError("structure root must be an object")
    return spec


def _clause_fact_known(fact, facts, collections):
    """A clause may read a global fact, or a field of the row it is evaluated for."""
    if not isinstance(fact, str) or not fact:
        return False
    if fact in facts:
        return True
    if fact.startswith("listing."):
        for cfg in (collections or {}).values():
            if fact.split(".", 1)[1] in ((cfg or {}).get("fields") or {}):
                return True
    return False


def validate(spec):
    """Return a list of problems: {severity, code, at, message}."""
    problems = []

    def add(severity, code, at, message):
        problems.append({"severity": severity, "code": code,
                         "at": at, "message": message})

    if spec.get("schema") != SCHEMA_ID:
        add("error", "schema", "schema", "expected %r, found %r"
            % (SCHEMA_ID, spec.get("schema")))
    for key in ("version", "updated"):
        if not isinstance(spec.get(key), str) or not spec.get(key):
            add("error", "missing", key, "required string is missing")

    app = spec.get("app") or {}
    for key in ("id", "title", "language"):
        if not isinstance(app.get(key), str) or not app.get(key):
            add("error", "missing", "app.%s" % key, "required string is missing")

    surfaces = spec.get("surfaces") or {}
    if not surfaces:
        add("error", "missing", "surfaces", "at least one surface is required")
    caps_by_surface = {}
    for name, cfg in surfaces.items():
        at = "surfaces.%s" % name
        if not isinstance(cfg, dict):
            add("error", "type", at, "surface must be an object")
            continue
        caps = cfg.get("capabilities")
        if not isinstance(caps, dict) or not caps:
            add("error", "missing", "%s.capabilities" % at,
                "capabilities must be a non-empty object")
            caps = {}
        for cap, value in caps.items():
            if not isinstance(value, bool):
                add("error", "type", "%s.capabilities.%s" % (at, cap),
                    "capability must be true or false")
        caps_by_surface[name] = caps
        depth = cfg.get("max_depth")
        if not isinstance(depth, int) or depth < 1:
            add("error", "type", "%s.max_depth" % at,
                "max_depth must be a positive integer")
        if not isinstance(cfg.get("keys") or {}, dict):
            add("error", "type", "%s.keys" % at, "keys must be an object")

    roles = spec.get("roles") or {}
    if not roles:
        add("error", "missing", "roles", "role table is required")
    for name, cfg in roles.items():
        at = "roles.%s" % name
        if not isinstance(cfg, dict):
            add("error", "type", at, "role must be an object")
            continue
        for surface in surfaces:
            if surface not in cfg:
                add("warn", "role_surface", at,
                    "role has no %s presentation" % surface)

    facts = spec.get("facts") or {}
    for path, cfg in facts.items():
        at = "facts.%s" % path
        if not isinstance(cfg, dict):
            add("error", "type", at, "fact must be an object")
            continue
        kind = cfg.get("type")
        if kind not in FACT_TYPES:
            add("error", "fact_type", at, "unknown fact type %r" % (kind,))
        if kind == "enum" and not isinstance(cfg.get("values"), list):
            add("error", "fact_enum", at, "enum fact needs a values list")

    vocabulary = spec.get("vocabulary") or {}
    categories = {}
    for name, entries in vocabulary.items():
        if name == "densities":
            continue
        if not isinstance(entries, list) or not entries:
            add("error", "vocabulary", "vocabulary.%s" % name,
                "vocabulary entry must be a non-empty list")
            continue
        seen = set()
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("key"):
                add("error", "vocabulary", "vocabulary.%s" % name,
                    "every entry needs a key")
                continue
            if entry["key"] in seen:
                add("error", "duplicate", "vocabulary.%s" % name,
                    "duplicate key %r" % entry["key"])
            seen.add(entry["key"])
            if not entry.get("label"):
                add("error", "vocabulary", "vocabulary.%s.%s" % (name, entry["key"]),
                    "entry needs a label")
            if "role" in entry and entry["role"] not in roles:
                add("error", "role", "vocabulary.%s.%s" % (name, entry["key"]),
                    "unknown role %r" % entry["role"])
        categories[name] = {entry.get("key"): entry for entry in entries
                            if isinstance(entry, dict) and entry.get("key")}

    collections = ((spec.get("data") or {}).get("collections") or {})
    if not collections:
        add("warn", "data", "data.collections", "no collections declared")
    for name, cfg in collections.items():
        at = "data.collections.%s" % name
        if not isinstance(cfg, dict) or not isinstance(cfg.get("fields"), dict):
            add("error", "type", at, "collection needs a fields object")
            continue
        if not cfg.get("key"):
            add("error", "collection", at, "collection needs a key field")

    channels = spec.get("channels") or {}
    if not channels:
        add("error", "missing", "channels", "channels table is required")

    actions = spec.get("actions") or {}
    if not actions:
        add("error", "missing", "actions", "action registry is required")
    for name, cfg in actions.items():
        at = "actions.%s" % name
        if not isinstance(cfg, dict):
            add("error", "type", at, "action must be an object")
            continue
        if not cfg.get("label"):
            add("error", "action", at, "action needs a label")
        if cfg.get("role") and cfg["role"] not in roles:
            add("error", "role", at, "unknown role %r" % cfg["role"])
        if cfg.get("scope") and cfg["scope"] not in ACTION_SCOPES:
            add("error", "action", at, "unknown scope %r" % cfg["scope"])
        arg_names = []
        for arg in cfg.get("args") or []:
            if not isinstance(arg, dict) or not arg.get("name"):
                add("error", "action", at, "every arg needs a name")
                continue
            arg_names.append(arg["name"])
            if "from" in arg and arg["from"] not in facts and not str(arg["from"]).startswith("selection."):
                add("warn", "action_arg", at,
                    "arg %r reads undeclared fact %r" % (arg["name"], arg["from"]))
        if len(arg_names) != len(set(arg_names)):
            add("error", "action", at, "arg names are not unique")
        transports = cfg.get("transports") or []
        if not transports:
            add("error", "action", at, "action needs at least one transport")
        for transport in transports:
            tat = "%s.transports[%s]" % (at, transport.get("surface") if isinstance(transport, dict) else "?")
            if not isinstance(transport, dict):
                add("error", "type", tat, "transport must be an object")
                continue
            kind = transport.get("kind")
            if kind not in TRANSPORT_KINDS:
                add("error", "transport", tat, "unknown transport kind %r" % (kind,))
            if kind == "http" and not (transport.get("method") and transport.get("path")):
                add("error", "transport", tat, "http transport needs method and path")
            if kind == "cli" and not transport.get("command"):
                add("error", "transport", tat, "cli transport needs a command")
            if kind == "url" and not transport.get("template"):
                add("error", "transport", tat, "url transport needs a template")
            if kind == "local" and (transport.get("channel") != "browser-local"
                                    or transport.get("store") != "bookmarks"):
                add("error", "transport", tat,
                    "local transport supports browser-local bookmarks only")
            if (transport.get("channel") == "local" and kind == "cli"
                    and any((transport.get("command") or "").startswith(scheme)
                            for scheme in ("http:", "https:"))):
                add("error", "transport", tat,
                    "local transport runs a command on this machine, never a URL")
            for field in ("template", "command"):
                value = transport.get(field)
                if not isinstance(value, str):
                    continue
                for placeholder in _placeholders(value):
                    if placeholder not in arg_names:
                        add("error", "transport", tat,
                            "placeholder %r is not a declared arg" % placeholder)
            channel = transport.get("channel")
            if not channel:
                add("error", "transport", tat, "transport needs a channel")
            elif channel not in channels:
                add("error", "transport", tat,
                    "unknown channel %r (declared: %s)"
                    % (channel, ", ".join(sorted(channels))))
        feedback = cfg.get("feedback")
        if feedback is not None and not isinstance(feedback, dict):
            add("error", "action", at, "feedback must be an object")
        if cfg.get("role") == "primary":
            kinds = {t.get("channel") for t in transports if isinstance(t, dict)}
            if "static" not in kinds and "chat-frame" not in kinds:
                add("warn", "action", at,
                    "primary action has no fallback for a page opened from disk")

    seen_ids = {}
    seen_keys = {name: set() for name in surfaces}

    def visit(nodes, at, depth):
        if not isinstance(nodes, list):
            add("error", "type", at, "children must be a list")
            return
        order_seen = {}
        for index, node in enumerate(nodes):
            if not isinstance(node, dict):
                add("error", "type", at, "node must be an object")
                continue
            node_id = node.get("id")
            where = "%s[%d]" % (at, index)
            if not isinstance(node_id, str) or not node_id:
                add("error", "node", where, "node needs an id")
                continue
            if node_id in seen_ids:
                add("error", "duplicate", where,
                    "id %r already used at %s" % (node_id, seen_ids[node_id]))
            seen_ids[node_id] = where
            kind = node.get("kind")
            if kind not in KINDS:
                add("error", "kind", where, "unknown kind %r" % (kind,))
            if kind in {"group", "view", "action", "filter", "sort", "query"} and not node.get("label") and kind != "action":
                add("error", "label", where, "kind %r needs a label" % kind)
            if node.get("role") and node["role"] not in roles:
                add("error", "role", where, "unknown role %r" % node["role"])
            for clause in node.get("when") or []:
                if not isinstance(clause, dict) or not _clause_fact_known(clause.get("fact"), facts, collections):
                    add("error", "when", where,
                        "when clause reads undeclared fact %r" % (clause.get("fact") if isinstance(clause, dict) else clause,))
                    continue
                if clause.get("op", "is") not in OPS:
                    add("error", "when", where, "unknown operator %r" % clause.get("op"))
            for flag in node.get("flags") or []:
                fat = "%s > %s" % (where, (flag or {}).get("id") if isinstance(flag, dict) else "?")
                if not isinstance(flag, dict):
                    add("error", "type", fat, "flag must be an object")
                    continue
                if not flag.get("id") or not flag.get("label"):
                    add("error", "flag", fat, "flag needs an id and a label")
                if flag.get("role") and flag["role"] not in roles:
                    add("error", "role", fat, "unknown role %r" % flag["role"])
                if not flag.get("when"):
                    add("warn", "flag", fat, "flag has no when clause, it would always show")
                for clause in flag.get("when") or []:
                    if not isinstance(clause, dict) or not _clause_fact_known(
                            clause.get("fact"), facts, collections):
                        add("error", "when", fat,
                            "flag clause reads undeclared fact %r"
                            % (clause.get("fact") if isinstance(clause, dict) else clause,))
                        continue
                    if clause.get("op", "is") not in OPS:
                        add("error", "when", fat, "unknown operator %r" % clause.get("op"))
            for cap in node.get("requires") or []:
                if not any(surface_caps.get(cap) is not None for surface_caps in caps_by_surface.values()):
                    add("error", "requires", where, "capability %r is declared on no surface" % cap)
                elif not any(cap in surface_caps for surface_caps in caps_by_surface.values()):
                    add("warn", "requires", where, "capability %r is unused by every surface" % cap)
            for surface, key in (node.get("keys") or {}).items():
                if surface not in surfaces:
                    add("error", "keys", where, "key targets unknown surface %r" % surface)
                    continue
                if key in seen_keys.setdefault(surface, set()):
                    add("error", "duplicate_key", where,
                        "key %r is already bound on %s" % (key, surface))
                seen_keys[surface].add(key)
            if kind == "action":
                ref = node.get("action")
                if ref not in actions:
                    add("error", "action_ref", where, "unknown action %r" % (ref,))
            if kind == "item":
                collection = node.get("collection")
                if collection not in collections:
                    add("error", "collection", where, "unknown collection %r" % (collection,))
            bind = node.get("bind")
            if kind in {"meter", "field", "filter", "sort", "query", "status"}:
                if bind is None:
                    add("warn", "bind", where, "node has no data binding")
                elif isinstance(bind, str):
                    if bind.startswith("listing."):
                        field = bind.split(".", 1)[1]
                        if "listings" in collections and field not in collections["listings"]["fields"]:
                            add("error", "bind", where,
                                "listing field %r is not declared" % field)
                    elif bind.startswith("filter."):
                        pass
                    elif bind not in facts:
                        add("error", "bind", where, "undeclared fact %r" % bind)
                elif isinstance(bind, dict):
                    vocab_name = bind.get("vocabulary")
                    if vocab_name not in vocabulary:
                        add("error", "bind", where, "unknown vocabulary %r" % (vocab_name,))
                    elif bind.get("key") is not None and bind["key"] not in categories.get(vocab_name, {}):
                        add("error", "bind", where,
                            "unknown %s key %r" % (vocab_name, bind["key"]))
                else:
                    add("error", "bind", where, "bind must be a string or an object")
            if "score" in node and not isinstance(node["score"], (int, float)):
                add("error", "type", where, "score must be numeric")
            children = node.get("children")
            if children is not None:
                visit(children, "%s > %s" % (at, node_id), depth + 1)
            for surface, cfg in surfaces.items():
                # compute effective depth for this surface
                # depth counts tree roots as 1
                limit = cfg.get("max_depth")
                if kind != "item" and isinstance(limit, int) and depth > limit:
                    add("warn", "depth", where,
                        "depth %d exceeds %s max_depth %d" % (depth, surface, limit))
                    break

    visit(spec.get("tree"), "tree", 1)
    if not spec.get("tree"):
        add("error", "missing", "tree", "tree must contain at least one root")

    tabs = spec.get("tabs")
    if tabs is not None:
        if not isinstance(tabs, list) or len(tabs) < 2:
            add("error", "tabs", "tabs", "tabs must be a list with at least two entries")
        else:
            seen_tabs = set()
            for index, tab in enumerate(tabs):
                at = "tabs[%d]" % index
                if not isinstance(tab, dict):
                    add("error", "type", at, "tab must be an object")
                    continue
                tid = tab.get("id")
                if not isinstance(tid, str) or not tid:
                    add("error", "tabs", at, "tab needs an id")
                elif tid in seen_tabs:
                    add("error", "duplicate", at, "tab id %r already used" % tid)
                else:
                    seen_tabs.add(tid)
                if not tab.get("label"):
                    add("error", "tabs", at, "tab needs a label")
                root = tab.get("root")
                if root not in seen_ids:
                    add("error", "tabs", at, "tab root %r is not a node id" % (root,))

    # reachability of vocabulary filters
    filter_keys = set()
    stage_selectors = []
    def collect_filters(nodes):
        for node in nodes or []:
            if not isinstance(node, dict):
                continue
            bind = node.get("bind")
            if node.get("kind") == "filter" and isinstance(bind, dict):
                if bind.get("vocabulary") == "statuses":
                    filter_keys.add(bind.get("key"))
                elif (bind.get("vocabulary") == "stages"
                      and node.get("mode") == "select" and bind.get("key") is None):
                    stage_selectors.append(node.get("id"))
            collect_filters(node.get("children"))
    collect_filters(spec.get("tree"))
    for entry in vocabulary.get("statuses") or []:
        if entry.get("filter") and entry.get("key") not in filter_keys:
            add("error", "reachability", "vocabulary.statuses.%s" % entry.get("key"),
                "status is filterable but no filter node reaches it")
    if vocabulary.get("stages") and not stage_selectors:
        add("error", "reachability", "vocabulary.stages",
            "stages need a current-stage selector")

    states = spec.get("states") or {}
    for name, cfg in states.items():
        at = "states.%s" % name
        if not isinstance(cfg, dict) or not cfg.get("message"):
            add("error", "state", at, "state needs a message")
            continue
        if cfg.get("role") and cfg["role"] not in roles:
            add("error", "role", at, "unknown role %r" % cfg["role"])
        for field in ("shows", "hides"):
            for node_id in cfg.get(field) or []:
                if node_id not in seen_ids:
                    add("error", "state_ref", at,
                        "%s references unknown node %r" % (field, node_id))

    return problems


def _placeholders(text):
    out, index = [], 0
    while True:
        start = text.find("{", index)
        if start < 0:
            return out
        end = text.find("}", start)
        if end < 0:
            return out
        out.append(text[start + 1:end])
        index = end + 1


# --------------------------------------------------------------------------
# context and evaluation
# --------------------------------------------------------------------------

def empty_context(mode="static", state="ready"):
    return {
        "session": {"mode": mode, "state": state},
        "facts": {},
        "rows": [],
        "selection": {},
    }


def context_from_feed(feed, mode="served", limit=None, state="ready"):
    """Project a normalized feed (tools/feed.py output) onto declared facts."""
    counts = feed.get("counts") or {}
    scan = feed.get("scan") or {}
    bits = []
    if scan.get("scraped_at"):
        bits.append("latest AMS scan %s" % scan["scraped_at"][:16].replace("T", " "))
    if scan.get("location"):
        bits.append(scan["location"])
    if scan.get("period"):
        bits.append(scan["period"])
    warnings = feed.get("warnings") or []
    ctx = empty_context(mode=mode, state=state)
    ctx["facts"] = {
        "counts.total": counts.get("total", 0),
        "counts.new": counts.get("new", 0),
        "counts.nudged": counts.get("nudged", 0),
        "counts.applied": counts.get("applied", 0),
        "counts.interview": counts.get("interview", 0),
        "counts.blocked": counts.get("blocked", 0),
        "counts.skipped": counts.get("skipped", 0),
        "counts.expired": counts.get("expired", 0),
        "counts.in_latest_scan": counts.get("in_latest_scan", 0),
        "scan.available": bool(scan),
        "scan.scraped_at": scan.get("scraped_at", ""),
        "scan.line": " | ".join(bits) or "no scan metadata",
        "feed.warnings.count": len(warnings),
        "feed.degraded": bool(warnings),
        "selection.listing_id": "",
    }
    rows = []
    source_jobs = feed.get("jobs") or []
    for job in source_jobs if limit is None else source_jobs[:limit]:
        rows.append({
            "id": job.get("id", ""),
            "title": job.get("title", ""),
            "company": job.get("company", ""),
            "location": job.get("location", ""),
            "url": job.get("url", ""),
            "posted_date": job.get("posted_date", ""),
            "score": job.get("score"),
            "pay_monthly_min": job.get("pay_monthly_min"),
            "pay_yearly_min": job.get("pay_yearly_min"),
            "pay_monthly_calc": job.get("pay_monthly_calc"),
            "pay_basis": job.get("pay_basis", ""),
            "score_source": job.get("score_source", ""),
            "in_latest_scan": bool(job.get("in_latest_scan")),
            "status": job.get("status", "new"),
            "stage": job.get("stage", ""),
            "stage_label": job.get("stage_label", ""),
            "stage_short": job.get("stage_short", ""),
            "stage_at": job.get("stage_at", ""),
            "stage_due": job.get("stage_due", ""),
            "bookmarked": bool(job.get("bookmarked")),
            "bookmarked_at": job.get("bookmarked_at", ""),
            "tracked_date": (job.get("tracked_date") or "")[:10],
            "note": job.get("note", ""),
            "posted_basis": job.get("posted_basis", ""),
            "posted_age_days": job.get("posted_age_days"),
            "employment_type": job.get("employment_type", ""),
            "occupation_group": job.get("occupation_group", ""),
            "roles": list(job.get("roles") or []),
            "skills": list(job.get("skills") or []),
            "snippet": job.get("snippet", ""),
            "full_text": job.get("full_text", ""),
            "company_description": job.get("company_description", ""),
            "occupation": job.get("occupation", ""),
            "job_description": job.get("job_description", ""),
            "general_info": job.get("general_info", ""),
        })
    ctx["rows"] = rows
    if rows:
        ctx["facts"]["selection.listing_id"] = rows[0]["id"]
        ctx["selection"] = {"listing_id": rows[0]["id"],
                            "listing_url": rows[0]["url"],
                            "row": rows[0]}
    return ctx


def _eq(left, right):
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    return left == right


def fact_value(ctx, surface, path, row=None):
    if path == "surface.id":
        return surface
    if path in ("session.mode", "session.state"):
        return (ctx.get("session") or {}).get(path.split(".", 1)[1])
    if path.startswith("listing.") and isinstance(row, dict):
        return row.get(path.split(".", 1)[1])
    return (ctx.get("facts") or {}).get(path)


def conditions_hold(spec, ctx, surface, clauses, row=None):
    for clause in clauses or []:
        fact = clause.get("fact")
        op = clause.get("op", "is")
        want = clause.get("value")
        have = fact_value(ctx, surface, fact, row)
        if op == "present":
            if have in (None, "", [], {}):
                return False
            continue
        if have is None:
            return False
        if op == "is" and not _eq(have, want):
            return False
        if op == "is_not" and _eq(have, want):
            return False
        if op in ("gt", "gte", "lt", "lte"):
            if not isinstance(have, (int, float)) or isinstance(have, bool):
                return False
            if not isinstance(want, (int, float)):
                return False
            if op == "gt" and not have > want:
                return False
            if op == "gte" and not have >= want:
                return False
            if op == "lt" and not have < want:
                return False
            if op == "lte" and not have <= want:
                return False
        if op in ("in", "not_in"):
            if not isinstance(want, list):
                return False
            if (have in want) is (op == "not_in"):
                return False
        if op == "truthy" and not have:
            return False
        if op == "falsy" and have:
            return False
    return True


def merge_node(node, surface):
    merged = dict(node)
    override = (node.get("surface") or {}).get(surface) or {}
    for key, value in override.items():
        if key == "keys" and isinstance(value, dict):
            merged["keys"] = {**(node.get("keys") or {}), **value}
        elif key in EDITABLE_OVERRIDES or key in {"layout", "placeholder", "text"}:
            merged[key] = value
    return merged


class Projection:
    """A tree filtered for one surface and one context."""

    def __init__(self, spec, ctx, surface):
        self.spec = spec
        self.ctx = ctx
        self.surface = surface
        self.surface_cfg = (spec.get("surfaces") or {}).get(surface) or {}
        self.caps = self.surface_cfg.get("capabilities") or {}
        self.state_cfg = (spec.get("states") or {}).get(
            ((ctx.get("session") or {}).get("state") or "ready"), {})
        self.hidden_by_capability = []
        self.hidden_by_condition = []
        self.state_hidden = set(self.state_cfg.get("hides") or [])
        self.tree = self._project(spec.get("tree") or [], 1)

    def _project(self, nodes, depth):
        out = []
        limit = self.surface_cfg.get("max_depth")
        for node in nodes:
            node = merge_node(node, self.surface)
            if node.get("hidden") or node.get("id") in self.state_hidden:
                continue
            missing = [cap for cap in node.get("requires") or [] if not self.caps.get(cap)]
            if missing:
                self.hidden_by_capability.append((node.get("id"), missing))
                continue
            if not conditions_hold(self.spec, self.ctx, self.surface, node.get("when")):
                self.hidden_by_condition.append(node.get("id"))
                continue
            children = node.get("children") or []
            if isinstance(limit, int) and depth >= limit and children:
                # surface cannot show this depth: keep the node, drop the rest
                node = dict(node)
                node["children"] = []
                node["truncated"] = len(children)
                out.append(node)
                continue
            node = dict(node)
            node["children"] = self._project(children, depth + 1)
            if node.get("kind") == "group" and not node["children"] and not node.get("keep_empty"):
                continue
            out.append(node)
        return _sort_nodes(out)

    def flatten(self):
        rows = []

        def walk(nodes, depth, trail):
            for node in nodes:
                label = node.get("label") or node.get("text") or node.get("id")
                path = trail + [label]
                rows.append({
                    "depth": depth,
                    "path": " > ".join(path),
                    "id": node.get("id"),
                    "kind": node.get("kind"),
                    "label": label,
                    "key": (node.get("keys") or {}).get(self.surface, ""),
                    "role": node.get("role", "neutral"),
                    "action": node.get("action", ""),
                })
                walk(node.get("children") or [], depth + 1, path)

        walk(self.tree, 1, [])
        return rows


def _sort_nodes(nodes):
    return sorted(nodes, key=lambda n: (n.get("order", 1000), nodes.index(n)))


def _plain_hyphens(value):
    """Normalize display text only; keep source values and URLs untouched."""
    return str(value).replace(chr(0x2014), "-").replace(chr(0x2013), "-")


def _unstyled(text):
    """The same painted line with its colour codes stripped, for hit testing."""
    return re.sub(r"\x1b\[[0-9;]*m", "", str(text))


def value_text(spec, ctx, surface, value, fallback="-"):
    if value is None or value == "" or value == []:
        return fallback
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return ", ".join(_plain_hyphens(item) for item in value)
    return _plain_hyphens(value)


def vocabulary_label(spec, name, key):
    for entry in (spec.get("vocabulary") or {}).get(name) or []:
        if isinstance(entry, dict) and entry.get("key") == key:
            return entry.get("label", key)
    return key


def status_label(spec, status):
    return vocabulary_label(spec, "statuses", status)


def row_flags(spec, ctx, surface, node, row):
    """Flags declared on the item node, evaluated against one row."""
    out = []
    for flag in node.get("flags") or []:
        if conditions_hold(spec, ctx, surface, flag.get("when"), row):
            out.append({"id": flag.get("id"), "label": flag.get("label"),
                        "role": flag.get("role", "neutral"),
                        "meaning": flag.get("meaning", "")})
    return out


def ranked_listings(rows):
    """Score descending, then comparable monthly pay, unknowns last."""
    def number(value):
        if isinstance(value, bool) or value in (None, ""):
            return None
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        return result if math.isfinite(result) else None

    def key(row):
        score = number(row.get("score"))
        monthly = number(row.get("pay_monthly_min"))
        yearly = number(row.get("pay_yearly_min"))
        # Monthly figures stay comparable with each other; a listing that only
        # states a yearly figure ranks after them, never converted into a
        # monthly number the ad never gave.
        derived = number(row.get("pay_monthly_calc"))
        tier = 0 if monthly is not None else (1 if derived is not None
                                              else (2 if yearly is not None else 3))
        amount = monthly if monthly is not None else (derived if derived is not None
                                                      else (yearly or 0.0))
        return (score is None, -(score or 0), tier, -amount,
                str(row.get("title") or "").casefold(), str(row.get("id") or ""))

    return sorted(rows, key=key)


def bind_value(spec, ctx, surface, node, row=None):
    bind = node.get("bind")
    if bind is None:
        return ""
    if isinstance(bind, str):
        if bind.startswith("listing."):
            field = bind.split(".", 1)[1]
            return (row or {}).get(field)
        if bind.startswith("query."):
            return node.get("placeholder", "")
        return fact_value(ctx, surface, bind)
    if isinstance(bind, dict):
        return bind.get("key") or [entry.get("label") for entry in
                                   (spec.get("vocabulary") or {}).get(bind.get("vocabulary"), [])]
    return ""


# --------------------------------------------------------------------------
# terminal projection
# --------------------------------------------------------------------------

def render_tui(spec, ctx, surface="tui", width=None):
    cfg = (spec.get("surfaces") or {}).get(surface) or {}
    width = width or cfg.get("width") or 78
    proj = Projection(spec, ctx, surface)
    app = spec.get("app") or {}
    meta = "mode: %s | state: %s | surface: %s | %d cols" % (
        (ctx.get("session") or {}).get("mode", "static"),
        (ctx.get("session") or {}).get("state", "ready"), surface, width)
    tree_lines = []
    state_message = proj.state_cfg.get("message")
    if state_message:
        role = proj.state_cfg.get("role", "neutral")
        tree_lines.extend(_wrap("[" + role + "] " + state_message, max(16, width - 4), "  "))
        tree_lines.append("")
    _walk_tui(spec, ctx, proj, proj.tree, 1, tree_lines, max(16, width - 4))
    primary = _primary_action(spec, proj)
    keys = cfg.get("keys") or {}
    hints = []
    if keys.get("focus_next"):
        hints.append("%s/%s move" % (keys.get("focus_prev", ""), keys["focus_next"]))
    if keys.get("search"):
        hints.append("%s search" % keys["search"])
    if primary:
        code = (primary.get("keys") or {}).get(surface, "?")
        hints.append("%s %s" % (code, primary.get("label", "run")))
    if keys.get("back"):
        hints.append("%s back" % keys["back"])
    footer = [_fit("keys: " + "  ".join(hints), max(16, width - 4))]

    if proj.hidden_by_capability:
        caps = sorted({cap for _, missing in proj.hidden_by_capability for cap in missing})
        footer.append(_fit("hidden on %s: %d node(s) need %s"
                           % (surface, len(proj.hidden_by_capability), ", ".join(caps)),
                           max(16, width - 4)))
    if proj.hidden_by_condition:
        footer.append(_fit("hidden by condition or state: %d node(s)"
                           % len(proj.hidden_by_condition), max(16, width - 4)))
    footer.append(_fit("structure: %s | renderer: tools/ui.py %s"
                       % ((spec.get("app") or {}).get("source_of_truth", "ui/interface.json"), surface),
                       max(16, width - 4)))

    return "\n".join(
        _rounded_box("%s · %s" % (app.get("title", "UI"),
                                  (ctx.get("session") or {}).get("mode", "static")),
                     [app.get("subtitle", ""), meta], width)
        + [""] + _rounded_box("Projection", tree_lines, width)
        + [""] + _rounded_box("Keys and status", footer, width))


def _primary_action(spec, proj):
    found = []
    def walk(nodes):
        for node in nodes:
            if node.get("kind") == "action" and node.get("role") == "primary":
                found.append(node)
            walk(node.get("children") or [])
    walk(proj.tree)
    return found[0] if found else None


def _walk_tui(spec, ctx, proj, nodes, depth, lines, width):
    surface = proj.surface
    for node in nodes:
        kind = node.get("kind")
        key = (node.get("keys") or {}).get(surface)
        indent = "  " * (depth - 1)
        key_text = ("[%s] " % key) if key else "  "
        role_marker = (((spec.get("roles") or {}).get(node.get("role", "neutral"), {})
                        .get(surface, {}) or {}).get("marker", " "))
        layout = (node.get("layout") or {}).get(surface)
        children = node.get("children") or []

        if kind == "group":
            lines.append(_fit("%s+ %s" % (indent, node.get("label", node["id"])), width))
            if layout == "inline" and children:
                lines.extend(_inline_line(spec, ctx, proj, children, depth + 1, width))
            else:
                _walk_tui(spec, ctx, proj, children, depth + 1, lines, width)
            continue

        if kind == "view":
            lines.append(_fit("%s%s%s" % (indent, key_text, node.get("label", node["id"])), width))
            _walk_tui(spec, ctx, proj, children, depth + 1, lines, width)
            continue

        if kind == "item":
            lines.extend(_item_block(spec, ctx, proj, node, depth, width))
            continue

        if kind == "action":
            label = node.get("label") or spec.get("actions", {}).get(node.get("action"), {}).get("label", "")
            suffix = " *" if node.get("role") == "primary" else ""
            lines.append(_fit("%s%s%s%s" % (indent, key_text, label, suffix), width))
            continue

        if kind in ("meter", "field"):
            value = bind_value(spec, ctx, proj.surface, node)
            text = value_text(spec, ctx, proj.surface, value, node.get("fallback", "-"))
            if kind == "field" and node.get("vocabulary") == "statuses":
                text = status_label(spec, text)
            label = node.get("label") or node["id"]
            lines.extend(_kv(indent + "   ", label, text, width, label_width=14))
            continue

        if kind == "status":
            value = bind_value(spec, ctx, proj.surface, node)
            if isinstance(value, bool):
                text = node.get("true_label") if value else node.get("false_label")
            else:
                text = status_label(spec, value)
            lines.extend(_kv(indent + "   ", node.get("label") or "Status", text or "-", width,
                             label_width=14))
            continue

        if kind in ("filter", "sort", "query"):
            bind = node.get("bind")
            if kind == "sort" and isinstance(bind, dict):
                names = [entry.get("label") for entry in
                         (spec.get("vocabulary") or {}).get(bind.get("vocabulary"), [])]
                lines.extend(_kv(indent + "   ", "Sort", ", ".join(names), width, label_width=7))
                continue
            if kind == "query":
                lines.append(_fit("%s%s%s (%s)" % (indent, key_text, node.get("label"),
                                                   node.get("placeholder", "")), width))
                continue
            lines.append(_fit("%s%s%s %s" % (indent, key_text, role_marker,
                                             node.get("label")), width))
            continue

        if kind == "message":
            prefix = (node.get("label") + ": ") if node.get("label") else ""
            lines.extend(_wrap(prefix + node.get("text", ""), max(24, width - len(indent)), indent + "  "))
            continue

        if kind == "separator":
            lines.append(indent + "-" * max(8, width - len(indent)))
            continue


def _inline_line(spec, ctx, proj, children, depth, width):
    parts = []
    for child in children:
        key = (child.get("keys") or {}).get(proj.surface)
        label = child.get("label") or child.get("id")
        if child.get("kind") in ("meter", "field"):
            value = bind_value(spec, ctx, proj.surface, child)
            text = value_text(spec, ctx, proj.surface, value, child.get("fallback", "-"))
            parts.append("%s %s" % (label, text))
            continue
        parts.append(("[%s] %s" % (key, label)) if key else label)
    indent = "  " * (depth - 1)
    return _wrap(indent + "  ".join(parts), width, indent)


def _item_block(spec, ctx, proj, node, depth, width):
    indent = "  " * (depth - 1)
    rows = ctx.get("rows") or []
    lines = []
    declared = len(rows)
    heading = node.get("label") or "Rows"
    lines.append(_fit("%s- %s template (%d row(s) in context)" % (indent, heading, declared), width))
    if not rows:
        lines.append(_fit("%s  (no rows loaded: pass --context or --feed)" % indent, width))
        return lines
    trail = "  " * depth
    for row in rows[:3]:
        flag = "latest scan" if row.get("in_latest_scan") else "older scan"
        score = "" if row.get("score") is None else "%.2f" % row["score"]
        inner = max(20, width - len(trail) - 2)
        title_lines = _wrap("%s @ %s" % (row.get("title") or "untitled", row.get("company") or "-"),
                            inner, "")
        lines.append(_fit(trail + "- " + title_lines[0], width))
        lines.extend(_fit(trail + "  " + line, width) for line in title_lines[1:])
        meta = "%s | %s | %s | %s | %s" % (
            row.get("location") or "-", row.get("posted_date") or "-", score or "no score",
            status_label(spec, row.get("status")), flag)
        meta_lines = _wrap(meta, inner, "")
        lines.append(_fit(trail + "  " + meta_lines[0], width))
        lines.extend(_fit(trail + "  " + line, width) for line in meta_lines[1:])
    if len(rows) > 3:
        lines.append(_fit("%s  ... %d more row(s)" % (indent, len(rows) - 3), width))
    selected = (ctx.get("selection") or {}).get("row") or rows[0]
    lines.append(_fit("%s  focused listing detail" % indent, width))
    badge = next((child for child in node.get("children") or []
                  if (child.get("layout") or {}).get(proj.surface) == "badge"
                  and child.get("bind") == "listing.score"), None)
    if badge:
        score = bind_value(spec, ctx, proj.surface, badge, selected)
        if isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(score):
            lines.append(_fit("%s  [%s]" % (indent, value_text(spec, ctx, proj.surface, score)), width))
    for child in node.get("children") or []:
        if (child.get("layout") or {}).get(proj.surface) == "badge":
            continue
        label = child.get("label") or child.get("id")
        value = bind_value(spec, ctx, proj.surface, child, selected)
        if child.get("kind") == "status":
            if isinstance(value, bool):
                value = child.get("true_label") if value else child.get("false_label")
            else:
                value = status_label(spec, value)
        text = value_text(spec, ctx, proj.surface, value, child.get("fallback", "-"))
        lines.extend(_kv(indent + "    ", label, text, width, label_width=13))
    return lines


def _kv(indent, label, value, width, label_width=20):
    """One label and a possibly long value, wrapped with a hanging indent."""
    head = "%s%s" % (indent, (label + ": ").ljust(label_width))
    avail = max(16, width - len(head))
    text = str(value)
    if len(text) <= avail:
        return [head + text]
    chunks = _wrap(text, avail, "")
    out = [head + chunks[0]]
    out.extend(" " * len(head) + chunk for chunk in chunks[1:])
    return [line[:width] for line in out]


def _fit(text, width):
    text = _terminal_text(text)
    if width and len(text) > width:
        return text[:max(0, width - 1)] + "~"
    return text


def _terminal_text(text):
    """Keep untrusted listing text from injecting control codes into the TUI."""
    text = _plain_hyphens(str(text)).replace("\t", "    ")
    return "".join(character if character.isprintable() else " " for character in text)


_COLOUR = {"on": False}

# Palettes follow the published upstream anchors of each colourway, with the
# background, surface, border and muted steps derived from them so one
# structure can switch. Roles: bg, surface, raised, fg, muted, border, accent,
# success, warning, danger, cyan, magenta.
THEMES = {
    "catppuccin-mocha": {
        "label": "Catppuccin Mocha",
        "bg": "#1e1e2e", "surface": "#181825", "raised": "#313244",
        "fg": "#cdd6f4", "muted": "#a6adc8", "border": "#45475a",
        "accent": "#89b4fa", "success": "#a6e3a1", "warning": "#f9e2af",
        "danger": "#f38ba8", "cyan": "#89dceb", "magenta": "#cba6f7",
    },
    "catppuccin-latte": {
        "label": "Catppuccin Latte",
        "bg": "#eff1f5", "surface": "#e6e9ef", "raised": "#ccd0da",
        # Latte anchors darkened where the light background needs AA contrast.
        "fg": "#4c4f69", "muted": "#5b5e76", "border": "#bcc0cc",
        "accent": "#1d63ee", "success": "#327d22", "warning": "#986114",
        "danger": "#d00f38", "cyan": "#037cac", "magenta": "#ab5694",
    },
    "monokai-pro": {
        "label": "Monokai Pro",
        "bg": "#2d2a2e", "surface": "#221f22", "raised": "#403e41",
        "fg": "#fcfcfa", "muted": "#c1c0c0", "border": "#5b595c",  # Monokai Pro is proprietary; values match the MIT port
        "accent": "#78dce8", "success": "#a9dc76", "warning": "#ffd866",
        "danger": "#ff6188", "cyan": "#78dce8", "magenta": "#ab9df2",
    },
    "tokyo-night": {
        "label": "Tokyo Night",
        "bg": "#1a1b26", "surface": "#16161e", "raised": "#292e42",
        "fg": "#c0caf5", "muted": "#a9b1d6", "border": "#3b4261",
        "accent": "#7aa2f7", "success": "#9ece6a", "warning": "#e0af68",
        "danger": "#f7768e", "cyan": "#7dcfff", "magenta": "#bb9af7",
    },
    "dracula": {
        "label": "Dracula",
        "bg": "#282a36", "surface": "#21222c", "raised": "#44475a",
        "fg": "#f8f8f2", "muted": "#bfbfb5", "border": "#4d5066",
        "accent": "#8be9fd", "success": "#50fa7b", "warning": "#f1fa8c",
        "danger": "#ff5c5c", "cyan": "#8be9fd", "magenta": "#ff79c6",
    },
    "nord": {
        "label": "Nord",
        "bg": "#2e3440", "surface": "#292e39", "raised": "#3b4252",
        "fg": "#eceff4", "muted": "#a9b3c4", "border": "#4c566a",
        "accent": "#88c0d0", "success": "#a3be8c", "warning": "#ebcb8b",
        "danger": "#d66d77", "cyan": "#8fbcbb", "magenta": "#b48ead",
    },
    "gruvbox-dark": {
        "label": "Gruvbox Dark",
        "bg": "#282828", "surface": "#1d2021", "raised": "#3c3836",
        "fg": "#ebdbb2", "muted": "#bdae93", "border": "#504945",
        "accent": "#83a598", "success": "#b8bb26", "warning": "#fabd2f",
        "danger": "#fb4934", "cyan": "#8ec07c", "magenta": "#d3869b",
    },
    "solarized-dark": {
        "label": "Solarized Dark",
        "bg": "#002b36", "surface": "#00252e", "raised": "#073642",
        "fg": "#eee8d5", "muted": "#93a1a1", "border": "#0d4a58",
        "accent": "#268bd2", "success": "#859900", "warning": "#b58900",
        "danger": "#e53431", "cyan": "#2aa198", "magenta": "#db3887",
    },
    "one-dark": {
        "label": "One Dark",
        "bg": "#282c34", "surface": "#21252b", "raised": "#2f333d",
        "fg": "#abb2bf", "muted": "#8e949f", "border": "#3b4048",
        "accent": "#61afef", "success": "#98c379", "warning": "#e5c07b",
        "danger": "#e06c75", "cyan": "#56b6c2", "magenta": "#c678dd",
    },
}
THEME_ROLES = ("bg", "surface", "raised", "fg", "muted", "border",
               "accent", "success", "warning", "danger", "cyan", "magenta")
_THEME = {"name": os.environ.get("JOBUI_THEME", "catppuccin-mocha")}


def load_themes():
    return {name: dict(theme) for name, theme in THEMES.items()}


def current_theme():
    return THEMES.get(_THEME["name"]) or THEMES["catppuccin-mocha"]


def theme_names():
    return list(THEMES.keys())


def set_theme(name):
    """Select a colourway by name; unknown names are rejected, never guessed."""
    if name not in THEMES:
        return False
    _THEME["name"] = name
    return True


def next_theme(step=1):
    names = theme_names()
    at = names.index(_THEME["name"]) if _THEME["name"] in names else 0
    _THEME["name"] = names[(at + step) % len(names)]
    return _THEME["name"]


def _rgb(value):
    value = str(value or "").lstrip("#")
    if len(value) != 6:
        return (0, 0, 0)
    return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))


def _hex(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, round(channel))) for channel in rgb)


def _mix(first, second, amount):
    """Blend two colours; used to derive steps the upstream palette omits."""
    a, b = _rgb(first), _rgb(second)
    return _hex(tuple(a[i] + (b[i] - a[i]) * amount for i in range(3)))


def theme_colours(theme=None):
    """Every role one renderer may use, derived only from the theme's anchors."""
    theme = theme or current_theme()
    colours = {role: theme[role] for role in THEME_ROLES}
    colours["faint"] = _mix(theme["fg"], theme["bg"], 0.55)
    colours["selected"] = _mix(theme["accent"], theme["bg"], 0.72)
    colours["line"] = _mix(theme["border"], theme["bg"], 0.35)
    return colours


CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config"),
    "jobui")
THEME_FILE = os.path.join(CONFIG_DIR, "theme")


def saved_generation_setting(config_dir, name, choices, default):
    try:
        with open(os.path.join(config_dir, name), encoding="utf-8") as handle:
            code = handle.read().strip()
    except (OSError, ValueError):
        return default
    return code if code in choices else default


def save_generation_setting(config_dir, name, code):
    os.makedirs(config_dir, exist_ok=True)
    path = os.path.join(config_dir, name)
    staged = path + ".tmp"
    with open(staged, "w", encoding="utf-8") as handle:
        handle.write(code + "\n")
    os.replace(staged, path)


def save_theme(name):
    """Persist the chosen colourway outside the repo, so a build never resets it."""
    try:
        os.makedirs(os.path.dirname(THEME_FILE), exist_ok=True)
        with open(THEME_FILE, "w", encoding="utf-8") as handle:
            handle.write(name + "\n")
        return True
    except OSError:
        return False


def saved_theme(fallback="catppuccin-mocha"):
    """Preference from JOBUI_THEME, then the saved name, then the default."""
    if os.environ.get("JOBUI_THEME") in THEMES:
        return os.environ["JOBUI_THEME"]
    try:
        with open(THEME_FILE, encoding="utf-8") as handle:
            name = handle.read().strip()
    except OSError:
        return fallback
    return name if name in THEMES else fallback


def theme_label():
    return current_theme().get("label", _THEME["name"])


def _colour_enabled(stream=None):
    """Colour only on a real terminal; NO_COLOR and dumb terminals stay plain."""
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR") or os.environ.get("TERM", "") == "dumb":
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def _c(text, *codes):
    """Apply SGR codes when colour is on; return the plain string otherwise."""
    if not text or not codes or not _COLOUR.get("on"):
        return text
    return "\033[%sm%s\033[0m" % (";".join(codes), text)


def _fg_sgr(role):
    """Truecolour foreground for a theme role, so a switch recolours everything."""
    return "38;2;%d;%d;%d" % _rgb(theme_colours()[role])


def _bg_sgr(role):
    return "48;2;%d;%d;%d" % _rgb(theme_colours()[role])


def _theme_fg(role):
    return _fg_sgr(role if role in THEME_ROLES + ("faint", "line") else "accent")


def _score_sgr(score):
    """Red to green score colour on the theme's accent-to-success ramp."""
    if (not isinstance(score, (int, float)) or isinstance(score, bool)
            or not math.isfinite(score)):
        return _fg_sgr("muted")
    colours = theme_colours()
    ratio = max(0.0, min(10.0, float(score))) / 10.0
    red, green, blue = colorsys.hls_to_rgb(ratio * (125 / 360.0), 0.70, 0.80)
    ramp = _hex((red * 255, green * 255, blue * 255))
    # Keep the theme's hue family: blend the ramp toward its success or danger.
    target = colours["success"] if ratio >= 0.5 else colours["danger"]
    mixed = _mix(ramp, target, 0.45)
    return "38;2;%d;%d;%d" % _rgb(mixed)


def _paint(coloured, plain, width):
    """Pad a coloured line to width using the visible length of the plain line."""
    pad = " " * max(0, (width or 0) - len(plain))
    return coloured + pad


def _wrap(text, width, indent=""):
    words = str(text).split()
    lines, current = [], indent
    for word in words:
        if len(current) + len(word) + 1 > width and current.strip():
            lines.append(current)
            current = indent + word
        else:
            current = current + (" " if current.strip() else "") + word
    lines.append(current)
    return lines


_BUTTON_ROLE = {"neutral": "surface", "primary": "accent", "secondary": "cyan",
                "info": "cyan", "success": "success", "warning": "warning",
                "danger": "danger", "muted": "raised"}


def _button_sgr(role, active):
    """Filled active buttons and dim inactive ones, both from the theme."""
    if not active:
        return (_fg_sgr("muted"), "2")
    colours = theme_colours()
    fill_role = _BUTTON_ROLE.get(role or "neutral", "surface")
    fill = colours[fill_role]
    # Pick the readable ink for that fill instead of assuming dark or light.
    ink = colours["bg"] if _luminance(fill) > 0.45 else colours["fg"]
    return ("38;2;%d;%d;%d" % _rgb(ink),
            "48;2;%d;%d;%d" % _rgb(fill), "1")


def _luminance(value):
    red, green, blue = (channel / 255.0 for channel in _rgb(value))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _pill(label, key="", active=False, role="neutral"):
    mark = "●" if active else "○"
    key_text = (str(key) + " ") if key else ""
    plain = " %s %s%s " % (mark, key_text, _terminal_text(label))
    return plain, [(plain, _button_sgr(role, active))]


def _pack_pills(pills, width, gap="  "):
    """Pack rounded button pills without splitting a button across lines."""
    width = max(1, width)
    rows = []
    segments = []
    visible = 0
    for pill in pills:
        plain, style_segments = pill
        if len(plain) > width:
            plain = _fit(plain, width)
            codes = style_segments[0][1] if style_segments else None
            style_segments = [(plain, codes)]
        if segments and visible + len(gap) + len(plain) > width:
            rows.append(("".join(part for part, _ in segments), segments))
            segments = []
            visible = 0
        if segments:
            segments.append((gap, None))
            visible += len(gap)
        segments.extend(style_segments)
        visible += len(plain)
    if segments:
        rows.append(("".join(part for part, _ in segments), segments))
    return rows


# -- process gauge --------------------------------------------------------
# The stage vocabulary is the pipeline: entries with "flow": true are the steps
# an application walks, the rest are the branch ends it can stop at. Nothing
# here decides where a record sits; it only reads the stage the writer stored.
STAGE_DONE = "●"
STAGE_NOW = "◉"
STAGE_TODO = "○"


def _stage_entries(spec):
    entries = (spec.get("vocabulary") or {}).get("stages") or []
    return [entry for entry in entries if isinstance(entry, dict) and entry.get("key")]


def stage_flow(spec):
    return [entry for entry in _stage_entries(spec) if entry.get("flow", True)]


def stage_outcomes(spec):
    return [entry for entry in _stage_entries(spec) if not entry.get("flow", True)]


def stage_entry(spec, key):
    return next((entry for entry in _stage_entries(spec) if entry.get("key") == key), {})


def stage_label(spec, key):
    entry = stage_entry(spec, key)
    return _terminal_text(entry.get("label") or key or "-")


def stage_gauge_state(spec, row):
    """(current stage, reached index in the pipeline, outcome key or "")."""
    flow = [entry.get("key") for entry in stage_flow(spec)]
    if not flow:
        return "", 0, ""
    current = _plain_hyphens(str((row or {}).get("stage") or "")).strip()
    outcomes = [entry.get("key") for entry in stage_outcomes(spec)]
    if current in flow:
        return current, flow.index(current), ""
    if current in outcomes:
        return current, len(flow) - 1, current
    return flow[0], 0, ""


def next_stage_key(spec, stage):
    """One step further along the pipeline, or "" when it ends there."""
    flow = [entry.get("key") for entry in stage_flow(spec)]
    if not flow:
        return ""
    stage = _plain_hyphens(str(stage or "")).strip()
    if stage in flow:
        return flow[flow.index(stage) + 1] if flow.index(stage) + 1 < len(flow) else ""
    if stage in [entry.get("key") for entry in stage_outcomes(spec)]:
        return ""              # a decided application has no next step
    return flow[0]


def prev_stage_key(spec, stage):
    """One step back along the pipeline, or "" when there is nothing behind."""
    flow = [entry.get("key") for entry in stage_flow(spec)]
    if not flow:
        return ""
    stage = _plain_hyphens(str(stage or "")).strip()
    if stage in flow:
        index = flow.index(stage)
        return flow[index - 1] if index > 0 else ""
    if stage in [entry.get("key") for entry in stage_outcomes(spec)]:
        return flow[-1]           # back out of a decided outcome into the flow
    return ""


def stage_needs_outcome(spec, stage):
    """True when stepping forward would leave the pipeline and pick an outcome.

    The last flow step is where an application stops waiting and becomes either
    an interview, a rejection, or a success, so the surface has to ask instead
    of guessing.
    """
    flow = [entry.get("key") for entry in stage_flow(spec)]
    stage = _plain_hyphens(str(stage or "")).strip()
    return bool(flow) and stage == flow[-1]


OUTCOME_CHOICES = ("interview", "rejected", "success")


def _wrap_tokens(tokens, width):
    """Pack (plain, sgr) tokens into coloured lines without splitting a step."""
    width = max(1, int(width or 1))
    rows, segments, visible = [], [], 0
    for plain, codes in tokens:
        if len(plain) > width:
            plain = _fit(plain, width)
        if segments and visible + len(plain) > width:
            rows.append(("".join(part for part, _ in segments), segments))
            segments, visible = [], 0
        segments.append((plain, codes))
        visible += len(plain)
    if segments:
        rows.append(("".join(part for part, _ in segments), segments))
    return rows


def stage_gauge_lines(spec, row, width):
    """Unread > Reading > Generating > Control > Sending > Confirming > Waiting > Call."""
    _current, index, outcome = stage_gauge_state(spec, row)
    tokens = []
    for position, entry in enumerate(stage_flow(spec)):
        if entry.get("strip") is False:
            continue              # stage 0: a pre-stage, no circle of its own
        if tokens:
            tokens.append(("──", (_theme_fg("line"),)))
        if position == index:
            glyph, style = STAGE_NOW, ("1", _theme_fg(entry.get("role") or "accent"))
        elif position < index:
            glyph, style = STAGE_DONE, (_theme_fg("success"),)
        else:
            glyph, style = STAGE_TODO, (_theme_fg("line"), "2")
        name = _terminal_text(entry.get("short") or entry.get("label") or entry.get("key"))
        tokens.append(("%s %s" % (glyph, name), style))
    if outcome:
        entry = stage_entry(spec, outcome)
        tokens.append((" ▸ ", (_theme_fg("line"),)))
        tokens.append((_terminal_text(entry.get("label") or outcome),
                       ("1", _theme_fg(entry.get("role") or "accent"))))
    return _wrap_tokens(tokens, width)


def stage_strip(spec, row):
    """Compact card mark: one glyph per pipeline step plus the stage name."""
    current, index, outcome = stage_gauge_state(spec, row)
    if not current:
        return None
    flow = [entry for entry in stage_flow(spec) if entry.get("strip") is not False]
    marks = []
    for position, entry in enumerate(stage_flow(spec)):
        if entry.get("strip") is False:
            continue
        marks.append(STAGE_NOW if position == index
                     else STAGE_DONE if position < index else STAGE_TODO)
    glyphs = "".join(marks)
    key = outcome or current
    entry = stage_entry(spec, key)
    plain = " %s %s " % (glyphs, _terminal_text(entry.get("label") or key))
    codes = (_theme_fg(entry.get("role") or "accent"),)
    if outcome:
        codes = ("1",) + codes
    return plain, [(plain, codes)]


def _days_since(iso_date):
    try:
        start = datetime.strptime(iso_date, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    return (date.today() - start).days


def stage_note(spec, row):
    """Age in the current stage, and when the 7 day wait turns into a call."""
    current, _index, outcome = stage_gauge_state(spec, row)
    parts = []
    stage_at = str((row or {}).get("stage_at") or "")
    days = _days_since(stage_at)
    if stage_at and days is not None:
        parts.append("in stage since %s (%d d)" % (stage_at, days))
    due = str((row or {}).get("stage_due") or "")
    if due and current in ("sent", "confirmed", "waiting"):
        left = _days_since(due)
        if left is None:
            pass
        elif left > 0:
            parts.append("call/contact %d d overdue" % left)
        elif left == 0:
            parts.append("call/contact due today")
        else:
            parts.append("call/contact in %d d" % -left)
    if outcome:
        parts.append("outcome %s" % stage_label(spec, outcome))
    return " · ".join(parts)


def _rounded_box(title, content, width, border_sgr=None, title_sgr=None):
    """Render a rounded terminal card; content may carry per-segment colours."""
    width = max(10, int(width or 78))
    border_sgr = border_sgr or _theme_fg("border")
    inner = width - 4
    title = _terminal_text(title)
    title = _fit(title, max(1, width - 5))
    fill = max(0, width - 5 - len(title))
    top_plain = "╭─ " + title + " " + "─" * fill + "╮"
    title_codes = title_sgr or ("1", _theme_fg("accent"))
    if isinstance(title_codes, str):
        title_codes = (title_codes,)
    top = (_c("╭─ ", border_sgr) + _c(title, *title_codes)
           + _c(" " + "─" * fill + "╮", border_sgr))
    bottom = _c("╰" + "─" * (width - 2) + "╯", border_sgr)
    out = [top if _COLOUR.get("on") else top_plain]
    for item in content or []:
        segments = None
        style = None
        if isinstance(item, tuple) and len(item) == 2:
            plain, style_data = item
            if isinstance(style_data, list):
                segments = style_data
            elif isinstance(style_data, str):
                style = (style_data,)
            elif style_data:
                style = tuple(style_data)
        else:
            plain = item
        plain = _fit(plain, inner)
        if segments is None:
            padded = plain.ljust(inner)
            body = _c(padded, *(style or ()))
        else:
            pieces = []
            used = 0
            for text, codes in segments:
                text = _terminal_text(text)
                if used >= inner:
                    break
                shown = text[:inner - used]
                used += len(shown)
                if codes:
                    if isinstance(codes, str):
                        codes = (codes,)
                    pieces.append(_c(shown, *codes))
                else:
                    pieces.append(shown)
            pieces.append(" " * max(0, inner - used))
            body = "".join(pieces)
        out.append(_c("│ ", border_sgr) + body + _c(" │", border_sgr))
    out.append(bottom)
    return out


# --------------------------------------------------------------------------
# browser projection
# --------------------------------------------------------------------------

def gui_theme_css():
    """One rule block per colourway; switching data-theme recolours the page."""
    blocks = []
    for name, theme in THEMES.items():
        blocks.append(
            'body[data-theme="%s"]{'
            "--ui-bg:%s;--ui-card:%s;--ui-fg:%s;--ui-muted:%s;--ui-border:%s;"
            "--ui-accent:%s;--ui-success:%s;--ui-warning:%s;--ui-danger:%s;"
            "--ui-raised:%s;--ui-cyan:%s;--ui-magenta:%s;color-scheme:%s}"
            % (name, theme["surface"], theme["bg"], theme["fg"], theme["muted"],
               theme["border"], theme["accent"], theme["success"], theme["warning"],
               theme["danger"], theme["raised"], theme["cyan"], theme["magenta"],
               "light" if _luminance(theme["bg"]) > 0.55 else "dark"))
    return "\n".join(blocks) + "\n"


def gui_theme_options(selected):
    return "".join('<option value="%s"%s>%s</option>'
                   % (html.escape(name), ' selected' if name == selected else "",
                      html.escape(theme["label"]))
                   for name, theme in THEMES.items())


GUI_TEMPLATE = """<!doctype html>
<html lang="%(lang)s"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%(title)s</title>
<style>%(css)s</style></head>
<body data-surface="gui" data-theme="%(theme)s">
<header class="app-head">
  <div>
    <h1>%(title)s</h1>
    <p class="sub">%(subtitle)s</p>
    <p class="meta" id="scan-line">%(scan_line)s</p>
  </div>
  <dl class="meters">%(meters)s</dl>
  <label class="theme-pick">Theme
    <select id="theme-select" aria-label="Colour theme">%(themes)s</select>
  </label>
</header>
<details class="view-switcher"><summary>Views</summary>
  <nav class="tabbar" role="tablist" aria-label="Views">%(tabs)s</nav>
</details>
<div class="shell">
  <nav class="app-nav" aria-label="Sections">%(nav)s</nav>
  <main id="main">%(main)s</main>
</div>
<p class="foot">%(foot)s</p>
<p class="status" id="feedback" role="status" aria-live="polite"></p>
<script>%(js)s</script>
</body></html>
"""

GUI_CSS = """
:root{
  /* One palette anchor per role; every other colour derives from these. */
  --ui-bg:#1e1e2e; --ui-card:#181825; --ui-fg:#cdd6f4; --ui-muted:#a6adc8;
  --ui-border:#45475a; --ui-accent:#89b4fa; --ui-success:#a6e3a1;
  --ui-warning:#f9e2af; --ui-danger:#f38ba8; --ui-raised:#313244;
  --ui-cyan:#89dceb; --ui-magenta:#cba6f7;
  color-scheme: dark;
}
*{box-sizing:border-box}
body{margin:0;padding:16px 18px 24px;background:var(--ui-bg);
color:var(--ui-fg);font-family:var(--font,system-ui,-apple-system,"Segoe UI",sans-serif);
font-size:14px;line-height:1.45}
h1{font-size:19px;margin:0 0 2px;letter-spacing:0}
.sub{margin:0;color:var(--ui-muted);font-size:12.5px}
.meta{margin:5px 0 0;color:var(--ui-muted);font-size:12px}
.app-head{display:flex;flex-wrap:wrap;gap:14px 22px;justify-content:space-between;
align-items:flex-start;padding-bottom:12px;border-bottom:1px solid var(--ui-border)}
.meters{display:flex;flex-wrap:wrap;gap:6px;margin:0}
.meters div{background:var(--ui-card);border:1px solid var(--ui-border);
border-radius:8px;padding:5px 10px;min-width:74px}
.meters dt{font-size:11px;color:var(--ui-muted);margin:0}
.meters dd{margin:0;font-weight:650;font-variant-numeric:tabular-nums}
.theme-pick{display:inline-flex;align-items:center;gap:6px;font-size:12px;
color:var(--ui-muted)}
.theme-pick select{font-size:12px;padding:4px 7px}
.tabbar{display:flex;gap:4px;flex-wrap:wrap;margin:14px 0 0;border-bottom:1px solid var(--ui-border)}
.tab{appearance:none;background:transparent;border:0;border-bottom:2px solid transparent;
color:var(--ui-muted);font:inherit;font-size:13px;font-weight:650;
padding:7px 11px;cursor:pointer;border-radius:6px 6px 0 0}
.tab:hover{color:var(--ui-fg);background:var(--ui-card)}
.tab[aria-selected="true"]{color:var(--ui-accent);border-bottom-color:var(--ui-accent)}
.tab:focus-visible{outline:2px solid var(--ui-accent);outline-offset:2px}
.tabpanel[hidden]{display:none}
body[data-tab="tab.listings"] .app-nav{display:none}
body[data-tab="tab.listings"] .shell{gap:0}
body[data-tab="tab.listings"] .app-head .sub,
body[data-tab="tab.listings"] .app-head .meta,
body[data-tab="tab.listings"] .app-head .meters{display:none}
body[data-tab="tab.listings"] .app-head{border:0;padding-bottom:0}
.view-switcher{position:relative;display:inline-block;margin-top:8px}
.view-switcher summary{color:var(--ui-muted);cursor:pointer;font-size:12px}
.view-switcher .tabbar{position:absolute;z-index:5;min-width:220px;display:grid;
  background:var(--ui-card);border:1px solid var(--ui-border);
  padding:6px;border-radius:8px;margin:2px 0;box-shadow:0 12px 24px #0006}
.view-switcher .tab{display:block;text-align:left;border:0;border-radius:5px}
.view-switcher .tab[aria-selected="true"]{background:color-mix(in srgb,var(--ui-accent) 22%,var(--ui-bg))}
.list-top{display:flex;gap:12px;align-items:baseline;justify-content:space-between;
  color:var(--ui-muted);font-size:12px;margin:0 0 8px;max-width:900px}
.list-filters{display:flex;flex-wrap:wrap;align-items:center;gap:5px;
  margin:0 0 9px;max-width:900px}
.list-filters input[type=search]{flex:1 1 180px}
.list-filters .stage-filter{display:flex;align-items:center;gap:5px;
  color:var(--ui-muted);font-size:11.5px}
.list-filters button{padding:4px 7px;font-size:11.5px}
.job-list li[hidden]{display:none}
.job-list{list-style:none;margin:0;padding:0;display:grid;gap:7px;max-width:900px}
.job-list li{border:1px solid var(--ui-border);border-radius:9px;
  background:var(--ui-card);overflow:hidden}
.job-list li:has(.job-row.is-selected){border-color:var(--ui-accent)}
.job-row{display:grid;grid-template-columns:auto minmax(0,1fr) var(--pay-col,0ch);
  column-gap:10px;row-gap:3px;align-items:baseline;border:0;border-radius:0;
  background:transparent;width:100%;text-align:left;font-size:14px;
  padding:9px 13px;color:var(--ui-fg)}
.job-row-score{grid-column:1;grid-row:1}
.job-row-title{grid-column:2;grid-row:1;font-weight:650;overflow-wrap:anywhere}
.job-row-pay{grid-column:3;grid-row:1;justify-self:end;white-space:nowrap;
  font-size:12px;color:var(--ui-fg)}
.job-row-city{grid-column:2 / span 2;grid-row:2;display:flex;flex-wrap:wrap;gap:5px}
.job-row-city .tag-chip{min-width:var(--city-col,0ch);justify-content:center}
.job-row-bookmark{display:inline-flex;align-items:center;margin-left:7px;padding:1px 6px;
  border:1px solid currentColor;border-radius:999px;color:var(--ui-accent);
  font-size:10px;font-weight:700;vertical-align:middle}
.job-row-bookmark[hidden]{display:none}
.generation-indicator{display:inline-flex;align-items:center;gap:5px;color:var(--ui-accent);
  font-size:12px;font-weight:600;margin-inline-start:6px}
.generation-indicator[hidden]{display:none}
.generation-indicator::before{content:"";inline-size:.85em;block-size:.85em;border:2px solid currentColor;
  border-inline-end-color:transparent;border-radius:50%;animation:cover-letter-spin .8s linear infinite}
@keyframes cover-letter-spin{to{transform:rotate(360deg)}}
@media(prefers-reduced-motion:reduce){.generation-indicator::before{animation:none}}
.bookmark-state[hidden]{display:none}
.job-row:hover,.job-row.is-selected{background:color-mix(in srgb,
  var(--ui-accent) 12%,var(--ui-card))}
.job-row.is-selected{box-shadow:inset 3px 0 var(--ui-accent)}
.job-row:focus-visible{outline:2px solid var(--ui-accent);outline-offset:-2px}
.job-detail[hidden],.job-list-view[hidden],.job-card[hidden]{display:none}
.job-detail{max-width:780px}
.job-detail h2{font-size:19px;line-height:1.35;margin:12px 0 16px}
.job-properties,.ad-panel{border:1px solid color-mix(in srgb,
  var(--ui-fg) 48%, var(--ui-card));
  border-radius:9px;padding:12px 14px;margin:12px 0;max-width:76ch}
.job-properties h3{font-size:13px;margin:0 0 9px}
.job-detail dl{display:grid;grid-template-columns:140px minmax(0,1fr);gap:9px 16px;margin:0}
.job-detail dt{color:var(--ui-muted)}
.job-detail dd{margin:0;overflow-wrap:anywhere;white-space:pre-wrap}
.ad-panel summary{cursor:pointer;font-size:13px;font-weight:650}
.ad-panel summary:focus-visible,.ad-archive summary:focus-visible{
  outline:2px solid var(--ui-accent);outline-offset:2px}
.ad-section{margin:16px 0 20px;max-width:76ch}
.ad-section h3{font-size:15px;margin:0 0 7px;color:var(--ui-fg)}
.ad-section summary{cursor:pointer;list-style:none;display:flex;align-items:center;gap:7px}
.ad-section summary::-webkit-details-marker{display:none}
.ad-section summary::before{content:"\\25BE";font-size:11px;color:var(--ui-muted)}
.ad-section:not([open]) summary::before{content:"\\25B8"}
.ad-section summary:focus-visible{outline:2px solid var(--ui-accent);outline-offset:3px}
.ad-section summary h3{margin:0}
.ad-section.is-focused{box-shadow:inset 3px 0 var(--ui-accent);padding-left:12px}
.ad-section.is-focused summary h3{color:var(--ui-accent)}
.ad-section p,.ad-archive p{white-space:pre-wrap;overflow-wrap:anywhere;margin:0;line-height:1.55}
.ad-section p+p,.ad-archive p+p{margin-top:12px}
.ad-section p.is-paragraph,.ad-archive p.is-paragraph,.card-preview p.is-paragraph{
  outline:2px solid var(--ui-accent);outline-offset:3px;border-radius:3px}
.ad-archive{margin:14px 0 20px;max-width:76ch;color:var(--ui-muted)}
.ad-archive summary{cursor:pointer}
.ad-archive p{margin-top:9px;color:var(--ui-fg)}
.card-preview{margin:0 0 18px;color:var(--ui-muted);font-size:12px}
.card-preview summary{cursor:pointer}
.card-preview summary:focus-visible{outline:2px solid var(--ui-accent);outline-offset:2px}
.card-preview p{white-space:pre-wrap;overflow-wrap:anywhere;max-width:72ch}
.card-preview .card-caption{margin:0 0 4px;font-size:11px;font-weight:650;
  letter-spacing:.03em;text-transform:uppercase;color:var(--ui-muted)}
.card-preview .card-body{color:var(--ui-fg)}
.job-detail .flags{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin:0 0 16px}
.stage-chip{margin-left:auto;display:inline-flex;align-items:center;gap:7px;
font-size:12px;font-weight:700;letter-spacing:.02em;padding:5px 11px;
border-radius:999px;border:1.5px solid currentColor;white-space:nowrap;
color:var(--ui-muted);background:var(--ui-card)}
.stage-chip [aria-hidden]{letter-spacing:.12em}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;
clip:rect(0,0,0,0);white-space:nowrap;border:0}
.stage-chip.is-primary{color:var(--ui-accent)}
.stage-chip.is-success{color:var(--ui-success)}
.stage-chip.is-warning{color:var(--ui-warning)}
.stage-chip.is-danger{color:var(--ui-danger)}
.stage-chip.is-info{color:color-mix(in srgb,var(--ui-accent) 70%,var(--ui-fg))}
.job-detail a{color:var(--ui-accent)}
.job-detail a.act{display:inline-flex;align-items:center;gap:5px;text-decoration:none;
border:1px solid var(--ui-border);border-radius:7px;
background:var(--ui-card)}
.job-detail a.act:hover{border-color:var(--ui-accent)}
.job-detail a.act:focus-visible{outline:2px solid var(--ui-accent);outline-offset:2px}
.job-back{margin:0 0 8px}
.job-action-group{margin:12px 0}
.outcome-panel{margin:8px 0 0;padding:9px 11px;border:1px solid var(--ui-accent);
  border-radius:9px;background:color-mix(in srgb,var(--ui-accent) 8%,transparent)}
.outcome-panel[hidden]{display:none}
.outcome-caption{margin:0 0 7px;font-size:12px;color:var(--ui-muted)}
.outcome-actions{display:flex;flex-wrap:wrap;gap:7px}
.job-actions-caption{margin:0 0 5px;color:var(--ui-muted);font-size:11px}
.job-actions{display:flex;gap:5px;flex-wrap:wrap;align-items:center}
.job-actions .act{display:inline-flex;gap:5px;align-items:center;justify-content:center;
min-height:28px;padding:4px 7px;font-size:11.5px;line-height:1.2;white-space:nowrap}
.job-actions .act svg{display:block;width:13px;height:13px;flex:none}
.job-actions [data-action="toggle_bookmark"][aria-pressed=true]{border-color:var(--ui-accent)}
.bookmark-note{font-size:11px;color:var(--ui-muted);margin:6px 0 0}
.score-bubble{display:inline-flex;align-items:center;justify-content:center;vertical-align:middle;
height:18px;padding:0 5px;border:2px solid var(--score-color,var(--ui-muted));
border-radius:999px;background:color-mix(in srgb,var(--score-color,var(--ui-muted)) 15%,
var(--ui-card));color:var(--ui-fg);
font-size:11px;line-height:1;font-weight:800;letter-spacing:.02em;
font-variant-numeric:tabular-nums;white-space:nowrap}
.job-row .score-bubble{height:14px;padding:0 3px;font-size:10px}
/* The expanded score chip uses the flag geometry: same box, its own colour. */
.job-detail .flags .flag,.job-detail .flags .score-bubble{display:inline-flex;
  align-items:center;min-height:29px;padding:0 10px;font-size:12px;line-height:1;
  font-weight:700}
.job-detail .flags .score-bubble{border-width:2px}
.tag-chip{display:inline-flex;align-items:center;justify-content:center;vertical-align:middle;
height:18px;padding:0 6px;border:1.5px solid var(--tag-color,var(--ui-border));
border-radius:999px;color:var(--tag-color,var(--ui-muted));
background:color-mix(in srgb,var(--tag-color,var(--ui-border)) 12%,transparent);
font-size:11px;line-height:1;font-weight:650;white-space:nowrap;max-width:100%;
overflow:hidden;text-overflow:ellipsis}
.job-row .tag-chip{height:14px;padding:0 4px;font-size:10px}
.action-result{max-width:740px;margin:8px 0;padding:12px;border:1px solid var(--ui-border);
border-radius:8px;background:var(--ui-card)}
.action-result p{margin:0 0 8px}
.action-result [hidden]{display:none}
.packet-view{margin:10px 0 0;padding:10px;border:1px solid var(--ui-border);border-radius:8px;
background:var(--ui-surface,#141821);color:var(--ui-fg,#e8eaed);white-space:pre-wrap;
font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;max-height:52vh;overflow:auto}
.packet-view[hidden]{display:none}
#generation-dialog{max-width:calc(100% - 32px);padding:20px;border:1px solid var(--ui-border);
  border-radius:12px;background:var(--ui-bg);color:var(--ui-fg)}
#generation-dialog::backdrop{background:#0009}
#generation-dialog h2{margin:0 0 12px}
#generation-dialog select{display:block;margin:4px 0 16px;max-width:100%}
.generation-dialog-actions{display:flex;gap:8px;justify-content:flex-end}
.action-result textarea{display:block;width:100%;min-height:68px;resize:vertical;
background:var(--ui-bg);color:var(--ui-fg);
border:1px solid var(--ui-border);border-radius:6px;padding:8px;
font:12px ui-monospace,SFMono-Regular,monospace;margin-bottom:8px}
.profile-editor{max-width:760px}
.profile-editor h2{font-size:20px;margin:0 0 14px}
.profile-editor [role=tablist]{border-bottom:1px solid var(--ui-border);padding-bottom:6px}
.profile-editor p{max-width:72ch;color:var(--ui-fg)}
.file-label{display:block;margin:14px 0 5px;font-weight:600}
.profile-editor input[type=file]{display:block;max-width:100%;margin-bottom:14px}
.skill-entry{display:grid;grid-template-columns:1fr 1fr auto;gap:8px;align-items:end;margin:8px 0}
.skill-entry label{display:grid;gap:4px;font-size:12px;color:var(--ui-muted)}
.skill-entry input{min-width:0;width:100%;padding:7px 9px;border-radius:6px;
border:1px solid var(--ui-border);background:var(--ui-card);
color:var(--ui-fg);font:inherit}
.skill-entry input:focus-visible,.profile-editor input:focus-visible,
.action-result textarea:focus-visible{outline:2px solid var(--ui-accent);outline-offset:2px}
.skill-controls{display:flex;flex-wrap:wrap;gap:8px;margin:16px 0}
@media(max-width:560px){.skill-entry{grid-template-columns:1fr auto}.skill-entry label:nth-child(2){grid-column:1/3}}
.list-help{margin:8px 0;color:var(--ui-muted);font-size:12px}
body[data-tab="tab.listings"] .foot{display:none}
.flagcell{white-space:nowrap}
td.cell{max-width:172px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
td.lead{max-width:250px;font-weight:600}
td.group{max-width:132px}
.rowdetail{margin:6px 0 10px;padding:7px 11px;border-left:3px solid var(--ui-accent);
background:var(--ui-card);border-radius:0 7px 7px 0;font-size:12.5px;
color:var(--ui-fg);line-height:1.45;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.rowdetail:empty{display:none}
.flags{display:inline-flex;gap:6px;align-items:center;flex-wrap:wrap}
.flag{font-size:12px;font-weight:700;letter-spacing:.04em;padding:5px 10px;border-radius:999px;
border:1.5px solid currentColor}
.flag.is-primary{color:var(--ui-accent)}.flag.is-success{color:var(--ui-success)}
.flag.is-info{color:color-mix(in srgb,var(--ui-accent) 70%,var(--ui-fg))}
.flag.is-warning{color:var(--ui-warning)}.flag.is-danger{color:var(--ui-danger)}
.flag.is-muted{color:var(--ui-muted)}
tr.is-cursor{background:color-mix(in srgb, var(--ui-accent) 14%, transparent);
box-shadow:inset 3px 0 0 var(--ui-accent)}
.shell{display:flex;gap:20px;align-items:flex-start;margin-top:16px}
.app-nav{flex:0 0 238px;position:sticky;top:12px;max-height:calc(100vh - 40px);overflow:auto}
.app-nav>ul{list-style:none;margin:0;padding:0}
.app-nav details{margin:0 0 2px}
.app-nav summary{cursor:pointer;font-weight:600;padding:5px 4px;font-size:12.5px;
color:var(--ui-fg);border-radius:6px}
.app-nav summary:focus-visible{outline:2px solid var(--ui-accent);outline-offset:1px}
.app-nav ul{list-style:none;margin:0;padding:0 0 4px 10px;
border-left:1px solid var(--ui-border)}
.app-nav a{display:flex;justify-content:space-between;align-items:center;gap:8px;
padding:4px 7px;border-radius:6px;color:inherit;text-decoration:none;font-size:12.5px}
.app-nav a:hover{background:var(--ui-card);color:var(--ui-accent)}
.app-nav a:focus-visible{outline:2px solid var(--ui-accent);outline-offset:1px}
main{flex:1 1 auto;min-width:0;max-width:1180px}
body[data-tab="tab.listings"] main{max-width:none}
section.panel{margin:0 0 18px}
section.panel>h2{font-size:13.5px;margin:0 0 2px;font-weight:650}
section.panel>h3{font-size:14.5px;margin:18px 0 3px;font-weight:650}
section.panel>h4{font-size:11.5px;letter-spacing:.05em;text-transform:uppercase;
margin:14px 0 4px;font-weight:650;color:var(--ui-muted)}
section.panel section.panel{margin:0}
section.panel>.hint{color:var(--ui-muted);font-size:12px;margin:0 0 8px;max-width:70ch}
.toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 9px}
input[type=search],select{background:var(--ui-card);color:inherit;
border:1px solid var(--ui-border);border-radius:7px;padding:6px 9px;font:inherit;font-size:13px}
input[type=search]{flex:1 1 240px;min-width:0}
select{max-width:100%}
button{font:inherit;font-size:12.5px;background:var(--ui-card);color:inherit;
border:1px solid var(--ui-border);border-radius:7px;padding:5px 10px;cursor:pointer}
button:hover{border-color:var(--ui-accent)}
button:disabled,input:disabled{opacity:.5;cursor:not-allowed}
button:focus-visible{outline:2px solid var(--ui-accent);outline-offset:2px}
button[aria-pressed=true]{border-color:var(--ui-accent);color:var(--ui-accent);
background:color-mix(in srgb, var(--ui-accent) 12%, transparent)}
button.is-primary{border-color:var(--ui-accent);color:var(--ui-accent);font-weight:650}
button.is-danger{border-color:color-mix(in srgb,var(--ui-danger) 55%,var(--ui-card));color:var(--ui-danger)}
.actwrap{display:inline-block}
.acts{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 12px}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.tablewrap{overflow-x:auto;max-width:100%;border:1px solid var(--ui-border);border-radius:8px}
table{border-collapse:collapse;width:100%;font-size:12.5px}
caption{text-align:left;padding:8px 9px 4px;font-size:11.5px}
th,td{text-align:left;padding:7px 9px;border-bottom:1px solid var(--ui-border);vertical-align:top}
table th,table td{padding:7px 7px}
th{font-size:11px;letter-spacing:.03em;text-transform:uppercase;
color:var(--ui-muted);font-weight:600;white-space:nowrap}
tr:last-child td{border-bottom:none}
tbody tr.is-focus{background:color-mix(in srgb, var(--ui-accent) 10%, transparent)}
tbody tr.is-focus td:first-child{box-shadow:inset 3px 0 0 var(--ui-accent)}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.badge{display:inline-block;border:1px solid var(--ui-border);border-radius:999px;
padding:0 7px;font-size:11px;line-height:17px;white-space:nowrap}
.badge.is-success{color:var(--ui-success);
border-color:color-mix(in srgb,var(--ui-success) 45%,var(--ui-card))}
.badge.is-danger{color:var(--ui-danger);
border-color:color-mix(in srgb,var(--ui-danger) 45%,var(--ui-card))}
.badge.is-info{color:var(--ui-accent);
border-color:color-mix(in srgb,var(--ui-accent) 45%,var(--ui-card))}
.badge.is-muted,.badge.is-neutral{color:#b6bfcc;border-color:#3d4859}
.badge.is-warning{color:#ffcf6b;border-color:#7a6b2f}
.callout{margin:0 0 10px;padding:8px 11px;border-left:3px solid var(--ui-border);
background:var(--ui-card);border-radius:0 7px 7px 0;font-size:12.5px;max-width:80ch}
.callout.is-warning{border-left-color:#ffcf6b}
.callout.is-danger{border-left-color:#ff8f8f}
.callout.is-muted{color:var(--ui-muted)}
dl.inline{display:grid;grid-template-columns:max-content 1fr;gap:3px 12px;margin:0 0 10px}
dl.inline dt{color:var(--ui-muted);font-size:12px}
dl.inline dd{margin:0}
kbd{font-family:ui-monospace,SFMono-Regular,monospace;font-size:10.5px;
border:1px solid var(--ui-border);border-radius:4px;padding:0 3px;
color:var(--ui-muted);margin-left:6px}
.foot{color:var(--ui-muted);font-size:11.5px;margin:18px 0 4px;max-width:80ch}
.status{min-height:18px;font-size:12.5px;color:var(--ui-accent);margin:0}
@media (max-width:860px){
  body{padding:12px}
  .shell{flex-direction:column;gap:12px}
  .app-nav{display:none}
  .tabbar{overflow-x:auto;flex-wrap:nowrap;padding-bottom:2px}
  .app-nav>ul{display:flex;flex-wrap:wrap;gap:4px 10px}
  .app-nav ul{padding-left:6px;border-left:none}
  .app-nav a{padding:3px 6px}
  .app-nav kbd{display:none}
  .shell{align-items:stretch}
  main{width:100%;max-width:none}
  .tablewrap{width:100%;max-width:100%}
  section.panel>.hint,.callout,.foot{max-width:100%}
  table{width:100%}
}
@media (max-width:480px){
  body{font-size:13px;padding:10px}
  h1{font-size:17px}
  .meters div{flex:1 1 44%;min-width:0}
  .app-head{gap:10px}
  dl.inline{grid-template-columns:1fr;gap:0 0}
  dl.inline dt{margin-top:4px}
  .job-detail dl{grid-template-columns:1fr;gap:0}
  .job-detail dd{margin:0 0 10px}
}
"""

GUI_JS = """
(function(){
  var jobs = Array.prototype.slice.call(document.querySelectorAll('.job-row'));
  var cards = Array.prototype.slice.call(document.querySelectorAll('.job-card'));
  var listView = document.getElementById('job-list-view');
  var detailView = document.getElementById('job-detail');
  var backButton = document.getElementById('job-back');
  var jobIndex = 0;
  var paragraphIndex = -1;
  var sectionIndex = -1;
  var inSection = false;
  function sectionsOf(card){
    return Array.prototype.slice.call(
      card.querySelectorAll('details.ad-section, details.ad-archive'));
  }
  function paragraphsOf(section){
    // A paragraph inside a closed section still reports a box, so the open
    // state of every wrapping details decides, not getClientRects.
    return Array.prototype.slice.call(section.querySelectorAll('p, .card-body'))
      .filter(function(p){ return !p.closest('details:not([open])'); });
  }
  var stageFlow = /*STAGE_FLOW*/[];
  var stageMeta = /*STAGE_META*/{};
  var currentStage = '';
  function stageStep(step){
    var at = stageFlow.indexOf(currentStage);
    if (at < 0) return step > 0 ? (stageFlow[0] || '') : '';
    var target = at + step;
    return target >= 0 && target < stageFlow.length ? stageFlow[target] : '';
  }
  function stageAtEnd(){
    return stageFlow.length > 0 && currentStage === stageFlow[stageFlow.length - 1];
  }
  var themeSelect = document.getElementById('theme-select');
  var themes = themeSelect
    ? Array.prototype.slice.call(themeSelect.options).map(function(option){ return option.value; })
    : [];
  function applyTheme(name, persist){
    if (!name || themes.indexOf(name) < 0) return;
    document.body.dataset.theme = name;
    if (themeSelect) themeSelect.value = name;
    if (!persist) return;
    try { localStorage.setItem('jobui.theme.v1', name); } catch (error) {}
  }
  if (themeSelect) themeSelect.addEventListener('change', function(){
    applyTheme(themeSelect.value, true);
  });
  try {
    var storedTheme = localStorage.getItem('jobui.theme.v1');
    if (storedTheme) applyTheme(storedTheme, false);
  } catch (error) {}
  function visibleJobIndexes(){
    return jobs.map(function(button, i){ return button.parentElement.hidden ? -1 : i; })
               .filter(function(i){ return i >= 0; });
  }
  function selectJob(index, focus){
    if (!jobs.length) return;
    var visible = visibleJobIndexes();
    if (!visible.length) {
      jobs.forEach(function(button){
        button.classList.remove('is-selected'); button.removeAttribute('aria-current');
        button.setAttribute('aria-expanded', 'false');
      });
      return;
    }
    var later = visible.filter(function(i){ return i >= index; });
    jobIndex = visible.indexOf(index) >= 0 ? index :
      (later.length ? later[0] : visible[visible.length - 1]);
    jobs.forEach(function(button, i){
      button.classList.toggle('is-selected', i === jobIndex);
      if (i === jobIndex) button.setAttribute('aria-current', 'true');
      else button.removeAttribute('aria-current');
      button.setAttribute('aria-expanded', detailView && !detailView.hidden && i === jobIndex ? 'true' : 'false');
    });
    cards.forEach(function(card, i){ card.hidden = i !== jobIndex; });
    if (focus) {
      jobs[jobIndex].focus({preventScroll:true});
      jobs[jobIndex].scrollIntoView({block:'nearest'});
    }
  }
  function openJob(index){
    if (!jobs.length || !detailView || !listView || jobs[index].parentElement.hidden) return;
    detailView.hidden = false;
    listView.hidden = true;
    paragraphIndex = -1;
    var result = document.getElementById('action-result');
    if (result) result.hidden = true;
    var command = document.getElementById('action-command');
    if (command) command.value = '';
    selectJob(index, false);
    var flags = cards[index] && cards[index].querySelector('.flags');
    var actions = document.querySelector('.job-action-group');
    if (flags && actions) {
      flags.insertAdjacentElement('afterend', actions);
      if (result) actions.insertAdjacentElement('afterend', result);
    }
    retarget({dataset:{id:jobs[index].dataset.id || '',
                       href:jobs[index].dataset.href || '',
                       stage:jobs[index].dataset.stage || '',
                       title:jobs[index].textContent || '', company:''}});
    refreshBookmark();
    showPacketText('');
    syncGenerationButton();
    var id = jobs[index].dataset.id;
    if (generationNotices.has(id)) {
      showAction(generationNotices.get(id), '');
      generationNotices.delete(id);
    }
    sectionIndex = -1;
    inSection = false;
    paragraphIndex = -1;
    focusSection(0);
    if (backButton) backButton.focus();
  }
  function closeJob(){
    if (!detailView || detailView.hidden || !listView) return;
    detailView.hidden = true;
    listView.hidden = false;
    paragraphIndex = -1;
    sectionIndex = -1;
    inSection = false;
    selectJob(jobIndex, true);
  }
  function moveJob(step){
    var visible = visibleJobIndexes();
    if (!visible.length) return;
    var at = visible.indexOf(jobIndex);
    selectJob(visible[Math.max(0, Math.min(visible.length - 1, at + step))], true);
  }
  function stepParagraph(step){
    var card = cards[jobIndex];
    if (!card || !inSection) return;
    var section = sectionsOf(card)[sectionIndex];
    if (!section) return;
    if (!section.open) section.open = true;
    var panel = card.querySelector('.ad-panel');
    if (panel && !panel.open) panel.open = true;
    var targets = paragraphsOf(section);
    if (!targets.length) return;
    Array.prototype.slice.call(card.querySelectorAll('.is-paragraph'))
      .forEach(function(p){ p.classList.remove('is-paragraph'); });
    paragraphIndex = Math.max(0, Math.min(targets.length - 1,
      paragraphIndex < 0 ? 0 : paragraphIndex + step));
    var target = targets[paragraphIndex];
    target.classList.add('is-paragraph');
    target.tabIndex = -1;
    target.focus({preventScroll:true});
    target.scrollIntoView({block:'nearest'});
  }
  function focusSection(step){
    var card = cards[jobIndex];
    if (!card) return;
    var list = sectionsOf(card);
    if (!list.length) return;
    sectionIndex = Math.max(0, Math.min(list.length - 1,
      sectionIndex < 0 ? 0 : sectionIndex + step));
    list.forEach(function(section, i){
      section.classList.toggle('is-focused', i === sectionIndex);
    });
    list[sectionIndex].scrollIntoView({block:'nearest'});
  }
  function enterSection(){
    var card = cards[jobIndex];
    if (!card) return;
    var list = sectionsOf(card);
    if (!list.length) { closeJob(); return; }
    if (sectionIndex < 0) sectionIndex = 0;
    var target = list[sectionIndex];
    // Forward only steps in. A closed section opens first, because there is
    // nothing to walk inside it; folding itself belongs to space alone.
    if (!target.open) target.open = true;
    if (!paragraphsOf(target).length) return;
    inSection = true;
    paragraphIndex = -1;
    stepParagraph(1);
  }
  function toggleSectionFold(){
    var card = cards[jobIndex];
    if (!card) return;
    var list = sectionsOf(card);
    if (!list.length) return;
    if (sectionIndex < 0) sectionIndex = 0;
    var target = list[sectionIndex];
    if (!target.children.length) return;
    target.open = !target.open;
    if (!target.open) leaveSection();
    else focusSection(0);
  }
  function leaveSection(){
    inSection = false;
    paragraphIndex = -1;
    var card = cards[jobIndex];
    if (card) {
      Array.prototype.slice.call(card.querySelectorAll('.is-paragraph'))
        .forEach(function(p){ p.classList.remove('is-paragraph'); });
    }
  }
  jobs.forEach(function(button, i){
    button.addEventListener('click', function(){ openJob(i); });
    button.addEventListener('focus', function(){ selectJob(i, false); });
  });
  if (backButton) backButton.addEventListener('click', closeJob);
  document.addEventListener('keydown', function(event){
    if (document.body.dataset.tab !== 'tab.listings' || event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.target.closest && event.target.closest('input,textarea,select,.view-switcher')) return;
    if (generationDialog && generationDialog.open) return;
    var key = event.key;
    if ((key === 't' || key === 'T') && themes.length) {
      event.preventDefault(); event.stopImmediatePropagation();
      var themeAt = themes.indexOf(document.body.dataset.theme);
      applyTheme(themes[(themeAt + (key === 'T' ? -1 : 1) + themes.length) % themes.length], true);
      return;
    }
    if (detailView && !detailView.hidden) {
      if (key === 'h' || key === 'ArrowLeft' || key === 'Escape') {
        event.preventDefault(); event.stopImmediatePropagation();
        if (inSection) leaveSection(); else closeJob();
      } else if (key === 'j' || key === 'k') {
        event.preventDefault(); event.stopImmediatePropagation();
        if (inSection) stepParagraph(key === 'j' ? 1 : -1);
        else focusSection(key === 'j' ? 1 : -1);
      } else if (key === 'l' || key === 'Enter' || key === 'ArrowRight') {
        // A focused summary or button already owns Enter natively.
        if (event.target.closest &&
            event.target.closest('summary,button,a,input,textarea,select')) return;
        event.preventDefault(); event.stopImmediatePropagation();
        enterSection();
      } else if (key === ' ' || key === 'Spacebar') {
        // Space is the only fold, on both surfaces.
        if (event.target.closest &&
            event.target.closest('summary,button,a,input,textarea,select')) return;
        event.preventDefault(); event.stopImmediatePropagation();
        toggleSectionFold();
      } else if (key === 'ArrowDown' || key === 'ArrowUp') {
        event.preventDefault(); event.stopImmediatePropagation();
        window.scrollBy(0, key === 'ArrowDown' ? 70 : -70);
      }
      return;
    }
    if (key === 'j' || key === 'ArrowDown' || key === 'k' || key === 'ArrowUp') {
      event.preventDefault(); event.stopImmediatePropagation();
      moveJob((key === 'j' || key === 'ArrowDown') ? 1 : -1);
    } else if (key === 'l' || key === 'ArrowRight' || key === 'Enter') {
      if (visibleJobIndexes().length &&
          (event.target === document.body || event.target.classList.contains('job-row'))) {
        event.preventDefault(); event.stopImmediatePropagation(); openJob(jobIndex);
      }
    }
  }, true);
  selectJob(0, false);
  var rows = function(){ return Array.prototype.slice.call(document.querySelectorAll('#rows tr')); };
  var chips = Array.prototype.slice.call(document.querySelectorAll('[data-filter-status]'));
  var stageFilter = document.getElementById('stage-filter');
  function apply(){
    var on = chips.filter(function(c){return c.getAttribute('aria-pressed')==='true';})
                  .map(function(c){return c.dataset.filterStatus;});
    var selectedStage = stageFilter ? stageFilter.value : '';
    var q = (document.getElementById('q')||{}).value || '';
    q = q.trim().toLowerCase();
    var shown = 0;
    rows().forEach(function(row){
      var okStatus = !on.length || on.indexOf(row.dataset.status) >= 0;
      var okStage = !selectedStage || row.dataset.stage === selectedStage;
      var okScan = !document.getElementById('scan-only') ||
                   document.getElementById('scan-only').getAttribute('aria-pressed') !== 'true' ||
                   row.dataset.latestScan === 'true';
      var okQuery = !q || row.textContent.toLowerCase().indexOf(q) >= 0;
      var ok = okStatus && okStage && okScan && okQuery;
      row.hidden = !ok;
      if (ok) shown++;
    });
    var counter = document.getElementById('shown');
    if (counter) counter.textContent = shown;
    var empty = document.getElementById('empty-state');
    if (empty) empty.hidden = shown > 0;
    var listShown = 0;
    jobs.forEach(function(button){
      var ok = (!on.length || on.indexOf(button.dataset.status) >= 0) &&
        (!selectedStage || button.dataset.stage === selectedStage) &&
        (!scanOnly || scanOnly.getAttribute('aria-pressed') !== 'true' ||
         button.dataset.latestScan === 'true') &&
        (!q || (button.dataset.search || '').indexOf(q) >= 0);
      button.parentElement.hidden = !ok;
      if (ok) listShown++;
    });
    var jobShown = document.getElementById('job-shown');
    if (jobShown) jobShown.textContent = listShown;
    var jobEmpty = document.getElementById('job-empty');
    if (jobEmpty) jobEmpty.hidden = listShown > 0;
    if (listView && !listView.hidden) selectJob(jobIndex, false);
  }
  chips.forEach(function(chip){
    chip.addEventListener('click', function(){
      var wasOn = chip.getAttribute('aria-pressed') === 'true';
      chips.forEach(function(other){
        other.setAttribute('aria-pressed', other === chip && !wasOn ? 'true' : 'false');
      });
      apply();
    });
  });
  if (stageFilter) stageFilter.addEventListener('change', apply);
  var scanOnly = document.getElementById('scan-only');
  if (scanOnly) scanOnly.addEventListener('click', function(){
    scanOnly.setAttribute('aria-pressed', scanOnly.getAttribute('aria-pressed') === 'true' ? 'false' : 'true');
    apply();
  });
  var q = document.getElementById('q');
  if (q) q.addEventListener('input', apply);
  if (q) document.addEventListener('keydown', function(event){
    if (!(generationDialog && generationDialog.open) && event.key === '/' && document.activeElement !== q) { event.preventDefault(); q.focus(); }
  });
  var feedback = document.getElementById('feedback');
  var actionResult = document.getElementById('action-result');
  var actionMessage = document.getElementById('action-message');
  var actionCommand = document.getElementById('action-command');
  var actionCopy = document.getElementById('action-copy');
  var hasChat = !!(window.hermes && window.hermes.send);
  var liveMarks = (location.protocol === 'http:' &&
    (location.hostname === '127.0.0.1' || location.hostname === 'localhost') &&
    location.port === '8766');
  var actionsCaption = document.getElementById('job-actions-caption');
  if (actionsCaption) actionsCaption.textContent = liveMarks ?
    'Actions - changes stored through VPS writer' : hasChat ?
    'Actions - requests sent to chat' : 'Actions - written to the tracker';
  function showAction(message, command){
    if (actionResult) actionResult.hidden = false;
    if (actionMessage) actionMessage.textContent = message;
    if (actionCommand) { actionCommand.value = command || ''; actionCommand.hidden = !command; }
    if (actionCopy) actionCopy.hidden = !command;
    if (actionResult) actionResult.scrollIntoView({block:'nearest'});
    else if (feedback) feedback.textContent = message + (command ? ' ' + command : '');
  }
  function packetView(){ return document.getElementById('packet-view'); }
  function showPacketText(text){
    var box = packetView();
    if (box) { box.hidden = !text; box.textContent = text || ''; }
  }
  function served(live, command){
    if (!live) { showAction('This needs the served dashboard. Run: python3 -B tools/dashboard.py serve --port 8766', command || ''); return false; }
    return true;
  }
  function viewingJob(id){
    return !!(detailView && !detailView.closest('[hidden]') && jobs[jobIndex] &&
              jobs[jobIndex].dataset.id === id);
  }
  var generationId = '';
  var generationNotices = new Map();
  function syncGenerationButton(){
    var button = document.querySelector('[data-action="generate_application"]');
    if (button) button.disabled = !!generationId && viewingJob(generationId);
  }
  function paintGeneration(id, busy){
    var index = jobs.findIndex(function(job){ return job.dataset.id === id; });
    [jobs[index], cards[index]].forEach(function(root){
      var marker = root && root.querySelector('.generation-indicator');
      if (marker) marker.hidden = !busy;
    });
    syncGenerationButton();
  }
  function reportGeneration(id, message){
    if (viewingJob(id)) showAction(message, '');
    else generationNotices.set(id, message);
  }
  function showPacket(id, statusMessage){
    if (!served(liveMarks, 'python3 -B tools/dashboard.py packet ' + id)) return;
    showAction('Reading the application packet...', '');
    fetch('/api/packet?id=' + encodeURIComponent(id))
      .then(function(response){ return response.json().then(function(data){
        if (!response.ok) throw Error(data.error || 'No packet for this listing yet.');
        return data;
      }); })
      .then(function(data){
        if (!viewingJob(id)) return;
        showPacketText(data.text || '');
        showAction(data.text ? (statusMessage || 'Application packet') : 'No packet for this listing yet.', '');
      })
      .catch(function(error){
        if (viewingJob(id)) { showPacketText(''); showAction(error.message, ''); }
      });
  }
  var generationDialog = document.getElementById('generation-dialog');
  var generationChoice = null;
  if (generationDialog) {
    generationDialog.querySelector('form').addEventListener('submit', function(event){
      event.preventDefault();
      var choice = generationChoice;
      var model = generationDialog.querySelector('select').value;
      generationChoice = null;
      generationDialog.close();
      if (choice && event.submitter && event.submitter.value === 'run' && viewingJob(choice.id)) {
        generatePacket(choice.id, choice.force, model);
      }
    });
    generationDialog.addEventListener('cancel', function(){ generationChoice = null; });
  }
  function chooseGenerationModel(id, force, model){
    if (!served(liveMarks, 'python3 -B tools/dashboard.py generate ' + id)) return;
    if (!generationDialog || generationDialog.open) return;
    generationChoice = {id:id, force:force};
    generationDialog.returnValue = '';
    generationDialog.querySelector('h2').textContent = force ? 'Regenerate cover letter' : 'Generate cover letter';
    generationDialog.querySelector('p').hidden = !force;
    generationDialog.querySelector('[value="run"]').textContent = force ? 'Regenerate' : 'Generate';
    var select = generationDialog.querySelector('select');
    select.value = model || select.querySelector('[selected]').value;
    generationDialog.showModal();
  }
  function postPacket(id, force, model){
    return fetch('/api/generate', {method:'POST', credentials:'same-origin',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({id:id, force:force, generation_model:model})})
      .then(function(response){ return response.json().then(function(data){
        if (response.status === 409 && data.needs_confirm) return data;
        if (!response.ok) throw Error(data.error || 'The generator failed.');
        if (data.ok !== true || data.packet_written !== true || data.id !== id)
          throw Error('Server did not confirm a draft for this listing.');
        return data;
      }); });
  }
  function undoStage(id, button){
    showAction('Stepping back to the stage this listing held before...', '');
    if (button) button.disabled = true;
    fetch('/api/unstage', {method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({id:id})})
      .then(function(response){ return response.json().then(function(data){
        if (!response.ok) throw Error(data.error || 'Server rejected the undo.');
        return data; }); })
      .then(function(data){
        if (data.ok !== true || !data.stage)
          throw Error('Server did not confirm the restored stage.');
        showAction('Back at ' + data.stage + '. Reloading the verified feed...', '');
        location.reload();
      })
      .catch(function(error){ showAction('Undo failed: ' + error.message, ''); })
      .finally(function(){ if (button) button.disabled = false; });
  }
  function stageOf(id){
    var index = jobs.findIndex(function(item){ return item.dataset.id === id; });
    return index < 0 ? '' : String(jobs[index].dataset.stage || '');
  }
  function generatePacket(id, force, model){
    if (generationId) { showAction('A cover letter is already being generated.', ''); return; }
    generationId = id;
    generationNotices.delete(id);
    paintGeneration(id, true);
    postPacket(id, force, model).then(function(data){
      if (data.needs_confirm) {
        if (force) throw Error('The server refused to replace the existing draft.');
        if (viewingJob(id)) chooseGenerationModel(id, true, model);
        else reportGeneration(id, 'A cover letter already exists. Generate again to confirm replacement.');
        return;
      }
      if (data.stage_updated && data.stored_stage)
        updateStageView(id, data.stored_stage, data.stored_status);
      var message = data.stage_updated ?
        'Packet written. Stage: ' + (data.stage_label || 'Generating application') + '.' :
        'Packet written, but the stage update was not confirmed: ' +
          (data.stage_error || 'tracker read-back did not match.');
      if (viewingJob(id)) showPacket(id, message);
      else reportGeneration(id, message);
    }).catch(function(error){ reportGeneration(id, 'Generate failed: ' + error.message); })
      .finally(function(){ generationId = ''; paintGeneration(id, false); });
  }
  function notePacket(id){
    if (!served(liveMarks, 'python3 -B tools/dashboard.py note ' + id)) return;
    var text = window.prompt('Note for this application (feedback, what was said, what is next):');
    if (!text || !text.trim()) { showAction('Note cancelled.', ''); return; }
    showAction('Saving the note...', '');
    fetch('/api/note', {method:'POST', credentials:'same-origin',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({id:id, note:text.trim()})})
      .then(function(response){ return response.json().then(function(data){
        if (!response.ok) throw Error(data.error || 'The tracker rejected the note.');
        return data;
      }); })
      .then(function(data){ showAction('Note saved: ' + (data.note || text.trim()), ''); })
      .catch(function(error){ showAction('Note failed: ' + error.message, ''); });
  }
  var bookmarkButton = document.querySelector('[data-action="toggle_bookmark"]');
  var bookmarkFeedback = document.getElementById('bookmark-feedback');
  function refreshBookmark(){
    jobs.forEach(function(job, index){
      var saved = job.dataset.bookmarked === 'true';
      var marker = job.querySelector('.job-row-bookmark');
      if (marker) marker.hidden = !saved;
      var detailMarker = cards[index] && cards[index].querySelector('.bookmark-state');
      if (detailMarker) detailMarker.hidden = !saved;
    });
    if (!bookmarkButton || !jobs[jobIndex]) return;
    var saved = jobs[jobIndex].dataset.bookmarked === 'true';
    bookmarkButton.setAttribute('aria-pressed', saved ? 'true' : 'false');
    var label = bookmarkButton.querySelector('.act-label');
    if (label) label.textContent = saved ? 'Remove bookmark' : 'Bookmark';
    bookmarkButton.title = saved ? 'Bookmarked in the tracker' : 'Not bookmarked in the tracker';
  }
  function updateBookmarkView(id, saved){
    var index = jobs.findIndex(function(job){ return job.dataset.id === id; });
    if (index < 0) return;
    jobs[index].dataset.bookmarked = saved ? 'true' : 'false';
    refreshBookmark();
  }
  refreshBookmark();
  if (actionCopy) actionCopy.addEventListener('click', function(){
    if (!actionCommand || !actionCommand.value) return;
    actionCommand.focus(); actionCommand.select();
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(actionCommand.value).then(function(){
        if (actionMessage) actionMessage.textContent = 'Copied. Run this on your Mac to update the VPS tracker.';
      }).catch(function(){
        if (actionMessage) actionMessage.textContent = 'Clipboard blocked. The command is selected; copy it manually.';
      });
    } else if (actionMessage) {
      actionMessage.textContent = 'Clipboard unavailable on HTTP. The command is selected; copy it manually.';
    }
  });
  Array.prototype.slice.call(document.querySelectorAll('[data-action]')).forEach(function(button){
    if (button.dataset.localStore) return;
    if (button.dataset.cli && !hasChat && !liveMarks) {
      var label = button.querySelector('.act-label');
      button.setAttribute('aria-label', 'Show command for ' + (label ? label.textContent : button.textContent));
      button.title = 'Read-only: shows a command, does not change the tracker';
    }
    button.addEventListener('click', function(event){
      if (button.dataset.sourceLink) {
        if (!button.href || button.getAttribute('href') === '#') {
          event.preventDefault();
          showAction('No valid source URL for this listing.', ''); return;
        }
        showAction('Opening the official AMS address. If AMS displays maintenance or an expired listing, its page is unavailable; this app cannot repair the source site.', '');
        return;
      }
      if (button.dataset.action === 'stage_next' && stageAtEnd()) {
        var panel = document.getElementById('outcome-panel');
        if (panel) {
          panel.hidden = false;
          var caption = document.getElementById('outcome-caption');
          if (caption) caption.textContent = 'Outcome after ' +
            (stageFlow[stageFlow.length - 1] || 'contact') +
            ': pick one, the pipeline ends here.';
          panel.scrollIntoView({block:'nearest'});
        }
        showAction('The waiting stage is over: choose interview, rejected, or success.', '');
        return;
      }
      var template = button.dataset.template || '';
      var cli = button.dataset.cli || '';
      if (template && hasChat) {
        window.hermes.send(template);
        showAction('Sent to the chat frame for the tracker writer. No status is assumed until verified.', '');
        return;
      }
      if (button.dataset.action === 'toggle_bookmark') {
        if (liveMarks) {
          var selectedJob = jobs[jobIndex];
          if (!selectedJob || !selectedJob.dataset.id) {
            showAction('Select a listing before changing its bookmark.', ''); return;
          }
          var bookmarkId = selectedJob.dataset.id;
          showAction('Saving bookmark through the tracker...', '');
          button.disabled = true;
          fetch('/api/bookmark', {method:'POST', credentials:'same-origin',
              headers:{'Content-Type':'application/json'},
              body:JSON.stringify({id:bookmarkId})})
            .then(function(response){ return response.json().then(function(data){
              if (!response.ok) throw Error(data.error || 'The tracker rejected the bookmark.');
              return data;
            }); })
            .then(function(data){
              if (data.ok !== true || data.id !== bookmarkId ||
                  typeof data.bookmarked !== 'boolean')
                throw Error('Tracker did not confirm the saved bookmark state.');
              updateBookmarkView(bookmarkId, data.bookmarked);
              var message = data.bookmarked ? 'Bookmark saved in the tracker.' :
                'Bookmark removed from the tracker.';
              if (bookmarkFeedback) bookmarkFeedback.textContent = message;
              showAction(message, '');
            })
            .catch(function(error){ showAction('Bookmark update failed: ' + error.message, ''); })
            .finally(function(){ button.disabled = false; });
          return;
        }
        if (cli) {
          var remoteBookmark = cli;
          showAction('Read-only page. Run this command from your private Jobhunter checkout; clicking here did not update the tracker.', remoteBookmark);
        } else showAction('This bookmark action is unavailable in the read-only public page.', '');
        return;
      }
      if (cli) {
        var generate = /^python3 -B tools\\/dashboard\\.py generate ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})( --force)?$/i.exec(cli);
        if (generate) {
          if (generationId) { showAction('A cover letter is already being generated.', ''); return; }
          chooseGenerationModel(generate[1], !!generate[2]);
          return;
        }
        var noteCmd = /^python3 -B tools\\/dashboard\\.py note ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/i.exec(cli);
        if (noteCmd) { notePacket(noteCmd[1]); return; }
        var packetCmd = /^python3 -B tools\\/dashboard\\.py packet ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/i.exec(cli);
        if (packetCmd) { showPacket(packetCmd[1]); return; }
        var mark = /^python3 -B tools\\/dashboard\\.py mark ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}) (applied|interview|blocked|skipped|nudged|rejected|success|unclear|unread|read|generated|checked|sent|confirmed|waiting|contact|preparation|awaiting)$/i.exec(cli);
        if (!mark) { showAction('Could not prepare a safe mark command for this listing.', ''); return; }
        // The stage key pressed twice steps back: a listing already sitting in
        // this stage is un-blocked, un-failed or un-sent rather than stamped
        // with the same stage a second time.
        if (liveMarks && stageOf(mark[1]) === mark[2].toLowerCase()) {
          undoStage(mark[1], button);
          return;
        }
        if (liveMarks) {
          showAction('Updating the tracker through the VPS writer...', '');
          button.disabled = true;
          fetch('/api/mark', {method:'POST', credentials:'same-origin',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({id:mark[1], status:mark[2]})})
            .then(function(response){ return response.json().then(function(data){
              if (!response.ok) throw Error(data.error || 'Server rejected the update.');
              return data;
            }); })
            .then(function(data){
              var stored = data.stored_stage || data.stored_status;
              if (data.ok !== true || data.id !== mark[1] || stored !== mark[2])
                throw Error('Server did not confirm the stored stage (got ' +
                  (stored || 'nothing') + ').');
              showAction('Stored as ' + mark[2] + '. Reloading the verified feed...', '');
              location.reload();
            }).catch(function(error){ showAction('Update failed: ' + error.message, ''); })
            .finally(function(){ button.disabled = false; });
          return;
        }
        var remote = cli;
        showAction('Read-only page. Run this command from your private Jobhunter checkout; clicking here did not update the tracker.', remote);
        return;
      }
      showAction('This action is unavailable in the read-only public page.', '');
    });
  });
  // The public page never includes modules/profile/profile.json. Only a local
  // draft and an explicitly downloaded proposal can leave this browser.
  var skillRows = document.getElementById('skill-rows');
  var profileFeedback = document.getElementById('profile-feedback');
  var draftKey = 'jobui.profile.skills.v1';
  function profileStatus(text){ if (profileFeedback) profileFeedback.textContent = text; }
  function addSkill(name, source){
    if (!skillRows) return;
    var row = document.createElement('div'); row.className = 'skill-entry';
    function field(labelText, value, cls, max){
      var label = document.createElement('label'); label.textContent = labelText;
      var input = document.createElement('input'); input.type = 'text'; input.maxLength = max;
      input.className = cls; input.value = value || '';
      label.appendChild(input); row.appendChild(label); return input;
    }
    var input = field('Skill', name, 'skill-name', 120);
    field('Source or evidence', source, 'skill-source', 240);
    var remove = document.createElement('button'); remove.type = 'button';
    remove.textContent = 'Remove'; remove.setAttribute('aria-label', 'Remove skill');
    remove.addEventListener('click', function(){ row.remove(); profileStatus('Skill removed from this draft. Save to keep the change.'); });
    row.appendChild(remove); skillRows.appendChild(row); return input;
  }
  function proposal(){
    var skills = Array.prototype.slice.call(skillRows.querySelectorAll('.skill-entry')).map(function(row){
      return {name:row.querySelector('.skill-name').value.trim(),
              source:row.querySelector('.skill-source').value.trim()};
    });
    if (skills.length > 100) throw Error('Limit: 100 skills.');
    if (!skills.length) throw Error('Add at least one skill before saving or exporting.');
    skills.forEach(function(s, i){
      if (!s.name || !s.source) throw Error('Skill ' + (i + 1) + ' needs a name and a source.');
    });
    if (new Set(skills.map(function(s){ return s.name.toLocaleLowerCase(); })).size !== skills.length)
      throw Error('Remove duplicate skill names.');
    return {schema_version:1, kind:'profile-skills-proposal', skills:skills};
  }
  function importSkills(data){
    var list = data && (Array.isArray(data.skills) ? data.skills : data.strong_signals);
    if (!Array.isArray(list) || list.length > 100) throw Error('Expected a profile.json with strong_signals, or a skills proposal (max 100).');
    var normalized = list.map(function(item){
      if (typeof item === 'string') return {name:item, source:''};
      if (item && typeof item.name === 'string' && typeof item.source === 'string') return item;
      throw Error('Each skill must be text or a name/source pair.');
    });
    normalized.forEach(function(s){
      if (s.name.length > 120 || s.source.length > 240) throw Error('A skill or source exceeds the field limit.');
    });
    skillRows.replaceChildren();
    normalized.forEach(function(s){ addSkill(s.name, s.source); });
    profileStatus('Imported ' + normalized.length + ' skills into an unsaved local draft. Add a source for every skill before saving.');
  }
  if (skillRows) {
    var privateEditorAllowed = location.protocol === 'file:' || location.protocol === 'https:' ||
      location.hostname === 'localhost' || location.hostname === '127.0.0.1';
    if (!privateEditorAllowed) {
      ['profile-import', 'skill-add', 'skill-save', 'skill-export'].forEach(function(id){
        document.getElementById(id).disabled = true;
      });
      profileStatus('Profile editing is disabled on public HTTP. Use jobui web on your Mac to edit a private local draft.');
    } else {
    try {
      var saved = localStorage.getItem(draftKey);
      if (saved) importSkills(JSON.parse(saved));
    } catch (error) { profileStatus('Could not load local draft: ' + error.message); }
    document.getElementById('skill-add').addEventListener('click', function(){
      var input = addSkill('', ''); if (input) input.focus();
    });
    document.getElementById('skill-save').addEventListener('click', function(){
      try { var data = proposal(); localStorage.setItem(draftKey, JSON.stringify(data));
        profileStatus('Saved ' + data.skills.length + ' skills in this browser only. Canonical profile unchanged.');
      } catch (error) { profileStatus('Not saved: ' + error.message); }
    });
    document.getElementById('skill-export').addEventListener('click', function(){
      try {
        var data = proposal();
        var blob = new Blob([JSON.stringify(data, null, 2) + '\\n'], {type:'application/json'});
        var url = URL.createObjectURL(blob); var link = document.createElement('a');
        link.href = url; link.download = 'profile-skills-proposal.json'; link.click();
        setTimeout(function(){ URL.revokeObjectURL(url); }, 1000);
        profileStatus('Downloaded a proposal. The canonical profile was not changed.');
      } catch (error) { profileStatus('Not exported: ' + error.message); }
    });
    document.getElementById('profile-import').addEventListener('change', function(event){
      var file = event.target.files && event.target.files[0]; if (!file) return;
      if (file.size > 262144) { profileStatus('Import limit: 256 KiB.'); return; }
      file.text().then(function(text){ importSkills(JSON.parse(text)); })
        .catch(function(error){ profileStatus('Import failed: ' + error.message); });
      event.target.value = '';
    });
    }
  }
  Array.prototype.slice.call(document.querySelectorAll('[data-shortcut]')).forEach(function(el){
    var combo = el.dataset.shortcut.split('+');
    var key = combo[combo.length - 1].toLowerCase();
    var alt = combo[0].toLowerCase() === 'alt';
    document.addEventListener('keydown', function(event){
      if (alt && !event.altKey) return;
      if (event.key.toLowerCase() !== key) return;
      event.preventDefault();
      el.click();
      el.focus();
    });
  });
  // views
  var tabs = Array.prototype.slice.call(document.querySelectorAll('.tab'));
  var cursor = -1;
  function visibleRows(){ return rows().filter(function(row){ return !row.hidden; }); }
  function paint(){
    var list = visibleRows();
    rows().forEach(function(row){ row.classList.remove('is-cursor', 'is-focus'); });
    var detail = document.getElementById('row-detail');
    if (!list.length) {
      cursor = -1;
      if (detail) { detail.textContent = ''; }
      return;
    }
    if (cursor < 0) {
      var seed = rows().filter(function(row){ return row.getAttribute('aria-current') === 'true'; })[0];
      cursor = seed ? Math.max(0, list.indexOf(seed)) : 0;
    }
    cursor = Math.max(0, Math.min(list.length - 1, cursor));
    var active = list[cursor];
    active.classList.add('is-cursor');
    if (detail) { detail.textContent = active.dataset.detail ? 'More: ' + active.dataset.detail : ''; }
    retarget(active);
  }
  function retarget(row){
    var id = row.dataset.id || '';
    var url = row.dataset.href || '';
    currentStage = row.dataset.stage || '';
    var outcome = document.getElementById('outcome-panel');
    if (outcome) outcome.hidden = true;
    var source = document.querySelector('[data-source-link]');
    if (source) {
      try {
        var parsed = new URL(url);
        source.href = (parsed.protocol === 'https:' || parsed.protocol === 'http:') &&
                      parsed.hostname && !parsed.username && !parsed.password ? url : '#';
      } catch (error) { source.href = '#'; }
    }
    Array.prototype.slice.call(document.querySelectorAll('[data-cli-tmpl],[data-chat-tmpl]')).forEach(function(button){
      function fill(text){ return text.split('{listing_id}').join(id)
                                      .split('{listing_url}').join(url)
                                      .split('{stage_next}').join(stageStep(1))
                                      .split('{stage_prev}').join(stageStep(-1)); }
      if (button.dataset.cliTmpl) button.dataset.cli = fill(button.dataset.cliTmpl);
      if (button.dataset.chatTmpl) button.dataset.template = fill(button.dataset.chatTmpl);
    });
    var line = document.getElementById('focus-line');
    if (line) {
      var bits = [row.dataset.title, row.dataset.company].filter(Boolean);
      while (line.firstChild) line.removeChild(line.firstChild);
      var strong = document.createElement('b');
      strong.textContent = 'Focused: ';
      line.appendChild(strong);
      line.appendChild(document.createTextNode(bits.join(' @ ')));
    }
  }
  function updateStageView(id, stage, status){
    var index = jobs.findIndex(function(button){ return button.dataset.id === id; });
    if (index < 0) return;
    var button = jobs[index];
    button.dataset.stage = stage;
    if (status) button.dataset.status = status;
    var meta = stageMeta[stage] || {};
    var label = meta.label || stage;
    var position = stageFlow.indexOf(stage);
    if (position < 0) position = Math.max(0, stageFlow.length - 1);
    var visibleFlow = stageFlow.filter(function(key){
      return !stageMeta[key] || stageMeta[key].strip !== false;
    });
    var dots = visibleFlow.map(function(key){
      var at = stageFlow.indexOf(key);
      return at === position ? '◉' : at < position ? '●' : '○';
    }).join('');
    var chip = cards[index] && cards[index].querySelector('.stage-chip');
    if (chip){
      var role = meta.role || 'accent';
      var tone = role === 'accent' ? 'primary' : role === 'neutral' ? 'muted' : role;
      chip.className = 'stage-chip is-' + tone;
      chip.title = label + ', step ' + (position + 1) + ' of ' + stageFlow.length;
      while (chip.firstChild) chip.removeChild(chip.firstChild);
      var marks = document.createElement('span');
      marks.setAttribute('aria-hidden', 'true'); marks.textContent = dots;
      chip.appendChild(marks);
      chip.appendChild(document.createTextNode(' ' + label));
      var sr = document.createElement('span');
      sr.className = 'sr-only';
      sr.textContent = ': ' + (position + 1) + ' of ' + stageFlow.length + ' steps';
      chip.appendChild(sr);
    }
    rows().forEach(function(row){
      if (row.dataset.id !== id) return;
      row.dataset.stage = stage;
      if (status) row.dataset.status = status;
    });
    if (index === jobIndex && detailView && !detailView.hidden){
      var title = button.querySelector('.job-row-title');
      retarget({dataset:{id:id, href:button.dataset.href || '', stage:stage,
                         title:title ? title.textContent.trim() : '', company:''}});
    }
    apply();
  }
  function selectTab(id){
    tabs.forEach(function(tab){
      var on = tab.dataset.tab === id;
      tab.setAttribute('aria-selected', on ? 'true' : 'false');
      tab.tabIndex = on ? 0 : -1;
    });
    Array.prototype.slice.call(document.querySelectorAll('.tabpanel')).forEach(function(panel){
      panel.hidden = panel.dataset.tab !== id;
    });
    document.body.dataset.tab = id;
    cursor = -1; paint();
  }
  tabs = tabs.filter(function(tab, index){
    if (!document.querySelector('.tabpanel[data-tab="' + tab.dataset.tab + '"]')) {
      tab.parentNode.removeChild(tab);
      return false;
    }
    tab.addEventListener('click', function(){
      closeJob();
      selectTab(tab.dataset.tab);
      var switcher = document.querySelector('.view-switcher');
      if (switcher) switcher.open = false;
    });
    tab.addEventListener('keydown', function(event){
      if (event.key !== 'ArrowRight' && event.key !== 'ArrowLeft') return;
      event.preventDefault();
      var step = event.key === 'ArrowRight' ? 1 : -1;
      var next = tabs[(index + step + tabs.length) % tabs.length];
      if (next) { next.focus(); selectTab(next.dataset.tab); }
    });
    return true;
  });
  document.addEventListener('keydown', function(event){
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.target && event.target.matches && event.target.matches('input,textarea,select')) return;
    var key = event.key;
    if (key === 'j') { event.preventDefault(); cursor += 1; paint(); }
    else if (key === 'k') { event.preventDefault(); cursor -= 1; paint(); }
    else if (key === 'g') { event.preventDefault(); cursor = 0; paint(); }
    else if (key === 'G') { event.preventDefault(); cursor = 1e6; paint(); }
    else if (key === '[' || key === ']') {
      event.preventDefault();
      var active = tabs.filter(function(tab){ return tab.getAttribute('aria-selected') === 'true'; })[0];
      var at = tabs.indexOf(active);
      var next = tabs[(at + (key === ']' ? 1 : -1) + tabs.length) % tabs.length];
      if (next) selectTab(next.dataset.tab);
    }

  });
  var activeTab = document.querySelector('.tab[aria-selected="true"]');
  document.body.dataset.tab = activeTab ? activeTab.dataset.tab : '';
  apply();
  paint();
})();
"""


def render_gui(spec, ctx, surface="gui"):
    proj = Projection(spec, ctx, surface)
    app = spec.get("app") or {}
    counts = ctx.get("facts") or {}
    meters = []
    meter_nodes = []
    seen = set()
    header_source = []

    def find_group(nodes, node_id):
        for node in nodes:
            if node.get("id") == node_id:
                return [node]
            found = find_group(node.get("children") or [], node_id)
            if found:
                return found
        return []

    header_source = find_group(proj.tree, "overview.counts") or proj.tree

    def collect_meters(nodes):
        for node in nodes:
            if node.get("kind") == "meter" and node["id"] not in seen:
                seen.add(node["id"])
                value = bind_value(spec, ctx, surface, node)
                text = value_text(spec, ctx, surface, value, node.get("fallback", "-"))
                meter_nodes.append(node)
                meters.append("<div><dt>%s</dt><dd>%s</dd></div>"
                              % (html.escape(str(node.get("label"))), html.escape(str(text))))
            collect_meters(node.get("children") or [])

    collect_meters(header_source)
    nav = _gui_nav(proj)
    dup_ids = set()

    def collect_scan_fields(nodes):
        for node in nodes:
            if node.get("bind") in ("scan.line", "scan.scraped_at"):
                dup_ids.add(node["id"])
            collect_scan_fields(node.get("children") or [])
    collect_scan_fields(proj.tree)
    main = _gui_main(spec, ctx, proj, skip_ids=dup_ids)
    foot = ("Listings: j/k move, l opens, h goes back; [ ] switches views. "
            "Detail mark buttons show commands. Structure: %s (v%s). Renderer: "
            "tools/ui.py gui. Counts come from the normalized feed, never hardcoded."
            % (html.escape(str(app.get("source_of_truth", "ui/interface.json"))),
               html.escape(str(spec.get("version", "")))))
    return GUI_TEMPLATE % {
        "lang": html.escape(str(app.get("language", "en"))),
        "title": html.escape(str(app.get("title", "UI"))),
        "subtitle": html.escape(str(app.get("subtitle", ""))),
        "scan_line": html.escape(str(counts.get("scan.line", ""))),
        "meters": "".join(meters),
        "nav": nav,
        "tabs": _gui_tabs(spec),
        "main": main,
        "foot": foot,
        "css": GUI_CSS + gui_theme_css(),
        "js": GUI_JS.replace("/*STAGE_FLOW*/[]", json.dumps(
            [entry.get("key") for entry in stage_flow(spec)])).replace(
                "/*STAGE_META*/{}", json.dumps({
                    entry.get("key"): {"label": entry.get("label") or entry.get("key"),
                                      "role": entry.get("role") or "accent",
                                      "strip": entry.get("strip")}
                    for entry in _stage_entries(spec)})),
        "theme": html.escape(saved_theme()),
        "themes": gui_theme_options(saved_theme()),
    }


def node_label(spec, node):
    """Labels live on the node; an action node may borrow the action's label."""
    if node.get("label"):
        return str(node["label"])
    ref = node.get("action")
    action = (spec.get("actions") or {}).get(ref) or {}
    if action.get("label"):
        return str(action["label"])
    raw = str(node.get("id") or "")
    tail = raw.rsplit(".", 1)[-1]
    return tail.replace("_", " ").replace("-", " ").strip().capitalize()[0:1].upper() + \
        tail.replace("_", " ").replace("-", " ").strip().capitalize()[1:]


def _find_node(spec, node_id, nodes=None):
    nodes = nodes if nodes is not None else spec.get("tree") or []
    for node in nodes:
        if node.get("id") == node_id:
            return node
        found = _find_node(spec, node_id, node.get("children") or [])
        if found:
            return found
    return {}


def _gui_nav(proj):
    """Navigation lists destinations only: views and actions, never filters or fields."""
    parts = []

    def visible(node):
        if node.get("kind") in ("view", "action"):
            return True
        return any(visible(child) for child in node.get("children") or [])

    def walk(nodes, depth):
        kept = [node for node in nodes if visible(node)]
        if depth == 1:
            parts.append("<ul>")
        for node in kept:
            label = html.escape(node_label(proj.spec, node))
            children = [child for child in node.get("children") or [] if visible(child)]
            if children:
                parts.append("<li><details open><summary>%s</summary><ul>" % label)
                walk(children, depth + 1)
                parts.append("</ul></details></li>")
            else:
                slug = str(node.get("id", "")).replace(".", "-")
                key = (node.get("keys") or {}).get(proj.surface, "")
                parts.append('<li><a href="#%s"%s>%s</a></li>'
                             % (html.escape(slug),
                                (' data-shortcut="%s" title="%s"' % (html.escape(key), html.escape(key))) if key else "",
                                label))
        if depth == 1:
            parts.append("</ul>")

    walk(proj.tree, 1)
    return "".join(parts)


def _gui_tabs(spec):
    tabs = [tab for tab in (spec.get("tabs") or []) if isinstance(tab, dict)]
    out = []
    for index, tab in enumerate(tabs):
        slug = str(tab.get("id", "")).replace(".", "-")
        out.append('<button class="tab" type="button" role="tab" id="%s-tab" data-tab="%s" '
                   'aria-selected="%s" aria-controls="panel-%s" tabindex="%s">%s</button>'
                   % (slug, html.escape(str(tab.get("id", ""))),
                      "true" if index == 0 else "false", slug,
                      "0" if index == 0 else "-1",
                      html.escape(str(tab.get("label") or tab.get("id")))))
    return "".join(out)


def _gui_main(spec, ctx, proj, skip_ids=None):
    tabs = [tab for tab in (spec.get("tabs") or []) if isinstance(tab, dict)]
    if not tabs:
        return _render_nodes(spec, ctx, proj, proj.tree, skip_ids)
    panels = []
    first = True
    for tab in tabs:
        root = _find_projected(proj.tree, tab.get("root"))
        if root is None:
            continue
        nodes = root.get("children") or [root]
        if tab.get("id") == "tab.listings":
            item = _find_projected(nodes, "item.listing")
            body = _gui_compact_list(spec, ctx, proj, item) if item else ""
        elif tab.get("id") == "tab.profile":
            body = _gui_profile(root)
        else:
            body = _render_nodes(spec, ctx, proj, nodes, skip_ids)
        if not body:
            continue
        slug = str(tab.get("id", "")).replace(".", "-")
        panels.append('<section class="tabpanel" id="panel-%s" role="tabpanel" '
                      'aria-labelledby="%s-tab" data-tab="%s"%s>%s</section>'
                      % (slug, slug, html.escape(str(tab.get("id", ""))),
                         "" if first else " hidden", body))
        first = False
    return "".join(panels)


def _gui_profile(root):
    """Edit browser-local drafts; private profile data never enters a public build."""
    skills = _find_projected(root.get("children") or [], "profile.skills")
    if not skills:
        return ""
    return ('<section class="profile-editor" aria-labelledby="profile-title">'
            '<h2 id="profile-title">%s</h2>'
            '<div role="tablist" aria-label="Profile sections">'
            '<button role="tab" type="button" aria-selected="true" '
            'aria-controls="profile-skills" id="profile-skills-tab">%s</button></div>'
            '<section id="profile-skills" role="tabpanel" aria-labelledby="profile-skills-tab">'
            '<p>Skills are a local draft in this browser, not changes to the private profile. '
            'Import your own profile.json or add a skill. Each claim needs a source. '
            'Export a proposal for review before it enters the canonical profile. '
            'Public HTTP disables this editor; open jobui web locally on your Mac.</p>'
            '<label class="file-label" for="profile-import">Import profile.json or a skills proposal</label>'
            '<input id="profile-import" type="file" accept="application/json,.json">'
            '<div id="skill-rows"></div>'
            '<div class="skill-controls">'
            '<button id="skill-add" type="button">Add skill</button>'
            '<button id="skill-save" type="button">Save draft in this browser</button>'
            '<button id="skill-export" type="button">Download proposal</button></div>'
            '<p id="profile-feedback" class="status" role="status" aria-live="polite"></p>'
            '</section></section>'
            % (html.escape(str(root.get("label") or "Profile")),
               html.escape(str(skills.get("label") or "Skills"))))


def _score_bubble(score, text):
    """One accessible score chip for the list and detail; missing stays unknown."""
    valid = (isinstance(score, (int, float)) and not isinstance(score, bool)
             and math.isfinite(score))
    label = "Score %s" % text if valid else "Score unknown"
    visible = text if valid else "?"
    # Score 0 is red, score 10 is green; clamp for out-of-range source values.
    hue = round(125 * max(0.0, min(10.0, score)) / 10) if valid else None
    style = ' style="--score-color:hsl(%d 80%% 70%%)"' % hue if valid else ""
    return ('<span class="score-bubble%s"%s role="img" aria-label="%s" '
            'title="%s">%s</span>' %
            ("" if valid else " is-unknown", style, html.escape(label, quote=True),
             html.escape(label, quote=True), html.escape(visible)))

def _known_monthly_pay(pay):
    """Format only a real explicit-monthly feed value, without inferring pay."""
    if (not isinstance(pay, (int, float)) or isinstance(pay, bool)
            or not math.isfinite(pay) or pay <= 0):
        return ""
    amount = format(pay, ',.0f') if pay == int(pay) else format(pay, ',.2f')
    return "Listed EUR %s gross/month" % amount


def _known_yearly_pay(pay):
    """Format only a real explicit-yearly feed value, without inferring pay."""
    if (not isinstance(pay, (int, float)) or isinstance(pay, bool)
            or not math.isfinite(pay) or pay <= 0):
        return ""
    amount = format(pay, ',.0f') if pay == int(pay) else format(pay, ',.2f')
    return "EUR %s gross/year" % amount


def _amount_text(pay):
    """Bare amount for a card cell: 3800.0 -> '3,800'."""
    if (not isinstance(pay, (int, float)) or isinstance(pay, bool)
            or not math.isfinite(pay) or pay <= 0):
        return ""
    return format(pay, ',.0f') if pay == int(pay) else format(pay, ',.2f')


def _listing_pay_text(row):
    """One pay cell: the ad's own monthly figure, else yearly / 14 for Austria.

    A yearly figure is only ever shown as a calculation and always says so,
    because the ad named a year, not a month. `feed.PAYMENTS_PER_YEAR_AT` is 14
    (twelve salary months plus holiday and Christmas pay).
    """
    row = row or {}
    stated = _amount_text(row.get("pay_monthly_min"))
    if stated:
        return "EUR %s/mo" % stated
    calc = _amount_text(row.get("pay_monthly_calc"))
    yearly = _amount_text(row.get("pay_yearly_min"))
    if calc and yearly:
        # Calculated, so it says so: the ad named a year, and 14 payments a year
        # is the Austrian rule the figure comes from.
        return "≈ EUR %s/mo (yr/14)" % calc
    if yearly:
        return "EUR %s gross/year" % yearly
    return ""


def _listing_pay_cell(row):
    """Detail cell: the monthly figure first, with the yearly basis beside it."""
    row = row or {}
    stated = _amount_text(row.get("pay_monthly_min"))
    calc = _amount_text(row.get("pay_monthly_calc"))
    yearly = _amount_text(row.get("pay_yearly_min"))
    if stated:
        return "EUR %s gross/month" % stated
    if calc and yearly:
        return ("EUR %s gross/month (calculated from EUR %s gross/year / 14)"
                % (calc, yearly))
    if yearly:
        return "EUR %s gross/year" % yearly
    return "not stated"

def _city_name(location):
    """City key from AMS location text: drop postal codes and district suffixes."""
    text = _plain_hyphens(str(location or "")).strip()
    if not text:
        return ""
    parts = [part.strip() for part in text.split(",")]
    parts = [part for part in parts if part and not part.isdigit()]
    return parts[0] if parts else ""


# A neutral, deterministic palette: city identity comes from the listing, not
# from any person's preferred location.
DEFAULT_CITY_COLOUR = (None, 62, 68)


def _city_colours(locations):
    """Stable (hue, saturation, lightness) per city in the current feed."""
    names = {name for name in (_city_name(item) for item in locations) if name}
    ordered = sorted(names, key=lambda name: hashlib.sha256(
        name.casefold().encode("utf-8")).hexdigest())
    total = len(ordered)
    colours = {}
    taken = set()
    for index, name in enumerate(ordered):
        hue = round(index * 360 / total) % 360
        offset = 1
        while hue in taken and offset < 360:
            hue = (hue + offset) % 360
            offset += 1
        taken.add(hue)
        colours[name] = (hue,) + DEFAULT_CITY_COLOUR[1:]
    return colours


def _tag_chip(text, title="", colour=None):
    """Inline chip in the score chip's family, for short facts like a city."""
    value = str(text or "").strip()
    if not value:
        return ""
    style = ""
    if colour and colour[0] is not None:
        style = ' style="--tag-color:hsl(%d %d%% %d%%)"' % colour
    return ('<span class="tag-chip"%s title="%s">%s</span>' %
            (style, html.escape(title or value, quote=True), html.escape(value)))

def _ad_paragraphs(text):
    """Separate AMS line breaks into keyboard-addressable escaped paragraphs."""
    chunks = [line.strip() for line in str(text).splitlines() if line.strip()]
    return "".join("<p>%s</p>" % html.escape(_plain_hyphens(line)) for line in chunks) or "<p></p>"

def _gui_compact_list(spec, ctx, proj, item):
    """Score, title and known location/pay; item fields on expansion."""
    rows = ranked_listings(ctx.get("rows") or [])
    fields = [node for node in item.get("children") or []
              if node.get("kind") in ("field", "status")
              and node.get("bind") != "listing.title"
              and (node.get("layout") or {}).get("gui") != "badge"]
    score_node = next((node for node in item.get("children") or []
                       if (node.get("layout") or {}).get("gui") == "badge"
                       and node.get("bind") == "listing.score"), None)
    location_node = next((node for node in item.get("children") or []
                          if node.get("bind") == "listing.location"), None)
    controls = _find_projected(proj.tree, "listings.controls") or {}
    control_nodes = controls.get("children") or []
    query_node = next((node for node in control_nodes if node.get("kind") == "query"), None)
    filter_nodes = [child for node in control_nodes for child in node.get("children") or []
                    if child.get("kind") == "filter"]
    filters = (('<div class="list-filters" aria-label="Filter listings">'
                + (_gui_control(spec, ctx, query_node) if query_node else "")
                + "".join(_gui_control(spec, ctx, node) for node in filter_nodes)
                + '</div>'))
    city_hues = _city_colours([row.get("location") for row in rows])
    names, cards = [], []
    for index, row in enumerate(rows):
        title = html.escape(_plain_hyphens(row.get("title") or "(untitled)"))
        url = str(row.get("url") or "")
        parsed = urlsplit(url)
        safe_url = url if parsed.scheme in ("http", "https") and parsed.netloc else ""
        score = bind_value(spec, ctx, proj.surface, score_node, row) if score_node else None
        score_text = (value_text(spec, ctx, proj.surface, score)
                      if isinstance(score, (int, float)) and not isinstance(score, bool)
                      and math.isfinite(score) else "")
        badge_score = _score_bubble(score, score_text) if score_node else ""
        location = bind_value(spec, ctx, proj.surface, location_node, row) if location_node else None
        location_text = _plain_hyphens(location) if location else ""
        city_name = _city_name(location_text)
        location_chip = _tag_chip(city_name, location_text or "Location",
                                  city_hues.get(city_name))
        pay_text = _listing_pay_text(row)
        search = " ".join(str(row.get(field) or "") for field in
                          ("title", "company", "location", "roles", "skills", "occupation_group"))
        # One grid for the whole list: score, title and pay share the first row
        # and the city chip sits under the title, so every row lines up.
        bookmarked = bool(row.get("bookmarked"))
        names.append('<li><button type="button" class="job-row" data-index="%d" '
                     'data-id="%s" data-href="%s" data-status="%s" data-stage="%s" '
                     'data-latest-scan="%s" data-search="%s" data-bookmarked="%s" '
                     'aria-expanded="false">'
                     '<span class="job-row-score">%s</span>'
                     '<span class="job-row-title">%s<span class="job-row-bookmark" '
                     'aria-label="Bookmark"%s>Bookmark</span>'
                     '<span class="generation-indicator" role="img" '
                     'aria-label="Generating cover letter" title="Generating cover letter" hidden></span></span>'
                     '<span class="job-row-pay">%s</span>'
                     '<span class="job-row-city">%s</span>'
                     '</button></li>'
                     % (index, html.escape(str(row.get("id") or ""), quote=True),
                        html.escape(safe_url, quote=True),
                        html.escape(str(row.get("status") or "new"), quote=True),
                        html.escape(str(row.get("stage") or ""), quote=True),
                        "true" if row.get("in_latest_scan") else "false",
                        html.escape(_plain_hyphens(search).lower(), quote=True),
                        "true" if bookmarked else "false",
                        badge_score, title, "" if bookmarked else " hidden",
                        html.escape(pay_text), location_chip))
        data, ad_sections = [], []
        has_sections = bool(row.get("job_description") or row.get("company_description"))
        for field in fields:
            raw = bind_value(spec, ctx, proj.surface, field, row)
            if field.get("bind") == "listing.pay_monthly_min":
                # One pay cell covers both bases: monthly first, yearly if that
                # is all the ad states, so the row is never blank when a figure
                # exists.
                raw = _listing_pay_cell(row)
            if field.get("kind") == "status":
                if isinstance(raw, bool):
                    raw = field.get("true_label") if raw else field.get("false_label")
                else:
                    raw = status_label(spec, raw)
            label = field.get("label") or field.get("id")
            if field.get("bind") == "listing.posted_date" and row.get("posted_basis") == "extraction":
                label = "Extraction date (posting date not given)"
            text = html.escape(_plain_hyphens(value_text(
                spec, ctx, proj.surface, raw, field.get("fallback", "-"))))
            if (field.get("layout") or {}).get("gui") == "preview":
                if field.get("bind") == "listing.full_text" and has_sections:
                    if raw:
                        ad_sections.append('<details class="ad-archive"><summary>%s</summary>'
                                           '%s</details>' % (html.escape(str(label)), _ad_paragraphs(raw)))
                    continue
                ad_sections.append('<details class="ad-section" data-field="%s" open>'
                                   '<summary><h3>%s</h3></summary>%s</details>' %
                                   (html.escape(str(field.get("bind") or ""), quote=True),
                                    html.escape(str(label)), _ad_paragraphs(value_text(
                                        spec, ctx, proj.surface, raw,
                                        field.get("fallback", "-")))))
            else:
                data.append('<dt>%s</dt><dd>%s</dd>' % (html.escape(str(label)), text))
        full_text = row.get("full_text") or ""
        preview = row.get("snippet") or ""
        # With no captured body the card is the only text there is, so show it
        # inline under a caption that names the cut and why it happened.
        card_preview = ('<div class="card-preview" data-card-preview="true">'
                        '<p class="card-caption">AMS search card, cut by AMS to 253 characters</p>'
                        '<p class="card-body">%s</p></div>' % html.escape(_plain_hyphens(preview))
                        if not full_text and preview else "")
        flags = row_flags(spec, ctx, proj.surface, item, row)
        badges = badge_score + "".join('<span class="flag is-%s" title="%s">%s</span>' %
                         (html.escape(str(flag["role"])),
                          html.escape(str(flag.get("meaning") or flag["label"])),
                          html.escape(str(flag["label"]))) for flag in flags)
        bookmark_state = ('<span class="bookmark-state flag is-info" aria-label="Bookmark"%s>'
                          'Bookmark</span>' % ("" if row.get("bookmarked") else " hidden"))
        badges += (bookmark_state + '<span class="generation-indicator" role="status" hidden>'
                   'Generating cover letter</span>' + _stage_chip_html(spec, row))
        cards.append('<section class="job-card" data-index="%d" hidden><h2>%s</h2>'
                     '<div class="flags">%s</div>'
                     '<div class="job-properties"><h3>Properties</h3><dl>%s</dl></div>'
                     '<details class="ad-panel" open><summary>AMS description</summary>%s</details>'
                     '%s</section>'
                     % (index, title, badges, "".join(data), "".join(ad_sections), card_preview))
    # Column widths follow the widest cell in the list, so the spacing is
    # dynamic but identical on every row: a real column, not per-row padding.
    pay_width = max([len(_listing_pay_text(row)) for row in rows] or [0])
    city_width = max([len(_city_name(_plain_hyphens(row.get("location"))))
                      for row in rows] or [0])
    columns = ' style="--pay-col:%dch;--city-col:%dch"' % (max(pay_width, 1) + 2,
                                                         min(max(city_width, 1) + 3, 20))
    pay_count = sum(bool(_listing_pay_text(row)) for row in rows)
    note = ("Sorted by score, then monthly pay (%d/%d list a monthly or yearly figure)"
            % (pay_count, len(rows)))
    actions = _find_projected(proj.tree, "listings.actions") or {}
    buttons = "".join(_gui_action_button(spec, ctx, node, shortcuts=False)
                      for node in actions.get("children") or [] if node.get("kind") == "action")
    # The individual stage jumps stay out of the browser row, but the three
    # endings sit one click away in the panel that Next opens at the last step.
    outcome_buttons = "".join(
        _gui_action_button(spec, ctx, node, shortcuts=False)
        for node in (_find_node(spec, "action.stage_" + key) for key in OUTCOME_CHOICES)
        if node)
    outcome_panel = ('<div class="outcome-panel" id="outcome-panel" hidden>'
                     '<p class="outcome-caption" id="outcome-caption">Outcome after the '
                     'waiting stage</p><div class="outcome-actions">%s</div></div>'
                     % outcome_buttons)
    model_options = "".join('<option value="%s"%s>%s: %s</option>' % (
        html.escape(code, quote=True), " selected" if code == DEFAULT_GENERATION_MODEL else "",
        html.escape(preset.label), html.escape(preset.model))
        for code, preset in GENERATION_MODELS.items())
    return ('<div class="job-list-view" id="job-list-view">'
            '<div class="list-top"><span><b id="job-shown">%d</b>/%d jobs</span>'
            '<span>%s</span></div>%s'
            '<ol class="job-list" id="job-list"%s>%s</ol>'
            '<p id="job-empty" class="list-help" hidden>No jobs match these filters.</p>'
            '<p class="list-help">j/k or up/down move - l or right opens</p></div>'
            '<article class="job-detail" id="job-detail" hidden>'
            '<button class="job-back" type="button" id="job-back">Back to jobs</button>'
            '%s<div class="job-action-group"><p class="job-actions-caption" '
            'id="job-actions-caption">Actions - written to the tracker</p>'
            '<div class="job-actions">%s</div>%s<p class="bookmark-note" '
            'id="bookmark-feedback" role="status" aria-live="polite">'
            'Bookmarks are stored in the tracker and shown in both views.</p></div>'
            '<div id="action-result" class="action-result" hidden><p id="action-message" '
            'role="status" aria-live="polite"></p><textarea id="action-command" '
            'aria-label="Command to run on your Mac" readonly rows="3"></textarea>'
            '<button id="action-copy" type="button">Copy command</button>'
            '<pre id="packet-view" class="packet-view" hidden></pre></div>'
            '<dialog id="generation-dialog" aria-labelledby="generation-title"><form method="dialog">'
            '<h2 id="generation-title">Generate cover letter</h2>'
            '<p hidden>The previous draft is kept in Versions.</p>'
            '<label for="generation-model">Generation model</label>'
            '<select id="generation-model" autofocus>%s</select>'
            '<div class="generation-dialog-actions"><button value="cancel">Cancel</button>'
            '<button value="run">Generate</button></div></form></dialog>'
            '<p class="list-help">h or left goes back - j/k jump by paragraph; '
            'up/down scroll by line. Bookmark status is read from the tracker. '
            'Public preview is read-only; tracker actions show a command for your Mac.</p></article>'
            % (len(rows), len(rows), html.escape(note), filters, columns,
               "".join(names), "".join(cards), buttons, outcome_panel, model_options))


def _stage_chip_html(spec, row):
    """Browser twin of the terminal stage strip: dots plus the stage name."""
    current, index, outcome = stage_gauge_state(spec, row)
    if not current:
        return ""
    flow = stage_flow(spec)
    dots = "".join(STAGE_NOW if position == index
                   else STAGE_DONE if position < index else STAGE_TODO
                   for position in range(len(flow)))
    key = outcome or current
    entry = stage_entry(spec, key)
    label = _terminal_text(entry.get("label") or key)
    full = label
    role = entry.get("role") or "accent"
    tone = {"accent": "primary", "neutral": "muted"}.get(role, role)
    note = "%s, step %d of %d" % (full, index + 1, len(flow))
    detail = stage_note(spec, row)
    if detail:
        note = "%s - %s" % (note, detail)
    return ('<span class="stage-chip is-%s" title="%s"><span aria-hidden="true">%s</span>'
            ' %s<span class="sr-only">: %d of %d steps</span></span>'
            % (html.escape(tone), html.escape(note, quote=True), dots,
               html.escape(label), index + 1, len(flow)))


def _render_nodes(spec, ctx, proj, nodes, skip_ids=None):
    parts = []
    skip_ids = skip_ids or set()

    def walk(nodes, depth, heading="h2"):
        for node in nodes:
            if node.get("id") in skip_ids:
                continue
            kind = node.get("kind")
            children = [child for child in (node.get("children") or [])
                        if child.get("id") not in skip_ids]
            remaining = [_counts_descendant(child, skip_ids) for child in children]
            if kind in ("group", "view"):
                if not children or not any(remaining):
                    continue
                slug = str(node.get("id", "")).replace(".", "-")
                label = node_label(spec, node)
                # one remaining child: its own heading carries the meaning
                if kind == "group" and len(children) == 1:
                    walk(children, depth, heading)
                    continue
                tag = "h2" if depth == 1 else ("h3" if depth == 2 else "h4")
                parts.append('<a id="%s"></a><section class="panel">' % html.escape(slug))
                parts.append("<%s>%s</%s>" % (tag, html.escape(label), tag))
                if kind == "view" and node.get("meaning"):
                    parts.append('<p class="hint">%s</p>' % html.escape(str(node["meaning"])))
                if kind == "group" and _has_selection_actions(spec, children):
                    parts.append(_focused_line(spec, ctx, proj))
                walk(children, depth + 1, tag)
                parts.append("</section>")
            elif kind == "item":
                parts.append(_gui_table(spec, ctx, proj, node))
            elif kind == "action":
                parts.append(_gui_action_button(spec, ctx, node))
            elif kind in ("filter", "sort", "query"):
                parts.append(_gui_control(spec, ctx, node))
            elif kind in ("meter", "field", "status"):
                value = bind_value(spec, ctx, proj.surface, node)
                if kind == "status":
                    if isinstance(value, bool):
                        value = node.get("true_label") if value else node.get("false_label")
                    else:
                        value = status_label(spec, value)
                text = value_text(spec, ctx, proj.surface, value, node.get("fallback", "-"))
                role = node.get("role", "neutral")
                parts.append('<dl class="inline"><dt>%s</dt><dd class="%s">%s</dd></dl>'
                             % (html.escape(str(node.get("label") or "")),
                                html.escape(role), html.escape(str(text))))
            elif kind == "message":
                role = node.get("role", "muted")
                parts.append('<p class="callout is-%s">%s</p>'
                             % (html.escape(role),
                                html.escape(str(node.get("text") or node.get("label") or ""))))

    walk(nodes, 1)
    state_message = proj.state_cfg.get("message")
    if state_message:
        parts.insert(0, '<p class="callout is-%s" role="status">%s</p>'
                     % (html.escape(proj.state_cfg.get("role", "muted")),
                        html.escape(str(state_message))))
    return "".join(parts)


def _counts_descendant(node, skip_ids):
    if node.get("id") not in skip_ids:
        if node.get("kind") not in ("group", "view") or node.get("children"):
            return True
        return True
    return any(_counts_descendant(child, skip_ids) for child in node.get("children") or [])


def _has_selection_actions(spec, nodes):
    for node in nodes:
        if node.get("kind") == "action":
            action = (spec.get("actions") or {}).get(node.get("action")) or {}
            if action.get("scope") == "selection":
                return True
    return False


def _focused_line(spec, ctx, proj):
    row = (ctx.get("selection") or {}).get("row") or {}
    title = row.get("title") or ""
    company = row.get("company") or ""
    if not title:
        return ('<p class="callout is-muted">No listing focused. Pick a row first; '
                'marking writes only through ams_nudge.py.</p>')
    score = "" if row.get("score") is None else " | score %.2f" % row["score"]
    return ('<p class="callout" id="focus-line"><b>Focused:</b> %s%s%s</p>'
            % (html.escape(_plain_hyphens(title)),
               (" @ " + html.escape(_plain_hyphens(company))) if company else "",
               html.escape(score)))


def _gui_control(spec, ctx, node):
    kind = node.get("kind")
    label = node.get("label") or ""
    bind = node.get("bind")
    key = (node.get("keys") or {}).get("gui", "")
    shortcut = ' data-shortcut="%s"' % html.escape(key) if key else ""
    if kind == "query":
        return ('<div class="toolbar"><input type="search" id="q" '
                'placeholder="Filter: %s" aria-label="%s"></div>'
                % (html.escape(str(node.get("placeholder", ""))), html.escape(str(label))))
    if kind == "sort" and isinstance(bind, dict):
        options = []
        for entry in (spec.get("vocabulary") or {}).get(bind.get("vocabulary")) or []:
            selected = " selected" if entry.get("key") == node.get("default") else ""
            options.append('<option value="%s"%s>%s</option>'
                           % (html.escape(entry.get("key", "")), selected,
                              html.escape(entry.get("label", ""))))
        return ('<div class="toolbar"><label class="lbl">Sort</label>'
                '<select aria-label="Sort listings">%s</select></div>' % "".join(options))
    if kind == "filter":
        if (node.get("mode") == "select" and isinstance(bind, dict)
                and bind.get("vocabulary") == "stages"):
            options = ['<option value="">All stages</option>']
            for entry in (spec.get("vocabulary") or {}).get("stages") or []:
                options.append('<option value="%s">%s</option>'
                               % (html.escape(str(entry.get("key") or ""), quote=True),
                                  html.escape(str(entry.get("label") or ""))))
            return ('<label class="stage-filter"><span>%s</span>'
                    '<select id="stage-filter" aria-label="Filter by current stage">%s</select></label>'
                    % (html.escape(str(label)), "".join(options)))
        status = (str(bind.get("key") or "") if isinstance(bind, dict)
                  else str(bind).replace("filter.", ""))
        meaning = str(node.get("meaning") or "")
        title = ' title="%s"' % html.escape(meaning, quote=True) if meaning else ""
        if status in ("scan_only", "latest_scan"):
            return ('<button type="button" id="scan-only" aria-pressed="false"%s%s>%s</button>'
                    % (shortcut, title, html.escape(str(label))))
        return ('<button type="button" data-filter-status="%s" aria-pressed="false"%s%s>%s</button>'
                % (html.escape(status), shortcut, title, html.escape(str(label))))
    return ""


def _gui_action_icon(ref):
    """Decorative glyphs; action labels and behavior stay in the structure."""
    shapes = {
        "stage_generated": '<path d="M6 3h6l3 3v11H6z"/><path d="M12 3v3h3M8 10h5M8 13h5"/>',
        "stage_checked": '<circle cx="10" cy="10" r="7"/><path d="m6.5 10 2.5 2.5 4.5-5"/>',
        "stage_sent": '<path d="M3 10 17 4l-4 13-3-5-7-2z"/><path d="m10 17 3-5"/>',
        "stage_confirmed": '<path d="m3 10 3 3 5-6"/><path d="m9 13 3 3 5-6"/>',
        "stage_waiting": '<circle cx="10" cy="10" r="7"/><path d="M10 6v4l3 2"/>',
        "stage_prev": '<path d="m13 4-6 6 6 6"/>',
        "stage_next": '<path d="m7 4 6 6-6 6"/>',
        "stage_interview": '<rect x="3" y="4.5" width="14" height="12" rx="2"/>'
                           '<path d="M6 3v3M14 3v3M3 8h14M7 12h6"/>',
        "stage_success": '<path d="M5 4h10v5a5 5 0 0 1-10 0z"/><path d="M7.5 17h5M10 14v3"/>',
        "stage_rejected": '<circle cx="10" cy="10" r="7"/><path d="m7 7 6 6M13 7l-6 6"/>',
        "stage_blocked": '<circle cx="10" cy="10" r="7"/><path d="M5 5l10 10"/>',
        "stage_unclear": '<circle cx="10" cy="10" r="7"/><path d="M8 8a2 2 0 1 1 3 1.7c-.7.4-1 .9-1 1.6"/>'
                         '<path d="M10 14.5v.5"/>',
        "stage_skipped": '<path d="M4 5v10l8-5-8-5zM15 5v10"/>',
        "mark_applied": '<path d="m4 10 4 4 8-8"/>',
        "mark_interview": '<rect x="3" y="4.5" width="14" height="12" rx="2"/>'
                          '<path d="M6 3v3M14 3v3M3 8h14M7 12h6"/>',
        "mark_blocked": '<circle cx="10" cy="10" r="7"/><path d="M5 5l10 10"/>',
        "mark_skipped": '<path d="M4 5v10l8-5-8-5zM15 5v10"/>',
        "mark_nudged": '<path d="M6 5H3v3M3 5a7 7 0 1 1-1 7"/>',
        "toggle_bookmark": '<path d="M5 3h10v14l-5-3-5 3z"/>',
        "open_source": '<path d="M9 4H4v12h12v-5M11 4h5v5M16 4l-7 7"/>',
    }
    shape = shapes.get(ref)
    if not shape:
        return ""
    return ('<span class="act-icon" aria-hidden="true">'
            '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" '
            'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">'
            '%s</svg></span>' % shape)


def _gui_action_button(spec, ctx, node, shortcuts=True):
    ref = node.get("action")
    action = (spec.get("actions") or {}).get(ref) or {}
    label = node.get("label") or action.get("label") or ref or "Run"
    role = node.get("role") or action.get("role") or "secondary"
    key = (node.get("keys") or {}).get("gui", "")
    transports = action.get("transports") or []
    template = next((t.get("template") for t in transports if t.get("kind") == "hermes.send"), "")
    cli = next((t.get("command") for t in transports if t.get("kind") == "cli"), "")
    raw_template, raw_cli = template, cli
    if ref in ("stage_next", "stage_prev"):
        # The browser cannot resolve a row's neighbouring step on its own, so
        # retarget() fills one placeholder from the row's own stage.
        token = "{stage_next}" if ref == "stage_next" else "{stage_prev}"
        template = template.replace("{stage}", token)
        cli = cli.replace("{stage}", token)
        raw_template = raw_template.replace("{stage}", token)
        raw_cli = raw_cli.replace("{stage}", token)
    local = next((t for t in transports if t.get("channel") == "browser-local"
                  and t.get("kind") == "local"), None)
    selection = (ctx.get("selection") or {})
    for name, value in selection.items():
        template = template.replace("{%s}" % name, str(value))
        cli = cli.replace("{%s}" % name, str(value))
    is_source = any(t.get("kind") == "url" and t.get("channel") == "browser" for t in transports)
    if is_source:
        source = str(selection.get("listing_url") or "")
        try:
            parsed = urlsplit(source)
            valid = (parsed.scheme in ("http", "https") and parsed.hostname
                     and not parsed.username and not parsed.password
                     and not any(ord(c) < 33 for c in source))
        except ValueError:
            valid = False
        href = html.escape(source if valid else "#", quote=True)
        parts = ['<a class="act is-%s" data-action="%s" data-source-link="true" '
                 'href="%s" target="_blank" rel="noopener noreferrer"'
                 % (html.escape(role), html.escape(str(ref)), href)]
    else:
        parts = ['<button type="button" class="act is-%s" data-action="%s"'
                 % (html.escape(role), html.escape(str(ref)))]
    if local:
        parts.append(' data-local-store="%s" aria-pressed="false"'
                     % html.escape(str(local.get("store") or ""), quote=True))
    if template:
        parts.append(' data-template="%s"' % html.escape(template))
    if cli:
        parts.append(' data-cli="%s"' % html.escape(cli))
    if raw_template and ("{listing_id}" in raw_template or "{listing_url}" in raw_template):
        parts.append(' data-chat-tmpl="%s"' % html.escape(raw_template))
    if raw_cli and ("{listing_id}" in raw_cli or "{listing_url}" in raw_cli):
        parts.append(' data-cli-tmpl="%s"' % html.escape(raw_cli))
    if key and shortcuts:
        parts.append(' data-shortcut="%s"' % html.escape(key))
    parts.append('>%s<span class="act-label">%s</span>%s</%s>' % (
                                _gui_action_icon(ref), html.escape(str(label)),
                                ("<kbd>%s</kbd>" % html.escape(key)) if key and shortcuts else "",
                                "a" if is_source else "button"))
    return '<span class="actwrap">' + "".join(parts) + "</span>"


def _gui_table(spec, ctx, proj, item):
    rows = ctx.get("rows") or []
    declared = [child for child in (item.get("children") or [])
                if child.get("kind") in ("field", "status")]
    fields = [child for child in declared
              if (child.get("layout") or {}).get("gui") not in ("detail", "preview")]
    extras = [child for child in declared
              if (child.get("layout") or {}).get("gui") in ("detail", "preview")]
    head = "<th scope=\"col\">Flags</th>" + "".join(
        "<th scope=\"col\">%s</th>" % html.escape(str(field.get("label") or field["id"]))
        for field in fields)
    body = []
    for row in rows:
        cells = []
        flags = row_flags(spec, ctx, proj.surface, item, row)
        cells.append('<td class="flagcell"><span class="flags">%s</span></td>' % "".join(
            '<span class="flag is-%s" title="%s">%s</span>'
            % (html.escape(str(flag["role"])), html.escape(str(flag.get("meaning") or flag["label"])),
               html.escape(str(flag["label"])))
            for flag in flags))
        for field in fields:
            value = bind_value(spec, ctx, proj.surface, field, row)
            cls = ""
            if field.get("kind") == "status":
                if isinstance(value, bool):
                    text = field.get("true_label") if value else field.get("false_label")
                    cls = "badge is-info" if value else "badge is-muted"
                else:
                    text = status_label(spec, value)
                    role = next((entry.get("role", "neutral") for entry in
                                 (spec.get("vocabulary") or {}).get("statuses") or []
                                 if entry.get("key") == value), "neutral")
                    cls = "badge is-" + str(role)
                value = text
                cells.append('<td><span class="%s">%s</span></td>' % (html.escape(cls), html.escape(str(value or "-"))))
                continue
            text = value_text(spec, ctx, proj.surface, value)
            note = ""
            if str(field.get("bind", "")) == "listing.posted_date" \
                    and row.get("posted_basis") == "extraction":
                note = "extraction date: the ad carried no posting date, this is the scan date"
            if str(field.get("bind", "")).endswith("score"):
                cells.append('<td class="num">%s</td>' % html.escape(text))
            elif str(field.get("bind", "")) == "listing.occupation_group":
                first_group = (text or "").split(",")[0].strip() or "-"
                cells.append('<td class="cell group" title="%s">%s</td>'
                             % (html.escape(text), html.escape(first_group)))
            elif str(field.get("bind", "")) == "listing.title":
                cells.append('<td class="cell lead" title="%s">%s</td>'
                             % (html.escape(text), html.escape(text)))
            else:
                cells.append('<td class="cell" title="%s">%s</td>'
                             % (html.escape(note or text), html.escape(text)))
        focused = str(row.get("id", "")) == str((ctx.get("selection") or {}).get("listing_id", ""))
        detail = []
        title_text = ""
        company_text = ""
        for field in declared:
            bind = str(field.get("bind", ""))
            if bind == "listing.title":
                title_text = value_text(spec, ctx, proj.surface,
                                        bind_value(spec, ctx, proj.surface, field, row))
            elif bind == "listing.company":
                company_text = value_text(spec, ctx, proj.surface,
                                          bind_value(spec, ctx, proj.surface, field, row))
        for field in extras:
            value = value_text(spec, ctx, proj.surface,
                               bind_value(spec, ctx, proj.surface, field, row))
            if value and value.strip() not in ("", "-", "0") \
                    and str(field.get("bind")) not in ("listing.score_source", "listing.in_latest_scan"):
                detail.append("%s: %s" % (field.get("label") or field["id"], value))
        body.append('<tr data-status="%s" data-stage="%s" data-latest-scan="%s" data-id="%s" data-href="%s" '
                    'data-detail="%s" data-title="%s" data-company="%s" tabindex="-1"%s>%s</tr>'
                    % (html.escape(str(row.get("status", ""))),
                       html.escape(str(row.get("stage", ""))),
                       "true" if row.get("in_latest_scan") else "false",
                       html.escape(str(row.get("id", ""))),
                       html.escape(str(row.get("url", ""))),
                       html.escape("  ·  ".join(detail)),
                       html.escape(title_text), html.escape(company_text),
                       ' class="is-focus" aria-current="true"' if focused else "",
                       "".join(cells)))
    empty = ('<p class="callout is-muted" id="empty-state"%s>No rows in the current context. '
             'Load a feed to fill this table.</p>' % ("" if not rows else " hidden"))
    total = (ctx.get("facts") or {}).get("counts.total")
    suffix = ("of %s known" % total) if total else "shown"
    return ('<div class="toolbar"><span class="sub"><b id="shown">%d</b> %s</span></div>'
            '<div class="tablewrap"><table><caption class="sub">%s</caption>'
            '<thead><tr>%s</tr></thead><tbody id="rows">%s</tbody></table></div>'
            '<p class="rowdetail" id="row-detail" aria-live="polite"></p>%s'
            % (len(rows), html.escape(suffix),
               html.escape(str(item.get("label") or "")), head, "".join(body), empty))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _print_problems(problems):
    errors = [p for p in problems if p["severity"] == "error"]
    warns = [p for p in problems if p["severity"] == "warn"]
    for problem in errors + warns:
        print("%-5s %-14s %-42s %s" % (problem["severity"].upper(), problem["code"],
                                       problem["at"], problem["message"]))
    print("%d error(s), %d warning(s)" % (len(errors), len(warns)))
    return len(errors)


def _read_context(path):
    if not path:
        return empty_context()
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _context_from_feed_file(path, mode="served"):
    sys.path.insert(0, HERE)
    import feed as feedmod
    if os.path.isdir(path):
        feed = feedmod.build_feed(path)
    else:
        with open(path, "r", encoding="utf-8") as handle:
            feed = json.load(handle)
    return context_from_feed(feed, mode=mode)


def _tracker_jobs():
    """Every application record the writer holds, keyed by listing id."""
    import feed as feedmod

    if os.path.exists(os.path.expanduser("~/.hermes/scripts/ams_nudge.py")):
        path = os.path.join(feedmod.DEFAULT_SOURCE, "applications.json")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                store = json.load(handle)
        except (OSError, ValueError):
            return {}
    else:
        # No writer here: ask the launcher for the store over the same scoped
        # hop a write uses. Anything but a clean answer leaves the feed alone.
        if os.environ.get("JOBUI_REMOTE_WRITES") != "1":
            return {}
        try:
            import dashboard as dashboardmod
            return dashboardmod.remote_state()
        except Exception:  # noqa: BLE001 - the menu still works on the feed
            return {}
    jobs = store.get("jobs") if isinstance(store, dict) else None
    return jobs if isinstance(jobs, dict) else {}


def _overlay_tracker(ctx, jobs):
    """Rows as the tracker has them, not as the last feed build left them.

    Every write lands in the tracker before any feed file is rebuilt, so the
    list, the gauges, the filters and the header counts all read the writer's
    own record here. Only fields the tracker stores are copied.
    """
    import feed as feedmod

    rows = ctx.get("rows") or []
    if not jobs or not rows:
        return ctx
    for row in rows:
        record = jobs.get(str(row.get("id") or ""))
        if not isinstance(record, dict):
            continue
        stage = str(record.get("stage") or "").strip()
        status = str(record.get("status") or "").strip()
        if stage in feedmod.STAGE_STATUS:
            row["status"] = feedmod.STAGE_STATUS[stage]
        elif status:
            stage = feedmod.STATUS_STAGE.get(status, stage)
            row["status"] = status
        if stage:
            row["stage"] = stage
            row["stage_label"] = feedmod.STAGE_LABEL.get(stage, stage)
            row["stage_short"] = feedmod.STAGE_SHORT.get(stage, row["stage_label"])
        if row.get("status"):
            row["status_label"] = feedmod.STATUS_LABEL.get(row["status"], row["status"])
        row["stage_at"] = str(record.get("stage_at") or "")[:10]
        row["tracked_date"] = str(record.get("sent_at") or "")[:10]
        row["note"] = str(record.get("note") or "")
        row["bookmarked"] = bool(record.get("bookmarked"))
        row["bookmarked_at"] = str(record.get("bookmarked_at") or "")
    facts = ctx.get("facts") or {}
    facts["counts.total"] = len(rows)
    for key in list(facts):
        if not key.startswith("counts."):
            continue
        name = key.split(".", 1)[1]
        if name in ("total", "with_score", "in_latest_scan"):
            continue
        facts[key] = sum(1 for row in rows if row.get("status") == name)
    return ctx


def overlay_tracker_state(ctx, jobs=None):
    """Fold the tracker's own stage over a context any surface just built.

    build_feed describes the scan; the tracker knows what the user last
    pressed. Served pages have to show the second one too, or a stage key
    pressed twice on the page would not know the first press happened.
    """
    return _overlay_tracker(ctx, _tracker_jobs() if jobs is None else jobs)


def _context_with_tracker_state(path, mode="served"):
    """The feed on disk, with the tracker's live state folded over it."""
    return _overlay_tracker(_context_from_feed_file(path, mode=mode), _tracker_jobs())


def _live_context(path, mode="served"):
    """The freshest context this host can build, for the first frame and `r`."""
    if os.path.exists(os.path.expanduser("~/.hermes/scripts/ams_nudge.py")):
        return _refreshed_context(path, mode=mode)
    return _context_with_tracker_state(path, mode=mode)


def _refreshed_context(path, mode="served"):
    """Rebuild the feed from the tracker, then project it.

    A stage write lands in applications.json; only a rebuild carries it into
    the projected rows, so the gauge moves to where the writer says it is.
    """
    try:
        import feed as feedmod  # same directory, only when running the menu
        feed = feedmod.build_feed()
        feedmod.write_feed(feed, path)
    except Exception:  # noqa: BLE001 - fall back to the feed already on disk
        return _context_from_feed_file(path, mode=mode)
    return _context_from_feed_file(path, mode=mode)


# ---------------------------------------------------------------------------
# Interactive terminal navigation: the same structure, but as a menu that can
# be walked. Stage keys run the declared local transport (tools/dashboard.py
# mark ...), which shells out to ams_nudge.py, still the single writer. Nothing
# runs unless the surface declares the writes capability and an executor is
# injected, so frame dumps and tests stay read only.
# ---------------------------------------------------------------------------

HELP_GROUPS = [
    ("Move", [("j / k", "next listing; inside one: next section, then next paragraph"),
              ("l / right", "open the listing; step into the section under the cursor"),
              ("h / left", "leave the section, then leave the listing"),
              ("space", "fold or unfold the section under the cursor (the only fold)"),
              ("gg / G", "first or last listing"),
              ("pgdn / pgup", "one page down or up"),
              ("esc / h", "close the detail, back to the list")]),
    ("Open", [("l / enter", "open the listing, stage 0 becomes read"),
              ("v", "open the ad on jobs.ams.at"),
              ("/", "search title, company, skills"),
              ("f", "refresh from the feed")]),
    ("Stage", [("n / p", "next or previous stage"),
               ("m", "generating application"),
               ("e", "control application (proof-read)"),
               ("a", "sending out"),
               ("c", "confirming"),
               ("u", "waiting"),
               ("C", "call/contact"),
               ("i", "interview"),
               ("z", "interview preparation"),
               ("X", "waiting for outcome"),
               ("d", "fail"),
               ("W", "won"),
               ("b", "blocked"),
               ("U", "unclear"),
               ("K", "skipped"),
               ("twice", "the same stage key again undoes it")]),
    ("Application", [("R", "write the packet (asks before writing a second time)"),
                    ("V", "open the packet: letter, CV notes, documents, why"),
                    ("N", "note the feedback on this application")]),
    ("Filter", [("1-9 / 0", "status filters, one at a time"),
                ("S", "choose one current application stage"),
                ("*", "bookmarked only"),
                ("s", "latest scan only"),
                ("r", "reset every filter")]),
    ("Other", [("B", "bookmark or unbookmark the row"),
               (",", "settings: choose the generation language and model"),
               ("t / T", "cycle the theme"),
               ("M", "mouse wheel on or off"),
               ("?", "this help, any key closes"),
               ("q", "quit")]),
]


def help_lines(width):
    """The whole key map, two columns when the window is wide enough."""
    key_width = max(len(keys) for _, items in HELP_GROUPS for keys, _ in items) + 2
    text_width = max(len(text) for _, items in HELP_GROUPS for _, text in items)
    span = key_width + text_width + 3
    cells = []
    for group, items in HELP_GROUPS:
        cells.append((group, "", True))
        for keys, text in items:
            cells.append((keys.ljust(key_width), text, False))

    def cell_text(cell):
        keys, text, head = cell
        return keys if head else "%s%s" % (keys, text)

    if width < span * 2:
        return [cell_text(cell).rstrip() for cell in cells]
    half = (len(cells) + 1) // 2
    lines = []
    for index in range(half):
        left_text = "%-*s" % (span, cell_text(cells[index]))
        right = cells[index + half] if index + half < len(cells) else ("", "", True)
        lines.append((left_text + cell_text(right)).rstrip())
    return [line for line in lines if line.strip()]


NAV_HELP = [
    "j / k         next / previous entry",
    "h / l         go up one level / open the focused entry",
    "ctrl-d/u      page down / up",
    "gg / G        first / last entry",
    "enter         open group, apply filter, copy command",
    "tab / ] / [   next / previous tab",
    "1-9 / 0 / s   filter by status, toggle latest scan",
    "S             filter by one current application stage",
    "open a row    the first time marks it read, stage 0 becomes stage 1",
    "a i d b       sending out, interview, fail, blocked",
    "W U K         won, unclear, skipped",
    "m e c u C     generating, control, confirming, waiting, call/contact",
    "z X           interview preparation, waiting for outcome",
    "R / V         write the application packet, or open the one already there",
    "N             type a feedback note; enter saves it, esc cancels",
    "n             advance the application one stage",
    "v             open the selected AMS listing in a browser",
    "/             search listings (enter applies, esc clears)",
    "mouse wheel   scroll the open listing, or walk the list (off: drag to copy)",
    "M             take the mouse wheel, or hand it back so text can be copied",
    "r             reset filters and search",
    "?             hide this help",
    "q             close the focused level, then quit",
]


def action_command(spec, action_id, values):
    """Return the static-channel command for an action, or ''."""
    action = (spec.get("actions") or {}).get(action_id) or {}
    for transport in action.get("transports") or []:
        if transport.get("channel") == "static" and transport.get("kind") == "cli":
            command = transport.get("command") or ""
            for name, value in values.items():
                command = command.replace("{%s}" % name, str(value))
            return command
    return ""


def _action_values(action, row, ctx):
    values = {}
    for arg in action.get("args") or []:
        if "const" in arg:
            values[arg.get("name")] = arg.get("const")
        elif arg.get("from") == "selection.listing_id":
            values[arg.get("name")] = (row or {}).get("id", "")
        elif arg.get("from") == "selection.listing_url":
            values[arg.get("name")] = (row or {}).get("url", "")
    return values


# Row fields the tracker itself reports back after a write. Anything else in
# its echo (titles, ok flags) is display noise and never overwrites a row.
_ECHO_FIELDS = ("stage", "status", "stage_at", "note", "bookmarked", "bookmarked_at")


def _writer_echo(output):
    """The tracker's JSON answer at the end of a write, or {}.

    ``ams_nudge.py`` prints one JSON object after its status line, and
    ``dashboard.py`` passes it through untouched, so the last object in the
    stream is the record of what the writer actually stored.
    """
    text = (output or "").strip()
    start = text.rfind("{")
    while start >= 0:
        try:
            payload = json.loads(text[start:])
        except ValueError:
            start = text.rfind("{", 0, start)
            continue
        return payload if isinstance(payload, dict) else {}
    return {}


def _safe_ams_url(url):
    """Accept only HTTPS links on the AMS jobs host before opening a browser."""
    if not isinstance(url, str) or not url:
        return False
    try:
        parsed = urlsplit(url.strip())
        port = parsed.port
    except ValueError:
        return False
    return (parsed.scheme.lower() == "https"
            and (parsed.hostname or "").lower() == "jobs.ams.at"
            and parsed.username is None and parsed.password is None
            and port in (None, 443))


def _find_projected(nodes, node_id):
    for node in nodes or []:
        if node.get("id") == node_id:
            return node
        hit = _find_projected(node.get("children") or [], node_id)
        if hit:
            return hit
    return None


class Nav:
    """Tabs, cursor, filters, search and expansion over one projected tree."""

    def __init__(self, spec, ctx, surface="tui", browser_opener=None, persist_theme=False,
                 explain_once=False, config_dir=None, executor=None, loader=None,
                 persist_settings=False, clipboard=None, generation_submit=None):
        self.spec = spec
        self.ctx = ctx
        self.surface = surface
        self.browser_opener = browser_opener if browser_opener is not None else webbrowser.open
        # executor(command) runs a local write; loader() rebuilds the context.
        # Both stay injectable so no test or frame dump can touch the tracker.
        self.executor = executor
        self.loader = loader
        self.clipboard = clipboard
        self.writes = 0
        self.statuses = set()
        self.stage_filter = ""
        self.latest_only = False
        self.bookmarked_only = False
        self.query = ""
        self.sort_key = self._default_sort()
        self.collapsed = set()
        self.selected_id = ((ctx.get("selection") or {}).get("listing_id") or "")
        self.focus = 0
        self.message = ""
        self.show_help = False
        self.prompt = None
        self.prompt_text = ""
        self.quit = False
        self.pending_g = False
        self.expanded_id = None
        self.detail_scroll = 0
        self.detail_width = 74
        self.paragraph_starts = []
        # The detail is a ladder: cards at the element level, paragraphs inside
        # the open card. One cursor per level, plus the folded ids and the spans
        # the last render measured.
        self.detail_folded = set()
        self.detail_sections = []
        self.detail_level = "elements"
        self.detail_card = 0
        self.detail_para = 0
        self.detail_follow = True
        self.persist_theme = persist_theme
        self.explain_once = explain_once
        self.config_dir = config_dir or CONFIG_DIR
        self.persist_settings = persist_settings
        self.settings_open = False
        self.generation_language = saved_generation_setting(
            self.config_dir, "generation-language", GENERATION_LANGUAGES, DEFAULT_LANGUAGE)
        self.generation_model = saved_generation_setting(
            self.config_dir, "generation-model", GENERATION_MODELS, DEFAULT_GENERATION_MODEL)
        self.popup = None
        self.popup_choices = {}
        self.generation_picker = None
        self.packet_view = None
        self.packet_pane = "letter"
        self.packet_block = 0
        self.packet_follow = False
        self.packet_scroll: dict[str, int] = {"letter": 0, "notes": 0, "details": 0}
        self.packet_terminal_action = None
        self.packets = {}
        self.generated_packets = set()
        self.generation_submit = generation_submit
        self.generation = None
        self.generation_frame = 0
        self.action_rows = range(0)
        self.tab_menu = False
        # Mouse reporting is off by default: while it is on the terminal hands
        # drags to the app instead of selecting text, so nothing can be copied.
        # `M` takes the wheel when it is wanted and gives it back afterwards,
        # and `m` stays the "generated" stage key.
        self.mouse = False
        # The painted frame doubles as the hit map for button clicks, so the
        # loop hands the last one back here before it reads the next event.
        self.last_frame = []
        set_theme(saved_theme())
        self.tabs = sorted([tab for tab in (spec.get("tabs") or []) if isinstance(tab, dict)],
                           key=lambda tab: (tab.get("order", 1000),))
        self.tab_index = 0
        self.reload_projection()
        self.focus_first_row()

    # -- projection -------------------------------------------------------
    def _default_sort(self):
        found = []

        def walk(nodes):
            for node in nodes:
                if node.get("kind") == "sort" and node.get("default"):
                    found.append(node["default"])
                walk(node.get("children") or [])

        walk(self.spec.get("tree") or [])
        return found[0] if found else None

    def active_tab(self):
        if not self.tabs:
            return None
        if self.tab_index >= len(self.tabs):
            self.tab_index = 0
        return self.tabs[self.tab_index]

    def reload_projection(self):
        self.proj = Projection(self.spec, self.ctx, self.surface)
        tab = self.active_tab()
        self.tab_node = _find_projected(self.proj.tree, tab.get("root")) if tab else None
        if self.tab_node is None:
            self.tab_children = self.proj.tree
        else:
            self.tab_children = self.tab_node.get("children") or [self.tab_node]

    def focus_first_row(self):
        """Open on content, not on chrome: land the cursor on the first listing."""
        for index, entry in enumerate(self.entries()):
            if entry["kind"] == "item":
                self.focus = index
                return

    def switch_tab(self, delta):
        if not self.tabs:
            return
        self.tab_index = (self.tab_index + delta) % len(self.tabs)
        self.focus = 0
        self.collapsed.clear()
        self.close_detail()
        self.reload_projection()
        self.focus_first_row()
        tab = self.active_tab()
        self.message = "tab: %s" % (tab.get("label") if tab else "-")

    def meters(self):
        out = []

        def walk(nodes):
            for node in nodes:
                if node.get("kind") == "meter":
                    value = fact_value(self.ctx, self.surface, node.get("bind"))
                    out.append((node.get("label") or node.get("id"),
                                value_text(self.spec, self.ctx, self.surface, value)))
                walk(node.get("children") or [])

        walk(self.proj.tree)
        return out

    # -- rows -------------------------------------------------------------
    def rows(self):
        rows = list(self.ctx.get("rows") or [])
        if self.statuses:
            rows = [row for row in rows if row.get("status") in self.statuses]
        if self.stage_filter:
            rows = [row for row in rows if row.get("stage") == self.stage_filter]
        if self.latest_only:
            rows = [row for row in rows if row.get("in_latest_scan")]
        if self.bookmarked_only:
            rows = [row for row in rows if row.get("bookmarked")]
        if self.query:
            needle = self.query.lower()
            keep = []
            for row in rows:
                hay = " ".join([str(row.get(key) or "") for key in
                                ("title", "company", "location", "roles", "skills",
                                 "occupation_group")])
                if needle in hay.lower():
                    keep.append(row)
            rows = keep
        if self.sort_key == "score":
            return ranked_listings(rows)
        if self.sort_key:
            direction = 1
            for entry in (self.spec.get("vocabulary") or {}).get("sort_keys") or []:
                if entry.get("key") == self.sort_key and entry.get("default_direction") == "desc":
                    direction = -1

            def sort_key(row):
                value = row.get(self.sort_key)
                if isinstance(value, bool) or value in (None, ""):
                    return (1, 0.0, "")
                try:
                    return (0, float(value), "")
                except (TypeError, ValueError):
                    return (1, 0.0, str(value).lower())

            rows = sorted(rows, key=sort_key, reverse=direction < 0)
        return rows

    def selected_row(self):
        for row in self.rows():
            if row.get("id") == self.selected_id:
                return row
        rows = self.rows()
        return rows[0] if rows else None

    def flags_text(self, node, row):
        flags = row_flags(self.spec, self.ctx, self.surface, node, row)
        return " ".join("[%s]" % flag["label"] for flag in flags)

    def row_label(self, node, row, index):
        title = _plain_hyphens(row.get("title") or row.get("id") or "(untitled)")
        company = row.get("company") or ""
        status = status_label(self.spec, row.get("status"))
        flags = self.flags_text(node, row)
        line = "%d. %s%s%s" % (index + 1, flags, " " if flags else "", title)
        if company:
            line += " - " + company
        return "%s (%s)" % (line, status)

    # -- entries ----------------------------------------------------------
    @staticmethod
    def listing_entry(item, row):
        title = _plain_hyphens(row.get("title") or "(untitled)")
        return {"id": "item.listing:%s" % row.get("id", ""),
                "kind": "item", "depth": 0, "label": title,
                "node": item, "row": row, "parent": None, "path": [title]}

    def entries(self):
        out = []
        query = self.query.lower()

        # The Listings tab starts with job names only. Its item node remains
        # the shared schema for the browser and terminal detail views.
        tab = self.active_tab() or {}
        if tab.get("id") == "tab.listings":
            item = _find_projected(self.tab_children, "item.listing")
            if item is None:
                return []
            return [self.listing_entry(item, row) for row in self.rows()]

        def matches(node, row=None):
            if not query:
                return True
            hay = " ".join([str(node.get("label") or ""), str(node.get("id") or "")])
            if row:
                hay += " " + " ".join(str(row.get(key) or "") for key in
                                      ("title", "company", "location", "occupation_group"))
            return query in hay.lower()

        def walk(nodes, depth, trail, parent):
            for node in nodes:
                node_id = node.get("id")
                label = node_label(self.spec, node)
                kind = node.get("kind")
                path = trail + [label]
                if kind == "item":
                    rows = self.rows()
                    if not rows:
                        out.append({"id": node_id + ":empty", "kind": "message", "depth": depth,
                                    "label": "(no listings match)", "node": node, "row": None,
                                    "path": path + ["no rows"], "parent": parent})
                        continue
                    for index, row in enumerate(rows):
                        if not matches(node, row):
                            continue
                        out.append({"id": "%s:%s" % (node_id, row.get("id")), "kind": "item",
                                    "depth": depth, "label": self.row_label(node, row, index),
                                    "node": node, "row": row, "parent": parent,
                                    "path": path + [str(row.get("title") or "")]})
                    continue
                if kind in ("group", "view") and node.get("children"):
                    collapsed = node_id in self.collapsed
                    if not collapsed and not self._subtree_matches(node, matches):
                        continue
                    out.append({"id": node_id, "kind": kind, "depth": depth, "label": label,
                                "node": node, "row": None, "path": path, "parent": parent,
                                "collapsed": collapsed})
                    if not collapsed:
                        walk(node["children"], depth + 1, path, node_id)
                    continue
                if not matches(node):
                    continue
                out.append({"id": node_id, "kind": kind, "depth": depth, "label": label,
                            "node": node, "row": None, "path": path, "parent": parent})

        walk(self.tab_children, 0, [], None)
        return out

    def _subtree_matches(self, node, matches):
        if matches(node):
            return True
        for child in node.get("children") or []:
            if child.get("kind") == "item":
                if any(matches(child, row) for row in self.rows()):
                    return True
                continue
            if self._subtree_matches(child, matches):
                return True
        return False

    def focused(self):
        if self.expanded_id is not None and (self.active_tab() or {}).get("id") == "tab.listings":
            # A successful stage write can remove the open listing from the
            # filtered list. The open detail must still stay on its own id.
            item = _find_projected(self.tab_children, "item.listing")
            row = next((row for row in self.ctx.get("rows") or []
                        if row.get("id") == self.expanded_id), None)
            return self.listing_entry(item, row) if item is not None and row is not None else None
        entries = self.entries()
        if not entries:
            return None
        if self.focus >= len(entries):
            self.focus = len(entries) - 1
        if self.focus < 0:
            self.focus = 0
        entry = entries[self.focus]
        if entry["kind"] == "item":
            self.selected_id = (entry.get("row") or {}).get("id") or self.selected_id
        return entry

    def branch(self):
        """Ancestor chain of the focused entry, root first (the yazi parent column)."""
        entries = self.entries()
        focus = self.focused()
        if focus is None:
            return []
        by_id = {entry["id"]: entry for entry in entries}
        chain = []
        parent = focus.get("parent")
        while parent and parent in by_id:
            node = by_id[parent]
            chain.append(node)
            parent = node.get("parent")
        return list(reversed(chain))

    def siblings(self):
        focus = self.focused()
        if focus is None:
            return []
        return [entry for entry in self.entries() if entry.get("parent") == focus.get("parent")]

    # -- activation -------------------------------------------------------
    def activate(self, entry):
        if not entry:
            return
        node = entry["node"]
        kind = entry["kind"]
        node_id = node.get("id")
        if kind in ("group", "view"):
            if node_id in self.collapsed:
                self.collapsed.discard(node_id)
                self.message = "%s expanded" % node_label(self.spec, node)
            elif node.get("children"):
                self.focus += 1
                self.message = "%s opened" % node_label(self.spec, node)
            return
        if kind == "item":
            self.selected_id = (entry.get("row") or {}).get("id") or self.selected_id
            if (self.active_tab() or {}).get("id") == "tab.listings":
                first = self.expanded_id is None and self._explain_pending()
                self.close_detail()
                self.expanded_id = self.selected_id
                self.message = ""
                self.mark_read_on_open(entry.get("row") or {})
                if first:
                    self.popup = ("Flags explained",
                                  self._flag_explanations(),
                                  "press any key to continue")
            else:
                self.message = "selected: %s" % (entry.get("row") or {}).get("title", "")
            return
        if kind == "filter":
            self.apply_filter(node)
            return
        if kind == "sort":
            self.cycle_sort(node)
            return
        if kind == "query":
            self.prompt = "query"
            self.prompt_text = self.query
            self.message = "search: "
            return
        if kind == "action":
            self.describe_action(node)
            return
        self.message = "read only: %s" % (node.get("meaning") or (node.get("bind") or ""))
        return

    def apply_filter(self, node):
        bind = node.get("bind")
        if isinstance(bind, dict) and bind.get("vocabulary") == "stages":
            if node.get("mode") == "select":
                self.open_stage_filter_picker()
                return
            key = str(bind.get("key") or "")
            if not key or key == self.stage_filter:
                self.stage_filter = ""
                self.message = "stage filter off"
            else:
                self.stage_filter = key
                self.message = "stage filter on: %s" % node.get("label")
            self.focus = 0
            self.close_detail()
            return
        if isinstance(bind, dict) and bind.get("vocabulary") == "statuses":
            key = bind.get("key")
            if key in self.statuses and len(self.statuses) == 1:
                self.statuses.clear()
                self.message = "filter off: %s" % node.get("label")
            else:
                # A record has exactly one status, so the status filter is one
                # lens at a time: picking blocked stops showing new.
                self.statuses = {key}
                self.message = "filter on: %s" % node.get("label")
            self.focus = 0
            self.close_detail()
            return
        if bind == "filter.bookmarked_only" or (isinstance(bind, dict)
                                                and bind.get("field") == "bookmarked"):
            self.bookmarked_only = not self.bookmarked_only
            self.message = "bookmarked only: %s" % ("on" if self.bookmarked_only else "off")
            self.focus = 0
            self.close_detail()
            return
        self.latest_only = not self.latest_only
        self.message = "latest scan only: %s" % ("on" if self.latest_only else "off")
        self.focus = 0
        self.close_detail()

    def open_stage_filter_picker(self):
        """Choose one exact application stage, or clear the stage facet."""
        stages = (self.spec.get("vocabulary") or {}).get("stages") or []
        choice_keys = list("123456789abcdefghijklmnopqrstuvwxyz")
        if len(stages) > len(choice_keys):
            self.message = "stage picker has more options than available keys"
            return
        self.popup_choices = {}
        lines = []
        for choice, entry in zip(choice_keys, stages):
            key = str(entry.get("key") or "")
            label = str(entry.get("label") or key)
            selected = "*" if key == self.stage_filter else " "
            lines.append("%s %s %s" % (choice, selected, label))
            self.popup_choices[choice] = {
                "id": "filter.stage." + key,
                "kind": "filter",
                "label": label,
                "bind": {"vocabulary": "stages", "key": key},
            }
        lines.append("0   All stages (clear)")
        self.popup_choices["0"] = {
            "id": "filter.stage.all",
            "kind": "filter",
            "label": "All stages",
            "bind": {"vocabulary": "stages", "key": ""},
        }
        self.popup = ("Filter by current stage", lines,
                      "1-9, a-z choose · 0 all stages · esc cancel")

    def cycle_sort(self, node):
        keys = [entry.get("key") for entry in
                (self.spec.get("vocabulary") or {}).get("sort_keys") or []]
        if not keys:
            self.message = "no sort keys declared"
            return
        if self.sort_key not in keys:
            self.sort_key = keys[0]
        else:
            self.sort_key = keys[(keys.index(self.sort_key) + 1) % len(keys)]
        self.message = "sort: %s" % vocabulary_label(self.spec, "sort_keys", self.sort_key)

    def mark_read_on_open(self, row):
        """Stage 0 is unread; opening the listing fills the first circle.

        This is the one write the menu does on its own, and only for a row
        that was never opened. Surfaces without an executor stay read only.
        """
        stage = str((row or {}).get("stage") or "unread").strip()
        if stage not in ("", "unread") or self.executor is None:
            return
        if not row.get("id"):
            return
        self.describe_action({"id": "action.stage_read", "action": "stage_read",
                              "label": "Mark read"})
        self.message = ""

    # ---- the feedback note on an application ------------------------------

    def open_note_prompt(self, row):
        """Type the note on this application; enter saves through the writer."""
        if not str((row or {}).get("id") or ""):
            self.message = "select a listing row first"
            return
        self.prompt = "note"
        self.prompt_text = str((row or {}).get("note") or "")
        self.message = ("note for %s: enter saves, esc cancels"
                        % (row.get("title") or row.get("id")))

    def save_note(self, text):
        """Store the note with the application, so feedback survives the stage."""
        row = self.focused() or {}
        row = row.get("row") if row.get("kind") == "item" else self.selected_row()
        row = row or {}
        row_id = str(row.get("id") or "")
        action = (self.spec.get("actions") or {}).get("note_application") or {}
        command = action_command(self.spec, "note_application", {"listing_id": row_id})
        if not row_id or not command:
            self.message = "note: no command declared"
            return
        command = "%s %s" % (command, shlex.quote(text))
        if self.executor is None:            # read-only surface
            self.message = "run: " + command
            return
        group = _find_projected(self.tab_children, "listings.actions") or {}
        node = next((child for child in group.get("children") or []
                     if child.get("action") == "note_application"), None)
        self.write_stage(node or {"id": "action.note_application"}, action, command, target="")
        if self.writes and "failed" not in self.message:
            self.message = "Note saved"

    # ---- application packets: write one, then review it -------------------

    def packet_root(self):
        return os.environ.get("JOBHUNTER_OUTPUT_DIR", os.path.join(WORKSPACE, "modules", "applications", "packages"))

    def _generate(self):
        import generate as generatemod  # tools/ is on sys.path when ui runs
        return generatemod

    def packet_ready(self, row_id):
        """True only when the packet's meta.json parses and names this listing."""
        if row_id in self.generated_packets or row_id in self.packets:
            return True
        try:
            return bool(self._generate().packet_ready(self.packet_root(), str(row_id)))
        except Exception:  # noqa: BLE001 - a broken generator must not break the menu
            return False

    def describe_packet_action(self, action_id, row):
        """`R` writes the packet, `V` opens the review window."""
        row_id = str((row or {}).get("id") or "")
        if not row_id:
            self.message = "select a listing row first"
            return
        if action_id == "view_packet":
            self.open_packet_view(row_id)
            return
        if self.generation is not None:
            self.message = "A cover letter is already being generated"
            return
        self.choose_generation_model(row, force=self.packet_ready(row_id))

    def choose_generation_model(self, row, force, model=None):
        if self.generation is not None:
            self.message = "A cover letter is already being generated"
            return
        self.generation_picker = {"row": row, "force": force,
                                  "model": model or self.generation_model}
        self.message = ""

    def generate_packet(self, row, force, model):
        """Run the generator, then move the stage through the single writer."""
        row_id = str((row or {}).get("id") or "")
        if not row_id:
            self.message = "select a listing row first"
            return
        command = action_command(self.spec, "generate_application", {"listing_id": row_id})
        if not command:
            self.message = "generate: no command declared"
            return
        command += " --language " + self.generation_language
        command += " --generation-model " + model
        if force:
            command += " --force"
        executor = self.executor
        if executor is None:            # read-only surface
            self.message = "run: " + command
            return
        if self.generation is not None:
            return

        def run():
            return executor(command, timeout=1250)

        if self.generation_submit is None:
            # An injected executor can run inline outside the interactive loop.
            self.finish_generation(row, force, model, _generation_future(run, background=False))
        else:
            self.generation_frame = 0
            self.generation = (row, force, model, self.generation_submit(run))
            self.message = ""

    def generation_indicator(self, row_id):
        if self.generation is not None and str(self.generation[0].get("id")) == str(row_id):
            return "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[self.generation_frame % 10] + " Generating…"
        return ""

    def poll_generation(self):
        """Only the terminal loop may apply a completed result to navigation state."""
        if self.generation is None or not self.generation[-1].done():
            return False
        pending, self.generation = self.generation, None
        self.finish_generation(*pending)
        return True

    def finish_generation(self, row, force, model, future):
        row_id = str(row["id"])
        still_here = ((self.active_tab() or {}).get("id") == "tab.listings"
                      and self.acting_row_id() == row_id
                      and not (self.settings_open or self.tab_menu or self.show_help
                               or self.prompt or self.popup or self.generation_picker))
        try:
            result = future.result()
        except Exception as exc:  # noqa: BLE001 - surfaced in the message line
            self.message = "Generate failed: %s" % exc
            return
        output = ((getattr(result, "stdout", "") or "") +
                  (getattr(result, "stderr", "") or "")).strip()
        code = getattr(result, "returncode", 0) or 0
        if code:
            if not force and "a packet already exists for %s;" % row_id in output:
                # The server can have a packet that has not synced to this client.
                self.generated_packets.add(row_id)
                if still_here:
                    self.choose_generation_model(row, force=True, model=model)
                else:
                    self.message = "A cover letter already exists for %s" % (row.get("title") or row_id)
                return
            first = output.splitlines()[0] if output else "exit %s" % code
            self.message = "Generate failed: %s. Use %s to choose a model and retry." % (
                first.rstrip("."), self._generate_key())
            return
        self.writes += 1
        self.generated_packets.add(row_id)
        self.mark_generated(row)
        status = self.message
        self.packets.pop(row_id, None)
        if not still_here:
            self.message = "%s: %s" % (row.get("title") or row_id, status)
        elif not self.open_packet_view(row_id, fresh=True):
            self.message = "%s · %s" % (status, self.message)
        else:
            self.message = status

    def mark_generated(self, row):
        """Move the stage through the writer, so the tracker agrees with the packet."""
        action = (self.spec.get("actions") or {}).get("stage_generated") or {}
        group = _find_projected(self.tab_children, "listings.actions") or {}
        node = next((child for child in group.get("children") or []
                     if child.get("action") == "stage_generated"), None)
        command = action_command(self.spec, "stage_generated",
                                 _action_values(action, row, self.ctx))
        if node is None or not command:
            self.message = "Application written · no stage command declared"
            return
        row_id = str(row["id"])
        keep = (self.focused() or {}).get("id")
        echo = self.write_stage(node, action, command, target="generated", row_id=row_id)
        # Updating a row can remove it from the active stage filter. Keep the
        # current selection by id rather than by its old index in that list.
        for index, entry in enumerate(self.entries()):
            if entry["id"] == keep:
                self.focus = index
                break
        if echo and echo.get("stage") == "generated":
            self.message = "Application written · stage %s" % stage_label(self.spec, "generated")
        elif echo is not None:
            self.message = "Application written · tracker did not confirm the generated stage"
        else:
            self.message = "Application written · " + self.message

    def open_packet_view(self, row_id, fresh=False):
        """Open the packet as a child of its listing, not a dismiss-on-any-key popup."""
        packet = self.packets.get(row_id)
        if packet is None and not fresh:
            try:
                packet = self._generate().read_packet(self.packet_root(), row_id)
            except (OSError, ValueError, AttributeError):
                packet = None
        if not packet and self.executor is not None:
            # Fetch on demand, not during paints. A server-written packet may
            # not be synced yet (and a regenerated one may still be stale).
            try:
                command = "python3 -B tools/dashboard.py packet %s --json" % shlex.quote(row_id)
                result = self.executor(command)
                if result.returncode == 0:
                    packet = json.loads(result.stdout)
                elif result.returncode != 4:
                    raise ValueError((result.stderr or "exit %s" % result.returncode).splitlines()[0])
            except Exception as exc:  # noqa: BLE001 - surfaced in the notice
                self.message = "Cannot open application packet: %s" % exc
                return False
        meta = packet.get("meta.json") if isinstance(packet, dict) else None
        if not isinstance(meta, dict) or meta.get("listing_id") != row_id:
            self.message = ("no application packet for this listing yet; "
                            "press %s to write one" % self._generate_key())
            return False
        self.packets[row_id] = packet
        self.expanded_id = row_id
        self.detail_card = self.detail_para = 0
        self.detail_level = "elements"
        self.detail_follow = True
        self.packet_view = packet
        self.packet_pane = "letter"
        self.packet_block = 0
        self.packet_follow = False
        self.packet_scroll = dict.fromkeys(self.packet_scroll, 0)
        self.message = ""
        return True

    def handle_packet_key(self, key):
        if key == ",":
            self.settings_open = True
            self.message = ""
            return
        if key in ("h", "left") and self.packet_pane == "notes":
            self.packet_pane = "letter"
            self.packet_follow = True
            return
        if key in ("h", "left", "escape", "q"):
            self.packet_view = None
            self.message = ""
        elif key == "i":
            self.packet_pane = "letter" if self.packet_pane == "details" else "details"
        elif key == "c":
            self.packet_terminal_action = ["cv"]
        elif key == "v" and self.expanded_id:
            self.packet_terminal_action = ["versions", self.expanded_id]
        elif key == "e" and self.packet_pane != "details":
            from review import digest

            blocks = _packet_blocks(self.packet_view)
            if self.packet_block >= len(blocks):
                self.message = "No saved cover-letter block to revise"
            elif not self.expanded_id:
                self.message = "No listing selected"
            else:
                self.packet_terminal_action = ["revise", self.expanded_id, str(self.packet_block + 1),
                                               digest(blocks[self.packet_block])]
        elif key in ("l", "right", "tab", "shift_tab") and self.packet_pane != "details":
            self.packet_pane = ("letter" if self.packet_pane == "notes" and key in ("tab", "shift_tab")
                                else "notes")
            self.packet_follow = True
        elif key in ("j", "k") and self.packet_pane != "details":
            blocks = _packet_blocks(self.packet_view)
            self.packet_block = max(0, min(len(blocks) - 1, self.packet_block + (1 if key == "j" else -1)))
            self.packet_follow = True
            self.message = ""
        elif key in ("y", "Y") and self.packet_pane != "details":
            self.copy_packet_text(whole=key == "Y")
        elif key in ("j", "down", "k", "up", "page_down", "page_up", "wheel_down", "wheel_up"):
            self.packet_follow = False
            step = {"page_down": 10, "page_up": -10, "wheel_down": 3, "wheel_up": -3}
            self.packet_scroll[self.packet_pane] = max(0, self.packet_scroll[self.packet_pane] + step.get(
                key, 1 if key in ("j", "down") else -1))
        elif key == self._generate_key():
            row = (self.focused() or {}).get("row") or {}
            if row.get("id") == self.expanded_id:
                self.choose_generation_model(row, force=True)
        elif key == "M":
            self.mouse = not self.mouse

    def copy_packet_text(self, whole=False):
        packet = self.packet_view or {}
        if whole:
            text = "\n".join(line.removeprefix("# ") for line in
                             str(packet.get("cover-letter.md") or "").strip().splitlines())
        else:
            blocks = _packet_blocks(packet)
            block = blocks[self.packet_block] if self.packet_block < len(blocks) else {}
            text = block.get("text", "").removeprefix("# ")
            if block.get("why"):
                excerpt = block.get("excerpt") or text
                text = f"Cover-letter block:\n{text}\n\nExcerpt: {excerpt}\nWhy this wording: {block['why']}"
        if not text:
            self.message = "No letter text to copy"
            return
        if self.clipboard is None:
            self.message = "Clipboard unavailable"
            return
        try:
            method = self.clipboard(text)
        except (OSError, subprocess.SubprocessError) as exc:
            self.message = "Copy failed: %s" % exc
            return
        label = "letter" if whole else "review block"
        self.message = ("Copy requested for %s; paste to check" if method == "terminal" else "Copied %s") % label


    def _generate_key(self):
        group = _find_projected(self.tab_children, "listings.actions") or {}
        for child in group.get("children") or []:
            if child.get("action") == "generate_application":
                return (child.get("keys") or {}).get(self.surface) or "R"
        return "R"

    def open_outcome_prompt(self, row):
        """Ask which outcome follows the last flow step instead of guessing one."""
        actions = _find_projected(self.tab_children, "listings.actions") or {}
        lines, choices = [], {}
        for index, key in enumerate(OUTCOME_CHOICES, start=1):
            node = next((child for child in actions.get("children") or []
                         if child.get("action") == "stage_" + key), None)
            if node is None:
                continue
            choices[str(index)] = node
            lines.append("%d  %s" % (index, stage_label(self.spec, key)))
        if not choices:
            self.message = "no outcome action is declared for this surface"
            return
        self.popup = ("Outcome after %s" % stage_label(self.spec, (row or {}).get("stage")),
                      lines + ["", "The pipeline ends here: what did the employer do?"],
                      "1-%d choose · esc cancel" % len(choices))
        self.popup_choices = choices

    def describe_action(self, node):
        action_id = node.get("action")
        action = (self.spec.get("actions") or {}).get(action_id) or {}
        # Bind the write to the row under the cursor, never to a stale selection.
        entry = self.focused() or {}
        row = entry.get("row") if entry.get("kind") == "item" else self.selected_row()
        if action.get("scope") == "selection" and not row:
            self.message = "select a listing row first (%s)" % node_label(self.spec, node)
            return
        if action_id in ("generate_application", "view_packet"):
            self.describe_packet_action(action_id, row)
            return
        if action_id == "note_application":
            self.open_note_prompt(row)
            return
        if any(t.get("channel") == "browser-local" and t.get("kind") == "local"
               for t in action.get("transports") or []):
            self.message = "Bookmark is stored in the browser only; use the browser view."
            return
        browser = next((transport for transport in action.get("transports") or []
                        if transport.get("channel") == "browser"
                        and transport.get("kind") == "url"), None)
        if browser:
            values = _action_values(action, row, self.ctx)
            url = browser.get("template") or ""
            for name, value in values.items():
                url = url.replace("{%s}" % name, str(value))
            if not _safe_ams_url(url):
                self.message = "No safe AMS link on this listing"
                return
            try:
                opened = self.browser_opener(url, new=2)
            except Exception as exc:  # noqa: BLE001 - surfaced to the user
                self.message = "Browser open failed: %s | %s" % (exc, url)
                return
            self.message = ("Opened AMS listing in browser" if opened else
                            "Could not launch browser; open: %s" % url)
            return
        values = _action_values(action, row, self.ctx)
        if (action_id.startswith("stage_")
                and action_id not in ("stage_next", "stage_prev")
                and row and values.get("stage") and values["stage"] == row.get("stage")):
            # The same stage key pressed twice turns the move back. The writer
            # remembers the step before, so pressing b on a listing that is
            # already blocked returns it to where it was instead of blocking it
            # again with nothing to show for the press.
            if self.undo_stage(node, row):
                return
        if action_id in ("stage_next", "stage_prev"):
            # The step is resolved against the row, so one key walks the whole
            # pipeline in either direction.
            stage = (row or {}).get("stage")
            if action_id == "stage_next" and stage_needs_outcome(self.spec, stage):
                self.open_outcome_prompt(row)
                return
            step = next_stage_key if action_id == "stage_next" else prev_stage_key
            target = step(self.spec, stage)
            if not target:
                edge = ("end of the pipeline" if action_id == "stage_next"
                        else "start of the pipeline")
                self.message = "%s is the %s" % (stage_label(self.spec, stage), edge)
                return
            values["stage"] = target
        command = action_command(self.spec, action_id, values)
        if not command:
            self.message = "%s: no terminal command declared" % node_label(self.spec, node)
            return
        if "{" in command:
            self.message = "%s: listing has no id" % node_label(self.spec, node)
            return
        local = next((transport for transport in action.get("transports") or []
                      if transport.get("channel") == "local"
                      and transport.get("kind") == "cli"), None)
        if local is None or self.executor is None:
            # Read-only surface (frame dump, static page): show the exact writer.
            self.message = "run: " + command
            return
        text = local.get("command") or command
        for name, value in values.items():
            text = text.replace("{%s}" % name, str(value))
        self.write_stage(node, action, text, target=str(values.get("stage") or ""))

    def undo_stage(self, node, row):
        """Ask the writer to step the row back to the stage before the last move."""
        undo = (self.spec.get("actions") or {}).get("undo_stage")
        if not undo:
            return False
        command = action_command(self.spec, "undo_stage",
                                 {"listing_id": str((row or {}).get("id") or "")})
        if not command or "{" in command:
            return False
        local = next((transport for transport in undo.get("transports") or []
                      if transport.get("channel") == "local"
                      and transport.get("kind") == "cli"), None)
        if local is None:
            return False
        text = local.get("command") or command
        text = text.replace("{listing_id}", str((row or {}).get("id") or ""))
        self.write_stage(node, undo, text, target="")
        return True

    def write_stage(self, node, action, command, target="", row_id=None):
        """Run one local write, then reload so the gauge and the card follow it."""
        label = action.get("label") or node_label(self.spec, node)
        if self.executor is None:            # read-only surface
            self.message = "run: " + command
            return
        targeted = row_id is not None
        if not targeted:
            row_id = self.acting_row_id()
        try:
            result = self.executor(command)
        except Exception as exc:  # noqa: BLE001 - surfaced in the message line
            self.message = "%s failed: %s" % (label, exc)
            return
        output = ((getattr(result, "stdout", "") or "") +
                  (getattr(result, "stderr", "") or "")).strip()
        code = getattr(result, "returncode", 0) or 0
        if code:
            first = output.splitlines()[0] if output else "exit %s" % code
            self.message = "%s failed: %s" % (label, first)
            return
        self.writes += 1
        stage = stage_label(self.spec, target) if target else ""
        if targeted:
            # Background completion already has a specific ID. No feed reload
            # (potentially another SSH hop) is needed to reflect the writer echo.
            payload = _writer_echo(output)
            if str(payload.get("uuid") or payload.get("id") or "") != row_id:
                return {}
            return self.apply_write_echo(row_id, output)
        reloaded = self.reload_after_write()
        # The writer answers with its own record of the change. Fold that into
        # the row, so a surface reading a feed file (the Mac client, the
        # browser) shows the star or the gauge the key just moved instead of
        # waiting for the next rebuild to reach it.
        echo = self.apply_write_echo(row_id, output)
        if isinstance(echo.get("bookmarked"), bool):
            self.message = "Bookmark %s · written to the tracker" % (
                "on" if echo["bookmarked"] else "off")
            return
        if action.get("undo"):
            back = stage_label(self.spec, echo.get("stage")) if echo.get("stage") else ""
            self.message = ("Back at %s · the stage key was pressed twice" % back if back
                            else "Undone · " + (echo.get("stage") or "no stage left"))
            return
        if not reloaded:
            self.message = "%s %s; reload the feed with f" % (stage or "Write", label)
            return
        self.message = "%s · %s" % (stage or "Written", label)

    def acting_row_id(self):
        """The id of the row the key acted on, whichever view has focus."""
        entry = self.focused() or {}
        row = entry.get("row") if entry.get("kind") == "item" else self.selected_row()
        return str((row or {}).get("id") or self.selected_id or "")

    def apply_write_echo(self, row_id, output):
        """Copy the writer's own JSON answer into the row it reported on.

        Only fields the tracker printed are copied, so a stale feed file cannot
        invent a change the writer never confirmed.
        """
        payload = _writer_echo(output)
        if not payload:
            return {}
        ident = str(payload.get("uuid") or payload.get("id") or row_id or "")
        if not ident:
            return {}
        for row in self.ctx.get("rows") or []:
            if str(row.get("id") or "") != ident:
                continue
            changed = {}
            for key in _ECHO_FIELDS:
                if key in payload:
                    row[key] = payload[key]
                    changed[key] = payload[key]
            if "stage" in changed:
                label = stage_label(self.spec, changed["stage"])
                row["stage_label"] = label
                row["stage_short"] = stage_entry(self.spec, changed["stage"]).get("short") or label
            if "status" in changed:
                row["status_label"] = status_label(self.spec, changed["status"])
            return changed
        return {}

    def click_at(self, x, y):
        """Fire the button pill under a click, when the pointer lands on one.

        Only rows painted by the action band are eligible. The pill's key and
        label, even on a continuation row, run the same action as the keyboard.
        """
        lines = self.last_frame or []
        index = y - 1
        if index < 0 or index >= len(lines):
            return False
        line = _unstyled(lines[index])
        if index not in self.action_rows:
            return False
        for item in _action_entries(self):
            if not item.get("band", True) or not item.get("key"):
                continue
            needle = "%s %s" % (item["key"], item["short"])
            start = line.find(needle)
            if start < 0:
                continue
            # The mark and the padding around the label belong to the pill too.
            if start - 2 <= x - 1 <= start + len(needle) + 1:
                self.handle(item["key"])
                return True
        return False

    def reload_after_write(self):
        """Rebuild the context from disk, keeping the cursor on the same listing."""
        if self.loader is None:
            return False
        focused = self.focused() or {}
        keep = ((focused.get("row") or {}).get("id") or self.selected_id or "")
        try:
            ctx = self.loader()
        except Exception:  # noqa: BLE001 - the write already landed
            return False
        self.ctx = ctx
        self.reload_projection()
        self.focus_first_row()
        if keep:
            for index, entry in enumerate(self.entries()):
                row = entry.get("row") or {}
                if row.get("id") == keep or str(entry.get("id") or "").endswith(":" + keep):
                    self.focus = index
                    self.selected_id = keep
                    break
        return True

    # -- keys -------------------------------------------------------------
    def theme_label(self):
        return theme_label()

    def cycle_theme(self, step=1):
        name = next_theme(step)
        self.message = "theme: %s" % theme_label()
        if self.persist_theme and not save_theme(name):
            self.message += " (not saved)"

    def hotkey(self, key):
        entries = self.entries()
        for index, entry in enumerate(entries):
            if (entry["node"].get("keys") or {}).get(self.surface) == key:
                self.focus = index
                self.activate(entry)
                return True
        return False

    # -- one-time explanation --------------------------------------------
    def wheel(self, direction, step=3):
        """Wheel scrolls this window: detail lines, or one card in the list."""
        if self.expanded_id is not None:
            self.detail_follow = False
            self.detail_scroll = max(0, self.detail_scroll + direction * step)
        elif self.entries():
            self.focus = max(0, min(len(self.entries()) - 1, self.focus + direction))
        return

    # -- detail ladder ----------------------------------------------------
    def reset_detail_state(self):
        """Back to the top of the ladder: first card, nothing folded."""
        self.detail_level = "elements"
        self.detail_card = 0
        self.detail_para = 0
        self.detail_follow = True

    def detail_spans(self):
        """Cards of the open listing, measured with the current folds."""
        _, _, spans = _listing_detail_lines(self.spec, self, self.detail_width + 4)
        return spans

    def detail_card_span(self, spans=None):
        spans = self.detail_spans() if spans is None else spans
        if not spans:
            return None
        self.detail_card = max(0, min(self.detail_card, len(spans) - 1))
        return spans[self.detail_card]

    def detail_cursor_span(self):
        """The lines the cursor points at: a paragraph, or the whole card."""
        card = self.detail_card_span(self.detail_sections or None)
        if card is None:
            return None
        if self.detail_level == "paragraphs" and card.get("paragraphs"):
            blocks = card["paragraphs"]
            low, high = blocks[min(max(self.detail_para, 0), len(blocks) - 1)]
            return {"start": low, "end": high}
        return {"start": card["start"], "end": card["end"]}

    def detail_window_start(self, start, height):
        """Keep the cursor on screen without fighting a manual scroll."""
        if not self.detail_follow or height <= 0:
            return start
        span = self.detail_cursor_span()
        if span is None:
            return start
        # The bottom row can be replaced by the "scroll for more" edge, so the
        # cursor has to land two lines above the window floor: one row for the
        # edge itself, one for the line it would have covered.
        limit = max(1, height - 2)
        if span["start"] < start:
            return span["start"]
        if span["end"] > start + limit:
            if span["end"] - span["start"] >= limit:
                return span["start"]
            return span["end"] - limit
        return start

    def detail_move(self, step):
        """j/k: cards at the element level, paragraphs inside one card."""
        spans = self.detail_spans()
        if not spans:
            return
        if self.detail_level == "paragraphs":
            card = self.detail_card_span(spans) or {}
            count = len(card.get("paragraphs") or [])
            if not count:
                self.detail_level = "elements"
                return
            self.detail_para = max(0, min(self.detail_para + step, count - 1))
            self.detail_follow = True
            self.message = "%s  ·  paragraph %d of %d" % (card["title"],
                                                          self.detail_para + 1, count)
            return
        previous = self.detail_card
        self.detail_card = max(0, min(self.detail_card + step, len(spans) - 1))
        self.detail_para = 0
        self.detail_follow = True
        if self.detail_card != previous:
            self.message = ""

    def detail_enter(self):
        """Right/l/enter only steps in: into the card, then into its paragraphs."""
        spans = self.detail_spans()
        if not spans:
            self.close_detail()
            return
        if self.detail_level == "paragraphs":
            self.message = "already inside %s  ·  h leaves it, space folds it" % (
                (self.detail_card_span(spans) or {}).get("title") or "the section")
            return
        card = self.detail_card_span(spans)
        if card is None:
            return
        if card["id"] == "application-packet":
            self.message = "press ; to open the review window"
            return
        if card["folded"]:
            # A folded card has nothing to step into, so opening it comes first.
            self.detail_folded.discard(card["id"])
            self.detail_follow = True
            card = self.detail_card_span(self.detail_spans()) or card
        if not card.get("paragraphs"):
            self.message = "%s has no text to step through" % card["title"]
            return
        self.detail_level = "paragraphs"
        self.detail_para = 0
        self.detail_follow = True
        self.message = "inside %s  ·  %d paragraphs  ·  h leaves it" % (
            card["title"], len(card["paragraphs"]))

    def detail_leave(self, reason=""):
        """Left/h: out of the card first, then out of the listing."""
        if self.detail_level == "paragraphs":
            card = self.detail_card_span() or {}
            self.detail_level = "elements"
            self.detail_para = 0
            self.detail_follow = True
            self.message = "out of %s" % (card.get("title") or "the section")
            return
        self.close_detail(reason)

    def toggle_detail_fold(self):
        """Space is the only fold: it collapses or opens the card under the cursor."""
        spans = self.detail_spans()
        card = self.detail_card_span(spans) if spans else None
        if card is None:
            return
        if not card["foldable"]:
            self.message = "%s has nothing to fold" % card["title"]
            return
        if card["id"] in self.detail_folded:
            self.detail_folded.discard(card["id"])
            self.message = "unfolded: %s" % card["title"]
        else:
            self.detail_folded.add(card["id"])
            self.message = "folded: %s  ·  space unfolds it" % card["title"]
            if self.detail_level == "paragraphs":
                self.detail_level = "elements"
                self.detail_para = 0
        self.detail_follow = True

    def close_detail(self, reason=""):
        self.expanded_id = None
        self.detail_scroll = 0
        self.detail_folded.clear()
        self.detail_sections = []
        self.reset_detail_state()
        if reason:
            self.message = reason

    def handle_detail_key(self, key):
        """Keys that only mean something while a listing is open."""
        if key == ";":
            card = self.detail_card_span()
            if card is None or card["id"] != "application-packet":
                return False
            self.open_packet_view(str(self.expanded_id))
        elif key in ("left", "h", "escape"):
            self.detail_leave()
        elif key in ("right", "l", "enter"):
            self.detail_enter()
        elif key == "space":
            self.toggle_detail_fold()
        elif key in ("j", "k"):
            self.detail_move(1 if key == "j" else -1)
        elif key in ("down", "up", "page_down", "page_up"):
            self.detail_follow = False
            step = {"down": 1, "up": -1, "page_down": 10, "page_up": -10}[key]
            self.detail_scroll = max(0, self.detail_scroll + step)
        elif key == "q":
            self.close_detail()
        else:
            return False
        return True

    def _explain_pending(self):
        if not self.explain_once:
            return False
        return not os.path.exists(os.path.join(self.config_dir, "flags-explained"))

    def _mark_explained(self):
        if not self.explain_once:
            return
        try:
            os.makedirs(self.config_dir, exist_ok=True)
            with open(os.path.join(self.config_dir, "flags-explained"), "w",
                      encoding="utf-8") as handle:
                handle.write("shown\n")
        except OSError:
            pass

    def _flag_explanations(self):
        node = (self.focused() or {}).get("node") or {}
        lines = []
        for flag in node.get("flags") or []:
            meaning = str(flag.get("meaning") or "").strip()
            label = str(flag.get("label") or "").strip()
            if label:
                lines.append("%s: %s" % (label, meaning.lower() or "no further detail"))
        return lines or ["No flags are defined for this list."]

    def settings_choices(self):
        return {**{str(i): ("generation_language", code)
                   for i, code in enumerate(GENERATION_LANGUAGES, 1)},
                **{str(i): ("generation_model", code)
                   for i, code in enumerate(GENERATION_MODELS, 1 + len(GENERATION_LANGUAGES))}}

    def handle_settings_key(self, key):
        if key in ("escape", "h", "left", ",", "q"):
            self.settings_open = False
            return
        choice = self.settings_choices().get(key)
        if choice is None:
            return
        attr, code = choice
        if self.persist_settings:
            try:
                save_generation_setting(self.config_dir, attr.replace("_", "-"), code)
            except OSError as exc:
                self.message = "Settings could not be saved: %s" % exc
                return
        setattr(self, attr, code)
        self.message = ""

    def handle(self, key):
        if self.generation_picker is not None:
            choice = self.generation_picker
            models = tuple(GENERATION_MODELS)
            keys = {str(i): code for i, code in enumerate(models, 1)}
            if key in keys:
                choice["model"] = keys[key]
                return
            if key in ("up", "down", "j", "k"):
                step = 1 if key in ("down", "j") else -1
                choice["model"] = models[(models.index(choice["model"]) + step) % len(models)]
                return
            self.generation_picker = None
            # A yank key must never confirm replacement inside the review.
            if key == "enter" or (key == "y" and choice["force"] and self.packet_view is None):
                self.generate_packet(**choice)
            else:
                self.message = "regeneration cancelled" if choice["force"] else "generation cancelled"
            return
        if isinstance(key, tuple) and key and key[0] == "click":
            self.click_at(key[1], key[2])
            return
        if self.settings_open:
            self.handle_settings_key(key)
            return
        if self.packet_view is not None:
            self.handle_packet_key(key)
            return
        if self.popup is not None:
            node = self.popup_choices.get(key)
            self.popup = None
            self.popup_choices = {}
            self._mark_explained()
            if node is not None:
                if node.get("kind") == "filter":
                    self.apply_filter(node)
                else:
                    self.describe_action(node)
            return
        if self.tab_menu:
            if key in ("escape", "ctrl_t", "enter", "q"):
                self.tab_menu = False
                return
            if len(key) == 1 and key.isdigit():
                index = int(key) - 1
                if 0 <= index < len(self.tabs):
                    if index != self.tab_index:
                        self.tab_index = index
                        self.focus = 0
                        self.expanded_id = None
                        self.message = "tab: %s" % node_label(self.spec, self.tabs[index])
                    else:
                        self.message = ""
                self.tab_menu = False
            return
        if key == "ctrl_t":
            self.tab_menu = True
            return
        if self.prompt == "query":
            if key == "enter":
                self.query = self.prompt_text
                self.prompt = None
                self.focus = 0
                self.message = "search: %s" % (self.query or "(cleared)")
            elif key == "escape":
                self.prompt = None
                self.message = "search cancelled"
            elif key == "backspace":
                self.prompt_text = self.prompt_text[:-1]
            elif len(key) == 1 and key.isprintable():
                self.prompt_text += key
            return
        if self.prompt == "note":
            if key == "enter":
                text = self.prompt_text.strip()
                self.prompt = None
                if text:
                    self.save_note(text)
                else:
                    self.message = "note cancelled"
            elif key == "escape":
                self.prompt = None
                self.message = "note cancelled"
            elif key == "backspace":
                self.prompt_text = self.prompt_text[:-1]
            elif len(key) == 1 and key.isprintable():
                self.prompt_text += key
            return
        if self.show_help:
            self.show_help = False
            if key in ("q", "?", "enter"):
                return
        if key == ",":
            self.settings_open = True
            self.message = ""
            return
        if key in ("wheel_up", "wheel_down"):
            self.wheel(-1 if key == "wheel_up" else 1)
            return
        if key == "mouse_other":
            return
        if key in ("t", "T"):
            self.cycle_theme(-1 if key == "T" else 1)
            return
        if (self.active_tab() or {}).get("id") == "tab.listings":
            if self.expanded_id is not None and self.handle_detail_key(key):
                # Cards and their paragraphs own the arrows while a listing is
                # open; stage keys, v, filters and tabs still fall through.
                return
            if key in ("left", "h", "escape"):
                self.expanded_id = None
                return
            source = _find_projected(self.tab_children, "action.open_source")
            source_key = ((source or {}).get("keys") or {}).get(self.surface)
            if source_key and key == source_key:
                self.focused()
                self.describe_action(source)
                return
            for group_id in ("listings.filters.status", "listings.filters.stage",
                             "listings.filters.scope"):
                group = _find_projected(self.tab_children, group_id)
                for node in (group or {}).get("children") or []:
                    node_key = (node.get("keys") or {}).get(self.surface)
                    if node.get("kind") == "filter" and node_key and key == node_key:
                        self.apply_filter(node)
                        return
            # Stage keys work on the focused row from the list as well: the
            # action binds to that row, so `n`, `p`, `b`, `U` and `K` never need
            # the detail view first.
            actions = _find_projected(self.tab_children, "listings.actions") or {}
            for node in actions.get("children") or []:
                if (node.get("keys") or {}).get(self.surface) == key:
                    self.describe_action(node)
                    return
            if key in ("right", "l", "enter", "space"):
                self.activate(self.focused())
                return
            if key == "q" and self.expanded_id is not None:
                self.expanded_id = None
                return
        if key in ("down", "j"):
            self.focus += 1
        elif key in ("up", "k"):
            self.focus -= 1
        elif key == "page_down":
            self.focus += 10
        elif key == "page_up":
            self.focus -= 10
        elif key == "G":
            self.focus = max(0, len(self.entries()) - 1)
        elif key == "g":
            if self.pending_g:
                self.focus = 0
                self.pending_g = False
            else:
                self.pending_g = True
            return
        elif key == "home":
            self.focus = 0
        elif key == "end":
            self.focus = max(0, len(self.entries()) - 1)
        elif key == "right" or key == "l":
            self.activate(self.focused())
        if key == "M":
            # `m` is the generated stage, so the mouse hand-off lives on M.
            self.mouse = not self.mouse
            self.message = ("mouse on: wheel scrolls, a click on a button runs it "
                            "(M off to select text again)"
                            if self.mouse else
                            "mouse off: drag to select and copy, M to re-enable the wheel and clicks")
            return
        if key == "ctrl_t" or key == "\\x14":
            entry = self.focused()
            if entry:
                if entry["kind"] in ("group", "view") and entry["node"].get("children") \
                        and entry["node"].get("id") not in self.collapsed:
                    self.collapsed.add(entry["node"].get("id"))
                else:
                    self.go_parent(entry)
        elif key in ("enter", "space"):
            self.activate(self.focused())
        elif key in ("tab", "]"):
            self.switch_tab(1)
        elif key in ("shift_tab", "["):
            self.switch_tab(-1)
        elif key == "?":
            self.show_help = True
        elif key == "/":
            self.prompt = "query"
            self.prompt_text = self.query
            self.message = "search: "
        elif key == "r":
            self.statuses.clear()
            self.stage_filter = ""
            self.latest_only = False
            self.bookmarked_only = False
            self.query = ""
            self.sort_key = self._default_sort()
            self.focus = 0
            self.message = "filters reset"
        elif key == "q":
            if self.query:
                self.query = ""
                self.message = "search cleared"
            elif self.collapsed:
                self.collapsed.clear()
                self.message = "all groups expanded"
            else:
                self.quit = True
        elif len(key) == 1 and self.hotkey(key):
            pass
        else:
            self.pending_g = False
        return

    def go_parent(self, entry):
        entries = self.entries()
        parent = entry.get("parent")
        if not parent:
            self.focus = 0
            return
        for index, candidate in enumerate(entries):
            if candidate["id"] == parent:
                self.focus = index
                return
        self.focus = 0


def nav_tabbar(nav, width):
    pills = []
    for index, tab in enumerate(nav.tabs):
        label = str(tab.get("label") or tab.get("id"))
        active = index == nav.tab_index
        plain = (" %s%s " % ("▸ " if active else "", _terminal_text(label))).replace("  ", " ")
        role = "info" if active else "neutral"
        pills.append((plain, [(plain, _button_sgr(role, active))]))
    return _pack_pills(pills, max(1, width), gap=" ")


def _header_bits(nav):
    """The focused listing as header pieces: title, score and flags, stage."""
    entry = nav.focused()
    row = (entry or {}).get("row") or {}
    if not entry or not row:
        return None
    spec = nav.spec
    node = entry["node"]
    score_node = next((child for child in node.get("children") or []
                       if child.get("bind") == "listing.score"), None)
    score = bind_value(spec, nav.ctx, nav.surface, score_node, row) if score_node else None
    known = (isinstance(score, (int, float)) and not isinstance(score, bool)
             and math.isfinite(score))
    score_text = value_text(spec, nav.ctx, nav.surface, score) if known else "?"
    chips = [(score_text, (_score_sgr(score), "1"))]
    for flag in row_flags(spec, nav.ctx, nav.surface, node, row):
        chips.append((_terminal_text(flag["label"]), (_theme_fg(flag["role"]), "1")))
    stage = stage_strip(spec, row)
    return {"title": _plain_hyphens(entry.get("label") or ""),
            "chips": chips,
            "stage": stage}


def nav_header(spec, nav, width):
    """One line: app, tab and count, the listing, its score and flags, stage."""
    app = spec.get("app") or {}
    tab = nav.active_tab() or {}
    place = node_label(spec, tab) if tab else ""
    rows = nav.rows()
    left = " %s" % (app.get("title") or "UI")
    where = " | %s [%s]" % (place, format(len(rows), ",")) if place else \
        " | %s" % format(len(rows), ",")
    sep = " | "
    bits = _header_bits(nav)
    stage = (bits or {}).get("stage")
    busy = nav.generation_indicator(nav.expanded_id) if nav.expanded_id else ""
    if busy:
        plain, segments = stage or ("", [])
        progress = busy + ("  " if plain else "")
        stage = (progress + plain, [(progress, (_theme_fg("accent"),))] + segments)
    stage_plain = stage[0] if stage else ""
    budget = width - len(left) - len(where) - len(stage_plain) - 1
    pieces = [(_c(left, "1", _theme_fg("accent")), len(left)),
              (_c(where, _theme_fg("accent")), len(where))]
    if bits:
        title = bits["title"]
        tags = " ".join(text for text, _ in bits["chips"])
        cost = len(sep) + len(title) + (len(sep) + len(tags) if tags else 0)
        if title and cost > budget:
            keep = budget - len(sep) - (len(sep) + len(tags) if tags else 0) - 1
            title = title[:max(0, keep)] + "…" if keep > 8 else ""
        if title:
            pieces.append((_c(sep, _theme_fg("line")), len(sep)))
            pieces.append((_c(title, _theme_fg("fg")), len(title)))
        if tags and len(sep) + len(tags) <= budget - (len(sep) + len(title) if title else 0):
            pieces.append((_c(sep, _theme_fg("line")), len(sep)))
            pieces.append((" ".join(_c(text, *codes) for text, codes in bits["chips"]),
                           len(tags)))
    used = sum(length for _, length in pieces)
    if stage:
        pieces.append((" " * max(1, width - used - len(stage_plain)), 0))
        pieces.append(("".join(_c(text, *codes) for text, codes in stage[1]), len(stage_plain)))
    return [_fit_line("".join(text for text, _ in pieces), width)]


def _fit_line(line, width):
    """Trim a styled line to width without cutting an escape sequence apart."""
    plain = _SGR_RE.sub("", line)
    if len(plain) <= width:
        return line
    if not _COLOUR.get("on"):
        return plain[:width]
    out, visible, index = [], 0, 0
    while index < len(line) and visible < width:
        match = _SGR_RE.match(line, index)
        if match:
            out.append(match.group(0))
            index = match.end()
            continue
        out.append(line[index])
        index += 1
        visible += 1
    return "".join(out) + "\033[0m"


def _nav_filter_pills(nav):
    pills = []
    for group_id in ("listings.filters.status", "listings.filters.scope"):
        group = _find_projected(nav.tab_children, group_id)
        for node in (group or {}).get("children") or []:
            if node.get("kind") != "filter":
                continue
            bind = node.get("bind")
            if isinstance(bind, dict) and bind.get("vocabulary") == "statuses":
                active = bind.get("key") in nav.statuses
            elif bind == "filter.bookmarked_only" or (isinstance(bind, dict)
                                                       and bind.get("field") == "bookmarked"):
                active = nav.bookmarked_only
            else:
                active = nav.latest_only
            key = (node.get("keys") or {}).get(nav.surface, "")
            pills.append(_pill(node_label(nav.spec, node), key, active,
                               node.get("role", "neutral")))
    stage_group = _find_projected(nav.tab_children, "listings.filters.stage") or {}
    stage_node = next((node for node in stage_group.get("children") or []
                       if node.get("kind") == "filter"), None)
    if stage_node:
        selected = stage_entry(nav.spec, nav.stage_filter) if nav.stage_filter else {}
        stage_text = (selected.get("short") or selected.get("label")
                      or nav.stage_filter or "All")
        label = "%s: %s" % (stage_node.get("label") or "Stage", stage_text)
        key = (stage_node.get("keys") or {}).get(nav.surface, "")
        pills.append(_pill(label, key, bool(nav.stage_filter),
                           stage_node.get("role", "info")))
    return pills


def _short_action(label):
    """`Mark applied` reads as `applied` in a one-row band."""
    for prefix in ("Mark ", "Set "):
        if label.startswith(prefix):
            label = label[len(prefix):]
    if label.startswith("Open "):
        label = label[len("Open "):]
    return label.replace(" in browser", "")


def _action_entries(nav):
    """Key, label, lit state and role for every action on the listings tab."""
    group = _find_projected(nav.tab_children, "listings.actions") or {}
    row = (nav.focused() or {}).get("row") or nav.selected_row()
    current = str((row or {}).get("stage") or "")
    entries = []
    for node in group.get("children") or []:
        action_id = node.get("action")
        action = (nav.spec.get("actions") or {}).get(action_id) or {}
        key = (node.get("keys") or {}).get(nav.surface, "")
        role = action.get("role") or node.get("role", "neutral")
        if action_id == "open_source":
            active = _safe_ams_url((row or {}).get("url", ""))
            role = "info"
        elif action_id == "generate_application":
            active = bool(row)
        elif action_id == "stage_next":
            # Lit while a step is left to walk, or while the outcome question
            # is the next step.
            active = bool(row) and bool(next_stage_key(nav.spec, current)
                                        or stage_needs_outcome(nav.spec, current))
        elif action_id == "stage_prev":
            active = bool(row) and bool(prev_stage_key(nav.spec, current))
        else:
            # The button matching the current stage is the lit one; the rest
            # stay grey so the row reads as a gauge at a glance.
            target = next((arg.get("const") for arg in action.get("args") or []
                           if arg.get("name") == "stage"), "")
            active = bool(row) and target == current
        entries.append({"id": action_id, "key": key,
                        "label": node_label(nav.spec, node),
                        "short": _short_action(node_label(nav.spec, node)),
                        "active": bool(active), "role": role,
                        # `band: false` keeps a step out of the button row while
                        # its key still works inside the expanded listing.
                        "band": node.get("band") is not False})
    return entries


def _nav_action_pills(nav, short=False):
    return [_pill(item["short"] if short else item["label"], item["key"],
                  item["active"], item["role"]) for item in _action_entries(nav)]


def _nav_control_box(nav, width):
    pills = _nav_filter_pills(nav)
    source = _find_projected(nav.tab_children, "action.open_source") or {}
    row = (nav.focused() or {}).get("row") or nav.selected_row()
    if source:
        key = (source.get("keys") or {}).get(nav.surface, "")
        pills.append(_pill("Open AMS", key,
                           _safe_ams_url((row or {}).get("url", "")), "info"))
    lines = _pack_pills(pills, max(1, width - 4))
    active_count = (len(nav.statuses) + int(bool(nav.stage_filter)) + int(nav.latest_only)
                    + int(bool(nav.bookmarked_only)) + int(bool(nav.query)))
    title = "Filters · %d active" % active_count
    return _rounded_box(title, lines or ["No controls available"], width,
                        border_sgr=_theme_fg("border"))


def _city_sgr(name, colours):
    colour = colours.get(name or "")
    if not colour:
        return "2"
    hue, saturation, lightness = colour
    red, green, blue = colorsys.hls_to_rgb(hue / 360.0, lightness / 100.0,
                                          saturation / 100.0)
    return "38;2;%d;%d;%d" % (round(red * 255), round(green * 255), round(blue * 255))


def _card_cells(spec, nav, entry, city_colours):
    """(column key, plain text, sgr codes) for one listing card row.

    The keys let a page of cards share one set of column widths, so score, city,
    stage, pay and flags line up instead of drifting with the text length.
    """
    row = entry.get("row") or {}
    badge_node = next((child for child in entry["node"].get("children") or []
                       if (child.get("layout") or {}).get("tui") == "badge"
                       and child.get("bind") == "listing.score"), None)
    score = bind_value(spec, nav.ctx, nav.surface, badge_node, row) if badge_node else None
    score_text = (" %s " % value_text(spec, nav.ctx, nav.surface, score)
                  if isinstance(score, (int, float)) and not isinstance(score, bool)
                  and math.isfinite(score) else " ? ")
    location_node = next((child for child in entry["node"].get("children") or []
                          if child.get("bind") == "listing.location"), None)
    location = bind_value(spec, nav.ctx, nav.surface, location_node, row) if location_node else None
    city = _city_name(_plain_hyphens(location)) if location else ""
    city = city or "Location unknown"
    # A cap keeps one long place name from stretching every card; the full
    # address still shows in the detail view.
    city = _fit(city, 18)
    cells = [("score", score_text, (_score_sgr(score),)),
             ("city", " %s " % city, (_city_sgr(city, city_colours),))]
    strip = stage_strip(spec, row)
    if strip:
        cells.append(("stage", strip[0], tuple(strip[1][0][1] or ())))
    # The pay column exists on every card, filled or not, so the flags behind it
    # sit in the same place on each row of the list.
    cells.append(("pay", _listing_pay_text(row), ("2",)))
    for flag in row_flags(spec, nav.ctx, nav.surface, entry["node"], row):
        label = str(flag.get("label") or "").strip()
        if not label:
            continue
        codes = ((_theme_fg("warning") if label.upper() == "HOT"
                  else _theme_fg("accent")),)
        cells.append(("flag:" + label, " %s " % label, codes))
    return cells


def _card_widths(spec, nav, entries, city_colours):
    """Widest plain text per column across the cards on screen."""
    widths = {}
    for entry in entries:
        for key, plain, _codes in _card_cells(spec, nav, entry, city_colours):
            widths[key] = max(widths.get(key, 0), len(plain))
    return widths


def _listing_card(spec, nav, entry, index, width, city_colours, widths=None):
    row = entry.get("row") or {}
    focused = index == nav.focus
    title = _plain_hyphens(entry.get("label") or "(untitled)")
    star = "★ " if row.get("bookmarked") else ""
    busy = nav.generation_indicator(row.get("id"))
    heading = "%02d %s%s%s%s" % (index + 1, "▶ " if focused else "  ",
                                   busy + " · " if busy else "", star, title)
    cells = _card_cells(spec, nav, entry, city_colours)
    widths = widths or {}
    plain_parts, segments = [], []
    for key, plain, codes in cells:
        if plain_parts:
            plain_parts.append("  ")
            segments.append(("  ", None))
        # ljust on the shared width keeps the next column in the same place on
        # every card, whatever the city, stage or pay text happens to be.
        padded = plain.ljust(widths.get(key, len(plain)))
        plain_parts.append(padded)
        segments.append((padded, codes))
    body = ("".join(plain_parts).rstrip(), segments)
    return _rounded_box(heading, [body], width,
                        border_sgr=_theme_fg("accent") if focused else _theme_fg("line"),
                        title_sgr=("1", _theme_fg("accent")) if focused
                        else (_theme_fg("fg"),))


_WIDE_FIELDS = ("title", "group", "matched roles", "matched skills", "ams id",
                "date basis", "pay (ad, monthly or yearly)", "seen in latest ams scan",
                "stage")


def _property_lines(fields, width):
    """Two label/value columns when both cells fit, full width otherwise."""
    single = [line for label, value in fields
              for line in _text_lines("%s: %s" % (label, value), width)]
    if width < 78 or not fields:
        return single
    column = (width - 2) // 2
    short = [label for label, value in fields
             if label.lower() not in _WIDE_FIELDS and len(label) <= 15]
    key_width = min(15, max((len(label) for label in short), default=0) + 2)

    def cell(label, value):
        gap = " " * max(1, key_width - len(label) - 1)
        text = "%s:%s%s" % (label, gap, value)
        return text if len(text) <= column else None

    lines, pending = [], None
    for label, value in fields:
        if label.lower() in _WIDE_FIELDS:
            if pending:
                lines.append(cell(*pending) or ("%s: %s" % pending))
                pending = None
            lines.extend(_text_lines("%s: %s" % (label, value), width))
            continue
        left = cell(label, value)
        if left is None:
            if pending:
                lines.append(cell(*pending) or ("%s: %s" % pending))
                pending = None
            lines.extend(_text_lines("%s: %s" % (label, value), width))
            continue
        if pending is None:
            pending = (label, value)
            continue
        right = cell(*pending)
        lines.append(left + " " * (column - len(left)) + "  " + right)
        pending = None
    if pending:
        lines.append(cell(*pending) or ("%s: %s" % pending))
    return lines


def _band_rows(rows, width, leader="ACTIONS"):
    """Filled bar rows: background across the width, no frame, leader once."""
    lead = " %s " % _terminal_text(leader)
    out = []
    for index, (plain, segments) in enumerate(rows):
        prefix = lead if index == 0 else " " * len(lead)
        body = (prefix + plain)[:width]
        if _COLOUR.get("on"):
            head_codes = ("1", _theme_fg("accent")) if index == 0 else (_theme_fg("muted"),)
            line = _c(prefix, _bg_sgr("raised"), *head_codes)
            line += "".join(_c(text, _bg_sgr("raised"), *(codes or ()))
                            for text, codes in segments)
            pad = width - len(body)
            if pad > 0:
                line += _c(" " * pad, _bg_sgr("raised"))
            out.append(line)
        else:
            marker = "\u258c" if index == 0 else " "
            out.append(marker + body[:max(0, width - 1)])
    return out


def _action_bar(nav, width):
    """One filled band: the stage buttons, then the remaining jumps by key."""
    entries = _action_entries(nav)
    shown = [item for item in entries if item.get("band", True)]
    # The rest of the stage jumps live behind `?`: one short hint beats a
    # wrapped band, and the help list names every key anyway.
    jump = "  ? keys" if any(not item.get("band", True) and item["key"]
                             for item in entries) else ""
    head = [_pill(item["short"], item["key"], item["active"], item["role"])
            for item in shown] or [_pill("no action for this stage", "", False, "neutral")]
    plain = "  ".join(pill[0] for pill in head).strip()
    segments = [segment for pill in head for segment in pill[1]]
    if jump:
        segments.append(("  ", None))
        segments.append((jump, (_theme_fg("muted"),)))
        plain += jump
    rows = [(plain, segments)]
    if len(plain) > width - 9:
        rows = _pack_pills(head, max(20, width - 9), gap="  ")
        if jump:
            rows = rows + [(jump, [(jump, (_theme_fg("muted"),))])]
    return _band_rows(rows, max(20, width))


def _keyboard_line(lines, width):
    """Inline hint at the very bottom: no box, so it cannot read as content."""
    text = " · ".join(item for item in lines if item)
    text = text[:max(1, width)]
    if _COLOUR.get("on"):
        return [_c(text, _theme_fg("faint"))]
    return [text]


def _tab_menu_lines(nav, width, height):
    rows = []
    for index, tab in enumerate(nav.tabs, start=1):
        marker = "▶" if index - 1 == nav.tab_index else " "
        rows.append("%s %d  %s" % (marker, index, node_label(nav.spec, tab)))
    if not rows:
        rows = ["No tabs"]
    body = _rounded_box("Tabs", rows[:max(1, height - 4)], width)
    return body


def _popup_lines(nav, width, height):
    title, lines, hint = nav.popup
    content = []
    for line in lines:
        content.extend(_text_lines(line, max(1, width - 4)))
    content.append("")
    content.append(_terminal_text(hint))
    visible = content[:max(1, height - 6)]
    return _rounded_box(title, visible, width, border_sgr=_theme_fg("accent"))


def _packet_details_lines(packet, inner):
    meta = packet.get("meta.json") or {}
    stamp = str(meta.get("generated_at") or "").replace("T", " ")[:16]
    body = _text_lines("Model %s · %s" % (meta.get("model") or "unknown", stamp or "unknown time"), inner)
    for title, key in (("Documents", "documents.json"),
                       ("CV notes", "cv-notes.md"), ("Company", "company.md"),
                       ("Why this letter, why this channel", "rationale.md")):
        text = packet.get(key)
        if key == "documents.json" and isinstance(text, list):
            text = "\n".join(str(item.get("name") or "") +
                              (" - " + str(item["note"]) if item.get("note") else "")
                              for item in text if isinstance(item, dict))
        if isinstance(text, str) and text.strip():
            body.extend(["", *_text_lines(title, inner)])
            body.extend(line for paragraph in text.strip().splitlines()
                        for line in _text_lines(paragraph, inner))
    return body


def _packet_blocks(packet):
    review = letter_review(packet)
    if review:
        return review
    letter = packet.get("cover-letter.md")
    return [{"text": line} for line in letter.splitlines() if line.strip()] if isinstance(letter, str) else []


def _packet_view_lines(nav, width, height):
    packet = nav.packet_view
    notices = _text_lines(nav.message, width - 4) if nav.message else []
    size = max(1, height - 3 - len(notices))

    def pane(kind, title, body, columns, starts=()):
        active = nav.packet_pane == kind
        if nav.packet_follow and nav.packet_block < len(starts):
            target = starts[nav.packet_block]
            if not nav.packet_scroll[kind] <= target < nav.packet_scroll[kind] + size:
                nav.packet_scroll[kind] = target
        start = nav.packet_scroll[kind] = min(nav.packet_scroll[kind], max(0, len(body) - size))
        visible = body[start:start + size]
        label = ("› " if active else "") + title
        if len(body) > size:
            position = " · %d/%d" % (start + 1, len(body))
            if len(label + position) <= columns - 5:
                label += position
        return _rounded_box(label, visible + [""] * (size - len(visible)), columns,
                            border_sgr=_theme_fg("accent" if active else "border"))

    if nav.packet_pane == "details":
        content = pane("details", "Application packet", _packet_details_lines(packet, width - 4), width)
        hints = "h back · j/k scroll · i letter · c CV history"
    else:
        columns = (width - 1) * 55 // 100
        right = width - columns - 1
        letter = packet.get("cover-letter.md")
        paper, paragraphs = [], []
        for paragraph in (letter.strip().splitlines() if isinstance(letter, str) else []):
            subject = paragraph.startswith("# ")
            lines = _text_lines(paragraph.removeprefix("# "), columns - 6)
            selected = False
            if paragraph.strip():
                selected = len(paragraphs) == nav.packet_block
                paragraphs.append(len(paper))
            for index, line in enumerate(lines):
                line = ("› " if selected and not index else "  ") + line
                paper.append((line, ("1",)) if subject else line)
        paper = paper or _text_lines("No cover letter saved.", columns - 4)

        notes, cards, note_width = [], [], right - 4
        review = letter_review(packet)
        for index, block in enumerate(review, 1):
            cards.append(len(notes))
            excerpt = block.get("excerpt") or block["text"].removeprefix("# ")
            quote = _text_lines('“' + excerpt.strip() + '”', note_width - 4)
            why = _text_lines(block["why"], note_width - 4)
            # Nested cards are plain strings: the outer box owns ANSI styling.
            label = ("╭› %02d " if nav.packet_block == index - 1 else "╭─ %02d ") % index
            notes.append(label + "─" * (note_width - len(label) - 1) + "╮")
            notes.extend("│ " + line.ljust(note_width - 4) + " │" for line in quote + [""] + why)
            notes.extend(["╰" + "─" * (note_width - 2) + "╯", ""])
        notes = notes or _text_lines("No matching wording notes saved for this draft.", note_width)
        left_pane = pane("letter", "Cover letter", paper, columns, paragraphs)
        right_pane = pane("notes", "Why this wording" if right >= 23 else "Notes", notes, right, cards)
        content = [left + " " + note for left, note in zip(left_pane, right_pane)]
        hints = "e revise · v versions · c CV · h/l panes · j/k blocks · y copy · Y all"
    tools = ", model · %s regenerate" % nav._generate_key()
    if width >= 92:
        hints += " · i details"
    return content + notices + _keyboard_line([tools + " · " + hints], width)


def _keyboard_box(lines, message, width):
    content = []
    for line in lines:
        content.extend(_wrap(line, max(1, width - 4), ""))
    return _rounded_box("Keyboard", content, width, border_sgr=_theme_fg("line"),
                        title_sgr=("1", _theme_fg("muted")))

def _text_lines(value, width):
    """Wrap untrusted ad text without dropping long words or control filtering."""
    clean = _terminal_text(value)
    return textwrap.wrap(clean, width=max(1, width), break_long_words=True,
                         break_on_hyphens=False) or [""]

def _detail_blocks(content, starts):
    """(offset, line count) per paragraph; one block for the whole card."""
    if not content:
        return []
    bounds = sorted({offset for offset in starts if 0 <= offset < len(content)})
    if not bounds:
        return [(0, len(content))]
    blocks = []
    for index, offset in enumerate(bounds):
        end = bounds[index + 1] if index + 1 < len(bounds) else len(content)
        blocks.append((offset, max(1, end - offset)))
    return blocks


def _detail_card_title(title, focused, level, para_index, para_count):
    """Cursor marker that survives a colourless terminal."""
    if not focused:
        return title
    if level == "paragraphs" and para_count:
        return "%s  ·  %d/%d" % (title, min(para_index, para_count - 1) + 1, para_count)
    return title + "  ›"


def _listing_detail_lines(spec, nav, width):
    """Detail cards as one navigable ladder: a card, then its paragraphs.

    Every card reports the physical lines it occupies and the lines of each
    paragraph inside it, so the cursor can put the card or the paragraph it
    points at into the window. A folded card keeps its title and nothing else
    and reports no paragraphs, which is what makes it a leaf of the ladder.
    """
    entry = nav.focused()
    if not entry or not entry.get("row"):
        return _rounded_box("Listing detail", ["Listing no longer available"], width), [], []
    node, row = entry["node"], entry["row"]
    inner = width - 4
    sections = []
    if nav.packet_ready(str(row.get("id") or "")) or row.get("stage") == "generated":
        sections.append(("application-packet", "Application packet", ["; to open the review window"], []))
    # The header carries the stage gauge now; the detail keeps only the timing
    # note, folded into Properties so the ad text starts higher on the page.
    stage_line = stage_note(spec, row)

    fields = []
    if stage_line:
        fields.append(("Stage", stage_line))
    for child in node.get("children") or []:
        if (child.get("layout") or {}).get("tui") in ("preview", "badge"):
            continue
        raw = bind_value(spec, nav.ctx, nav.surface, child, row)
        if child.get("bind") == "listing.pay_monthly_min":
            raw = _listing_pay_cell(row)
        if isinstance(raw, bool):
            value = child.get("true_label") if raw else child.get("false_label")
        elif child.get("vocabulary") and raw:
            value = vocabulary_label(spec, child["vocabulary"], raw)
        else:
            value = value_text(spec, nav.ctx, nav.surface, raw)
        label = _terminal_text(child.get("label") or child.get("id"))
        fields.append((label, _terminal_text(value)))
        if child.get("id") == "field.posted" and row.get("posted_basis") == "extraction":
            fields.append(("Date basis", "Last seen in AMS scan"))
    fields.append(("AMS id", _terminal_text(row.get("id", ""))))
    sections.append(("properties", "Properties", _property_lines(fields, inner), []))

    full_text = row.get("full_text") or ""
    has_sections = bool(row.get("company_description") or row.get("job_description"))
    for child in node.get("children") or []:
        if (child.get("layout") or {}).get("tui") != "preview":
            continue
        if child.get("bind") == "listing.full_text" and has_sections:
            continue
        raw = bind_value(spec, nav.ctx, nav.surface, child, row)
        text = value_text(spec, nav.ctx, nav.surface, raw,
                          child.get("fallback", "Not captured from the AMS detail page."))
        content, starts = [], []
        for paragraph in str(text).splitlines() or [""]:
            if paragraph.strip() and raw:
                starts.append(len(content))
            content.extend(_text_lines(paragraph, inner) if paragraph.strip() else [""])
        title = str(child.get("label") or child.get("id"))
        sections.append((str(child.get("id") or title), title, content, starts))
    if not full_text and row.get("snippet"):
        content = [item for paragraph in row["snippet"].splitlines()
                   for item in _text_lines(paragraph, inner)]
        sections.append(("snippet", "AMS search card - incomplete", content, [0]))

    folds = getattr(nav, "detail_folded", None) or set()
    focused_index = int(getattr(nav, "detail_card", 0) or 0)
    level = getattr(nav, "detail_level", "elements")
    para_focus = int(getattr(nav, "detail_para", 0) or 0)
    accent = _theme_fg("accent")
    lines, anchors, spans = [], [], []
    for index, (section_id, title, content, paragraph_offsets) in enumerate(sections):
        if lines:
            lines.append("")
        # A card starts at the top border; scrolling there preserves the heading.
        start = len(lines)
        foldable = bool(content) and section_id != "application-packet"
        folded = foldable and section_id in folds
        here = index == focused_index
        blocks = [] if folded else _detail_blocks(content, paragraph_offsets)
        card_title = _detail_card_title(_terminal_text(title), here, level,
                                        para_focus, len(blocks))
        if folded:
            lines.extend(_rounded_box(
                card_title + "  (folded)", [], width,
                border_sgr=accent if here else _theme_fg("muted"),
                title_sgr=("1", accent) if here else ("1", _theme_fg("muted"))))
        else:
            body = list(content)
            if here and level == "paragraphs" and blocks:
                offset, count = blocks[min(para_focus, len(blocks) - 1)]
                for line_index in range(offset, min(offset + count, len(body))):
                    body[line_index] = (body[line_index], ("1", accent))
            lines.extend(_rounded_box(card_title, body, width,
                                      border_sgr=accent if here else None,
                                      title_sgr=("1", accent) if here else None))
        end = len(lines)
        body_top, body_bottom = start + 1, start + 1 + len(content)
        paragraphs = [(body_top + offset, min(body_top + offset + count, body_bottom))
                      for offset, count in blocks]
        stops = [0] if folded else (paragraph_offsets or [0])
        anchors.extend(start if offset == 0 else start + 1 + offset
                       for offset in stops)
        spans.append({"id": section_id, "title": title, "start": start, "end": end,
                      "folded": folded, "foldable": foldable,
                      "paragraphs": paragraphs})
    return lines, anchors, spans

_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")

def _detail_window(lines, start, size, width):
    """Keep clipped section borders legible in a short terminal viewport."""
    visible = list(lines[start:start + size])
    if not visible:
        return visible
    def edge(line):
        return _SGR_RE.sub("", line)[:1]
    inside = sum(edge(line) == "╭" for line in lines[:start]) - sum(
        edge(line) == "╰" for line in lines[:start])
    if inside > 0:
        visible[0] = _rounded_box("Section continued", [], width)[0]
    remaining_inside = inside + sum(edge(line) == "╭" for line in lines[start:start + size]) - sum(
        edge(line) == "╰" for line in lines[start:start + size])
    if remaining_inside > 0 and start + size < len(lines):
        label = " scroll for more "
        plain = "╰─" + label + "─" * max(0, width - 3 - len(label)) + "╯"
        visible[-1] = _c(plain, _theme_fg("border"))
    return visible


def nav_branch(spec, nav, width):
    chain = nav.branch()
    lines = [_fit(" /", width)]
    for entry in chain:
        lines.append(_fit("  " + entry["label"], width))
    return lines


def nav_siblings(spec, nav, width, height):
    entries = nav.siblings()
    focus = nav.focused()
    if focus is None or not entries:
        return ["(no entries)"]
    index = entries.index(focus)
    start = max(0, index - height // 2)
    view = entries[start:start + height]
    lines = []
    for offset, entry in enumerate(view):
        node = entry["node"]
        key = (node.get("keys") or {}).get(nav.surface, "")
        marker = ">" if index == start + offset else " "
        if entry["kind"] in ("group", "view") and node.get("children"):
            fold = "+" if node.get("id") in nav.collapsed else "-"
        else:
            fold = " "
        text = "%s%s " % (marker, fold) + entry["label"]
        if entry["kind"] == "action" and key:
            text += "  [%s]" % key
        elif key and entry["kind"] in ("filter", "query", "sort"):
            text += "  %s" % key
        lines.append(_fit(text, width))
    return lines


def nav_detail(spec, nav, width, paragraph_starts=None):
    entry = nav.focused()
    if not entry:
        return ["nothing focused"]
    node = entry["node"]
    row = entry.get("row")
    path = "/" + " / ".join(entry["path"][:-1]) if len(entry["path"]) > 1 else "/"
    lines = [_fit(path, width), ""]
    kind = entry["kind"]
    if kind == "item" and row is not None:
        flags = row_flags(spec, nav.ctx, nav.surface, node, row)
        score_node = next((child for child in node.get("children") or []
                           if (child.get("layout") or {}).get("tui") == "badge"
                           and child.get("bind") == "listing.score"), None)
        score = bind_value(spec, nav.ctx, nav.surface, score_node, row) if score_node else None
        score_badge = ("[%s] " % value_text(spec, nav.ctx, nav.surface, score)
                       if isinstance(score, (int, float)) and not isinstance(score, bool)
                       and math.isfinite(score) else "")
        if flags or score_badge:
            lines.append(_fit("  " + score_badge + " ".join("[%s]" % f["label"] for f in flags), width))
            if flags:
                lines.extend(_wrap("  " + ", ".join(f["meaning"] for f in flags if f.get("meaning")),
                                   width, "  "))
            lines.append("")
        for child in node.get("children") or []:
            label = child.get("label") or child.get("id")
            if (child.get("layout") or {}).get("tui") in ("preview", "badge"):
                continue
            raw = bind_value(spec, nav.ctx, nav.surface, child, row)
            if isinstance(raw, bool):
                value = child.get("true_label") if raw else child.get("false_label")
            elif child.get("vocabulary") and raw:
                value = vocabulary_label(spec, child["vocabulary"], raw)
            else:
                value = value_text(spec, nav.ctx, nav.surface, raw)
            if child.get("id") == "field.posted" and row.get("posted_basis") == "extraction":
                value = "%s (extraction)" % value
            lines.extend(_kv("  ", label, value, width, label_width=12))
        lines.extend(_kv("  ", "AMS id", row.get("id", ""), width, label_width=12))
        full_text = row.get("full_text") or ""
        has_sections = bool(row.get("company_description") or row.get("job_description"))
        preview_fields = [child for child in node.get("children") or []
                          if (child.get("layout") or {}).get("tui") == "preview"]
        for child in preview_fields:
            if child.get("bind") == "listing.full_text" and has_sections:
                continue
            raw = bind_value(spec, nav.ctx, nav.surface, child, row)
            text = value_text(spec, nav.ctx, nav.surface, raw,
                              child.get("fallback", "Not captured from the AMS detail page."))
            lines.append("")
            lines.append(_fit("  " + str(child.get("label") or child.get("id")), width))
            for paragraph in text.splitlines():
                if paragraph.strip() and paragraph_starts is not None:
                    paragraph_starts.append(max(0, len(lines) - 2))
                lines.extend(_wrap(paragraph, width, "  ") if paragraph.strip() else [""])
        snippet = row.get("snippet") or ""
        if not full_text and snippet:
            lines.append("")
            lines.append(_fit("  AMS search card, cut by AMS to 253 characters:", width))
            if paragraph_starts is not None:
                paragraph_starts.append(max(0, len(lines) - 2))
            lines.extend(_wrap(snippet, width, "  "))
        return lines
    if kind == "action":
        action_id = node.get("action")
        action = (spec.get("actions") or {}).get(action_id) or {}
        lines.extend(_kv("  ", "Action", action_id or "-", width, label_width=12))
        lines.extend(_kv("  ", "Scope", action.get("scope", "-"), width, label_width=12))
        command = action_command(spec, action_id, _action_values(action, nav.selected_row(), nav.ctx))
        lines.append("")
        lines.append(_fit("  " + (command or "(no command declared)"), width))
        lines.append("")
        lines.append(_fit("  enter prints it on the message line; nothing is written.", width))
        return lines
    if kind == "filter":
        bind = node.get("bind")
        if isinstance(bind, dict):
            state = "on" if bind.get("key") in nav.statuses else "off"
            lines.extend(_kv("  ", "Filter", "%s = %s" % (bind.get("key"), state), width, label_width=12))
        else:
            lines.extend(_kv("  ", "Filter", "%s = %s" % (bind, "on" if nav.latest_only else "off"),
                             width, label_width=12))
        if node.get("meaning"):
            lines.append("")
            lines.extend(_wrap(node["meaning"], width, "  "))
        return lines
    if kind in ("group", "view"):
        children = node.get("children") or []
        lines.extend(_kv("  ", "Children", len(children), width, label_width=12))
        keys = node.get("keys") or {}
        if keys.get(nav.surface):
            lines.extend(_kv("  ", "Key", keys[nav.surface], width, label_width=12))
        if node.get("meaning"):
            lines.append("")
            lines.extend(_wrap(node["meaning"], width, "  "))
        return lines
    value = fact_value(nav.ctx, nav.surface, node.get("bind"), row)
    if node.get("bind") and not isinstance(node.get("bind"), dict):
        lines.extend(_kv("  ", "Value", value_text(spec, nav.ctx, nav.surface, value), width, label_width=12))
    if node.get("meaning"):
        lines.append("")
        lines.extend(_wrap(node["meaning"], width, "  "))
    return lines


def render_listing_frame(spec, nav, width, height):
    """List-first cards and separate, paragraph-scrollable detail sections."""
    width = max(40, width)
    height = max(12, height)
    nav.action_rows = range(0)
    entries = nav.entries()
    if nav.expanded_id is None:
        nav.focused()
    header = nav_header(spec, nav, width)
    notice_text = nav.message
    if nav.prompt == "query":
        notice_text = "search: " + nav.prompt_text + "_"
    elif nav.prompt == "note":
        notice_text = "note: " + nav.prompt_text + "_"
    notice = (_rounded_box("Notice", _text_lines(notice_text, width - 4), width)
              if notice_text else [])
    notice_tail = ([""] + notice) if notice else []

    if nav.packet_view is not None:
        return header + _packet_view_lines(nav, width, height - len(header))
    if nav.show_help:
        keys_box = _rounded_box("Keys", help_lines(width - 4), width,
                                border_sgr=_theme_fg("accent"))
        hint = _keyboard_line(["any key closes · ? toggles this list"], width)
        return header + [""] + keys_box + hint
    if nav.tab_menu:
        body = _tab_menu_lines(nav, width, height)
        footer = _keyboard_line(["1-9 pick tab · esc close"], width)
        return header + [""] + body + footer
    if nav.popup is not None:
        body = _popup_lines(nav, width, height)
        hint = ("%d-%d choose · any other key cancels" % (1, len(nav.popup_choices))
                if nav.popup_choices else "any key closes")
        footer = _keyboard_line([hint], width)
        return header + [""] + body + footer

    if nav.expanded_id is None:
        controls = _nav_control_box(nav, width)
        footer = _keyboard_line([
            ", model/settings · j/k · l open · / find · M wheel · ? keys · q quit",
        ], width)
        fixed = len(header) + len(controls) + len(footer) + len(notice_tail) + 2
        available = max(3, height - fixed)
        card_count = max(1, available // 3)
        if entries:
            card_count = min(card_count, len(entries))
            start = max(0, min(nav.focus - card_count // 2, len(entries) - card_count))
            city_colours = _city_colours([row.get("location") for row in nav.ctx.get("rows") or []])
            visible = entries[start:start + card_count]
            widths = _card_widths(spec, nav, visible, city_colours)
            cards = [_listing_card(spec, nav, entry, index, width, city_colours, widths)
                     for index, entry in enumerate(visible, start)]
        else:
            cards = [_rounded_box("Listings · 0 matches",
                                  ["No listings match the current filters"], width,
                                  border_sgr=_theme_fg("line"))]
        return header + controls + [""] + \
            [line for card in cards for line in card] + notice_tail + [""] + footer

    if nav.detail_level == "paragraphs":
        footer = _keyboard_line([
            ", model · j/k paras · h out · space fold · v AMS · n next · ? keys · q back",
        ], width)
    else:
        footer = _keyboard_line([
            ", model · j/k sections · l in · h back · space fold · v AMS · n next · ? keys · q back",
        ], width)
    actions = _action_bar(nav, width)
    nav.detail_width = max(16, width - 4)
    detail_lines, nav.paragraph_starts, nav.detail_sections = _listing_detail_lines(
        spec, nav, width)
    content_height = max(3, height - len(header) - len(actions) - len(footer)
                         - len(notice_tail) - 3)
    start = min(nav.detail_scroll, max(0, len(detail_lines) - content_height))
    start = nav.detail_window_start(max(0, start), content_height)
    nav.detail_scroll = max(0, start)
    visible = _detail_window(detail_lines, start, content_height, width)
    # Clicks on wrapped continuation rows must work just like the leader row.
    action_start = len(header) + len(visible) + 2
    nav.action_rows = range(action_start, action_start + len(actions))
    return header + [""] + visible + [""] + actions + notice_tail + footer


def render_nav_frame(spec, nav, width=78, height=24):
    if nav.generation_picker is not None:
        nav.action_rows = range(0)
        choice = nav.generation_picker
        title = ("Regenerate" if choice["force"] else "Generate") + " cover letter"
        lines = [_fit(_terminal_text(choice["row"].get("title") or ""), width - 4)]
        for index, (code, preset) in enumerate(GENERATION_MODELS.items(), 1):
            marker = "▶" if code == choice["model"] else " "
            lines.extend(_text_lines("%s %d %s: %s" % (marker, index, preset.label, preset.model), width - 4))
        if choice["force"]:
            lines.append("Previous draft kept in Versions")
        lines.append("1-%d pick · Enter run · Esc cancel" % len(GENERATION_MODELS))
        return nav_header(spec, nav, width) + _rounded_box(title, lines, width)
    if nav.settings_open:
        nav.action_rows = range(0)
        choices = nav.settings_choices()
        lines = ["Generation Language: " + GENERATION_LANGUAGES[nav.generation_language],
                 " · ".join(key + " " + GENERATION_LANGUAGES[code] for key, (attr, code) in choices.items()
                            if attr == "generation_language"), "",
                 "Generation Model: " + GENERATION_MODELS[nav.generation_model].label]
        for key, (attr, code) in choices.items():
            if attr == "generation_model":
                preset = GENERATION_MODELS[code]
                lines.extend(_text_lines("%s %s: %s" % (key, preset.label, preset.model), width - 4))
        lines += ["", "h / Esc back"]
        if nav.message:
            lines.extend(_text_lines(nav.message, width - 4))
        return nav_header(spec, nav, width) + _rounded_box("Settings", lines, width)
    if (nav.active_tab() or {}).get("id") == "tab.listings":
        return render_listing_frame(spec, nav, width, height)
    header = nav_header(spec, nav, width)
    if nav.show_help:
        footer_lines = NAV_HELP
    else:
        footer_lines = ["j/k move · h/l level · enter open · ]/[ tabs · / search · q quit",
                        "filters: %s · sort: %s" %
                        (", ".join(sorted(nav.statuses)) or "none", nav.sort_key or "default")]
    footer = _keyboard_line(footer_lines, width)
    notice = nav.message
    if nav.prompt == "query":
        notice = "search: " + nav.prompt_text + "_"
    elif nav.prompt == "note":
        notice = "note: " + nav.prompt_text + "_"
    # The card contributes its own lines to the frame, exactly like the listing
    # tab: nesting the box in a list puts a list where a line belongs and the
    # paint loop dies on it.
    notice_tail = ([""] + _rounded_box("Notice", _text_lines(notice, width - 4), width)
                   if notice else [])
    body_height = max(3, height - len(header) - len(footer)
                      - len(notice_tail) - 2)
    content_height = max(1, body_height - 2)
    if width >= 104:
        branch_width = min(26, max(18, width // 5))
        sib_width = min(46, max(30, int(width * 0.38)))
        detail_width = width - branch_width - sib_width - 4
        columns = [("Path", nav_branch(spec, nav, branch_width - 4), branch_width),
                   ("Entries", nav_siblings(spec, nav, sib_width - 4, content_height), sib_width),
                   ("Details", nav_detail(spec, nav, detail_width - 4), detail_width)]
        panels = []
        for title, lines, panel_width in columns:
            body = list(lines[:content_height])
            body.extend([""] * (content_height - len(body)))
            panels.append(_rounded_box(title, body, panel_width))
        body = ["  ".join(panel[index] for panel in panels)
                for index in range(body_height)]
    elif width >= 80:
        sib_width = width // 2 - 1
        detail_width = width - sib_width - 2
        left_body = nav_siblings(spec, nav, sib_width - 4, content_height)[:content_height]
        right_body = nav_detail(spec, nav, detail_width - 4)[:content_height]
        left_body.extend([""] * (content_height - len(left_body)))
        right_body.extend([""] * (content_height - len(right_body)))
        left = _rounded_box("Entries", left_body, sib_width)
        right = _rounded_box("Details", right_body, detail_width)
        body = [left[index] + "  " + right[index] for index in range(body_height)]
    else:
        content = nav_siblings(spec, nav, width - 4, content_height)[:content_height]
        content.extend([""] * (content_height - len(content)))
        body = _rounded_box("Entries", content, width)
    return header + [""] + body + notice_tail + [""] + footer


def frame_lines(frame):
    """A frame as flat text lines.

    A renderer that hands back a nested list used to take the whole menu down
    with a TypeError from the paint loop; flattening keeps the screen alive and
    still shows the content while the renderer is fixed.
    """
    lines = []
    for line in frame or []:
        if isinstance(line, (list, tuple)):
            lines.extend(str(part) for part in line)
        else:
            lines.append(str(line))
    return lines


MOUSE_ON = "\x1b[?1002h\x1b[?1006h"
MOUSE_OFF = "\x1b[?1002l\x1b[?1006l"


def _mouse_event(data):
    """Decode one SGR mouse report: wheel, a click, or everything else.

    A button press on the left button comes back as ``("click", x, y)`` in
    terminal coordinates (1-based), so the menu can tell a real button from a
    drag; releases and motion stay ``mouse_other``.
    """
    if not isinstance(data, bytes) or not data.startswith(b"\x1b[<"):
        return None
    body = data[3:]
    end = max(body.find(b"M"), body.find(b"m"))
    if end < 0:
        return None
    press = body[end:end + 1] == b"M"
    try:
        button, x, y = (int(part) for part in body[:end].split(b";"))
    except (TypeError, ValueError):
        return None
    if press and button == 64:
        return "wheel_up"
    if press and button == 65:
        return "wheel_down"
    if press and button == 0:
        return ("click", x, y)
    return "mouse_other"


def local_executor(command, cwd=ROOT, timeout=120):
    """Run a declared transport, opting into SSH only for the local TUI."""
    argv = shlex.split(command)
    if not argv:
        raise ValueError("empty command")
    env = os.environ.copy()
    if not os.path.exists(os.path.expanduser("~/.hermes/scripts/ams_nudge.py")):
        env["JOBUI_REMOTE_WRITES"] = "1"
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)


def copy_to_clipboard(text):
    """Use the local desktop clipboard, or request it from the SSH terminal."""
    data = "".join(char for char in text if char.isprintable() or char in "\n\t").encode("utf-8")
    if not (os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY")):
        choices = []
        if sys.platform == "darwin":
            choices.append(["/usr/bin/pbcopy"])
        if os.environ.get("WAYLAND_DISPLAY"):
            choices.append(["wl-copy"])
        if os.environ.get("DISPLAY"):
            choices.append(["xclip", "-selection", "clipboard"])
        for command in choices:
            if shutil.which(command[0]):
                try:
                    subprocess.run(command, input=data, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, check=True, timeout=2)
                    return "clipboard"
                except (OSError, subprocess.SubprocessError):
                    continue
    if not os.isatty(1) or os.environ.get("TERM") in (None, "", "dumb"):
        raise OSError("no desktop clipboard or compatible terminal")
    encoded = base64.b64encode(data)
    if len(encoded) > 99_000:
        raise OSError("text too large for terminal clipboard; copy one block instead")
    sys.stdout.write("\x1b]52;c;" + encoded.decode("ascii") + "\x07")
    sys.stdout.flush()
    return "terminal"


def _generation_future(run, background=True):
    future = Future()

    def execute():
        try:
            future.set_result(run())
        except Exception as exc:
            future.set_exception(exc)

    if background:
        threading.Thread(target=execute, name="cover-letter", daemon=True).start()
    else:
        execute()
    return future


def run_nav(spec, ctx, surface="tui", width=None, loader=None):
    """Full screen loop. Requires a tty; returns an exit code."""
    import select
    import termios
    import tty

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        nav = Nav(spec, ctx, surface)
        print("\n".join(frame_lines(render_nav_frame(spec, nav, width or 78, 30))))
        print("\n(no tty: printed one frame instead of an interactive menu)")
        return 0

    _COLOUR["on"] = _colour_enabled()

    def size():
        try:
            cols, lines = os.get_terminal_size(1)
        except OSError:
            cols, lines = shutil.get_terminal_size((width or 78, 26))
        if width:
            cols = width
        return max(40, cols), max(12, lines - 1)

    caps = ((spec.get("surfaces") or {}).get(surface) or {}).get("capabilities") or {}
    # A surface that declares no write capability can look but not mark.
    nav = Nav(spec, ctx, surface, persist_theme=True, explain_once=True,
              executor=local_executor if caps.get("writes") else None, loader=loader,
              persist_settings=True, clipboard=copy_to_clipboard,
              generation_submit=_generation_future)
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)

    buffer = {"data": b""}
    escape_table = [(b"\x1b[A", "up"), (b"\x1b[B", "down"), (b"\x1b[C", "right"),
                    (b"\x1b[D", "left"), (b"\x1b[H", "home"), (b"\x1b[F", "end"),
                    (b"\x1b[5~", "page_up"), (b"\x1b[6~", "page_down"),
                    (b"\x1b[Z", "shift_tab")]

    def fill():
        try:
            chunk = os.read(fd, 64)
        except OSError:
            return False
        if not chunk:
            return False
        buffer["data"] += chunk
        return True

    def take(count):
        out = buffer["data"][:count]
        buffer["data"] = buffer["data"][count:]
        return out

    def read_key():
        """One event per call: a key, an escape sequence, or a mouse report."""
        if not buffer["data"] and not fill():
            return "eof"
        if buffer["data"][:1] == b"\x1b":
            # Give the rest of a sequence a moment, without eating the next key.
            ready, _, _ = select.select([fd], [], [], 0.02)
            if ready:
                fill()
            data = buffer["data"]
            if data.startswith(b"\x1b[<"):
                ends = [index for index in (data.find(b"M"), data.find(b"m")) if index >= 0]
                if not ends:
                    return "escape"
                return _mouse_event(take(min(ends) + 1)) or "mouse_other"
            for sequence, name in escape_table:
                if data.startswith(sequence):
                    take(len(sequence))
                    return name
            take(1)
            return "escape"
        data = take(1)
        if data in (b"\r", b"\n"):
            return "enter"
        if data in (b"\x7f", b"\x08"):
            return "backspace"
        if data == b"\x03":
            return "interrupt"
        if data == b"\t":
            return "tab"
        if data == b"\x14":
            return "ctrl_t"
        if data == b"\x04":
            return "page_down"
        if data == b"\x15":
            return "page_up"
        # A UTF-8 character may arrive in pieces; complete it from the buffer.
        head = data[0]
        need = 1 if head < 0x80 else 2 if head >> 5 == 0b110 else 3 if head >> 4 == 0b1110 else 4
        while len(data) < need and fill():
            data += take(1)
        return data.decode("utf-8", "ignore")

    def paint():
        cols, height = size()
        nav.last_frame = frame_lines(render_nav_frame(spec, nav, cols, height))
        # The same frame is both the click hit map and the visible screen.
        painted = "\r\n".join("\x1b[2K" + line for line in nav.last_frame)
        os.write(1, ("\x1b[H\x1b[2J" + painted + "\x1b[0m\x1b[J").encode("utf-8"))

    try:
        tty.setraw(fd, termios.TCSANOW)
        # Always release mouse mode at boot if this run does not claim it.
        os.write(1, (MOUSE_ON if nav.mouse else MOUSE_OFF).encode("ascii"))
        claimed = {"on": nav.mouse}
        while not nav.quit:
            nav.poll_generation()
            paint()
            if nav.generation is not None and not buffer["data"]:
                ready, _, _ = select.select([fd], [], [], 0.12)
                if not ready:
                    nav.generation_frame += 1
                    continue
            key = read_key()
            if key in ("interrupt", "eof"):
                break
            reload_key = (key == "r" and nav.packet_view is None and nav.generation_picker is None
                          and not nav.settings_open)
            nav.handle(key)
            if nav.packet_terminal_action is not None:
                action, nav.packet_terminal_action = nav.packet_terminal_action, None
                buffer["data"] = b""
                os.write(1, (MOUSE_OFF + "\x1b[H\x1b[2J").encode("ascii"))
                termios.tcsetattr(fd, termios.TCSAFLUSH, saved)
                try:
                    launcher = os.path.join(ROOT, "ui", "jobhunter")
                    child = subprocess.run(["sh", launcher, *action], cwd=ROOT,
                                           env={**os.environ, "JOBUI_REPO": ROOT})
                    if child.returncode:
                        input("Press Enter to return to Jobhunter.")
                    if not child.returncode and action[0] in ("versions", "revise") and nav.expanded_id:
                        pane, block, scroll = nav.packet_pane, nav.packet_block, nav.packet_scroll.copy()
                        nav.packets.pop(nav.expanded_id, None)
                        if nav.open_packet_view(nav.expanded_id, fresh=True):
                            nav.packet_pane = pane
                            nav.packet_block = min(block, max(0, len(_packet_blocks(nav.packet_view)) - 1))
                            nav.packet_scroll = scroll
                            nav.packet_follow = True
                    else:
                        nav.message = "Returned to application review" if not child.returncode else "Action stopped"
                except (OSError, EOFError, KeyboardInterrupt) as exc:
                    nav.message = "Action stopped" + (f": {exc}" if str(exc) else "")
                finally:
                    tty.setraw(fd, termios.TCSAFLUSH)
                    os.write(1, (MOUSE_ON if nav.mouse else MOUSE_OFF).encode("ascii"))
            if nav.mouse != claimed["on"]:
                claimed["on"] = nav.mouse
                os.write(1, (MOUSE_ON if nav.mouse else MOUSE_OFF).encode("ascii"))
            if reload_key and loader is not None:
                try:
                    nav.ctx = loader()
                    nav.reload_projection()
                    nav.message = "reloaded from feed"
                except Exception as exc:  # noqa: BLE001 - surfaced to the user
                    nav.message = "reload failed: %s" % exc
    finally:
        try:
            os.write(1, MOUSE_OFF.encode("ascii"))
        except OSError:
            pass
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        os.write(1, b"\x1b[H\x1b[2J")
        if nav.writes:
            print("left the menu; %d tracker write(s) landed" % nav.writes)
        else:
            print("left the menu; no tracker state was touched")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Generate UI structure projections.")
    parser.add_argument("--structure", default=UI_FILE)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("validate")
    flat = sub.add_parser("flatten")
    flat.add_argument("--surface", default="tui")
    flat.add_argument("--context")
    flat.add_argument("--format", default="table", choices=["table", "csv", "json"])

    tui = sub.add_parser("tui")
    tui.add_argument("--surface", default="tui")
    tui.add_argument("--context")
    tui.add_argument("--feed")
    tui.add_argument("--width", type=int)
    tui.add_argument("--out")

    browse = sub.add_parser("browse")
    browse.add_argument("--surface", default="tui")
    browse.add_argument("--context")
    browse.add_argument("--feed")
    browse.add_argument("--width", type=int)

    gui = sub.add_parser("gui")
    gui.add_argument("--surface", default="gui")
    gui.add_argument("--context")
    gui.add_argument("--feed")
    gui.add_argument("--out")

    ctx_cmd = sub.add_parser("context")
    ctx_cmd.add_argument("--feed", default=os.environ.get("JOBHUNTER_FEED", os.path.join(WORKSPACE, ".ai", "feed", "jobs.json")))
    ctx_cmd.add_argument("--mode", default="served")
    ctx_cmd.add_argument("--out")

    build = sub.add_parser("build")
    build.add_argument("--context")
    build.add_argument("--feed", default=os.environ.get("JOBHUNTER_FEED", os.path.join(WORKSPACE, ".ai", "feed", "jobs.json")))
    build.add_argument("--width", type=int, default=78)

    args = parser.parse_args(argv)
    try:
        spec = load(args.structure)
    except ValidationError as exc:
        print("error: %s" % exc)
        return 2

    if args.command == "validate":
        return 1 if _print_problems(validate(spec)) else 0

    problems = validate(spec)
    errors = [p for p in problems if p["severity"] == "error"]
    if errors:
        _print_problems(problems)
        return 1
    warnings = [p for p in problems if p["severity"] == "warn"]

    if args.command == "context":
        if not os.path.exists(args.feed):
            print("error: feed not found: %s" % args.feed)
            return 2
        ctx = _context_from_feed_file(args.feed, mode=args.mode)
        text = json.dumps(ctx, indent=2, ensure_ascii=False) + "\n"
        if args.out:
            _write(args.out, text)
            print("wrote %s (%d row(s))" % (args.out, len(ctx["rows"])))
        else:
            sys.stdout.write(text)
        return 0

    def context_for(namespace):
        if namespace.context:
            return _read_context(namespace.context)
        if getattr(namespace, "feed", None):
            return _context_from_feed_file(namespace.feed)
        return empty_context()

    if args.command == "flatten":
        ctx = _read_context(args.context) if args.context else empty_context()
        rows = Projection(spec, ctx, args.surface).flatten()
        if args.format == "json":
            sys.stdout.write(json.dumps(rows, indent=2) + "\n")
        elif args.format == "csv":
            print("depth,path,id,kind,label,key,role,action")
            for row in rows:
                print(",".join(['"%s"' % str(row[k]).replace('"', '""')
                                for k in ("depth", "path", "id", "kind", "label", "key", "role", "action")]))
        else:
            for row in rows:
                print("%s%-28s %-8s %-32s %s" % ("  " * (row["depth"] - 1), row["id"][:28],
                                                 row["kind"], row["label"][:32], row["key"]))
        return 0

    if args.command == "tui":
        ctx = context_for(args)
        text = render_tui(spec, ctx, surface=args.surface, width=args.width)
        if args.out:
            _write(args.out, text + "\n")
            print("wrote %s" % args.out)
        else:
            print(text)
        if warnings:
            print("\n%d structure warning(s); run validate for details" % len(warnings))
        return 0

    if args.command == "gui":
        ctx = context_for(args)
        text = render_gui(spec, ctx, surface=args.surface)
        if args.out:
            _write(args.out, text)
            print("wrote %s (%d bytes)" % (args.out, len(text)))
        else:
            sys.stdout.write(text)
        return 0

    if args.command == "browse":
        if args.feed and not os.path.exists(os.path.expanduser("~/.hermes/scripts/ams_nudge.py")):
            # The interactive menu is the one surface allowed to ask the server
            # for anything; a read-only dump never opts in.
            os.environ.setdefault("JOBUI_REMOTE_WRITES", "1")
        ctx = context_for(args)
        if args.feed:
            # Start on the tracker's state, not on the last feed build: the
            # rows carry the stage the writer stored, and `r` asks again.
            ctx = _live_context(args.feed)
            loader = lambda: _live_context(args.feed)
        elif args.context:
            loader = lambda: _read_context(args.context)
        else:
            loader = None
        return run_nav(spec, ctx, args.surface, args.width, loader)

    if args.command == "build":
        os.makedirs(OUT_DIR, exist_ok=True)
        ctx = _read_context(args.context) if args.context else (
            _context_from_feed_file(args.feed) if os.path.exists(args.feed) else empty_context())
        tui_text = render_tui(spec, ctx, width=args.width)
        gui_text = render_gui(spec, ctx)
        _write(os.path.join(OUT_DIR, "tui.txt"), tui_text + "\n")
        _write(os.path.join(OUT_DIR, "gui.html"), gui_text)
        nodes = Projection(spec, ctx, "tui").flatten()
        print("wrote ui/out/tui.txt and ui/out/gui.html")
        print("nodes: %d | rows: %d | warnings: %d"
              % (len(nodes), len(ctx.get("rows") or []), len(warnings)))
        return 0

    return 2


def _write(path, text):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


if __name__ == "__main__":
    sys.exit(main())
