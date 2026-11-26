"""Tests for tools/feed.py. Standard library unittest only."""
import json
import os
import shutil
import sys
import tempfile
import unittest

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
sys.path.insert(0, TOOLS)

import feed as feedmod  # noqa: E402


def write_json(path, payload):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)


class FeedSandbox(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="feed-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def write_sources(self, state=None, raw=None, scored=None, apps=None):
        write_json(os.path.join(self.dir, "state.json"),
                   state if state is not None else {"seen": {}, "updated_at": "2026-01-01"})
        write_json(os.path.join(self.dir, "raw_jobs.json"),
                   raw if raw is not None else {"jobs": [], "unique_jobs": 0})
        write_json(os.path.join(self.dir, "scored_jobs.json"),
                   scored if scored is not None else [])
        write_json(os.path.join(self.dir, "applications.json"),
                   apps if apps is not None else {"jobs": {}, "updated_at": "2026-01-01"})

    def sample(self):
        state = {
            "updated_at": "2026-11-10T07:43:54+01:00",
            "seen": {
                "id-a": {"title": "A", "company": "Alpha", "ort": "Sampletown", "score": 7.0,
                         "first_seen": "2026-11-01", "last_seen": "2026-11-10"},
                "id-b": {"title": "B", "company": "Beta", "ort": "Exampleville", "score": 9.0,
                         "first_seen": "2026-10-26", "last_seen": "2026-11-09"},
                "id-c": {"title": "C", "company": "Gamma", "ort": "Rivertown",
                         "first_seen": "2026-10-26", "last_seen": "2026-10-30"},
            },
        }
        raw = {
            "scraped_at": "2026-11-10T06:43:54.444Z", "period": "LAST_WEEK",
            "location": "Sampletown (20 km)", "keywords": 32, "unique_jobs": 2,
            "jobs": [
                {"uuid": "id-a", "title": "A fresh", "company": "Alpha", "ort": "Sampletown",
                 "url": "https://example.invalid/a", "datum": "05.11.2026"},
                {"uuid": "id-b", "title": "B fresh", "company": "Beta", "ort": "Exampleville",
                 "url": "https://example.invalid/b", "datum": "04.11.2026"},
            ],
        }
        scored = [
            {"uuid": "id-a", "score": 8.5, "matched_roles": ["Coordination"],
             "matched_skills": ["Scheduling"], "datum": "05.11.2026"},
        ]
        apps = {
            "updated_at": "2026-11-10T17:00:11+01:00",
            "jobs": {
                "id-b": {"title": "B", "company": "Beta", "ort": "Exampleville", "score": 9.0,
                         "status": "applied", "sent_at": "2026-11-09T09:00:00+01:00",
                         "note": "sent by post", "roles": ["Coordination"]},
            },
        }
        self.write_sources(state, raw, scored, apps)
        return feedmod.build_feed(self.dir)


class BuildTests(FeedSandbox):
    def test_total_counts_every_known_id(self):
        feed = self.sample()
        self.assertEqual(feed["counts"]["total"], 3)

    def test_status_defaults_to_new(self):
        feed = self.sample()
        by_id = {job["id"]: job for job in feed["jobs"]}
        self.assertEqual(by_id["id-a"]["status"], "new")
        self.assertEqual(by_id["id-c"]["status"], "new")

    def test_tracker_status_wins(self):
        feed = self.sample()
        by_id = {job["id"]: job for job in feed["jobs"]}
        self.assertEqual(by_id["id-b"]["status"], "applied")
        self.assertEqual(by_id["id-b"]["status_label"], "Applied")

    def test_current_scan_flag(self):
        feed = self.sample()
        by_id = {job["id"]: job for job in feed["jobs"]}
        self.assertTrue(by_id["id-a"]["in_latest_scan"])
        self.assertFalse(by_id["id-c"]["in_latest_scan"])

    def test_current_scan_fields_override_history(self):
        feed = self.sample()
        by_id = {job["id"]: job for job in feed["jobs"]}
        self.assertEqual(by_id["id-a"]["title"], "A fresh")
        self.assertEqual(by_id["id-c"]["title"], "C")

    def test_score_prefers_current_scan_scores(self):
        feed = self.sample()
        by_id = {job["id"]: job for job in feed["jobs"]}
        self.assertEqual(by_id["id-a"]["score"], 8.5)
        self.assertEqual(by_id["id-a"]["score_source"], "scored_jobs.json")

    def test_score_falls_back_to_history(self):
        feed = self.sample()
        by_id = {job["id"]: job for job in feed["jobs"]}
        self.assertEqual(by_id["id-b"]["score"], 9.0)
        self.assertIn("historical", by_id["id-b"]["score_source"])

    def test_score_missing_is_none(self):
        feed = self.sample()
        by_id = {job["id"]: job for job in feed["jobs"]}
        self.assertIsNone(by_id["id-c"]["score"])

    def test_roles_come_from_score_record(self):
        feed = self.sample()
        by_id = {job["id"]: job for job in feed["jobs"]}
        self.assertEqual(by_id["id-a"]["roles"], ["Coordination"])

    def test_roles_fall_back_to_tracker(self):
        feed = self.sample()
        by_id = {job["id"]: job for job in feed["jobs"]}
        self.assertEqual(by_id["id-b"]["roles"], ["Coordination"])

    def test_status_order_puts_outcomes_first(self):
        feed = self.sample()
        self.assertEqual(feed["jobs"][0]["status"], "applied")

    def test_high_score_new_listing_sorts_above_low_score(self):
        feed = self.sample()
        new_jobs = [job for job in feed["jobs"] if job["status"] == "new"]
        self.assertEqual(new_jobs[0]["id"], "id-a")

    def test_counts_include_status_buckets(self):
        feed = self.sample()
        self.assertEqual(feed["counts"]["new"], 2)
        self.assertEqual(feed["counts"]["applied"], 1)
        self.assertEqual(feed["counts"]["interview"], 0)
        self.assertEqual(feed["counts"]["in_latest_scan"], 2)

    def test_scan_metadata_is_carried(self):
        feed = self.sample()
        self.assertEqual(feed["scan"]["location"], "Sampletown (20 km)")
        self.assertEqual(feed["scan"]["period"], "LAST_WEEK")
        self.assertEqual(feed["scan"]["keyword_count"], 32)

    def test_source_hashes_are_recorded(self):
        feed = self.sample()
        self.assertEqual(feed["sources"]["state"]["sha256"].__len__(), 64)
        self.assertTrue(feed["sources"]["raw"]["present"])

    def test_no_dash_characters_in_rendered_titles(self):
        feed = self.sample()
        table = feedmod.render_table(feed["jobs"], feed["counts"])
        self.assertNotIn("\u2014", table)
        self.assertNotIn("\u2013", table)

    def test_build_is_deterministic_except_timestamp(self):
        first = self.sample()
        second = feedmod.build_feed(self.dir)
        first.pop("generated_at")
        second.pop("generated_at")
        self.assertEqual(first, second)


class PayTests(FeedSandbox):
    def test_explicit_monthly_amounts_in_real_snippet_shapes(self):
        cases = [
            ("Gehalt € 3.500 brutto/Monat Fixum", 3500.0),
            ("2.705,40 beträgt der kollektivvertragliche Bruttomonatslohn", 2705.4),
            ("Entlohnung von monatlich 3.000", 3000.0),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(expected, feedmod._pay_monthly_min(text))

    def test_unspecified_or_annual_pay_remains_unknown(self):
        for text in ("Gehalt: ab EUR 3.144,20 Arbeitszeit: Vollzeit",
                     "ab € 4.000,- Zeit für eine neue Herausforderung",
                     "Jahresgehalt € 60.000, monatlich wechselnde Dienste",
                     "Inserat-Nr: 2009_2026 Ansprechpartner: +1 202-555-0100"):
            with self.subTest(text=text):
                self.assertIsNone(feedmod._pay_monthly_min(text))

    def test_yearly_pay_is_divided_by_fourteen_austrian_payments(self):
        # 12 salary months plus holiday and Christmas pay.
        self.assertEqual(14, feedmod.PAYMENTS_PER_YEAR_AT)
        self.assertEqual(3800.0, feedmod.monthly_from_yearly(53200))
        self.assertEqual(2535.71, feedmod.monthly_from_yearly(35500))
        self.assertIsNone(feedmod.monthly_from_yearly(None))
        self.assertIsNone(feedmod.monthly_from_yearly("53200"))

    def test_feed_adds_a_calculated_month_only_when_it_has_a_year(self):
        raw = {"jobs": [{"uuid": "y1", "title": "Yearly", "snippet": "",
                         "general_info": "Mindestentgelt 53.200,00 EUR brutto pro Jahr"},
                        {"uuid": "m1", "title": "Monthly", "snippet":
                         "Gehalt € 3.500 brutto/Monat"},
                        {"uuid": "u1", "title": "Unknown", "snippet": "Nichts zum Gehalt"}]}
        self.write_sources(raw=raw)
        jobs = {row["id"]: row for row in feedmod.build_feed(self.dir)["jobs"]}
        self.assertEqual(3800.0, jobs["y1"]["pay_monthly_calc"])
        self.assertIsNone(jobs["y1"]["pay_monthly_min"])
        self.assertEqual(3500.0, jobs["m1"]["pay_monthly_min"])
        self.assertIsNone(jobs["m1"]["pay_monthly_calc"])
        self.assertIsNone(jobs["u1"]["pay_monthly_calc"])

    def test_feed_includes_pay_without_mutating_source(self):
        raw = {"jobs": [{"uuid": "m1", "title": "Monthly", "snippet":
                         "Gehalt € 3.500 brutto/Monat"},
                        {"uuid": "u2", "title": "Unknown", "snippet":
                         "Gehalt EUR 3.200 Arbeitszeit Vollzeit"}]}
        self.write_sources(raw=raw)
        jobs = {row["id"]: row for row in feedmod.build_feed(self.dir)["jobs"]}
        self.assertEqual(3500.0, jobs["m1"]["pay_monthly_min"])
        self.assertIsNone(jobs["u2"]["pay_monthly_min"])

    def test_monthly_pay_is_found_outside_the_search_card(self):
        # The card is often empty; the AMS general-info sentence and the ad
        # body both carry the figure, so both are searched.
        raw = {"jobs": [
            {"uuid": "g1", "title": "General", "snippet": "",
             "general_info": "Das Mindestentgelt beträgt 2.920,00 EUR brutto pro Monat "
                             "auf Basis Vollzeitbeschäftigung."},
            {"uuid": "b1", "title": "Body",
             "full_text": "Entlohnung: Gehalt nach KV ab € 2947,89,- brutto/Monat"},
        ]}
        self.write_sources(raw=raw)
        jobs = {row["id"]: row for row in feedmod.build_feed(self.dir)["jobs"]}
        self.assertEqual(2920.0, jobs["g1"]["pay_monthly_min"])
        self.assertEqual("month", jobs["g1"]["pay_basis"])
        self.assertEqual(2947.89, jobs["b1"]["pay_monthly_min"])
        self.assertIsNone(jobs["b1"]["pay_yearly_min"])

    def test_yearly_pay_replaces_a_missing_monthly_figure(self):
        raw = {"jobs": [{"uuid": "y1", "title": "Yearly", "snippet": "",
                         "general_info": "Das Mindestentgelt beträgt 53.200,00 EUR brutto "
                                         "pro Jahr auf Basis Vollzeitbeschäftigung."}]}
        self.write_sources(raw=raw)
        job = feedmod.build_feed(self.dir)["jobs"][0]
        self.assertIsNone(job["pay_monthly_min"])
        self.assertEqual(53200.0, job["pay_yearly_min"])
        self.assertEqual("year", job["pay_basis"])

    def test_pay_terms_keeps_both_bases_without_converting_between_them(self):
        self.assertEqual((3500.0, 53200.0),
                         feedmod._pay_terms("Gehalt € 3.500 brutto/Monat",
                                            "Jahresgehalt 53.200,00 EUR brutto pro Jahr"))
        self.assertEqual((None, None), feedmod._pay_terms("", "Keine Angabe zum Gehalt"))

    def test_amount_beside_both_bases_stays_unknown(self):
        mixed = "Jahresgehalt € 60.000, monatlich wechselnde Dienste"
        self.assertIsNone(feedmod._pay_monthly_min(mixed))
        self.assertIsNone(feedmod._pay_yearly_min(mixed))


class FullListingTextTests(FeedSandbox):
    def test_structured_detail_sections_from_cache_reach_the_feed(self):
        self.write_sources(raw={"jobs": [{"uuid": "one", "title": "Card title", "snippet": "Card preview"}]})
        cache = os.path.join(self.dir, "listing_text.json")
        write_json(cache, {"one": {
            "text": "Unternehmensbeschreibung\nAn honest employer\n\nBeruf\nAPI role\n\nStellenbeschreibung\nFull tasks",
            "company_description": "An honest employer", "occupation": "API role",
            "job_description": "Full tasks", "general_info": "Paid monthly", "source": "https://jobs.ams.at/public/emps/api/joboffer/uuid/one",
            "fetched_at": "2026-11-14T00:00:00Z"}})
        job = feedmod.build_feed(self.dir, text_cache=cache)["jobs"][0]
        self.assertEqual("An honest employer", job["company_description"])
        self.assertEqual("API role", job["occupation"])
        self.assertEqual("Full tasks", job["job_description"])
        self.assertEqual("Paid monthly", job["general_info"])
        self.assertIn("Unternehmensbeschreibung", job["full_text"])
        self.assertEqual("Card preview", job["snippet"])

    def test_missing_detail_sections_are_empty_not_filled_from_title_or_snippet(self):
        self.write_sources(raw={"jobs": [{"uuid": "one", "title": "Card title", "snippet": "Card preview"}]})
        job = feedmod.build_feed(self.dir, text_cache=os.path.join(self.dir, "missing.json"))["jobs"][0]
        for key in ("company_description", "occupation", "job_description", "general_info"):
            self.assertEqual("", job[key])

    def test_enrich_existing_feed_preserves_snapshot_when_scan_is_empty(self):
        old = {"jobs": [{"id": "one", "title": "Historical title", "snippet": "Historical preview",
                         "score": 9.2}, {"id": "two", "title": "Second"}],
               "counts": {"total": 2}, "sources": {"raw": {"sha256": "old"}}}
        cached = {"one": {"text": "Long source text", "company_description": "Employer",
                          "occupation": "Full title", "job_description": "Full tasks"}}
        enriched = feedmod.enrich_with_cached_details(old, cached)
        self.assertEqual("Historical preview", enriched["jobs"][0]["snippet"])
        self.assertEqual(9.2, enriched["jobs"][0]["score"])
        self.assertEqual("Employer", enriched["jobs"][0]["company_description"])
        self.assertEqual("", enriched["jobs"][1]["job_description"])
        self.assertEqual(2, enriched["counts"]["total"])
        self.assertEqual("old", enriched["sources"]["raw"]["sha256"])
        self.assertEqual("", old["jobs"][0].get("job_description", ""))

    def test_full_text_is_never_inferred_from_a_card_preview(self):
        self.write_sources(raw={"jobs": [{"uuid": "one", "title": "One",
                                          "snippet": "A 253-character preview..."}]})
        job = feedmod.build_feed(self.dir)["jobs"][0]
        self.assertEqual("", job["full_text"])
        self.assertEqual("A 253-character preview...", job["snippet"])

    def test_actual_full_text_survives_normalization(self):
        text = "Full ad heading\n\nTasks and salary\nApply by email"
        self.write_sources(raw={"jobs": [{"uuid": "one", "title": "One",
                                          "snippet": "Full ad heading...",
                                          "full_text": text}]})
        job = feedmod.build_feed(self.dir)["jobs"][0]
        self.assertEqual(text, job["full_text"])

    def test_captured_ad_body_fills_full_text_when_the_source_has_none(self):
        self.write_sources(raw={"jobs": [{"uuid": "one", "title": "One",
                                          "snippet": "Card preview only..."}]})
        cache = os.path.join(self.dir, "listing_text.json")
        write_json(cache, {"one": {"text": "Captured body\nSecond line",
                                   "source": "https://jobs.ams.at/x",
                                   "fetched_at": "2026-11-13T20:00:00Z"}})
        job = feedmod.build_feed(self.dir, text_cache=cache)["jobs"][0]
        self.assertEqual("Captured body\nSecond line", job["full_text"])
        self.assertEqual("Card preview only...", job["snippet"])

    def test_a_source_body_wins_over_the_cache(self):
        self.write_sources(raw={"jobs": [{"uuid": "one", "title": "One",
                                          "full_text": "From the source"}]})
        cache = os.path.join(self.dir, "listing_text.json")
        write_json(cache, {"one": {"text": "From the cache"}})
        self.assertEqual("From the source",
                         feedmod.build_feed(self.dir, text_cache=cache)["jobs"][0]["full_text"])

    def test_a_missing_cache_leaves_the_text_empty(self):
        self.write_sources(raw={"jobs": [{"uuid": "one", "title": "One",
                                          "snippet": "Card preview only..."}]})
        missing = os.path.join(self.dir, "absent.json")
        self.assertEqual("", feedmod.build_feed(self.dir, text_cache=missing)["jobs"][0]["full_text"])


class DegradedTests(FeedSandbox):
    def test_missing_source_produces_warning(self):
        self.write_sources()
        os.remove(os.path.join(self.dir, "scored_jobs.json"))
        feed = feedmod.build_feed(self.dir)
        self.assertTrue(any("scored" in warning for warning in feed["warnings"]))
        self.assertEqual(feed["counts"]["total"], 0)

    def test_corrupt_source_degrades_without_raising(self):
        self.write_sources()
        with open(os.path.join(self.dir, "raw_jobs.json"), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        feed = feedmod.build_feed(self.dir)
        self.assertTrue(any("raw" in warning for warning in feed["warnings"]))

    def test_missing_directory_still_builds(self):
        feed = feedmod.build_feed(os.path.join(self.dir, "does-not-exist"))
        self.assertEqual(feed["jobs"], [])
        self.assertEqual(len(feed["warnings"]), 4)

    def test_raw_only_job_appears_without_history(self):
        self.write_sources(raw={"jobs": [{"uuid": "z", "title": "Zed", "company": "Zeta"}],
                                "unique_jobs": 1})
        feed = feedmod.build_feed(self.dir)
        self.assertEqual(feed["counts"]["total"], 1)
        self.assertEqual(feed["jobs"][0]["first_seen"], "")


class SelectionTests(FeedSandbox):
    def setUp(self):
        super().setUp()
        self.feed = self.sample()

    def test_open_only_excludes_decided_rows(self):
        jobs = feedmod.select(self.feed, open_only=True)
        self.assertEqual({job["id"] for job in jobs}, {"id-a", "id-c"})

    def test_status_filter(self):
        jobs = feedmod.select(self.feed, statuses=["applied"])
        self.assertEqual([job["id"] for job in jobs], ["id-b"])

    def test_status_filter_is_case_insensitive(self):
        jobs = feedmod.select(self.feed, statuses=["APPLIED"])
        self.assertEqual(len(jobs), 1)

    def test_min_score_drops_unscored(self):
        jobs = feedmod.select(self.feed, min_score=8.0)
        self.assertEqual({job["id"] for job in jobs}, {"id-a", "id-b"})

    def test_limit_truncates(self):
        self.assertEqual(len(feedmod.select(self.feed, limit=2)), 2)

    def test_limit_zero_returns_nothing(self):
        self.assertEqual(feedmod.select(self.feed, limit=0), [])

    def test_select_does_not_mutate_feed(self):
        before = len(self.feed["jobs"])
        feedmod.select(self.feed, limit=1, statuses=["new"])
        self.assertEqual(len(self.feed["jobs"]), before)


class RenderTests(FeedSandbox):
    def test_table_has_header_and_summary(self):
        feed = self.sample()
        table = feedmod.render_table(feed["jobs"], feed["counts"])
        self.assertIn("STATUS", table)
        self.assertIn("total 3", table)
        self.assertIn("showing 3", table)

    def test_empty_table_still_renders(self):
        table = feedmod.render_table([], {"total": 0})
        self.assertIn("showing 0", table)

    def test_score_format_handles_none(self):
        self.assertEqual(feedmod.format_score(None).strip(), "-")
        self.assertEqual(feedmod.format_score(8.5).strip(), "8.50")


class WriteTests(FeedSandbox):
    def test_write_leaves_no_pending_file(self):
        feed = self.sample()
        path = feedmod.write_feed(feed, os.path.join(self.dir, "out", "jobs.json"))
        self.assertTrue(os.path.exists(path))
        leftovers = [name for name in os.listdir(os.path.dirname(path))
                     if name.startswith(".pending-") or ".pending-" in name]
        self.assertEqual(leftovers, [])

    def test_written_feed_round_trips(self):
        feed = self.sample()
        path = feedmod.write_feed(feed, os.path.join(self.dir, "jobs.json"))
        with open(path, encoding="utf-8") as handle:
            loaded = json.load(handle)
        self.assertEqual(loaded["counts"]["total"], 3)

    def test_main_prints_json(self):
        self.sample()
        self.assertEqual(feedmod.main(["--source", self.dir, "--json"]), 0)

    def test_main_writes_file(self):
        self.sample()
        target = os.path.join(self.dir, "jobs.json")
        saved = feedmod.FEED_FILE
        feedmod.FEED_FILE = target
        self.addCleanup(setattr, feedmod, "FEED_FILE", saved)
        self.assertEqual(feedmod.main(["--source", self.dir, "--write"]), 0)
        self.assertTrue(os.path.exists(target))

    def test_main_unknown_flag_exits_two(self):
        with self.assertRaises(SystemExit) as caught:
            feedmod.main(["--nonsense"])
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
