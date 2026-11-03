from copy import deepcopy
import hashlib
import importlib
import json
from pathlib import Path
from unittest import TestCase,main,mock

from collectors.model_evidence.contract import ContractError,encode
from collectors.model_evidence.native import timestamp
from collectors.model_evidence.selected_ci import normalize,join_input
from collectors.model_evidence.selected_ci_contract import accept_source,accept_result,INPUT_JSON_SCHEMA,OUTPUT_JSON_SCHEMA,MAX_BYTES
from collectors.model_evidence.join_contract import accept_input,accept_output
from collectors.model_evidence.joins import derive

DIRECTORY=Path(__file__).parent/'fixtures'/'model-evidence-selected-ci-v1'


def supplied():
    return json.loads((DIRECTORY/'source.json').read_bytes())


def normalized(value=None):
    value=supplied() if value is None else value
    return normalize(encode(value),generated_at=timestamp(value['capture_time']))


def checks(value):
    return [c for s in value['head']['checkSuites']['nodes'] for c in s['checkRuns']['nodes']]


def jobs(value):
    return {j['name']:j for j in value['assessment']['technical_jobs']}


def rows(value):
    return [{k:v for k,v in r.items() if k!='evidence'} for r in value['ci_attempts']]


class SelectedCI(TestCase):
    def test_exact_supplied_golden_and_raw_digest(self):
        raw=(DIRECTORY/'source.json').read_bytes()
        value=normalize(raw,generated_at=timestamp(json.loads(raw)['capture_time']))
        self.assertEqual(hashlib.sha256(raw).hexdigest(),'0ede3398820409a93b56b6a9c09db20787c67a63557843655508cf22380f1d3a')
        self.assertEqual(encode(value),(DIRECTORY/'normalized.golden.json').read_bytes())
        self.assertEqual(value['observations'],json.loads(raw))
        self.assertEqual(value['authentication'],'not-performed')
        self.assertFalse(value['merge_authority'])

    def test_preserves_all_suites_checks_workflows_and_original_clocks(self):
        source=supplied();value=normalized(source)
        self.assertEqual(value['assessment']['suites_retained'],4)
        self.assertEqual(value['assessment']['check_rows_retained'],6)
        cursor=next(s for s in value['observations']['head']['checkSuites']['nodes'] if s['app']['slug']=='cursor')
        self.assertEqual(cursor['checkRuns']['nodes'],[])
        self.assertNotIn('workflowRun',cursor)
        emitted={r['check_id']:r for r in value['ci_attempts'] if r['check_id'] is not None}
        self.assertEqual(len(emitted),5)
        for suite in source['head']['checkSuites']['nodes']:
            for check in suite['checkRuns']['nodes']:
                if check['databaseId']==121199777740:continue
                row=emitted[check['databaseId']]
                self.assertEqual((row['suite_id'],row['app_id'],row['app_slug']),(suite['databaseId'],suite['app']['databaseId'],suite['app']['slug']))
                self.assertEqual((row['workflow_run_id'],row['event'],row['run_number']),(suite['workflowRun']['databaseId'],suite['workflowRun']['event'],suite['workflowRun']['runNumber']))
                self.assertEqual((row['started_at'],row['completed_at']),(timestamp(check['startedAt']),timestamp(check['completedAt'])))

    def test_equal_times_and_results_do_not_merge_distinct_checks(self):
        fast=[r for r in normalized()['ci_attempts'] if r['name']=='fast']
        self.assertEqual({r['check_id'] for r in fast},{121304224870,121199775743})
        self.assertEqual(len({r['id'] for r in fast}),2)
        self.assertEqual(len({(r['started_at'],r['completed_at'],r['conclusion']) for r in fast}),1)

    def test_invalid_interval_is_preserved_and_isolated(self):
        value=normalized()
        raw=next(c for c in checks(value['observations']) if c['databaseId']==121199777740)
        self.assertEqual((raw['startedAt'],raw['completedAt']),('2026-10-25T14:43:57Z','2026-10-25T14:43:56Z'))
        self.assertNotIn(121199777740,{r['check_id'] for r in value['ci_attempts']})
        self.assertIn('invalid-interval',{d['reason'] for d in value['dispositions']})
        self.assertTrue(all(j['captured_history_complete'] for j in jobs(value).values()))
        self.assertEqual(value['assessment']['emitted_valid_checks'],5)

    def test_original_failure_and_later_pass_do_not_supply_attempts(self):
        value=normalized();integration=[r for r in value['ci_attempts'] if r['name']=='integration']
        self.assertEqual({r['check_id']:r['conclusion'] for r in integration},{121210124124:'FAILURE',121304223728:'SUCCESS'})
        job=jobs(value)['integration']
        self.assertEqual((job['earliest_started_outcome'],job['earliest_completed_outcome'],job['latest_completed_outcome']),('fail','fail','pass'))
        self.assertTrue(all(r['run_attempt'] is None and r['workflow_id'] is None for r in value['ci_attempts']))
        self.assertFalse(value['assessment']['workflow_attempt_known'])

    def test_workflow_names_numbers_and_row_order_do_not_fill_missing_ids(self):
        source=supplied()
        for suite in source['head']['checkSuites']['nodes']:
            if 'workflowRun' not in suite:continue
            suite['workflowRun']['runNumber']=1
            suite['workflowRun']['workflow']['name']='1'
            suite['checkRuns']['nodes'].reverse()
        self.assertTrue(all(r['workflow_id'] is None and r['run_attempt'] is None for r in normalized(source)['ci_attempts']))
        source['head']['checkSuites']['nodes'][1].pop('workflowRun')
        emitted=[r for r in normalized(source)['ci_attempts'] if r['name']=='integration']
        self.assertTrue(all(r['workflow_run_id'] is None and r['event'] is None and r['run_number'] is None for r in emitted))

    def test_timeline_count_overrules_false_page_flag_and_current_nondraft(self):
        value=normalized()
        self.assertFalse(value['assessment']['timeline_complete'])
        self.assertEqual(value['assessment']['first_ready'],'unknown')
        self.assertEqual(len(value['observations']['opening_receipts']),1)
        self.assertEqual(value['observations']['opening_receipts'][0]['id'],'18162214939')
        source=supplied();source['pr_readiness']['timelineItems']['totalCount']=2
        value=normalized(source)
        self.assertTrue(value['assessment']['timeline_complete'])
        self.assertEqual(value['assessment']['first_ready'],'unknown')

    def test_later_policy_and_all_protected_contexts_remain_distinct(self):
        value=normalized();assessment=value['assessment']
        self.assertEqual(assessment['protected_contexts'],['fast','integration','review-gate'])
        self.assertEqual(assessment['technical_contexts'],['fast','integration'])
        self.assertIn('review-gate',assessment['excluded_from_technical_domain'])
        self.assertEqual({r['name'] for r in value['policies'][0]['required']},{'fast','integration','review-gate'})
        self.assertEqual(value['observations']['policy']['read'],1793285006.010581)
        self.assertEqual(assessment['historical_policy_applicability'],'unknown')
        self.assertFalse(value['policies'][0]['complete'])
        self.assertIn('policy-after-head-read',{d['reason'] for d in value['dispositions']})

    def test_commit_status_stays_separate_latest_state_without_app_inference(self):
        value=normalized();status=next(r for r in value['ci_attempts'] if r['mechanism']=='commit_status')
        self.assertEqual(status['name'],'review-gate')
        self.assertEqual(status['conclusion'],'SUCCESS')
        self.assertIsNone(status['app_id']);self.assertIsNone(status['app_slug'])
        self.assertFalse(status['history_complete']);self.assertIsNone(status['started_at'])
        self.assertEqual(status['completed_at'],timestamp('2026-10-25T15:38:54Z'))
        source=supplied();source['head']['status']['contexts'][0]['state']='PENDING'
        self.assertEqual(next(r for r in normalized(source)['ci_attempts'] if r['mechanism']=='commit_status')['status'],'IN_PROGRESS')

    def test_wrong_repository_pr_or_head_are_rejected(self):
        for mutate in (lambda s:s.update(repository='wrong/repository'),lambda s:s.update(pr_number=451),
                       lambda s:s.update(sha='e'*40),lambda s:s['head'].update(oid='e'*40),
                       lambda s:s['policy'].update(repository='wrong/repository'),
                       lambda s:s['opening_receipts'][0].update(head_sha='e'*40)):
            with self.subTest(mutate=mutate):
                source=supplied();mutate(source)
                with self.assertRaises(ContractError):normalized(source)

    def test_extra_private_fields_at_any_depth_are_rejected(self):
        for mutate in (lambda s:s.update(prompt='private'),lambda s:checks(s)[0].update(output='private'),
                       lambda s:s['head']['checkSuites']['nodes'][1]['workflowRun'].update(command='private'),
                       lambda s:s['head']['status']['contexts'][0]['creator'].update(auth='private')):
            source=supplied();mutate(source)
            with self.assertRaises(ContractError):normalized(source)

    def test_decoded_duplicate_keys_and_invalid_json_are_rejected(self):
        raw=(DIRECTORY/'source.json').read_bytes()
        for bad in (b'{"schema":0,"sch\\u0065ma":1}',raw.rstrip()[:-1],raw.replace(b'"databaseId":',b'"databaseId":0,"databaseId":',1),b'NaN'):
            with self.subTest(size=len(bad)):
                with self.assertRaises(ContractError):accept_source(bad)

    def test_size_count_and_typed_numeric_bounds(self):
        with self.assertRaises(ContractError):accept_source(b' '*(MAX_BYTES+1))
        for mutate in (lambda s:s['head'].update(fetched=10**1000),lambda s:s['policy'].update(read=True),
                       lambda s:checks(s)[0].update(databaseId=1.5),lambda s:checks(s)[0].update(databaseId=0),
                       lambda s:s['head']['checkSuites'].update(nodes=s['head']['checkSuites']['nodes']*4),
                       lambda s:s['head']['checkSuites']['nodes'][1]['checkRuns'].update(nodes=[checks(s)[0]]*51),
                       lambda s:s.update(opening_receipts=s['opening_receipts']*2),
                       lambda s:s['pr_readiness']['timelineItems'].update(nodes=s['pr_readiness']['timelineItems']['nodes']*51)):
            source=supplied();mutate(source)
            with self.assertRaises(ContractError):normalized(source)

    def test_reordering_changes_custody_digest_without_changing_normalized_facts(self):
        source=supplied();before=normalized(source)
        source['head']['checkSuites']['nodes'].reverse()
        for suite in source['head']['checkSuites']['nodes']:suite['checkRuns']['nodes'].reverse()
        source['pr_readiness']['timelineItems']['nodes'].reverse()
        for rule in source['policy']['rules']['branchProtectionRules']['nodes']:
            rule['requiredStatusChecks'].reverse();rule['requiredStatusCheckContexts'].reverse()
        source['policy']['rules']['rulesets']['nodes'][0]['rules']['nodes'].reverse()
        after=normalized(source)
        self.assertEqual(rows(before),rows(after));self.assertEqual(before['assessment'],after['assessment'])
        self.assertEqual(before['policies'][0]['id'],after['policies'][0]['id'])
        self.assertNotEqual(before['projection_sha256'],after['projection_sha256'])
        self.assertEqual(after['observations'],source)

    def test_exact_copies_do_not_create_attempts(self):
        source=supplied();before=normalized(source)
        for suite in source['head']['checkSuites']['nodes']:
            suite['checkRuns']['nodes']+=deepcopy(suite['checkRuns']['nodes'])
        source['head']['checkSuites']['nodes']+=deepcopy(source['head']['checkSuites']['nodes'])
        source['head']['status']['contexts']*=2
        after=normalized(source)
        self.assertEqual(rows(before),rows(after))
        self.assertIn('exact-duplicate',{d['reason'] for d in after['dispositions']})

    def test_conflicting_check_copies_withhold_only_affected_job(self):
        source=supplied();copy=deepcopy(source['head']['checkSuites']['nodes'][1])
        next(c for c in copy['checkRuns']['nodes'] if c['databaseId']==121210124124)['conclusion']='SUCCESS'
        source['head']['checkSuites']['nodes'].append(copy)
        expected=normalized(source)
        self.assertNotIn(121210124124,{r['check_id'] for r in expected['ci_attempts']})
        self.assertFalse(jobs(expected)['integration']['captured_history_complete'])
        self.assertEqual(jobs(expected)['integration']['earliest_started_outcome'],'unknown')
        self.assertTrue(jobs(expected)['fast']['captured_history_complete'])
        self.assertIn('check-identity-conflict',{d['reason'] for d in expected['dispositions']})
        source['head']['checkSuites']['nodes'].reverse()
        self.assertEqual(rows(expected),rows(normalized(source)))

    def test_app_conflict_and_wrong_app_cannot_enter_technical_domain(self):
        source=supplied();source['head']['checkSuites']['nodes'][2]['app']['slug']='different-app'
        value=normalized(source)
        self.assertIn('app-identity-conflict',{d['reason'] for d in value['dispositions']})
        self.assertFalse(any(r['app_id']==15368 for r in value['ci_attempts']))
        source=supplied();source['head']['checkSuites']['nodes'][1]['app']={'databaseId':999,'slug':'different-app'}
        value=normalized(source)
        self.assertEqual(jobs(value)['fast']['valid_observations'],0)
        self.assertIn('outside-technical-app-domain',{d['reason'] for d in value['dispositions']})
        self.assertTrue(any(r['app_id']==999 for r in value['ci_attempts']))

    def test_wrong_stable_app_id_cannot_borrow_github_actions_slug(self):
        source=supplied()
        for suite in source['head']['checkSuites']['nodes']:
            if suite['app']['slug']=='github-actions':suite['app']['databaseId']=999
        value=normalized(source)
        self.assertTrue(any(r['app_id']==999 for r in value['ci_attempts']))
        self.assertTrue(all(j['valid_observations']==0 and not j['captured_history_complete'] for j in jobs(value).values()))
        self.assertIn('outside-technical-app-domain',{d['reason'] for d in value['dispositions']})

    def test_missing_stable_identity_is_not_filled_from_names_or_order(self):
        for field in ('databaseId','app'):
            source=supplied();source['head']['checkSuites']['nodes'][1][field]=None
            value=normalized(source)
            self.assertFalse(any(r['name']=='fast' for r in value['ci_attempts']))
            self.assertIn('stable-identity-unavailable-or-conflicting',{d['reason'] for d in value['dispositions']})
        source=supplied();checks(source)[0]['databaseId']=None
        self.assertEqual(normalized(source)['assessment']['emitted_valid_checks'],4)

    def test_missing_fields_reject_but_truncation_keeps_valid_observations(self):
        source=supplied();del source['policy']['read']
        with self.assertRaises(ContractError):normalized(source)
        source=supplied();suite=source['head']['checkSuites']['nodes'][1]
        suite['checkRuns']['nodes'].pop()
        value=normalized(source)
        self.assertEqual(len([r for r in value['ci_attempts'] if r['suite_id']==suite['databaseId']]),3)
        self.assertFalse(jobs(value)['integration']['captured_history_complete'])
        self.assertIn('check-count-or-pagination-incomplete',{d['reason'] for d in value['dispositions']})
        source=supplied();source['head']['checkSuites']['pageInfo']['hasNextPage']=True
        self.assertTrue(all(not r['history_complete'] for r in normalized(source)['ci_attempts']))

    def test_partial_and_full_copies_have_order_independent_completeness(self):
        source=supplied();copy=deepcopy(source['head']['checkSuites']['nodes'][1])
        copy['checkRuns']['nodes'].pop();source['head']['checkSuites']['nodes'].append(copy)
        before=normalized(source)
        source['head']['checkSuites']['nodes'].reverse()
        after=normalized(source)
        self.assertEqual(rows(before),rows(after))
        self.assertFalse(jobs(after)['fast']['captured_history_complete'])

    def test_coverage_assertions_never_cure_semantic_or_retention_gaps(self):
        source=supplied();source['source_gaps']=[];source['event_coverage']['gaps']=[]
        value=normalized(source)
        self.assertIsNone(value['sources'][0]['complete_through'])
        self.assertIsNone(value['sources'][0]['generated_at'])
        self.assertEqual(value['sources'][0]['origin'],'supplied')
        self.assertEqual(value['assessment']['first_ready'],'unknown')
        source['source_gaps']=['selected-history-partial']
        self.assertTrue(all(not r['history_complete'] for r in normalized(source)['ci_attempts']))

    def test_invalid_missing_and_future_clocks_are_not_replaced(self):
        for at in (None,'invalid'):
            source=supplied();checks(source)[0]['startedAt']=at
            value=normalized(source)
            self.assertEqual(checks(value['observations'])[0]['startedAt'],at)
            self.assertNotIn(checks(source)[0]['databaseId'],{r['check_id'] for r in value['ci_attempts']})
        source=supplied();checks(source)[0]['startedAt']='2026-11-01T00:00:00Z'
        with self.assertRaises(ContractError):normalized(source)
        source=supplied();source['event_coverage']['coverage']=[[5,4]]
        with self.assertRaises(ContractError):normalized(source)
        source=supplied();source['opening_receipts'][0]['t']+=1
        with self.assertRaises(ContractError):normalized(source)
        source=supplied()
        with self.assertRaises(ContractError):normalize(encode(source),generated_at=timestamp(source['capture_time'])-1)
        source=supplied();source['head']['fetched']=timestamp('2026-10-25T23:00:00Z')
        value=normalized(source)
        self.assertIn('check-clock-after-head-read',{d['reason'] for d in value['dispositions']})
        self.assertEqual(jobs(value)['integration']['earliest_started_outcome'],'unknown')

    def test_clock_ties_with_different_outcomes_remain_unknown(self):
        source=supplied();fast=[c for c in checks(source) if c['name']=='fast']
        fast[0]['conclusion']='FAILURE'
        result=jobs(normalized(source))['fast']
        self.assertEqual((result['earliest_started_outcome'],result['earliest_completed_outcome'],result['latest_completed_outcome']),('unknown','unknown','unknown'))

    def test_clock_description_is_not_numeric_check_order(self):
        source=supplied();integration=[c for c in checks(source) if c['name']=='integration']
        integration[0]['databaseId'],integration[1]['databaseId']=integration[1]['databaseId'],integration[0]['databaseId']
        job=jobs(normalized(source))['integration']
        self.assertEqual((job['earliest_started_outcome'],job['latest_completed_outcome']),('fail','pass'))

    def test_schemas_and_closed_output_cannot_acquire_authority(self):
        directory=Path(__file__).parents[1]/'collectors'/'model_evidence'
        self.assertEqual(json.loads((directory/'selected-ci-source-v1.schema.json').read_bytes()),INPUT_JSON_SCHEMA)
        self.assertEqual(json.loads((directory/'selected-ci-normalized-v1.schema.json').read_bytes()),OUTPUT_JSON_SCHEMA)
        value=normalized();self.assertEqual(accept_result(value),value)
        for key,val in (('merge_authority',True),('authentication','qualified'),('operations',[{}]),('assignments',[{}])):
            other=deepcopy(value);other[key]=val
            with self.assertRaises(ContractError):accept_result(other)

    def test_existing_contracts_accept_synthetic_structure_without_roles_or_head_defaults(self):
        agent=json.loads((DIRECTORY.parent/'model-evidence-head-joins-v1'/'synthetic.json').read_bytes())['agent_evidence']
        self.assertEqual(agent['origin']['kind'],'synthetic-replay')
        value=normalized();bundle=join_input(value,agent,generated_at=value['generated_at'])
        self.assertEqual(accept_input(bundle),bundle)
        self.assertEqual(bundle['heads'],[])
        self.assertEqual(bundle['operations'],[]);self.assertEqual(bundle['assignments'],[])
        result=derive(bundle);self.assertEqual(accept_output(result),result)
        self.assertEqual(result['agent_evidence'],bundle['agent_evidence'])
        for key in ('exact_pr_linked_responses','verified_r0_ci_heads','explicit_assignment_roles'):
            self.assertEqual(result['denominators'][key],0)
        self.assertEqual(len(result['records']['ci_attempts']),6)

    def test_import_and_normalization_do_not_acquire_external_state(self):
        source=supplied();raw=encode(source);at=timestamp(source['capture_time'])
        import collectors.model_evidence.selected_ci as module
        with mock.patch('builtins.open',side_effect=AssertionError('filesystem')),mock.patch('sqlite3.connect',side_effect=AssertionError('database')),mock.patch('socket.socket',side_effect=AssertionError('network')),mock.patch('os.getenv',side_effect=AssertionError('environment')):
            importlib.reload(module)
            self.assertEqual(module.normalize(raw,generated_at=at)['observations'],source)


class SelectedCISuiteCopies(TestCase):
    def copied(self,keep):
        source=supplied()
        suite=next(s for s in source['head']['checkSuites']['nodes'] if any(c['name']=='fast' for c in s['checkRuns']['nodes']))
        copy=deepcopy(suite)
        if keep is not None:copy['checkRuns']['nodes']=[c for c in copy['checkRuns']['nodes'] if c['name'] in keep]
        source['head']['checkSuites']['nodes'].append(copy)
        return source,suite['databaseId']

    def orders(self,source):
        for offset,reverse in ((0,False),(0,True),(2,False)):
            copy=deepcopy(source);suites=copy['head']['checkSuites']['nodes']
            copy['head']['checkSuites']['nodes']=suites[offset:]+suites[:offset]
            if reverse:copy['head']['checkSuites']['nodes'].reverse()
            for suite in copy['head']['checkSuites']['nodes']:suite['checkRuns']['nodes'].reverse()
            yield copy

    def assert_uncertain(self,result,suite_id):
        affected=[r for r in result['ci_attempts'] if r['suite_id']==suite_id]
        self.assertEqual(len(affected),4)
        self.assertTrue(all(not r['history_complete'] for r in affected))
        for job in jobs(result).values():
            self.assertFalse(job['captured_history_complete'])
            self.assertEqual((job['earliest_started_outcome'],job['earliest_completed_outcome'],job['latest_completed_outcome']),('unknown','unknown','unknown'))
        self.assertIn('check-count-or-pagination-incomplete',{d['reason'] for d in result['dispositions']})

    def test_s1_empty_incomplete_copy_taints_full_copy_union_in_both_orders(self):
        source,suite_id=self.copied(())
        for index,ordered in enumerate(self.orders(source)):
            with self.subTest(order=index):self.assert_uncertain(normalized(ordered),suite_id)

    def test_s1_copy_omitting_entire_fast_job_taints_absent_rows(self):
        source,suite_id=self.copied(('integration',))
        for index,ordered in enumerate(self.orders(source)):
            with self.subTest(order=index):self.assert_uncertain(normalized(ordered),suite_id)

    def test_s1_copy_omitting_entire_integration_job_taints_absent_rows(self):
        source,suite_id=self.copied(('fast',))
        for index,ordered in enumerate(self.orders(source)):
            with self.subTest(order=index):self.assert_uncertain(normalized(ordered),suite_id)

    def test_s1_baseline_and_exact_full_copies_remain_complete(self):
        baseline=normalized();source,suite_id=self.copied(None)
        self.assertTrue(all(j['captured_history_complete'] for j in jobs(baseline).values()))
        for ordered in self.orders(source):
            result=normalized(ordered)
            self.assertEqual(rows(result),rows(baseline))
            self.assertEqual(result['assessment']['technical_jobs'],baseline['assessment']['technical_jobs'])
            self.assertIn('exact-duplicate',{d['reason'] for d in result['dispositions']})

    def test_s1_deleting_partial_rows_never_restores_confidence(self):
        results=[]
        for keep in (('fast',),('integration',),()):
            source,suite_id=self.copied(keep)
            result=normalized(source);results.append(result)
        for result in results:
            with self.subTest(retained=result['assessment']['check_rows_retained']):self.assert_uncertain(result,suite_id)
        self.assertEqual([j['captured_history_complete'] for j in jobs(results[-1]).values()],[False,False])

    def test_s1_uncertainty_does_not_spread_to_other_suite_job_or_app(self):
        source,affected_id=self.copied(())
        target=next(s for s in source['head']['checkSuites']['nodes'] if s['databaseId']==affected_id)
        for suite_id,app,name,check_id in ((900001,{'databaseId':15368,'slug':'github-actions'},'synthetic-lint',900011),
                                         (900002,{'databaseId':900003,'slug':'synthetic-app'},'fast',900012)):
            suite=deepcopy(target);suite.update(databaseId=suite_id,app=app)
            suite['workflowRun']['databaseId']=suite_id+100
            check=deepcopy(target['checkRuns']['nodes'][0]);check.update(databaseId=check_id,name=name)
            suite['checkRuns'].update(totalCount=1,nodes=[check]);source['head']['checkSuites']['nodes'].append(suite)
        source['head']['checkSuites']['totalCount']+=2
        for ordered in self.orders(source):
            result=normalized(ordered)
            independent=[r for r in result['ci_attempts'] if r['suite_id'] in (900001,900002)]
            self.assertEqual(len(independent),2);self.assertTrue(all(r['history_complete'] for r in independent))
            self.assertEqual({r['app_id'] for r in independent},{15368,900003})

    def test_s1_unrelated_empty_app_connection_does_not_taint_technical_jobs(self):
        source=supplied();cursor=next(s for s in source['head']['checkSuites']['nodes'] if s['app']['slug']=='cursor')
        partial=deepcopy(cursor);partial['checkRuns']['totalCount']=1
        source['head']['checkSuites']['nodes'].append(partial)
        for ordered in self.orders(source):
            result=normalized(ordered)
            self.assertTrue(all(j['captured_history_complete'] for j in jobs(result).values()))
            self.assertEqual(len(result['ci_attempts']),6)
            self.assertIn('check-count-or-pagination-incomplete',{d['reason'] for d in result['dispositions']})

    def test_s1_partial_connections_retain_original_facts_and_unknown_gates(self):
        source,suite_id=self.copied(())
        result=normalized(source);baseline=normalized()
        self.assertEqual(result['observations'],source)
        clean=lambda value:[{k:v for k,v in row.items() if k not in ('history_complete','evidence')} for row in value['ci_attempts']]
        self.assertEqual(clean(result),clean(baseline))
        self.assertEqual(result['sources'][0]['snapshot_at'],baseline['sources'][0]['snapshot_at'])
        self.assertEqual(result['generated_at'],baseline['generated_at'])
        self.assertEqual(result['assessment']['protected_contexts'],['fast','integration','review-gate'])
        self.assertEqual(result['assessment']['head_bridge'],'unavailable-missing-replaced-state')
        self.assertEqual(result['assessment']['first_ready'],'unknown')
        self.assertEqual(result['assessment']['historical_policy_applicability'],'unknown')
        self.assertTrue(all(r['run_attempt'] is None and r['workflow_id'] is None for r in result['ci_attempts']))
        self.assertEqual(result['heads'],[]);self.assertEqual(result['operations'],[]);self.assertEqual(result['assignments'],[])

    def test_s1_full_rows_with_open_pagination_remain_unknown(self):
        source,suite_id=self.copied(None);source['head']['checkSuites']['nodes'][-1]['checkRuns']['pageInfo']['hasNextPage']=True
        for ordered in self.orders(source):self.assert_uncertain(normalized(ordered),suite_id)


if __name__=='__main__':
    main()
