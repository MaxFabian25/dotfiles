from __future__ import annotations

from dataclasses import dataclass

from claude_host import ClaudeState
from codex_host import CodexState
import portable
from host_state import DeviceRoots, Host, HostState, Platform


@dataclass(frozen=True)
class Device:
    platform: Platform
    roots: DeviceRoots
    hosts: tuple[HostState, ...]
    absent: tuple[Host, ...]

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
        return cls(Platform.native(), roots, tuple(hosts), tuple(absent))

    def host(self, host: Host) -> HostState:
        return next(state for state in self.hosts if state.host == host)
