import copy
import json
from pathlib import Path
import unittest

from collectors.model_evidence.contract import ContractError, encode
from collectors.model_evidence.join_contract import accept_input, accept_output, INPUT_JSON_SCHEMA, OUTPUT_JSON_SCHEMA
from collectors.model_evidence.joins import derive, empty_input, normalize_pr_record, refresh

FIXTURE = Path(__file__).parent/'fixtures'/'model-evidence-head-joins-v1'/'synthetic.json'


def fixture():
    return json.loads(FIXTURE.read_text())


class EvidenceHeadJoins(unittest.TestCase):
    def test_distinct_producer_publisher_and_independent_review(self):
        b=fixture();out=derive(b)
        roles={e['role']:e for e in out['edges']}
        self.assertNotEqual(roles['produce']['run_id'],roles['publish']['run_id'])
        self.assertTrue(all(e['state']=='linked' and e['basis']=='synthetic' for e in roles.values()))
        self.assertEqual(out['denominators']['exact_pr_linked_responses'],15)
        self.assertEqual(out['denominators']['independent_reviews'],1)
        self.assertEqual(out['agent_evidence'],accept_input(b)['agent_evidence'])
        self.assertEqual(out['authentication'],'not-performed');self.assertFalse(out['merge_authority'])

    def test_no_receipts_means_observations_without_proven_links(self):
        b=fixture();out=derive(empty_input(b['agent_evidence'],generated_at=b['generated_at']))
        self.assertEqual(len(out['agent_evidence']['requests']),15)
        self.assertEqual(out['edges'],[]);self.assertEqual(out['denominators']['exact_pr_linked_responses'],0)
        self.assertTrue(all(r['allocation']=='unknown' for r in out['request_links']))

    def test_wrong_repository_same_sha_does_not_join(self):
        b=fixture();b['heads'][0]['repository']='another/repo';out=derive(b)
        self.assertTrue(all(e['state']=='unmatched' for e in out['edges']))
        self.assertEqual(out['denominators']['exact_pr_linked_responses'],0)

    def test_rebase_new_sha_does_not_transfer_old_receipts(self):
        b=fixture();b['heads'][0]['sha']='e'*40;out=derive(b)
        self.assertEqual(out['denominators']['exact_pr_linked_responses'],0)
        self.assertIn('exact-pr-head-missing',out['edges'][0]['reasons'])

    def test_shared_membership_has_no_exclusive_allocation_or_double_count(self):
        b=fixture();h=copy.deepcopy(b['heads'][0]);h.update(id='head:2',pr=2);b['heads'].append(h)
        out=derive(b)
        self.assertEqual(out['denominators']['shared_pr_responses'],15)
        self.assertEqual(out['denominators']['exact_pr_linked_responses'],0)
        self.assertTrue(all(len(q['head_ids'])==2 and q['allocation']=='shared' for q in out['request_links']))

    def test_first_red_is_not_erased_by_later_pass_or_first_decided(self):
        out=derive(fixture());ci=out['ci'][0]
        self.assertEqual((ci['first_attempt'],ci['first_completed'],ci['latest']),('fail','fail','pass'))
        self.assertTrue(ci['verified_r0_ci_eligible'])
        b=fixture();b['heads'][0]['r0_status']='candidate';out=derive(b)
        self.assertTrue(out['records']['heads'][0]['first_decided'])
        self.assertFalse(out['ci'][0]['verified_r0_ci_eligible'])

    def test_missing_workflow_attempt_is_not_run_number(self):
        b=fixture()
        for c in b['ci_attempts']:c['run_attempt']=None
        out=derive(b)
        self.assertFalse(out['ci'][0]['workflow_attempt_known'])
        self.assertTrue(all(c['run_attempt'] is None and c['run_number']==99 for c in out['records']['ci_attempts']))
        b['ci_attempts'][0]['check_id']=None
        self.assertFalse(derive(b)['ci'][0]['verified_r0_ci_eligible'])

    def test_first_completed_is_not_first_attempt_or_verified_r0(self):
        b=fixture();b['ci_attempts'][0].update(status='QUEUED',conclusion=None,completed_at=None)
        b['heads'][0]['first_attempt'].update(outcome='unknown',failed=[])
        b['heads'][0]['first_completed'].update(outcome='pass',failed=[])
        ci=derive(b)['ci'][0]
        self.assertEqual((ci['first_attempt'],ci['first_completed']),('unknown','pass'))
        self.assertFalse(ci['verified_r0_ci_eligible'])

    def test_reused_r0_receipt_cannot_verify_two_prs(self):
        b=fixture();h=copy.deepcopy(b['heads'][0]);h.update(id='head:2',pr=2);b['heads'].append(h)
        out=derive(b)
        self.assertEqual(out['denominators']['verified_r0_ci_heads'],0)
        self.assertTrue(all('r0-receipt-conflict' in c['reasons'] for c in out['ci']))

    def test_missing_policy_and_failed_refresh_exclude_ci_but_keep_old_read(self):
        b=fixture();read=b['heads'][0]['read'];b['heads'][0]['refresh_failed']=read+1
        out=derive(b);self.assertFalse(out['ci'][0]['verified_r0_ci_eligible'])
        self.assertEqual(out['records']['heads'][0]['read'],read)
        b=fixture();b['policies']=[]
        self.assertFalse(derive(b)['ci'][0]['verified_r0_ci_eligible'])

    def test_completed_check_correction_cannot_hide_first_failure(self):
        b=fixture();c=copy.deepcopy(b['ci_attempts'][0]);c.update(revision=1,conclusion='SUCCESS');b['ci_attempts'].append(c)
        out=derive(b)
        self.assertIn('terminal-check-revision-conflict',[d['reason'] for d in out['dispositions']])
        self.assertFalse(out['ci'][0]['verified_r0_ci_eligible'])

    def test_same_actor_review_excluded_and_cannot_trigger_rework(self):
        b=fixture();a=b['assignments'][0];a.update(actor_id='produce',review_outcome='changes_requested')
        new=copy.deepcopy(a);new.update(id='assignment:rework',role='rework',actor_id='produce',trigger='review',trigger_id=a['id'])
        new.update(assigned_at=a['resolved_at'],resolved_at=a['resolved_at']+1);b['assignments'].append(new)
        out=derive(b)
        self.assertEqual(out['denominators']['independent_reviews'],0)
        self.assertIn('same-actor-review',next(e for e in out['edges'] if e['receipt_id']==a['id'])['reasons'])
        self.assertIn('review-trigger-unqualified',next(e for e in out['edges'] if e['receipt_id']==new['id'])['reasons'])

    def test_exact_ci_failure_trigger_and_missing_trigger(self):
        b=fixture();a=b['assignments'][0];a.update(role='rework',trigger='ci-failure',trigger_id='check:1')
        out=derive(b);self.assertEqual(next(e for e in out['edges'] if e['role']=='rework')['state'],'linked')
        a['trigger_id']='check:missing';out=derive(b)
        self.assertIn('ci-trigger-unresolved',next(e for e in out['edges'] if e['role']=='rework')['reasons'])

    def test_supersession_is_explicit_and_cycles_are_excluded(self):
        b=fixture();old=b['assignments'][0];new=copy.deepcopy(old);new.update(id='assignment:new',supersedes=old['id'],assigned_at=old['assigned_at']+1)
        b['assignments'].append(new);out=derive(b)
        self.assertIn('assignment-superseded',next(e for e in out['edges'] if e['receipt_id']==old['id'])['reasons'])
        old['supersedes']=new['id'];out=derive(b)
        self.assertTrue(all(e['state']!='linked' for e in out['edges'] if e['role']=='review'))

    def test_terminal_completion_and_original_owner_bindings_required(self):
        for change,reason in [({'terminal':'unknown'},'terminal-success-unproven'),
                              ({'completion_process_id':'wrong'},'completion-identity-mismatch'),
                              ({'native_turn_id':'another-turn'},'owner-turn-unresolved'),
                              ({'attempt_id':'another-attempt'},'operation-owner-mismatch')]:
            with self.subTest(change=change):
                b=fixture();b['operations'][0].update(change);out=derive(b)
                self.assertIn(reason,next(e for e in out['edges'] if e['role']=='produce')['reasons'])
                self.assertEqual(out['denominators']['exact_pr_linked_responses'],0)

    def test_conflicting_producers_cannot_claim_exclusive_usage(self):
        b=fixture();op=copy.deepcopy(b['operations'][0]);r=b['agent_evidence']['runs'][1]
        op.update(id='operation:other',run_id=r['id'],attempt_id=r['attempt_id'],native_session_id=r['native_session_id'],actor_id='publish',invocation_id='other',completion_invocation_id='other')
        b['operations'].append(op);out=derive(b)
        self.assertEqual(out['denominators']['exact_pr_linked_responses'],0)
        self.assertTrue(all('producer-conflict' in e['reasons'] for e in out['edges'] if e['role']=='produce'))

    def test_receipt_cannot_cross_known_owner_completion_boundary(self):
        b=fixture();b['operations'][0]['completion_evidence']['at']=b['agent_evidence']['runs'][0]['ended_at']+1
        b['operations'][0]['evidence']['at']=b['operations'][0]['completion_evidence']['at']
        out=derive(b)
        self.assertIn('operation-owner-clock-conflict',next(e for e in out['edges'] if e['role']=='produce')['reasons'])
        self.assertEqual(out['denominators']['exact_pr_linked_responses'],0)

    def test_deduplication_order_and_conflicting_duplicates(self):
        b=fixture();expected=derive(b)
        for key in ('heads','policies','ci_attempts','operations','assignments'):
            b[key]=list(reversed(b[key]+copy.deepcopy(b[key])))
        self.assertEqual(encode(derive(b)),encode(expected))
        b=fixture();bad=copy.deepcopy(b['operations'][0]);bad['sha']='e'*40;b['operations'].append(bad)
        out=derive(b);self.assertIn('conflicting-duplicate',[d['reason'] for d in out['dispositions']])
        self.assertEqual(out['denominators']['exact_pr_linked_responses'],0)

    def test_child_unknown_and_main_mismatch_do_not_block_partial_descriptive_link(self):
        b=fixture();b['agent_evidence']['runs'][0]['expected_children']=None;out=derive(b)
        self.assertEqual(out['denominators']['exact_pr_linked_responses'],15)
        self.assertTrue(all(not q['whole_task_usage_eligible'] for q in out['request_links']))

    def test_unverified_supplied_is_not_synthetic_or_authenticated(self):
        b=fixture();b['sources'][0]['origin']='supplied';out=derive(b)
        self.assertTrue(all(e['basis']=='unverified-supplied' for e in out['edges']))
        self.assertEqual(out['authentication'],'not-performed')
        b['sources'][0]['qualified']=True
        with self.assertRaises(ContractError):derive(b)

    def test_strict_privacy_identity_clock_and_hash_validation(self):
        for mutate in (lambda b:b['operations'][0].update(command='private'),
                       lambda b:b['heads'][0].update(sha='a'*12),
                       lambda b:b['heads'][0].update(repository='FlightOps/Example'),
                       lambda b:b['operations'][0]['evidence'].update(source_digest='e'*64),
                       lambda b:b.update(generated_at=10**1000)):
            b=fixture();mutate(b)
            with self.assertRaises(ContractError):derive(b)

    def test_failed_missing_and_regressed_refresh_retain_source_clocks(self):
        b=fixture();now=b['generated_at']+1;good=refresh(None,b,now=now)
        for incoming in (None,{}, {**b,'generated_at':b['generated_at']-1}):
            bad=refresh(good,incoming,now=now+10)
            self.assertEqual(bad['result'],good['result']);self.assertEqual(bad['received_at'],now)
        b['generated_at']+=10;b['sources'][0]['snapshot_at']-=1;b['sources'][0]['generated_at']-=1
        accept_input(b)
        bad=refresh(good,b,now=now+20)
        self.assertEqual(bad['result'],good['result'])

    def test_current_pr_record_projection_keeps_modes_and_drops_trailer_roles(self):
        raw=json.loads((Path(__file__).parent/'fixtures'/'pr-facts.json').read_text())
        b=fixture();record=next(iter(raw['prs'].values()))
        rows=normalize_pr_record(record,'flightops/example',b['heads'][0]['evidence'])
        self.assertEqual(rows[0]['first_attempt'],record['heads'][0]['first_attempt'])
        self.assertNotIn('family',rows[0]);self.assertNotIn('trailers',rows[0])
        self.assertEqual(rows[0]['r0_status'],record['r0']['status'])
        record['r0']['receipt']=123
        self.assertEqual(normalize_pr_record(record,'flightops/example',b['heads'][0]['evidence'])[0]['r0_receipt'],123)

    def test_versioned_schemas_and_output_validation(self):
        directory=Path('collectors/model_evidence')
        self.assertEqual(json.loads((directory/'evidence-head-input-v1.schema.json').read_text()),INPUT_JSON_SCHEMA)
        self.assertEqual(json.loads((directory/'evidence-head-output-v1.schema.json').read_text()),OUTPUT_JSON_SCHEMA)
        out=derive(fixture());self.assertEqual(accept_output(out),out)
        out['merge_authority']=True
        with self.assertRaises(ContractError):accept_output(out)


class JoinHistoryRepairs(unittest.TestCase):
    def conflicted_ci(self):
        b=fixture();bad=copy.deepcopy(b['ci_attempts'][0])
        bad.update(revision=1,conclusion='SUCCESS');b['ci_attempts'].append(bad)
        for key in ('first_attempt','first_completed','latest'):
            b['heads'][0][key]={'outcome':'pass','reason':None,'failed':[]}
        return b

    def successors(self):
        b=fixture();old=b['assignments'][0]
        valid=copy.deepcopy(old);valid.update(id='valid-successor',supersedes=old['id'],assigned_at=old['assigned_at']+1)
        foreign=copy.deepcopy(old);foreign.update(id='foreign-successor',task_id='unrelated-task',supersedes=old['id'],assigned_at=old['assigned_at']+2)
        b['assignments']=[old,valid,foreign]
        return b

    def edge(self, result, receipt):
        return next(e for e in result['edges'] if e['receipt_id']==receipt)

    def assert_history_withheld(self, result):
        ci=next(c for c in result['ci'] if c['head_id']=='head:1')
        self.assertEqual(ci['first_attempt'],'unknown')
        self.assertFalse(ci['verified_r0_ci_eligible'])
        self.assertIn('check-history-conflict',ci['reasons'])

    def test_j1_unconflicted_first_red_later_green_remains_known(self):
        ci=derive(fixture())['ci'][0]
        self.assertEqual((ci['first_attempt'],ci['first_completed'],ci['latest']),('fail','fail','pass'))
        self.assertTrue(ci['verified_r0_ci_eligible'])

    def test_j1_conflicting_first_check_cannot_be_replaced_by_pass_summary(self):
        result=derive(self.conflicted_ci())
        self.assert_history_withheld(result)
        self.assertIn('terminal-check-revision-conflict',[d['reason'] for d in result['dispositions']])

    def test_j1_permutations_and_exact_duplicates_preserve_conflict(self):
        b=self.conflicted_ci();expected=derive(b)
        for duplicated in (False,True):
            with self.subTest(duplicated=duplicated):
                other=copy.deepcopy(b)
                for key in ('heads','policies','ci_attempts','operations','assignments'):
                    other[key]=list(reversed(other[key]+copy.deepcopy(other[key]) if duplicated else other[key]))
                result=derive(other)
                self.assertEqual(encode(result),encode(expected))
                self.assert_history_withheld(result)

    def test_j1_unrelated_head_and_repository_histories_remain_eligible(self):
        for repository,sha in (('flightops/example','e'*40),('another/repository','a'*40)):
            with self.subTest(repository=repository):
                b=self.conflicted_ci();clean=fixture()
                h=copy.deepcopy(clean['heads'][0]);h.update(id='head:other',repository=repository,sha=sha,policy_id='policy:other',r0_receipt='opened:other')
                p=copy.deepcopy(clean['policies'][0]);p.update(id='policy:other',repository=repository,sha=sha)
                b['heads'].append(h);b['policies'].append(p)
                for c in clean['ci_attempts']:
                    c.update(id='other:'+c['id'],repository=repository,sha=sha);b['ci_attempts'].append(c)
                result=derive(b)
                self.assert_history_withheld(result)
                other=next(c for c in result['ci'] if c['head_id']=='head:other')
                self.assertEqual(other['first_attempt'],'fail');self.assertTrue(other['verified_r0_ci_eligible'])

    def test_j1_unrequired_job_conflict_does_not_taint_required_job(self):
        b=fixture();old=copy.deepcopy(b['ci_attempts'][0]);old.update(id='optional:1',name='optional lint')
        bad=copy.deepcopy(old);bad.update(revision=1,conclusion='SUCCESS');b['ci_attempts'].extend([old,bad])
        ci=derive(b)['ci'][0]
        self.assertEqual(ci['first_attempt'],'fail');self.assertTrue(ci['verified_r0_ci_eligible'])
        self.assertNotIn('check-history-conflict',ci['reasons'])

    def test_j1_duplicate_and_identity_conflicts_cover_original_scope(self):
        for change in ({'revision':0},{'sha':'e'*40},{'name':'different job'}):
            with self.subTest(change=change):
                b=self.conflicted_ci();b['ci_attempts'][-1].update(change)
                self.assert_history_withheld(derive(b))

    def test_j2_wrong_task_is_unmatched_with_or_without_valid_successor(self):
        for include_valid in (False,True):
            with self.subTest(include_valid=include_valid):
                b=self.successors()
                if not include_valid:b['assignments']=[b['assignments'][0],b['assignments'][2]]
                foreign=self.edge(derive(b),'foreign-successor')
                self.assertNotEqual(foreign['state'],'linked')
                self.assertIn('supersession-unresolved',foreign['reasons'])

    def test_j2_valid_pair_links_and_predecessor_remains_superseded(self):
        b=self.successors();b['assignments']=b['assignments'][:2]
        result=derive(b)
        self.assertEqual(self.edge(result,'valid-successor')['state'],'linked')
        self.assertIn('assignment-superseded',self.edge(result,'assignment:review')['reasons'])

    def test_j2_wrong_repo_pr_and_order_cannot_borrow_valid_relation(self):
        for variant in ('repository','pr','order'):
            with self.subTest(variant=variant):
                b=self.successors();old,valid,foreign=b['assignments'];foreign['task_id']=old['task_id']
                if variant=='order':foreign['assigned_at']=old['assigned_at']
                else:
                    h=copy.deepcopy(b['heads'][0]);h.update(id='head:other',r0_receipt='opened:other')
                    if variant=='pr':foreign['pr']=2;h['pr']=2
                    else:
                        foreign['repository']='another/repository';h['repository']=foreign['repository']
                        op=copy.deepcopy(b['operations'][0]);op.update(id='operation:other',repository=foreign['repository'],invocation_id='other',completion_invocation_id='other')
                        b['operations'].append(op)
                    b['heads'].append(h)
                foreign_edge=self.edge(derive(b),'foreign-successor')
                self.assertNotEqual(foreign_edge['state'],'linked')
                self.assertIn('supersession-unresolved',foreign_edge['reasons'])

    def test_j2_dangling_and_cycles_stay_excluded(self):
        b=self.successors();b['assignments'][2]['supersedes']='assignment:absent'
        foreign=self.edge(derive(b),'foreign-successor')
        self.assertNotEqual(foreign['state'],'linked');self.assertIn('supersession-unresolved',foreign['reasons'])
        b=self.successors();b['assignments'][0]['supersedes']='valid-successor'
        result=derive(b)
        self.assertTrue(all(e['state']!='linked' for e in result['edges'] if e['role']=='review'))


if __name__=='__main__':
    unittest.main()
