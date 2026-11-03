import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from collectors import runs
from collectors.model_evidence import ContractError, encode, export_bundle
from collectors.model_evidence.ingest import accept_envelope
from test_model_evidence_core import bundle


class Ingestion(unittest.TestCase):
    def test_invalid_neighbor_is_removed_while_valid_neighbor_survives(self):
        value=export_bundle(bundle())
        bad=copy.deepcopy(value['runs'][0]); bad['id']='codex:bad'; bad['native_run_id']='bad'
        bad['turns'][0]['reported']['model_id']['basis']='trust-me'
        value['runs'].append(bad)
        out=accept_envelope(value)
        self.assertEqual([r['id'] for r in out['runs']],['codex:r1'])
        self.assertEqual(len(out['operations']),1)
        self.assertEqual(out['health']['state'],'partial')

    def test_removed_parent_invalidates_dependent_receipts(self):
        value=export_bundle(bundle()); value['runs'][0]['status']='bogus'
        out=accept_envelope(value)
        self.assertEqual(out['runs'],[]); self.assertEqual(out['operations'],[]); self.assertEqual(out['requests'],[])
        self.assertEqual(out['health']['state'],'partial')

    def test_usage_flags_and_unknown_settings_are_recomputed_or_rejected(self):
        for mutate in (lambda q:q['usage']['known'].update(cache_write=False),
                       lambda q:q['usage'].update(input_total=999),
                       lambda q:q['reported']['effort'].update(value='xhigh')):
            value=export_bundle(bundle()); mutate(value['requests'][0])
            out=accept_envelope(value)
            self.assertEqual(out['requests'],[]); self.assertEqual(out['health']['state'],'partial')

    def test_publisher_cannot_set_live_qualified_or_claim_unvalidated_origin(self):
        for path,value in (('live_qualified',True),):
            e=export_bundle(bundle()); e['health'][path]=value
            with self.assertRaises(ContractError): accept_envelope(e)
        e=export_bundle(bundle()); e['raw_prompt']='should-not-publish'
        with self.assertRaises(ContractError): accept_envelope(e)

    def test_failed_then_good_then_failed_then_recovery_retains_source_clock(self):
        raw=encode(export_bundle(bundle()))
        c=runs.ingest(None,None,now=1000)
        self.assertIsNone(c['export']); self.assertEqual(c['last_error'],'source-unavailable')
        c=runs.ingest(c,raw,now=1001); source=c['export']['as_of']
        original=copy.deepcopy(c['export'])
        for invalid in (None,b'{"schema":',b'{"schema":"agent-runs-export/1"}'):
            c=runs.ingest(c,invalid,now=1100)
            self.assertEqual(c['export'],original)
            self.assertEqual((c['received_at'],c['export']['as_of']),(1001,source))
            self.assertEqual(runs.status(c,now=2000)['state'],'error')
            self.assertTrue(runs.status(c,now=2000)['stale'])
        c=runs.ingest(c,raw,now=2001)
        self.assertIsNone(c['failed_at']); self.assertIsNone(c['last_error'])
        self.assertEqual(c['export']['as_of'],950)
        self.assertTrue(runs.status(c,now=2001)['stale'])

    def test_old_or_future_publication_is_not_fresh_data(self):
        c=runs.ingest(None,encode(export_bundle(bundle())),now=1000)
        b=bundle(); b['generated_at']=999; b['as_of']=949; b['sources'][0].update(snapshot_at=949,history_end=949,complete_through=949); b['window']['end']=949
        c2=runs.ingest(c,encode(export_bundle(b)),now=1100)
        self.assertEqual(c2['last_error'],'source-regressed'); self.assertEqual(c2['received_at'],1000)
        c3=runs.ingest(c,encode(export_bundle(bundle())),now=999)
        self.assertEqual(c3['last_error'],'source-invalid')

    def test_source_age_uses_oldest_source_not_new_publication(self):
        b=bundle(); b['sources'][0].update(snapshot_at=920,history_end=920,complete_through=920)
        c=runs.ingest(None,encode(export_bundle(b)),now=1000)
        self.assertEqual(runs.status(c,now=1100,stale_after_s=100)['source_at'],920)
        self.assertTrue(runs.status(c,now=1100,stale_after_s=100)['stale'])

    def test_atomic_replace_failure_keeps_original_bytes_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d,'published.json'); c=runs.ingest(None,encode(export_bundle(bundle())),now=1000)
            runs.atomic_write(path,c); original=path.read_bytes()
            with patch('collectors.runs.os.replace',side_effect=OSError('private failure')):
                with self.assertRaises(OSError): runs.atomic_write(path,{**c,'failed_at':1100})
            self.assertEqual(path.read_bytes(),original)
            self.assertEqual(list(Path(d).iterdir()),[path])
            self.assertEqual(runs.load(path),c)

    def test_missing_invalid_interrupted_file_and_recovery(self):
        with tempfile.TemporaryDirectory() as d:
            cache=Path(d,'cache.json'); source=Path(d,'source.json')
            c=runs.refresh(cache,now=1000,source_path=source)
            self.assertEqual(c['last_error'],'source-unavailable')
            source.write_bytes(encode(export_bundle(bundle())))
            c=runs.refresh(cache,now=1001,source_path=source); old=copy.deepcopy(c['export'])
            source.write_bytes(b'{"schema":')
            c=runs.refresh(cache,now=1100,source_path=source)
            self.assertEqual(c['export'],old); self.assertEqual(runs.load(cache),c)
            source.write_bytes(encode(export_bundle(bundle('partial'))))
            c=runs.refresh(cache,now=1200,source_path=source)
            self.assertIsNone(c['last_error']); self.assertTrue(runs.status(c,now=1200)['partial'])

    def test_transport_budget_is_explicit_and_timeout_cannot_fake_freshness(self):
        class Stub:
            def read(self,**kw):
                self.budget=kw
                return encode(export_bundle(bundle()))
        with tempfile.TemporaryDirectory() as d:
            transport=Stub(); path=Path(d,'cache.json')
            c=runs.refresh(path,now=1000,transport=transport,timeout_s=2)
            self.assertEqual(transport.budget,{'max_bytes':4*1024*1024,'timeout_s':2})
            with patch.object(transport,'read',side_effect=TimeoutError):
                failed=runs.refresh(path,now=1100,transport=transport)
            self.assertEqual(failed['export'],c['export']); self.assertEqual(failed['received_at'],1000)

    def test_cli_writes_explicit_file_and_never_echoes_input_errors(self):
        with tempfile.TemporaryDirectory() as d:
            source=Path(d,'input.json'); output=Path(d,'output.json')
            source.write_text('{"schema":"SECRET-BODY"}')
            p=subprocess.run([sys.executable,'-m','collectors.model_evidence',str(source),str(output)],capture_output=True,text=True)
            self.assertEqual(p.returncode,2); self.assertNotIn('SECRET',p.stderr); self.assertFalse(output.exists())
            source.write_text(json.dumps(bundle()))
            subprocess.run([sys.executable,'-m','collectors.model_evidence',str(source),str(output)],check=True)
            self.assertEqual(output.read_bytes(),encode(export_bundle(bundle())))

    def test_deadline_interrupt_keeps_existing_publication(self):
        with tempfile.TemporaryDirectory() as d:
            source=Path(d,'in.json'); destination=Path(d,'out.json')
            source.write_bytes(encode(bundle())); destination.write_bytes(b'last-good')
            script = """
import sys,time
import collectors.model_evidence.__main__ as cli
cli.export_bundle=lambda value:time.sleep(3)
raise SystemExit(cli.main([sys.argv[1],sys.argv[2],'--timeout','1']))
"""
            p=subprocess.run([sys.executable,'-c',script,str(source),str(destination)],capture_output=True,text=True,timeout=4)
            self.assertEqual(p.returncode,2)
            self.assertEqual(destination.read_bytes(),b'last-good')

    def test_metadata_basis_cannot_be_relabelled_as_reported(self):
        value=export_bundle(bundle())
        t=value['runs'][0]['turns'][0]
        t['reported']['effort']=copy.deepcopy(t['requested']['effort'])
        self.assertEqual(accept_envelope(value)['runs'],[])


    def test_import_and_pure_export_do_not_read_files_environment_keys_db_or_network(self):
        script='''
import builtins, os, pathlib, socket, sqlite3, subprocess
from unittest.mock import patch
import json
fixture=json.loads(%r)
def forbidden(*a,**kw): raise AssertionError('external side effect')
with patch.object(builtins,'open',forbidden), patch.object(pathlib.Path,'open',forbidden), \\
     patch.object(os,'getenv',forbidden), patch.object(sqlite3,'connect',forbidden), \\
     patch.object(socket,'socket',forbidden), patch.object(subprocess,'Popen',forbidden), \\
     patch.object(os.path,'expanduser',forbidden):
    import collectors.runs
    from collectors.model_evidence import export_bundle
    result=export_bundle(fixture)
    assert result['origin']['kind']=='offline-fixture'
''' % json.dumps(bundle())
        subprocess.run([sys.executable,'-c',script],check=True,capture_output=True,text=True)


if __name__ == '__main__': unittest.main()
