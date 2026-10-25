from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from configuration import _expand, installed_chrome, patch_fields, read_fields


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


if __name__ == '__main__':
    unittest.main()
