from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))


class CandidateOutputTest(unittest.TestCase):
    """What sync.py --candidate prints."""

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

    def test_it_prints_the_rows_and_the_summary_a_person_reads(self):
        code, output, error = self.candidate()
        self.assertEqual((code, error), (1, ''))
        self.assertEqual(output, 'rows\nManaged changes: 2; blocking issues: 0; unmanaged differences left untouched: 1.\n')

    def test_a_blocked_plan_counts_its_blockers_and_exits_2(self):
        code, output, error = self.candidate(blocked=('stale lock',))
        self.assertEqual((code, error), (2, ''))
        self.assertIn('blocking issues: 1;', output)


if __name__ == '__main__':
    unittest.main()
