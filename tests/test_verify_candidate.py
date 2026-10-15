from pathlib import Path
import json
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from verify_candidate import validate


class ReviewedAdditions(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.old = Path(self.temporary.name)/'old'
        self.new = Path(self.temporary.name)/'new'
        for root in (self.old,self.new):
            (root/'.codex-plugin').mkdir(parents=True)
            (root/'.codex-plugin/plugin.json').write_text(json.dumps({'name':'example','skills':'./skills/'}))
            self.skill(root,'existing')

    def skill(self,root,name,implicit=False):
        folder=root/'skills'/name
        (folder/'agents').mkdir(parents=True,exist_ok=True)
        (folder/'SKILL.md').write_text(f'---\nname: {name}\ndescription: A test workflow.\n---\nRead the supplied input.\n')
        (folder/'agents/openai.yaml').write_text('policy:\n  allow_implicit_invocation: '+str(implicit).lower()+'\n')

    def test_default_rejects_new_skill(self):
        self.skill(self.new,'new-workflow')
        with self.assertRaisesRegex(ValueError,'Selection changed'):
            validate(self.old,self.new)

    def test_reviewed_explicit_addition_is_reported(self):
        self.skill(self.new,'new-workflow')
        result=validate(self.old,self.new,allowed_added_skills=['new-workflow'])
        self.assertEqual(result,{'skills':2,'selectionPreserved':False,'invocationPreserved':True,'runtimeConfigurationPreserved':True,'reviewedAddedSkills':['new-workflow']})

    def test_reviewed_names_must_match_exactly(self):
        self.skill(self.new,'new-workflow')
        with self.assertRaisesRegex(ValueError,'Selection changed'):
            validate(self.old,self.new,allowed_added_skills=['different-workflow'])

    def test_reviewed_addition_can_be_verified_again(self):
        self.skill(self.new,'new-workflow')
        result=validate(self.new,self.new,allowed_added_skills=['new-workflow'])
        self.assertEqual(result,{'skills':2,'selectionPreserved':True,'invocationPreserved':True,'runtimeConfigurationPreserved':True})

    def test_protected_course_selection_cannot_expand(self):
        self.skill(self.new,'new-workflow')
        profile={'kind':'course','selectedSkills':[{'installedName':'existing'}]}
        with self.assertRaisesRegex(ValueError,'protected list'):
            validate(self.old,self.new,profile,allowed_added_skills=['new-workflow'])

    def test_addition_cannot_be_automatic(self):
        self.skill(self.new,'new-workflow',implicit=True)
        with self.assertRaisesRegex(ValueError,'must be explicit-only'):
            validate(self.old,self.new,allowed_added_skills=['new-workflow'])

    def test_follow_upstream_allows_only_the_sidecar_policy_to_change(self):
        self.skill(self.new,'existing',implicit=True)
        with self.assertRaisesRegex(ValueError,'Invocation settings changed'):
            validate(self.old,self.new)
        result=validate(self.old,self.new,upstream_invocation_skills={'existing'})
        self.assertEqual(result['invocationFollowsUpstream'],['existing'])
        self.assertFalse(result['invocationPreserved'])
        skill=self.new/'skills/existing/SKILL.md'
        skill.write_text(skill.read_text().replace('description:','allowed-tools: Bash\ndescription:'))
        with self.assertRaisesRegex(ValueError,'Invocation settings changed'):
            validate(self.old,self.new,upstream_invocation_skills={'existing'})

    def test_existing_invocation_cannot_change(self):
        self.skill(self.new,'existing',implicit=True)
        self.skill(self.new,'new-workflow')
        with self.assertRaisesRegex(ValueError,'Invocation settings changed'):
            validate(self.old,self.new,allowed_added_skills=['new-workflow'])

    def test_authorized_discovery_can_add_an_automatic_skill(self):
        self.skill(self.new,'new-workflow',implicit=True)
        result=validate(self.old,self.new,allowed_added_skills=['new-workflow'],allow_implicit_additions=True)
        self.assertEqual(result['reviewedAddedSkills'],['new-workflow'])
        self.assertTrue(result['invocationPreserved'])
        self.skill(self.new,'existing',implicit=True)
        with self.assertRaisesRegex(ValueError,'Invocation settings changed'):
            validate(self.old,self.new,allowed_added_skills=['new-workflow'],allow_implicit_additions=True)

    def test_review_does_not_allow_removals(self):
        self.skill(self.old,'removed-workflow')
        self.skill(self.new,'new-workflow')
        with self.assertRaisesRegex(ValueError,'Selection changed'):
            validate(self.old,self.new,allowed_added_skills=['new-workflow'])

    def test_review_does_not_allow_integration_changes(self):
        self.skill(self.new,'new-workflow')
        (self.new/'.mcp.json').write_text('{"mcpServers":{}}')
        with self.assertRaisesRegex(ValueError,'integration configuration changed'):
            validate(self.old,self.new,allowed_added_skills=['new-workflow'])


if __name__=='__main__':
    unittest.main()
