"""Tests for tools/listing_text.py. Standard library unittest only."""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import listing_text  # noqa: E402

LD_PAGE = """<html><head><title>Stellenangebot</title></head><body>
<script type="application/ld+json">
{"@context": "https://schema.org", "@type": "JobPosting", "title": "Service Coordinator",
 "description": "<p>Deine Aufgaben:</p><ul><li>Coordination</li><li>Scheduling</li></ul>"}
</script></body></html>"""

MAIN_PAGE = """<html><head><title>Job</title></head><body>
<main><h1>Service Coordinator</h1><p>%s</p><p>%s</p></main></body></html>""" % ("A" * 150, "B" * 150)

MAINTENANCE_PAGE = """<html><head><title>Wartungsarbeiten | AMS Österreich</title></head>
<body>Unsere Job-Suchmaschine ist vorübergehend nicht verfügbar.</body></html>"""


class ExtractTests(unittest.TestCase):
    def test_json_ld_jobposting_description_becomes_plain_text(self):
        text, extractor = listing_text.extract_text(LD_PAGE)
        self.assertEqual("json-ld", extractor)
        self.assertIn("Deine Aufgaben:", text)
        self.assertIn("- Coordination", text)
        self.assertIn("- Scheduling", text)
        self.assertNotIn("<", text)

    def test_main_element_is_the_fallback_and_short_bodies_are_rejected(self):
        text, extractor = listing_text.extract_text(MAIN_PAGE)
        self.assertEqual("main", extractor)
        self.assertIn("A" * 150, text)
        short, _ = listing_text.extract_text("<main><p>Zu wenig</p></main>")
        self.assertEqual("", short)

    def test_maintenance_is_detected_from_url_and_title(self):
        self.assertTrue(listing_text.is_maintenance(
            "https://jobs.ams.at/wartung/apache_services.html", ""))
        self.assertTrue(listing_text.is_maintenance("https://jobs.ams.at/x", MAINTENANCE_PAGE))
        self.assertFalse(listing_text.is_maintenance("https://jobs.ams.at/x", LD_PAGE))

    def test_maintenance_page_raises_instead_of_capturing(self):
        original = listing_text.fetch_detail
        listing_text.fetch_detail = lambda url, timeout=25: (_ for _ in ()).throw(
            listing_text.Maintenance("https://jobs.ams.at/wartung/apache_services.html"))
        try:
            with self.assertRaises(listing_text.Maintenance):
                listing_text.fetch_detail("https://jobs.ams.at/x")
        finally:
            listing_text.fetch_detail = original

    def test_longest_candidate_wins_so_a_wrapper_cannot_beat_the_ad_body(self):
        wrapper = ("<html><body><main><p>%s</p></main>"
                   "<div class=\"job-detail\"><p>%s</p><p>%s</p></div></body></html>"
                   % ("E" * 250, "C" * 300, "D" * 300))
        text, extractor = listing_text.extract_text(wrapper)
        self.assertEqual("job-container", extractor)
        self.assertIn("C" * 300, text)
        ranked = listing_text.candidates(wrapper)
        self.assertEqual([len(entry["text"]) for entry in ranked],
                         sorted((len(entry["text"]) for entry in ranked), reverse=True))
        self.assertGreater(len(ranked), 1)

    def test_a_page_without_any_container_yields_nothing(self):
        self.assertEqual("", listing_text.extract_text("<html><body><script>x</script></body></html>")[0])
        self.assertEqual([], listing_text.candidates("<html><body><p>hi</p></body></html>"))

    def test_live_ams_json_maps_three_named_sections_and_general_info(self):
        payload = {
            "objectType": "searchDetailJobOffer", "uuid": "abc",
            "title": "Example Role <b>m/w/d</b>",
            "companyIntroduction": "<p>Über uns &amp; Team</p><p>Contact: contact@example.test</p>",
            "summary": "<p>Aufgaben</p><p>Installieren &lt;prüfen&gt;</p>",
            "generalEqualitiyInfo": "€ 3.000 brutto pro Monat",
            "occupations": [{"name": "Example Role"}],
        }
        record = listing_text.record_from_ams_api(payload, "abc", "https://jobs.ams.at/public/emps/jobs/abc")
        self.assertEqual("Über uns & Team\nContact: contact@example.test", record["company_description"])
        self.assertEqual("Example Role m/w/d", record["occupation"])
        self.assertEqual("Aufgaben\nInstallieren <prüfen>", record["job_description"])
        self.assertEqual("€ 3.000 brutto pro Monat", record["general_info"])
        self.assertIn("Unternehmensbeschreibung\n", record["text"])
        self.assertIn("Beruf\n", record["text"])
        self.assertIn("Stellenbeschreibung\n", record["text"])
        self.assertEqual("ams-detail-api", record["extractor"])
        self.assertIn("/api/joboffer/uuid/abc", record["source"])
        self.assertTrue(record["fetched_at"].endswith("Z"))

    def test_detail_identity_must_match_before_any_section_is_cached(self):
        payload = {"uuid": "other", "title": "Wrong job", "summary": "<p>A" + "B" * 300 + "</p>"}
        with self.assertRaises(ValueError):
            listing_text.record_from_ams_api(payload, "abc", "https://jobs.ams.at/public/emps/jobs/abc")
        with self.assertRaises(ValueError):
            listing_text.record_from_ams_api({"uuid": "abc", "title": "T"}, "abc",
                                             "https://jobs.ams.at/public/emps/jobs/abc")


class CaptureTests(unittest.TestCase):
    def _job(self, **overrides):
        job = {"id": "abc", "url": "https://jobs.ams.at/public/emps/jobs/abc",
               "title": "Service Coordinator", "snippet": "kurz"}
        job.update(overrides)
        return job

    def test_capture_stores_text_once_and_keeps_provenance(self):
        cache = {}
        original = listing_text.fetch_detail
        listing_text.fetch_detail = lambda url, timeout=25: LD_PAGE
        try:
            self.assertEqual("captured", listing_text.capture(self._job(), cache))
        finally:
            listing_text.fetch_detail = original
        entry = cache["abc"]
        self.assertIn("Coordination", entry["text"])
        self.assertEqual("https://jobs.ams.at/public/emps/jobs/abc", entry["source"])
        self.assertEqual("json-ld", entry["extractor"])
        self.assertTrue(entry["fetched_at"].endswith("Z"))

    def test_a_body_no_longer_than_the_card_preview_is_not_stored(self):
        cache = {}
        original = listing_text.fetch_detail
        listing_text.fetch_detail = lambda url, timeout=25: MAIN_PAGE
        try:
            result = listing_text.capture(self._job(snippet="A" * 400), cache)
        finally:
            listing_text.fetch_detail = original
        self.assertEqual("too-short", result)
        self.assertEqual({}, cache)

    def test_jobs_without_an_ams_url_are_skipped(self):
        cache = {}
        self.assertEqual("skipped", listing_text.capture(self._job(url=""), cache))
        self.assertEqual("skipped", listing_text.capture(self._job(url="https://example.invalid/1"), cache))
        self.assertEqual({}, cache)

    def test_cache_roundtrip_and_status_counts(self):
        with tempfile.TemporaryDirectory() as folder:
            cache_path = os.path.join(folder, "listing_text.json")
            feed_path = os.path.join(folder, "jobs.json")
            with open(feed_path, "w", encoding="utf-8") as handle:
                json.dump({"jobs": [{"id": "abc", "url": "https://jobs.ams.at/x"},
                                    {"id": "def", "url": "https://jobs.ams.at/y"}]}, handle)
            listing_text.save_cache({"abc": {"text": "voller Text", "source": "u",
                                             "extractor": "json-ld",
                                             "fetched_at": "2026-11-13T20:00:00Z"}}, cache_path)
            cache = listing_text.load_cache(cache_path)
            self.assertEqual(["abc"], list(cache))
            self.assertEqual([{"id": "abc", "url": "https://jobs.ams.at/x"},
                              {"id": "def", "url": "https://jobs.ams.at/y"}],
                             listing_text.load_jobs(feed_path))
            self.assertEqual(0, listing_text.main(["--feed", feed_path,
                                                   "--cache", cache_path, "status"]))

    def test_missing_or_broken_cache_reads_as_empty(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual({}, listing_text.load_cache(os.path.join(folder, "nope.json")))
            broken = os.path.join(folder, "broken.json")
            with open(broken, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            self.assertEqual({}, listing_text.load_cache(broken))


if __name__ == "__main__":
    unittest.main()
