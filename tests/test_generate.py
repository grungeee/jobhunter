"""tools/generate.py: the packet, the refusals, and the overwrite gate."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from types import ModuleType, SimpleNamespace
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import generate  # noqa: E402

LISTING = "00000000-0000-4000-8000-000000000001"
TEXT = ("We are hiring an operations coordinator for a site in Sampletown. "
        "You will maintain clear records, coordinate routine checks, and "
        "work with teams on delivery planning. A relevant qualification and "
        "experience in a comparable role are helpful. We offer a stable schedule, "
        "supportive colleagues, and paid training. ") * 2

PAYLOAD = {
    "cover_letter": [
        {"text": "Your advert asks for clear records and coordinated delivery. Both are central to the way I organize work. I would bring that same care to your team.",
         "excerpt": "clear records and coordinated delivery",
         "why": "Connect the wording to requirements stated in the advert."},
        {"text": "I would welcome the opportunity to discuss how I could contribute to your team.",
         "excerpt": "how I could contribute", "why": "Close by inviting a discussion about the role."},
    ],
    "cv_notes": "Lead with coordination experience.\nKeep the example systems current.",
    "documents": [{"name": "cv-example-2026.pdf", "note": "the CV release"},
                  {"name": "reference-example.pdf", "note": "the work reference"}],
    "company_summary": "A site in Sampletown. It coordinates routine operations and "
                       "supports several teams.",
    "rationale": {"why_letter": "The advert names two duties that match the example.",
                  "why_channel": "The advert asks for a PDF by email, so no portal."},
}


class GenerateTest(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="gen-test-")
        self.out = os.path.join(self.base, "packages")
        self.feed = os.path.join(self.base, "jobs.json")
        self.profile = os.path.join(self.base, "generation-inputs.json")
        self.prompt = ""
        patch = mock.patch.object(generate, "GENERATION_PROFILE", self.profile)
        patch.start()
        self.addCleanup(patch.stop)
        self.write_feed(TEXT)

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def write_feed(self, text, listing=LISTING):
        payload = {"jobs": [{"id": listing, "title": "Operations Coordinator",
                             "company": "Example Systems", "location": "Sampletown",
                             "full_text": text}]}
        with open(self.feed, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

    def stub(self, payload=None):
        """An engine that never leaves the process."""
        def engine(prompt):
            self.prompt = prompt
            if isinstance(payload, Exception):
                raise payload
            return dict(payload if payload is not None else PAYLOAD)
        return engine

    def files(self, listing=LISTING):
        return sorted(os.listdir(os.path.join(self.out, listing)))

    def test_regeneration_reloads_approved_cv_facts_and_preferences(self):
        generate.generate(LISTING, self.feed, self.out, self.stub())
        fact = "Documented fixture experience with preventive checks."
        rule = "Lead with the advert's main task, not a generic introduction."
        self.assertNotIn(fact, self.prompt)
        with open(self.profile, "w", encoding="utf-8") as handle:
            json.dump({"cv_facts": [{"fact": fact, "source": "Fixture CV section 2"}],
                       "application_preferences": [rule]}, handle)
        with self.assertRaisesRegex(generate.GenerateError, "already exists"):
            generate.generate(LISTING, self.feed, self.out, self.stub())
        generate.generate(LISTING, self.feed, self.out, self.stub(), force=True)
        self.assertIn(fact, self.prompt)
        self.assertIn("Fixture CV section 2", self.prompt)
        self.assertIn(rule, self.prompt)

    def test_a_fact_without_provenance_stops_before_engine_or_writes(self):
        with open(self.profile, "w", encoding="utf-8") as handle:
            json.dump({"cv_facts": [{"fact": "unsupported claim", "source": " "}],
                       "application_preferences": []}, handle)
        engine = mock.Mock(side_effect=AssertionError("must not call the engine"))
        with self.assertRaisesRegex(generate.GenerateError, "CV fact needs text and a source"):
            generate.generate(LISTING, self.feed, self.out, engine)
        engine.assert_not_called()
        self.assertFalse(os.path.exists(self.out))

    def test_generation_presets_pin_the_provider_model_and_reasoning(self):
        routes = {
            "fast-free": ("nous", "tencent/hy4-flash:free", "none"),
            "fastest": ("openai-codex", "gpt-6-luna", "low"),
            "regular": ("deepseek", "deepseek-flash", "none"),
            "smart": ("openai-codex", "gpt-6.2-sol", "medium"),
        }
        for preset, (provider, model, reasoning) in routes.items():
            with self.subTest(preset=preset), mock.patch.dict(os.environ, {"GENERATE_ENGINE_CMD": ""}):
                with mock.patch.object(generate, "run_pinned_engine", return_value=json.dumps(PAYLOAD)) as run:
                    self.assertEqual(0, generate.main([LISTING, "--feed", self.feed, "--out", self.out,
                                                      "--generation-model", preset, "--force"]))
                run.assert_called_once()
                prompt, selected, _ = run.call_args.args
                self.assertEqual((provider, model, reasoning), selected[1:])
                self.assertIn("Operations Coordinator", prompt)
                packet = generate.read_packet(self.out, LISTING)
                self.assertEqual(model, packet["meta.json"]["model"])
                self.assertEqual(provider, packet["meta.json"]["provider"])
                self.assertEqual(preset, packet["meta.json"]["generation_model"])

    def test_deepseek_flash_accepts_its_documented_serving_revision_only(self):
        def module(name, **attributes):
            mod = ModuleType(name)
            vars(mod).update(attributes)
            return mod

        preset = generate.GENERATION_MODELS["regular"]
        client = SimpleNamespace(base_url="https://api.deepseek.com/v1")
        route = mock.Mock(return_value=SimpleNamespace(client=client, model=preset.model))
        request = mock.Mock(return_value={"model": preset.model})
        answer = mock.Mock()
        modules = {
            "agent.auxiliary_client": module("agent.auxiliary_client", _build_call_kwargs=request,
                _create_with_progress_once=answer, _provider_requires_stream=lambda *_: False),
            "agent.route_binding": module("agent.route_binding", resolve_route_entry_client=route),
            "hermes_cli.env_loader": module("hermes_cli.env_loader", load_hermes_dotenv=lambda: None),
            "hermes_cli.fallback_check": module("hermes_cli.fallback_check",
                answered_by_requested_model=lambda requested, reported: requested == reported),
        }
        with mock.patch.dict(sys.modules, modules):
            for reported, accepted in (("deepseek-flash", True), ("deepseek-v4.1-flash-1017", True),
                                       ("deepseek-v4-pro", False), ("", False)):
                with self.subTest(reported=reported):
                    answer.return_value = SimpleNamespace(model=reported, choices=[
                        SimpleNamespace(message=SimpleNamespace(content="fixture reply"))])
                    if accepted:
                        self.assertEqual("fixture reply", generate._pinned_reply("prompt", preset, 10))
                    else:
                        with self.assertRaisesRegex(generate.EngineError, "did not confirm"):
                            generate._pinned_reply("prompt", preset, 10)
            self.assertEqual(4, answer.call_count)
            self.assertEqual("deepseek-flash", request.call_args.args[1])
            client.base_url = "https://example.com/v1"
            with self.assertRaisesRegex(generate.EngineError, "provider endpoint"):
                generate._pinned_reply("prompt", preset, 10)
            self.assertEqual(4, answer.call_count)

    def test_failed_selected_engine_cannot_replace_saved_draft(self):
        generate.generate(LISTING, self.feed, self.out, self.stub())
        previous = generate.read_packet(self.out, LISTING)
        failure = generate.EngineError("quota exhausted")
        with mock.patch.dict(os.environ, {"GENERATE_ENGINE_CMD": ""}), \
                mock.patch.object(generate, "run_pinned_engine", side_effect=failure) as run, \
                mock.patch.object(generate.subprocess, "run") as fallback:
            with self.assertRaisesRegex(generate.EngineError, "Fast & free.*quota exhausted"):
                generate.generate(LISTING, self.feed, self.out, force=True, generation_model="fast-free")
        run.assert_called_once()
        fallback.assert_not_called()
        self.assertEqual(previous, generate.read_packet(self.out, LISTING))

    def test_the_packet_holds_every_section(self):
        result = generate.generate(LISTING, self.feed, self.out, self.stub())
        for name in generate.FILES:
            self.assertIn(name, result["files"])
        self.assertEqual(sorted(result["files"]), self.files())
        self.assertTrue(generate.packet_ready(self.out, LISTING))

    def test_the_letter_also_ships_as_a_docx(self):
        import zipfile
        generate.generate(LISTING, self.feed, self.out, self.stub())
        path = os.path.join(self.out, LISTING, "cover-letter.docx")
        self.assertTrue(os.path.exists(path))
        with zipfile.ZipFile(path) as archive:
            self.assertIn("word/document.xml", archive.namelist())
            xml = archive.read("word/document.xml").decode("utf-8")
        self.assertIn("Your advert asks for clear records", xml)
        self.assertIn("Bewerbung als Operations Coordinator", xml)
        self.assertIn("Sehr geehrte Damen und Herren,", xml)
        self.assertIn("Mit freundlichen Grüßen", xml)
        self.assertIn(generate.APPLICANT, xml)
        for block in PAYLOAD["cover_letter"]:
            self.assertNotIn(block["why"], xml)
        with open(os.path.join(self.out, LISTING, "meta.json"), encoding="utf-8") as handle:
            meta = json.load(handle)
        self.assertIn("cover-letter.docx", meta["files"])

    def test_a_failed_docx_render_keeps_the_markdown_packet(self):
        def broken(*args, **kwargs):
            raise OSError("no zip for you")

        saved = generate.render_letter.write_docx
        generate.render_letter.write_docx = broken
        self.addCleanup(setattr, generate.render_letter, "write_docx", saved)
        result = generate.generate(LISTING, self.feed, self.out, self.stub())
        self.assertIn("cover-letter.md", result["files"])
        self.assertNotIn("cover-letter.docx", result["files"])
        self.assertTrue(generate.packet_ready(self.out, LISTING))

    def test_meta_names_the_listing_and_the_advert_size(self):
        generate.generate(LISTING, self.feed, self.out, self.stub())
        with open(os.path.join(self.out, LISTING, "meta.json"), encoding="utf-8") as handle:
            meta = json.load(handle)
        self.assertEqual(LISTING, meta["listing_id"])
        self.assertEqual(len(TEXT), meta["full_text_chars"])
        self.assertEqual("Operations Coordinator", meta["title"])

    def test_language_defaults_to_german_and_applies_to_all_prose(self):
        output = generate.generate(LISTING, self.feed, self.out, self.stub())
        packet = generate.read_packet(self.out, LISTING)
        self.assertIn("Write all application prose in German", self.prompt)
        self.assertNotIn("Write in English", self.prompt)
        self.assertEqual("de", packet["meta.json"]["language"])
        self.assertIn("# Warum dieses Anschreiben", packet["rationale.md"])
        self.assertTrue(output["packet"])

    def test_explicit_english_overrides_german_without_translating_source(self):
        output = generate.generate(LISTING, self.feed, self.out, dry_run=True, language="en")
        self.assertIn("Write all application prose in English", output["prompt"])
        self.assertIn(TEXT.strip(), output["prompt"])
        self.assertFalse(os.path.exists(self.out))

    def test_the_letter_carries_the_adverts_own_words_into_the_prompt(self):
        generate.generate(LISTING, self.feed, self.out, self.stub())
        self.assertIn("delivery planning", self.prompt)
        self.assertIn("never use em dashes", self.prompt.lower())

    def test_a_listing_without_advert_text_is_refused_and_writes_nothing(self):
        self.write_feed("Maintenance technician wanted.")
        with self.assertRaises(generate.GenerateError) as caught:
            generate.generate(LISTING, self.feed, self.out, self.stub())
        self.assertIn("characters of advert text", str(caught.exception))
        self.assertFalse(os.path.exists(self.out))

    def test_an_unknown_listing_is_refused(self):
        other = "11111111-2222-3333-4444-555555555555"
        with self.assertRaises(generate.GenerateError) as caught:
            generate.generate(other, self.feed, self.out, self.stub())
        self.assertIn("not in the feed", str(caught.exception))

    def test_a_listing_id_that_is_not_one_cannot_become_a_path(self):
        with self.assertRaises(generate.GenerateError):
            generate.generate("../../etc", self.feed, self.out, self.stub())

    def test_a_second_run_refuses_without_force(self):
        generate.generate(LISTING, self.feed, self.out, self.stub())
        with self.assertRaises(generate.GenerateError) as caught:
            generate.generate(LISTING, self.feed, self.out, self.stub())
        self.assertIn("already exists", str(caught.exception))

    def test_unconfirmed_generation_cannot_overwrite_a_packet_created_during_the_model_call(self):
        def racing_engine(prompt):
            generate.generate(LISTING, self.feed, self.out, self.stub())
            return dict(PAYLOAD, company_summary="Overwritten by a racing request")

        with self.assertRaisesRegex(generate.GenerateError, "already exists"):
            generate.generate(LISTING, self.feed, self.out, racing_engine)
        packet = generate.read_packet(self.out, LISTING)
        self.assertIn(PAYLOAD["company_summary"], packet["company.md"])

    def test_regeneration_preserves_previous_versions_and_can_restore_one(self):
        generate.generate(LISTING, self.feed, self.out, self.stub())
        original = generate.read_packet(self.out, LISTING)
        changed = dict(PAYLOAD, cover_letter=[dict(PAYLOAD["cover_letter"][0]),
                                              dict(PAYLOAD["cover_letter"][1],
                                                   text=("I would welcome a conversation about the project and explain how my "
                                                         "experience coordinating routine operations can support your team. "
                                                         "I would be glad to discuss the role and its priorities."),
                                                   excerpt="a conversation")])
        generate.generate(LISTING, self.feed, self.out, self.stub(changed), force=True)
        current = generate.read_packet(self.out, LISTING)
        self.assertNotEqual(original["cover-letter.md"], current["cover-letter.md"])
        self.assertEqual([1, 2], list(generate.letter_versions(self.out, LISTING)))
        self.assertEqual(original["cover-letter.md"],
                         generate.read_letter_version(self.out, LISTING, 1)["cover-letter.md"])
        with self.assertRaisesRegex(generate.EngineError, "fixture failure"):
            generate.generate(LISTING, self.feed, self.out,
                              self.stub(generate.EngineError("fixture failure")), force=True)
        self.assertEqual([1, 2], list(generate.letter_versions(self.out, LISTING)))
        with self.assertRaisesRegex(generate.GenerateError, "changed"):
            generate.restore_letter_version(self.out, LISTING, 1, expected=1)
        generate.restore_letter_version(self.out, LISTING, 1, expected=2)
        restored = generate.read_packet(self.out, LISTING)
        self.assertEqual(original["cover-letter.md"], restored["cover-letter.md"])
        self.assertEqual(original[generate.LETTER_REVIEW], restored[generate.LETTER_REVIEW])
        self.assertEqual(1, restored["meta.json"]["restored_from"])
        self.assertEqual([1, 2, 3], list(generate.letter_versions(self.out, LISTING)))
        self.assertEqual(current["cover-letter.md"],
                         generate.read_letter_version(self.out, LISTING, 2)["cover-letter.md"])

    def test_publish_failure_restores_active_packet(self):
        generate.generate(LISTING, self.feed, self.out, self.stub())
        original = generate.read_packet(self.out, LISTING)
        real_replace = os.replace

        def replace(source, dest):
            if os.path.basename(source).startswith('.packet-') and dest == os.path.join(self.out, LISTING):
                raise OSError('fixture publish failure')
            return real_replace(source, dest)

        with mock.patch.object(generate.os, 'replace', side_effect=replace):
            with self.assertRaisesRegex(generate.GenerateError, "fixture publish failure"):
                generate.generate(LISTING, self.feed, self.out, self.stub(), force=True)
        self.assertEqual(original, generate.read_packet(self.out, LISTING))
        self.assertEqual([1], list(generate.letter_versions(self.out, LISTING)))

    def test_a_stub_letter_leaves_no_packet_behind(self):
        short = dict(PAYLOAD, cover_letter=[{"text": "Dear team, hire me.", "excerpt": "hire me", "why": "Direct."}])
        with self.assertRaisesRegex(generate.EngineError, "stub"):
            generate.generate(LISTING, self.feed, self.out, self.stub(short))
        self.assertFalse(os.path.exists(os.path.join(self.out, LISTING)))
        leftovers = [name for name in os.listdir(self.out)] if os.path.isdir(self.out) else []
        self.assertEqual([], leftovers)

    def test_each_letter_block_requires_a_nonempty_explanation(self):
        for why in (None, "  ", ["not text"]):
            with self.subTest(why=why):
                blocks = list(PAYLOAD["cover_letter"])
                blocks[1] = dict(blocks[1], why=why)
                with self.assertRaisesRegex(generate.EngineError, "block 2"):
                    generate.generate(LISTING, self.feed, self.out,
                                      self.stub(dict(PAYLOAD, cover_letter=blocks)))
                self.assertFalse(os.path.exists(self.out))

    def test_excerpt_from_another_paragraph_is_rejected(self):
        first, second = PAYLOAD["cover_letter"]
        blocks = [dict(first, excerpt=second["excerpt"]), second]
        with self.assertRaisesRegex(generate.EngineError, "block 1 needs an exact excerpt"):
            generate.generate(LISTING, self.feed, self.out,
                              self.stub(dict(PAYLOAD, cover_letter=blocks)))
        self.assertFalse(os.path.exists(self.out))

    def test_a_missing_rationale_is_a_refusal_not_a_packet(self):
        partial = dict(PAYLOAD, rationale={"why_letter": "because"})
        with self.assertRaises(generate.EngineError) as caught:
            generate.generate(LISTING, self.feed, self.out, self.stub(partial))
        self.assertIn("why the letter and the channel", str(caught.exception))

    def test_a_dry_run_calls_no_engine_at_all(self):
        def engine(prompt):
            raise AssertionError("the engine must not be called")

        result = generate.generate(LISTING, self.feed, self.out, engine, dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertFalse(os.path.exists(self.out))

    def test_the_cli_reports_a_refusal_in_one_line(self):
        self.write_feed("too short")
        code = generate.main([LISTING, "--feed", self.feed, "--out", self.out])
        self.assertEqual(1, code)

    def test_the_cli_prints_the_packet_as_json(self):
        os.environ["GENERATE_ENGINE_CMD"] = "%s %s" % (
            sys.executable, os.path.join(HERE, "stub_engine.py"))
        try:
            with open(os.path.join(self.base, "reply.json"), "w", encoding="utf-8") as handle:
                json.dump(PAYLOAD, handle)
            os.environ["STUB_REPLY"] = os.path.join(self.base, "reply.json")
            code = generate.main([LISTING, "--feed", self.feed, "--out", self.out])
        finally:
            os.environ.pop("GENERATE_ENGINE_CMD", None)
            os.environ.pop("STUB_REPLY", None)
        self.assertEqual(0, code)
        self.assertTrue(generate.packet_ready(self.out, LISTING))

    def test_read_packet_returns_the_sections_for_a_review_window(self):
        generate.generate(LISTING, self.feed, self.out, self.stub())
        packet = generate.read_packet(self.out, LISTING)
        blocks = packet["cover-letter-review.json"]
        self.assertEqual("\n\n".join(block["text"] for block in blocks) + "\n",
                         packet["cover-letter.md"])
        self.assertEqual(PAYLOAD["cover_letter"], blocks[2:-2])
        self.assertEqual(blocks, generate.letter_review(packet))
        packet["cover-letter.md"] += "Edited after generation\n"
        self.assertEqual([], generate.letter_review(packet))
        self.assertIn("Warum dieser Bewerbungsweg", packet["rationale.md"])
        self.assertEqual(2, len(packet["documents.json"]))

    def test_read_packet_says_nothing_for_an_empty_folder(self):
        self.assertEqual({}, generate.read_packet(self.out, LISTING))


class EngineReplyTest(unittest.TestCase):
    def test_the_last_json_object_wins_among_chatter(self):
        text = 'thinking {"draft": 1}\nhere you go:\n{"a": {"b": 2}}\ndone'
        self.assertEqual({"a": {"b": 2}}, generate.last_json_object(text))

    def test_a_reply_without_json_is_none(self):
        self.assertIsNone(generate.last_json_object("I cannot help with that."))


if __name__ == "__main__":
    unittest.main()
