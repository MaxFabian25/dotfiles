from __future__ import annotations

from pathlib import Path, PurePosixPath
import subprocess

from description import (AlignedInstall, BuildSource, CourseRecipe, EngineInstall, GenericRecipe, MarketplaceInstall,
                         LocalSource, Package, RepoSource, Roots, Setup, SkillRoot, SnapshotRecipe, SnapshotSource, Visibility, entry_path)
from host_state import CATALOGS, Host, SkillMode
import portable


def _worktree(path: Path) -> Path:
    existing = next(parent for parent in (path, *path.parents) if parent.exists())
    try:
        top = subprocess.run([*portable.GIT, '-C', str(existing), 'rev-parse', '--show-toplevel'],
                             capture_output=True, text=True, check=True).stdout.strip()
        return Path(top).resolve()
    except (OSError, subprocess.CalledProcessError):
        return path.resolve()


def check_placement(roots: Roots) -> list[str]:
    """Neither root may sit inside the other's git worktree."""
    if roots.private is None:
        return []
    public_tree, private_tree = _worktree(roots.public), _worktree(roots.private)
    if roots.private.resolve().is_relative_to(public_tree) or roots.public.resolve().is_relative_to(private_tree):
        return [f'{roots.private}:: the private root may not sit inside the public root\'s worktree, or the reverse']
    return []


def validate(setup: Setup) -> list[str]:
    """The checks load() runs across entries, on a setup held in memory: the public root alone first, then
    both roots together."""
    public = Visibility.PUBLIC
    alone = Setup(Roots(setup.roots.public, None),
                  {n: e for n, e in setup.packages.items() if e.visibility == public},
                  {n: e for n, e in setup.repos.items() if e.visibility == public},
                  {n: e for n, e in setup.loose.items() if e.visibility == public},
                  {}, None, setup.note_rules, setup.public_repositories,
                  {n: lock for n, lock in setup.lock.items() if setup.visibility(n) == public})
    errors: list[str] = []
    check_privacy(alone, errors)
    check_references(alone, Visibility.PUBLIC, errors)
    check_references(setup, Visibility.PRIVATE, errors)
    check_invariants(setup, errors)
    return errors


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
        for host in Host:
            placement = package.placement(host)
            installs = placement.installs if placement else ()
            if sum(install.enabled for install in installs) > 1:
                errors.append(f'{path}:{host.value}.install: at most one enabled install per host')
            for index, install in enumerate(installs):
                key = f'{host.value}.install[{index}]'
                if isinstance(install, EngineInstall) and package.source is None:
                    errors.append(f'{path}:{key}: an engine install needs a source')
                if isinstance(install, AlignedInstall) and install.enabled and not (
                        package.codex and any(i.enabled for i in package.codex.installs)):
                    errors.append(f'{path}:{key}: an enabled aligned copy needs an enabled Codex install')
        selected = package.source.recipe.skills() if isinstance(package.source, BuildSource) and package.source.recipe else ()
        splits = [i for i in (package.claude.installs if package.claude else ()) if isinstance(i, EngineInstall) and i.skills]
        if splits:
            shares = [skill for split in splits for skill in split.skills]
            if len(shares) != len(set(shares)) or (selected and set(shares) != set(selected)):
                errors.append(f'{path}:claude.install: split installs must partition the selected skills exactly')
        if selected and not isinstance(package.source.recipe, GenericRecipe):
            for skill in sorted(set(package.modes) - set(selected)):
                errors.append(f'{path}:skills: {skill} is not a selected skill')
        if SkillMode.ON in package.modes.values() and package.source is not None:
            errors.append(f'{path}:skills.on: only catalog packages take on')
        _check_claude_modes(package, path, selected, errors)
    for host, modes in setup.host_skills.items():
        path = (roots.private or roots.public) / 'hosts' / f'{host.value}.toml'
        for key in sorted(modes):
            owner = _toggle_owner(setup, host, key)
            if owner:
                errors.append(f'{path}:skills.{key}: {owner}')
    for name, skill in setup.loose.items():
        if isinstance(skill.source, LocalSource) and skill.source.variants and set(skill.source.variants) != set(skill.place):
            errors.append(f'{entry_path(roots.of(skill.visibility), "skills", name)}:source.variants: '
                          'names a path for each placement, and only those')
        if isinstance(skill.source, SnapshotSource) and set(skill.source.snapshots) != set(skill.place):
            errors.append(f'{entry_path(roots.of(skill.visibility), "skills", name)}:source.snapshot: '
                          'names a snapshot for each place the skill goes, and only those')
    for name in sorted(set(setup.packages) & set(setup.loose)):
        errors.append(f'{name}:: a package and a loose skill share the name, and the lock keys both by name')


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
        elif package_mode != SkillMode.OFF:
            errors.append(f'{key}: {skill} is {package_mode.value if package_mode else "auto"} for the package; '
                          'only a skill the package has off takes a Claude mode')


_SKILL_ROOTS = {Host.CODEX: (SkillRoot.CODEX, SkillRoot.AGENTS), Host.CLAUDE: (SkillRoot.CLAUDE,)}


def _toggle_owner(setup: Setup, host: Host, key: str) -> str | None:
    if ':' in key:
        return 'a plugin skill belongs to its package'
    for name, skill in setup.loose.items():
        placed = [root for root in _SKILL_ROOTS[host] if root in skill.place]
        if placed and key in {name, *(f'{{{root.value}_home}}/skills/{name}/SKILL.md' for root in placed)}:
            return f'owned by the loose skill {name}'
    return None
