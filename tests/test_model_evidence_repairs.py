import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from collectors import runs
from collectors.model_evidence import ContractError, encode, export_bundle
from collectors.model_evidence.contract import stamp
from collectors.model_evidence.ingest import accept_envelope
from collectors.model_evidence.schema import TIME, validate
from test_model_evidence_core import bundle

A = 'a' * 40
B = 'b' * 40
BAD_FINITE = (10**400, 2**53, True, -1)
BAD_FLOAT = (float('nan'), float('inf'), -float('inf'))
FIXED_ERROR = 'offline evidence export failed: invalid-or-unavailable-input\n'


class CapturedWitness(unittest.TestCase):
    def derive(self, command, output):
        b = bundle()
        b['calls'][0]['command'] = command
        b['completions'][0]['output'] = output
        return export_bundle(b)

    def reject(self, command, output):
        result = self.derive(command, output)
        self.assertFalse(any(o['operation'] in ('commit', 'merge') for o in result['operations']))
        self.assertEqual(result['health']['state'], 'partial')
        self.assertIn('no-proven-operation', [e['code'] for e in result['errors']])

    def test_e1_exact_hidden_commit_hidden_head_unrelated_visible_sha(self):
        self.reject('git commit -m x >/dev/null && git rev-parse HEAD >/dev/null && git rev-parse ' + B, B + '\n')

    def test_extra_visible_rev_parse_log_and_show_cannot_supply_commit_witness(self):
        for other in ('git rev-parse ' + B, 'git log -1 --format=%H ' + B, 'git show -s --format=%H ' + B):
            with self.subTest(other=other):
                self.reject('git commit -m x >/dev/null && git rev-parse HEAD >/dev/null && ' + other, B + '\n')
                self.reject('git commit -m x && ' + other, '[main aaaaaaa] x\n' + B + '\n')

    def test_captured_commit_and_head_and_hidden_commit_visible_head_controls(self):
        for commit in ('git commit -m x', 'git commit -m x >/dev/null', 'git commit -m x 2>/dev/null'):
            for head in ('git rev-parse HEAD', 'git rev-parse HEAD >&2', 'git rev-parse HEAD 3>&1 >/dev/null >&3'):
                with self.subTest(commit=commit, head=head):
                    output = A + '\n'
                    if commit != 'git commit -m x >/dev/null':
                        output = '[main aaaaaaa] x\n' + output
                    result = self.derive(commit + ' && ' + head, output)
                    self.assertEqual([(o['sha'], o['proof']) for o in result['operations']], [(A, 'log-head-printed')])

    def test_visible_commit_hidden_head_uses_commit_line_not_hex_output(self):
        for routing in ('>/dev/null', '>&-', '2>&1 >/dev/null'):
            with self.subTest(routing=routing):
                result = self.derive('git commit -m x && git rev-parse HEAD ' + routing, '[main aaaaaaa] x\n')
                self.assertEqual([(o['sha'], o['proof']) for o in result['operations']], [(A, 'log-commit-line')])
                self.reject('git commit -m x >/dev/null && git rev-parse HEAD ' + routing, B + '\n')

    def test_unknown_routing_and_descriptor_restoration_do_not_create_foreign_witness(self):
        for routing in ('>$DEST', '>&3', '>/dev/stdout', '>/dev/null >/dev/stdout'):
            with self.subTest(routing=routing):
                self.reject('git commit -m x >/dev/null && git rev-parse HEAD ' + routing, B + '\n')
        for other in ('git rev-parse ' + B + ' 3>&1 >/dev/null >&3',
                      'cat other 4>&1 >/dev/null >&4',
                      'git rev-parse ' + B + ' >/dev/null >&2'):
            with self.subTest(other=other):
                self.reject('git commit -m x >/dev/null && git rev-parse HEAD >/dev/null && ' + other, B + '\n')

    def test_quiet_companions_commit_push_and_bound_head_push_controls(self):
        cases = (
            ('git add f && git commit -m x', '[main aaaaaaa] x\n', ['commit']),
            ('cat file >/dev/null 2>&1 && git commit -m x', '[main aaaaaaa] x\n', ['commit']),
            ('git commit -m x && git push origin main', '[main aaaaaaa] x\n   bbbbbbb..aaaaaaa main -> main\n', ['commit', 'push']),
            ('git commit -m x && git rev-parse HEAD && git push origin main', '[main aaaaaaa] x\n' + A + '\n   bbbbbbb..aaaaaaa main -> main\n', ['commit', 'push']),
        )
        for cmd, output, kinds in cases:
            with self.subTest(cmd=cmd):
                ops = self.derive(cmd, output)['operations']
                self.assertEqual([o['operation'] for o in ops], kinds)
                self.assertTrue(all(o['sha'] == A for o in ops))

    def test_merge_requires_its_captured_creation_line_and_bound_head(self):
        good = self.derive('git merge feature && git rev-parse HEAD', "Merge made by the 'ort' strategy.\n" + A + '\n')
        self.assertEqual([(o['operation'], o['sha']) for o in good['operations']], [('merge', A)])
        self.reject('git merge feature >/dev/null && git rev-parse HEAD', "Merge made by the 'ort' strategy.\n" + A + '\n')
        self.reject('git merge feature && git rev-parse HEAD', 'remote: Merge made by a different command\n' + A + '\n')


    def test_push_requires_captured_stderr_and_cannot_borrow_hidden_commit_line(self):
        for routing in ('2>/dev/null', '2>&-', '2>$DEST', '2>/dev/stderr'):
            with self.subTest(routing=routing):
                result = self.derive('git push origin main ' + routing, '   bbbbbbb..aaaaaaa main -> main\n')
                self.assertEqual(result['operations'], [])
        result = self.derive('git commit -m x >/dev/null && git push origin main', '   bbbbbbb..aaaaaaa main -> main\n')
        self.assertEqual([o['operation'] for o in result['operations']], ['push'])
        self.assertEqual(result['operations'][0]['sha'], A)


class NumericBoundaries(unittest.TestCase):
    def test_bounded_predicates_reject_large_and_nonfinite_numbers_without_conversion(self):
        for value in BAD_FINITE + BAD_FLOAT:
            with self.subTest(value_type=type(value).__name__, value=str(value)[:24]):
                self.assertFalse(stamp(value))
                with self.assertRaises(ContractError):
                    validate(value, TIME)
        for value in (0, 0.0, 2**53 - 1, float(2**53 - 1)):
            self.assertTrue(stamp(value))
            validate(value, TIME)

    def test_bad_header_and_source_times_keep_last_good_and_original_clocks(self):
        original = export_bundle(bundle())
        previous = runs.ingest(None, encode(original), now=1000)
        for path in (('generated_at',), ('as_of',), ('sources', 0, 'snapshot_at'),
                     ('sources', 0, 'history_start'), ('sources', 0, 'history_end'),
                     ('sources', 0, 'complete_through'), ('window', 'end')):
            for value in BAD_FINITE:
                incoming = copy.deepcopy(original)
                target = incoming
                for part in path[:-1]:
                    target = target[part]
                target[path[-1]] = value
                with self.subTest(path=path, value=str(value)[:24]):
                    failed = runs.ingest(previous, encode(incoming), now=1100)
                    self.assertEqual(failed['export'], previous['export'])
                    self.assertEqual(failed['received_at'], 1000)
                    self.assertEqual(failed['export']['as_of'], 950)
                    self.assertEqual(failed['last_error'], 'source-invalid')
                    self.assertEqual(failed['failed_at'], 1100)
        for raw in (b'NaN', b'Infinity', b'-Infinity'):
            payload = encode(original).replace(b'"generated_at":1000', b'"generated_at":' + raw)
            self.assertEqual(runs.ingest(previous, payload, now=1100)['last_error'], 'source-invalid')

    def test_nonfinite_json_rejects_at_every_timestamp_location_without_losing_last_good(self):
        previous = runs.ingest(None, encode(export_bundle(bundle())), now=1000)
        for path in (('generated_at',), ('sources', 0, 'snapshot_at'),
                     ('runs', 0, 'started_at'), ('runs', 0, 'evidence', 'at'),
                     ('requests', 0, 'final_sources', 0, 'at'),
                     ('operations', 0, 'completion_evidence', 'at')):
            for value in BAD_FLOAT:
                incoming = copy.deepcopy(previous['export'])
                target = incoming
                for part in path[:-1]:
                    target = target[part]
                target[path[-1]] = value
                raw = json.dumps(incoming).encode()
                with self.subTest(path=path, value=str(value)):
                    result = runs.ingest(previous, raw, now=1100)
                    self.assertEqual(result['last_error'], 'source-invalid')
                    self.assertEqual(result['export'], previous['export'])
                    self.assertEqual(result['received_at'], 1000)

    def test_maximum_valid_timestamp_and_zero_run_time_are_accepted_end_to_end(self):
        b = bundle(); b['generated_at'] = 2**53 - 1
        b['runs'][0].update(started_at=0, ended_at=0)
        result = runs.ingest(None, encode(export_bundle(b)), now=2**53 - 1)
        self.assertIsNone(result['last_error'])
        self.assertEqual(result['export']['generated_at'], 2**53 - 1)
        self.assertEqual(result['export']['runs'][0]['started_at'], 0)


    def test_bad_provenance_and_run_timestamps_preserve_valid_neighbors_as_partial(self):
        for collection, path in (('runs', ('started_at',)), ('runs', ('ended_at',)),
                                 ('runs', ('evidence', 'at')), ('requests', ('final_sources', 0, 'at')),
                                 ('operations', ('completion_evidence', 'at'))):
            for value in BAD_FINITE:
                e = export_bundle(bundle())
                bad = copy.deepcopy(e[collection][0])
                if collection == 'runs':
                    bad.update(id='codex:bad', native_run_id='bad')
                elif collection == 'requests':
                    bad.update(id='codex:bad', native_request_id='bad')
                else:
                    bad['call_id'] = 'codex:bad'
                target = bad
                for part in path[:-1]:
                    target = target[part]
                target[path[-1]] = value
                e[collection].append(bad)
                with self.subTest(collection=collection, path=path, value=str(value)[:24]):
                    partial = accept_envelope(e)
                    self.assertEqual(len(partial[collection]), 1)
                    self.assertEqual(partial['health']['state'], 'partial')
                    self.assertTrue(partial['errors'])

    def test_fixture_huge_header_rejects_and_huge_record_time_is_recoverable(self):
        for field in ('generated_at', 'as_of'):
            b = bundle(); b[field] = 10**400
            with self.assertRaises(ContractError):
                export_bundle(b)
        for path in (('started_at',), ('evidence', 'at')):
            b = bundle(); bad = copy.deepcopy(b['runs'][0]); bad['native_run_id'] = 'bad'
            target = bad
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = 10**400
            b['runs'].append(bad)
            partial = export_bundle(b)
            self.assertEqual([r['id'] for r in partial['runs']], ['codex:r1'])
            self.assertEqual(partial['health']['state'], 'partial')

    def test_huge_timestamp_cli_returns_fixed_error_without_replacing_destination(self):
        with tempfile.TemporaryDirectory() as d:
            source, destination = Path(d, 'input.json'), Path(d, 'output.json')
            b = bundle(); b['generated_at'] = 10**400
            source.write_bytes(encode(b)); destination.write_bytes(b'LAST-GOOD')
            p = subprocess.run([sys.executable, '-m', 'collectors.model_evidence', str(source), str(destination)],
                               text=True, capture_output=True, timeout=5)
            self.assertEqual(p.returncode, 2)
            self.assertEqual(p.stderr, FIXED_ERROR)
            self.assertEqual(destination.read_bytes(), b'LAST-GOOD')


ALARM_SCRIPT = r'''
import sys,time
from pathlib import Path
import collectors.model_evidence.__main__ as cli
from collectors.model_evidence import exporter,operations,ingest,schema
boundary,source,destination = sys.argv[1:]
def stalled(original):
    def wrapper(*a,**kw):
        time.sleep(2)
        return original(*a,**kw)
    return wrapper
if boundary == 'run':
    exporter.run_record = stalled(exporter.run_record)
elif boundary == 'request':
    exporter.request_observation = stalled(exporter.request_observation)
elif boundary == 'operation':
    operations.shell.printed_commit = stalled(operations.shell.printed_commit)
elif boundary == 'prepublication':
    active = False
    original_accept, original_validate = cli.accept_envelope, ingest.validate
    def accepting(value):
        global active
        active = True
        return original_accept(value)
    def validating(value,spec):
        if active and spec is schema.RUN:
            time.sleep(2)
        return original_validate(value,spec)
    cli.accept_envelope, ingest.validate = accepting, validating
else:
    raise AssertionError('unknown test boundary')
raise SystemExit(cli.main([source,destination,'--timeout','1']))
'''


class FatalDeadlines(unittest.TestCase):
    def alarm_at(self, boundary):
        with tempfile.TemporaryDirectory() as d:
            source, destination = Path(d, 'input.json'), Path(d, 'output.json')
            source.write_bytes(encode(bundle())); destination.write_bytes(b'LAST-GOOD')
            p = subprocess.run([sys.executable, '-c', ALARM_SCRIPT, boundary, str(source), str(destination)],
                               capture_output=True, text=True, timeout=5)
            self.assertEqual(p.returncode, 2, p.stderr)
            self.assertEqual(p.stderr, FIXED_ERROR)
            self.assertEqual(destination.read_bytes(), b'LAST-GOOD')
            self.assertEqual(sorted(p.name for p in Path(d).iterdir()), ['input.json', 'output.json'])

    def test_actual_alarm_in_run_record_is_fatal(self):
        self.alarm_at('run')

    def test_actual_alarm_in_request_observation_is_fatal(self):
        self.alarm_at('request')

    def test_actual_alarm_in_operation_proof_is_fatal(self):
        self.alarm_at('operation')

    def test_actual_alarm_in_prepublication_record_validation_is_fatal(self):
        self.alarm_at('prepublication')

    def test_ordinary_malformed_neighbor_still_publishes_partial_without_cancellation(self):
        with tempfile.TemporaryDirectory() as d:
            source, destination = Path(d, 'input.json'), Path(d, 'output.json')
            b = bundle(); b['runs'].append({'native_run_id': None})
            source.write_bytes(encode(b)); destination.write_bytes(b'LAST-GOOD')
            p = subprocess.run([sys.executable, '-m', 'collectors.model_evidence', str(source), str(destination)],
                               capture_output=True, text=True, timeout=5)
            self.assertEqual(p.returncode, 0, p.stderr)
            publication = json.loads(destination.read_bytes())
            self.assertEqual([r['id'] for r in publication['runs']], ['codex:r1'])
            self.assertEqual(publication['health']['state'], 'partial')
            self.assertFalse(publication['health']['live_qualified'])


if __name__ == '__main__':
    unittest.main()
