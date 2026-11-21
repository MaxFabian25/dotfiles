# Read a page in the user's own Chrome with `read_page.py`

With Chrome running, or at any time on the Omarchy laptop, run `scripts/read_page.py URL...` with Python 3; its `--help` says what it opens, reads and skips. Pass `--profile` with the profile that holds the subscriptions: `Default`, its default, on macOS. Elsewhere, read the profiles under `profile.info_cache` in Chrome's `Local State`, and ask the user which one when there is more than one. On WSL, pass `--cua cua-driver.exe` to drive Chrome on the Windows desktop, because WSL's own `cua-driver` draws on a hidden display.

The accessibility tree that `read_page.py` reads hides link URLs, so take article URLs from the NYT sitemap index (`https://www.nytimes.com/sitemaps/new/sitemap.xml.gz`, which lists monthly sitemaps of article URLs) or an FT stream's RSS (`https://www.ft.com/<stream>?format=rss`), not from site search. Read only a few NYT pages per task, because it shows a bot check after a longer run or a few searches; the script refuses more than five in one run. When the script stops at a bot check, tell the user.

On Windows and WSL, each page opens in a window of its own, because Windows refuses the address bar's Return key in the background, and a table prints one cell per line, because Windows' accessibility tree hides table rows. Before you run it on a Hyprland desktop, such as the Omarchy laptop, read [hyprland.md](hyprland.md).

For any other page the user asks you to read in their own Chrome, use the same script, and add `--expand` to open collapsed FAQ answers first. Pass `--out DIR` when the text feeds a deliverable, so the source survives a compaction.
