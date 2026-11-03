from copy import deepcopy
import json
from pathlib import Path
import stat
import tempfile
import types
import unittest
from unittest.mock import patch

from collectors.model_evidence import owner_lookup as lookup
from collectors.model_evidence import owner_lookup_contract as contract
from collectors.model_evidence.contract import encode

CASE_RECEIPTS=[]
FIXTURE=Path(lookup.__file__).parents[2]/'tests/fixtures/model-evidence-owner-lookup-synthetic.json'


class OwnerLookupPublicationRepairTests(unittest.TestCase):
    def publication(self,case):
        bundle=json.loads(FIXTURE.read_bytes())
        old=lookup.derive_owner_lookup(bundle['selector'],bundle['rows'],bundle['acquisition_observations'])
        observations=deepcopy(bundle['acquisition_observations'])
        observations['acquisition_started_at']=100001
        observations['acquisition_completed_at']=100002
        new=lookup.derive_owner_lookup(bundle['selector'],bundle['rows'],observations)
        self.assertNotEqual(encode(old),encode(new))
        self.assertEqual(old['selector'],new['selector'])
        state={'case':case,'origin':'SYNTHETIC','now':0,'pending':False,'reads':0,'verifications':0,
            'replace_calls':0,'file_fsyncs':0,'directory_fsyncs':0,'events':[]}
        marker=contract.LookupCancelled()
        original_read=lookup._read_existing
        original_replace=lookup.os.replace
        original_fsync=lookup.os.fsync
        original_stat=lookup.os.stat
        def callback():
            state['events'].append('guard')
            if state['pending']:raise marker
        def read(fd,name):
            result=original_read(fd,name)
            state['reads']+=1;state['events'].append('prior-read-'+str(state['reads']))
            if state['reads']==2:
                if case=='deadline-final-read':state['now']=6
                if case=='cancel-final-read':state['pending']=True
                if case=='prior-identity-change':
                    raw,identity=result
                    result=raw,types.SimpleNamespace(st_dev=identity.st_dev,st_ino=identity.st_ino+1)
            return result
        def replace(*args,**kwargs):
            state['replace_calls']+=1;state['events'].append('replace')
            if case=='replace-failure':raise OSError('SYNTHETIC-replace-failure')
            result=original_replace(*args,**kwargs)
            if case=='deadline-after-replace':state['now']=6
            if case=='cancel-after-replace':state['pending']=True
            return result
        def fsync(fd):
            directory=stat.S_ISDIR(lookup.os.fstat(fd).st_mode)
            state['directory_fsyncs' if directory else 'file_fsyncs']+=1
            state['events'].append('directory-fsync' if directory else 'file-fsync')
            if directory and case=='fsync-after-replace':raise OSError('SYNTHETIC-fsync-failure')
            return original_fsync(fd)
        def code_stat(path,**kwargs):
            result=original_stat(path,**kwargs)
            if case=='code-change-final-read' and state['reads']>=2 and Path(path)==Path(lookup.__file__):
                return types.SimpleNamespace(st_dev=result.st_dev,st_ino=result.st_ino+1,st_size=result.st_size,
                    st_mtime_ns=result.st_mtime_ns,st_ctime_ns=result.st_ctime_ns)
            return result
        with tempfile.TemporaryDirectory(prefix='SYNTHETIC-owner-publish-') as directory:
            root=Path(directory);destination=root/'sidecar.json';destination.write_bytes(encode(old))
            with lookup._CodeLease(lambda:None) as lease:
                original_verify=lease.verify
                state['leased_files']=len(lease.entries)
                def verify():
                    state['verifications']+=1;state['events'].append('lease-verify')
                    original_verify()
                    if state['reads']>=2:
                        if case=='deadline-final-lease':state['now']=6
                        if case=='cancel-final-lease':state['pending']=True
                with patch.object(lookup.time,'monotonic',side_effect=lambda:state['now']), \
                        patch.object(lookup,'_read_existing',side_effect=read), \
                        patch.object(lookup.os,'replace',side_effect=replace), \
                        patch.object(lookup.os,'fsync',side_effect=fsync), \
                        patch.object(lookup.os,'stat',side_effect=code_stat), \
                        patch.object(lease,'verify',side_effect=verify):
                    guard=lookup._Budget(callback)
                    if case=='deadline-early':state['now']=6
                    if case=='cancel-early':state['pending']=True
                    state['outcome']='returned';caught=None
                    try:lookup._publish(destination,new,guard,lease)
                    except contract.LookupCancelled as error:
                        caught=error;state['outcome']='cancelled'
                        state['cancellation_type']=type(error).__name__
                        state['cancellation_args']=list(error.args)
                        state['same_cancellation_instance']=error is marker
                    except contract.LookupError as error:
                        caught=error;state['outcome']='error';state['error_code']=error.code
            state['lease_entries_after_close']=len(lease.entries)
            raw=destination.read_bytes()
            state['old_preserved']=raw==encode(old)
            state['new_present']=raw==encode(new)
            state['valid_readback']=contract.accept_result(raw) in (old,new)
            state['temporary_files_remaining']=len(list(root.glob('.owner-lookup-*')))
            state['destination_files']=sorted(p.name for p in root.iterdir())
            CASE_RECEIPTS.append(deepcopy(state))
            self.assertEqual(state['lease_entries_after_close'],0)
            self.assertEqual(state['temporary_files_remaining'],0)
            self.assertEqual(state['destination_files'],['sidecar.json'])
            self.assertTrue(state['valid_readback'])
            self.assertLessEqual(state['replace_calls'],1)
            return state,caught

    def assert_before_replace_cancelled(self,case,deadline=False):
        state,error=self.publication(case)
        self.assertEqual(state['replace_calls'],0,state)
        self.assertTrue(state['old_preserved'],state)
        self.assertFalse(state['new_present'],state)
        self.assertIsInstance(error,contract.LookupCancelled,state)
        if deadline:self.assertEqual(error.args,('owner-lookup-deadline',))
        else:self.assertTrue(state['same_cancellation_instance'],state)
        return state

    def test_deadline_during_final_prior_read_preserves_last_good(self):
        self.assert_before_replace_cancelled('deadline-final-read',True)

    def test_callback_during_final_prior_read_preserves_last_good(self):
        self.assert_before_replace_cancelled('cancel-final-read')

    def test_deadline_during_final_lease_checked_before_replace(self):
        state=self.assert_before_replace_cancelled('deadline-final-lease',True)
        self.assertEqual(state['events'][-2:],['lease-verify','guard'])

    def test_callback_during_final_lease_checked_before_replace(self):
        state=self.assert_before_replace_cancelled('cancel-final-lease')
        self.assertEqual(state['events'][-2:],['lease-verify','guard'])

    def test_source_change_after_final_read_refuses_replacement(self):
        state,error=self.publication('code-change-final-read')
        self.assertEqual(state['replace_calls'],0,state)
        self.assertTrue(state['old_preserved'],state)
        self.assertIsInstance(error,contract.LookupError,state)
        self.assertEqual(error.code,'publication-failed')

    def test_normal_one_replace_and_complete_readback(self):
        state,error=self.publication('normal')
        self.assertIsNone(error)
        self.assertEqual(state['replace_calls'],1)
        self.assertTrue(state['new_present'])
        self.assertEqual((state['file_fsyncs'],state['directory_fsyncs']),(1,1))

    def test_early_deadline_preserves_last_good(self):
        self.assert_before_replace_cancelled('deadline-early',True)

    def test_early_callback_preserves_last_good(self):
        self.assert_before_replace_cancelled('cancel-early')

    def test_deadline_after_real_replace_remains_ambiguous(self):
        state,error=self.publication('deadline-after-replace')
        self.assertEqual(state['replace_calls'],1)
        self.assertTrue(state['new_present'])
        self.assertIsInstance(error,contract.LookupCancelled)
        self.assertEqual(error.args,('owner-lookup-deadline',))

    def test_callback_after_real_replace_remains_ambiguous(self):
        state,error=self.publication('cancel-after-replace')
        self.assertEqual(state['replace_calls'],1)
        self.assertTrue(state['new_present'])
        self.assertIsInstance(error,contract.LookupCancelled)
        self.assertTrue(state['same_cancellation_instance'])

    def test_prior_file_identity_mismatch_refuses_replacement(self):
        state,error=self.publication('prior-identity-change')
        self.assertEqual(state['replace_calls'],0)
        self.assertTrue(state['old_preserved'])
        self.assertIsInstance(error,contract.LookupError)

    def test_replace_failure_preserves_last_good_without_retry(self):
        state,error=self.publication('replace-failure')
        self.assertEqual(state['replace_calls'],1)
        self.assertTrue(state['old_preserved'])
        self.assertIsInstance(error,contract.LookupError)
        self.assertEqual(error.code,'publication-failed')

    def test_directory_fsync_after_replace_reports_ambiguous(self):
        state,error=self.publication('fsync-after-replace')
        self.assertEqual(state['replace_calls'],1)
        self.assertTrue(state['new_present'])
        self.assertIsInstance(error,contract.LookupError)
        self.assertEqual(error.code,'publication-outcome-unknown')


if __name__=='__main__':unittest.main()
