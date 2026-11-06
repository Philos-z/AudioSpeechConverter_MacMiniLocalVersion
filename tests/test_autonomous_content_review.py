"""Meaningful failure/restart coverage; no real Proxy, models or production data."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'web'))
import autonomous_content_review as review


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False))


class FixtureClient:
    model = 'offline-independent-review'
    config = {'base_url': 'http://127.0.0.1:0/v1', 'api_key': 'SECRET_SENTINEL_NEVER_SAVE'}

    def __init__(self, owner):
        self.owner = owner
        self.records = []

    def json(self, system, data):
        with self.owner.lock:
            self.owner.calls.append(copy.deepcopy(data))
        response = {'rows': [{'id': row['id'], 'approved': True, 'issues': []} for row in data['items']]}
        if self.owner.mutate:
            response = self.owner.mutate(response, data)
        self.records.append({'model': self.model, 'text_only': True, 'elapsed_s': .001, 'usage': {}})
        return response


class AutonomousContentReviewTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.folder = self.root / 'jobs/web_fixture'
        self.folder.mkdir(parents=True)
        self.video = self.root / 'source.mp4'
        self.video.write_bytes(b'actual source fixture')
        write(self.folder / 'source.json', {'input': str(self.video)})
        write(self.root / 'web/state/jobs/web_fixture.json',
              {'source_sha256': hashlib.sha256(self.video.read_bytes()).hexdigest()})
        self.rows = [{'segment_id': 'line%04d' % (i+1), 'speaker_id': 'S01',
                      'start_s': i*3., 'end_s': i*3.+2.,
                      'text_de': 'Es sind 3 Menschen.', 'text_source': 'Es sind 3 Menschen.',
                      'source_language': 'de', 'text_zh': '有3人。',
                      'translation_review_method': 'proxy_second_pass_and_numeric_checks'}
                     for i in range(19)]
        self.plan()
        self.calls = []
        self.mutate = None
        self.lock = threading.Lock()
        self.factory = lambda root: FixtureClient(self)
        self.adoptions = {}
        self.loader = patch('user_context_adoption.load_adoptions', side_effect=lambda *args: copy.deepcopy(self.adoptions))
        self.loader.start()
        self.addCleanup(self.loader.stop)
        # An existing rejected record is immutable and not confused with this pass.
        write(self.folder / 'translation/independent_content_review.json',
              {'schema': 'old-rejected-independent-fixture', 'rows': {'line0001': {'approved': False}}})
        write(self.folder / 'recovery/website_journal.json', {
            'schema':'website_recovery_journal_v1','job':self.folder.name,
            'max_actions':6,'max_per_input':2,'events':[
                {'id':f'{i+1:064x}','input_fingerprint':f'{i+20:064x}',
                 'action':'translation','status':'failed','proxy_calls':2,
                 'segment_ids':['line0001']} for i in range(6)]})
        self.protected = {str(p): p.read_bytes() for p in self.folder.rglob('*.json')}

    def plan(self):
        write(self.folder / 'segments.plan.json', {'segments': self.rows})
        write(self.folder / 'translation/input.json', {'segments': self.rows})

    def run_review(self, **kwargs):
        return review.ensure_review(self.root, self.folder, self.rows,
                                    client_factory=self.factory, **kwargs)

    def assert_protected(self):
        for name, raw in self.protected.items():
            self.assertEqual(Path(name).read_bytes(), raw, name)

    def issue(self, response, request, kind='translation_mismatch'):
        response['rows'][0] = {'id': request['items'][0]['id'], 'approved': False,
            'issues': [{'type': kind, 'detail': '译文的主体数量含义与源行存在明确矛盾。',
                        'source_quote': '3 Menschen', 'translation_quote': '3人', 'related_ids': []}]}
        return response

    def test_full_whole_script_context_actual_calls_and_idempotent_resume(self):
        result = self.run_review()
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['covered_rows'], 19)
        self.assertEqual(len(self.calls), 2)
        self.assertCountEqual([len(call['items']) for call in self.calls], [16,3])
        self.assertTrue(all(len(call['whole_script']) == 19 for call in self.calls))
        tail_batch=next(call for call in self.calls if call['items'][0]['id']=='line0017')
        self.assertEqual(tail_batch['previous_context'][-1]['id'], 'line0016')
        report = review.verify_report(self.root, self.folder, self.rows, result, client_factory=self.factory)
        self.assertEqual(report, result)
        again = self.run_review()
        self.assertEqual(again, result)
        self.assertEqual(len(self.calls), 2)
        self.assert_protected()
        self.assertFalse(result['old_rejections_closed'])
        for path in (self.folder / review.DIRECTORY).rglob('*.json'):
            self.assertNotIn('SECRET_SENTINEL', path.read_text())

    def test_genuine_new_rejection_has_structured_separate_recovery_report(self):
        self.mutate = self.issue
        result = self.run_review()
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(set(result['findings']), {'line0001','line0017'})
        pending = json.loads((self.folder / review.RECOVERY_REPORT).read_text())
        self.assertEqual(pending['schema'], 'independent_content_review_v1')
        self.assertEqual(pending['review_method'], 'autonomous_proxy_source_semantics')
        self.assertFalse(pending['rows']['line0001']['approved'])
        review.verify_report(self.root, self.folder, self.rows, result, recovery_report=pending, client_factory=self.factory)
        self.assert_protected()

    def test_invalid_coverage_decisions_and_ungrounded_issues_never_pass(self):
        mutations = ('missing','duplicate','unknown','approved_with_issue','false_without_issue',
                     'truthy_approval','rewritten_target','false_source_quote','false_target_quote',
                     'unknown_issue_type','unknown_related','missing_field','extra_field')
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                temp = tempfile.TemporaryDirectory()
                self.addCleanup(temp.cleanup)
                original_folder = self.folder
                self.folder = self.root / ('jobs/web_' + mutation)
                self.folder.mkdir()
                write(self.folder / 'source.json', {'input': str(self.video)})
                self.plan()
                def alter(response, request):
                    if mutation == 'missing': response['rows'].pop()
                    elif mutation == 'duplicate': response['rows'][-1] = copy.deepcopy(response['rows'][0])
                    elif mutation == 'unknown': response['rows'][0]['id'] = 'line9999'
                    elif mutation == 'approved_with_issue':
                        response = self.issue(response, request); response['rows'][0]['approved'] = True
                    elif mutation == 'false_without_issue': response['rows'][0]['approved'] = False
                    elif mutation == 'truthy_approval': response['rows'][0]['approved'] = 1
                    elif mutation == 'rewritten_target': response['rows'][0]['text_zh'] = '乱改'
                    elif mutation == 'missing_field': response['rows'][0].pop('issues')
                    elif mutation == 'extra_field': response['unexpected'] = True
                    else:
                        response = self.issue(response, request)
                        issue = response['rows'][0]['issues'][0]
                        if mutation == 'false_source_quote': issue['source_quote'] = 'not in source'
                        elif mutation == 'false_target_quote': issue['translation_quote'] = '不存在中文'
                        elif mutation == 'unknown_issue_type': issue['type'] = 'needs_style'
                        else: issue['related_ids'] = ['line9999']
                    return response
                self.mutate = alter
                with self.assertRaises(review.ContentReviewIncomplete): self.run_review()
                self.assertFalse((self.folder / review.REPORT).exists())
                events = json.loads((self.folder / review.JOURNAL).read_text())['events']
                self.assertTrue(any(event['status'] == 'invalid_response' for event in events))
                self.folder = original_folder

    def test_failed_calls_spend_budget_and_never_create_hidden_retry(self):
        def fail(response, request): raise TimeoutError('secret-bearing transport message')
        self.mutate = fail
        with self.assertRaises(review.ContentReviewIncomplete): self.run_review()
        self.assertEqual(len(self.calls), 2)
        with self.assertRaises(review.ContentReviewIncomplete): self.run_review()
        self.assertEqual(len(self.calls), 4)
        with self.assertRaises(review.ContentReviewBudgetExhausted): self.run_review()
        self.assertEqual(len(self.calls), 4)
        self.assert_protected()
        self.assertNotIn('secret-bearing', (self.folder / review.JOURNAL).read_text())

    def transport_failure(self, status=503):
        from proxy_client import ProxyTransportError
        raise ProxyTransportError('offline actual typed fixture', {
            'schema':'bounded_stage_failure_v1','origin':'proxy_transport','stage':'translation',
            'category':'http_response','http_status':status})

    def single_batch(self):
        self.rows = self.rows[:1]
        self.plan()

    def test_new_task_third_real_transport_attempt_can_pass_without_fourth_call(self):
        self.single_batch()
        self.mutate = lambda response, request: self.transport_failure(500 if len(self.calls)==1 else 503)
        for _ in range(2):
            with self.assertRaises(review.ContentReviewIncomplete):self.run_review()
        before = json.loads((self.folder/review.JOURNAL).read_text())
        self.assertEqual(before['max_per_request'],3)
        self.assertEqual(len(before['events']),2)
        self.mutate = None
        report = self.run_review()
        self.assertEqual(report['status'],'passed')
        self.assertEqual(len(self.calls),3)
        self.assertEqual(self.run_review(),report)
        self.assertEqual(len(self.calls),3)
        self.assertEqual(json.loads((self.folder/review.JOURNAL).read_text())['events'][:2],before['events'])

    def test_new_task_three_transport_failures_are_durably_maximum(self):
        self.single_batch()
        self.mutate = lambda response, request: self.transport_failure()
        for _ in range(3):
            with self.assertRaises(review.ContentReviewIncomplete):self.run_review()
        frozen = (self.folder/review.JOURNAL).read_bytes()
        with self.assertRaises(review.ContentReviewBudgetExhausted):self.run_review()
        self.assertEqual(len(self.calls),3)
        self.assertEqual((self.folder/review.JOURNAL).read_bytes(),frozen)

    def test_total_ninety_six_call_cap_is_not_expanded_by_third_attempt_policy(self):
        self.single_batch()
        journal=review._journal(self.folder)
        journal['events']=[{'id':f'{index+1:032x}','request_sha256':f'{index+1:064x}',
            'attempt_identity_sha256':f'{index+1000:064x}',
            'status':'interrupted','started_at_unix':1.,'finished_at_unix':2.}
            for index in range(96)]
        review.recovery._save(self.folder/review.JOURNAL,journal)
        with self.assertRaisesRegex(review.ContentReviewBudgetExhausted,'持久调用预算不足'):self.run_review()
        self.assertEqual(self.calls,[])
        self.assertEqual(len(review._journal(self.folder)['events']),96)

    def test_interrupted_reservation_spends_call_without_third_transport_authority(self):
        self.single_batch()
        self.mutate=lambda response,request:self.transport_failure()
        with self.assertRaises(review.ContentReviewIncomplete):self.run_review()
        journal=review._journal(self.folder)
        first=journal['events'][0]
        journal['events'].append({'id':'b'*32,'request_sha256':first['request_sha256'],
            'attempt_identity_sha256':first['attempt_identity_sha256'],
            'status':'reserved','started_at_unix':1.})
        review.recovery._save(self.folder/review.JOURNAL,journal)
        self.mutate=None
        with self.assertRaises(review.ContentReviewBudgetExhausted):self.run_review()
        self.assertEqual(len(self.calls),1)
        self.assertEqual(review._journal(self.folder)['events'][-1]['status'],'interrupted')

    def test_legacy_two_cap_survives_upgrade_and_code_identity_change(self):
        self.single_batch()
        journal = review._journal(self.folder)
        journal['max_per_request']=2
        review.recovery._save(self.folder/review.JOURNAL,journal)
        old_code={key:'a'*64 for key in review._code_identity()}
        self.mutate = lambda response, request: self.transport_failure()
        with patch.object(review,'_code_identity',return_value=old_code):
            for _ in range(2):
                with self.assertRaises(review.ContentReviewIncomplete):self.run_review()
        # Persist the true legacy format, which predates the separate budget key.
        journal=json.loads((self.folder/review.JOURNAL).read_text())
        for event in journal['events']:event.pop('attempt_identity_sha256')
        review.recovery._save(self.folder/review.JOURNAL,journal)
        frozen=(self.folder/review.JOURNAL).read_bytes()
        self.mutate=None
        with self.assertRaises(review.ContentReviewBudgetExhausted):self.run_review()
        self.assertEqual(len(self.calls),2)
        self.assertEqual((self.folder/review.JOURNAL).read_bytes(),frozen)
        self.assertEqual(review._journal(self.folder)['max_per_request'],2)
        # Editing a persisted cap does not enlarge an already bound old journal.
        journal['max_per_request']=3
        write(self.folder/review.JOURNAL,journal)
        with self.assertRaisesRegex(review.ContentReviewError,'真实完整请求'):self.run_review()
        self.assertEqual(len(self.calls),2)

    def test_third_attempt_requires_both_actual_retryable_transport_failures(self):
        for first,second in (('transport','malformed'),('malformed','transport'),
                             ('transport','claimed'),('transport','hard_http')):
            with self.subTest(first=first,second=second):
                self.folder=self.root/('jobs/web_third_'+first+'_'+second)
                self.folder.mkdir()
                write(self.folder/'source.json',{'input':str(self.video)})
                self.single_batch();self.calls=[]
                def mutate(response, request):
                    kind=(first,second)[len(self.calls)-1]
                    if kind=='transport':self.transport_failure()
                    if kind=='hard_http':self.transport_failure(401)
                    if kind=='claimed':raise RuntimeError('ProxyTransportError HTTP 503')
                    response['rows'].pop()
                    return response
                self.mutate=mutate
                for _ in range(2):
                    with self.assertRaises(review.ContentReviewIncomplete):self.run_review()
                self.mutate=None
                with self.assertRaises(review.ContentReviewBudgetExhausted):self.run_review()
                self.assertEqual(len(self.calls),2)
                self.assertFalse((self.folder/review.REPORT).exists())

    def test_concurrent_transport_plus_invalid_response_cannot_gain_network_retry(self):
        import proxy_client
        metadata={'schema':'bounded_stage_failure_v1','origin':'proxy_transport','stage':'translation',
                  'category':'http_response','http_status':503}
        def mixed(response, request):
            if request['items'][0]['id']=='line0001':
                raise proxy_client.ProxyTransportError('actual bounded transport',metadata)
            response['rows'].pop()
            return response
        self.mutate=mixed
        with patch.object(proxy_client,'emit_transport_failure') as emit:
            with self.assertRaises(review.ContentReviewIncomplete):self.run_review()
        self.assertEqual(len(self.calls),2)
        self.assertFalse(any(call.kwargs.get('event')=='proxy_stage_failure' for call in emit.call_args_list))
        events=json.loads((self.folder/review.JOURNAL).read_text())['events']
        self.assertEqual({event['status'] for event in events},{'failed','invalid_response'})
        self.assertFalse((self.folder/review.REPORT).exists())

    def test_concurrent_transport_plus_text_only_claim_is_not_confirmed_transport(self):
        import proxy_client
        metadata={'schema':'bounded_stage_failure_v1','origin':'proxy_transport','stage':'translation',
                  'category':'transport_timeout','exception_type':'TimeoutError'}
        def mixed(response, request):
            if request['items'][0]['id']=='line0001':
                raise proxy_client.ProxyTransportError('actual timeout',metadata)
            raise RuntimeError('HTTP 503 ProxyTransportError transport_timeout fake source text')
        self.mutate=mixed
        with patch.object(proxy_client,'emit_transport_failure') as emit:
            with self.assertRaises(review.ContentReviewIncomplete):self.run_review()
        self.assertEqual(len(self.calls),2)
        self.assertFalse(any(call.kwargs.get('event')=='proxy_stage_failure' for call in emit.call_args_list))
        events=json.loads((self.folder/review.JOURNAL).read_text())['events']
        self.assertEqual(len(events),2)
        self.assertEqual(sum('failure_metadata' in event for event in events),1)

    def test_partial_completed_wave_is_kept_and_only_failed_batch_retried(self):
        def partial(response, request):
            if request['items'][0]['id'] == 'line0017': response['rows'].pop()
            return response
        self.mutate = partial
        with self.assertRaises(review.ContentReviewIncomplete): self.run_review()
        self.mutate = None
        result = self.run_review()
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.calls[-1]['items'][0]['id'], 'line0017')

    def test_cancel_before_call_and_after_call_never_publishes_pass(self):
        with self.assertRaises(review.ContentReviewIncomplete): self.run_review(cancel=lambda: True)
        self.assertEqual(self.calls, [])
        def cancel_after(response, request):
            self.canceled = True
            return response
        self.canceled = False
        self.mutate = cancel_after
        with self.assertRaises(review.ContentReviewIncomplete): self.run_review(cancel=lambda: self.canceled)
        self.assertFalse((self.folder / review.REPORT).exists())
        self.assertEqual(len(self.calls), 2)
        self.mutate = None
        self.assertEqual(self.run_review()['status'], 'passed')
        self.assertEqual(len(self.calls), 2)

    def test_report_source_target_context_code_and_diagnostic_tampering_rejected(self):
        result = self.run_review()
        for mutation in ('target','source','speaker','source_video','context','diagnostic','report'):
            with self.subTest(mutation=mutation):
                rows = copy.deepcopy(self.rows)
                report = copy.deepcopy(result)
                saved_video = self.video.read_bytes()
                diagnostic = self.folder / result['diagnostics'][0]['path']
                saved_diag = diagnostic.read_bytes()
                if mutation == 'target': rows[0]['text_zh'] += '新内容'
                elif mutation == 'source': rows[0]['text_de'] += 'Neu'
                elif mutation == 'speaker': rows[0]['speaker_id'] = 'S02'
                elif mutation == 'source_video': self.video.write_bytes(b'different')
                elif mutation == 'context': rows[-1]['text_zh'] = '上下文改变'
                elif mutation == 'diagnostic': diagnostic.write_text('{}')
                else: report['decisions']['line0001']['approved'] = False
                with self.assertRaises((review.ContentReviewError, ValueError, RuntimeError)):
                    review.verify_report(self.root, self.folder, rows, report, client_factory=self.factory)
                self.video.write_bytes(saved_video)
                diagnostic.write_bytes(saved_diag)
        with patch.object(review, '_code_identity', return_value={'different.py': 'f'*64}):
            with self.assertRaises(review.ContentReviewError):
                review.verify_report(self.root, self.folder, self.rows, result, client_factory=self.factory)

    def test_all_active_rows_required_and_bad_source_rejected_before_calls(self):
        with self.assertRaises(review.ContentReviewError):
            review.ensure_review(self.root, self.folder, self.rows[:-1], client_factory=self.factory)
        self.assertFalse(self.calls)
        duplicate = copy.deepcopy(self.rows); duplicate[-1] = duplicate[0]
        with self.assertRaises(review.ContentReviewError):
            review.ensure_review(self.root, self.folder, duplicate, client_factory=self.factory)
        self.assertFalse(self.calls)

    def test_user_choice_separate_consistency_classification_no_fake_approval(self):
        self.adoptions = {'line0002': {'text_zh': self.rows[1]['text_zh'],
            'decision_ref': {'path': 'jobs/web_fixture/user-decision.json', 'sha256': 'a'*64},
            'proxy_approved': False, 'human_reviewed': False, 'source_words_verified': False}}
        self.rows[1].update(translation_review_method='user_contextual_translation_adoption',
            translation_model=None, decision_ref=self.adoptions['line0002']['decision_ref'],
            proxy_approved=False, human_reviewed=False, source_words_verified=False, human_listened=False)
        result = self.run_review()
        self.assertEqual(result['user_decision_consistency_checked_rows'], 1)
        self.assertEqual(result['machine_content_checked_rows'], 18)
        self.assertFalse(result['user_adoptions_reapproved'])
        item = next(item for call in self.calls for item in call['items'] if item['id']=='line0002')
        self.assertEqual(item['authority'], 'user_context_adoption')
        self.assertFalse(item['user_decision']['proxy_approved'])
        self.assert_protected()

    def test_restart_reserved_attempt_remains_spent(self):
        client = self.factory(self.root)
        snapshot = review._snapshot(self.root,self.folder,self.rows,review._client_identity(client))
        request = review._requests(snapshot)[0]
        journal = review._journal(self.folder)
        journal['events'].append({'id':'a'*32,'request_sha256':review._fp(request),
                                  'status':'reserved','started_at_unix':1.})
        write(self.folder / review.JOURNAL, journal)
        self.assertEqual(self.run_review()['status'], 'passed')
        events = json.loads((self.folder / review.JOURNAL).read_text())['events']
        self.assertEqual(events[0]['status'], 'interrupted')
        self.assertEqual(len(events), 3)

    def test_same_row_negative_survives_other_row_edit_and_later_pass(self):
        def first_only(response, request):
            return self.issue(response, request) if request['items'][0]['id'] == 'line0001' else response
        self.mutate = first_only
        first = self.run_review()
        self.assertEqual(set(first['findings']), {'line0001'})
        self.mutate = None
        self.rows[-1]['text_zh'] = '这里有3人。'
        self.plan()
        second = self.run_review()
        self.assertTrue(second['decisions']['line0001']['approved'])
        self.assertEqual(second['status'], 'rejected')
        self.assertFalse(second['findings']['line0001']['approved'])
        self.assertTrue(second['findings']['line0001']['current_review_approved'])
        self.assertTrue(second['historical_negative_diagnostics'])
        review.verify_report(self.root, self.folder, self.rows, second, client_factory=self.factory)
        # A substantive edit plus an actual fresh independent pass can close it.
        self.rows[0]['text_zh'] = '有3个人。'
        self.plan()
        third = self.run_review()
        self.assertEqual(third['status'], 'passed')
        self.assertFalse(third['findings'])
        self.assertTrue((self.folder / review.DIRECTORY / 'history' /
                         (first['snapshot_sha256'] + '.json')).is_file())

    def test_current_configuration_and_timestamps_cannot_reuse_approval(self):
        result = self.run_review()
        with patch.object(FixtureClient, 'model', 'changed-model'):
            with self.assertRaisesRegex(review.ContentReviewError, '模型配置'):
                review.verify_report(self.root, self.folder, self.rows, result, client_factory=self.factory)
        journal_path = self.folder / review.JOURNAL
        value = json.loads(journal_path.read_text())
        value['events'][0]['finished_at_unix'] = float('nan')
        write(journal_path, value)
        with self.assertRaises(review.ContentReviewError):
            self.run_review()

    def test_changing_chinese_cannot_close_uncertain_source_words(self):
        def uncertain(response, request):
            return self.issue(response, request, 'source_uncertain') if request['items'][0]['id'] == 'line0001' else response
        self.mutate = uncertain
        self.run_review()
        self.mutate = None
        self.rows[0]['text_zh'] = '有3个人。'
        self.plan()
        actual = self.run_review()
        self.assertEqual(actual['status'], 'rejected')
        self.assertEqual(actual['findings']['line0001']['typed_issues'][0]['type'], 'source_uncertain')

    def test_verified_later_source_resolution_closes_only_source_uncertainty(self):
        import translation_recovery
        def first_only(response, request):
            if request['items'][0]['id'] == 'line0001':
                response = self.issue(response, request, 'source_uncertain')
                response['rows'][0]['issues'].append({
                    'type':'translation_mismatch','detail':'中文存在另一处独立核心含义错误。',
                    'source_quote':'3 Menschen','translation_quote':'3人','related_ids':[]})
            return response
        self.mutate = first_only
        with patch.object(translation_recovery,'autonomous_diagnostic_resolved',return_value=False,create=True):
            first = self.run_review()
        self.mutate = None
        self.rows[-1]['text_zh'] = '这里有3人。'
        self.plan()
        with patch.object(translation_recovery,'autonomous_diagnostic_resolved',return_value=False,create=True):
            second = self.run_review()
        self.assertEqual({issue['type'] for issue in second['findings']['line0001']['typed_issues']},
                         {'source_uncertain','translation_mismatch'})
        # A changed, verified recovery receipt is part of the real controller's
        # source hint binding. Simulate that new evidence without mutating a row.
        write(self.folder/'translation/source_review_hints.json',
              {'schema':'source_review_hints_v1','rows':{}})
        with patch.object(translation_recovery,'autonomous_diagnostic_resolved',return_value=True,create=True) as resolved:
            third = self.run_review()
        self.assertEqual(third['status'],'rejected')
        self.assertEqual([issue['type'] for issue in third['findings']['line0001']['typed_issues']],
                         ['translation_mismatch'])
        self.assertTrue(resolved.called)
        args = resolved.call_args.args
        self.assertEqual(args[1],'line0001')
        self.assertEqual(args[2],first['snapshot']['whole_script'][0]['source_binding'])
        self.assertEqual(set(args[3]),{'path','sha256'})
        self.assertGreater(args[4],0)

    def test_source_resolution_errors_never_become_approvals(self):
        import translation_recovery
        self.mutate = lambda response, request: self.issue(response, request, 'source_uncertain')
        with patch.object(translation_recovery,'autonomous_diagnostic_resolved',return_value=False,create=True):
            self.run_review()
        self.mutate = None
        self.rows[-1]['text_zh'] = '这里有3人。'
        self.plan()
        with patch.object(translation_recovery,'autonomous_diagnostic_resolved',side_effect=ValueError('corrupt proof'),create=True):
            with self.assertRaises(ValueError):self.run_review()
        with patch.object(translation_recovery,'autonomous_diagnostic_resolved',return_value='yes',create=True):
            with self.assertRaises(review.ContentReviewError):self.run_review()

    def test_actual_saved_target_change_during_network_prevents_publication(self):
        write(self.folder / 'segments.json', {'segments': self.rows})
        def concurrent_edit(response, request):
            changed = copy.deepcopy(self.rows)
            changed[0]['text_zh'] = '内容在审校时被改变。'
            write(self.folder / 'segments.json', {'segments': changed})
            return response
        self.mutate = concurrent_edit
        with self.assertRaises(review.ContentReviewError): self.run_review()
        self.assertFalse((self.folder / review.REPORT).exists())

    def test_path_reports_and_absent_current_report(self):
        self.assertIsNone(review.current_report(self.folder, self.rows))
        result = self.run_review()
        actual = review.verify_report(self.root, self.folder, self.rows, review.REPORT,
                                      client_factory=self.factory)
        self.assertEqual(actual, result)
        with self.assertRaises((ValueError, review.ContentReviewError)):
            review.verify_report(self.root, self.folder, self.rows, '../outside.json',
                                 client_factory=self.factory)


if __name__ == '__main__':
    unittest.main()
