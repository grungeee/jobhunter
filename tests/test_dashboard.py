"""Tests for tools/dashboard.py. Standard library unittest only."""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock
import urllib.error
import urllib.request

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
sys.path.insert(0, TOOLS)

import dashboard  # noqa: E402
import feed as feedmod  # noqa: E402


def write_json(path, payload):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)


SAMPLE_STATE = {
    "updated_at": "2026-11-10T07:43:54+01:00",
    "seen": {
        "job-1": {"title": "Example Technician", "company": "Alpha", "ort": "Sampletown",
                  "score": 9.1, "first_seen": "2026-11-01", "last_seen": "2026-11-10"},
        "job-2": {"title": "Service Associate", "company": "Beta", "ort": "Rivertown",
                  "score": 7.4, "first_seen": "2026-10-30", "last_seen": "2026-11-09"},
    },
}
SAMPLE_RAW = {
    "scraped_at": "2026-11-10T06:43:54.444Z", "period": "LAST_WEEK",
    "location": "Sampletown (20 km)", "keywords": 32, "unique_jobs": 2,
    "jobs": [
        {"uuid": "job-1", "title": "Example Technician", "company": "Alpha", "ort": "Sampletown",
         "url": "https://example.invalid/1", "datum": "05.11.2026"},
        {"uuid": "job-2", "title": "Service Associate", "company": "Beta", "ort": "Rivertown",
         "url": "https://example.invalid/2", "datum": "04.11.2026"},
    ],
}
SAMPLE_SCORED = [{"uuid": "job-1", "score": 9.1, "matched_roles": ["Service"]}]
SAMPLE_APPS = {"updated_at": "2026-11-10T17:00:11+01:00",
               "jobs": {"job-2": {"status": "applied", "sent_at": "2026-11-09T09:00:00+01:00"}}}


class DashSandbox(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="dash-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.source = os.path.join(self.base, "ams")
        os.makedirs(self.source)
        write_json(os.path.join(self.source, "state.json"), SAMPLE_STATE)
        write_json(os.path.join(self.source, "raw_jobs.json"), SAMPLE_RAW)
        write_json(os.path.join(self.source, "scored_jobs.json"), SAMPLE_SCORED)
        write_json(os.path.join(self.source, "applications.json"), SAMPLE_APPS)
        self.feed = feedmod.build_feed(self.source)


class MarkTests(DashSandbox):
    def test_mark_forwards_to_runner(self):
        calls = []
        dashboard.mark("job-1", "applied", "note here",
                       runner=lambda *args: calls.append(args) or "done")
        self.assertEqual(calls, [("job-1", "applied", "note here")])

    def test_mark_rejects_unknown_status(self):
        with self.assertRaises(dashboard.DashboardError):
            dashboard.mark("job-1", "hired", runner=lambda *a: "no")

    def test_mark_rejects_empty_id(self):
        with self.assertRaises(dashboard.DashboardError):
            dashboard.mark("   ", "applied", runner=lambda *a: "no")

    def test_mark_is_case_insensitive_on_status(self):
        calls = []
        dashboard.mark("job-1", "APPLIED", runner=lambda *a: calls.append(a) or "ok")
        self.assertEqual(calls[0][1], "applied")

    def test_all_markable_statuses_are_accepted(self):
        for status in dashboard.MARKABLE:
            dashboard.mark("job-1", status, runner=lambda *a: "ok")

    def test_mark_requires_tracker_script(self):
        saved = dashboard.TRACKER_SCRIPT
        dashboard.TRACKER_SCRIPT = os.path.join(self.base, "absent.py")
        self.addCleanup(setattr, dashboard, "TRACKER_SCRIPT", saved)
        with self.assertRaises(dashboard.DashboardError) as caught:
            dashboard.mark("job-1", "applied")
        self.assertIn("tracker script not found", str(caught.exception))

    def test_runner_errors_are_wrapped(self):
        def boom(*_args):
            raise RuntimeError("pipeline down")
        with self.assertRaises(RuntimeError):
            dashboard.mark("job-1", "applied", runner=boom)


class RenderTests(DashSandbox):
    def test_html_contains_embedded_jobs(self):
        html_text = dashboard.render_html(self.feed)
        self.assertIn("job-1", html_text)
        self.assertIn("Example Technician", html_text)

    def test_html_has_filter_and_mark_controls(self):
        html_text = dashboard.render_html(self.feed)
        self.assertIn('data-filter="applied"', html_text)
        self.assertIn("data-status=", html_text)

    def test_html_reports_counts(self):
        html_text = dashboard.render_html(self.feed)
        self.assertIn('id="c-total"', html_text)
        self.assertIn('id="c-applied"', html_text)

    def test_html_has_no_dash_characters(self):
        html_text = dashboard.render_html(self.feed)
        self.assertNotIn("\u2014", html_text)
        self.assertNotIn("\u2013", html_text)

    def test_html_escapes_hostile_text(self):
        self.feed["jobs"][0]["title"] = "<script>alert(1)</script>"
        html_text = dashboard.render_html(self.feed)
        self.assertNotIn("<script>alert(1)</script>", html_text)
        self.assertIn("\\u003cscript\\u003ealert(1)", html_text)

    def test_inline_json_cannot_close_the_script_tag(self):
        self.feed["jobs"][0]["company"] = "</script><img src=x>"
        html_text = dashboard.render_html(self.feed)
        self.assertEqual(html_text.count("</script>"), 1)
        self.assertNotIn("<img src=x>", html_text)

    def test_html_warns_about_degraded_sources(self):
        self.feed["warnings"] = ["source scored unavailable (missing)"]
        html_text = dashboard.render_html(self.feed)
        self.assertIn("warning:", html_text)

    def test_served_mode_note_differs(self):
        self.assertIn("Served mode", dashboard.render_html(self.feed, mode="served"))
        self.assertIn("Static build", dashboard.render_html(self.feed, mode="static"))

    def test_payload_shape(self):
        data = dashboard.payload(self.feed)
        self.assertEqual(data["counts"]["total"], 2)
        self.assertEqual(data["jobs"][0]["id"], "job-2")
        self.assertIn("scan", data["scanLine"])

    def test_build_writes_dashboard_file(self):
        target = os.path.join(self.base, "DASHBOARD.html")
        _feed, path = dashboard.build(self.source, target)
        self.assertEqual(path, target)
        with open(target, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("Job listings dashboard", text)

    def test_build_leaves_no_pending_file(self):
        target = os.path.join(self.base, "DASHBOARD.html")
        dashboard.build(self.source, target)
        self.assertEqual([name for name in os.listdir(self.base)
                          if ".pending-" in name], [])

    def test_build_command_reports_counts(self):
        target = os.path.join(self.base, "DASHBOARD.html")
        self.assertEqual(dashboard.main(["build", "--source", self.source,
                                         "--out", target]), 0)


class ServeTests(DashSandbox):
    def setUp(self):
        super().setUp()
        self.calls = []

        def fake_runner(job_id, status, note=""):
            self.calls.append((job_id, status, note))
            return "recorded"

        self.httpd = dashboard.serve(self.source, "127.0.0.1", 0, runner=fake_runner)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def get(self, path):
        with urllib.request.urlopen(self.url(path), timeout=10) as response:
            return response.status, response.read().decode("utf-8")

    def post(self, path, payload):
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(self.url(path), data=data, method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_root_serves_html(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("Job listings dashboard", body)

    def test_local_live_view_serves_the_structured_job_list(self):
        status, body = self.get("/gui.html")
        self.assertEqual(status, 200)
        self.assertIn('id="job-list-view"', body)
        self.assertIn('id="profile-skills"', body)
        self.assertIn('data-action="stage_next"', body)
        self.assertIn('data-action="stage_skipped"', body)

    def test_cache_busting_query_still_serves_the_live_view(self):
        status, body = self.get("/gui.html?v=20261113")
        self.assertEqual(status, 200)
        self.assertIn('id="job-list-view"', body)

    def test_mark_rejects_cross_origin_browser_requests(self):
        data = json.dumps({"id": "job-1", "status": "applied"}).encode()
        request = urllib.request.Request(self.url("/api/mark"), data=data, method="POST",
                                         headers={"Content-Type": "application/json",
                                                  "Origin": "https://attacker.example"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(caught.exception.code, 403)
        self.assertEqual(self.calls, [])

    def test_bookmark_endpoint_flips_the_flag_through_the_tracker(self):
        path = os.path.join(self.source, "applications.json")

        def run(verb, job_id):
            with open(path, encoding="utf-8") as handle:
                apps = json.load(handle)
            job = apps["jobs"].setdefault(job_id, {})
            job["bookmarked"] = not job.get("bookmarked")
            write_json(path, apps)
            return SimpleNamespace(returncode=0, stdout='{"ok": true}', stderr="")

        self.httpd.RequestHandlerClass.bookmark_tracker = staticmethod(run)
        status, body = self.post("/api/bookmark", {"id": "job-1"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["bookmarked"])
        status, body = self.post("/api/bookmark", {"id": "job-1"})
        self.assertFalse(body["bookmarked"])

    def test_note_endpoint_stores_through_the_writer(self):
        seen = []

        def fake_note(job_id, text):
            seen.append((job_id, text))
            return json.dumps({"ok": True, "uuid": job_id, "note": text})

        patcher = mock.patch.object(dashboard, "note", fake_note)
        patcher.start()
        self.addCleanup(patcher.stop)
        status, body = self.post("/api/note", {"id": "job-1", "note": "they said next week"})
        self.assertEqual(status, 200)
        self.assertEqual([("job-1", "they said next week")], seen)
        self.assertEqual("they said next week", body["note"])

    def test_note_endpoint_refuses_an_empty_note(self):
        status, body = self.post("/api/note", {"id": "job-1", "note": "  "})
        self.assertEqual(status, 400)
        self.assertIn("empty", body["error"])

    def test_unstage_endpoint_reports_the_restored_stage(self):
        seen = []

        def fake_unstage(job_id):
            seen.append(job_id)
            return json.dumps({"ok": True, "uuid": job_id, "stage": "sent",
                               "status": "applied", "undone": "blocked"})

        patcher = mock.patch.object(dashboard, "unstage", fake_unstage)
        patcher.start()
        self.addCleanup(patcher.stop)
        status, body = self.post("/api/unstage", {"id": "job-1"})
        self.assertEqual(status, 200)
        self.assertEqual(["job-1"], seen)
        self.assertEqual("sent", body["stage"])

    def test_unstage_endpoint_says_why_it_could_not_undo(self):
        def fake_unstage(job_id):
            raise dashboard.DashboardError("no earlier stage recorded for %s" % job_id)

        patcher = mock.patch.object(dashboard, "unstage", fake_unstage)
        patcher.start()
        self.addCleanup(patcher.stop)
        status, body = self.post("/api/unstage", {"id": "job-1"})
        self.assertEqual(status, 400)
        self.assertIn("no earlier stage", body["error"])

    def test_generate_endpoint_asks_before_overwriting(self):
        def fake_generate(job_id, force=False):
            if not force:
                raise dashboard.DashboardError(
                    "a packet already exists for %s; pass --force to regenerate it" % job_id)
            return '{"ok": true}'

        patcher = mock.patch.object(dashboard, "generate", fake_generate)
        patcher.start()
        self.addCleanup(patcher.stop)
        status, body = self.post("/api/generate", {"id": "job-1"})
        self.assertEqual(409, status)
        self.assertTrue(body["needs_confirm"])
        status, body = self.post("/api/generate", {"id": "job-1", "force": True})
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        self.assertTrue(body["packet_written"])
        self.assertFalse(body["stage_updated"])

    def test_generate_endpoint_records_and_reads_back_generated_stage(self):
        path = os.path.join(self.source, "applications.json")

        def write_stage(job_id, stage, note=""):
            self.calls.append((job_id, stage, note))
            with open(path, encoding="utf-8") as handle:
                apps = json.load(handle)
            apps["jobs"][job_id] = {"stage": stage,
                                    "status": feedmod.STAGE_STATUS[stage],
                                    "sent_at": ""}
            write_json(path, apps)
            return "recorded"

        self.httpd.RequestHandlerClass.runner = staticmethod(write_stage)
        patcher = mock.patch.object(dashboard, "generate", lambda job_id, force=False: '{"ok": true}')
        patcher.start()
        self.addCleanup(patcher.stop)

        status, body = self.post("/api/generate", {"id": "job-1"})
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        self.assertTrue(body["stage_updated"])
        self.assertEqual("generated", body["stored_stage"])
        self.assertEqual([("job-1", "generated", "")], self.calls)
        row = next(job for job in feedmod.build_feed(self.source)["jobs"]
                   if job["id"] == "job-1")
        self.assertEqual("generated", row["stage"])

    def test_packet_endpoint_returns_the_review_text(self):
        packet = {"meta.json": {"model": "deepseek-v4-flash", "generated_at": "2026-11-16T22:33:00"},
                  "cover-letter.md": "Dear hiring team,\n\nYour advert asks for CMMS records.",
                  "documents.json": [{"name": "cv.pdf", "note": "the CV release"}],
                  "rationale.md": "# Why this letter\n\nTwo duties match."}
        patcher = mock.patch.object(dashboard.generatemod, "read_packet",
                                    lambda root, listing_id: packet)
        patcher.start()
        self.addCleanup(patcher.stop)
        status, raw = self.get("/api/packet?id=job-1")
        self.assertEqual(200, status)
        text = json.loads(raw)["text"]
        self.assertIn("COVER LETTER", text)
        self.assertIn("cv.pdf - the CV release", text)
        self.assertIn("deepseek-v4-flash", text)

    def test_packet_endpoint_is_404_without_a_packet(self):
        patcher = mock.patch.object(dashboard.generatemod, "read_packet",
                                    lambda root, listing_id: {})
        patcher.start()
        self.addCleanup(patcher.stop)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/api/packet?id=job-1")
        self.assertEqual(404, caught.exception.code)

    def test_feed_endpoint_returns_json(self):
        status, body = self.get("/api/feed")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["counts"]["total"], 2)

    def test_unknown_path_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/nope")
        self.assertEqual(caught.exception.code, 404)

    def test_mark_endpoint_persists(self):
        status, body = self.post("/api/mark", {"id": "job-1", "status": "applied"})
        self.assertEqual(status, 200)
        self.assertEqual(self.calls, [("job-1", "applied", "")])
        # This stub runner records the call but stores nothing, so the endpoint
        # must not tell the browser the write landed.
        self.assertFalse(body["ok"])

    def test_mark_endpoint_reads_back_sandbox_state(self):
        path = os.path.join(self.source, "applications.json")

        def write_sandbox(job_id, status, note=""):
            with open(path, encoding="utf-8") as handle:
                apps = json.load(handle)
            apps["jobs"][job_id] = {"status": status, "sent_at": ""}
            write_json(path, apps)
            return "recorded"

        self.httpd.RequestHandlerClass.runner = staticmethod(write_sandbox)
        status, body = self.post("/api/mark", {"id": "job-1", "status": "applied"})
        self.assertEqual(status, 200)
        self.assertEqual(body["id"], "job-1")
        self.assertEqual(body["stored_status"], "applied")
        self.assertEqual(feedmod.build_feed(self.source)["jobs"][0]["status"], "applied")

    def test_mark_endpoint_confirms_a_stage_not_the_coarse_status_behind_it(self):
        path = os.path.join(self.source, "applications.json")

        def write_sandbox(job_id, status, note=""):
            with open(path, encoding="utf-8") as handle:
                apps = json.load(handle)
            apps["jobs"][job_id] = {"stage": status, "status": "applied", "sent_at": ""}
            write_json(path, apps)
            return "recorded"

        self.httpd.RequestHandlerClass.runner = staticmethod(write_sandbox)
        status, body = self.post("/api/mark", {"id": "job-1", "status": "sent"})
        self.assertEqual(status, 200)
        # The button asked for a stage; the browser has to hear that stage back,
        # not the coarse status word the tracker derived from it.
        self.assertTrue(body["ok"])
        self.assertEqual("sent", body["stored_stage"])
        self.assertEqual("applied", body["stored_status"])

    def test_mark_endpoint_fails_closed_when_the_writer_drops_the_stage(self):
        status, body = self.post("/api/mark", {"id": "job-1", "status": "waiting"})
        self.assertEqual(status, 200)
        self.assertEqual("unread", body["stored_stage"])
        self.assertFalse(body["ok"])

    def test_mark_endpoint_returns_fresh_status(self):
        _status, body = self.post("/api/mark", {"id": "job-1", "status": "applied"})
        self.assertIn("status", body)
        self.assertIn("tracked_date", body)

    def test_mark_endpoint_echoes_the_requested_status(self):
        _status, body = self.post("/api/mark", {"id": "job-1", "status": "applied"})
        self.assertEqual(body["status"], "applied")
        self.assertEqual(body["previous_status"], "new")

    def test_mark_endpoint_rejects_bad_status(self):
        status, body = self.post("/api/mark", {"id": "job-1", "status": "hired"})
        self.assertEqual(status, 400)
        self.assertIn("neither a status nor a stage", body["error"])

    def test_mark_endpoint_rejects_bad_json(self):
        data = b"{not json"
        request = urllib.request.Request(self.url("/api/mark"), data=data, method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        self.assertEqual(status, 400)

    def test_post_to_unknown_path_is_404(self):
        status, _body = self.post("/api/other", {})
        self.assertEqual(status, 404)


class CommandTests(DashSandbox):
    def test_unknown_command_returns_two(self):
        self.assertEqual(dashboard.main(["frobnicate"]), 2)

    def test_help_returns_zero(self):
        self.assertEqual(dashboard.main(["--help"]), 0)

    def test_mark_command_uses_tracker_override(self):
        script = os.path.join(self.base, "fake_tracker.py")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write("import sys\nprint('fake ok', sys.argv[1:])\n")
        self.assertEqual(dashboard.main(["mark", "job-1", "applied", "--tracker", script]), 0)

    def test_mark_command_rejects_unknown_status(self):
        self.assertEqual(dashboard.main(["mark", "job-1", "hired"]), 1)


class RemoteWriterTests(DashSandbox):
    """The Mac path: no local tracker, so writes go through the launcher."""

    def setUp(self):
        super().setUp()
        saved = dashboard.TRACKER_SCRIPT
        dashboard.TRACKER_SCRIPT = os.path.join(self.base, "absent.py")
        self.addCleanup(setattr, dashboard, "TRACKER_SCRIPT", saved)
        saved_env = os.environ.get("JOBUI_REMOTE_WRITES")
        os.environ["JOBUI_REMOTE_WRITES"] = "1"

        def restore_env():
            if saved_env is None:
                os.environ.pop("JOBUI_REMOTE_WRITES", None)
            else:
                os.environ["JOBUI_REMOTE_WRITES"] = saved_env
        self.addCleanup(restore_env)
        self.calls = []

    def stub(self, *replies):
        """Replace subprocess.run for the launcher hop only."""
        replies = list(replies) or [SimpleNamespace(returncode=0, stdout="ok\n", stderr="")]

        def fake_run(command, **kwargs):
            self.calls.append(list(command))
            return replies.pop(0) if len(replies) > 1 else replies[0]

        patcher = mock.patch.object(dashboard.subprocess, "run", fake_run)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_stage_status_uses_the_stage_verb(self):
        self.stub()
        dashboard.default_runner("job-1", "sent")
        command = self.calls[0]
        self.assertEqual(os.path.basename(command[0]), "jobhunter")
        self.assertEqual(command[1:4], ["jobhunter-remote", "stage", "job-1"])
        self.assertEqual(command[4], "sent")

    def test_coarse_status_uses_the_set_verb(self):
        self.stub()
        dashboard.default_runner("job-1", "applied", "note")
        self.assertEqual(self.calls[0][1:4], ["jobhunter-remote", "set", "job-1"])
        self.assertEqual(self.calls[0][4:], ["applied", "note"])

    def test_unknown_job_is_tracked_then_retried(self):
        self.stub(SimpleNamespace(returncode=1, stdout="unknown job job-9", stderr=""),
                  SimpleNamespace(returncode=0, stdout="ok\n", stderr=""))
        self.assertEqual(dashboard.default_runner("job-9", "sent").strip(), "ok")
        self.assertEqual(self.calls[1][1:4], ["jobhunter-remote", "track", "job-9"])
        self.assertEqual(self.calls[2][1:4], ["jobhunter-remote", "stage", "job-9"])

    def test_rejection_is_wrapped(self):
        self.stub(SimpleNamespace(returncode=2, stdout="", stderr="boom\n"))
        with self.assertRaises(dashboard.DashboardError) as caught:
            dashboard.default_runner("job-1", "sent")
        self.assertIn("remote tracker rejected the update: boom",
                      str(caught.exception))

    def test_bookmark_goes_through_the_launcher(self):
        self.stub()
        dashboard.bookmark("job-2")
        self.assertEqual(self.calls[0][1:4], ["jobhunter-remote", "bookmark", "job-2"])

    def test_generate_hops_to_the_server_when_there_is_no_engine_here(self):
        self.stub(SimpleNamespace(returncode=0,
                                  stdout='{"ok": true, "listing_id": "a1"}\n', stderr=""))
        patcher = mock.patch.object(dashboard.shutil, "which", lambda name: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        output = dashboard.generate("a1", force=True, language="en", generation_model="smart")
        self.assertEqual(self.calls[0][1:], ["jobhunter-remote", "generate", "a1", "--language", "en",
                                            "--generation-model", "smart", "--force"])
        self.assertIn("ok", output)

    def test_packet_json_reads_the_server_before_syncthing(self):
        listing_id = "00000000-0000-4000-8000-000000000001"
        packet = {"meta.json": {"listing_id": listing_id}, "cover-letter.md": "Fixture"}
        self.stub(SimpleNamespace(returncode=0, stdout=json.dumps(packet), stderr=""))
        with mock.patch.object(dashboard.shutil, "which", return_value=None), \
                mock.patch.object(dashboard.sys, "stdout") as stdout:
            self.assertEqual(0, dashboard.cmd_packet([listing_id, "--json"]))
        self.assertEqual(self.calls[0][1:], ["jobhunter-remote", "packet", listing_id])
        self.assertEqual(packet, json.loads(stdout.write.call_args.args[0]))

    def test_packet_json_refuses_paths_before_starting_ssh(self):
        self.stub()
        with self.assertRaises(dashboard.DashboardError):
            dashboard.cmd_packet(["../outside", "--json"])
        self.assertEqual([], self.calls)

    def test_generate_fails_closed_without_the_opt_in(self):
        os.environ.pop("JOBUI_REMOTE_WRITES", None)
        patcher = mock.patch.object(dashboard.shutil, "which", lambda name: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        with self.assertRaises(dashboard.DashboardError):
            dashboard.generate("a1")

    def test_a_rejected_generation_carries_the_first_line(self):
        self.stub(SimpleNamespace(returncode=1, stdout="",
                                  stderr="listing a1 has 12 characters of advert text\n"))
        patcher = mock.patch.object(dashboard.shutil, "which", lambda name: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        with self.assertRaises(dashboard.DashboardError) as caught:
            dashboard.generate("a1")
        self.assertIn("characters of advert text", str(caught.exception))

    def test_generate_runs_locally_when_the_engine_is_here(self):
        self.stub()
        patcher = mock.patch.object(dashboard.shutil, "which", lambda name: "/usr/bin/" + name)
        patcher.start()
        self.addCleanup(patcher.stop)
        dashboard.generate("a1")
        command = self.calls[0]
        self.assertTrue(any(part.endswith("tools/generate.py") for part in command), command)
        self.assertEqual(command[-3:], ["a1", "--language", "de"])

    def test_state_read_goes_through_the_launcher(self):
        self.stub(SimpleNamespace(returncode=0,
                                  stdout=json.dumps({"jobs": {"a1": {"stage": "sent"}}}),
                                  stderr=""))
        jobs = dashboard.remote_state()
        self.assertEqual(self.calls[0][1:3], ["jobhunter-remote", "state"])
        self.assertEqual("sent", jobs["a1"]["stage"])

    def test_state_read_fails_closed_without_the_opt_in(self):
        os.environ.pop("JOBUI_REMOTE_WRITES", None)
        self.stub()
        with self.assertRaises(dashboard.DashboardError):
            dashboard.remote_state()
        self.assertEqual([], self.calls)

    def test_state_read_wraps_a_rejection(self):
        self.stub(SimpleNamespace(returncode=2, stdout="", stderr="no such file\n"))
        with self.assertRaises(dashboard.DashboardError) as caught:
            dashboard.remote_state()
        self.assertIn("state read", str(caught.exception))

    def test_without_the_opt_in_it_still_fails_closed(self):
        os.environ.pop("JOBUI_REMOTE_WRITES", None)
        self.stub()
        with self.assertRaises(dashboard.DashboardError) as caught:
            dashboard.default_runner("job-1", "sent")
        self.assertIn("tracker script not found", str(caught.exception))
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
