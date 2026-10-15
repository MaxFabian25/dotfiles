"""Check a staged plugin against the currently installed skill contract."""
import argparse
import json
from pathlib import Path

import yaml

from portable import read_utf8, write_utf8


def manifest(root):
    for relative in ('plugin.json','.codex-plugin/plugin.json','.claude-plugin/plugin.json'):
        path=root/relative
        if path.is_file():return relative,json.loads(read_utf8(path))
    raise ValueError(f'No plugin manifest in {root}')


def skill_contract(root):
    relative,data=manifest(root)
    declared=['./skills/'] if relative=='plugin.json' else data.get('skills',['./skills/'])
    if isinstance(declared,str):declared=[declared]
    result={}
    for entry in declared:
        selected=(root/entry).resolve()
        if not selected.is_relative_to(root.resolve()):raise ValueError('Skill root escapes plugin')
        if not selected.is_dir():raise ValueError(f'Missing exported skill root: {entry}')
        for path in selected.rglob('SKILL.md'):
            if any(p in ('.git','node_modules','references','templates','examples') for p in path.relative_to(selected).parts):continue
            text=read_utf8(path);parts=text.split('---',2)
            if len(parts)!=3 or parts[0].strip():raise ValueError(f'Invalid skill frontmatter: {path}')
            front=yaml.safe_load(parts[1]);name=front.get('name');description=front.get('description')
            if not isinstance(name,str) or not name:raise ValueError(f'Invalid skill name: {path}')
            if not isinstance(description,str) or not 0<len(description)<=1024:raise ValueError(f'Invalid description: {path}')
            if name in result and result[name]['path']!=str(path):raise ValueError(f'Duplicate skill name: {name}')
            sidecar=path.parent/'agents/openai.yaml'
            settings=yaml.safe_load(read_utf8(sidecar)) or {} if sidecar.is_file() else {}
            result[name]={
                'path':str(path),
                'invocation':{
                    'allow_implicit_invocation':settings.get('policy',{}).get('allow_implicit_invocation',True),
                    'disable_model_invocation':front.get('disable-model-invocation',False),
                    'user_invocable':front.get('user-invocable',True),
                    'allowed_tools':front.get('allowed-tools'),
                },
            }
    return data,result


def validate(previous,candidate,profile=None,*,allowed_added_skills=(),allow_implicit_additions=False,upstream_invocation_skills=()):
    old_manifest,old=skill_contract(previous);new_manifest,new=skill_contract(candidate)
    if old_manifest.get('name')!=new_manifest.get('name'):raise ValueError('Plugin identity changed')
    removed=set(old)-set(new);added=set(new)-set(old);reviewed=set(allowed_added_skills)
    already_present=not added and reviewed.issubset(old)
    if removed or (added!=reviewed and not already_present):raise ValueError(f'Selection changed; removed={sorted(removed)}, added={sorted(added)}, reviewed additions={sorted(reviewed)}')
    for name in added:
        if new[name]['invocation']['allow_implicit_invocation'] and not allow_implicit_additions:
            raise ValueError(f'Reviewed new skill must be explicit-only: {name}')
    if profile and profile.get('kind') in {'course', 'public-emil'}:
        expected={s['installedName'] for s in profile['selectedSkills']}
        if set(new)!=expected:raise ValueError('Skill selection differs from the recorded protected list')
    followed=[]
    for name in old:
        if old[name]['invocation']!=new[name]['invocation']:
            # A recorded follow-upstream collection may change only the sidecar policy, to upstream's value.
            same_otherwise={**old[name]['invocation'],'allow_implicit_invocation':new[name]['invocation']['allow_implicit_invocation']}==new[name]['invocation']
            if name not in upstream_invocation_skills or not same_otherwise:raise ValueError(f'Invocation settings changed: {name}')
            followed.append(name)
    for key in ('mcpServers','hooks','apps','extensions'):
        if old_manifest.get(key)!=new_manifest.get(key):
            # Presentation-only fields are not permissions.
            def runtime(value):
                if key!='extensions' or not isinstance(value,dict):return value
                cleaned=json.loads(json.dumps(value))
                if isinstance(cleaned.get('com.openai'),dict):cleaned['com.openai'].pop('interface',None)
                return cleaned
            if runtime(old_manifest.get(key))!=runtime(new_manifest.get(key)):raise ValueError(f'Plugin runtime configuration changed: {key}')
    for relative in ('mcp.json','.mcp.json','.app.json','hooks/hooks.json'):
        def content(root):
            p=root/relative;return p.read_bytes() if p.is_file() else None
        if content(previous)!=content(candidate):raise ValueError(f'Plugin integration configuration changed: {relative}')
    for root in (previous,candidate):
        for path in root.rglob('*'):
            if path.is_symlink() and not path.resolve().is_relative_to(root.resolve()):raise ValueError(f'Package symlink escapes root: {path}')
    result={'skills':len(new),'selectionPreserved':not added,'invocationPreserved':not followed,'runtimeConfigurationPreserved':True}
    if followed:result['invocationFollowsUpstream']=sorted(followed)
    if added:result['reviewedAddedSkills']=sorted(added)
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('previous',type=Path);p.add_argument('candidate',type=Path);a=p.parse_args()
    print(json.dumps(validate(a.previous,a.candidate),indent=2))
