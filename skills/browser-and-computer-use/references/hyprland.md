# read_page.py on a Hyprland desktop

Reached from the paywalled-news bullet of `SKILL.md`.

On a Hyprland desktop, such as the Omarchy laptop, `scripts/read_page.py` runs unattended from a shell without the desktop's environment, such as a scheduled job's: it starts a `cua-driver` daemon of its own with that environment, opens each page in a window of its own on a hidden workspace, and when the user's Chrome isn't running, starts it there, and Chrome quits when the script closes its last window. Runs there wait for each other and go one at a time. On 14 November 2026, in a systemd user unit given only `PATH` and `HOME`, it read the FT's and The Economist's front pages that way while the user's focused window stayed focused.

A Chrome the user started is readable only when, as it started, `ACCESSIBILITY_ENABLED=1` was in its environment, which the laptop's app launcher sets, or the desktop's AT-SPI switch was on, and the script stops with exit 3 at one that is not: that day a Chrome started with neither showed accessibility tools no page, and every `cua-driver` exit turned that switch off.
