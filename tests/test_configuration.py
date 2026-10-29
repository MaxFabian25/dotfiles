from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from configuration import _expand, desired, installed_chrome, patch_fields, read_fields
from host_state import DeviceRoots, Platform


class ConfigurationTests(unittest.TestCase):
    def test_runtime_patch_preserves_out_of_order_array_tables(self):
        before = b'''[[root.config]]
value = 1

[unrelated]
value = 2

[[root.config]]
value = 3

[[root.config]]
value = 4

[features.multi_agent_v2]
expose_spawn_agent_model_overrides = false # managed
'''
        expected = b'''[[root.config]]
value = 1

[unrelated]
value = 2

[[root.config]]
value = 3

[[root.config]]
value = 4

[features.multi_agent_v2]
expose_spawn_agent_model_overrides = true # managed
'''
        fields = {('features', 'multi_agent_v2', 'expose_spawn_agent_model_overrides'): True}
        self.assertEqual(patch_fields(before, fields), expected)
        self.assertEqual(patch_fields(expected, fields), expected)

    def test_managed_fields_preserve_local_secrets_comments_and_line_endings(self):
        before = b'\xef\xbb\xbf# local settings\r\n[tools]\r\nnode = "22" # pinned\r\n[env]\r\nLOCAL_ONLY = "not-a-real-secret"\r\n'
        fields = {('tools', 'node'): '24'}
        after = patch_fields(before, fields)
        self.assertEqual(after, before.replace(b'"22"', b'"24"'))
        self.assertEqual(patch_fields(after, fields), after)
        self.assertEqual(read_fields(after, fields), {('tools', 'node'): {'present': True, 'value': '24'}})

    def test_absent_and_false_are_distinct(self):
        fields = {('features', 'flag'): True}
        self.assertEqual(read_fields(None, fields), {('features', 'flag'): {'present': False}})
        self.assertEqual(read_fields(b'[features]\nflag = false\n', fields),
                         {('features', 'flag'): {'present': True, 'value': False}})

    def test_conflicting_structure_is_not_replaced(self):
        before = b'features = "user-owned"\n'
        with self.assertRaises(ValueError):
            patch_fields(before, {('features', 'flag'): True})

    def test_npmrc_insert_preserves_credentials_comments_sections_and_line_endings(self):
        before = (b'\xef\xbb\xbf; local settings\r\n//registry.example.invalid/:_authToken=test-only-placeholder\r\n'
                  b'allow-scripts=@stripe/cli\r\n[scope]\r\nmin-release-age=9\r\n')
        fields = {('min-release-age',): '3'}
        self.assertEqual(read_fields(before, fields, 'npmrc'), {('min-release-age',): {'present': False}})
        after = patch_fields(before, fields, 'npmrc')
        self.assertEqual(after, before.replace(b'[scope]', b'min-release-age=3\r\n[scope]'))
        self.assertEqual(patch_fields(after, fields, 'npmrc'), after)
        self.assertEqual(read_fields(after, fields, 'npmrc'), {('min-release-age',): {'present': True, 'value': '3'}})

    def test_npmrc_patch_replaces_the_effective_assignment(self):
        before = b'min-release-age=1\n# min-release-age=5\nmin-release-age = "7"'
        fields = {('min-release-age',): '3'}
        self.assertEqual(read_fields(before, fields, 'npmrc'), {('min-release-age',): {'present': True, 'value': '7'}})
        self.assertEqual(patch_fields(before, fields, 'npmrc'), b'min-release-age=1\n# min-release-age=5\nmin-release-age=3')

    def test_npmrc_insert_creates_or_terminates_the_file(self):
        fields = {('min-release-age',): '3'}
        self.assertEqual(patch_fields(None, fields, 'npmrc'), b'min-release-age=3\n')
        self.assertEqual(patch_fields(b'fund=false', fields, 'npmrc'), b'fund=false\nmin-release-age=3\n')

    def test_json_patch_keeps_other_keys_and_unchanged_bytes(self):
        fields = {('executablePath',): '/opt/google/chrome/chrome'}
        self.assertEqual(patch_fields(None, fields, 'json'), b'{\n  "executablePath": "/opt/google/chrome/chrome"\n}\n')
        before = b'{"headed": true, "executablePath": "/old/chrome", "extensions": ["/x"]}'
        after = patch_fields(before, fields, 'json')
        self.assertEqual(after, b'{\n  "headed": true,\n  "executablePath": "/opt/google/chrome/chrome",\n'
                                b'  "extensions": [\n    "/x"\n  ]\n}\n')
        self.assertEqual(patch_fields(after, fields, 'json'), after)
        compact = b'{"executablePath":"/opt/google/chrome/chrome","headed":true}'
        self.assertEqual(patch_fields(compact, fields, 'json'), compact)
        self.assertEqual(read_fields(after, fields, 'json'),
                         {('executablePath',): {'present': True, 'value': '/opt/google/chrome/chrome'}})
        with self.assertRaises(ValueError):
            read_fields(b'["not", "an", "object"]', fields, 'json')

    def test_installed_chrome_keeps_a_working_choice_and_never_invents_one(self):
        installed = {'/opt/google/chrome/chrome', '/usr/bin/google-chrome-stable', '/cft/chrome'}.__contains__
        candidates = ['/missing/chrome', '/opt/google/chrome/chrome', '/usr/bin/google-chrome-stable']
        self.assertEqual(installed_chrome(None, candidates, installed), '/opt/google/chrome/chrome')
        self.assertEqual(installed_chrome('/usr/bin/google-chrome-stable', candidates, installed),
                         '/usr/bin/google-chrome-stable')
        self.assertEqual(installed_chrome('/cft/chrome', candidates, installed), '/opt/google/chrome/chrome')
        self.assertEqual(installed_chrome('/missing/chrome', candidates, installed), '/opt/google/chrome/chrome')
        self.assertIsNone(installed_chrome('/cft/chrome', ['/missing/chrome'], installed))

    def test_windows_candidates_need_their_variables(self):
        template = r'%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe'
        self.assertEqual(_expand(template, Path('/home'), {'ProgramFiles(x86)': r'C:\Program Files (x86)'}),
                         r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe')
        self.assertIsNone(_expand(template, Path('/home'), {}))
        self.assertEqual(_expand('{home}/Applications/Chrome', Path('/Users/u'), {}), '/Users/u/Applications/Chrome')


class PstackPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.private = root / 'private'
        self.source = self.private / 'config/pstack'
        self.source.mkdir(parents=True)
        home = root / 'home'
        self.roots = DeviceRoots(home, home / '.codex', home / '.claude', home / '.agents', root / 'engine')

    def configure(self, settings, codex='current-model@high', claude='opus@high'):
        models = f'## claude-code\narena runners: {claude}\n## codex\narena runners: {codex}\n'.encode()
        (self.source / 'models.md').write_bytes(models)
        (self.source / 'runtime.toml').write_text(settings)
        return desired(None, self.private, self.roots, Platform.LINUX)

    def test_excluded_models_are_rejected_in_direct_prefixed_and_panel_seats(self):
        for codex, claude, section in [
            ('retired-model@high', 'opus@high', 'codex'),
            ('codex:retired-model@high', 'opus@high', 'codex'),
            ('current-model@high, retired-model@high', 'opus@high', 'codex'),
            ('current-model@high', 'opus@high, codex:retired-model@high', 'claude-code'),
        ]:
            with self.subTest(codex=codex, claude=claude), self.assertRaisesRegex(
                    ValueError, f'Excluded Codex model for {section}/arena runners: retired-model'):
                self.configure('spawn_overrides = true\nexcluded_codex_models = ["retired-model"]\n', codex, claude)
            self.assertFalse(self.roots.home.exists())

    def test_exclusion_is_exact_and_stays_out_of_native_config(self):
        configurations = self.configure(
            'spawn_overrides = false\nexcluded_codex_models = ["current", "current-model-old", "opus"]\n',
            codex='current-model@high, claude:opus@high')
        models, runtime = configurations
        self.assertEqual(models.data, (self.source / 'models.md').read_bytes())
        self.assertEqual(runtime.fields, {('features', 'multi_agent_v2', 'expose_spawn_agent_model_overrides'): False})

    def test_missing_or_empty_exclusions_preserve_existing_behavior(self):
        for policy in ('', 'excluded_codex_models = []\n'):
            with self.subTest(policy=policy):
                configurations = self.configure('spawn_overrides = true\n' + policy, codex='retired-model@high')
                self.assertEqual(len(configurations), 2)

    def test_inherited_model_selection_is_not_a_concrete_excluded_model(self):
        for model in ('auto', 'inherit-parent'):
            with self.subTest(model=model):
                self.assertEqual(len(self.configure(
                    'spawn_overrides = true\nexcluded_codex_models = ["retired-model"]\n',
                    codex=f'{model}@high')), 2)

    def test_malformed_exclusion_policy_is_rejected(self):
        for value in ('"retired-model"', 'false', '1', '{}', '[1]', '[""]', '[" "]',
                      '[" retired-model"]', '["retired model"]', '["codex:retired-model"]',
                      '["retired-model@high"]', '["auto"]', '["inherit-parent"]'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'excluded_codex_models'):
                self.configure(f'spawn_overrides = true\nexcluded_codex_models = {value}\n')

    def test_required_boolean_and_unknown_runtime_keys_remain_rejected(self):
        for settings in ('', 'spawn_overrides = "true"\n',
                         'spawn_overrides = true\nunknown = []\n'):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                self.configure(settings)


if __name__ == '__main__':
    unittest.main()
