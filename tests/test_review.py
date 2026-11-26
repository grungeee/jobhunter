import json
import curses
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from test_generate import LISTING, PAYLOAD, TEXT, generate

import cv_history
import review
import review_tui
import rule_chat


class ReviewTest(unittest.TestCase):
    def setUp(self):
        base = tempfile.TemporaryDirectory()
        self.addCleanup(base.cleanup)
        self.profile = Path(base.name) / 'generation-inputs.json'
        profile = patch.object(generate, 'GENERATION_PROFILE', self.profile)
        profile.start()
        self.addCleanup(profile.stop)

    def test_framing_and_prompt_share_rule_without_rewriting_advert(self):
        row = {'title': 'Operations Lead_in (m/w/d) für R&D', 'full_text': TEXT}
        before = generate.generation_profile()
        self.assertIn('Lead_in', generate._letter_blocks(row, PAYLOAD['cover_letter'], 'de', before)[0]['text'])
        proposed = review.merge(before, {'masculine_role_titles': True, 'preferences': [], 'facts': []}, [])
        review.save_profile(before, proposed)
        profile = generate.generation_profile()
        self.assertEqual(generate._letter_blocks(row, PAYLOAD['cover_letter'], 'de', profile)[0]['text'],
                         '# Bewerbung als Operations Lead für R&D')
        prompt = generate.build_prompt(row, mats={}, profile=profile)
        self.assertIn('applicant-facing role title: Operations Lead für R&D', prompt)
        self.assertIn('title as advertised: Operations Lead_in (m/w/d) für R&D', prompt)
        self.assertIn('Lead_in', generate._letter_blocks(row, PAYLOAD['cover_letter'], 'en', profile)[0]['text'])
        self.assertEqual(generate.titled_role('Operations_Module', 'de', profile), 'Operations_Module')

    def test_write_is_atomic_private_and_refuses_stale_profile(self):
        base = generate.generation_profile()
        proposal = review.merge(base, {'masculine_role_titles': True, 'preferences': [], 'facts': []}, [])
        self.assertFalse(self.profile.exists())
        review.save_profile(base, proposal)
        self.assertEqual(self.profile.stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(generate.GenerateError, 'changed'):
            review.save_profile(base, proposal)
        self.assertEqual(generate.generation_profile(), proposal)

    def test_fresh_exact_approval_changes_next_generated_letter_only(self):
        base = generate.generation_profile()
        proposal = review.merge(base, {'masculine_role_titles': True, 'preferences': [], 'facts': []}, [])
        root = self.profile.parent
        feed = root / 'jobs.json'
        feed.write_text(json.dumps({'jobs': [{'id': LISTING, 'title': 'Operations Lead_in', 'full_text': TEXT}]}))
        out = root / 'packets'
        generate.generate(LISTING, feed, out, lambda _: PAYLOAD)
        old = generate.read_packet(out, LISTING)
        approval = f'apply {review.digest(["fixture nonce", base, proposal])[:12]}'
        with patch('builtins.print'), patch('builtins.input', return_value='yes'):
            review.offer_save(base, proposal)
        self.assertFalse(self.profile.exists())
        with (patch('builtins.print'), patch('builtins.input', return_value=approval),
              patch.object(review.uuid, 'uuid4') as nonce):
            nonce.return_value.hex = 'fixture nonce'
            review.offer_save(base, proposal)
        self.assertEqual(generate.read_packet(out, LISTING), old)
        generate.generate(LISTING, feed, out, lambda _: PAYLOAD, force=True)
        new = generate.read_packet(out, LISTING)
        self.assertEqual(new['cover-letter.md'].splitlines()[0], '# Bewerbung als Operations Lead')
        self.assertEqual(generate.letter_review(new)[0]['excerpt'], 'Bewerbung als Operations Lead')
        self.assertEqual(new['meta.json']['title'], 'Operations Lead_in')

    def test_only_verbatim_user_evidence_can_support_a_new_fact(self):
        candidate = {'masculine_role_titles': False, 'preferences': [], 'facts': [
            {'fact': 'fixture fact', 'evidence': 'Invented'}]}
        with self.assertRaisesRegex(generate.GenerateError, 'evidence'):
            review.merge(generate.generation_profile(), candidate, ['Unrelated correction'])

    def test_save_this_letter_is_independent_of_profile_approval(self):
        out = self.profile.parent / 'packets'
        folder = out / LISTING
        folder.mkdir(parents=True)
        generate._write(folder, LISTING, {'title': 'Operations Lead_in'}, PAYLOAD, None, 'de')
        before = generate.read_packet(out, LISTING)
        block = review.review_blocks(before)[0]
        state = review_tui.Revision(block, 'de', 1,
            save_letter=lambda draft: review.save_block(out, LISTING, 1, before, draft))
        state.latest = {'text': 'Bewerbung als Operations Lead', 'reason': 'Use the corrected title.'}
        with patch.object(review, 'call_model') as model:
            state.key(curses.KEY_F2)
            self.assertEqual(state.mode, 'confirm_letter')
            state.key('\x1b')
            self.assertEqual(before, generate.read_packet(out, LISTING))
            state.key(curses.KEY_F2)
            state.key('\n')
            model.assert_not_called()
        self.assertEqual(state.saved_letter, 2)
        after = generate.read_packet(out, LISTING)
        self.assertEqual(after['cover-letter.md'].splitlines()[0], '# Bewerbung als Operations Lead')
        self.assertEqual(generate.letter_review(after)[0]['excerpt'], 'Bewerbung als Operations Lead')
        self.assertEqual(after['cv-notes.md'], before['cv-notes.md'])
        self.assertFalse(self.profile.exists())
        with ZipFile(folder / 'cover-letter.docx') as docx:
            content = docx.read('word/document.xml').decode()
        self.assertIn('Bewerbung als Operations Lead<', content)
        self.assertNotIn('Lead_in', content)
        self.assertEqual(before, generate.read_letter_version(out, LISTING, 1))
        with self.assertRaisesRegex(generate.GenerateError, 'changed'):
            review.save_block(out, LISTING, 1, before, state.latest)

    def test_session_starts_with_selected_block_and_pro_uses_accepted_edit(self):
        patcher = patch.object(generate, 'DEFAULT_OUT', self.profile.parent / 'packets')
        with patcher, patch.object(review, 'call_model') as model:
            packet = self.profile.parent / 'packets' / LISTING
            packet.mkdir(parents=True)
            row = {'title': 'Operations Lead_in', 'full_text': TEXT}
            generate._write(packet, LISTING, row, PAYLOAD, None, 'de')
            block = review.load_selection(LISTING, 1, None)
            self.assertEqual(block['excerpt'], 'Bewerbung als Operations Lead_in')
            with self.assertRaisesRegex(generate.GenerateError, 'changed on the server'):
                review.load_selection(LISTING, 1, 'wrong digest')
            model.side_effect = [
                {'text': 'Bewerbung als Operations Lead', 'reason': 'Masculine self-reference.'},
                {'masculine_role_titles': True, 'preferences': [], 'facts': []}]
            answers = iter(['Use the masculine title in German applications.', '/accept', 'no'])
            with patch('builtins.input', side_effect=lambda _: next(answers)), patch('builtins.print'):
                review.conversation(block, 'de')
            flash, pro = [args.args for args in model.call_args_list]
            self.assertEqual((flash[0], pro[0]), ('deepseek-v4-flash', 'deepseek-v4-pro'))
            self.assertIn('Operations Lead_in', flash[2])
            self.assertIn('Bewerbung als Operations Lead"', pro[2])
            self.assertNotEqual(flash[1], pro[1])
            self.assertFalse(self.profile.exists())
            self.assertEqual((packet / 'cover-letter.md').read_text().splitlines()[0], '# Bewerbung als Operations Lead_in')


class CVHistoryTest(unittest.TestCase):
    def test_inventory_is_numeric_and_diff_reads_only_selected_versions(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            src = root / 'src'
            src.mkdir()
            for name in ['cv_v7.0.1.pdf', 'cv_v9.0.0_2026-01-01.pdf', 'cv_v11.0.0_2026-01-02.pdf', 'other.pdf']:
                (src / name).touch()
            inventory = cv_history.versions(src)
            self.assertEqual(list(inventory), ['7.0.1', '9.0.0', '11.0.0'])
            change = cv_history.diff('9.0.0', ['Basics', 'Old'], '11.0.0', ['Basics', 'New'])
            self.assertIn('--- CV 9.0.0', change)
            self.assertIn('-Old', change)
            self.assertIn('+New', change)


class ChatRuleTests(unittest.TestCase):
    profile: Path
    setUp = ReviewTest.setUp
    def test_jobhunter_rule_from_chat_requires_exact_fresh_approval_and_preserves_cv(self):
        base = {'cv_facts': [{'fact': 'Existing fact', 'source': 'Fixture'}], 'application_preferences': ['Keep this']}
        self.profile.write_text(json.dumps(base))
        with patch.object(rule_chat, 'PENDING', self.profile.parent / 'proposals'):
            receipt = rule_chat.propose('Mention only evidence relevant to the advertised duties.')
            self.assertEqual(generate.generation_profile(), base)
            with self.assertRaisesRegex(generate.GenerateError, 'approval'):
                rule_chat.apply(receipt['proposal_id'], 'yes')
            with self.assertRaisesRegex(generate.GenerateError, 'approval'):
                rule_chat.apply(receipt['proposal_id'], receipt['challenge'], now=receipt['expires_at'] + 1)
            self.assertEqual(generate.generation_profile(), base)
            active = rule_chat.apply(receipt['proposal_id'], receipt['challenge'])
            self.assertEqual(active['rule'], receipt['rule'])
            profile = generate.generation_profile()
            self.assertEqual(profile['cv_facts'], base['cv_facts'])
            self.assertIn(active['rule'], generate.build_prompt({'full_text': TEXT}))
            with self.assertRaises(generate.GenerateError):
                rule_chat.apply(receipt['proposal_id'], receipt['challenge'])

    def test_stale_chat_proposal_cannot_overwrite_another_change(self):
        with patch.object(rule_chat, 'PENDING', self.profile.parent / 'proposals'):
            receipt = rule_chat.propose('Prefer plain verbs.')
            second = rule_chat.propose(None, masculine=True)
            rule_chat.apply(second['proposal_id'], second['challenge'])
            with self.assertRaisesRegex(generate.GenerateError, 'changed'):
                rule_chat.apply(receipt['proposal_id'], receipt['challenge'])
            self.assertEqual(generate.generation_profile()['application_preferences'], [generate.MASCULINE_TITLES])


class LayoutTest(unittest.TestCase):
    def test_revision_frame_keeps_before_after_and_the_next_action_visible(self):
        state = review_tui.Revision({'text': '# Bewerbung als Operations Lead_in', 'why': 'Names the advertised title.'}, 'de', 1)
        before = '\n'.join(line for line, _ in review_tui.frame(state, 100, 26))
        self.assertIn('Original block', before)
        self.assertIn('Suggested revision', before)
        self.assertIn('Type what you want changed', before)
        self.assertNotIn('Review profile changes', before)
        state.latest = {'text': 'Bewerbung als Operations Lead', 'reason': 'Matches the applicant.'}
        after = '\n'.join(line for line, _ in review_tui.frame(state, 100, 26))
        self.assertIn('Bewerbung als Operations Lead_in', after)
        self.assertIn('Bewerbung als Operations Lead ', after)
        self.assertIn('Tab Review profile changes', after)
        self.assertNotIn('/accept', after)

    def test_profile_review_has_a_separate_confirm_and_escape_cancels(self):
        state = review_tui.Revision({'text': 'Old'}, 'de', 1)
        base = {'cv_facts': [], 'application_preferences': []}
        proposed = {'cv_facts': [], 'application_preferences': ['Prefer plain verbs.']}
        state.results.put(('pro', (base, proposed), ''))
        state.poll()
        for width in (42, 80, 120):
            frame = review_tui.frame(state, width, 24)
            self.assertTrue(all(len(line) < width for line, _ in frame))
        with patch.object(review, 'save_profile') as save, patch.object(generate, 'generation_profile', return_value=proposed):
            state.key('\n')
            state.key('a')
            save.assert_not_called()
            state.key('\x1b')
            self.assertEqual(state.mode, 'proposal')
            save.assert_not_called()
            state.key('a')
            state.key('\n')
            save.assert_called_once_with(base, proposed)
            self.assertEqual(state.mode, 'saved')