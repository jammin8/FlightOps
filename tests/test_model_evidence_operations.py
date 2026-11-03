import copy
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from collectors.model_evidence import export_bundle
from collectors.model_evidence import shell
from test_model_evidence_core import bundle, evidence

A='a'*40
B='b'*40


class Operations(unittest.TestCase):
    def operation(self, command, output=None, **changes):
        b=bundle(); b['calls'][0]['command']=command; b['calls'][0].update(changes)
        if output is not None: b['completions'][0]['output']=output
        return export_bundle(b)

    def test_literal_quoted_scalar_and_tab_controls(self):
        for cmd in ('git commit -m "$MSG" && git rev-parse HEAD', "git commit -m 'literal $@' && git rev-parse HEAD",
                    'git\tcommit -m literal && git rev-parse HEAD'):
            with self.subTest(cmd=cmd): self.assertEqual(self.operation(cmd)['operations'][0]['sha'],A)

    def test_quoted_multiargument_expansions_are_not_scalar(self):
        for word in ('"$@"','"${arr[@]}"','"${unknown:-$@}"'):
            cmd='set -- ignored --dry-run >/dev/null 2>&1 && git commit -m '+word+' && git rev-parse HEAD'
            with self.subTest(word=word): self.assertEqual(self.operation(cmd, A+'\n')['operations'],[])

    def test_executed_bash_dryrun_noop_and_actual_creation_control(self):
        with tempfile.TemporaryDirectory() as d:
            env={**os.environ,'HOME':d,'XDG_CONFIG_HOME':d,'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':os.devnull,
                 'GIT_AUTHOR_NAME':'fixture','GIT_AUTHOR_EMAIL':'fixture@example.invalid',
                 'GIT_COMMITTER_NAME':'fixture','GIT_COMMITTER_EMAIL':'fixture@example.invalid'}
            env={k:v for k,v in env.items() if not k.startswith('GIT_') or k in (
                'GIT_CONFIG_NOSYSTEM','GIT_CONFIG_GLOBAL','GIT_AUTHOR_NAME','GIT_AUTHOR_EMAIL','GIT_COMMITTER_NAME','GIT_COMMITTER_EMAIL')}
            def git(*args):
                return subprocess.check_output(['git','-c','core.hooksPath=/dev/null',*args],cwd=d,env=env,text=True,stderr=subprocess.STDOUT)
            git('init','-b','main'); git('config','core.hooksPath','/dev/null'); env['MSG']='message'; Path(d,'f').write_text('initial'); git('add','f'); git('commit','-m','initial')
            before=git('rev-parse','HEAD').strip()
            Path(d,'f').write_text('changed'); git('add','f')
            dry='set -- ignored --dry-run >/dev/null 2>&1 && git commit -m "$@" && git rev-parse HEAD'
            result=subprocess.run(['bash','--noprofile','--norc','-c',dry],cwd=d,env=env,text=True,capture_output=True)
            self.assertEqual(result.returncode,0); self.assertEqual(git('rev-parse','HEAD').strip(),before)
            self.assertIsNone(shell.produced_by(dry,result.stdout+result.stderr,[before],d))
            control='git commit -m "$MSG" && git rev-parse HEAD'
            result=subprocess.run(['bash','--noprofile','--norc','-c',control],cwd=d,env=env,text=True,capture_output=True)
            after=git('rev-parse','HEAD').strip()
            self.assertEqual(result.returncode,0); self.assertNotEqual(before,after)
            self.assertEqual(shell.produced_by(control,result.stdout+result.stderr,[before,after],d),after)

    def test_executed_pr_title_expansion_selects_other_head_but_not_a_receipt(self):
        cmd='set -- title -Hother >/dev/null 2>&1 && gh pr create -t "$@" --body body'
        result=subprocess.run(['bash','--noprofile','--norc','-c',
                               'gh() { printf "%s\\n" "$@"; }; '+cmd],capture_output=True,text=True,check=True)
        self.assertIn('-Hother',result.stdout.splitlines())
        ops=shell.operations(cmd,'/fixture/work')
        self.assertEqual(ops[-1]['op'],'unresolved')
        safe=shell.operations('gh pr create -t "$TITLE" --body body','/fixture/work')
        self.assertEqual(safe[0]['op'],'pr-create')
        self.assertEqual(self.operation(cmd,'https://github.com/flightops/demo/pull/7')['operations'],[])

    def test_noop_dryrun_other_repo_and_unsupported_forms(self):
        commands=('git commit --dry-run && git rev-parse HEAD','git merge --ff-only main && git rev-parse HEAD',
                  'git merge main && git rev-parse HEAD','git -C /another commit -m x && git rev-parse HEAD',
                  'git commit -m x; git rev-parse HEAD','echo "[main aaaaaaa] x" && git commit -m x',
                  'git commit -m $(cat /secret) && git rev-parse HEAD','git commit -m x | cat',
                  'GIT_DIR=/another git commit -m x && git rev-parse HEAD')
        for cmd in commands:
            with self.subTest(cmd=cmd): self.assertEqual(self.operation(cmd,A+'\n')['operations'],[])

    def test_unsupported_abbreviated_and_implicit_dryrun_options_stay_unknown(self):
        for command in ('git commit --dry-r && git rev-parse HEAD',
                        'git commit --porcelain && git rev-parse HEAD',
                        'git commit --amend --no-edit && git rev-parse HEAD',
                        'git push --dry-r origin main'):
            with self.subTest(command=command):
                self.assertEqual(self.operation(command,A+'\n')['operations'],[])


    def test_abbreviation_collision_and_full_object_control(self):
        b=bundle(); b['repositories']['flightops/demo']['objects'].append('aaaaaaa'+'c'*33)
        b['calls'][0]['command']='git commit -m x'; b['completions'][0]['output']='[main aaaaaaa] x\n'
        self.assertEqual(export_bundle(b)['operations'],[])
        b['calls'][0]['command']='git commit -m x && git rev-parse HEAD'
        b['completions'][0]['output']='[main aaaaaaa] x\n'+A+'\n'
        self.assertEqual(export_bundle(b)['operations'][0]['sha'],A)

    def test_completion_identity_missing_wrong_session_and_late_final(self):
        for field,value in (('native_call_id','wrong'),('native_session_id','wrong'),('exit_code',None),
                            ('process_session_id','different'),('final',False)):
            b=bundle(); b['completions'][0][field]=value
            with self.subTest(field=field): self.assertEqual(export_bundle(b)['operations'],[])
        b=bundle(); running=copy.deepcopy(b['completions'][0]); running['final']=False; running['exit_code']=None
        running['output']='running'; b['completions'].insert(0,running)
        out=export_bundle(b)['operations'][0]
        self.assertEqual(out['completion_id'],'completion-1')
        b['completions'].append(copy.deepcopy(b['completions'][-1]))
        self.assertEqual(export_bundle(b)['operations'],[])

    def test_push_destination_is_actual_ref_not_requested_pr_or_head(self):
        out=self.operation('git push origin HEAD:other', 'aaaaaaa..bbbbbbb HEAD -> other\n')
        self.assertEqual(out['operations'][0]['destination_ref'],'refs/heads/other')
        self.assertEqual(out['operations'][0]['sha'],B)
        for cmd,output in (('git push -fn origin main','aaaaaaa..bbbbbbb main -> main'),
                           ('git push origin main','! [rejected] main -> main'),
                           ('git push origin main','* [new branch] main -> main')):
            self.assertEqual(self.operation(cmd,output)['operations'],[])

    def late_child_bundle(self):
        b=bundle(); parent=b['runs'][0]
        parent['expected_children']=['codex:child']; parent['turns'][0]['has_subagents']=True
        child=copy.deepcopy(parent); child.update(native_run_id='child',native_session_id='session-child',expected_children=[],
                                                parent={'provider_namespace':'codex','native_run_id':'r1','native_turn_id':'turn-r1','evidence':evidence()})
        child['turns'][0].update(native_turn_id='turn-child',has_subagents=False,usage_scope='child')
        newer=copy.deepcopy(parent); newer.update(native_run_id='r2',native_session_id='session-new',expected_children=[])
        newer['turns'][0].update(native_turn_id='turn-r2',has_subagents=False)
        b['runs'] += [child,newer]
        b['calls'][0].update(native_run_id='child',native_session_id='session-child',native_turn_id='turn-child',
                             command='git\tcommit --allow-empty -m child')
        b['completions'][0].update(native_session_id='session-child',output='observer-only, no commit line')
        b['observations']=[{'provider_namespace':'codex','native_call_id':'call-1','native_session_id':'session-child',
                            'repository':'flightops/demo','completion_id':'completion-1','before_sha':B,'after_sha':A,
                            'parents':[B],'evidence':evidence(905,3)}]
        return b

    def test_late_identified_child_observer_only_call_retains_parent(self):
        b=self.late_child_bundle(); out=export_bundle(b)
        self.assertEqual(len(out['operations']),1)
        op=out['operations'][0]
        self.assertEqual((op['owning_run_id'],op['root_run_id']),('codex:child','codex:r1'))
        self.assertNotEqual(op['root_run_id'],'codex:r2')
        b['calls'][0]['command']=b['calls'][0]['command'].replace('\t',' ')
        self.assertEqual(export_bundle(b)['operations'][0]['root_run_id'],'codex:r1')

    def test_unresolved_child_no_timestamp_fallback_and_noop_observation_veto(self):
        b=self.late_child_bundle(); b['runs'][1]['parent']['native_run_id']='missing-or-ambiguous'
        self.assertEqual(export_bundle(b)['operations'],[])
        b=self.late_child_bundle(); b['observations'][0]['before_sha']=A
        self.assertEqual(export_bundle(b)['operations'],[])
        b=self.late_child_bundle(); b['calls'][0]['command']='git commit -m "$@"'
        self.assertEqual(export_bundle(b)['operations'],[])

    def test_conflicting_producer_owners_are_ambiguous(self):
        b=bundle(); r=copy.deepcopy(b['runs'][0]); r.update(native_run_id='r2',native_session_id='session-r2'); r['turns'][0]['native_turn_id']='turn-r2'
        b['runs'].append(r)
        c=copy.deepcopy(b['calls'][0]); c.update(native_run_id='r2',native_session_id='session-r2',native_turn_id='turn-r2',native_call_id='call-2')
        done=copy.deepcopy(b['completions'][0]); done.update(native_session_id='session-r2',native_call_id='call-2',completion_id='completion-2')
        b['calls'].append(c); b['completions'].append(done)
        ops=export_bundle(b)['operations']
        self.assertEqual(len(ops),2); self.assertTrue(all(o['state']=='ambiguous-owner' for o in ops))


if __name__ == '__main__': unittest.main()
