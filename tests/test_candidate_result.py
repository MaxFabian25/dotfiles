from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))
from candidate_result import CandidateResult, HeldCache, dump, read
import release

HELD = HeldCache('plugin:claude:kit@m', Path('/cache/kit/1.0.0'), (4242, 4343))
LEASE = 'Claude lease: plugin:claude:kit@m at /cache/kit/1.0.0, held by running Claude processes 4242, 4343'
GOOD = {'blocking': 0, 'held': []}


class ReadTest(unittest.TestCase):
    def test_what_dump_writes_reads_back_as_the_same_result(self):
        result = CandidateResult(2, (HELD,))
        self.assertEqual(read(dump(result)), result)

    def test_a_missing_key_is_named(self):
        for key in GOOD:
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, f'^the candidate result has no {key}$'):
                read(json.dumps({k: v for k, v in GOOD.items() if k != key}))

    def test_a_held_cache_missing_its_holders_is_named(self):
        with self.assertRaisesRegex(ValueError, '^held cache 0 has no holders$'):
            read(json.dumps({**GOOD, 'held': [{'identity': 'kit', 'cache': '/c'}]}))

    def test_a_wrong_type_is_named_with_its_value(self):
        for value, shown in (('0', "'0'"), (0.0, '0.0')):
            with self.subTest(value=value), self.assertRaisesRegex(
                    ValueError, f'^the candidate result has blocking {shown}, which is not of type int$'):
                read(json.dumps({**GOOD, 'blocking': value}))

    def test_a_count_given_as_true_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'has blocking True, which is not of type int'):
            read(json.dumps({**GOOD, 'blocking': True}))

    def test_a_holder_that_is_not_a_process_id_is_named(self):
        for holder, shown in (('1', "'1'"), (True, 'True')):
            with self.subTest(holder=holder), self.assertRaisesRegex(
                    ValueError, f'^held cache 0 has holders that are not all process ids: \\[4242, {shown}\\]$'):
                read(json.dumps({**GOOD, 'held': [{'identity': 'kit', 'cache': '/c', 'holders': [4242, holder]}]}))

    def test_a_key_a_later_release_adds_is_ignored(self):
        held = {'identity': HELD.identity, 'cache': str(HELD.cache), 'holders': list(HELD.holders), 'since': 'boot'}
        self.assertEqual(read(json.dumps({'blocking': 0, 'held': [held], 'unmanaged': 4})), CandidateResult(0, (HELD,)))

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


def candidate_sync(result, code=1, rows=''):
    """A worktree's sync.py that writes rows to stderr and this candidate result to stdout, and exits with code."""
    return f'import sys\nsys.stderr.write({rows!r})\nprint({result!r})\nraise SystemExit({code})\n'



class DeviceCheckTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.public, self.private = Path(temp.name, 'public'), Path(temp.name, 'private')
        self.public.mkdir()
        self.private.mkdir()

    def check(self, sync):
        """Runs device_check on a worktree whose sync.py first stops unless it is asked for the candidate result of
        this release's public and private setup."""
        asked = ['--candidate', '--json', '--public', str(self.public / 'setup'), '--private', str(self.private / 'setup')]
        guard = f'import sys\nif sys.argv[1:] != {asked!r}:\n    sys.exit("sync.py was given " + repr(sys.argv[1:]))\n'
        (self.public / 'sync.py').write_text(guard + sync, encoding='utf-8')
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as error:
            try:
                release.device_check(self.public, self.private, 'nothing was pushed')
            except RuntimeError as stop:
                return output.getvalue(), error.getvalue(), str(stop)
        return output.getvalue(), error.getvalue(), None

    def test_a_release_with_changes_and_no_blocker_passes_quietly(self):
        self.assertEqual(self.check(candidate_sync(json.dumps(GOOD))), ('', '', None))

    def test_a_blocked_release_stops_and_shows_the_rows_that_name_each_blocker_and_its_fix(self):
        rows = 'kit (private)  stale-lock  skills/kit changed after its tree was pinned; run lock_setup.py --entry kit\n'
        blocked = json.dumps({**GOOD, 'blocking': 1})
        for code in (1, 2):
            with self.subTest(code=code):
                output, error, stop = self.check(candidate_sync(blocked, code=code, rows=rows))
                self.assertEqual(stop, f'the pull on this device would stop on this release (sync exited {code}); '
                                       'nothing was pushed')
                self.assertEqual((output, error), ('', rows))

    def test_a_crash_that_exits_1_without_a_result_stops(self):
        output, error, stop = self.check('raise KeyError("models")\n')
        self.assertEqual(stop, 'the pull on this device would stop on this release (sync exited 1 and printed no '
                               'result); nothing was pushed')
        self.assertIn("KeyError: 'models'", error)

    def test_a_sync_that_exits_0_or_prints_only_a_blank_line_without_a_result_stops(self):
        for code, sync in ((0, 'raise SystemExit(0)\n'), (1, 'print()\nraise SystemExit(1)\n')):
            with self.subTest(code=code, sync=sync):
                output, error, stop = self.check(sync)
                self.assertEqual(stop, f'the pull on this device would stop on this release (sync exited {code} and '
                                       'printed no result); nothing was pushed')

    def test_a_crash_after_other_output_stops_and_shows_that_output(self):
        output, error, stop = self.check('print("Managed changes: 1; blocking issues: 0.")\nraise KeyError("models")\n')
        self.assertEqual(stop, 'the pull on this device would stop on this release (sync exited 1; the candidate '
                               'result is not JSON: Expecting value, line 1); nothing was pushed')
        self.assertTrue(error.startswith('Managed changes: 1; blocking issues: 0.\n'), error)
        self.assertIn("KeyError: 'models'", error)

    def test_a_sync_that_stops_with_status_3_stops_the_release(self):
        sync = 'import sys\nprint("Sync stopped: unconfirmed alias", file=sys.stderr)\nraise SystemExit(3)\n'
        output, error, stop = self.check(sync)
        self.assertEqual(stop, 'the pull on this device would stop on this release (sync exited 3); nothing was pushed')
        self.assertEqual(error, 'Sync stopped: unconfirmed alias\n')

    def test_a_result_without_blockers_from_a_sync_that_exits_2_or_3_stops(self):
        for code in (2, 3):
            with self.subTest(code=code):
                output, error, stop = self.check(candidate_sync(json.dumps(GOOD), code=code))
                self.assertEqual(stop, f'the pull on this device would stop on this release (sync exited {code}); '
                                       'nothing was pushed')

    def test_each_held_claude_cache_is_named_with_what_the_pull_does_about_it(self):
        other = HeldCache('plugin:claude:pack@m', Path('/cache/pack/2.0.0'), (5151,))
        output, error, stop = self.check(candidate_sync(dump(CandidateResult(0, (HELD, other)))))
        self.assertIsNone(stop)
        after = ('; the pull here swaps it at once if only skill text changes, and otherwise applies the rest of the '
                 'release and replaces that cache once they exit\n')
        self.assertEqual(output, LEASE + after + 'Claude lease: plugin:claude:pack@m at /cache/pack/2.0.0, held by '
                                                 'running Claude processes 5151' + after)


class CandidateOutputTest(unittest.TestCase):
    """What sync.py --candidate prints, with --json for release.py and without it for a person."""

    def candidate(self, *flags, blocked=()):
        import sync
        device = mock.Mock()
        device.roots.installed_engine.return_value = None
        current = SimpleNamespace(changes=('a', 'b'), blocked=blocked, unmanaged=('c',), expected=(),
                                  observations={'kit': 'observed'}, result='plan', exit_code=2 if blocked else 1)
        owner = mock.Mock()
        owner.held_replacements.side_effect = lambda observed: (
            [(HELD.identity, HELD.cache, list(HELD.holders))] if observed is current.observations else [])

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
        self.assertEqual(read(output), CandidateResult(0, (HELD,)))

    def test_with_json_a_blocked_plan_also_writes_its_rows_to_stderr(self):
        code, output, error = self.candidate('--json', blocked=('stale lock',))
        self.assertEqual((code, error), (2, 'rows\n'))
        self.assertEqual(read(output), CandidateResult(1, (HELD,)))

    def test_without_json_it_prints_the_lease_lines_and_summary_a_person_reads(self):
        code, output, error = self.candidate()
        self.assertEqual((code, error), (1, ''))
        self.assertEqual(output, f'{LEASE}\nrows\nManaged changes: 2; blocking issues: 0; '
                                 'unmanaged differences left untouched: 1.\n')


if __name__ == '__main__':
    unittest.main()
