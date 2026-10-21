"""Local website correction boundaries; no Proxy, network, GPU or ASR."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

WEB = Path(__file__).resolve().parents[1] / 'web'
sys.path.insert(0, str(WEB))
import website_review as review
import website_recovery as recovery


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.jid = 'web_sample'
        self.folder = self.root / 'jobs' / self.jid
        self.folder.mkdir(parents=True)
        self.video = self.root / 'inputs/web/source.mp4'
        self.video.parent.mkdir(parents=True)
        self.video.write_bytes(b'original immutable source')
        self.state_path = self.root / 'web/state/jobs' / (self.jid + '.json')
        self.state = {'id': self.jid, 'status': 'failed', 'source_language': 'de', 'source_sha256': recovery._sha(self.video),
            'failure_record': {'segment_ids': ['line0003']}}
        recovery._save(self.state_path, self.state)
        recovery._save(self.folder / 'source.json', {'input': str(self.video), 'duration_s': 8., 'audio_track': 0})
        self.rows = [{'segment_id': 'line%04d' % i, 'speaker_id': 'S01', 'start_s': (i - 1) * 2.,
            'end_s': (i - 1) * 2. + 1.5, 'text_de': 'Heute ist es warm.', 'text_source': 'Heute ist es warm.',
            'text_zh': '', 'source_ids': ['raw%04d' % i], 'translation_reviewed': False,
            'human_reviewed': False, 'source_language': 'de'} for i in range(1, 5)]
        self.original = {'schema_version': 1, 'segments': self.rows, 'human_reviewed': False, 'source_language': 'de'}
        recovery._save(self.folder / 'segments.plan.json', self.original)
        current = copy.deepcopy(self.original)
        for row in current['segments']:
            row.update(text_zh='今天天气暖和。', translation_reviewed=True)
        recovery._save(self.folder / 'segments.json', current)
        recovery._save(self.folder / 'translation/input.json', self.original)
        self.baseline = {name: recovery._sha(self.folder / name) for name in ('segments.plan.json', 'segments.json', 'translation/input.json', 'source.json')}
        self.source_sha = recovery._sha(self.video)

    def revision(self):
        proof = self.folder / 'revisions/source_review_v1/evidence/duplicate.json'
        recovery._save(proof, {'target_free_duplicate_check': 'actual-local-evidence'})
        derived = copy.deepcopy(self.original)
        derived['segments'] = derived['segments'][:3]
        dp = self.folder / 'revisions/source_review_v1/segments.plan.json'
        recovery._save(dp, derived)
        revision = {'schema': 'source_plan_revision_v1', 'original_plan_sha256': recovery._sha(self.folder / 'segments.plan.json'),
            'input_source_sha256': self.source_sha, 'revised_plan_sha256': recovery._sha(dp),
            'changes': [{'segment_id': 'line0004', 'action': 'exclude_asr_duplicate',
                'original_text_sha256': review._text_sha(self.rows[3]['text_de']), 'zero_duration_words': 4,
                'crop_asr_confirms_no_repetition': True,
                'evidence': [{'path': str(proof.relative_to(self.folder)), 'sha256': recovery._sha(proof)}]}],
            'unresolved': ['line0001', 'line0003'], 'blocking_unresolved': ['line0001', 'line0003'], 'human_reviewed': False}
        recovery._save(self.folder / 'source_plan_revision.json', revision)
        return revision

    def media_policies(self):
        old = {}
        for name, schema in [('tts/no_join_recovery.json', 'scoped_no_join_recovery_v1'),
                             ('tts/picture_holds.json', 'scoped_group_picture_holds_v1')]:
            value = {'schema': schema, 'job': self.jid, 'source_segments_sha256': recovery._sha(self.folder / 'segments.json'), 'groups': ['retained-old-permission']}
            recovery._save(self.folder / name, value)
            old[name] = (self.folder / name).read_bytes()
        recovery._save(self.folder / 'tts/media_recovery_journal.json', {'schema': 'bounded_media_recovery_journal_v1', 'entries': [{'token': 'already-consumed-budget'}]})
        recovery._save(self.folder / 'recovery/website_journal.json', {'schema': 'website_recovery_journal_v1', 'events': [{'id': 'already-consumed-proxy-budget'}]})
        budgets = {name: recovery._sha(self.folder / name) for name in ('tts/media_recovery_journal.json', 'recovery/website_journal.json')}
        return old, budgets

    def assert_originals(self):
        for name, expected in self.baseline.items():
            self.assertEqual(recovery._sha(self.folder / name), expected, name)
        self.assertEqual(recovery._sha(self.video), self.source_sha)

    def validate_derived(self):
        from pipeline import reviewed_source_plan
        return reviewed_source_plan(self.folder, self.folder / 'segments.plan.json', self.source_sha)[0]


class ReviewRowsTests(Fixture):
    def test_safe_current_failed_rows_only(self):
        self.revision()
        result = review.review_rows(self.root, self.jid)
        self.assertEqual([r['id'] for r in result], ['line0003', 'line0001'])
        self.assertEqual(set(result[0]), {'id', 'start_s', 'end_s', 'text_source', 'text_zh', 'uncertain'})
        self.assertEqual(result[0]['text_zh'], '今天天气暖和。')
        self.assertTrue(result[0]['uncertain'])
        self.assertNotIn(str(self.root), json.dumps(result))
        self.assert_originals()

    def test_unknown_duplicate_more_than_eight_ids_rejected(self):
        for ids in [['line9999'], ['line0001', 'line0001'], ['line0001'] * 9]:
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                review.review_rows(self.root, self.jid, ids)

    def test_running_queued_and_completed_cannot_read_or_write_review(self):
        for status in ('running', 'queued', 'completed'):
            self.state['status'] = status
            recovery._save(self.state_path, self.state)
            for action in (lambda: review.review_rows(self.root, self.jid),
                           lambda: review.save_corrections(self.root, self.jid, [{'id': 'line0001', 'source_text': 'Neu.'}])):
                with self.subTest(status=status), self.assertRaises(review.ReviewInputError):
                    action()
        self.assert_originals()

    def test_another_job_or_symlink_path_cannot_be_read(self):
        with self.assertRaises(ValueError):
            review.review_rows(self.root, '../web_sample')
        target = self.folder / 'source_plan_revision.json'
        target.symlink_to(self.state_path)
        with self.assertRaises(ValueError):
            review.review_rows(self.root, self.jid)
        target.unlink()
        outside = self.root / 'outside.json'
        recovery._save(outside, {'segments': []})
        (self.folder / 'segments.plan.json').unlink()
        (self.folder / 'segments.plan.json').symlink_to(outside)
        with self.assertRaises(ValueError):
            review.review_rows(self.root, self.jid)

    def test_source_sha_mismatch_prevents_review_and_corrections(self):
        self.video.write_bytes(b'changed source')
        with self.assertRaisesRegex(ValueError, '身份不符'):
            review.review_rows(self.root, self.jid)
        with self.assertRaisesRegex(ValueError, '身份不符'):
            review.save_corrections(self.root, self.jid, [{'id': 'line0001', 'preferred_translation': '今天天气暖和。'}])


class CorrectionTests(Fixture):
    def test_explicit_source_correction_preserves_original_times_speaker_and_duplicate_exclusion(self):
        previous = self.revision()
        result = review.save_corrections(self.root, self.jid, [{'id': 'line0001', 'source_text': 'Heute ist es kalt.',
            'preferred_translation': '今天天气寒冷。'}])
        self.assertEqual(result['source_corrected_ids'], ['line0001'])
        self.assertTrue(result['requires_proxy_review'])
        self.assertFalse(result['human_reviewed'])
        derived = self.validate_derived()
        self.assertEqual([r['segment_id'] for r in derived], ['line0001', 'line0002', 'line0003'])
        self.assertEqual(derived[0]['text_de'], 'Heute ist es kalt.')
        for key in set(self.rows[0]) - {'text_de', 'text_source'}:
            self.assertEqual(derived[0][key], self.rows[0][key])
        receipt = recovery._read(self.folder / 'source_plan_revision.json')
        self.assertEqual(receipt['changes'][0], previous['changes'][0])
        self.assertEqual(receipt['unresolved'], ['line0003'])
        self.assertEqual(receipt['blocking_unresolved'], ['line0003'])
        hint = recovery._read(self.folder / 'translation/source_review_hints.json')['rows']['line0001']
        self.assertEqual(hint['source_text_sha256'], review._text_sha(derived[0]['text_de']))
        self.assertEqual(hint['required_strings'], ['今天天气寒冷。'])
        self.assert_originals()

    def test_preferred_translation_only_is_hint_and_does_not_clear_source_uncertainty(self):
        previous = self.revision()
        receipt_before = recovery._sha(self.folder / 'source_plan_revision.json')
        result = review.save_corrections(self.root, self.jid, [{'id': 'line0003', 'preferred_translation': '今天很暖和。'}])
        self.assertEqual(result['source_corrected_ids'], [])
        self.assertEqual(recovery._sha(self.folder / 'source_plan_revision.json'), receipt_before)
        self.assertEqual(recovery._read(self.folder / 'source_plan_revision.json')['unresolved'], previous['unresolved'])
        self.assert_originals()

    def test_same_source_word_explicit_confirmation_can_clear_only_that_word(self):
        self.revision()
        review.save_corrections(self.root, self.jid, [{'id': 'line0003', 'source_text': self.rows[2]['text_de']}])
        current = recovery._read(self.folder / 'source_plan_revision.json')
        self.assertEqual(current['unresolved'], ['line0001'])
        self.assertEqual(self.validate_derived()[2]['text_de'], self.rows[2]['text_de'])
        self.assert_originals()

    def test_revising_existing_replacement_keeps_original_binding_and_prior_evidence(self):
        self.revision()
        review.save_corrections(self.root, self.jid, [{'id': 'line0001', 'source_text': 'Heute ist es kalt.'}])
        first = recovery._read(self.folder / 'source_plan_revision.json')
        previous_change = next(c for c in first['changes'] if c['segment_id'] == 'line0001')
        review.save_corrections(self.root, self.jid, [{'id': 'line0001', 'source_text': 'Heute ist es sonnig.'}])
        second = recovery._read(self.folder / 'source_plan_revision.json')
        current = next(c for c in second['changes'] if c['segment_id'] == 'line0001')
        self.assertEqual(current['original_text_sha256'], review._text_sha(self.rows[0]['text_de']))
        self.assertEqual(current['previous_evidence'], previous_change['evidence'])
        self.assertEqual(self.validate_derived()[0]['text_de'], 'Heute ist es sonnig.')
        journal = recovery._read(self.folder / review._JOURNAL)
        before = journal['events'][1]['transaction'][-1]['before']
        self.assertEqual(recovery._read(self.folder / before), first)
        self.assert_originals()

    def test_first_source_correction_creates_valid_derived_plan_without_editing_original(self):
        review.save_corrections(self.root, self.jid, [{'id': 'line0002', 'source_text': 'Heute ist es kalt.'}])
        self.assertEqual(len(self.validate_derived()), 4)
        self.assert_originals()

    def test_bad_invariants_unknown_id_extra_field_and_excluded_row_are_rejected(self):
        self.revision()
        inputs = [[{'id': 'line0001', 'preferred_translation': '有3辆汽车。'}],
            [{'id': 'line9999', 'source_text': 'Neu.'}], [{'id': 'line0004', 'source_text': 'Neu.'}],
            [{'id': 'line0001', 'source_text': 'Neu.', 'speaker_id': 'other'}],
            [{'id': 'line0001', 'source_text': ''}], [{'id': 'line0001', 'source_text': 'x' * 601}],
            [{'id': 'line0001', 'source_text': 'Neu.'}] * 9]
        for value in inputs:
            with self.subTest(value=value), self.assertRaises(ValueError):
                review.save_corrections(self.root, self.jid, value)
        self.assert_originals()

    def test_source_change_discards_stale_wording_constraint_not_other_rows(self):
        self.revision()
        recovery._save(self.folder / 'translation/source_review_hints.json', {'schema': 'source_review_hints_v1', 'rows': {
            'line0001': {'source_text_sha256': review._text_sha(self.rows[0]['text_de']), 'note': 'old advice', 'required_strings': ['原先的词']},
            'line0003': {'source_text_sha256': review._text_sha(self.rows[2]['text_de']), 'note': 'unrelated preserved', 'required_strings': ['暖和']}}})
        before = recovery._read(self.folder / 'translation/source_review_hints.json')['rows']['line0003']
        review.save_corrections(self.root, self.jid, [{'id': 'line0001', 'source_text': 'Heute ist es kalt.'}])
        hints = recovery._read(self.folder / 'translation/source_review_hints.json')['rows']
        self.assertEqual(hints['line0003'], before)
        self.assertEqual(hints['line0001']['required_strings'], [])
        self.assert_originals()


class PermissionArchiveTests(Fixture):
    def test_real_source_change_archives_both_permissions_without_resetting_budgets(self):
        self.revision()
        old, budgets = self.media_policies()
        result = review.save_corrections(self.root, self.jid, [{'id': 'line0001', 'source_text': 'Heute ist es kalt.'}])
        self.assertEqual(result['archived_media_permissions'], ['no_join_recovery', 'picture_holds'])
        self.assertFalse(result['media_recovery_budget_reset'])
        event = recovery._read(self.folder / review._JOURNAL)['events'][0]
        evidence = recovery._read(self.folder / event['evidence']['path'])
        self.assertEqual(len(evidence['archived_media_permissions']), 2)
        for name, data in old.items():
            self.assertFalse((self.folder / name).exists())
            item = next(item for item in event['transaction'] if item['target'] == name)
            self.assertEqual((self.folder / item['before']).read_bytes(), data)
        for name, digest in budgets.items():
            self.assertEqual(recovery._sha(self.folder / name), digest)
        self.validate_derived()
        self.assert_originals()

    def test_preferred_translation_and_same_source_confirmation_keep_permissions(self):
        self.revision()
        old, budgets = self.media_policies()
        review.save_corrections(self.root, self.jid, [{'id': 'line0003', 'preferred_translation': '今天很暖和。'}])
        review.save_corrections(self.root, self.jid, [{'id': 'line0001', 'source_text': self.rows[0]['text_de']}])
        for name, data in old.items():
            self.assertEqual((self.folder / name).read_bytes(), data)
        for name, digest in budgets.items():
            self.assertEqual(recovery._sha(self.folder / name), digest)
        self.assert_originals()

    def test_interruption_after_permission_removal_restores_both_original_permissions(self):
        self.revision()
        old, budgets = self.media_policies()
        atomic = recovery._atomic
        interrupted = []
        def injected(path, data):
            if path == self.folder / 'revisions/source_review_v1/segments.plan.json' and not interrupted:
                interrupted.append(True)
                raise KeyboardInterrupt('after permission deletions')
            return atomic(path, data)
        with patch.object(recovery, '_atomic', side_effect=injected):
            with self.assertRaises(KeyboardInterrupt):
                review.save_corrections(self.root, self.jid, [{'id': 'line0001', 'source_text': 'Heute ist es kalt.'}])
        for name, data in old.items():
            self.assertEqual((self.folder / name).read_bytes(), data)
        for name, digest in budgets.items():
            self.assertEqual(recovery._sha(self.folder / name), digest)
        self.assertEqual(self.validate_derived()[0]['text_de'], self.rows[0]['text_de'])
        self.assert_originals()

    def test_process_loss_after_permission_removal_finishes_on_next_read(self):
        self.revision()
        old, budgets = self.media_policies()
        def kill_after_deletes(root, folder, journal, event):
            for item in event['transaction']:
                if item['operation'] == 'delete':
                    (folder / item['target']).unlink()
            raise SystemExit('lost process after atomic deletions')
        with patch.object(review, '_apply', side_effect=kill_after_deletes):
            with self.assertRaises(SystemExit):
                review.save_corrections(self.root, self.jid, [{'id': 'line0001', 'source_text': 'Heute ist es kalt.'}])
        self.assertTrue(all(not (self.folder / name).exists() for name in old))
        rows = review.review_rows(self.root, self.jid, ['line0001'])
        self.assertEqual(rows[0]['text_source'], 'Heute ist es kalt.')
        self.assertTrue(all(not (self.folder / name).exists() for name in old))
        for name, digest in budgets.items():
            self.assertEqual(recovery._sha(self.folder / name), digest)
        self.assert_originals()


class TransactionTests(Fixture):
    def test_keyboard_interrupt_rolls_back_live_files_preserves_evidence_and_originals(self):
        self.revision()
        names = list(review._TARGETS)
        before = {name: (self.folder / name).read_bytes() if (self.folder / name).is_file() else None for name in names}
        atomic = recovery._atomic
        interrupted = []
        def injected(path, data):
            if path == self.folder / 'translation/source_review_hints.json' and not interrupted:
                interrupted.append(True)
                raise KeyboardInterrupt('simulated interruption')
            return atomic(path, data)
        with patch.object(recovery, '_atomic', side_effect=injected):
            with self.assertRaises(KeyboardInterrupt):
                review.save_corrections(self.root, self.jid, [{'id': 'line0001', 'source_text': 'Heute ist es kalt.'}])
        for name, expected in before.items():
            path = self.folder / name
            self.assertEqual(path.read_bytes() if path.is_file() else None, expected)
        journal = recovery._read(self.folder / review._JOURNAL)
        self.assertEqual(journal['events'][0]['status'], 'rolled_back')
        self.assertTrue((self.folder / journal['events'][0]['evidence']['path']).is_file())
        self.validate_derived()
        self.assert_originals()

    def test_state_cancellation_during_prepare_is_cas_conflict(self):
        self.revision()
        atomic = recovery._atomic
        changed = []
        def injected(path, data):
            result = atomic(path, data)
            if 'after_' in path.name and not changed:
                changed.append(True)
                state = recovery._read(self.state_path)
                state.update(status='canceled', updated_at='changed')
                atomic(self.state_path, recovery._bytes(state))
            return result
        with patch.object(recovery, '_atomic', side_effect=injected):
            with self.assertRaises(review.ReviewConflictError):
                review.save_corrections(self.root, self.jid, [{'id': 'line0001', 'source_text': 'Heute ist es kalt.'}])
        self.assertEqual(self.validate_derived()[0]['text_de'], self.rows[0]['text_de'])
        self.assert_originals()

    def test_state_change_during_last_live_write_rolls_back_whole_transaction(self):
        self.revision()
        before = (self.folder / 'source_plan_revision.json').read_bytes()
        atomic = recovery._atomic
        changed = []
        def injected(path, data):
            result = atomic(path, data)
            if path == self.folder / 'source_plan_revision.json' and not changed:
                changed.append(True)
                state = recovery._read(self.state_path)
                state['status'] = 'running'
                atomic(self.state_path, recovery._bytes(state))
            return result
        with patch.object(recovery, '_atomic', side_effect=injected):
            with self.assertRaises(ValueError):
                review.save_corrections(self.root, self.jid, [{'id': 'line0001', 'source_text': 'Heute ist es kalt.'}])
        self.assertEqual((self.folder / 'source_plan_revision.json').read_bytes(), before)
        self.assertEqual(self.validate_derived()[0]['text_de'], self.rows[0]['text_de'])
        self.assert_originals()

    def test_source_changed_during_prepare_never_gets_overwritten(self):
        self.revision()
        import proxy_client
        invariant = proxy_client.invariants
        def injected(*args, **kwargs):
            path = self.folder / 'segments.json'
            data = recovery._read(path)
            data['concurrent_edit'] = True
            recovery._save(path, data)
            return invariant(*args, **kwargs)
        with patch.object(proxy_client, 'invariants', side_effect=injected):
            with self.assertRaises(review.ReviewConflictError):
                review.save_corrections(self.root, self.jid, [{'id': 'line0001', 'preferred_translation': '今天很暖和。'}])
        self.assertTrue(recovery._read(self.folder / 'segments.json')['concurrent_edit'])
        self.assertFalse((self.folder / 'translation/source_review_hints.json').exists())

    def test_hard_process_loss_after_prepare_is_completed_idempotently_on_next_read(self):
        self.revision()
        with patch.object(review, '_apply', side_effect=SystemExit('simulated process loss before commit')):
            with self.assertRaises(SystemExit):
                review.save_corrections(self.root, self.jid, [{'id': 'line0001', 'source_text': 'Heute ist es kalt.'}])
        journal = recovery._read(self.folder / review._JOURNAL)
        self.assertEqual(journal['events'][0]['status'], 'applying')
        self.assertEqual(self.validate_derived()[0]['text_de'], self.rows[0]['text_de'])
        result = review.review_rows(self.root, self.jid, ['line0001'])
        self.assertEqual(result[0]['text_source'], 'Heute ist es kalt.')
        self.assertEqual(recovery._read(self.folder / review._JOURNAL)['events'][0]['status'], 'saved')
        review.review_rows(self.root, self.jid, ['line0001'])
        self.assertEqual(len(recovery._read(self.folder / review._JOURNAL)['events']), 1)
        self.assert_originals()


class ResumePendingTests(Fixture):
    def prepare_pending(self, permissions=False):
        self.revision()
        if permissions:
            self.media_policies()
        with patch.object(review, '_apply', side_effect=SystemExit('lost process before commit')):
            with self.assertRaises(SystemExit):
                review.save_corrections(self.root, self.jid, [{'id': 'line0001', 'source_text': 'Heute ist es kalt.'}])

    def test_pending_transaction_rolls_forward_once_and_protects_budgets(self):
        self.prepare_pending(permissions=True)
        budgets = {name: recovery._sha(self.folder / name) for name in
            ('tts/media_recovery_journal.json', 'recovery/website_journal.json')}
        result = review.resume_pending(self.root, self.jid)
        self.assertTrue(result['resumed'])
        self.assertEqual(result['transactions'], 1)
        self.assertFalse(result['media_recovery_budget_reset'])
        self.assertEqual(self.validate_derived()[0]['text_de'], 'Heute ist es kalt.')
        self.assertFalse((self.folder / 'tts/no_join_recovery.json').exists())
        self.assertFalse((self.folder / 'tts/picture_holds.json').exists())
        for name, digest in budgets.items():
            self.assertEqual(recovery._sha(self.folder / name), digest)
        journal_sha = recovery._sha(self.folder / review._JOURNAL)
        self.assertFalse(review.resume_pending(self.root, self.jid)['resumed'])
        self.assertEqual(recovery._sha(self.folder / review._JOURNAL), journal_sha)
        self.assert_originals()

    def test_no_pending_is_noop_with_missing_source_plan_and_source_hash(self):
        self.state.pop('source_sha256')
        recovery._save(self.state_path, self.state)
        (self.folder / 'source.json').unlink()
        (self.folder / 'segments.plan.json').unlink()
        self.video.unlink()
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*'))
        result = review.resume_pending(self.root, self.jid)
        self.assertFalse(result['resumed'])
        self.assertEqual(result['transactions'], 0)
        self.assertEqual(sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*')), before)

    def test_no_job_directory_or_journal_is_noop_if_state_is_inactive(self):
        import shutil
        shutil.rmtree(self.folder)
        self.state.pop('source_sha256')
        recovery._save(self.state_path, self.state)
        self.assertFalse(review.resume_pending(self.root, self.jid)['resumed'])
        self.assertFalse(self.folder.exists())

    def test_no_pending_still_rejects_active_and_completed_states(self):
        for status in ('running', 'queued', 'completed'):
            self.state.update(status=status)
            self.state.pop('source_sha256', None)
            recovery._save(self.state_path, self.state)
            with self.subTest(status=status), self.assertRaises(review.ReviewInputError):
                review.resume_pending(self.root, self.jid)
        self.assertFalse((self.folder / review._JOURNAL).exists())

    def test_changed_budget_is_never_reset_or_rolled_back(self):
        self.prepare_pending(permissions=True)
        budget = self.folder / 'tts/media_recovery_journal.json'
        value = recovery._read(budget)
        value['entries'].append({'token': 'newly-consumed-budget'})
        recovery._save(budget, value)
        changed_sha = recovery._sha(budget)
        with self.assertRaises(review.ReviewConflictError):
            review.resume_pending(self.root, self.jid)
        self.assertEqual(recovery._sha(budget), changed_sha)
        self.assertEqual(self.validate_derived()[0]['text_de'], self.rows[0]['text_de'])
        self.assert_originals()

    def test_pending_requires_original_state_cas_and_valid_source(self):
        self.prepare_pending()
        self.state['status'] = 'canceled'
        recovery._save(self.state_path, self.state)
        with self.assertRaises(review.ReviewConflictError):
            review.resume_pending(self.root, self.jid)
        self.assertEqual(self.validate_derived()[0]['text_de'], self.rows[0]['text_de'])
        self.assert_originals()

    def test_missing_budget_protection_does_not_apply_old_transaction(self):
        self.prepare_pending()
        path = self.folder / review._JOURNAL
        journal = recovery._read(path)
        journal['events'][0]['protected_files'].pop('tts/media_recovery_journal.json')
        recovery._save(path, journal)
        with self.assertRaisesRegex(ValueError, '预算保护'):
            review.resume_pending(self.root, self.jid)
        self.assertEqual(recovery._read(path)['events'][0]['status'], 'applying')
        self.assertEqual(self.validate_derived()[0]['text_de'], self.rows[0]['text_de'])
        self.assert_originals()


class AudioTests(Fixture):
    @unittest.skipUnless(Path('/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg').is_file(), 'No local ffmpeg')
    def test_real_local_cpu_preview_has_bound_hash_and_second_call_reuses(self):
        ffmpeg = '/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg'
        subprocess.run([ffmpeg, '-v', 'error', '-nostdin', '-f', 'lavfi', '-i', 'color=c=black:s=160x90:r=25:d=8',
            '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100:duration=8', '-c:v', 'libx264', '-threads', '1',
            '-c:a', 'aac', '-shortest', '-y', str(self.video)], check=True, capture_output=True)
        self.source_sha = recovery._sha(self.video)
        self.state['source_sha256'] = self.source_sha
        recovery._save(self.state_path, self.state)
        first = review.audio_preview(self.root, self.jid, 'line0002')
        self.assertTrue(first.is_file())
        before = first.stat().st_mtime_ns
        with patch.object(review.subprocess, 'run', side_effect=AssertionError('cache should avoid ffmpeg')):
            second = review.audio_preview(self.root, self.jid, 'line0002')
        self.assertEqual(first, second)
        self.assertEqual(first.stat().st_mtime_ns, before)
        meta = recovery._read(first.with_suffix('.json'))
        self.assertEqual(meta['binding']['start_s'], 1.)
        self.assertEqual(meta['binding']['end_s'], 4.5)
        self.assertFalse(meta['audio_sent_to_proxy'])
        self.assertEqual(meta['audio_sha256'], recovery._sha(first))

    def test_timeout_removes_partial_media_and_returns_precise_error(self):
        def injected(command, **kwargs):
            Path(command[-1]).write_bytes(b'partial')
            raise subprocess.TimeoutExpired(command, kwargs['timeout'])
        with patch.object(review.subprocess, 'run', side_effect=injected):
            with self.assertRaisesRegex(ValueError, '30秒'):
                review.audio_preview(self.root, self.jid, 'line0001')
        self.assertEqual(list((self.folder / 'review_audio').glob('*.mp3')), [])
        self.assert_originals()


if __name__ == '__main__':
    unittest.main(verbosity=2)
