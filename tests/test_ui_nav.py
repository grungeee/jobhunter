"""List-first terminal navigation driven by the shared interface structure."""
import inspect
import json
import os
import shlex
import sys
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(ROOT, "tools") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "tools"))
import ui  # noqa: E402


def make_context():
    rows = [
        {"id": "a1", "title": "Operations Assistant", "company": "Example Works",
         "location": "Sampletown", "url": "https://example.invalid/a1",
         "score": 8.2, "posted_date": "2026-10-26", "posted_basis": "extraction",
         "in_latest_scan": False, "status": "interview", "stage": "interview",
         "roles": ["maintenance"]},
        {"id": "b2", "title": "Customer Service Associate", "company": "Seed",
         "location": "Sampletown", "url": "https://example.invalid/b2",
         "score": 10.0, "posted_date": "2026-11-10", "posted_basis": "posting",
         "in_latest_scan": True, "status": "nudged", "stage": "read",
         "roles": ["automation"]},
    ]
    return {"session": {"mode": "served", "state": "ready"},
            "facts": {"counts.total": 2, "counts.new": 0, "counts.nudged": 1,
                      "counts.interview": 1, "scan.available": True},
            "rows": rows,
            "selection": {"listing_id": "a1", "listing_url": rows[0]["url"], "row": rows[0]}}


class StageCycleTest(unittest.TestCase):
    """Forward and back walk the pipeline; the last step asks for an outcome."""

    @classmethod
    def setUpClass(cls):
        cls.spec = ui.load()

    def setUp(self):
        ui.set_theme("catppuccin-mocha")
        self.ctx = make_context()
        self.nav = ui.Nav(self.spec, self.ctx)

    def test_prev_resolves_the_step_behind_and_stops_at_the_start(self):
        self.assertEqual("waiting", ui.prev_stage_key(self.spec, "contact"))
        self.assertEqual("unread", ui.prev_stage_key(self.spec, "read"))
        self.assertEqual("", ui.prev_stage_key(self.spec, "unread"))
        # An outcome steps back into the flow, it does not dead-end.
        self.assertEqual("contact", ui.prev_stage_key(self.spec, "rejected"))

    def test_back_button_names_the_previous_stage(self):
        self.nav.handle("l")
        self.nav.focused()["row"]["stage"] = "contact"
        self.nav.handle("p")
        self.assertIn("mark b2 waiting", self.nav.message)

    def test_forward_writes_the_next_stage(self):
        self.nav.handle("l")
        self.nav.focused()["row"]["stage"] = "read"
        self.nav.handle("n")
        self.assertIn("generated", self.nav.message)

    def test_forward_at_the_last_step_asks_between_interview_and_rejected(self):
        self.nav.handle("l")
        self.nav.focused()["row"]["stage"] = "contact"
        self.nav.handle("n")
        self.assertIsNotNone(self.nav.popup)
        self.assertEqual(["1", "2", "3"], sorted(self.nav.popup_choices))
        text = "\n".join(self.nav.popup[1])
        self.assertIn("Interview", text)
        self.assertIn("Fail", text)
        self.nav.handle("2")
        self.assertIsNone(self.nav.popup)
        self.assertIn("mark b2 rejected", self.nav.message)

    def test_escape_cancels_the_outcome_question_without_a_write(self):
        self.nav.handle("l")
        self.nav.focused()["row"]["stage"] = "contact"
        self.nav.handle("n")
        self.nav.handle("escape")
        self.assertIsNone(self.nav.popup)
        self.assertEqual({}, self.nav.popup_choices)


def _walk_tree(nodes):
    for node in nodes:
        yield node
        for child in _walk_tree(node.get("children") or []):
            yield child


def _node(spec, node_id):
    return next(node for node in _walk_tree(spec["tree"]) if node.get("id") == node_id)


class _WriteResult:
    """What a declared writer transport hands back to the menu."""

    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _tracker_echo(**fields):
    """Format the writer's answer the way ams_nudge.py prints it."""
    body = dict({"ok": True, "uuid": "a1", "title": "Operations Assistant"}, **fields)
    return "OK for a1\n" + json.dumps(body, indent=1, ensure_ascii=False) + "\n"


class BookmarkAndHelpTest(unittest.TestCase):
    """Bookmarks are tracker state and `?` is the one place every key is named."""

    @classmethod
    def setUpClass(cls):
        cls.spec = ui.load()

    def setUp(self):
        ui.set_theme("catppuccin-mocha")
        self.ctx = make_context()
        self.ctx["rows"][0]["bookmarked"] = True
        self.calls = []
        self.nav = ui.Nav(self.spec, self.ctx,
                          executor=lambda command, **kwargs: self.calls.append(command))

    def test_the_bookmark_key_writes_through_the_tracker(self):
        self.nav.focused()
        self.nav.handle("B")
        self.assertTrue(self.calls, "B must write")
        self.assertIn("dashboard.py bookmark", self.calls[-1])

    def test_a_bookmarked_row_carries_a_star_on_its_card(self):
        self.nav.focused()
        lines = ui.render_listing_frame(self.spec, self.nav, 100, 30)
        starred = [line for line in lines if "Operations Assistant" in line]
        self.assertTrue(starred)
        self.assertIn("★", starred[0])
        plain = [line for line in lines if "Customer Service Associate" in line]
        self.assertNotIn("★", plain[0])

    def test_a_confirmed_bookmark_turns_the_star_on_even_when_the_feed_is_stale(self):
        # The Mac client and the browser read a feed file, so the row they hold
        # predates the write. The tracker's own echo has to close that gap.
        stale = make_context()
        echo = _tracker_echo(bookmarked=True, bookmarked_at="2026-11-16T10:46:34+01:00")
        nav = ui.Nav(self.spec, stale, loader=make_context,
                     executor=lambda command, **kwargs: _WriteResult(echo))
        nav.focused()
        nav.handle("B")
        self.assertIn("Bookmark on", nav.message)
        row = next(row for row in nav.ctx["rows"] if row["id"] == "a1")
        self.assertTrue(row["bookmarked"])
        starred = [line for line in ui.render_listing_frame(self.spec, nav, 100, 30)
                   if "Operations Assistant" in line]
        self.assertIn("★", starred[0])

    def test_a_confirmed_unbookmark_clears_the_star_and_the_filter_row(self):
        stale = make_context()
        stale["rows"][0]["bookmarked"] = True
        echo = _tracker_echo(bookmarked=False, bookmarked_at="")
        nav = ui.Nav(self.spec, stale, loader=make_context,
                     executor=lambda command, **kwargs: _WriteResult(echo))
        nav.focused()
        nav.handle("B")
        self.assertIn("Bookmark off", nav.message)
        row = next(row for row in nav.ctx["rows"] if row["id"] == "a1")
        self.assertFalse(row["bookmarked"])
        starred = [line for line in ui.render_listing_frame(self.spec, nav, 100, 30)
                   if "Operations Assistant" in line]
        self.assertNotIn("★", starred[0])

    def test_a_rejected_write_leaves_the_row_alone(self):
        stale = make_context()
        result = _WriteResult("FAIL: tracker rejected the bookmark: unknown job a1\n", 1)
        nav = ui.Nav(self.spec, stale, loader=make_context,
                     executor=lambda command, **kwargs: result)
        nav.focused()
        nav.handle("B")
        self.assertIn("failed", nav.message)
        row = next(row for row in nav.ctx["rows"] if row["id"] == "a1")
        self.assertNotIn("bookmarked", row)

    def test_a_second_status_filter_replaces_the_first(self):
        # A record has one status, so the filter is one lens: picking blocked
        # after new must stop showing new.
        self.nav.apply_filter(_node(self.spec, "filter.new"))
        self.nav.apply_filter(_node(self.spec, "filter.blocked"))
        self.assertEqual({"blocked"}, self.nav.statuses)
        self.assertEqual([], [row["title"] for row in self.nav.rows()])
        self.nav.apply_filter(_node(self.spec, "filter.blocked"))
        self.assertEqual(set(), self.nav.statuses)
        self.assertEqual(2, len(self.nav.rows()))

    def test_the_status_filter_narrows_to_the_trackers_live_state(self):
        ctx = ui._overlay_tracker(make_context(), {"a1": {"stage": "blocked", "status": "blocked"}})
        nav = ui.Nav(self.spec, ctx, executor=lambda command, **kwargs: None)
        nav.apply_filter(_node(self.spec, "filter.new"))
        self.assertEqual([], [row["title"] for row in nav.rows()])
        nav.apply_filter(_node(self.spec, "filter.blocked"))
        self.assertEqual(["Operations Assistant"], [row["title"] for row in nav.rows()])

    def test_current_stage_picker_filters_generated_applications_exactly(self):
        ctx = make_context()
        ctx["rows"].append({"id": "c3", "title": "Generated Application",
                            "company": "Gamma", "location": "Sampletown", "score": 8.5,
                            "status": "new", "stage": "generated",
                            "in_latest_scan": True})
        nav = ui.Nav(self.spec, ctx)
        nav.handle("S")
        self.assertIn("Generating application", "\n".join(nav.popup[1]))
        nav.handle("3")
        self.assertEqual("generated", nav.stage_filter)
        self.assertEqual(["Generated Application"], [row["title"] for row in nav.rows()])
        nav.apply_filter(_node(self.spec, "filter.new"))
        self.assertEqual(["Generated Application"], [row["title"] for row in nav.rows()])
        nav.apply_filter(_node(self.spec, "filter.interview"))
        self.assertEqual([], nav.rows())

    def test_stage_filter_replaces_the_previous_stage_and_can_be_cleared(self):
        ctx = make_context()
        ctx["rows"].append({"id": "c3", "title": "Generated Application",
                            "company": "Gamma", "location": "Sampletown", "score": 8.5,
                            "status": "new", "stage": "generated",
                            "in_latest_scan": True})
        nav = ui.Nav(self.spec, ctx)
        nav.handle("S")
        nav.handle("2")
        self.assertEqual("read", nav.stage_filter)
        self.assertEqual(["Customer Service Associate"], [row["title"] for row in nav.rows()])
        nav.handle("S")
        nav.handle("3")
        self.assertEqual("generated", nav.stage_filter)
        self.assertEqual(["Generated Application"], [row["title"] for row in nav.rows()])
        nav.handle("S")
        nav.handle("0")
        self.assertEqual("", nav.stage_filter)
        self.assertEqual(3, len(nav.rows()))
        nav.handle("S")
        nav.handle("3")
        nav.handle("r")
        self.assertEqual("", nav.stage_filter)
        self.assertEqual(set(), nav.statuses)

    def test_the_bookmark_filter_keeps_only_starred_rows(self):
        node = next(node for node in _walk_tree(self.spec["tree"])
                    if node.get("id") == "filter.bookmarked")
        self.nav.apply_filter(node)
        self.assertTrue(self.nav.bookmarked_only)
        titles = [row["title"] for row in self.nav.rows()]
        self.assertEqual(titles, ["Operations Assistant"])
        self.nav.handle("r")
        self.assertFalse(self.nav.bookmarked_only)
        self.assertEqual(len(self.nav.rows()), 2)

    def test_question_mark_opens_the_key_list_and_closes_on_any_key(self):
        self.nav.focused()
        self.nav.handle("?")
        self.assertTrue(self.nav.show_help)
        frame = "\n".join(ui.render_listing_frame(self.spec, self.nav, 100, 40))
        for needle in ("Keys", "bookmarked only", "call/contact", "any key closes"):
            self.assertIn(needle, frame)
        self.nav.handle("j")
        self.assertFalse(self.nav.show_help)
        self.assertNotIn("any key closes",
                         "\n".join(ui.render_listing_frame(self.spec, self.nav, 100, 40)))

    def test_the_help_list_names_every_stage_key(self):
        keys = [keys for _, items in ui.HELP_GROUPS for keys, _ in items]
        for key in ("n / p", "m", "e", "a", "c", "u", "C", "i", "d", "W", "b", "U", "K", "B", "S"):
            self.assertIn(key, keys)


class TrackerStateOverlayTest(unittest.TestCase):
    """The feed on disk is a snapshot; the tracker store is the state."""

    @classmethod
    def setUpClass(cls):
        cls.spec = ui.load()

    def test_a_stale_feed_row_takes_the_trackers_stage_and_status(self):
        stale = make_context()
        stale["facts"]["counts.applied"] = 0
        ctx = ui._overlay_tracker(stale, {
            "a1": {"stage": "sent", "status": "applied",
                   "stage_at": "2026-11-16T09:12:44+01:00",
                   "sent_at": "2026-11-15T17:00:00+01:00",
                   "note": "short letter", "bookmarked": True,
                   "bookmarked_at": "2026-11-16T10:48:32+01:00"}})
        row = next(row for row in ctx["rows"] if row["id"] == "a1")
        self.assertEqual("sent", row["stage"])
        self.assertEqual("applied", row["status"])
        self.assertEqual("2026-11-16", row["stage_at"])
        self.assertEqual("2026-11-15", row["tracked_date"])
        self.assertEqual("short letter", row["note"])
        self.assertTrue(row["bookmarked"])
        self.assertEqual(1, ctx["facts"]["counts.applied"])
        self.assertEqual(0, ctx["facts"]["counts.interview"])

    def test_the_overlaid_stage_is_the_one_the_gauge_and_the_filters_use(self):
        ctx = ui._overlay_tracker(make_context(), {
            "a1": {"stage": "blocked", "status": "blocked"}})
        nav = ui.Nav(self.spec, ctx, executor=lambda command, **kwargs: None)
        frame = "\n".join(ui.render_listing_frame(self.spec, nav, 100, 30))
        self.assertIn("Blocked", frame)
        nav.apply_filter(next(node for node in _walk_tree(self.spec["tree"])
                              if node.get("id") == "filter.blocked"))
        self.assertEqual(["Operations Assistant"], [row["title"] for row in nav.rows()])

    def test_a_row_the_tracker_never_touched_keeps_what_the_feed_says(self):
        before = make_context()
        after = ui._overlay_tracker(make_context(), {"zz": {"stage": "sent"}})
        self.assertEqual([row["stage"] for row in before["rows"]],
                         [row["stage"] for row in after["rows"]])

    def test_no_tracker_state_leaves_the_context_alone(self):
        self.assertIsNotNone(ui._overlay_tracker(make_context(), {}))
        row = ui._overlay_tracker(make_context(), {})["rows"][0]
        self.assertEqual("interview", row["stage"])


class ClickedButtonTest(unittest.TestCase):
    """A click on a button pill runs the action its key runs, and nothing else."""

    @classmethod
    def setUpClass(cls):
        cls.spec = ui.load()

    def setUp(self):
        ui.set_theme("catppuccin-mocha")
        self.calls = []
        self.nav = ui.Nav(self.spec, make_context(),
                          executor=lambda command, **kwargs: self.calls.append(command) or _WriteResult(""))

    def _paint(self):
        self.nav.last_frame = ui.frame_lines(
            ui.render_listing_frame(self.spec, self.nav, 110, 34))

    def _click_on(self, needle):
        for index, line in enumerate(self.nav.last_frame):
            plain = ui._unstyled(line)
            if "ACTIONS" in plain and needle in plain:
                return ("click", plain.find(needle) + 2, index + 1)
        self.fail("no %r pill in the painted band" % needle)

    def test_a_click_on_the_block_button_blocks_the_listing(self):
        self.nav.focused()
        self.nav.handle("l")
        self._paint()
        target = self.nav.acting_row_id()
        self.nav.handle(self._click_on("b Block"))
        self.assertIn("dashboard.py mark %s blocked" % target, self.calls[-1])

    def test_a_click_off_the_pills_touches_nothing(self):
        self.nav.handle("l")
        self._paint()
        self.nav.handle(("click", 1, 1))
        self.assertEqual([], self.calls)


class TerminalMouseTest(unittest.TestCase):
    """Text selection and copy must work without a manual escape hatch."""

    @classmethod
    def setUpClass(cls):
        cls.spec = ui.load()

    def test_the_mouse_is_not_claimed_by_default(self):
        nav = ui.Nav(self.spec, make_context())
        # Drag to select only reaches the terminal while no mouse reporting is
        # on, so the wheel is opt-in.
        self.assertFalse(nav.mouse)
        self.assertIn("\u001b[?1002h", ui.MOUSE_ON)
        self.assertIn("\u001b[?1002l", ui.MOUSE_OFF)
        self.assertIn("M wheel", "\n".join(ui.render_nav_frame(self.spec, nav, 110, 26)))

    def test_jobhunter_is_the_primary_command_and_jobui_is_a_compatibility_alias(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.assertTrue(os.path.isfile(os.path.join(repo, "ui", "jobhunter")))
        alias = open(os.path.join(repo, "ui", "jobui"), encoding="utf-8").read()
        self.assertIn("/jobhunter", alias)

    def test_local_executor_sets_remote_write_opt_in_when_writer_is_absent(self):
        source = inspect.getsource(ui.local_executor)
        self.assertIn("JOBUI_REMOTE_WRITES", source)
        self.assertIn("ams_nudge.py", source)

    def test_local_client_refresh_does_not_rebuild_from_server_paths(self):
        source = inspect.getsource(ui.main)
        self.assertIn("_live_context(args.feed)", source)
        self.assertNotIn("_refreshed_context(args.feed)", source)
        # The host split lives in one helper: a rebuild only where the writer
        # is, the tracker's own state folded over the synced feed elsewhere.
        helper = inspect.getsource(ui._live_context)
        self.assertIn("_refreshed_context", helper)
        self.assertIn("_context_with_tracker_state", helper)
        self.assertIn("_context_from_feed_file",
                      inspect.getsource(ui._context_with_tracker_state))

    def test_terminal_redraw_is_a_bounded_in_place_view(self):
        source = inspect.getsource(ui.run_nav)
        self.assertIn("\\x1b[H\\x1b[2J", source)
        self.assertIn("\\x1b[2K", source)
        self.assertIn("\\x1b[J", source)
        self.assertNotIn("1049h", source)

    def test_terminal_mode_is_released_even_if_an_old_run_claimed_the_wheel(self):
        source = inspect.getsource(ui.run_nav)
        self.assertIn("MOUSE_OFF.encode", source)
        self.assertIn("MOUSE_ON if nav.mouse else MOUSE_OFF", source)

    def test_tui_stays_in_the_normal_screen_buffer_and_clears_frame_bounds(self):
        source = inspect.getsource(ui.run_nav)
        self.assertNotIn("\\x1b[?1049h", source)
        self.assertIn("\\x1b[2K", source)
        self.assertIn("\\x1b[J", source)
        self.assertIn("MOUSE_OFF", source)

    def test_the_mouse_toggle_takes_the_wheel_and_gives_it_back(self):
        nav = ui.Nav(self.spec, make_context())
        nav.handle("M")
        self.assertTrue(nav.mouse)
        self.assertIn("mouse on", nav.message)
        nav.handle("M")
        self.assertFalse(nav.mouse)
        self.assertIn("mouse off", nav.message)

    def test_the_generated_stage_stays_on_its_own_key(self):
        nav = ui.Nav(self.spec, make_context())
        nav.handle("m")
        self.assertFalse(nav.mouse)
        self.assertRegex(nav.message, r"generated|Mark generated")


class ListViewActionTest(unittest.TestCase):
    """Stage keys write the focused row without opening it first."""

    @classmethod
    def setUpClass(cls):
        cls.spec = ui.load()

    def setUp(self):
        self.commands = []

        def executor(command, cwd=None, timeout=120):
            self.commands.append(command)
            return subprocess.CompletedProcess(args=[command], returncode=0,
                                               stdout="ok", stderr="")

        self.ctx = make_context()
        self.nav = ui.Nav(self.spec, self.ctx, executor=executor)

    def test_blocked_from_the_list_writes_the_focused_row(self):
        self.nav.handle("b")
        self.assertEqual(1, len(self.commands))
        self.assertRegex(self.commands[0], r"mark b2 blocked")

    def test_next_from_the_list_walks_the_focused_row(self):
        self.nav.focused()["row"]["stage"] = "read"
        self.nav.handle("n")
        self.assertRegex(self.commands[0], r"mark b2 generated")

    def test_the_same_stage_key_twice_steps_back(self):
        """b on an already blocked listing undoes the block instead of repeating it."""
        def executor(command, cwd=None, timeout=120):
            self.commands.append(command)
            if "unstage" in command:
                echo = _tracker_echo(uuid="b2", stage="sent", status="applied",
                                     undone="blocked")
            else:
                echo = _tracker_echo(uuid="b2", stage="blocked", status="blocked")
            return subprocess.CompletedProcess(args=[command], returncode=0,
                                               stdout=echo, stderr="")

        nav = ui.Nav(self.spec, make_context(), executor=executor)
        nav.handle("b")
        self.assertRegex(self.commands[-1], r"mark b2 blocked")
        nav.handle("b")
        self.assertRegex(self.commands[-1], r"dashboard\.py unstage b2$")
        self.assertIn("pressed twice", nav.message)
        self.assertIn("Sending out", nav.message)

    def test_a_listing_is_never_marked_without_a_row(self):
        nav = ui.Nav(self.spec, ui.empty_context(), executor=lambda *a, **k: self.commands.append(a))
        nav.handle("b")
        self.assertEqual([], self.commands)
        self.assertIn("select a listing row", nav.message)


class ListingColumnsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = ui.load()

    def test_cards_share_one_column_grid_whatever_the_text_length(self):
        ui.set_theme("catppuccin-mocha")
        ctx = make_context()
        ctx["rows"][0].update({"pay_monthly_min": 2920.0})
        ctx["rows"][1].update({"pay_monthly_min": None, "pay_yearly_min": 53200.0,
                               "pay_monthly_calc": 3800.0})
        nav = ui.Nav(self.spec, ctx)
        frame = ui.render_listing_frame(self.spec, nav, 120, 26)
        bodies = [line for line in frame if line.startswith("\u2502 ") and "/mo" in line]
        self.assertEqual(2, len(bodies), "\n".join(frame))
        stage_at = [min(line.index(glyph) for glyph in ("\u25cf", "\u25c9", "\u25cb")
                        if glyph in line) for line in bodies]
        self.assertEqual(stage_at[0], stage_at[1])
        # The pay cell is padded to the shared width, so the flags behind it
        # start in the same column even though one cell is longer than the other.
        flags_at = [min(line.index(flag) for flag in ("HOT", "FRESH", "MATCH", "STALE")
                        if flag in line) for line in bodies]
        self.assertEqual(flags_at[0], flags_at[1])
        self.assertIn("(yr/14)", bodies[0])


class TerminalRenderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = ui.load()

    def setUp(self):
        ui.set_theme("catppuccin-mocha")
        self.ctx = make_context()
        self.nav = ui.Nav(self.spec, self.ctx)

    def test_first_view_contains_score_location_and_score_order(self):
        entries = self.nav.entries()
        self.assertEqual([entry["label"] for entry in entries],
                         ["Customer Service Associate", "Operations Assistant"])
        self.assertTrue(all(entry["kind"] == "item" for entry in entries))
        self.assertEqual(self.nav.focused()["row"]["id"], "b2")
        frame = "\n".join(ui.render_nav_frame(self.spec, self.nav, 78, 18))
        self.assertIn("Job Search OS", frame)
        self.assertIn("Listings [2]", frame)
        self.assertIn("Customer Service Associate", frame)
        self.assertIn("Operations Assistant", frame)
        self.assertIn("10.0", frame)
        self.assertIn("8.2", frame)
        self.assertIn("Sampletown", frame)
        self.assertIn("Open AMS", frame)
        self.assertIn("╭─ Filters", frame)
        self.assertNotIn("Example Works", frame)
        self.assertNotIn("[MATCH]", frame)
        self.assertNotIn("Score:", frame)

    def test_unread_stage_leaves_every_circle_empty(self):
        ctx = make_context()
        ctx["rows"][1].pop("stage", None)          # never opened
        nav = ui.Nav(self.spec, ctx)
        strip = ui.stage_strip(self.spec, ctx["rows"][1])
        self.assertEqual(strip[0].strip().split(" ")[0], "○○○○○○○")
        self.assertIn("Not read", strip[0])
        state = ui.stage_gauge_state(self.spec, ctx["rows"][1])
        self.assertEqual(state[0], "unread")

    def test_opening_a_listing_marks_it_read_through_the_writer(self):
        calls = []
        ctx = make_context()
        ctx["rows"][1].pop("stage", None)
        nav = ui.Nav(self.spec, ctx, executor=lambda command, **kwargs: calls.append(command))
        nav.focused()
        nav.handle("l")
        self.assertTrue(calls, "opening an unread listing must run the writer")
        self.assertIn("mark", calls[0])
        self.assertTrue(calls[0].rstrip().endswith(" read"), calls[0])

    def test_next_resolves_to_the_following_stage_not_the_word_next(self):
        calls = []
        ctx = make_context()
        ctx["rows"][1]["stage"] = "sent"      # b2 is the focused listing
        nav = ui.Nav(self.spec, ctx, executor=lambda command, **kwargs: calls.append(command))
        nav.focused()
        nav.handle("l")
        nav.handle("n")
        self.assertTrue(calls, "next must move the pipeline along")
        self.assertTrue(calls[-1].rstrip().endswith(" confirmed"), calls[-1])
        self.assertNotIn(" next", calls[-1])

    def test_opening_a_read_listing_writes_nothing(self):
        calls = []
        nav = ui.Nav(self.spec, make_context(),
                     executor=lambda command, **kwargs: calls.append(command))
        nav.focused()
        nav.handle("l")
        self.assertEqual(calls, [])

    def test_j_k_and_arrow_keys_move_through_list(self):
        self.nav.handle("j")
        self.assertEqual(self.nav.focused()["row"]["id"], "a1")
        self.nav.handle("k")
        self.assertEqual(self.nav.focused()["row"]["id"], "b2")
        self.nav.handle("down")
        self.assertEqual(self.nav.focused()["row"]["id"], "a1")
        self.nav.handle("up")
        self.assertEqual(self.nav.focused()["row"]["id"], "b2")

    def test_l_expands_h_returns_to_same_list_position(self):
        self.nav.handle("j")
        self.nav.handle("l")
        self.assertEqual("a1", self.nav.expanded_id)
        frame = "\n".join(ui.render_nav_frame(self.spec, self.nav, 96, 60))
        self.assertIn("Example Works", frame)
        posted_line = next(line for line in frame.splitlines() if "Posted:" in line)
        self.assertIn("2026-10-26", posted_line)
        self.assertIn("Date basis: Last seen in AMS scan", frame)
        self.nav.handle("h")
        self.assertIsNone(self.nav.expanded_id)
        self.assertEqual("a1", self.nav.focused()["row"]["id"])

    def test_expanded_record_places_score_before_flags_and_clarifies_scan(self):
        self.nav.handle("l")
        frame = "\n".join(ui.render_nav_frame(self.spec, self.nav, 100, 60))
        match_row = next(line for line in frame.splitlines() if "HOT" in line and "MATCH" in line)
        self.assertLess(match_row.index("10.0"), match_row.index("HOT"))
        self.assertIn("Seen in latest AMS scan: Yes", frame)
        self.assertNotIn("Score: 10.0", frame)

    def test_ad_sections_own_boxes_actions_own_band_keyboard_inline(self):
        self.ctx["rows"][1]["general_info"] = "Minimum pay is stated here."
        self.ctx["rows"][1]["job_description"] = "First paragraph.\nSecond paragraph."
        self.nav.handle("l")
        frame = "\n".join(ui.render_listing_frame(self.spec, self.nav, 100, 100))
        for section in ("Properties", "Stellenbeschreibung",
                        "Allgemeine Informationen"):
            self.assertIn("╭─ " + section, frame)
        # Score, flags and the stage live in the header, not in a Match box.
        self.assertNotIn("╭─ Match", frame)
        header = frame.splitlines()[0]
        for token in ("Customer Service Associate", "10.0", "HOT", "Read"):
            self.assertIn(token, header)
        self.assertIn("ACTIONS", frame)
        self.assertNotIn("╭─ Read-only actions", frame)
        self.assertNotIn("╭─ Keyboard", frame)
        self.assertNotIn("a applied  i interview", frame)
        self.assertNotIn("⟮", frame)
        self.assertNotIn("⟯", frame)
        self.assertLess(frame.index("Minimum pay is stated here."),
                        frame.index("ACTIONS"))

    def test_profile_view_is_reachable_in_terminal(self):
        for _ in range(3):
            self.nav.handle("]")
        self.assertEqual("tab.profile", self.nav.active_tab()["id"])
        self.assertTrue(any(e["id"] == "profile.skills" for e in self.nav.entries()))

    def test_right_left_and_enter_use_the_same_levels(self):
        self.nav.handle("right")
        self.assertEqual("b2", self.nav.expanded_id)
        self.nav.handle("left")
        self.assertIsNone(self.nav.expanded_id)
        self.nav.handle("enter")
        self.assertEqual("b2", self.nav.expanded_id)

    def card_index(self, section_id):
        return next(index for index, span in enumerate(self.nav.detail_spans())
                    if span["id"] == section_id)

    def test_j_k_walk_the_sections_of_an_open_listing(self):
        self.ctx["rows"][1]["job_description"] = "First paragraph.\nSecond paragraph."
        self.nav.handle("l")
        self.assertEqual("elements", self.nav.detail_level)
        self.assertEqual(0, self.nav.detail_card)
        self.nav.handle("j")
        self.assertEqual(1, self.nav.detail_card)
        self.assertEqual("elements", self.nav.detail_level)
        self.assertEqual(0, self.nav.detail_para)
        self.assertEqual("b2", self.nav.focused()["row"]["id"])
        frame = "\n".join(ui.render_listing_frame(self.spec, self.nav, 100, 40))
        self.assertIn(self.nav.detail_spans()[1]["title"] + "  ›", frame)
        self.nav.handle("k")
        self.assertEqual(0, self.nav.detail_card)

    def test_j_k_stop_at_the_last_section_and_keep_it_in_the_window(self):
        self.ctx["rows"][1]["job_description"] = "One.\nTwo."
        self.nav.handle("l")
        for _ in range(12):
            self.nav.handle("j")
        spans = self.nav.detail_spans()
        self.assertEqual(len(spans) - 1, self.nav.detail_card)
        frame = "\n".join(ui.render_listing_frame(self.spec, self.nav, 100, 22))
        self.assertIn(spans[-1]["title"] + "  ›", frame)
        self.assertGreater(self.nav.detail_scroll, 0)

    def test_right_steps_into_the_section_and_j_k_walk_its_paragraphs(self):
        self.ctx["rows"][1]["job_description"] = ("First paragraph.\n"
                                                  "Second paragraph.\n"
                                                  "Third paragraph.")
        self.nav.handle("l")
        self.nav.detail_card = self.card_index("field.job_description")
        self.nav.handle("l")
        self.assertEqual("paragraphs", self.nav.detail_level)
        self.assertEqual(0, self.nav.detail_para)
        self.nav.handle("j")
        self.assertEqual(1, self.nav.detail_para)
        frame = "\n".join(ui.render_listing_frame(self.spec, self.nav, 100, 26))
        self.assertIn("Stellenbeschreibung  ·  2/3", frame)
        self.assertIn("Second paragraph.", frame)
        self.nav.handle("j")
        self.nav.handle("j")
        self.assertEqual(2, self.nav.detail_para)
        self.nav.handle("k")
        self.assertEqual(1, self.nav.detail_para)

    def test_left_leaves_the_section_then_the_listing(self):
        self.ctx["rows"][1]["job_description"] = "First paragraph.\nSecond paragraph."
        self.nav.handle("l")
        self.nav.detail_card = self.card_index("field.job_description")
        self.nav.handle("l")
        self.assertEqual("paragraphs", self.nav.detail_level)
        self.nav.handle("h")
        self.assertEqual("elements", self.nav.detail_level)
        self.assertEqual("b2", self.nav.expanded_id)
        self.assertIn("out of Stellenbeschreibung", self.nav.message)
        self.nav.handle("h")
        self.assertIsNone(self.nav.expanded_id)

    def test_forward_only_steps_in_and_never_folds(self):
        self.ctx["rows"][1]["job_description"] = "First paragraph.\nSecond paragraph."
        self.nav.handle("l")
        self.nav.detail_card = self.card_index("field.job_description")
        self.nav.handle("l")
        self.assertEqual("paragraphs", self.nav.detail_level)
        self.assertEqual(set(), self.nav.detail_folded)
        self.nav.handle("l")
        self.assertEqual("paragraphs", self.nav.detail_level)
        self.assertEqual(set(), self.nav.detail_folded)
        self.assertIn("already inside Stellenbeschreibung", self.nav.message)
        frame = "\n".join(ui.render_listing_frame(self.spec, self.nav, 100, 30))
        self.assertIn("Second paragraph.", frame)
        self.assertNotIn("(folded)", frame)
        # Going back is h, and it leaves one level at a time.
        self.nav.handle("h")
        self.assertEqual("elements", self.nav.detail_level)
        self.assertEqual("b2", self.nav.expanded_id)
        self.nav.handle("h")
        self.assertIsNone(self.nav.expanded_id)

    def test_space_folds_the_section_under_the_cursor_and_unfolds_it(self):
        self.ctx["rows"][1]["job_description"] = "First paragraph.\nSecond paragraph."
        self.nav.handle("l")
        self.nav.detail_card = self.card_index("field.job_description")
        self.nav.handle("space")
        self.assertEqual({"field.job_description"}, self.nav.detail_folded)
        self.assertIn("folded: Stellenbeschreibung", self.nav.message)
        frame = "\n".join(ui.render_listing_frame(self.spec, self.nav, 100, 30))
        self.assertIn("Stellenbeschreibung", frame)
        self.assertIn("(folded)", frame)
        self.assertNotIn("Second paragraph.", frame)
        self.nav.handle("space")
        self.assertEqual(set(), self.nav.detail_folded)
        frame = "\n".join(ui.render_listing_frame(self.spec, self.nav, 100, 30))
        self.assertIn("Second paragraph.", frame)

    def test_a_folded_section_has_no_paragraphs_to_step_into(self):
        self.ctx["rows"][1]["job_description"] = "One.\nTwo.\nThree."
        self.nav.handle("l")
        index = self.card_index("field.job_description")
        self.assertEqual(3, len(self.nav.detail_spans()[index]["paragraphs"]))
        self.nav.detail_card = index
        self.nav.handle("space")
        span = self.nav.detail_spans()[index]
        self.assertEqual([], span["paragraphs"])
        self.assertTrue(span["folded"])
        # Forward on a folded section opens it again, but never folds anything.
        self.nav.handle("l")
        self.assertNotIn("field.job_description", self.nav.detail_folded)
        self.assertEqual("paragraphs", self.nav.detail_level)

    def test_arrows_keep_line_scrolling_and_the_cursor_takes_it_back(self):
        self.ctx["rows"][1]["job_description"] = "First paragraph.\nSecond paragraph."
        self.nav.handle("l")
        ui.render_listing_frame(self.spec, self.nav, 64, 18)
        self.nav.handle("down")
        self.assertEqual(1, self.nav.detail_scroll)
        self.assertFalse(self.nav.detail_follow)
        self.nav.handle("j")
        self.assertTrue(self.nav.detail_follow)

    def test_reopening_a_listing_starts_unfolded_at_the_first_section(self):
        self.nav.handle("l")
        self.nav.detail_card = self.card_index("field.job_description")
        self.nav.handle("space")
        self.assertEqual({"field.job_description"}, self.nav.detail_folded)
        self.nav.handle("h")
        self.nav.handle("l")
        self.assertEqual(set(), self.nav.detail_folded)
        self.assertEqual(0, self.nav.detail_card)
        self.assertEqual("elements", self.nav.detail_level)
        self.assertEqual(0, self.nav.detail_scroll)

    def test_status_filter_does_not_reassign_city_colours(self):
        self.ctx["rows"].extend([
            {"id": "c3", "title": "Third", "location": "Eastport", "score": 7,
             "status": "nudged"},
            {"id": "d4", "title": "Fourth", "location": "Rivertown", "score": 6,
             "status": "new"},
        ])
        nav = ui.Nav(self.spec, self.ctx)
        full = ui._city_colours([row.get("location") for row in nav.ctx["rows"]])
        nav.statuses.add("nudged")
        self.assertLess(len(nav.rows()), len(nav.ctx["rows"]))
        self.assertEqual({"nudged"}, {row["status"] for row in nav.rows()})
        ui._COLOUR["on"] = True
        try:
            frame = "\n".join(ui.render_listing_frame(self.spec, nav, 96, 28))
            self.assertIn("\033[" + ui._city_sgr("Eastport", full) + "m Eastport ", frame)
        finally:
            ui._COLOUR["on"] = False

    def test_filtering_an_expanded_job_returns_to_the_filtered_list(self):
        self.nav.handle("j")
        self.nav.handle("l")
        self.assertEqual("a1", self.nav.expanded_id)
        self.nav.handle("2")
        self.assertIsNone(self.nav.expanded_id)
        self.assertEqual(["b2"], [row["id"] for row in self.nav.rows()])
        frame = "\n".join(ui.render_listing_frame(self.spec, self.nav, 96, 28))
        self.assertIn("Customer Service Associate", frame)
        self.assertNotIn("Operations Assistant", frame)

    def test_q_in_detail_goes_back_and_q_in_list_exits(self):
        self.nav.handle("l")
        self.nav.handle("q")
        self.assertFalse(self.nav.quit)
        self.assertIsNone(self.nav.expanded_id)
        self.nav.handle("q")
        self.assertTrue(self.nav.quit)

    def test_tabs_remain_reachable_but_are_not_in_the_initial_list(self):
        self.nav.handle("]")
        self.assertEqual("tab.overview", self.nav.active_tab()["id"])
        self.assertTrue(any(e["id"] == "overview.counts" for e in self.nav.entries()))
        self.nav.handle("[")
        self.assertEqual("tab.listings", self.nav.active_tab()["id"])
        self.assertEqual(2, len(self.nav.entries()))

    def test_score_then_comparable_pay_tie_breaker(self):
        rows = [{"id": "x", "title": "X", "score": 9, "pay_monthly_min": None},
                {"id": "y", "title": "Y", "score": 9, "pay_monthly_min": 3200},
                {"id": "z", "title": "Z", "score": 9, "pay_monthly_min": 4100},
                {"id": "w", "title": "W", "score": 10, "pay_monthly_min": None},
                {"id": "n", "title": "N", "score": None, "pay_monthly_min": 9999}]
        self.assertEqual([r["id"] for r in ui.ranked_listings(rows)],
                         ["w", "z", "y", "x", "n"])

    def test_missing_pay_and_score_stay_missing(self):
        rows = [{"id": "a", "title": "A", "score": 5, "pay_monthly_min": "nan"},
                {"id": "b", "title": "B", "score": 5, "pay_monthly_min": 2000}]
        self.assertEqual([r["id"] for r in ui.ranked_listings(rows)], ["b", "a"])

    def test_search_keeps_only_matching_titles(self):
        self.nav.query = "Example Works"
        self.assertEqual([r["id"] for r in self.nav.rows()], ["a1"])
        self.assertEqual([e["label"] for e in self.nav.entries()], ["Operations Assistant"])

    def test_flags_live_only_in_the_detail_view(self):
        item = self.nav.entries()[0]["node"]
        flags = ui.row_flags(self.spec, self.ctx, "tui", item, self.nav.entries()[0]["row"])
        self.assertIn("HOT", [f["label"] for f in flags])
        self.assertNotIn("HOT", self.nav.entries()[0]["label"])

    def test_frame_lines_fit_screen_width(self):
        for width in (56, 78, 104):
            for expanded in (False, True):
                nav = ui.Nav(self.spec, make_context())
                if expanded:
                    nav.handle("l")
                frame = ui.render_nav_frame(self.spec, nav, width, 24)
                self.assertTrue(frame)
                self.assertTrue(all(len(line) <= width for line in frame))

    def test_every_tab_paints_text_lines_not_nested_lists(self):
        """A notice card used to nest its own lines and kill the paint loop."""
        previous = ui._COLOUR.get("on")
        try:
            for colour in (True, False):
                ui._COLOUR["on"] = colour
                for index in range(len(self.spec.get("tabs") or [])):
                    nav = ui.Nav(self.spec, make_context())
                    nav.tab_index = index
                    nav.message = "a notice long enough to wrap inside the card"
                    frame = ui.render_nav_frame(self.spec, nav, 96, 26)
                    self.assertTrue(all(isinstance(line, str) for line in frame))
                    self.assertEqual(frame, ui.frame_lines(frame))
        finally:
            ui._COLOUR["on"] = previous

    def test_action_command_is_read_only_data(self):
        cmd = ui.action_command(self.spec, "stage_sent", {"listing_id": "a1"})
        self.assertEqual(cmd, "python3 -B tools/dashboard.py mark a1 sent")

    def test_v_opens_the_focused_safe_ams_listing(self):
        self.ctx["rows"][1]["url"] = "https://jobs.ams.at/suche?id=b2"
        opened = []
        nav = ui.Nav(self.spec, self.ctx, browser_opener=lambda url, new=0:
                     opened.append((url, new)))
        nav.handle("v")
        self.assertEqual(opened, [("https://jobs.ams.at/suche?id=b2", 2)])

    def test_v_rejects_non_ams_listing_urls(self):
        opened = []
        nav = ui.Nav(self.spec, self.ctx, browser_opener=lambda url, new=0:
                     opened.append((url, new)))
        nav.handle("v")
        self.assertEqual(opened, [])
        self.assertEqual("No safe AMS link on this listing", nav.message)

    def test_button_styles_distinguish_active_from_inactive(self):
        active = ";".join(ui._button_sgr("info", True))
        self.assertIn("48;2;", active)
        self.assertIn("38;2;", active)
        self.assertEqual((ui._fg_sgr("muted"), "2"), ui._button_sgr("info", False))

    def test_theme_switch_recolours_every_terminal_component(self):
        themes = ui.load_themes()
        for name in ("catppuccin-mocha", "monokai-pro", "tokyo-night",
                     "dracula", "nord", "gruvbox-dark", "solarized-dark"):
            self.assertIn(name, themes)
        nav = ui.Nav(self.spec, self.ctx)
        ui._COLOUR["on"] = True
        try:
            before = "\n".join(ui.render_listing_frame(self.spec, nav, 96, 35))
            nav.handle("t")
            after = "\n".join(ui.render_listing_frame(self.spec, nav, 96, 35))
            self.assertNotEqual(before, after)
            self.assertIn(nav.theme_label(), after)
            self.assertIn(ui._theme_fg("border"), after)
            self.assertIn("48;2;", ";".join(ui._button_sgr("info", True)))
            self.assertNotIn("⟮", after)
        finally:
            ui._COLOUR["on"] = False

    def test_every_theme_keeps_text_contrast(self):
        def channel(value):
            value = value / 255.0
            return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

        def luminance(colour):
            red, green, blue = ui._rgb(colour)
            return (0.2126 * channel(red) + 0.7152 * channel(green)
                    + 0.0722 * channel(blue))

        def ratio(first, second):
            high, low = sorted((luminance(first), luminance(second)), reverse=True)
            return (high + 0.05) / (low + 0.05)

        for name, theme in ui.load_themes().items():
            for slot in ("fg", "muted"):
                for base in ("bg", "surface"):
                    self.assertGreaterEqual(
                        ratio(theme[slot], theme[base]), 4.5,
                        "%s: %s on %s" % (name, slot, base))
            for slot in ("accent", "success", "warning", "danger"):
                for base in ("bg", "surface", "raised"):
                    self.assertGreaterEqual(
                        ratio(theme[slot], theme[base]), 3.0,
                        "%s: %s on %s" % (name, slot, base))

    def test_header_is_one_line_with_count_listing_and_stage(self):
        ui.set_theme("catppuccin-mocha")
        frame = "\n".join(ui.render_listing_frame(self.spec, self.nav, 100, 26))
        header = frame.splitlines()[0]
        self.assertIn("Listings [2]", header)
        self.assertIn("Customer Service Associate", header)
        self.assertIn("10.0", header)
        self.assertTrue(header.rstrip().endswith("Reading"), header)
        self.assertNotIn(".ai/feed", header)
        # One header row only: the tab bar is gone and nothing else echoes it.
        self.assertNotIn("Catppuccin Mocha", header)

    def test_properties_pair_short_fields_in_two_columns(self):
        self.nav.handle("l")
        frame = "\n".join(ui.render_listing_frame(self.spec, self.nav, 100, 60))
        paired = [line for line in frame.splitlines()
                  if "Location:" in line and "Company:" in line]
        self.assertTrue(paired, "short properties should share a row at width 100")
        lines = frame.splitlines()
        start = next(i for i, line in enumerate(lines) if "Properties" in line and "╭" in line)
        end = next(i for i, line in enumerate(lines[start:], start) if "╰" in line)
        self.assertLess(end - start, 14)

    def test_gui_embeds_every_theme_and_no_legacy_palette(self):
        page = ui.render_gui(self.spec, self.ctx)
        for name, theme in ui.THEMES.items():
            self.assertIn('body[data-theme="%s"]' % name, page)
            self.assertIn(theme["accent"], page)
        self.assertIn('id="theme-select"', page)
        self.assertIn("jobui.theme.v1", page)
        for legacy in ("#0f1115", "#7cc4ff", "#244056", "#b85656"):
            self.assertNotIn(legacy, page)

    def test_tab_bar_is_hidden_and_lives_in_a_menu(self):
        frame = "\n".join(ui.render_listing_frame(self.spec, self.nav, 100, 26))
        self.assertNotIn("▸ Listings", frame)
        self.assertNotIn("Overview", frame)
        self.assertNotIn("Tracker", frame)
        self.nav.handle("ctrl_t")
        menu = "\n".join(ui.render_listing_frame(self.spec, self.nav, 100, 26))
        self.assertIn("Tabs", menu)
        self.assertIn("Overview", menu)
        self.assertIn("Tracker", menu)
        self.nav.handle("2")
        self.assertEqual(self.nav.active_tab()["id"], "tab.overview")

    def test_score_flags_and_stage_sit_in_the_header_line(self):
        self.nav.handle("l")
        lines = ui.render_listing_frame(self.spec, self.nav, 120, 40)
        header, body = lines[0], "\n".join(lines[1:])
        for token in ("10.0", "HOT", "MATCH"):
            self.assertIn(token, header)
        self.assertNotIn("Match", body.split("Properties")[0].split("\n")[1:2])
        self.assertTrue(header.rstrip().endswith("Reading"), header)
        self.assertNotIn("HOT: Score", body)
        # The header is the first content row: no empty line above it.
        self.assertTrue(header.startswith(" Job Search OS"))
        self.assertNotIn("╭─ Process", body)

    def test_first_expansion_explains_flags_once(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            nav = ui.Nav(self.spec, self.ctx, explain_once=True, config_dir=folder)
            nav.handle("l")
            frame = "\n".join(ui.render_listing_frame(self.spec, nav, 100, 30))
            self.assertIn("Flags", frame)
            self.assertIn("score 9.0 or better", frame.lower())
            nav.handle("x")
            self.assertIsNone(nav.popup)
            self.assertIsNotNone(nav.expanded_id)
            again = ui.Nav(self.spec, self.ctx, explain_once=True, config_dir=folder)
            again.handle("l")
            self.assertIsNone(again.popup)
            self.assertTrue(os.path.exists(os.path.join(folder, "flags-explained")))

    def test_actions_are_a_styled_bar_not_a_border_box(self):
        self.nav.handle("l")
        ui._COLOUR["on"] = True
        try:
            lines = ui.render_listing_frame(self.spec, self.nav, 100, 40)
        finally:
            ui._COLOUR["on"] = False
        joined = "\n".join(lines)
        self.assertNotIn("Read-only actions", joined)
        bar = [line for line in lines if "ACTIONS" in line]
        self.assertEqual(len(bar), 1, "the band must keep a single leader row")
        self.assertIn("48;2;", bar[0])
        # Lit steps are named; any remaining jumps pack behind `keys`, on the
        # same row when it fits and on an aligned continuation row when not.
        band = "\n".join(lines[lines.index(bar[0]):lines.index(bar[0]) + 3])
        self.assertNotIn("Mark", band)
        # The band is the stage cycle: back, forward and the three branches.
        for label in ("Back", "Next", "Block", "Unclear", "Skip"):
            self.assertIn(label, bar[0])
        plain = "\n".join(ui.render_listing_frame(self.spec, self.nav, 100, 40))
        self.assertIn("ACTIONS", plain)

    def test_keyboard_hint_is_inline_and_unboxed(self):
        self.nav.handle("l")
        lines = ui.render_listing_frame(self.spec, self.nav, 100, 40)
        last = lines[-1]
        self.assertIn("q back", last)
        self.assertNotIn("╭", last)
        self.assertNotIn("╰", last)
        self.assertNotIn("Keyboard", "\n".join(lines))


    def test_mouse_wheel_scrolls_the_detail_view(self):
        self.assertEqual("wheel_up", ui._mouse_event(b"\x1b[<64;10;5M"))
        self.assertEqual("wheel_down", ui._mouse_event(b"\x1b[<65;10;5M"))
        self.assertEqual(("click", 10, 5), ui._mouse_event(b"\x1b[<0;10;5M"))
        # A release is not a second press, and motion is not a button at all.
        self.assertEqual("mouse_other", ui._mouse_event(b"\x1b[<0;10;5m"))
        self.assertEqual("mouse_other", ui._mouse_event(b"\x1b[<32;10;5M"))
        self.assertIsNone(ui._mouse_event(b"\x1b[A"))
        self.nav.handle("l")
        before = self.nav.detail_scroll
        self.nav.handle("wheel_down")
        self.assertGreater(self.nav.detail_scroll, before)
        self.nav.handle("wheel_up")
        self.assertEqual(before, self.nav.detail_scroll)

    def test_interactive_loop_claims_and_releases_the_wheel(self):
        import inspect
        self.assertIn("1002h", ui.MOUSE_ON)
        self.assertIn("1006h", ui.MOUSE_ON)
        self.assertIn("1002l", ui.MOUSE_OFF)
        source = inspect.getsource(ui.run_nav)
        self.assertLess(source.index("MOUSE_ON"), source.index("while not nav.quit"))
        self.assertGreater(source.rindex("MOUSE_OFF"), source.index("finally"))
        # The loop must re-apply the mode after `m` hands the mouse back.
        self.assertIn("nav.mouse != claimed", source)



class _PacketNav(ui.Nav):
    """A menu whose packet state the test decides, with no folder on disk."""

    ready = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.viewed = []
        self.commands = []

    def packet_ready(self, row_id):
        return self.ready

    def open_packet_view(self, row_id, fresh=False):
        self.viewed.append(row_id)
        return True


class GenerateActionTest(unittest.TestCase):
    """R writes once, asks before regenerating, V opens the review window."""

    @classmethod
    def setUpClass(cls):
        cls.spec = ui.load()

    def setUp(self):
        ui.set_theme("catppuccin-mocha")
        self.calls = []

    def nav(self, ready=False, result=None):
        nav = _PacketNav(self.spec, make_context(), executor=self.executor)
        nav.ready = ready
        self.results = result if result is not None else _WriteResult("")
        return nav

    def executor(self, command, **kwargs):
        self.calls.append(command)
        return self.results

    def test_visible_generate_button_opens_a_scrollable_packet_that_semicolon_reopens(self):
        packet = {"meta.json": {"listing_id": "b2"},
                  "cover-letter.md": "Opening paragraph\n" + "Middle paragraph\n" * 30 + "End of letter"}
        def execute(command, **kwargs):
            self.calls.append(command)
            return _WriteResult(json.dumps(packet) if "dashboard.py packet" in command else "")

        nav = ui.Nav(self.spec, make_context(), executor=execute)
        with mock.patch.object(nav, "_generate") as generator:
            generator.return_value.packet_ready.return_value = False
            generator.return_value.read_packet.return_value = packet
            nav.handle("l")
            nav.last_frame = ui.frame_lines(ui.render_nav_frame(self.spec, nav, 40, 22))
            needle = "R Generate application"
            matches = [(y, ui._unstyled(line)) for y, line in enumerate(nav.last_frame, 1)
                       if needle in ui._unstyled(line)]
            self.assertEqual(1, len(matches), "Generate application needs a visible button")
            y, line = matches[0]
            nav.handle(("click", line.index(needle) + 3, y))
            self.assertEqual([], self.calls)
            nav.handle("enter")
            frame = lambda: "\n".join(ui.render_nav_frame(self.spec, nav, 84, 16))
            self.assertIn("Opening paragraph", frame())
            for _ in range(4):
                nav.handle("page_down")
                frame()
            self.assertIn("End of letter", frame())
            nav.handle("h")
            self.assertIn("Application packet", frame())
            self.assertNotIn("Opening paragraph", frame())
            nav.handle(";")
            self.assertIn("Opening paragraph", frame())
            self.assertEqual(1, sum("dashboard.py generate b2" in call for call in self.calls))
            nav.handle("escape")
            nav.handle("h")
            nav.handle("j")
            nav.handle("l")
            self.assertNotIn("Opening paragraph", frame())

    def test_packet_stays_with_the_listing_when_generation_moves_it_out_of_a_filter(self):
        packet = {"meta.json": {"listing_id": "b2"}, "cover-letter.md": "Draft for b2"}
        def execute(command, **kwargs):
            if "dashboard.py mark" in command:
                return _WriteResult(json.dumps({"uuid": "b2", "stage": "generated", "status": "applied"}))
            return _WriteResult(json.dumps(packet) if "dashboard.py packet" in command else "")

        nav = ui.Nav(self.spec, make_context(), executor=execute)
        nav.stage_filter = "read"
        nav.handle("l")
        nav.handle("R")
        nav.handle("enter")
        self.assertEqual([], nav.rows())
        nav.handle("h")
        nav.handle("l")
        self.assertIsNone(nav.packet_view)
        self.assertEqual("press ; to open the review window", nav.message)
        nav.handle(";")
        self.assertEqual(packet, nav.packet_view)
        self.assertEqual("b2", nav.acting_row_id())

    def test_letter_window_has_a_clean_letter_and_separate_quoted_excerpt_cards(self):
        blocks = [{"text": "First paragraph with a specific phrase.", "excerpt": "specific phrase",
                   "why": "First reason. " + "Long explanation. " * 5},
                  {"text": "Second paragraph", "excerpt": "Second paragraph", "why": "Second reason."}]
        nav = self.nav()
        nav.packet_view = {"meta.json": {"listing_id": "b2"},
                           "cover-letter.md": "\n\n".join(block["text"] for block in blocks),
                           "cover-letter-review.json": blocks}
        nav.packet_view["cv-notes.md"] = "Only in packet details"
        def frame(width, height=24):
            return [ui._unstyled(line) for line in ui.render_nav_frame(self.spec, nav, width, height)]
        wide = frame(96, 26)
        title = next(line for line in wide if "Cover letter" in line and "Why this wording" in line)
        right_edge = title.rindex("╭")
        letter = "\n".join(line[:right_edge] for line in wide)
        notes = "\n".join(line[right_edge:] for line in wide)
        self.assertIn("First paragraph with a specific phrase.", letter)
        self.assertIn("Second paragraph", letter)
        self.assertNotIn("First reason", letter)
        self.assertIn('“specific phrase”', notes)
        self.assertIn('“Second paragraph”', notes)
        self.assertIn("Second reason.", notes)
        self.assertNotIn("Only in packet details", "\n".join(wide))
        self.assertTrue(all(len(line) <= 96 for line in wide))
        narrow = frame(40, 26)
        self.assertTrue(any(line.count("╭") == 2 for line in narrow))
        self.assertTrue(all(len(line) <= 40 for line in narrow))
        before = frame(96, 15)
        nav.handle("tab")
        nav.handle("page_down")
        after = frame(96, 15)
        self.assertEqual(before[4][:right_edge], after[4][:right_edge])
        self.assertNotEqual([line[right_edge:] for line in before], [line[right_edge:] for line in after])
        nav.handle("i")
        self.assertIn("Only in packet details", "\n".join(frame(96)))
        nav.handle("i")

        nav.packet_view["cover-letter.md"] = "The edited letter."
        edited = "\n".join(ui.render_nav_frame(self.spec, nav, 86, 24))
        self.assertIn("The edited letter.", edited)
        self.assertIn("No matching wording notes", edited)
        self.assertNotIn("First reason.", edited)

    def test_keyboard_focus_follows_the_matching_note_and_yanks_only_the_selection(self):
        nav = self.nav()
        nav.expanded_id = "b2"
        clipboard = nav.clipboard = mock.Mock(return_value="clipboard")
        blocks = [{"text": "# Subject", "excerpt": "Subject", "why": "Names the role."},
                  {"text": "I documented downtime in the maintenance log.",
                   "excerpt": "maintenance log", "why": "Echoes the record-keeping requirement."}]
        nav.packet_view = {"meta.json": {"listing_id": "b2"},
                           "cover-letter.md": "\n\n".join(part["text"] for part in blocks),
                           "cover-letter-review.json": blocks}
        nav.handle("l")
        self.assertEqual("notes", nav.packet_pane)
        nav.handle("j")
        frame = "\n".join(ui.render_nav_frame(self.spec, nav, 100, 18))
        self.assertIn("› I documented downtime", frame)
        self.assertIn("╭› 02", frame)
        nav.handle("y")
        clipboard.assert_called_once_with("Cover-letter block:\nI documented downtime in the maintenance log."
                                          "\n\nExcerpt: maintenance log\nWhy this wording: Echoes the record-keeping requirement.")
        self.assertIn("Copied review block", nav.message)
        nav.handle("h")
        self.assertEqual("letter", nav.packet_pane)
        self.assertEqual(1, nav.packet_block)
        nav.handle("Y")
        clipboard.assert_called_with("Subject\n\nI documented downtime in the maintenance log.")
        from review import digest

        nav.handle("e")
        self.assertEqual(["revise", "b2", "2", digest(blocks[1])], nav.packet_terminal_action)
        nav.handle("c")
        self.assertEqual(["cv"], nav.packet_terminal_action)
        nav.choose_generation_model({"id": "b2"}, force=True)
        frame = "\n".join(ui.render_nav_frame(self.spec, nav))
        self.assertIn("Enter run", frame)
        nav.handle("y")
        self.assertEqual("regeneration cancelled", nav.message)
        self.assertEqual(2, clipboard.call_count)
        self.assertEqual([], self.calls)

    def test_local_mac_copy_passes_plain_utf8_to_pbcopy(self):
        with (mock.patch.object(ui.sys, "platform", "darwin"),
              mock.patch.dict(ui.os.environ, {}, clear=True),
              mock.patch.object(ui.shutil, "which", return_value="/usr/bin/pbcopy"),
              mock.patch.object(ui.subprocess, "run") as run):
            self.assertEqual("clipboard", ui.copy_to_clipboard("Grüße\nAbsatz"))
        self.assertEqual(["/usr/bin/pbcopy"], run.call_args.args[0])
        self.assertEqual("Grüße\nAbsatz".encode(), run.call_args.kwargs["input"])
        self.assertTrue(run.call_args.kwargs["check"])

    def test_remote_existing_packet_prompts_instead_of_failing_or_forcing(self):
        nav = self.nav(ready=False, result=_WriteResult("", 1,
                       "FAIL: the generator failed: a packet already exists for b2; pass --force to regenerate it"))
        nav.handle("R")
        nav.handle("1")
        nav.handle("enter")
        self.assertTrue(nav.generation_picker["force"])
        self.assertEqual("fast-free", nav.generation_picker["model"])
        self.assertEqual(1, len(self.calls))
        self.assertNotIn("--force", self.calls[0])
        nav.handle("escape")
        self.assertEqual(1, len(self.calls))

    def test_generation_uses_the_saved_setting_for_a_new_packet_and_regeneration(self):
        with tempfile.TemporaryDirectory() as config:
            nav = ui.Nav(self.spec, make_context(), config_dir=config, persist_settings=True)
            nav.handle(",")
            self.assertIn("Generation Language: German", "\n".join(ui.render_nav_frame(self.spec, nav)))
            nav.handle("2")
            settings = "\n".join(ui.render_nav_frame(self.spec, nav, width=50))
            for choice in ("3 Fast & free", "4 Fastest", "5 Regular", "6 Smart"):
                self.assertIn(choice, settings)
            nav.handle("4")
            self.assertIn("Generation Model: Fastest", "\n".join(ui.render_nav_frame(self.spec, nav)))
            nav.handle("h")
            nav.handle("R")
            self.assertEqual("fastest", nav.generation_picker["model"])
            nav.handle("enter")
            self.assertIn("--language en --generation-model fastest", nav.message)
            reopened = _PacketNav(self.spec, make_context(), config_dir=config, executor=self.executor)
            self.results = _WriteResult("")
            reopened.ready = True
            reopened.handle("R")
            self.assertEqual([], self.calls)
            reopened.handle("y")
            self.assertIn("--language en --generation-model fastest --force", self.calls[0])

    def test_review_exposes_model_settings_without_closing_or_regenerating(self):
        with tempfile.TemporaryDirectory() as config:
            nav = ui.Nav(self.spec, make_context(), config_dir=config,
                         persist_settings=True, executor=self.executor)
            nav.handle("l")
            packet = {"meta.json": {"listing_id": nav.expanded_id},
                      "cover-letter.md": "First paragraph\n\nSecond paragraph"}
            nav.packet_view = packet
            nav.handle("j")
            self.assertEqual(1, nav.packet_block)
            frame = "\n".join(ui.render_nav_frame(self.spec, nav, width=40))
            self.assertIn(", model", ui._unstyled(frame))
            nav.handle(",")
            self.assertTrue(nav.settings_open)
            nav.handle("3")
            nav.handle("escape")
            self.assertFalse(nav.settings_open)
            self.assertIs(nav.packet_view, packet)
            self.assertEqual(1, nav.packet_block)
            self.assertEqual("fast-free", nav.generation_model)
            self.assertEqual([], self.calls)
            nav.handle("R")
            self.assertEqual("fast-free", nav.generation_picker["model"])
            self.assertEqual([], self.calls)
            nav.handle("y")
            self.assertEqual([], self.calls)
            self.assertIs(nav.packet_view, packet)
            nav.handle("R")
            nav.handle("4")
            self.assertEqual([], self.calls)
            nav.handle("enter")
            self.assertIn("--generation-model smart --force", self.calls[0])
            self.assertEqual("fast-free", nav.generation_model)

    def test_the_first_press_writes_the_packet_then_moves_the_stage(self):
        nav = self.nav(ready=False)
        target = nav.acting_row_id()
        nav.handle("R")
        nav.handle("enter")
        self.assertTrue(self.calls[0].startswith("python3 -B tools/dashboard.py generate %s" % target),
                        self.calls[0])
        self.assertNotIn("--force", self.calls[0])
        self.assertIn("--language de", self.calls[0])
        self.assertTrue(any("dashboard.py mark %s generated" % target in call
                            for call in self.calls), self.calls)
        self.assertEqual([target], nav.viewed)

    def test_background_generation_marks_original_listing_without_stealing_focus(self):
        from concurrent.futures import Future

        done = Future()
        submit = mock.Mock(return_value=done)
        nav = _PacketNav(self.spec, make_context(), executor=self.executor,
                         generation_submit=submit)
        target = nav.acting_row_id()
        nav.handle("R")
        nav.handle("enter")
        self.assertIn("Generating…", "\n".join(ui.render_nav_frame(self.spec, nav, 84, 26)))
        nav.handle("R")
        submit.assert_called_once()
        nav.handle("l")
        self.assertIn("Generating…", "\n".join(ui.render_nav_frame(self.spec, nav, 84, 26)))
        nav.handle("h")
        nav.handle("j")
        other = nav.acting_row_id()
        self.assertNotEqual(target, other)
        nav.handle("l")
        self.results = _WriteResult(json.dumps({"uuid": target, "stage": "generated", "status": "new"}))
        done.set_result(_WriteResult(""))
        self.assertTrue(nav.poll_generation())
        self.assertEqual(other, nav.expanded_id)
        self.assertEqual([], nav.viewed)
        self.assertIn("dashboard.py mark %s generated" % target, self.calls[-1])
        rows = {row["id"]: row for row in nav.ctx["rows"]}
        self.assertEqual("generated", rows[target]["stage"])
        self.assertNotEqual("generated", rows[other].get("stage"))
        self.assertNotIn("Generating…", "\n".join(ui.render_nav_frame(self.spec, nav, 84, 26)))

    def test_background_error_clears_spinner_and_never_writes_stage(self):
        from concurrent.futures import Future

        done = Future()
        nav = _PacketNav(self.spec, make_context(), executor=self.executor,
                         generation_submit=lambda *args: done)
        nav.handle("R")
        nav.handle("enter")
        self.assertFalse(nav.poll_generation())
        done.set_exception(TimeoutError("model timeout"))
        self.assertTrue(nav.poll_generation())
        self.assertIn("model timeout", nav.message)
        self.assertEqual([], self.calls)
        self.assertNotIn("Generating…", "\n".join(ui.render_nav_frame(self.spec, nav, 84, 26)))

    def test_generate_shows_all_models_before_running_the_selected_one(self):
        nav = self.nav(ready=False)
        nav.handle("R")
        frame = ui._unstyled("\n".join(ui.render_nav_frame(self.spec, nav, 40, 18)))
        for label in ("Generate cover letter", "Fast & free", "Fastest", "Regular", "Smart"):
            self.assertIn(label, frame)
        self.assertEqual([], self.calls)
        nav.handle("2")
        self.assertEqual([], self.calls)
        nav.handle("enter")
        self.assertIn("--generation-model fastest", self.calls[0])
        self.assertNotIn("--force", self.calls[0])

    def test_cancel_generation_picker_does_not_run_anything(self):
        nav = self.nav(ready=False)
        nav.handle("R")
        nav.handle("1")
        nav.handle("escape")
        self.assertEqual([], self.calls)
        self.assertIsNone(nav.generation_picker)

    def test_a_second_press_asks_before_regenerating(self):
        nav = self.nav(ready=True)
        nav.handle("R")
        self.assertEqual([], self.calls)
        self.assertTrue(nav.generation_picker["force"])
        self.assertIn("Regenerate cover letter", "\n".join(ui.render_nav_frame(self.spec, nav)))

    def test_y_after_the_question_regenerates_with_force(self):
        nav = self.nav(ready=True)
        nav.handle("R")
        nav.handle("y")
        self.assertTrue(self.calls[0].endswith("--force"), self.calls[0])
        self.assertIsNone(nav.generation_picker)

    def test_any_other_key_drops_the_question(self):
        nav = self.nav(ready=True)
        nav.handle("R")
        nav.handle("d")
        self.assertEqual([], self.calls)
        self.assertEqual("regeneration cancelled", nav.message)
        self.assertIsNone(nav.generation_picker)

    def test_a_click_does_not_count_as_a_confirmation(self):
        nav = self.nav(ready=True)
        nav.handle("R")
        nav.handle(("click", 0, 0))
        self.assertIsNone(nav.generation_picker)

    def test_a_failed_generation_leaves_the_stage_alone(self):
        nav = self.nav(ready=False, result=_WriteResult("", returncode=1, stderr="no advert text\n"))
        nav.handle("R")
        nav.handle("enter")
        self.assertIn("Generate failed", nav.message)
        self.assertIn("Use R to choose a model and retry", nav.message)
        self.assertFalse(any("dashboard.py mark" in call for call in self.calls), self.calls)
        self.assertEqual([], nav.viewed)

    def test_v_says_how_to_write_one_when_there_is_no_packet(self):
        # The real review-window code, on a listing that has no packet folder.
        ctx = make_context()
        ctx["rows"][0]["id"] = "00000000-0000-4000-8000-000000000000"
        nav = ui.Nav(self.spec, ctx, executor=lambda cmd: _WriteResult("", 4))
        nav.handle("V")
        self.assertIn("press R", nav.message)

    def test_v_opens_the_packet_the_generator_left_behind(self):
        nav = self.nav(ready=True)
        target = nav.acting_row_id()
        nav.handle("V")
        self.assertEqual([target], nav.viewed)


class NotePromptTest(unittest.TestCase):
    """N asks for the words; the note is written only on enter."""

    @classmethod
    def setUpClass(cls):
        cls.spec = ui.load()

    def setUp(self):
        ui.set_theme("catppuccin-mocha")
        self.calls = []

    def nav(self):
        return ui.Nav(self.spec, make_context(), executor=self.executor)

    def executor(self, command, **kwargs):
        self.calls.append(command)
        return _WriteResult(_tracker_echo(note="they asked for a portfolio"))

    def test_n_opens_a_prompt_instead_of_writing(self):
        nav = self.nav()
        nav.handle("N")
        self.assertEqual("note", nav.prompt)
        self.assertEqual([], self.calls)

    def test_typing_and_enter_writes_the_note(self):
        nav = self.nav()
        target = nav.acting_row_id()
        nav.handle("N")
        for char in "call back":
            nav.handle(char)
        nav.handle("enter")
        self.assertIsNone(nav.prompt)
        self.assertTrue(self.calls[0].startswith("python3 -B tools/dashboard.py note %s" % target),
                        self.calls[0])
        self.assertIn("call back", self.calls[0])

    def test_the_note_is_one_quoted_argument(self):
        nav = self.nav()
        nav.handle("N")
        for char in "it's fine":
            nav.handle(char)
        nav.handle("enter")
        self.assertIn(shlex.quote("it's fine"), self.calls[0])

    def test_escape_writes_nothing(self):
        nav = self.nav()
        nav.handle("N")
        nav.handle("x")
        nav.handle("escape")
        self.assertIsNone(nav.prompt)
        self.assertEqual([], self.calls)
        self.assertEqual("note cancelled", nav.message)

    def test_enter_on_an_empty_note_writes_nothing(self):
        nav = self.nav()
        nav.handle("N")
        nav.handle("enter")
        self.assertEqual([], self.calls)
        self.assertEqual("note cancelled", nav.message)


if __name__ == "__main__":
    unittest.main()
