"""The writer accepts exactly the pipeline the menu paints.

Every stage in ``STAGES`` has to be writable and has to imply a coarse status,
otherwise a stage can be shown but never reached, or reached and never counted.
The test drives the real script against a throwaway store, so it proves the
deployed behaviour and not a copy of the table.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRITER = os.path.join(ROOT, "ams", "ams_nudge.py")
JOB = "11111111-2222-3333-4444-555555555555"


def load_writer_tables():
    """Read STAGES and STAGE_STATUS out of the script without importing it."""
    namespace = {}
    with open(WRITER, "r", encoding="utf-8") as handle:
        source = handle.read()
    start = source.index("STAGES = [")
    end = source.index("STATUS_STAGE = {")
    exec(compile(source[start:end], WRITER, "exec"), namespace)  # noqa: S102
    return namespace["STAGES"], namespace["STAGE_STATUS"]


class StageVocabularyTest(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="writer-stages-")
        self.addCleanup(shutil.rmtree, self.base, True)
        with open(os.path.join(self.base, "applications.json"), "w", encoding="utf-8") as handle:
            json.dump({"jobs": {JOB: {"title": "Tracked role", "status": "applied",
                                      "stage": "sent"}}}, handle)

    def run_writer(self, *args):
        env = dict(os.environ, AMS_NUDGE_BASE=self.base)
        return subprocess.run([sys.executable, "-B", WRITER] + list(args),
                              capture_output=True, text=True, env=env, timeout=60)

    def store(self):
        with open(os.path.join(self.base, "applications.json"), "r", encoding="utf-8") as handle:
            return json.load(handle)

    def test_every_declared_stage_can_be_written(self):
        stages, expected = load_writer_tables()
        for stage in stages:
            result = self.run_writer("stage", JOB, stage)
            self.assertEqual(0, result.returncode, "%s: %s" % (stage, result.stdout + result.stderr))
            echo = json.loads(result.stdout[result.stdout.index("{"):])
            self.assertEqual(stage, echo["stage"])
            self.assertEqual(expected[stage], echo["status"])
            self.assertEqual(stage, self.store()["jobs"][JOB]["stage"])

    def test_interview_preparation_and_waiting_for_outcome_are_interview(self):
        for stage in ("preparation", "awaiting"):
            result = self.run_writer("stage", JOB, stage)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("interview", self.store()["jobs"][JOB]["status"])

    def test_an_unknown_stage_is_rejected_without_writing(self):
        before = self.store()
        result = self.run_writer("stage", JOB, "controlled")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("invalid stage", result.stdout)
        self.assertEqual(before, self.store())

    def test_a_note_is_stored_with_the_outcome(self):
        result = self.run_writer("stage", JOB, "rejected", "no", "answer", "after", "3", "weeks")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("no answer after 3 weeks", self.store()["jobs"][JOB]["note"])

    def test_a_note_is_stored_without_moving_the_stage(self):
        self.run_writer("stage", JOB, "interview")
        before = self.store()["jobs"][JOB]["stage_at"]
        result = self.run_writer("note", JOB, "second round", "scheduled")
        self.assertEqual(0, result.returncode, result.stderr)
        record = self.store()["jobs"][JOB]
        self.assertEqual("second round scheduled", record["note"])
        self.assertEqual("interview", record["stage"])
        self.assertEqual(before, record["stage_at"])
        self.assertTrue(record.get("note_at"))

    def test_a_second_block_is_undone_by_the_next_press(self):
        """stage blocked, then unstage, must land back on the stage before it."""
        self.run_writer("stage", JOB, "interview")
        result = self.run_writer("stage", JOB, "blocked")
        self.assertEqual(0, result.returncode)
        result = self.run_writer("unstage", JOB)
        self.assertEqual(0, result.returncode)
        record = self.store()["jobs"][JOB]
        self.assertEqual("interview", record["stage"])
        self.assertEqual("interview", record["status"])
        echo = json.loads(result.stdout[result.stdout.index("{"):])
        self.assertEqual("blocked", echo.get("undone"))

    def test_an_undo_with_nothing_behind_it_says_so(self):
        result = self.run_writer("unstage", JOB)
        self.assertEqual(1, result.returncode)
        self.assertIn("no earlier stage recorded", result.stdout + result.stderr)

    def test_an_empty_note_is_refused(self):
        result = self.run_writer("note", JOB)
        self.assertEqual(2, result.returncode)
        self.assertIn("usage", result.stdout)


if __name__ == "__main__":
    unittest.main()
