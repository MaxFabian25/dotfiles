from pathlib import Path
import unittest

from configuration import patch_fields, read_fields


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


if __name__ == '__main__':
    unittest.main()
