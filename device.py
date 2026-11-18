from __future__ import annotations

from dataclasses import dataclass
from functools import partial
import os
from typing import Callable

from claude_host import ClaudeState
from codex_host import CodexState
import mise_tools
import portable
from host_state import DeviceList, DeviceRoots, Host, HostState, Platform


@dataclass(frozen=True)
class Device:
    platform: Platform
    roots: DeviceRoots
    hosts: tuple[HostState, ...]
    absent: tuple[Host, ...]
    wsl: bool = False
    # Installs the tools the home mise config pins. Device.current gives mise_tools.install; a device without it
    # installs none.
    install_tools: Callable[[], None] | None = None

    @property
    def device_list(self) -> DeviceList:
        return DeviceList.of(self.platform, self.wsl)

    @classmethod
    def current(cls) -> Device:
        roots = DeviceRoots.current()
        hosts, absent = [], []
        for host, state, locate in ((Host.CODEX, CodexState, portable.codex_cli),
                                    (Host.CLAUDE, ClaudeState, portable.claude_cli)):
            try:
                hosts.append(state(roots, locate()))
            except FileNotFoundError:
                absent.append(host)
        platform = Platform.native()
        # WSL's kernel names Microsoft in its release, as release.windows_side reads it.
        wsl = platform == Platform.LINUX and 'microsoft' in os.uname().release.lower()
        return cls(platform, roots, tuple(hosts), tuple(absent), wsl,
                   partial(mise_tools.install, roots.home, mise_tools.state_folder(roots.home)))

    def host(self, host: Host) -> HostState:
        return next(state for state in self.hosts if state.host == host)
