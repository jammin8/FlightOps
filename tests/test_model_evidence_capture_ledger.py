from copy import deepcopy
import fcntl
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

from collectors.model_evidence.contract import ContractError,encode,digest
from collectors.model_evidence.capture_contract import CaptureCancelled,APPEND_BYTES
from collectors.model_evidence.capture_ledger import Ledger,write_output,read_input
from collectors.model_evidence.capture_projection import project
from collectors.model_evidence.capture_cli import main
from test_model_evidence_capture_contract import fixture,rechained
import os as _os
import tempfile as _tempfile
_tempfile.tempdir = _os.path.realpath(_tempfile.gettempdir())


class Crash(BaseException):
    pass


class CaptureLedger(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.path=self.root/'ledger'
        self.ledger=Ledger(self.path,'recorder:synthetic','epoch:synthetic').create()

    def test_append_seal_replay_identity_clocks_and_idempotency(self):
        rows=fixture()['events'];snapshot=self.ledger.append(rows)
        self.assertEqual(snapshot['events'],rows);self.assertEqual(snapshot['gaps'],[])
        before=(self.path/'manifest.json').read_bytes()
        self.assertEqual(self.ledger.append(rows),snapshot)
        self.assertEqual((self.path/'manifest.json').read_bytes(),before)
        sealed=self.ledger.seal_snapshot(self.root/'sealed.json')
        self.assertEqual(sealed,snapshot)
        self.assertEqual(json.loads(read_input(self.root/'sealed.json')),snapshot)
        self.assertEqual(self.ledger.snapshot()['as_of'],108)

    def test_one_writer_never_steals_lock(self):
        with self.ledger.writer():
            with self.assertRaisesRegex(ContractError,'writer-busy'):self.ledger.append(fixture()['events'])
        self.assertEqual(self.ledger.append(fixture()['events'])['gaps'],[])

    def test_out_of_sequence_wrong_epoch_and_overflow_append_rejected(self):
        rows=fixture()['events'];bad=deepcopy(rows);bad[0]['sequence']=2
        with self.assertRaises(ContractError):self.ledger.append(bad)
        bad=deepcopy(rows);bad[0]['recorder_epoch']='epoch:wrong'
        with self.assertRaises(ContractError):self.ledger.append(bad)
        with self.assertRaises(ContractError):self.ledger.append(rows*20)
        self.assertEqual(self.ledger.snapshot()['events'],[])

    def test_conflicting_copies_preserve_history_and_neighbors(self):
        rows=fixture()['events'];self.ledger.append(rows)
        bad=deepcopy(rows[0]);bad.update(sequence=9,previous_event_digest=digest(rows[-1]),recorded_at=109)
        bad['payload']['input_sha']='e'*40
        snapshot=self.ledger.append([bad]);self.assertEqual(len(snapshot['events']),9)
        result=project(snapshot['events']);self.assertIn('assignment-conflict',result['assignments'][0]['reasons'])

    def test_segment_deletion_reports_retention_hole_keeps_other_segment(self):
        rows=fixture()['events'];self.ledger.append(rows[:3]);self.ledger.append(rows[3:])
        m=json.loads((self.path/'manifest.json').read_bytes());(self.path/m['segments'][0]['name']).unlink()
        snapshot=self.ledger.snapshot()
        self.assertIn('segment-unavailable',snapshot['gaps']);self.assertIn('event-chain-gap',snapshot['gaps'])
        self.assertEqual(snapshot['events'],rows[3:])
        with self.assertRaises(ContractError):self.ledger.append([rows[0]])

    def test_missing_manifest_is_unknown_not_healthy_empty(self):
        (self.path/'manifest.json').unlink();snapshot=self.ledger.snapshot()
        self.assertEqual(snapshot['gaps'],['manifest-unavailable']);self.assertIsNone(snapshot['as_of'])
        with self.assertRaises(ContractError):self.ledger.create()
        protected=self.root/'unrelated';protected.mkdir();(protected/'keep').write_bytes(b'unchanged')
        with self.assertRaises(ContractError):Ledger(protected,'recorder:synthetic','epoch:synthetic').create()
        self.assertFalse((protected/'.writer.lock').exists())
        self.assertEqual((protected/'keep').read_bytes(),b'unchanged')

    def test_partial_tail_and_invalid_neighbor_do_not_freshen_source(self):
        rows=fixture()['events'];self.ledger.append(rows)
        m=json.loads((self.path/'manifest.json').read_bytes());p=self.path/m['segments'][0]['name']
        p.write_bytes(encode(rows[0])+b'{"private":"DO-NOT-EXPORT"}\n'+encode(rows[-1])[:-1])
        snapshot=self.ledger.snapshot()
        self.assertIn('invalid-event',snapshot['gaps']);self.assertIn('partial-tail',snapshot['gaps'])
        self.assertNotIn(b'DO-NOT-EXPORT',encode(snapshot));self.assertEqual(snapshot['as_of'],108)

    def test_crash_points_preserve_old_manifest_and_expose_pending_data(self):
        for point in ('before-segment','during-write','after-file-fsync','before-publication','after-segment-before-manifest','before-manifest'):
            with self.subTest(point=point):
                path=self.root/point;ledger=Ledger(path,'recorder:synthetic','epoch:synthetic').create()
                initial=(path/'manifest.json').read_bytes()
                def crash(at):
                    if at==point:raise Crash()
                ledger.crash=crash
                with self.assertRaises(Crash):ledger.append(fixture()['events'])
                self.assertEqual((path/'manifest.json').read_bytes(),initial)
                snapshot=ledger.snapshot();self.assertEqual(snapshot['events'],[]);self.assertIsNone(snapshot['as_of'])
                if point!='before-segment':self.assertIn('unreferenced-ledger-entry',snapshot['gaps'])

    def test_cancel_before_manifest_never_publishes(self):
        rows=fixture()['events'];initial=(self.path/'manifest.json').read_bytes();cancelled=[False]
        def crash(at):
            if at=='before-manifest':cancelled[0]=True
        def check():
            if cancelled[0]:raise CaptureCancelled()
        self.ledger.crash=crash;self.ledger.check=check
        with self.assertRaises(CaptureCancelled):self.ledger.append(rows)
        self.assertEqual((self.path/'manifest.json').read_bytes(),initial)

    def test_symlink_roots_segments_locks_and_output_refused(self):
        link=self.root/'link';link.symlink_to(self.path,target_is_directory=True)
        with self.assertRaises(ContractError):Ledger(link,'recorder:synthetic','epoch:synthetic').snapshot()
        self.ledger.append(fixture()['events']);m=json.loads((self.path/'manifest.json').read_bytes())
        segment=self.path/m['segments'][0]['name'];copy=self.root/'outside';copy.write_bytes(segment.read_bytes());segment.unlink();segment.symlink_to(copy)
        self.assertIn('segment-unavailable',self.ledger.snapshot()['gaps'])
        output=self.root/'output';output.symlink_to(copy)
        with self.assertRaises(ContractError):write_output(output,b'{}\n')
        unsafe=self.root/'unsafe';unsafe.mkdir();(unsafe/'.writer.lock').symlink_to(copy)
        with self.assertRaises(ContractError):Ledger(unsafe,'recorder:synthetic','epoch:synthetic').create()

    def test_path_escape_hardlink_and_output_conflict_refused(self):
        with self.assertRaises(ContractError):Ledger(self.root/'x'/'..'/'ledger','recorder:synthetic','epoch:synthetic').snapshot()
        p=self.root/'input';p.write_bytes(b'{}');os.link(p,self.root/'alias')
        with self.assertRaises(ContractError):read_input(p)
        p=self.root/'stable';write_output(p,b'{}\n');write_output(p,b'{}\n')
        with self.assertRaises(ContractError):write_output(p,b'[]\n')
        self.assertEqual(p.read_bytes(),b'{}\n')

    def test_cli_supplied_replay_cancel_and_no_output_on_invalid_input(self):
        source=self.root/'source.json';source.write_bytes(encode(fixture()));output=self.root/'result.json'
        self.assertEqual(main(['validate','--input',str(source),'--output',str(output)]),0)
        result=json.loads(output.read_bytes());self.assertEqual(result['custody'],'supplied-replay')
        invalid=self.root/'invalid.json';invalid.write_bytes(b'{"prompt":"PRIVATE"}')
        with redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(main(['validate','--input',str(invalid),'--output',str(self.root/'absent')]),2)
            self.assertEqual(main(['validate','--input',str(source),'--output',str(self.root/'absent'),'--prompt','PRIVATE']),2)
        self.assertNotIn('PRIVATE',errors.getvalue());self.assertFalse((self.root/'absent').exists())
        def cancel():raise CaptureCancelled()
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(['validate','--input',str(source),'--output',str(self.root/'cancelled')],check=cancel),130)
        self.assertFalse((self.root/'cancelled').exists())

    def test_existing_output_unchanged_after_cancel_or_failure(self):
        output=self.root/'last-good';write_output(output,b'{}\n')
        def cancel():raise CaptureCancelled()
        with self.assertRaises(CaptureCancelled):write_output(output,b'[]\n',check=cancel)
        self.assertEqual(output.read_bytes(),b'{}\n')

    def test_identity_marker_loss_never_becomes_complete_or_reinitialized(self):
        self.ledger.append(fixture()['events'])
        (self.path/'identity.json').unlink()
        snapshot=self.ledger.snapshot()
        self.assertIn('identity-custody-unavailable',snapshot['gaps'])
        self.assertEqual(len(snapshot['events']),8)
        with self.assertRaises(ContractError):self.ledger.create()

    def test_manifest_write_crash_after_prior_rotation_retains_old_source_clock(self):
        rows=fixture()['events'];self.ledger.append(rows[:3]);old=(self.path/'manifest.json').read_bytes();calls=[0]
        def crash(at):
            if at=='after-file-fsync':
                calls[0]+=1
                if calls[0]==2:raise Crash()
        self.ledger.crash=crash
        with self.assertRaises(Crash):self.ledger.append(rows[3:])
        self.assertEqual((self.path/'manifest.json').read_bytes(),old)
        snapshot=self.ledger.snapshot();self.assertEqual(snapshot['events'],rows[:3]);self.assertEqual(snapshot['as_of'],103)
        self.assertIn('unreferenced-ledger-entry',snapshot['gaps'])

    def test_stable_input_read_refuses_in_place_change(self):
        p=self.root/'changing';p.write_bytes(b'{}\n');original=os.read;changed=[False]
        def read(fd,count):
            raw=original(fd,count)
            if raw and not changed[0]:changed[0]=True;p.write_bytes(b'[]\n')
            return raw
        with mock.patch('os.read',side_effect=read):
            with self.assertRaises(ContractError):read_input(p)

    def test_cli_replay_and_seal_use_only_explicit_temporary_paths(self):
        self.ledger.append(fixture()['events'])
        common=['--ledger',str(self.path),'--recorder','recorder:synthetic','--epoch','epoch:synthetic']
        self.assertEqual(main(['replay',*common,'--output',str(self.root/'replay.json')]),0)
        self.assertEqual(main(['seal',*common,'--output',str(self.root/'seal.json')]),0)
        self.assertEqual(json.loads((self.root/'replay.json').read_bytes())['authentication'],'not-performed')
        self.assertEqual(json.loads((self.root/'seal.json').read_bytes())['as_of'],108)


if __name__=='__main__':
    unittest.main()
