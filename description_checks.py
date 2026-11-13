from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import stat
import subprocess

from description import (AlignedInstall, BuildSource, CourseRecipe, EngineInstall, GenericRecipe, Hooks, MarketplaceInstall,
                         LocalSource, Package, RepoSource, Roots, Setup, SkillRoot, SnapshotRecipe, SnapshotSource, ToolNotes,
                         Visibility, entry_path)
from host_state import ANTHROPIC_SKILLS, CATALOGS, PRIMARY_RUNTIME, Host, SkillMode
import portable


class _PlacementDiscoveryError(Exception):
    pass


def _worktree(path: Path) -> Path | None:
    try:
        resolved = path.resolve()
        for existing in (resolved, *resolved.parents):
            try:
                mode = existing.stat().st_mode
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(mode):
                raise NotADirectoryError(f'{existing} is not a directory')
            break
        else:
            raise FileNotFoundError(f'{path} has no existing ancestor')
        environment = os.environ.copy()
        for name in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_CEILING_DIRECTORIES',
                     'GIT_CONFIG_PARAMETERS', 'GIT_CONFIG_COUNT'):
            environment.pop(name, None)
        environment.update(LC_ALL='C', LANGUAGE='C', GIT_DISCOVERY_ACROSS_FILESYSTEM='1')
        result = subprocess.run([*portable.GIT, '-C', str(existing), 'rev-parse', '--show-toplevel'],
                                capture_output=True, check=False, timeout=30, env=environment)
        if result.returncode == 0:
            invalid = f'git returned an invalid worktree path: {result.stdout!r}'
            if not result.stdout.endswith(b'\n'):
                raise ValueError(invalid)
            top = result.stdout[:-2] if os.name == 'nt' and result.stdout.endswith(b'\r\n') else result.stdout[:-1]
            if not top or b'\0' in top:
                raise ValueError(invalid)
            tree = Path(os.fsdecode(top))
            if not tree.is_absolute():
                raise ValueError(invalid)
            tree = tree.resolve(strict=True)
            if not stat.S_ISDIR(tree.stat().st_mode) or not existing.is_relative_to(tree):
                raise ValueError(invalid)
            return tree
        detail = f'git exited {result.returncode}: {result.stderr.decode("utf-8", "backslashreplace").rstrip() or "no diagnostic"}'
        if result.stdout:
            detail += f'; stdout={result.stdout.decode("utf-8", "backslashreplace")!r}'
        absent = b'fatal: not a git repository (or any of the parent directories): .git\n'
        if (result.returncode == 128 and not result.stdout
                and result.stderr in (absent, absent.replace(b'\n', b'\r\n') if os.name == 'nt' else absent)):
            for ancestor in (existing, *existing.parents):
                marker = ancestor / '.git'
                try:
                    marker.lstat()
                except FileNotFoundError:
                    continue
                raise _PlacementDiscoveryError(f'{path}:: cannot discover Git worktree: {detail}; found Git metadata at {marker}')
            return None
        raise _PlacementDiscoveryError(f'{path}:: cannot discover Git worktree: {detail}')
    except subprocess.TimeoutExpired as error:
        output = (error.stdout or b'').decode('utf-8', 'backslashreplace')
        diagnostic = (error.stderr or b'').decode('utf-8', 'backslashreplace')
        raise _PlacementDiscoveryError(f'{path}:: cannot discover Git worktree: git timed out after {error.timeout}s; '
                                       f'stdout={output!r}; stderr={diagnostic!r}') from error
    except (OSError, RuntimeError, ValueError) as error:
        raise _PlacementDiscoveryError(f'{path}:: cannot discover Git worktree: {error}') from error


def check_placement(roots: Roots) -> list[str]:
    """Neither root may sit inside the other's git worktree."""
    if roots.private is None:
        return []
    try:
        public_path, private_path = roots.public.resolve(), roots.private.resolve()
        public_tree, private_tree = _worktree(roots.public), _worktree(roots.private)
    except _PlacementDiscoveryError as error:
        return [str(error)]
    except (OSError, RuntimeError, ValueError) as error:
        return [f'{roots.public}, {roots.private}:: cannot resolve placement roots: {error}']
    public_boundary = public_tree if public_tree is not None else public_path
    private_boundary = private_tree if private_tree is not None else private_path
    if private_path.is_relative_to(public_boundary) or public_path.is_relative_to(private_boundary):
        return [f'{roots.private}:: the private root may not sit inside the public root\'s worktree, or the reverse']
    return []


def check_privacy(found: Setup, errors: list[str]) -> None:
    """Kinds that are private wherever they sit (amendment A2), checked on the public root read alone."""
    root = found.roots.public
    allowed = found.public_repositories

    def repository(path: Path, key: str, url: str) -> None:
        if url and url not in allowed:
            errors.append(f'{path}:{key}: {url} is not on public_repositories in setup.toml')

    for name, package in found.packages.items():
        path = entry_path(root, 'packages', name)
        source = package.source
        if source is None:
            errors.append(f'{path}:source: catalog and account toggles are private')
        elif isinstance(source, RepoSource) and source.imports:
            errors.append(f'{path}:source.imports: source snapshots are private')
        elif isinstance(source, BuildSource):
            recipe = source.recipe
            if isinstance(recipe, CourseRecipe):
                errors.append(f'{path}:source.build: courses are private')
            elif isinstance(recipe, SnapshotRecipe):
                errors.append(f'{path}:source.build: snapshots are private')
            elif recipe is not None:
                repository(path, 'source.repository', recipe.repository)
        if package.codex and package.codex.adaptation:
            repository(path, 'codex.adaptation.repository', package.codex.adaptation.repository)
    for name, repo in found.repos.items():
        repository(entry_path(root, 'repos', name), 'url', repo.url)
    for name, loose in found.loose.items():
        if isinstance(loose.source, SnapshotSource):
            errors.append(f'{entry_path(root, "skills", name)}:source.snapshot: personal snapshots are private')
    for host in found.host_skills:
        errors.append(f'{root / "hosts" / (host.value + ".toml")}:: host toggles are private')
    if found.rules is not None:
        errors.append(f'{root / "rules/AGENTS.md"}:: shared rules are private')
    for platform in found.expected_unmanaged:
        errors.append(f'{root / "devices" / (platform.value + ".toml")}:: device lists are private')


def check_references(setup: Setup, visibility: Visibility, errors: list[str]) -> None:
    """Every reference from one root's entries names a repo the setup holds, or a host catalog."""
    root, repos = setup.roots.of(visibility), setup.repos
    for name, package in setup.packages.items():
        if package.visibility != visibility:
            continue
        path = entry_path(root, 'packages', name)
        source = package.source
        if isinstance(source, RepoSource) and source.repo not in repos:
            errors.append(f'{path}:source.repo: unknown repo {source.repo}')
        for host in Host:
            placement = package.placement(host)
            for index, install in enumerate(placement.installs if placement else ()):
                if not isinstance(install, MarketplaceInstall):
                    continue
                key = f'{host.value}.install[{index}].via'
                if isinstance(source, RepoSource):
                    repo = repos.get(source.repo)
                    marketplace = repo.marketplace(host) if repo else None
                    if repo and (marketplace is None or marketplace.name != install.marketplace):
                        errors.append(f'{path}:{key}: repo {source.repo} does not register {install.marketplace} in {host.value}')
                elif source is None and install.marketplace not in CATALOGS[host]:
                    errors.append(f'{path}:{key}: unknown marketplace {install.marketplace}; without a source it must be a {host.value} catalog')
                elif (source is None and install.marketplace == PRIMARY_RUNTIME and install.enabled
                      and (runtime := setup.runtime) is not None
                      and (not package.platforms or runtime.platform in package.platforms)
                      and name not in runtime.plugins):
                    errors.append(f'{path}:{key}: the pinned {runtime.platform.value} runtime {runtime.version} offers no '
                                  f'{name}; set platforms to keep it off {runtime.platform.value}, or pin a runtime that has it')
                elif isinstance(source, (BuildSource, LocalSource)) and install.enabled:
                    errors.append(f'{path}:{key}: a build installs through the engine, not {install.marketplace}')
    for name, skill in setup.loose.items():
        if skill.visibility == visibility and isinstance(skill.source, RepoSource) and skill.source.repo not in repos:
            errors.append(f'{entry_path(root, "skills", name)}:source.repo: unknown repo {skill.source.repo}')


def check_invariants(setup: Setup, errors: list[str]) -> None:
    roots = setup.roots
    for entry in (*setup.packages.values(), *setup.loose.values()):
        if isinstance(entry.source, LocalSource):
            for path in (entry.source.path, *entry.source.variants.values()):
                if not path or PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts or '\\' in path or ':' in path:
                    errors.append(f'{entry.name}:source.local: expected a contained repository-relative path')
    for name, package in setup.packages.items():
        path = entry_path(roots.of(package.visibility), 'packages', name)
        if isinstance(package.source, LocalSource) and package.source.variants:
            errors.append(f'{path}:source.variants: only loose skills have host variants')
        if isinstance(package.source, RepoSource) and package.source.imports:
            for skill in package.source.imports:
                if not skill or skill in ('.', '..') or any(c in skill for c in '/\\:'):
                    errors.append(f'{path}:source.imports: expected a single skill directory name')
            for host in Host:
                placement = package.placement(host)
                if placement and any(i.enabled and not isinstance(i, (EngineInstall, AlignedInstall))
                                     for i in placement.installs):
                    errors.append(f'{path}:source.imports: composed packages install through an engine catalog')
        for host in Host:
            placement = package.placement(host)
            installs = placement.installs if placement else ()
            if sum(install.managed for install in installs) > 1:
                errors.append(f'{path}:{host.value}.install: at most one enabled or kept-current install per host')
            for index, install in enumerate(installs):
                key = f'{host.value}.install[{index}]'
                if isinstance(install, EngineInstall) and package.source is None:
                    errors.append(f'{path}:{key}: an engine install needs a source')
                # Claude Code runs these from the engine's catalog, which the install overlay's notes never reach.
                if (host == Host.CLAUDE and isinstance(install, EngineInstall) and install.managed
                        and package.tool_notes == ToolNotes.APPLY):
                    errors.append(f'{path}:{key}: Claude Code runs this install from the catalog, where tool notes do '
                                  'not reach; give it an aligned Claude install')
                if (host == Host.CLAUDE and install.managed and placement.windows_replace
                        and not isinstance(install, MarketplaceInstall)):
                    errors.append(f'{path}:{key}: Windows patches require a native marketplace install')
                if (host == Host.CLAUDE and install.managed and package.codex and package.codex.replace
                        and not isinstance(install, AlignedInstall)):
                    errors.append(f'{path}:{key}: codex.replace reaches Claude only through an aligned install')
                if isinstance(install, AlignedInstall) and install.managed and not (
                        package.codex and any(i.managed for i in package.codex.installs)):
                    errors.append(f'{path}:{key}: an enabled or kept-current aligned copy needs an enabled or '
                                  'kept-current Codex install')
                # Role files and hook approvals act in every session, so they follow the switch, not keep_current.
                if (host == Host.CODEX and not install.enabled and install.managed
                        and (placement.roles is not None or placement.hooks == Hooks.TRUSTED)):
                    errors.append(f'{path}:{key}.keep_current: a kept-current install gets no Codex roles or hook '
                                  'approval; enable it, or drop its roles and trusted hooks')
        selected = package.source.recipe.skills() if isinstance(package.source, BuildSource) and package.source.recipe else ()
        splits = [i for i in (package.claude.installs if package.claude else ()) if isinstance(i, EngineInstall) and i.skills]
        if splits:
            shares = [skill for split in splits for skill in split.skills]
            if len(shares) != len(set(shares)) or (selected and set(shares) != set(selected)):
                errors.append(f'{path}:claude.install: split installs must partition the selected skills exactly')
        if selected and not isinstance(package.source.recipe, GenericRecipe):
            for skill in sorted(set(package.modes) - set(selected)):
                errors.append(f'{path}:skills: {skill} is not a selected skill')
        _check_claude_modes(package, path, selected, errors)
        _check_claude_off(package, path, errors)
    for host, modes in setup.host_skills.items():
        path = (roots.private or roots.public) / 'hosts' / f'{host.value}.toml'
        for key in sorted(modes):
            owner = _toggle_owner(setup, host, key)
            if owner:
                errors.append(f'{path}:skills.{key}: {owner}')
    for name, skill in setup.loose.items():
        if isinstance(skill.source, RepoSource) and skill.source.imports:
            errors.append(f'{entry_path(roots.of(skill.visibility), "skills", name)}:source.imports: only packages import skills')
        if isinstance(skill.source, LocalSource) and skill.source.variants and set(skill.source.variants) != set(skill.place):
            errors.append(f'{entry_path(roots.of(skill.visibility), "skills", name)}:source.variants: '
                          'names a path for each placement, and only those')
        if isinstance(skill.source, SnapshotSource) and set(skill.source.snapshots) != set(skill.place):
            errors.append(f'{entry_path(roots.of(skill.visibility), "skills", name)}:source.snapshot: '
                          'names a snapshot for each place the skill goes, and only those')
    for name in sorted(set(setup.packages) & set(setup.loose)):
        errors.append(f'{name}:: a package and a loose skill share the name, and the lock keys both by name')


def _check_claude_off(package: Package, path: Path, errors: list[str]) -> None:
    """Claude Code 2.1.319 kept offering a personal-collections skill that skillOverrides turned off, with no drift
    reported, so the only way to drop one is to leave it out of the build."""
    off = {skill for skill, mode in package.modes.items() if mode == SkillMode.OFF}
    stuck = set()
    for install in package.claude.installs if package.claude else ():
        if isinstance(install, EngineInstall) and install.managed:
            stuck |= off & set(install.skills) if install.skills else off
    if stuck:
        errors.append(f'{path}:skills.off: {", ".join(sorted(stuck))} stay offered in Claude Code, which ignores '
                      'skillOverrides for a personal-collections install; remove them from source.select and raise '
                      'source.packaging')


def _check_claude_modes(package: Package, path: Path, selected: tuple[str, ...], errors: list[str]) -> None:
    claude = package.claude
    if claude is None or not claude.modes:
        return
    if not any(isinstance(install, AlignedInstall) for install in claude.installs):
        errors.append(f'{path}:claude.skills: a Claude mode needs an aligned Claude install')
    recipe = package.source.recipe if isinstance(package.source, BuildSource) else None
    # A generic recipe with an empty select takes every skill its manifest lists.
    every = isinstance(recipe, GenericRecipe) and not recipe.select
    for skill, mode in sorted(claude.modes.items()):
        key = f'{path}:claude.skills.{mode.value}'
        package_mode = package.modes.get(skill)
        if recipe is None or not (every or skill in selected):
            errors.append(f'{key}: {skill} is not a selected skill')
        elif package_mode != SkillMode.OFF and (package_mode, mode) != (SkillMode.EXPLICIT, SkillMode.ON):
            errors.append(f'{key}: {skill} is {package_mode.value if package_mode else "auto"} for the package; '
                          'a Claude mode needs an off skill, or is on for an explicit one')


_SKILL_ROOTS = {Host.CODEX: (SkillRoot.CODEX, SkillRoot.AGENTS), Host.CLAUDE: (SkillRoot.CLAUDE,)}


def _toggle_owner(setup: Setup, host: Host, key: str) -> str | None:
    if ':' in key and not (host == Host.CLAUDE and key.startswith(ANTHROPIC_SKILLS + ':')):
        return 'a plugin skill belongs to its package'
    for name, skill in setup.loose.items():
        placed = [root for root in _SKILL_ROOTS[host] if root in skill.place]
        if placed and key in {name, *(f'{{{root.value}_home}}/skills/{name}/SKILL.md' for root in placed)}:
            return f'owned by the loose skill {name}'
    return None
