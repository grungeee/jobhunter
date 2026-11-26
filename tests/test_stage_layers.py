"""One stage vocabulary, three layers, no drift.

The writer validates stage keys, the feed decides the coarse status and the
order, the spec paints labels and the gauge, and two more places accept the key
as a command (dashboard.py's allow list and the browser's mark parser). A key
that exists in only some of them is a stage the UI can show but never write, or
write but never show.
"""
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (os.path.join(ROOT, "tools"), os.path.join(ROOT, "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

import dashboard  # noqa: E402
import feed  # noqa: E402
import ui  # noqa: E402
from test_writer_stages import load_writer_tables  # noqa: E402


def spec_stages():
    spec = ui.load()
    return (spec.get("vocabulary") or {}).get("stages") or []


def browser_mark_parser():
    """The JS regex that decides whether a pill may write, read from the source."""
    with open(os.path.join(ROOT, "tools", "ui.py"), "r", encoding="utf-8") as handle:
        source = handle.read()
    match = re.search(r"dashboard\\\\\.py mark \(\[0-9a-f\]\{8\}.*?\) \(([a-z|]+)\)\$", source)
    if not match:
        raise AssertionError("no mark parser found in tools/ui.py")
    return match.group(1).split("|")


class StageLayersTest(unittest.TestCase):
    def test_the_spec_and_the_feed_agree_on_every_key(self):
        spec_keys = [entry["key"] for entry in spec_stages()]
        self.assertEqual(sorted(feed.STAGE_ORDER), sorted(spec_keys))
        for entry in spec_stages():
            key = entry["key"]
            self.assertEqual(entry["label"], feed.STAGE_LABEL[key], key)
            self.assertIn(key, feed.STAGE_STATUS, key)

    def test_the_writer_accepts_exactly_the_keys_the_feed_projects(self):
        stages, statuses = load_writer_tables()
        self.assertEqual(feed.STAGE_ORDER, stages)
        for key in stages:
            self.assertEqual(feed.STAGE_STATUS[key], statuses[key], key)

    def test_dashboard_allows_every_stage_to_be_marked(self):
        for key in feed.STAGE_ORDER:
            self.assertIn(key, dashboard.MARKABLE_ALL, key)

    def test_the_browser_parser_accepts_every_stage(self):
        allowed = browser_mark_parser()
        for key in feed.STAGE_ORDER:
            self.assertIn(key, allowed, key)


if __name__ == "__main__":
    unittest.main()
