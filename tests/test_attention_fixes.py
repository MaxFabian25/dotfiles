"""What a blocked sync tells the agent reading it: which kinds of row stop it, and how to clear a hook approval."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import device_sync
import drift
from description import Visibility
from host_state import Approved, DeviceList, FactKey, Host, Platform, Surface

IMPECCABLE = drift.EntryRef('impeccable', Visibility.PUBLIC)
KEYED = drift.Difference(drift.Kind.APPROVAL, FactKey(Host.CODEX, Surface.HOOK, 'impeccable@personal'), Approved(3), None,
                         IMPECCABLE)
ACCOUNT = drift.Difference(drift.Kind.APPROVAL, None, None, None, IMPECCABLE, host=Host.CODEX,
                           detail='work: approve 3 impeccable@personal hooks in this account home')
STALE = drift.Difference(drift.Kind.STALE_LOCK, None, None, None, IMPECCABLE,
                         detail='its lock is missing or no longer matches the entry; run lock_setup.py --entry impeccable --write')
HOW = ('approve them only for a change the user approved, as dotfiles-private\'s '
       'skills/release-dotfiles/references/pulls.md says under "Trusted Codex hooks"')
ABSENT = drift.Difference(drift.Kind.HOST_ABSENT, None, None, None, None, host=Host.CODEX)
EDITED = drift.Difference(drift.Kind.LOCAL_EDIT, FactKey(Host.CLAUDE, Surface.PLUGIN, 'impeccable@personal'), None, None,
                          IMPECCABLE, detail='plugin:impeccable: Installed bytes differ from the accepted image')
EDITED_HOME = drift.Difference(drift.Kind.LOCAL_EDIT, None, None, None, IMPECCABLE, host=Host.CODEX,
                               detail='work: Account home plugin changed during sync: /h/.codex-work/plugins/x')
UNREAD = drift.Difference(drift.Kind.INCOMPLETE, None, None, None, None, host=Host.CODEX,
                          detail='config.toml: permission denied')
UNREAD_KEYED = drift.Difference(drift.Kind.INCOMPLETE, FactKey(Host.CLAUDE, Surface.PLUGIN, 'impeccable@personal'), None,
                                None, IMPECCABLE, detail='plugin:impeccable: [Errno 13] Permission denied')
KEEP = 'sync never overwrites an edit, so ask the user whether to keep it or move it aside, then pull again'
REREAD = 'fix the read this names, then pull again'


def report(*differences):
    return drift.render(drift.Diff(Platform.LINUX, DeviceList.LINUX, {Visibility.PUBLIC: None, Visibility.PRIVATE: None},
                                   {Host.CODEX: '0.169'}, {Visibility.PUBLIC: 1, Visibility.PRIVATE: 0}, {Host.CODEX: 1}, 0,
                                   differences, ()))


class AttentionRowsNameTheirFix(unittest.TestCase):
    def test_a_hook_approval_row_names_who_decides_and_where_the_command_is(self):
        for difference, row in ((KEYED, 'impeccable@personal  approval  want 3 hooks approved · have absent · '),
                                (ACCOUNT, 'work: approve 3 impeccable@personal hooks in this account home · ')):
            with self.subTest(row=row):
                self.assertIn(row + HOW + '\n', report(difference))

    def test_other_attention_rows_keep_their_own_fix_alone(self):
        self.assertNotIn('Trusted Codex hooks', report(STALE))

    def test_an_absent_host_row_names_the_variable_that_finds_its_cli_and_who_decides_an_install(self):
        self.assertIn('host-absent codex  host is not installed here · if its CLI is installed, set CODEX_CLI to its path; '
                      'otherwise ask the user whether this machine should have it\n', report(ABSENT))

    def test_a_local_edit_row_shows_what_changed_and_leaves_the_edit_to_the_user(self):
        for difference, row in ((EDITED, 'impeccable@personal  local-edit  want absent · have absent · '
                                         'plugin:impeccable: Installed bytes differ from the accepted image · '),
                                (EDITED_HOME, 'local-edit  codex  work: Account home plugin changed during sync: '
                                              '/h/.codex-work/plugins/x · ')):
            with self.subTest(row=row):
                self.assertIn(row + KEEP + '\n', report(difference))

    def test_an_incomplete_row_says_to_fix_the_read_it_names(self):
        for difference, row in ((UNREAD, 'incomplete  codex  config.toml: permission denied · '),
                                (UNREAD_KEYED, 'impeccable@personal  incomplete  plugin:impeccable: [Errno 13] '
                                               'Permission denied · ')):
            with self.subTest(row=row):
                self.assertIn(row + REREAD + '\n', report(difference))

    def test_every_kind_that_blocks_sync_names_its_fix(self):
        named = {drift.Kind.APPROVAL: HOW, drift.Kind.STALE_LOCK: 'run lock_setup.py', drift.Kind.HOST_ABSENT: 'ask the user',
                 drift.Kind.LOCAL_EDIT: KEEP, drift.Kind.INCOMPLETE: REREAD}
        rows = {drift.Kind.APPROVAL: KEYED, drift.Kind.STALE_LOCK: STALE, drift.Kind.HOST_ABSENT: ABSENT,
                drift.Kind.LOCAL_EDIT: EDITED, drift.Kind.INCOMPLETE: UNREAD}
        blocking = {kind for kind in drift.Kind if kind.verdict == drift.Verdict.ATTENTION}
        self.assertEqual(blocking, named.keys())
        for kind in blocking:
            with self.subTest(kind=kind):
                self.assertIn(named[kind], report(rows[kind]))

    def test_a_blocked_apply_names_every_blocking_kind_and_points_at_the_rows(self):
        plan = device_sync.Plan(None, {}, (), (KEYED,), ())
        with self.assertRaises(RuntimeError) as stop:
            device_sync.apply(None, None, plan)
        self.assertEqual(str(stop.exception), 'Sync is blocked by incomplete reads, stale locks, conflicts, local edits, '
                                              'hook approvals or hosts that are not installed; no host files changed, '
                                              'and each attention row names its fix')


if __name__ == '__main__':
    unittest.main()
