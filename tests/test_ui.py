"""Tests for ui/interface.json and tools/ui.py. Standard library unittest only."""
import copy
import io
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import ui  # noqa: E402

STRUCTURE = os.path.join(ROOT, "ui", "interface.json")
ui_ansi_re = re.compile(r"\033\[[0-9;]*m")


def spec():
    return ui.load(STRUCTURE)


def errors(problems):
    return [p for p in problems if p["severity"] == "error"]


def codes(problems):
    return [p["code"] for p in problems]


def find_node(nodes, node_id):
    for node in nodes:
        if node.get("id") == node_id:
            return node
        found = find_node(node.get("children") or [], node_id)
        if found:
            return found
    return None


def sample_context(**overrides):
    ctx = {
        "session": {"mode": "served", "state": "ready"},
        "facts": {
            "counts.total": 3,
            "counts.new": 3,
            "counts.nudged": 0,
            "counts.applied": 0,
            "counts.interview": 0,
            "counts.blocked": 0,
            "counts.skipped": 0,
            "counts.expired": 0,
            "counts.in_latest_scan": 3,
            "scan.available": True,
            "scan.scraped_at": "2026-01-05T09:00:00Z",
            "scan.line": "latest demo scan 2026-01-05 09:00 | Sample City (20 km)",
            "feed.warnings.count": 0,
            "feed.degraded": False,
            "selection.listing_id": "job-1",
        },
        "rows": [{
            "id": "job-1", "title": "Example Technician", "company": "Example Works",
            "location": "Sample City", "url": "https://example.invalid/1",
            "posted_date": "2026-01-04", "score": 9.1, "score_source": "demo scan",
            "in_latest_scan": True, "status": "new", "tracked_date": "",
            "note": "",
        }],
        "selection": {"listing_id": "job-1", "listing_url": "https://example.invalid/1",
                      "row": None},
    }
    ctx["selection"]["row"] = ctx["rows"][0]
    for key, value in overrides.items():
        ctx["facts"][key] = value
    return ctx


class StructureTests(unittest.TestCase):
    def test_shipped_structure_is_clean(self):
        problems = ui.validate(spec())
        self.assertEqual([], errors(problems), codes(problems))

    def test_schema_and_version_are_declared(self):
        data = spec()
        self.assertEqual(ui.SCHEMA_ID, data["schema"])
        self.assertTrue(data["version"])
        self.assertTrue(data["app"]["source_of_truth"].endswith("ui/interface.json"))

    def test_ids_are_unique(self):
        data = spec()
        ids = []

        def walk(nodes):
            for node in nodes:
                ids.append(node["id"])
                walk(node.get("children") or [])
        walk(data["tree"])
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_filterable_status_has_a_filter_node(self):
        data = spec()
        filterable = [entry["key"] for entry in data["vocabulary"]["statuses"]
                      if entry.get("filter")]
        bound = []
        for row in ui.Projection(data, sample_context(), "tui").flatten():
            node = find_node(data["tree"], row["id"])
            bind = (node or {}).get("bind") or {}
            if (node and node.get("kind") == "filter"
                    and isinstance(bind, dict) and bind.get("vocabulary") == "statuses"):
                bound.append(bind["key"])
        for key in filterable:
            self.assertIn(key, bound, "status %r has no filter node" % key)
        self.assertIn("expired", bound)
        self.assertIn("skipped", bound)

    def test_stage_actions_cover_the_pipeline(self):
        data = spec()
        refs = {node["action"] for node in _walk(data["tree"]) if node.get("kind") == "action"}
        for key in ("generated", "checked", "sent", "confirmed", "waiting",
                    "interview", "success", "rejected", "blocked", "unclear", "skipped"):
            self.assertIn("stage_" + key, refs)
        self.assertIn("stage_next", refs)
        stages = [entry["key"] for entry in data["vocabulary"]["stages"]]
        for ref in refs:
            if not ref.startswith("stage_"):
                continue
            action = data["actions"][ref]
            channels = {t["channel"] for t in action["transports"]}
            self.assertIn("chat-frame", channels, ref)
            self.assertIn("local", channels, ref)
            self.assertTrue({"served", "static"} & channels, ref)
            consts = [arg["const"] for arg in action["args"] if arg["name"] == "stage"]
            self.assertEqual(1, len(consts), ref)
            if consts[0] not in ("next", "prev"):
                self.assertIn(consts[0], stages, ref)

    def test_open_source_is_browser_only_and_capability_gated(self):
        action = spec()["actions"]["open_source"]
        self.assertEqual({"browser"}, {t["channel"] for t in action["transports"]})
        self.assertIn("url_links", action["requires"])

    def test_single_writer_is_documented_on_mark_actions(self):
        data = spec()
        applied = data["actions"]["stage_sent"]
        text = " ".join(applied.get("notes") or [])
        self.assertIn("ams_nudge.py", text)
        static = [t for t in applied["transports"] if t["channel"] == "static"][0]
        self.assertIn("tools/dashboard.py mark", static["command"])
        local = [t for t in applied["transports"] if t["channel"] == "local"][0]
        self.assertIn("tools/dashboard.py mark", local["command"])
        self.assertIn("writes", applied["requires"])

    def test_no_em_or_en_dash_anywhere(self):
        data = spec()
        blob = str(data)
        self.assertNotIn("\u2014", blob)
        self.assertNotIn("\u2013", blob)


class ValidationTests(unittest.TestCase):
    def test_duplicate_id_is_rejected(self):
        data = spec()
        node = find_node(data["tree"], "filter.expired")
        node["id"] = "filter.skipped"
        self.assertIn("duplicate", codes(ui.validate(data)))

    def test_unknown_kind_is_rejected(self):
        data = spec()
        find_node(data["tree"], "system.help")["kind"] = "carousel"
        self.assertIn("kind", codes(ui.validate(data)))

    def test_undeclared_fact_is_rejected(self):
        data = spec()
        find_node(data["tree"], "meter.total")["bind"] = "counts.imaginary"
        self.assertIn("bind", codes(ui.validate(data)))

    def test_undeclared_listing_field_is_rejected(self):
        data = spec()
        find_node(data["tree"], "field.score")["bind"] = "listing.salary"
        self.assertIn("bind", codes(ui.validate(data)))

    def test_unknown_action_reference_is_rejected(self):
        data = spec()
        find_node(data["tree"], "action.stage_sent")["action"] = "stage_magic"
        self.assertIn("action_ref", codes(ui.validate(data)))

    def test_duplicate_hotkey_is_rejected(self):
        data = spec()
        find_node(data["tree"], "filter.expired")["keys"]["tui"] = "1"
        self.assertIn("duplicate_key", codes(ui.validate(data)))

    def test_transport_placeholder_must_be_a_declared_arg(self):
        data = spec()
        data["actions"]["stage_sent"]["transports"][3]["command"] = \
            "python3 -B tools/dashboard.py mark {jobid} sent"
        self.assertIn("transport", codes(ui.validate(data)))

    def test_unknown_channel_is_rejected(self):
        data = spec()
        data["actions"]["stage_sent"]["transports"][1]["channel"] = "carrier-pigeon"
        self.assertIn("transport", codes(ui.validate(data)))

    def test_bookmark_is_a_tracker_write_on_every_surface(self):
        data = spec()
        node = find_node(data["tree"], "action.toggle_bookmark")
        self.assertNotIn("hidden", node["surface"]["tui"])
        self.assertEqual(node["keys"]["tui"], "B")
        transports = data["actions"]["toggle_bookmark"]["transports"]
        local = [item for item in transports if item["channel"] == "local"]
        self.assertEqual(len(local), 1)
        self.assertIn("dashboard.py bookmark", local[0]["command"])
        self.assertIn("writes", data["actions"]["toggle_bookmark"]["requires"])

    def test_state_references_must_exist(self):
        data = spec()
        data["states"]["error"]["hides"] = ["listings.nowhere"]
        self.assertIn("state_ref", codes(ui.validate(data)))

    def test_unknown_vocabulary_key_is_rejected(self):
        data = spec()
        find_node(data["tree"], "filter.blocked")["bind"]["key"] = "ghosted"
        self.assertIn("bind", codes(ui.validate(data)))

    def test_depth_beyond_surface_limit_warns(self):
        data = spec()
        listing = find_node(data["tree"], "item.listing")
        listing["children"].append({
            "id": "field.deep", "kind": "field", "label": "Deep",
            "bind": "listing.note",
            "children": [{"id": "field.deeper", "kind": "field", "label": "Deeper",
                          "bind": "listing.note",
                          "children": [{"id": "field.deepest", "kind": "field",
                                        "label": "Deepest", "bind": "listing.note"}]}],
        })
        problems = ui.validate(data)
        self.assertIn("depth", codes(problems), "expected a depth warning")
        self.assertFalse([p for p in problems if p["code"] == "depth"
                          and p["severity"] == "error"])


class ProjectionTests(unittest.TestCase):
    def test_capability_filtering_hides_nodes(self):
        data = spec()
        node = find_node(data["tree"], "action.open_source")
        node["requires"] = ["url_links"]
        data["surfaces"]["tui"]["capabilities"]["url_links"] = False
        ctx = sample_context()
        tui_ids = {row["id"] for row in ui.Projection(data, ctx, "tui").flatten()}
        gui_ids = {row["id"] for row in ui.Projection(data, ctx, "gui").flatten()}
        self.assertNotIn("action.open_source", tui_ids)
        self.assertIn("action.open_source", gui_ids)

    def test_conditions_hide_empty_tracker_views(self):
        data = spec()
        ctx = sample_context()
        ids = {row["id"] for row in ui.Projection(data, ctx, "tui").flatten()}
        self.assertNotIn("tracker.applied", ids)
        ctx = sample_context(**{"counts.applied": 4, "counts.blocked": 2})
        ids = {row["id"] for row in ui.Projection(data, ctx, "tui").flatten()}
        self.assertIn("tracker.applied", ids)
        self.assertIn("tracker.blocked", ids)

    def test_static_mode_reveals_cli_fallback(self):
        data = spec()
        served = ui.Projection(data, sample_context(), "gui").flatten()
        static_ctx = sample_context()
        static_ctx["session"]["mode"] = "static"
        static = ui.Projection(data, static_ctx, "gui").flatten()
        self.assertNotIn("system.cli_fallback", {row["id"] for row in served})
        self.assertIn("system.cli_fallback", {row["id"] for row in static})

    def test_state_hides_a_table_while_loading(self):
        data = spec()
        ctx = sample_context()
        ctx["session"]["state"] = "loading"
        flat = ui.Projection(data, ctx, "gui").flatten()
        ids = {row["id"] for row in flat}
        self.assertNotIn("listings.table", ids)
        self.assertIn("listings", ids)

    def test_depth_limit_keeps_the_parent(self):
        data = spec()
        data["surfaces"]["tui"]["max_depth"] = 2
        proj = ui.Projection(data, sample_context(), "tui")
        ids = {row["id"] for row in proj.flatten()}
        self.assertIn("listings", ids)
        self.assertNotIn("listings.table", ids)
        self.assertIn("truncated", find_node(proj.tree, "listings"))

    def test_flatten_paths_show_nesting(self):
        rows = ui.Projection(spec(), sample_context(), "tui").flatten()
        applied = [row for row in rows if row["id"] == "action.stage_sent"][0]
        self.assertEqual(applied["depth"], 4)
        self.assertTrue(applied["path"].startswith("Workspace > Listings"))
        self.assertEqual(applied["role"], "primary")

    def test_surface_overrides_change_label_and_key(self):
        data = spec()
        tui = ui.Projection(data, sample_context(), "tui").flatten()
        gui = ui.Projection(data, sample_context(), "gui").flatten()
        tui_open = [row for row in tui if row["id"] == "action.open_source"][0]
        gui_open = [row for row in gui if row["id"] == "action.open_source"][0]
        self.assertEqual("Open AMS in browser", tui_open["label"])
        self.assertEqual("Open AMS listing", gui_open["label"])
        self.assertEqual("v", tui_open["key"])


class TerminalRenderingTests(unittest.TestCase):
    def test_every_line_fits_the_width(self):
        text = ui.render_tui(spec(), sample_context(), width=64)
        for line in text.splitlines():
            self.assertLessEqual(len(line), 64, line)

    def test_nesting_is_indented(self):
        text = ui.render_tui(spec(), sample_context())
        root = [line for line in text.splitlines() if "+ Workspace" in line][0]
        child = [line for line in text.splitlines() if "[l] Listings" in line][0]
        root_content = root.split("│", 1)[1]
        child_content = child.split("│", 1)[1]
        self.assertLess(len(root_content) - len(root_content.lstrip()),
                        len(child_content) - len(child_content.lstrip()))

    def test_primary_action_is_marked(self):
        text = ui.render_tui(spec(), sample_context())
        self.assertIn("[a] Sent *", text)
        self.assertIn("keys:", text)

    def test_values_come_from_context(self):
        ctx = sample_context(**{"counts.total": 1001})
        text = ui.render_tui(spec(), ctx)
        self.assertIn("1001", text)
        ctx["rows"][0]["title"] = "Example Specialist"
        text = ui.render_tui(spec(), ctx)
        self.assertIn("Example Specialist", text)

    def test_empty_context_does_not_crash(self):
        text = ui.render_tui(spec(), ui.empty_context())
        self.assertIn("no rows loaded", text)

    def test_hidden_capabilities_are_reported(self):
        data = spec()
        find_node(data["tree"], "action.open_source")["requires"] = ["url_links"]
        data["surfaces"]["tui"]["capabilities"]["url_links"] = False
        text = ui.render_tui(data, sample_context())
        self.assertIn("hidden on tui", text)


class BrowserRenderingTests(unittest.TestCase):
    def test_untrusted_listing_text_is_escaped(self):
        ctx = sample_context()
        hostile = '<img src=x onerror="alert(1)"> & <script>2</script>'
        ctx["rows"][0]["title"] = hostile
        ctx["rows"][0]["company"] = hostile
        ctx["selection"]["row"] = ctx["rows"][0]
        markup = ui.render_gui(spec(), ctx)
        self.assertNotIn("<img src=x", markup)
        self.assertNotIn("<script>2</script>", markup)
        self.assertIn("&lt;img", markup)

    def test_rendered_titles_use_plain_hyphens_without_changing_the_feed(self):
        ctx = sample_context()
        ctx["rows"][0]["title"] = "Role\u2013Engineer\u2014Senior"
        ctx["selection"]["row"] = ctx["rows"][0]
        markup = ui.render_gui(spec(), ctx)
        self.assertNotIn("\u2013", markup)
        self.assertNotIn("\u2014", markup)
        self.assertIn("Role-Engineer-Senior", markup)
        self.assertEqual("Role\u2013Engineer\u2014Senior", ctx["rows"][0]["title"])

    def test_no_external_assets(self):
        markup = ui.render_gui(spec(), sample_context())
        self.assertNotIn("<script src", markup)
        self.assertNotIn("<link", markup)
        self.assertNotIn("@import", markup)
        self.assertNotIn("http://", markup.replace("https://example.invalid", ""))

    def test_filters_are_in_the_job_list_and_keep_the_table_out(self):
        ctx = sample_context()
        ctx["rows"][0]["stage"] = "generated"
        markup = ui.render_gui(spec(), ctx)
        first = markup.split('id="job-list-view"', 1)[1].split('</div><article class="job-detail"', 1)[0]
        self.assertIn('class="job-row"', first)
        self.assertIn('data-filter-status="nudged"', first)
        self.assertIn('id="stage-filter"', first)
        self.assertIn('<option value="generated">Generating application</option>', first)
        self.assertIn('data-status="new"', first)
        self.assertIn('data-stage="generated"', first)
        self.assertIn('id="job-shown"', first)
        self.assertNotIn('<table', first)

    def test_mark_command_remains_declared_but_not_in_initial_list(self):
        self.assertEqual("python3 -B tools/dashboard.py mark job-1 sent",
                         ui.action_command(spec(), "stage_sent", {"listing_id": "job-1"}))

    def test_roles_become_classes(self):
        markup = ui.render_gui(spec(), sample_context())
        self.assertIn("is-primary", markup)
        self.assertIn("is-danger", markup)

    def test_rendering_is_deterministic(self):
        first = ui.render_gui(spec(), sample_context())
        second = ui.render_gui(spec(), sample_context())
        self.assertEqual(first, second)

    def test_scan_line_is_available_outside_simple_list(self):
        markup = ui.render_gui(spec(), sample_context())
        self.assertIn("latest AMS scan", markup)
        self.assertIn("Sorted by score, then monthly pay", markup)

    def test_empty_state_message_is_not_shown_when_rows_exist(self):
        markup = ui.render_gui(spec(), sample_context())
        self.assertNotIn('id="empty-state">', markup)


class ContextTests(unittest.TestCase):
    def test_context_from_feed_maps_counts_and_rows(self):
        feed = {
            "counts": {"total": 3, "new": 2, "nudged": 1, "applied": 0,
                       "interview": 0, "blocked": 0, "skipped": 0,
                       "expired": 0, "in_latest_scan": 2},
            "scan": {"scraped_at": "2026-11-10T06:43:54.444Z", "location": "Sample City"},
            "warnings": [],
            "jobs": [{"id": "a", "title": "T", "company": "C", "location": "W",
                      "url": "https://example.invalid/a", "score": 8.0,
                      "pay_monthly_min": 3500.0,
                      "in_latest_scan": True, "status": "new"}],
        }
        ctx = ui.context_from_feed(feed)
        self.assertEqual(3, ctx["facts"]["counts.total"])
        self.assertEqual("a", ctx["facts"]["selection.listing_id"])
        self.assertEqual(1, len(ctx["rows"]))
        self.assertEqual(3500.0, ctx["rows"][0]["pay_monthly_min"])
        self.assertIn("latest AMS scan", ctx["facts"]["scan.line"])

    def test_missing_scan_marks_scan_unavailable(self):
        feed = {"counts": {"total": 0}, "scan": {}, "warnings": ["missing"], "jobs": []}
        ctx = ui.context_from_feed(feed)
        self.assertFalse(ctx["facts"]["scan.available"])
        self.assertEqual(1, ctx["facts"]["feed.warnings.count"])

    def test_scan_only_view_disappears_without_scan(self):
        feed = {"counts": {"total": 1, "new": 1}, "scan": {}, "warnings": [], "jobs": []}
        ctx = ui.context_from_feed(feed)
        ids = {row["id"] for row in ui.Projection(spec(), ctx, "gui").flatten()}
        self.assertNotIn("review.latest_scan", ids)


class TabsAndColumnsTest(unittest.TestCase):
    """The browser list exposes rank and known metadata before detail."""

    def test_every_tab_declares_a_root_that_exists(self):
        data = spec()
        ids = {node["id"] for node in _walk(data["tree"])}
        self.assertTrue(data["tabs"])
        for tab in data["tabs"]:
            self.assertIn(tab["root"], ids)

    def test_render_gui_emits_one_panel_per_rendered_tab(self):
        html_text = ui.render_gui(spec(), sample_context())
        panels = html_text.split('class="tabpanel"')[1:]
        declared = [tab["id"] for tab in spec()["tabs"]]
        self.assertGreaterEqual(len(panels), 2)
        for panel in panels:
            tab_id = panel.split('data-tab="', 1)[1].split('"', 1)[0]
            self.assertIn(tab_id, declared)

    def test_first_panel_visible_and_others_hidden(self):
        html_text = ui.render_gui(spec(), sample_context())
        blocks = html_text.split('class="tabpanel"')[1:]
        self.assertNotIn(" hidden", blocks[0].split(">", 1)[0])
        self.assertTrue(any(" hidden" in block.split(">", 1)[0] for block in blocks[1:]))

    def test_initial_list_shows_score_title_pay_and_city_columns(self):
        html_text = ui.render_gui(spec(), sample_context())
        names = html_text.split('<ol class="job-list"', 1)[1].split('</ol>', 1)[0]
        self.assertIn('Example Technician', names)
        # Score, title and pay share the first row; the city chip sits under the
        # title, and the empty pay slot still holds its column.
        self.assertIn('<span class="job-row-score"><span class="score-bubble"', names)
        self.assertIn('</span></span><span class="job-row-title">Example Technician', names)
        self.assertIn('</span><span class="job-row-pay"></span>'
                      '<span class="job-row-city"><span class="tag-chip"', names)
        self.assertIn('aria-label="Score 9.1"', names)
        self.assertIn('title="Sample City">Sample City</span></span>', names)
        self.assertIn('id="job-list" style="--pay-col:', names)
        self.assertNotIn('job-row-location', names)
        self.assertNotIn('job-row-meta', names)
        self.assertNotIn('gross/month', names)
        for value in ("Alpha", "Status", "Group", "FRESH"):
            self.assertNotIn(value, names)

    def test_known_pay_sits_in_its_own_column_on_the_first_row(self):
        ctx = sample_context()
        ctx["rows"][0]["pay_monthly_min"] = 2705.4
        ctx["rows"].append({"id": "job-2", "title": "Other", "location": "Othercity",
                            "score": None, "pay_monthly_min": None})
        names = ui.render_gui(spec(), ctx).split('<ol class="job-list"', 1)[1].split('</ol>', 1)[0]
        self.assertIn('</span><span class="job-row-pay">EUR 2,705.40/mo</span>'
                      '<span class="job-row-city"><span class="tag-chip"', names)
        self.assertIn('title="Othercity">Othercity</span>', names)
        self.assertIn('aria-label="Score unknown"', names)
        self.assertIn('>?</span>', names)
        self.assertEqual(1, names.count('/mo'))
        # The second row keeps the same empty pay cell, so both rows line up.
        self.assertEqual(2, names.count('class="job-row-pay"'))

    def test_yearly_pay_becomes_a_calculated_monthly_figure(self):
        ctx = sample_context()
        ctx["rows"][0]["pay_monthly_min"] = None
        ctx["rows"][0]["pay_yearly_min"] = 53200.0
        ctx["rows"][0]["pay_monthly_calc"] = 3800.0
        names = ui.render_gui(spec(), ctx).split('<ol class="job-list"', 1)[1].split('</ol>', 1)[0]
        self.assertIn('<span class="job-row-pay">≈ EUR 3,800/mo (yr/14)', names)
        self.assertNotIn('gross/', names)
        self.assertEqual("EUR 3,800 gross/month (calculated from EUR 53,200 gross/year / 14)",
                         ui._listing_pay_cell(ctx["rows"][0]))

    def test_pay_text_prefers_the_stated_month_and_then_divides_the_year_by_14(self):
        row = {"pay_monthly_min": 2920.0, "pay_yearly_min": 53200.0,
               "pay_monthly_calc": 3800.0}
        self.assertEqual("EUR 2,920/mo", ui._listing_pay_text(row))
        self.assertEqual("≈ EUR 3,800/mo (yr/14)",
                         ui._listing_pay_text({"pay_yearly_min": 53200.0,
                                               "pay_monthly_calc": 3800.0}))
        self.assertEqual("EUR 53,200 gross/year",
                         ui._listing_pay_text({"pay_yearly_min": 53200.0}))
        self.assertEqual("", ui._listing_pay_text({"pay_yearly_min": "nan"}))
        self.assertEqual("not stated", ui._listing_pay_cell({}))

    def test_score_gradient_clamps_and_score_border_is_heavier_than_flag(self):
        self.assertIn('hsl(0 80% 70%)', ui._score_bubble(0, '0.0'))
        self.assertIn('hsl(125 80% 70%)', ui._score_bubble(10, '10.0'))
        self.assertIn('hsl(125 80% 70%)', ui._score_bubble(999, '999'))
        self.assertNotIn('style=', ui._score_bubble(None, ''))
        self.assertIn('height:18px;padding:0 5px;border:2px solid var(--score-color', ui.GUI_CSS)
        self.assertIn('.job-row .tag-chip{height:14px;padding:0 4px;font-size:10px}', ui.GUI_CSS)
        self.assertIn('border:1.5px solid var(--tag-color', ui.GUI_CSS)
        self.assertIn('border:1.5px solid currentColor', ui.GUI_CSS)
        # Expanded chip shares the flag box, only the border stays heavier.
        self.assertIn('.job-detail .flags .flag,.job-detail .flags .score-bubble{'
                      'display:inline-flex;', ui.GUI_CSS)
        self.assertIn('min-height:29px;padding:0 10px;font-size:12px', ui.GUI_CSS)
        self.assertIn('.job-detail .flags .score-bubble{border-width:2px}', ui.GUI_CSS)

    def test_each_city_has_one_stable_colour_and_districts_share_it(self):
        self.assertEqual("Sample City", ui._city_name("1100, Sample City,North District"))
        self.assertEqual("Sample City", ui._city_name("1230, Sample City,South District"))
        self.assertEqual("Sample City", ui._city_name("Sample City"))
        self.assertEqual("Northfield", ui._city_name("2332, Northfield"))
        colours = ui._city_colours(["1230, Sample City,South District", "Sample City",
                                    "1100, Sample City,North District", "2344, Exampleville",
                                    "2332, Northfield"])
        self.assertEqual(3, len(colours))
        self.assertEqual(len(set(colours.values())), 3)
        self.assertIn('style="--tag-color:hsl(181 62% 68%)"',
                      ui._tag_chip("Sample City", "Location", (181, 62, 68)))
        self.assertNotIn("style=", ui._tag_chip("Sample City", "Location"))

    def test_city_palette_is_generic_and_independent_of_feed_order(self):
        locations = ["Sample City", "12345, Sample City,North District", "Exampleville",
                     "Rivertown", "Hillview"]
        colours = ui._city_colours(locations)
        self.assertEqual(colours, ui._city_colours(reversed(locations)))
        self.assertEqual(len(colours), len(set(colours.values())))
        self.assertFalse(hasattr(ui, "CITY_COLOURS"))
        self.assertTrue(all(colour[0] is not None for colour in colours.values()))

    def test_city_colours_are_unique_even_when_hashes_collide(self):
        locations = ["Eastport", "Westhaven", "Sample City", "1100, Sample City,North District",
                     "3385, Midvale", "2344, Exampleville", "Southport",
                     "2500, Southport", ""]
        hues = ui._city_colours(locations)
        self.assertEqual(6, len(hues))
        self.assertEqual(len(set(hues.values())), len(hues))
        self.assertNotIn("", hues)
        self.assertEqual(hues, ui._city_colours(reversed(locations)))
        self.assertNotEqual(hues["Eastport"], hues["Westhaven"])
        self.assertNotEqual(hues["Midvale"], hues["Exampleville"])
        wheel = sorted(colour[0] for colour in hues.values())
        self.assertGreaterEqual(min(wheel[i + 1] - wheel[i] for i in range(len(wheel) - 1)), 10)

    def test_detail_fields_appear_only_inside_hidden_cards(self):
        html_text = ui.render_gui(spec(), sample_context())
        self.assertIn('class="job-detail" id="job-detail" hidden', html_text)
        self.assertIn('class="job-card" data-index="0" hidden', html_text)
        self.assertIn('<dt>Company</dt><dd>Example Works</dd>', html_text)
        self.assertIn('>9.1</span>', html_text)
        self.assertNotIn('>Score 9.1</span>', html_text)
        self.assertNotIn('<dt>Score</dt>', html_text)

    def test_posted_date_flags_an_extraction_basis(self):
        ctx = sample_context()
        ctx["rows"][0]["posted_basis"] = "extraction"
        html_text = ui.render_gui(spec(), ctx)
        self.assertIn(">EST</span>", html_text)
        self.assertIn("Extraction date (posting date not given)", html_text)

    def test_only_https_sources_become_links(self):
        ctx = sample_context()
        ctx["rows"][0]["url"] = "javascript:alert(1)"
        self.assertNotIn('<a href="javascript:', ui.render_gui(spec(), ctx))

    def test_hostile_listing_text_is_escaped_in_list_and_detail(self):
        ctx = sample_context()
        ctx["rows"][0]["title"] = "<img src=x onerror=alert(1)>"
        html_text = ui.render_gui(spec(), ctx)
        self.assertNotIn("<img src=x", html_text)
        self.assertIn("&lt;img", html_text)

    def test_three_ams_sections_are_visible_in_detail_and_escaped(self):
        ctx = sample_context()
        ctx["rows"][0].update({"company_description": "Werk & <script>alert(1)</script>",
                               "occupation": "Technikerin", "job_description": "Aufgaben:\nPrüfen & testen",
                               "general_info": "Gehalt ab 3.000 EUR", "full_text": "Archived full ad"})
        page = ui.render_gui(spec(), ctx)
        for label in ("Unternehmensbeschreibung", "Beruf", "Stellenbeschreibung", "Allgemeine Informationen"):
            self.assertIn(label, page)
        self.assertIn("Werk &amp; &lt;script&gt;", page)
        self.assertNotIn("Werk & <script>", page)
        self.assertIn("<p>Aufgaben:</p><p>Prüfen &amp; testen</p>", page)
        self.assertIn("Archived full ad", page)
        self.assertEqual("Technikerin", ui.context_from_feed({"jobs": [ctx["rows"][0]],
                                 "counts": {"total": 1}})["rows"][0]["occupation"])

    def test_ad_text_is_one_bordered_expandable_panel_with_separate_paragraphs(self):
        ctx = sample_context()
        ctx["rows"][0]["job_description"] = "First paragraph.\nSecond paragraph.\nThe role – technical — hands-on."
        page = ui.render_gui(spec(), ctx)
        self.assertNotIn("\u2013", page)
        self.assertNotIn("\u2014", page)
        self.assertIn('<details class="ad-panel"', page)
        self.assertIn('<summary>AMS description</summary>', page)
        self.assertIn('class="job-properties"', page)
        self.assertIn('<p>First paragraph.</p><p>Second paragraph.</p>', page)
        self.assertIn('.job-properties,.ad-panel{border:1px solid color-mix(', page)

    def test_each_ad_section_folds_on_its_own_summary_in_the_browser(self):
        ctx = sample_context()
        ctx["rows"][0].update({"occupation": "Technikerin",
                               "job_description": "Aufgaben:\nPruefen",
                               "general_info": "Gehalt ab 3.000 EUR"})
        page = ui.render_gui(spec(), ctx)
        for field, label in (("listing.occupation", "Beruf"),
                             ("listing.job_description", "Stellenbeschreibung"),
                             ("listing.general_info", "Allgemeine Informationen")):
            self.assertIn('<details class="ad-section" data-field="%s" open>'
                          '<summary><h3>%s</h3></summary>' % (field, label), page)
        self.assertIn('.ad-section summary{cursor:pointer', page)
        self.assertIn('.ad-section:not([open]) summary::before', page)
        self.assertIn('.ad-section.is-focused', page)

    def test_browser_detail_is_the_same_ladder_as_the_terminal(self):
        page = ui.render_gui(spec(), sample_context())
        # Sections, then paragraphs inside the focused one, one cursor per
        # level, and folding kept apart from forward.
        self.assertIn("var sectionIndex = -1;", page)
        self.assertIn("var inSection = false;", page)
        self.assertIn("function focusSection(step)", page)
        self.assertIn("function paragraphsOf(section)", page)
        self.assertIn("function enterSection()", page)
        self.assertIn("function toggleSectionFold()", page)
        self.assertIn("function leaveSection()", page)
        self.assertNotIn("foldAllSections", page)
        # j/k walk sections until a section is entered, then its paragraphs;
        # h leaves the section before it leaves the listing; space folds.
        self.assertIn("if (inSection) stepParagraph(key === 'j' ? 1 : -1);\n"
                      "        else focusSection(key === 'j' ? 1 : -1);", page)
        self.assertIn("if (inSection) leaveSection(); else closeJob();", page)
        self.assertIn("toggleSectionFold();", page)
        # A paragraph inside a folded section still reports a box, so the
        # paragraph walk checks the open state of every wrapping details.
        self.assertIn("details.ad-section, details.ad-archive", page)
        self.assertIn("details:not([open])", page)

    def test_bookmark_status_is_visible_in_main_and_expanded_listing(self):
        ctx = sample_context()
        ctx["rows"][0]["bookmarked"] = True
        page = ui.render_gui(spec(), ctx)
        list_row = page.split('<button type="button" class="job-row"', 1)[1].split('</button>', 1)[0]
        detail_card = page.split('<section class="job-card" data-index="0"', 1)[1].split('</section>', 1)[0]
        self.assertIn('data-bookmarked="true"', list_row)
        self.assertIn('aria-label="Bookmark">Bookmark</span>', list_row)
        self.assertIn('class="bookmark-state flag is-info"', detail_card)
        self.assertIn('>Bookmark</span>', detail_card)
        self.assertNotIn("★", page)

    def test_unbookmarked_listings_do_not_show_a_bookmark_status(self):
        page = ui.render_gui(spec(), sample_context())
        list_row = page.split('<button type="button" class="job-row"', 1)[1].split('</button>', 1)[0]
        detail_card = page.split('<section class="job-card" data-index="0"', 1)[1].split('</section>', 1)[0]
        self.assertIn('data-bookmarked="false"', list_row)
        self.assertIn('class="job-row-bookmark"', list_row)
        self.assertIn('aria-label="Bookmark" hidden>Bookmark</span>', list_row)
        self.assertIn('class="bookmark-state flag is-info"', detail_card)
        self.assertIn('aria-label="Bookmark" hidden>Bookmark</span>', detail_card)
        self.assertIn('.bookmark-state[hidden]{display:none}', page)

    def test_bookmark_action_uses_the_tracker_endpoint_not_browser_local_storage(self):
        page = ui.render_gui(spec(), sample_context())
        self.assertIn("fetch('/api/bookmark'", page)
        self.assertIn("button.dataset.action === 'toggle_bookmark'", page)
        self.assertNotIn('jobui.bookmarks.v1', page)
        self.assertNotIn('Stored in this browser only', page)

    def test_nudged_is_labelled_as_an_internal_reminder_not_asked(self):
        data = spec()
        self.assertEqual('Reminder', ui.vocabulary_label(data, 'statuses', 'nudged'))
        page = ui.render_gui(data, sample_context())
        self.assertIn('Step back one stage', page)
        self.assertNotIn('Back to asked', page)

    def test_unavailable_sections_are_shown_honestly_not_inferred_from_title(self):
        ctx = sample_context()
        ctx["rows"][0]["snippet"] = "Short preview..."
        page = ui.render_gui(spec(), ctx)
        self.assertIn("Unternehmensbeschreibung", page)
        self.assertIn("Beruf", page)
        self.assertIn("Stellenbeschreibung", page)
        self.assertIn("Not captured", page)
        self.assertNotIn("<dt>Beruf</dt><dd>Example Technician</dd>", page)

    def test_full_listing_text_replaces_snippet_when_present(self):
        ctx = sample_context()
        ctx["rows"][0]["snippet"] = "Short preview..."
        ctx["rows"][0]["full_text"] = "Complete ad\n\nTasks <script>alert(1)</script>\nApply by email"
        page = ui.render_gui(spec(), ctx)
        self.assertIn('<h3>Full listing text</h3>', page)
        self.assertIn('<p>Complete ad</p><p>Tasks &lt;script&gt;', page)
        self.assertNotIn('<dt>Snippet</dt>', page)
        self.assertNotIn('Short preview...', page)

    def test_missing_full_listing_text_is_labelled_unavailable_not_faked(self):
        ctx = sample_context()
        ctx["rows"][0]["snippet"] = "Short preview..."
        page = ui.render_gui(spec(), ctx)
        self.assertIn('AMS detail not captured yet', page)
        self.assertIn('may no longer be online', page)
        self.assertIn('<p class="card-caption">AMS search card, cut by AMS to 253 characters</p>', page)
        self.assertIn('<p class="card-body">Short preview...</p>', page)
        self.assertNotIn('<details class="card-preview">', page)
        self.assertNotIn('<dt>Snippet</dt>', page)

    def test_score_is_first_in_detail_flags_not_a_repeated_field(self):
        page = ui.render_gui(spec(), sample_context())
        card = page.split('class="job-card" data-index="0"', 1)[1].split('</section>', 1)[0]
        self.assertIn('class="score-bubble"', card)
        self.assertLess(card.index('class="score-bubble"'), card.index('>HOT</span>'))
        self.assertNotIn('<dt>Score</dt>', card)

    def test_scan_is_a_question_with_a_clear_answer(self):
        page = ui.render_gui(spec(), sample_context())
        self.assertIn('Seen in latest AMS scan', page)
        self.assertNotIn('<dt>Scan</dt><dd>latest scan</dd>', page)

    def test_source_is_one_action_not_a_duplicate_dead_link(self):
        page = ui.render_gui(spec(), sample_context())
        card = page.split('class="job-card" data-index="0"', 1)[1].split('</section>', 1)[0]
        self.assertNotIn('<a href=', card)
        self.assertIn('data-source-link', page)
        self.assertIn('id="action-result"', page)
        self.assertIn('href="https://example.invalid/1" target="_blank" rel="noopener noreferrer"', page)
        self.assertNotIn('Opened the AMS URL in a new tab', page)

    def test_unsafe_source_does_not_create_an_executable_href(self):
        ctx = sample_context()
        ctx["selection"]["listing_url"] = "javascript:alert(1)"
        page = ui.render_gui(spec(), ctx)
        self.assertNotIn('href="javascript:', page)

    def test_list_score_is_smaller_than_expanded_score(self):
        page = ui.render_gui(spec(), sample_context())
        self.assertIn('.score-bubble{display:inline-flex;', page)
        self.assertIn('height:18px;padding:0 5px;border:2px', page)
        self.assertIn('.job-row .score-bubble{height:14px;padding:0 3px;font-size:10px}', page)

    def test_the_browser_accepts_every_stage_it_can_write(self):
        page = ui.render_gui(spec(), sample_context())
        # `read` and `unread` are the first two steps; leaving them out of the
        # whitelist made the first click of the cycle fail.
        match = re.search(r"\((applied\|[a-z|]+)\)\$/i", page)
        self.assertIsNotNone(match)
        allowed = set(match.group(1).split("|"))
        for stage in ("unread", "read", "generated", "checked", "sent", "confirmed",
                      "waiting", "contact", "interview", "rejected", "success",
                      "blocked", "unclear", "skipped"):
            self.assertIn(stage, allowed)

    def test_detail_actions_use_small_icon_and_label_controls(self):
        page = ui.render_gui(spec(), sample_context())
        # Five cycle buttons, the three endings inside the outcome panel, the
        # bookmark and the AMS link. The individual stage jumps are not buttons
        # in the browser any more.
        self.assertEqual(10, page.count('class="act-icon" aria-hidden="true"'))
        for label in ('Step back one stage', 'Advance one stage', 'Mark blocked',
                      'Mark unclear', 'Mark skipped', 'Mark interview', 'Mark fail',
                      'Mark success', 'Bookmark', 'Open AMS listing'):
            self.assertIn('<span class="act-label">%s</span>' % label, page)
        for hidden in ('Mark generating', 'Mark control', 'Mark sending out',
                       'Mark confirming', 'Mark waiting'):
            self.assertNotIn('<span class="act-label">%s</span>' % hidden, page)
        self.assertIn('id="job-actions-caption"', page)
        self.assertIn('Actions - written to the tracker', page)
        self.assertNotIn("button.textContent = 'Show '", page)

    def test_profile_skills_editor_is_a_tab_without_private_data_embedded(self):
        page = ui.render_gui(spec(), sample_context())
        self.assertIn('data-tab="tab.profile"', page)
        self.assertIn('id="profile-skills"', page)
        self.assertIn('id="profile-import"', page)
        self.assertIn('id="skill-add"', page)
        self.assertIn('id="skill-save"', page)
        self.assertIn('id="skill-export"', page)
        self.assertNotIn('Deutsch Muttersprache', page)

    def test_tui_colour_is_opt_in_and_keeps_the_frame_width(self):
        ui.set_theme("catppuccin-mocha")
        ctx = sample_context()
        nav = ui.Nav(spec(), ctx, "tui")
        plain = ui.render_listing_frame(spec(), nav, 60, 20)
        self.assertNotIn("\033[", "\n".join(plain))
        self.assertTrue(any(line.startswith("╭─ 01 ▶") for line in plain))
        ui._COLOUR["on"] = True
        try:
            coloured = ui.render_listing_frame(spec(), nav, 60, 20)
            striped = [ui_ansi_re.sub("", line) for line in coloured]
            self.assertIn("\033[", "\n".join(coloured))
            self.assertIn("38;2;", "\n".join(coloured))
            self.assertIn(ui._fg_sgr("muted") + ";2m", "\n".join(coloured))
            nav.handle("l")
            expanded = "\n".join(ui.render_listing_frame(spec(), nav, 60, 30))
            self.assertIn("48;2;", expanded)
            self.assertTrue(any(line.startswith("╭─ 01 ▶") for line in striped))
            self.assertEqual(len(plain), len(coloured))
            for line in striped:
                self.assertLessEqual(len(line), 60)
            self.assertIn("9.1", "\n".join(striped))
            self.assertNotIn("⟮", "\n".join(striped))
        finally:
            ui._COLOUR["on"] = False

    def test_colour_disabled_when_not_a_tty_or_no_color_is_set(self):
        self.assertFalse(ui._colour_enabled(io.StringIO()))
        with mock_environ(NO_COLOR="1"):
            self.assertFalse(ui._colour_enabled(_FakeTTY()))
        self.assertTrue(ui._colour_enabled(_FakeTTY()))


class _FakeTTY(io.StringIO):
    def isatty(self):
        return True


class mock_environ:
    """Temporarily set environment variables for one assertion."""

    def __init__(self, **values):
        self.values = values
        self.saved = {}

    def __enter__(self):
        for key, value in self.values.items():
            self.saved[key] = os.environ.get(key)
            os.environ[key] = value
        return self

    def __exit__(self, *exc):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return False


def _walk(nodes):
    for node in nodes:
        yield node
        for child in _walk(node.get("children") or []):
            yield child


if __name__ == "__main__":
    unittest.main()
