from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))
from candidate_result import CandidateResult, dump, read

GOOD = {'blocking': 0, 'held': []}


class ReadTest(unittest.TestCase):
    def test_what_dump_writes_reads_back_as_the_same_result(self):
        result = CandidateResult(2)
        self.assertEqual(read(dump(result)), result)

    def test_a_result_without_blocking_is_named(self):
        with self.assertRaisesRegex(ValueError, '^the candidate result has no blocking$'):
            read(json.dumps({'held': []}))

    def test_a_result_without_held_caches_reads_as_one_with_none(self):
        self.assertEqual(read(json.dumps({'blocking': 1})), CandidateResult(1))

    def test_a_wrong_type_is_named_with_its_value(self):
        for value, shown in (('0', "'0'"), (0.0, '0.0')):
            with self.subTest(value=value), self.assertRaisesRegex(
                    ValueError, f'^the candidate result has blocking {shown}, which is not of type int$'):
                read(json.dumps({**GOOD, 'blocking': value}))

    def test_a_count_given_as_true_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'has blocking True, which is not of type int'):
            read(json.dumps({**GOOD, 'blocking': True}))

    def test_a_key_a_later_release_adds_is_ignored(self):
        self.assertEqual(read(json.dumps({**GOOD, 'unmanaged': 4})), CandidateResult(0))

    def test_output_after_the_result_is_refused(self):
        with self.assertRaisesRegex(ValueError, '^the candidate result is not JSON: Extra data, line 2$'):
            read(json.dumps(GOOD) + '\n{}\n')

    def test_text_that_is_not_json_is_named(self):
        for text, error in (('Managed changes: 2; blocking issues: 0.\n', 'Expecting value, line 1'),
                            ('{"blocking', 'Unterminated string starting at, line 1')):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, f'^the candidate result is not JSON: {error}$'):
                read(text)

    def test_json_that_is_not_an_object_is_named(self):
        with self.assertRaisesRegex(ValueError, '^the candidate result is of type list, not an object$'):
            read('[]')


class CandidateOutputTest(unittest.TestCase):
    """What sync.py --candidate prints, with --json for a program and without it for a person."""

    def candidate(self, *flags, blocked=()):
        import sync
        device = mock.Mock()
        device.roots.installed_engine.return_value = None
        current = SimpleNamespace(changes=('a', 'b'), blocked=blocked, unmanaged=('c',), expected=(),
                                  observations={'kit': 'observed'}, result='plan', exit_code=2 if blocked else 1)
        owner = mock.Mock()

        def validate(configurations, roots, *, proposed):
            if (configurations, roots, proposed) != (owner.configurations, device.roots, True):
                raise ValueError('settings checked against the wrong configurations')

        # Each step answers only for this device's setup, so a step given the wrong one fails the run.
        with mock.patch('sync.load') as load, mock.patch('sync.Device.current', return_value=device), \
                mock.patch('sync.plan', side_effect=lambda setup, on: current if (setup, on) == (load.return_value, device)
                           else None), mock.patch('sync.render', return_value='rows\n'), \
                mock.patch('accepted_installations.AcceptedInstallations', side_effect=lambda setup, on: owner
                           if (setup, on) == (load.return_value.for_platform.return_value, device) else None), \
                mock.patch('configuration.validate_runtime', side_effect=validate), mock.patch('sync_hosts.Rehearsal'), \
                redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as error:
            code = sync.main(['--candidate', *flags])
        return code, output.getvalue(), error.getvalue()

    def test_with_json_it_prints_one_result_and_nothing_else(self):
        code, output, error = self.candidate('--json')
        self.assertEqual((code, error), (1, ''))
        self.assertEqual(len(output.splitlines()), 1)
        # A pull no longer waits on a held Claude cache, but an older release.py still needs the key.
        self.assertEqual(json.loads(output), {'blocking': 0, 'held': []})

    def test_with_json_a_blocked_plan_also_writes_its_rows_to_stderr(self):
        code, output, error = self.candidate('--json', blocked=('stale lock',))
        self.assertEqual((code, error), (2, 'rows\n'))
        self.assertEqual(read(output), CandidateResult(1))

    def test_without_json_it_prints_the_summary_a_person_reads(self):
        code, output, error = self.candidate()
        self.assertEqual((code, error), (1, ''))
        self.assertEqual(output, 'rows\nManaged changes: 2; blocking issues: 0; unmanaged differences left untouched: 1.\n')


if __name__ == '__main__':
    unittest.main()
