"""Synthetic local policy authorities; no services, task content or models."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'web'))
import editorial_policy as policy


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class EditorialPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.folder = self.root / 'jobs/fixture'
        self.folder.mkdir(parents=True)
        self.state = self.root / 'web/state/jobs/fixture.json'
        self.job = {'id': 'fixture', 'source_sha256': 'a' * 64,
                    'options': {'editorial_policy': policy.FLUENCY}}
        write(self.state, self.job)

    def defaults(self):
        authorization = self.root / 'config/local_authorization.json'
        write(authorization, {'synthetic_authority': 'allow fluent paraphrasing'})
        value = {'schema': 'editorial_policy_defaults_v1', 'default_policy': policy.FLUENCY,
                 'decision_maker': 'proxy', 'ask_for_editorial_choices': False,
                 'meaning_deviation_allowed': True,
                 'authorization_ref': {'path': 'config/local_authorization.json',
                                       'sha256': sha(authorization)}}
        write(self.root / 'config/editorial_policy.json', value)
        return authorization, value

    def test_local_default_requires_unchanged_authority_and_does_not_migrate_old_job(self):
        authorization, _ = self.defaults()
        self.assertEqual(policy.default_policy(self.root), policy.FLUENCY)
        self.job['options'].clear()
        write(self.state, self.job)
        self.assertEqual(policy.policy_for_folder(self.folder), policy.STRICT)
        authorization.write_bytes(authorization.read_bytes() + b' ')
        with self.assertRaises(ValueError):
            policy.default_policy(self.root)

    def test_unknown_policy_and_wrong_review_identity_are_rejected(self):
        for value in (True, '', 'unregistered'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                policy.normalize(value)
        policy.check_record({}, policy.STRICT)
        policy.check_record(policy.fields(policy.FLUENCY), policy.FLUENCY)
        for record in ({}, {'editorial_policy': policy.FLUENCY},
                       {**policy.fields(policy.FLUENCY), 'editorial_policy_sha256': '0' * 64}):
            with self.subTest(record=record), self.assertRaises(ValueError):
                policy.check_record(record, policy.FLUENCY)

    def test_default_authority_rejects_traversal_symlinks_and_missing_user_choice(self):
        authorization, value = self.defaults()
        cases = []
        missing = copy.deepcopy(value); missing.pop('meaning_deviation_allowed'); cases.append(missing)
        escape = copy.deepcopy(value); escape['authorization_ref']['path'] = '../outside.json'; cases.append(escape)
        symlink = self.root / 'config/link.json'; symlink.symlink_to(authorization)
        linked = copy.deepcopy(value); linked['authorization_ref']['path'] = 'config/link.json'; cases.append(linked)
        for candidate in cases:
            write(self.root / 'config/editorial_policy.json', candidate)
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                policy.default_policy(self.root)

    def test_state_queue_holds_and_cancel_remain_effective(self):
        queue_path = self.root / 'web/state/tagesschau_daily_automation.json'
        for location in ('state', 'watch', 'intent', 'episode', 'projection'):
            job = copy.deepcopy(self.job)
            queue = {'watched': {}, 'daily_intents': {}, 'episodes': {}}
            if location == 'state':
                job['cancel_requested_at'] = 'synthetic cancellation'
            elif location == 'projection':
                queue['projection'] = {'active_decisions': [{'job_id': 'fixture'}]}
            else:
                field = {'watch': 'watched', 'intent': 'daily_intents', 'episode': 'episodes'}[location]
                queue[field]['fixture'] = {'job_id': 'fixture', 'status': 'user_hold'}
            write(self.state, job); write(queue_path, queue)
            with self.subTest(location=location), self.assertRaises(ValueError):
                policy.assert_unheld(self.folder)

    def test_episode_delegation_binds_parent_source_plan_clip_and_asr(self):
        plan = {'source_sha256': self.job['source_sha256'], 'episodes': [{'episode_index': 1}]}
        digest = hashlib.sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        child = self.root / ('jobs/fixture_e001_' + digest[:8]); child.mkdir()
        plan_path = self.folder / 'episode_plan.json'; write(plan_path, plan)
        clip = self.folder / 'episode_inputs/episode.mp4'; clip.parent.mkdir(); clip.write_bytes(b'synthetic clip identity')
        asr = self.folder / 'episode_asr/episode.json'; write(asr, {'synthetic': True})
        binding = {'schema': 'episode_editorial_policy_binding_v1', 'job_id': child.name,
                   'parent_id': 'fixture', 'episode_index': 1, 'editorial_policy': policy.FLUENCY,
                   'parent_source_sha256': self.job['source_sha256']}
        for name, path in (('episode_plan', plan_path), ('clip', clip), ('episode_asr', asr)):
            binding[name] = {'path': str(path.relative_to(self.root)), 'sha256': sha(path)}
        write(child / 'editorial_policy_binding.json', binding)
        write(child / 'source.json', {'input': str(clip)})
        self.assertEqual(policy.policy_for_folder(child), policy.FLUENCY)
        self.assertEqual(policy.policy_state_for_folder(child)['source_sha256'], sha(clip))
        for path in (plan_path, clip, asr):
            original = path.read_bytes(); path.write_bytes(original + b' ')
            with self.subTest(path=path), self.assertRaises(ValueError):
                policy.policy_for_folder(child)
            path.write_bytes(original)
        self.job['cancel_requested_at'] = 'synthetic cancellation'; write(self.state, self.job)
        with self.assertRaises(ValueError):
            policy.assert_unheld(child)


if __name__ == '__main__':
    unittest.main()
