# Track repository interest

Maintainers can view [GitHub Insights → Traffic](https://github.com/jamessi1989/thunderbirdmcp/graphs/traffic)
for page views, unique visitors, full clones, referral sites and popular pages.
GitHub exposes a rolling 14-day traffic window, with UTC dates. Views/clones
update hourly; referral and popular-page summaries update daily. See
[GitHub's traffic documentation](https://docs.github.com/en/repositories/viewing-activity-and-data-for-your-repository/viewing-traffic-to-a-repository).

The maintainer script preserves daily snapshots of those aggregates, stars,
forks and published release asset download counts. It uses an existing `gh`
login with access to the repository's traffic APIs. It does not access mail,
install tracking in the MCP, or collect individual visitor identities.

```bash
python3 scripts/repo_traffic.py collect
python3 scripts/repo_traffic.py report
python3 scripts/repo_traffic.py report --json
```

Snapshots stay outside Git under
`$XDG_DATA_HOME/thunderbird-mcp/repo-traffic/OWNER--REPOSITORY`, falling back to
`~/.local/share`. Each UTC day retains its latest successful collection; failed
requests leave the previous snapshot intact. Reports read the newest saved
snapshot without calling GitHub and show when it was collected. The JSON keeps
GitHub's daily breakdown and rolling-window totals. Overlapping windows and
daily unique counts must not be summed into lifetime unique visitors. Snapshot
history is retained until the maintainer removes it.

For a Linux machine that remains available, the repository includes a user
systemd timer. Adjust the checkout/Python paths in `thunderbird-repo-traffic.service` if
needed; the service must be able to use your existing GitHub CLI login.

```bash
mkdir -p ~/.config/systemd/user
cp systemd/thunderbird-repo-traffic.service systemd/thunderbird-repo-traffic.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user start thunderbird-repo-traffic.service
systemctl --user enable --now thunderbird-repo-traffic.timer
systemctl --user list-timers thunderbird-repo-traffic.timer
```

It collects daily after 06:00 UTC with up to 15 minutes of jitter, and catches up
after downtime while the user service manager is running. Check failures with
`journalctl --user -u thunderbird-repo-traffic.service`; stop collection with
`systemctl --user disable --now thunderbird-repo-traffic.timer` and
`systemctl --user stop thunderbird-repo-traffic.service`.

Clones and release downloads can include CI, bots and the maintainer's own
verification. They do not measure installations or active MCP users. Referral
reporting is incomplete: GitHub excludes search engines and GitHub itself, and
only returns its top reported sources. An empty list does not establish that
nobody arrived from ChatGPT. These metrics cannot count AI answer impressions
or recommendations. The collector makes no claim to identify human visitors.
