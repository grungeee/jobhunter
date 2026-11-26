import os
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import jobhunter


class DemoScanTests(unittest.TestCase):
    def test_each_selected_portal_returns_three_newest_profile_matches(self):
        now = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)
        result = jobhunter.run_demo_scan(["ams", "karriere-at", "jobs-at"], "operations-demo", now)
        self.assertEqual(3, result["limit_per_portal"])
        self.assertEqual(3, len(result["results"]))
        for portal_result in result["results"]:
            self.assertEqual("synthetic-fixture", portal_result["mode"])
            self.assertFalse(portal_result["live_available"])
            self.assertEqual(3, len(portal_result["jobs"]))
            posted = [job["posted"] for job in portal_result["jobs"]]
            self.assertEqual(posted, sorted(posted, reverse=True))
            self.assertTrue(all(job["synthetic"] for job in portal_result["jobs"]))
            self.assertTrue(all(job["category"] == "operations" for job in portal_result["jobs"]))

    def test_profile_selection_changes_the_matched_jobs(self):
        ops = jobhunter.run_demo_scan(["ams"], "operations-demo")
        analytics = jobhunter.run_demo_scan(["ams"], "analytics-demo")
        self.assertTrue(all(row["category"] == "operations" for row in ops["results"][0]["jobs"]))
        self.assertTrue(all(row["category"] == "analytics" for row in analytics["results"][0]["jobs"]))
        self.assertNotEqual(ops["results"][0]["jobs"][0]["id"], analytics["results"][0]["jobs"][0]["id"])

    def test_invalid_portal_and_profile_are_rejected(self):
        with self.assertRaises(ValueError):
            jobhunter.run_demo_scan(["unknown"], "operations-demo")
        with self.assertRaises(ValueError):
            jobhunter.run_demo_scan(["ams"], "private-profile")

    def test_generated_fixture_is_parsed_as_html(self):
        now = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)
        rows = jobhunter.parse_fixture(jobhunter._fixture_html("ams", now))
        self.assertEqual(6, len(rows))
        self.assertEqual("Operations Coordinator", rows[0]["title"])
        self.assertEqual("ams-sample-1", rows[0]["id"])
        self.assertTrue(rows[0]["posted"].endswith("Z"))


class DemoAPIAuthTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "JOBHUNTER_PASSCODE": "test-only-passcode",
            "JOBHUNTER_SESSION_SECRET": "test-secret-with-more-than-thirty-two-characters",
        })
        self.env.start()
        self.app = FastAPI()
        self.app.include_router(jobhunter.router)
        self.client = TestClient(self.app, base_url="https://testserver")

    def tearDown(self):
        self.client.close()
        self.env.stop()

    def test_private_api_rejects_requests_without_session(self):
        response = self.client.get("/api/v1/jobhunter/profiles")
        self.assertEqual(401, response.status_code)

    def test_login_cookie_unlocks_sample_profile_api(self):
        response = self.client.post("/api/v1/jobhunter/login", json={"passcode": "test-only-passcode"})
        self.assertEqual(200, response.status_code)
        self.assertNotIn("test-only-passcode", response.text)
        cookie = response.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertNotIn("test-only-passcode", cookie)
        profiles = self.client.get("/api/v1/jobhunter/profiles")
        self.assertEqual(200, profiles.status_code)
        self.assertEqual(2, len(profiles.json()["profiles"]))

    def test_wrong_passcode_does_not_create_session(self):
        response = self.client.post("/api/v1/jobhunter/login", json={"passcode": "wrong"})
        self.assertEqual(401, response.status_code)
        self.assertNotIn("jobhunter_session", self.client.cookies)

    def test_scan_requires_login_and_accepts_profile_and_portals(self):
        denied = self.client.post("/api/v1/jobhunter/scan", json={"portals": ["ams"], "profile_id": "operations-demo"})
        self.assertEqual(401, denied.status_code)
        self.client.post("/api/v1/jobhunter/login", json={"passcode": "test-only-passcode"})
        response = self.client.post("/api/v1/jobhunter/scan", json={"portals": ["ams"], "profile_id": "operations-demo"})
        self.assertEqual(200, response.status_code)
        self.assertEqual(3, len(response.json()["results"][0]["jobs"]))

    def test_visit_notification_is_minimal_and_cooled_down(self):
        jobhunter._LAST_VISIT_ALERT = None
        with patch.object(jobhunter.time, "monotonic", return_value=1.0):
            with patch.object(jobhunter, "_send_visit_alert", new=AsyncMock(return_value=True)) as send:
                first = self.client.post("/api/v1/jobhunter/visit")
                second = self.client.post("/api/v1/jobhunter/visit")
        self.assertEqual("sent", first.json()["notification"])
        self.assertEqual("cooldown", second.json()["notification"])
        send.assert_awaited_once()
        self.assertNotIn("ip", first.text.casefold())


if __name__ == "__main__":
    unittest.main()
