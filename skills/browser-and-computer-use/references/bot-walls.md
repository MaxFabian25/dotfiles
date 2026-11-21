# Pages behind a bot wall

When WebFetch, curl, or headless agent-browser gets a 403, a Cloudflare check, or another bot wall on a page you only read, outside the user's signed-in accounts, first try the routes that show nothing on the user's desktop: a web search limited to the site's domain to find each page's address, the site's `/llms.txt` or a `.md` version of the page, and an exact-URL Internet Archive copy at `https://web.archive.org/web/<year>/<url>`, which redirects to the nearest copy and returned the page on 9 November 2026. Fetch it with curl's `--compressed`, because the archive can send it gzipped. A dated copy you saved, or an archive copy, answers whether a link is alive: on 11 November 2026 `https://archive.org/wayback/available?url=<url>` returned a dated copy with status 200 for 7 of 8 pages that refused curl and WebFetch.

## A headed session on the hidden display

SKILL.md starts a `--headed` session on the hidden display `:99` with `scripts/ab_session.py start`. `start` saves that display with the flags, and every later `run NAME ...` and `close NAME` puts it back, so a run after that session's browser stopped starts it again on `:99`, not on the user's desktop. agent-browser 0.42.0's `--help` stops a session after an hour idle by default, but exempts headed ones from that default; a crash or restart stops it too.
