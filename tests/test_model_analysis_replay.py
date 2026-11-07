import contextlib
import difflib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from collectors.model_analysis.__main__ import main
from collectors.model_analysis import build_manifest, decide, ContractError
from collectors.model_analysis.contract import HISTORICAL_POLICY_VERSION, POLICY_VERSION, digest
from test_model_analysis_support import missing, packet, rebind

FROZEN_ENGINE = 'a5c1c24f23da65fe66667313d5f7a91298140a987fba4798c5164e8b740efc5c'


ROOT = Path(__file__).resolve().parent.parent


def outside_support(version):
    p = packet(400, kind='amount', direction='lower', effect=True)
    p['policy']['version'] = version
    for i, row in enumerate(p['observations']):
        if i % 400 >= 80: row['task'] = 'outside-reference'
        elif i % 400 >= 40: missing(row)
    rebind(p)
    return p


def frozen(p):
    bundle = {'schema': 'flightops-analysis-freeze/1', 'normalized_input': p,
              'manifest': build_manifest(p, replaying=True)}
    bundle['freeze_hash'] = digest(bundle)
    return bundle


def replayed(bundle):
    with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        source, out = Path(directory)/'frozen.json', Path(directory)/'decision.json'
        source.write_text(json.dumps(bundle))
        code = main(['replay', str(source), '--output', str(out)])
        return code, json.loads(out.read_text()) if code == 0 else None


class HistoricalPolicy(unittest.TestCase):
    def test_a_v1_freeze_replays_to_the_decision_the_pre_fix_engine_made(self):
        code, decision = replayed(frozen(outside_support(HISTORICAL_POLICY_VERSION)))
        self.assertEqual(code, 0)
        self.assertEqual(decision['manifest_hash'], '71baeebdf6d919398d86158e1e4c56f5f3ca0d43abe7acbfa12f10dee5305965')
        self.assertEqual(decision['decision_hash'], 'fd03bd6eb04ac8bbb174e08967f2d7f6301ab7af8f30c7a4c997ccd2251112b5')
        self.assertEqual(decision['policy_version'], HISTORICAL_POLICY_VERSION)
        self.assertEqual(decision['measures']['metric']['groups']['a']['calls'], ['Best'])
        self.assertEqual(decision['measures']['metric']['coverage']['a']['included'], 360)

    def test_the_same_input_under_the_current_policy_keeps_its_own_identity(self):
        code, decision = replayed(frozen(outside_support(POLICY_VERSION)))
        self.assertEqual(code, 0)
        self.assertEqual(decision['policy_version'], POLICY_VERSION)
        self.assertEqual(decision['measures']['metric']['groups']['a']['status'], 'InsufficientEvidence')
        self.assertEqual(decision['measures']['metric']['coverage']['a']['included'], 40)
        self.assertNotEqual(decision['manifest_hash'], '71baeebdf6d919398d86158e1e4c56f5f3ca0d43abe7acbfa12f10dee5305965')

    def test_a_v1_freeze_keeps_its_old_numeric_bytes(self):
        p = packet(80, effect=True)
        p['policy']['version'] = HISTORICAL_POLICY_VERSION
        weights = [0.266411259804338, 0.1419783183553549, 0.5022241992749249, 0.08938622256538212]
        p['policy']['reference_mix'] = [{'repo':'org/repo','task':str(i),'size':'s','weight':w} for i, w in enumerate(weights)]
        for i, row in enumerate(p['observations']): row['task'] = str(i % 4)
        rebind(p)
        decision = decide(build_manifest(p, replaying=True))
        self.assertEqual(decision['decision_hash'], '9f6456619b5ab5ac6159e33c88017a28249f625cf8717912daf0aea9025991a1')
        self.assertEqual(decision['measures']['metric']['groups']['a']['estimate'], 1.0000000000000002)

    def test_the_historical_engine_is_the_frozen_base_but_for_its_version_import(self):
        current = (ROOT/'collectors/model_analysis/engine_v1.py').read_text()
        base = current.replace('from .contract import DECISION_SCHEMA, HISTORICAL_POLICY_VERSION as POLICY_VERSION, MANIFEST_SCHEMA, digest, require', 'from .contract import DECISION_SCHEMA, MANIFEST_SCHEMA, POLICY_VERSION, digest, require')
        self.assertEqual(hashlib.sha256(base.encode()).hexdigest(), FROZEN_ENGINE)
        changed = [line for line in difflib.ndiff(base.splitlines(), current.splitlines()) if line[0] in '+-']
        self.assertEqual(changed, ['- from .contract import DECISION_SCHEMA, MANIFEST_SCHEMA, POLICY_VERSION, digest, require',
                                   '+ from .contract import DECISION_SCHEMA, HISTORICAL_POLICY_VERSION as POLICY_VERSION, MANIFEST_SCHEMA, digest, require'])

    def test_new_freezes_refuse_the_historical_policy(self):
        p = packet(2)
        p['policy']['version'] = HISTORICAL_POLICY_VERSION
        with self.assertRaisesRegex(ContractError, 'new freezes use the current policy'):
            build_manifest(p)
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stderr(io.StringIO()):
            source, out = Path(directory)/'input', Path(directory)/'output'
            source.write_text(json.dumps(p))
            self.assertEqual(main(['freeze', str(source), '--output', str(out)]), 2)
            self.assertFalse(out.exists())

    def test_an_old_freeze_cannot_be_relabelled_to_the_current_policy(self):
        p = packet(2)
        p['policy']['version'] = HISTORICAL_POLICY_VERSION
        relabelled = frozen(p)
        relabelled['manifest']['policy_version'] = POLICY_VERSION
        relabelled['manifest']['manifest_hash'] = digest({k: v for k, v in relabelled['manifest'].items() if k != 'manifest_hash'})
        relabelled.pop('freeze_hash')
        relabelled['freeze_hash'] = digest(relabelled)
        self.assertEqual(replayed(relabelled), (2, None))

    def test_an_unknown_policy_is_refused(self):
        p = packet(2)
        p['policy']['version'] = 'joint-strata-max-bootstrap/3'
        with self.assertRaisesRegex(ContractError, 'unsupported policy version'):
            build_manifest(p, replaying=True)


class Replay(unittest.TestCase):
    def test_cli_freeze_replay_immutable_bytes_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory); source = root/'input.json'; frozen = root/'frozen.json'
            first, second = root/'one.json', root/'two.json'
            source.write_text(json.dumps(packet(40, effect=True)))
            self.assertEqual(main(['freeze',str(source),'--output',str(frozen)]), 0)
            frozen_bytes = frozen.read_bytes()
            self.assertEqual(main(['replay',str(frozen),'--output',str(first)]), 0)

            source.write_text('{}')
            self.assertEqual(main(['replay',str(frozen),'--output',str(second)]), 0)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(frozen.read_bytes(), frozen_bytes)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(['replay',str(frozen),'--output',str(first)]), 2)
            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_duplicate_json_keys_and_nonfinite_refuse(self):
        for body in ('{"schema":1,"schema":2}', '{"x":NaN}'):
            with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stderr(io.StringIO()):
                source, out = Path(directory)/'input', Path(directory)/'output'
                source.write_text(body)
                self.assertEqual(main(['freeze',str(source),'--output',str(out)]), 2)
                self.assertFalse(out.exists())

    def test_recomputed_hash_cannot_skip_contract_replay(self):
        p = packet(2); m = build_manifest(p)
        m['inventory_complete'] = False
        bundle = {'schema':'flightops-analysis-freeze/1','normalized_input':p,'manifest':m}
        bundle['freeze_hash'] = digest(bundle)
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stderr(io.StringIO()):
            source, out = Path(directory)/'input', Path(directory)/'output'
            source.write_text(json.dumps(bundle))
            self.assertEqual(main(['replay',str(source),'--output',str(out)]), 2)
            self.assertFalse(out.exists())

    def test_proofs_bind_setting_size_value_and_unit_identity(self):
        for mutate in (lambda p: p['setups'][0]['model'].update(value='different'),
                       lambda p: p['observations'][0]['anchored_size'].update(changed_lines=300),
                       lambda p: p['observations'][0]['measures']['metric'].update(value=0),
                       lambda p: p['evidence'][4].update(measure_id='other')):
            p = packet(2); mutate(p)
            m = build_manifest(p)
            self.assertEqual(m['rows'][0]['measures']['metric']['status'], 'missing')

    def test_contract_limits_are_real_not_ignored_knobs(self):
        for mutate in (lambda p: p['policy'].update(bootstrap_replicates=1),
                       lambda p: p['policy'].update(calibration_simulations=1),
                       lambda p: p['policy'].update(seed=-1),
                       lambda p: p['policy']['reference_mix'][0].update(weight=.5),
                       lambda p: p.update(observations=p['observations']*1300)):
            p=packet(2); mutate(p)
            with self.assertRaises(ContractError): build_manifest(p)
