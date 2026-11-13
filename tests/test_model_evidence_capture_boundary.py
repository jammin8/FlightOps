from copy import deepcopy
from dataclasses import replace
from itertools import count
from pathlib import Path
import tempfile
import unittest

from collectors.model_evidence.contract import ContractError,digest,encode
from collectors.model_evidence.capture_contract import AssignmentIntent,DispatchObservation,OwnerBindingObservation,ExecutionStart,ExecutionResult,GitResult,AssignmentResolution
from collectors.model_evidence.capture_boundary import BoundaryRecorder,BoundaryPort,FixturePort,ExecReply
from collectors.model_evidence.capture_ledger import Ledger
from collectors.model_evidence.capture_projection import project,import_supplied
from test_model_evidence_capture_contract import fixture,rechained
import os as _os
import tempfile as _tempfile
_tempfile.tempdir = _os.path.realpath(_tempfile.gettempdir())


class Crash(BaseException):
    pass


class CaptureBoundary(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.ledger=Ledger(Path(self.temp.name)/'ledger','recorder:synthetic','epoch:synthetic').create()
        self.port=FixturePort([DispatchObservation(**fixture()['events'][1]['payload'])])
        ids=count();clock=count(1000)
        self.recorder=BoundaryRecorder(self.ledger,self.port,ids=lambda:str(next(ids)),wall_clock=lambda:next(clock),monotonic_clock=lambda:next(clock))

    def prepare(self,**changes):
        fields=fixture()['events'][0]['payload'];fields.update(changes)
        return self.recorder.prepare_assignment(AssignmentIntent(**fields))

    def bound(self,**changes):
        handle=self.prepare(**changes);self.recorder.dispatch(handle)
        owner=OwnerBindingObservation(**fixture()['events'][2]['payload'])
        self.recorder.observe_owner_binding(handle,self.port.receipt(handle,owner))
        return handle

    def operation(self,assignment=None,**changes):
        assignment=self.bound() if assignment is None else assignment
        fields=fixture()['events'][3]['payload'];fields['recorder_invocation_id']=None;fields.update(changes)
        return self.recorder.begin_operation(assignment,ExecutionStart(**fields))

    def finish(self,handle,exit_code=0,at=40):
        return self.recorder.observe_execution(handle,self.port.execution(handle,ExecReply('unified-exec',None,exit_code,None,at,None,[])))

    def test_intent_durable_before_one_dispatch_without_private_task(self):
        handle=self.prepare();snapshot=self.ledger.snapshot()
        self.assertEqual([e['kind'] for e in snapshot['events']],['assignment_intent'])
        self.assertEqual(self.port.dispatch_calls,0)
        self.recorder.dispatch(handle)
        self.assertEqual(self.port.dispatch_calls,1)
        self.assertEqual(self.port.descriptors,[{'thread_id':'thread:one','client_request_id':handle.identity}])
        with self.assertRaises(ContractError):self.recorder.dispatch(handle)
        self.assertEqual(self.port.dispatch_calls,1)

    def test_pending_dispatch_then_exact_later_owner_mapping(self):
        handle=self.prepare();facts=self.recorder.dispatch(handle)
        a=facts['assignments'][0];self.assertIsNone(a['dispatches'][0]['orchestration_run_id']);self.assertEqual(a['owner_state'],'unknown')
        owner=OwnerBindingObservation(**fixture()['events'][2]['payload'])
        facts=self.recorder.observe_owner_binding(handle,self.port.receipt(handle,owner))
        self.assertEqual(facts['assignments'][0]['owner_state'],'structurally-matched-unverified')
        self.assertEqual(facts['assignments'][0]['run_key'],'codex:attempt:one')
        self.assertIsNone(facts['assignments'][0]['intent']['pr'])

    def test_wrong_task_thread_attempt_turn_session_bindings_withheld(self):
        for field in ('task_id_from_transport','child_thread_id','attempt_id','orchestration_run_id','native_session_id'):
            rows=fixture()['events'];rows[2]['payload'][field]='wrong:identity'
            a=project(rows)['assignments'][0]
            self.assertEqual(a['owner_state'],'unknown',field)
        rows=fixture()['events'];rows[1]['payload']['orchestration_run_id']='run:one';rows[2]['payload']['task_id_from_transport']='wrong:task'
        self.assertEqual(project(rows)['assignments'][0]['owner_state'],'unknown')

    def test_opaque_handles_and_registered_port_receipts_not_json_flags(self):
        handle=self.prepare()
        with self.assertRaises(ContractError):self.recorder.dispatch({'identity':handle.identity,'qualified':True})
        with self.assertRaises(ContractError):BoundaryRecorder(self.ledger,{'adapter':'live'})
        other=FixturePort();owner=OwnerBindingObservation(**fixture()['events'][2]['payload'])
        with self.assertRaises(ContractError):self.recorder.observe_owner_binding(handle,other.receipt(handle,owner))
        with self.assertRaises(ContractError):self.recorder.observe_owner_binding(handle,owner.fields())

    def test_worker_finished_is_not_explicit_review_decision(self):
        handle=self.bound(role='review',reviewed_sha='a'*40)
        fields=fixture()['events'][6]['payload'];fields.update(resolution='unknown',review_outcome=None,reviewed_sha=None,resolved_at=None)
        facts=self.recorder.resolve_assignment(handle,self.port.receipt(handle,AssignmentResolution(**fields)))
        self.assertEqual(facts['assignments'][0]['decision_state'],'unknown')
        fields.update(resolution='completed',resolved_at=50)
        facts=self.recorder.resolve_assignment(handle,self.port.receipt(handle,AssignmentResolution(**fields)))
        self.assertIn('explicit-review-decision-missing',facts['assignments'][0]['reasons'])
        self.assertEqual(facts['assignments'][0]['decision_state'],'unknown')

    def test_explicit_review_decision_and_actor_labels_do_not_authenticate(self):
        handle=self.bound(role='review',reviewed_sha='a'*40,actor_binding_id='actor:claimed')
        fields=fixture()['events'][6]['payload'];fields.update(reviewed_sha='a'*40,review_outcome='changes_requested')
        facts=self.recorder.resolve_assignment(handle,self.port.receipt(handle,AssignmentResolution(**fields)))
        a=facts['assignments'][0];self.assertEqual(a['decision_state'],'explicit-unverified')
        self.assertIn('actor-independence-unproven',a['reasons']);self.assertEqual(facts['normalized_rows'],[])

    def test_continued_final_and_wrong_session_exec(self):
        handle=self.operation()
        progress=self.port.execution(handle,ExecReply('unified-exec',17,None,None,None,None,[]))
        facts=self.recorder.observe_execution(handle,progress)
        self.assertEqual(facts['operations'][0]['terminal_state'],'in_progress')
        with self.assertRaises(ContractError):self.port.execution(handle,ExecReply('unified-exec',None,0,None,40,None,[]),continuation_session=18)
        final=self.port.execution(handle,ExecReply('unified-exec',None,0,None,40,None,[]),continuation_session=17)
        facts=self.recorder.observe_execution(handle,final)
        self.assertEqual(facts['operations'][0]['terminal_state'],'success')
        self.assertEqual(facts['operations'][0]['results'][-1]['transport_session_id'],17)
        self.assertFalse(facts['operations'][0]['known']['process'])

    def test_immediate_exec_complete_without_pid_and_idempotent_final(self):
        handle=self.operation();facts=self.finish(handle)
        self.assertEqual(facts['operations'][0]['terminal_state'],'success')
        self.assertIsNone(facts['operations'][0]['results'][0]['process_identity'])
        before=self.ledger.snapshot()['events'];self.finish(handle)
        self.assertEqual(self.ledger.snapshot()['events'],before)

    def test_startup_ack_unknown_and_protocol_switch_cannot_fill_terminal(self):
        handle=self.operation(protocol='startup-ack')
        for protocol in ('startup-ack','unified-exec'):
            facts=self.recorder.observe_execution(handle,self.port.execution(handle,ExecReply(protocol,None,0,None,40,None,[])))
            self.assertEqual(facts['operations'][0]['terminal_state'],'unknown')
            self.assertIsNone(facts['operations'][0]['results'][0]['completed_at'])

    def test_contradictory_exec_shape_and_terminal_history_not_latest_wins(self):
        handle=self.operation()
        facts=self.recorder.observe_execution(handle,self.port.execution(handle,ExecReply('unified-exec',17,0,None,40,None,[])))
        self.assertEqual(facts['operations'][0]['terminal_state'],'unknown')
        handle=self.operation(self.recorder.assignments[handle.assignment]);self.finish(handle,0)
        facts=self.finish(handle,1,41)
        op=next(o for o in facts['operations'] if o['operation_id']==handle.identity)
        self.assertEqual(op['terminal_state'],'unknown');self.assertIn('operation-terminal-conflict',op['reasons'])
        self.assertEqual(len(op['results']),2)

    def test_git_creation_observation_consistency_not_production_authority(self):
        handle=self.operation();self.finish(handle)
        record=GitResult(**fixture()['events'][5]['payload'])
        facts=self.recorder.observe_git_result(handle,self.port.receipt(handle,record))
        observed=facts['operations'][0]['git_observations'][0]
        self.assertEqual(observed['observation']['result_sha'],'b'*40)
        self.assertEqual(observed['dispositions'],['git-observation-unverified'])
        self.assertEqual(facts['normalized_rows'],[])

    def test_git_wrong_repository_parent_reuse_noop_and_partial_never_prove(self):
        for change,reason in [({'repository':'other/project'},'git-repository-mismatch'),({'parent_shas':['e'*40]},'creation-parent-unproven'),
                              ({'object_existed_before':True,'outcome':'reused'},'object-preexisting'),
                              ({'result_sha':'a'*40,'outcome':'no-op'},'git-no-op'),({'outcome':'partial'},'git-partial'),
                              ({'dry_run':True},'git-dry-run'),({'witness_kind':'unsupported'},'unsupported-git-witness')]:
            rows=fixture()['events'];rows[5]['payload'].update(change)
            observed=project(rows)['operations'][0]['git_observations'][0]
            self.assertIn(reason,observed['dispositions'],change)
        rows=fixture()['events'];rows[5]['payload']['result_sha']=None
        self.assertIn('creation-result-unavailable',project(rows)['operations'][0]['git_observations'][0]['dispositions'])

    def test_publication_is_distinct_and_exact_destination_result_required(self):
        rows=fixture()['events'];start=rows[3]['payload'];result=rows[5]['payload']
        start.update(operation_kind='git-publish',intended_object_sha='b'*40,destination_repository='example/project',intended_destination_ref='refs/heads/feature')
        result.update(witness_kind='ref-publication',destination_repository='example/project',destination_ref='refs/heads/feature',ref_before_sha='a'*40,ref_result_sha='b'*40,outcome='published')
        good=project(rows)['operations'][0]['git_observations'][0]
        self.assertEqual(good['dispositions'],['git-observation-unverified'])
        result['destination_repository']='other/project'
        self.assertIn('publication-destination-mismatch',project(rows)['operations'][0]['git_observations'][0]['dispositions'])
        result['destination_repository']='example/project';result['ref_before_sha']='e'*40
        self.assertIn('publication-before-mismatch',project(rows)['operations'][0]['git_observations'][0]['dispositions'])

    def test_original_dispatch_worker_decision_clocks_and_result_target_preserved(self):
        rows=fixture()['events'];result=project(rows);a=result['assignments'][0]
        self.assertEqual(a['dispatches'][0]['request_observed_at'],20)
        self.assertEqual(a['resolutions'][0]['observed_at'],51)
        self.assertEqual(a['resolutions'][0]['worker_completed_at'],45)
        self.assertIsNone(a['intent']['pr']);self.assertEqual(a['resolutions'][0]['result_pr'],1)
        self.assertEqual((a['intent']['input_sha'],a['resolutions'][0]['result_sha']),('a'*40,'b'*40))
        for reason in ('v1-dispatch-clock-not-worker-clock','v1-result-observed-after-worker','v1-input-result-heads-differ'):
            self.assertIn(reason,a['reasons'])
        self.assertEqual(result['events'],rows)

    def test_valid_successor_cannot_validate_wrong_task_successor(self):
        for include_valid in (False,True):
            rows=fixture()['events'];old=rows[0]
            good=deepcopy(old);good.update(event_id='event:valid',assignment_id='assignment:valid',recorded_at=200)
            good['payload']['supersedes_assignment_id']=old['assignment_id']
            wrong=deepcopy(good);wrong.update(event_id='event:wrong',assignment_id='assignment:wrong',recorded_at=201)
            wrong['payload']['task_id']='work:unrelated'
            rows+=([good] if include_valid else [])+[wrong]
            facts=project(rechained(rows));by={a['assignment_id']:a for a in facts['assignments']}
            self.assertIn('supersession-unresolved',by['assignment:wrong']['reasons']);self.assertFalse(by['assignment:wrong']['consistent'])
            if include_valid:
                self.assertNotIn('supersession-unresolved',by['assignment:valid']['reasons'])
                self.assertIn('assignment-superseded',by['assignment:one']['reasons'])

    def test_wrong_repo_pr_target_time_and_cycles_stay_unresolved(self):
        for change in ({'repository':'other/project'},{'pr':99},{'input_sha':'e'*40},{'supersedes_assignment_id':'assignment:absent'}):
            rows=fixture()['events'];copy=deepcopy(rows[0]);copy.update(event_id='event:next',assignment_id='assignment:next',recorded_at=200)
            copy['payload'].update({'supersedes_assignment_id':'assignment:one',**change})
            a=next(a for a in project(rechained(rows+[copy]))['assignments'] if a['assignment_id']=='assignment:next')
            self.assertIn('supersession-unresolved',a['reasons'])
        rows=fixture()['events'];rows[0]['payload']['supersedes_assignment_id']='assignment:one'
        self.assertIn('assignment-dependency-cycle',project(rows)['assignments'][0]['reasons'])
        rows=fixture()['events'];copy=deepcopy(rows[0]);copy.update(event_id='event:early',assignment_id='assignment:early',recorded_at=100)
        copy['payload']['supersedes_assignment_id']='assignment:one'
        self.assertIn('supersession-unresolved',next(a for a in project(rechained(rows+[copy]))['assignments'] if a['assignment_id']=='assignment:early')['reasons'])

    def test_unrelated_operation_survives_terminal_conflict(self):
        rows=fixture()['events'];other=[]
        for original in rows[3:5]:
            row=deepcopy(original);row.update(event_id='other:'+row['event_id'],operation_id='operation:other',recorded_at=200+len(other))
            row['payload']['recorder_invocation_id']='invocation:other';other.append(row)
        bad=deepcopy(rows[4]);bad.update(event_id='event:contradiction',recorded_at=202);bad['payload'].update(terminal_state='failure',exit_code=1,completed_at=41)
        facts=project(rechained(rows+other+[bad]));by={o['operation_id']:o for o in facts['operations']}
        self.assertEqual(by['operation:one']['terminal_state'],'unknown');self.assertEqual(by['operation:other']['terminal_state'],'success')

    def test_crash_before_or_after_action_never_retries(self):
        for point,counted in (('before-action',0),('after-action-before-receipt',1)):
            handle=self.prepare()
            def crash(at):
                if at==point:raise Crash()
            self.recorder.crash=crash
            before=self.port.dispatch_calls
            with self.assertRaises(Crash):self.recorder.dispatch(handle)
            self.assertEqual(self.port.dispatch_calls-before,counted)
            with self.assertRaises(ContractError):self.recorder.dispatch(handle)
            facts=self.recorder.facts();a=next(a for a in facts['assignments'] if a['assignment_id']==handle.identity)
            self.assertEqual(a['dispatches'],[])

    def test_append_failure_after_action_preserves_pending_intent_without_retry(self):
        handle=self.prepare()
        original=self.port.dispatch
        def dispatch(descriptor,h):
            receipt=original(descriptor,h)
            def crash(at):
                if at=='after-segment-before-manifest':raise Crash()
            self.ledger.crash=crash
            return receipt
        self.port.dispatch=dispatch
        with self.assertRaises(Crash):self.recorder.dispatch(handle)
        self.assertEqual(self.port.dispatch_calls,1)
        with self.assertRaises(ContractError):self.recorder.dispatch(handle)
        snapshot=self.ledger.snapshot();self.assertEqual(len(snapshot['events']),1)
        self.assertIn('unreferenced-ledger-entry',snapshot['gaps'])

    def test_unavailable_port_failure_has_no_raw_exception_body_or_retry(self):
        handle=self.prepare()
        def broken(descriptor,h):raise RuntimeError('PRIVATE-ERROR-BODY')
        self.port.dispatch=broken
        with self.assertRaises(ContractError) as error:self.recorder.dispatch(handle)
        self.assertEqual(str(error.exception),'capture-dispatch-result-unavailable')
        with self.assertRaises(ContractError):self.recorder.dispatch(handle)

    def test_intent_append_failure_prevents_dispatch_and_missing_port_has_no_fallback(self):
        def crash(at):
            if at=='before-segment':raise Crash()
        self.ledger.crash=crash
        with self.assertRaises(Crash):self.prepare()
        self.assertEqual(self.port.dispatch_calls,0)
        self.ledger.crash=lambda _:None
        self.recorder.port=BoundaryPort();handle=self.prepare()
        with self.assertRaises(ContractError):self.recorder.dispatch(handle)
        self.assertEqual(self.port.dispatch_calls,0)

    def test_requested_owner_binding_and_client_request_mismatch_not_ignored(self):
        rows=fixture()['events'];rows[0]['payload']['requested_owner_binding_id']='owner:missing'
        a=project(rows)['assignments'][0]
        self.assertEqual(a['owner_state'],'unknown');self.assertIn('requested-owner-binding-unresolved',a['reasons'])
        rows=fixture()['events'];rows[1]['payload']['client_request_id']='client:wrong'
        self.assertFalse(project(rows)['assignments'][0]['consistent'])

    def test_explicit_resolution_revision_without_predecessor_is_unresolved(self):
        rows=fixture()['events'];rows[6]['payload']['revision']=1
        self.assertIn('resolution-revision-unresolved',project(rows)['assignments'][0]['reasons'])
        rows=fixture()['events'];old=deepcopy(rows[6]);old['payload'].update(resolution='unknown',resolved_at=None)
        revised=deepcopy(rows[6]);revised.update(event_id='event:revision',recorded_at=200,dependencies=[old['event_id']]);revised['payload']['revision']=1
        rows[6]=old;rows.append(revised)
        a=project(rechained(rows))['assignments'][0]
        self.assertNotIn('resolution-revision-unresolved',a['reasons'])
        self.assertEqual(a['decision_state'],'explicit-unverified')

    def test_rework_requires_qualified_explicit_changes_request_on_input_head(self):
        rows=fixture()['events'];rows[0]['payload'].update(role='review',reviewed_sha='a'*40)
        rows[6]['payload'].update(reviewed_sha='a'*40,review_outcome='changes_requested')
        rework=deepcopy(rows[0]);rework.update(event_id='event:rework',assignment_id='assignment:rework',recorded_at=200)
        rework['payload'].update(role='rework',reviewed_sha=None,trigger_kind='review',trigger_id='assignment:one')
        a=next(a for a in project(rechained(rows+[rework]))['assignments'] if a['assignment_id']=='assignment:rework')
        self.assertNotIn('review-trigger-unqualified',a['reasons'])
        rows[6]['payload']['terminal_run_id']='run:wrong'
        a=next(a for a in project(rechained(rows+[rework]))['assignments'] if a['assignment_id']=='assignment:rework')
        self.assertIn('review-trigger-unqualified',a['reasons']);self.assertFalse(a['consistent'])


class CaptureLifecycleBindings(unittest.TestCase):
    setUp=CaptureBoundary.setUp
    prepare=CaptureBoundary.prepare
    bound=CaptureBoundary.bound
    operation=CaptureBoundary.operation

    def terminals(self):
        return (('success',0,None),('failure',1,None),('cancelled',None,15))

    def continued(self,assignment):
        handle=self.operation(assignment)
        self.recorder.observe_execution(handle,self.port.execution(handle,ExecReply('unified-exec',17,None,None,None,None,[])))
        return handle

    def replay(self,state,exit_code,signal,session):
        rows=fixture()['events'];terminal=rows[4]
        terminal['payload'].update(terminal_state=state,exit_code=exit_code,signal=signal,transport_session_id=session,
            process_identity={'kind':'transport-session','protocol':'unified-exec','session_id':session} if session is not None else None)
        progress=deepcopy(terminal);progress.update(event_id='event:synthetic-progress',recorded_at=104.5)
        progress['payload'].update(terminal_state='in_progress',exit_code=None,signal=None,completed_at=None,transport_session_id=17,
            process_identity={'kind':'transport-session','protocol':'unified-exec','session_id':17})
        rows.insert(4,progress)
        return rechained(rows)

    def test_b1_fixture_port_refuses_missing_terminal_binding_for_all_outcomes(self):
        assignment=self.bound()
        for state,exit_code,signal in self.terminals():
            with self.subTest(outcome=state):
                handle=self.continued(assignment);before=self.ledger.snapshot()
                with self.assertRaisesRegex(ContractError,'capture-continuation-binding-missing'):
                    self.port.execution(handle,ExecReply('unified-exec',None,exit_code,signal,40,None,[]))
                self.assertEqual(self.ledger.snapshot(),before)

    def test_b1_other_registered_adapter_cannot_omit_terminal_binding(self):
        assignment=self.bound()
        for state,exit_code,signal in self.terminals():
            with self.subTest(outcome=state):
                handle=self.continued(assignment);fields=fixture()['events'][4]['payload']
                fields.update(recorder_invocation_id=handle.invocation,terminal_state=state,exit_code=exit_code,signal=signal)
                receipt=self.port.receipt(handle,ExecutionResult(**fields));before=self.ledger.snapshot()
                with self.assertRaisesRegex(ContractError,'capture-continuation-binding-missing'):self.recorder.observe_execution(handle,receipt)
                self.assertEqual(self.ledger.snapshot(),before)

    def test_b1_supplied_replay_missing_binding_stays_unknown_without_filling_session(self):
        for state,exit_code,signal in self.terminals():
            original=self.replay(state,exit_code,signal,None)
            for reversed_input in (False,True):
                rows=list(reversed(original)) if reversed_input else original
                for supplied in (False,True):
                    with self.subTest(outcome=state,reversed_input=reversed_input,supplied=supplied):
                        result=import_supplied(encode({'schema':'flightops-boundary-import/1','label':'SYNTHETIC','events':rows})) if supplied else project(rows)
                        op=result['operations'][0]
                        self.assertEqual(op['terminal_state'],'unknown')
                        self.assertIn('continuation-binding-missing',op['reasons'])
                        self.assertIn('execution-success-unproven',op['git_observations'][0]['dispositions'])
                        self.assertEqual(result['events'],rows)
                        self.assertIsNone(next(r for r in op['results'] if r['terminal_state']==state)['transport_session_id'])
                        self.assertEqual(next(r for r in op['results'] if r['terminal_state']==state)['completed_at'],40)
                        self.assertEqual(result['normalized_rows'],[]);self.assertEqual(result['authentication'],'not-performed')

    def test_b1_immediate_and_explicit_matching_terminals_preserve_all_outcomes(self):
        assignment=self.bound()
        for state,exit_code,signal in self.terminals():
            immediate=self.operation(assignment)
            facts=self.recorder.observe_execution(immediate,self.port.execution(immediate,ExecReply('unified-exec',None,exit_code,signal,40,None,[])))
            op=next(o for o in facts['operations'] if o['operation_id']==immediate.identity)
            self.assertEqual(op['terminal_state'],state);self.assertIsNone(op['results'][0]['process_identity'])
            handle=self.continued(assignment)
            facts=self.recorder.observe_execution(handle,self.port.execution(handle,ExecReply('unified-exec',None,exit_code,signal,40,None,[]),continuation_session=17))
            op=next(o for o in facts['operations'] if o['operation_id']==handle.identity)
            self.assertEqual(op['terminal_state'],state);self.assertEqual([p['transport_session_id'] for p in op['results']],[17,17])
            for rows in (self.replay(state,exit_code,signal,17),list(reversed(self.replay(state,exit_code,signal,17)))):
                op=project(rows)['operations'][0]
                self.assertEqual(op['terminal_state'],state);self.assertNotIn('continuation-binding-missing',op['reasons'])

    def test_b1_wrong_continuation_and_startup_controls_remain_conservative(self):
        assignment=self.bound();handle=self.continued(assignment)
        with self.assertRaisesRegex(ContractError,'capture-continuation-mismatch'):
            self.port.execution(handle,ExecReply('unified-exec',None,0,None,40,None,[]),continuation_session=18)
        result=project(self.replay('success',0,None,18));self.assertEqual(result['operations'][0]['terminal_state'],'unknown')
        self.assertIn('transport-session-conflict',result['operations'][0]['reasons'])
        startup=self.operation(assignment,protocol='startup-ack')
        result=self.recorder.observe_execution(startup,self.port.execution(startup,ExecReply('startup-ack',None,0,None,40,None,[])))
        op=next(o for o in result['operations'] if o['operation_id']==startup.identity)
        self.assertEqual(op['terminal_state'],'unknown')

    def scoped(self,kind,existing):
        rows=fixture()['events']
        next(e for e in rows if e['kind']==kind)['assignment_id']='assignment:other'
        if existing:
            other=deepcopy(rows[0]);other.update(event_id='event:synthetic-other-intent',assignment_id='assignment:other',recorded_at=200)
            other['payload'].update(task_id='work:other',target_thread_id='thread:other');rows.append(other)
        return rechained(rows)

    def test_b2_existing_or_missing_assignment_scope_conflicts_taint_each_stage(self):
        for kind in ('execution_started','execution_result','git_result'):
            for existing in (False,True):
                original=self.scoped(kind,existing)
                for reversed_input in (False,True):
                    with self.subTest(kind=kind,existing=existing,reversed_input=reversed_input):
                        rows=list(reversed(original)) if reversed_input else original;result=project(rows);op=result['operations'][0]
                        self.assertEqual(op['terminal_state'],'unknown')
                        self.assertIn('operation-assignment-conflict',op['reasons'])
                        self.assertIn('execution-success-unproven',op['git_observations'][0]['dispositions'])
                        self.assertEqual(result['events'],rows);self.assertEqual(result['normalized_rows'],[])
                        self.assertEqual(result['denominators']['authenticated_rows'],0)

    def test_b2_unrelated_operation_and_assignment_decision_remain_usable(self):
        rows=self.scoped('execution_result',False)
        clean=[]
        for original in fixture()['events'][3:6]:
            other=deepcopy(original);other.update(event_id='clean:'+other['event_id'],operation_id='operation:clean',recorded_at=200+len(clean))
            if 'recorder_invocation_id' in other['payload']:other['payload']['recorder_invocation_id']='invocation:clean'
            clean.append(other)
        rows=rechained(rows+clean)
        for ordered in (rows,list(reversed(rows))):
            result=project(ordered);by={o['operation_id']:o for o in result['operations']}
            self.assertEqual(by['operation:clean']['terminal_state'],'success')
            self.assertNotIn('operation-assignment-conflict',by['operation:clean']['reasons'])
            self.assertEqual(by['operation:clean']['git_observations'][0]['dispositions'],['git-observation-unverified'])
            self.assertEqual(next(a for a in result['assignments'] if a['assignment_id']=='assignment:one')['decision_state'],'explicit-unverified')
            self.assertEqual(by['operation:one']['terminal_state'],'unknown')
            self.assertEqual(result['denominators']['selected_events'],len(rows))

    def test_b2_same_assignment_positive_and_absent_scope_stays_explicit_unknown(self):
        baseline=project(fixture()['events'])
        self.assertEqual(baseline['operations'][0]['terminal_state'],'success')
        self.assertNotIn('operation-assignment-conflict',baseline['operations'][0]['reasons'])
        for all_absent in (False,True):
            rows=fixture()['events']
            for row in rows:
                if row['kind']=='execution_result' or all_absent and row['operation_id'] is not None:row['assignment_id']=None
            result=project(rechained(rows));op=result['operations'][0]
            self.assertIn('operation-assignment-unknown',op['reasons'])
            self.assertNotIn('operation-assignment-conflict',op['reasons'])
            self.assertEqual(op['terminal_state'],'success')
            self.assertEqual([e['assignment_id'] for e in result['events']],[e['assignment_id'] for e in rows])
            self.assertEqual(result['as_of'],baseline['as_of'])
            self.assertEqual(result['head_observations'],baseline['head_observations'])
            self.assertEqual(result['normalized_rows'],[])


if __name__=='__main__':
    unittest.main()
