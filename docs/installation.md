# Installation

Start with the [synthetic demo](demo.md) if you want to try search, references,
and reply preparation without connecting mail. For a real mailbox, Thunderbird
must already have an account configured. Local indexing reads downloaded **mbox
and Maildir** folders. Live IMAP search reads the server
directly and does not need a local index.

## Requirements

- Python 3.10 or later; CI covers 3.10–3.12 on Linux and 3.12 on macOS,
  with a separate native Windows installation job on 3.12.
- [uv](https://docs.astral.sh/uv/getting-started/installation/) for the commands below.
- Thunderbird for account settings and, for local search, downloaded messages.
- A working system keyring for stored IMAP/SMTP credentials.

Linux is the primary tested desktop. macOS has profile discovery and automated
package/setup checks, but a successful CI run does not verify a real Thunderbird
desktop, keychain, and AI-client combination. Current source includes native
Windows profile discovery, terminal launchers and direct Python MCP startup.
Windows CI exercises a synthetic installed-wheel workflow; actual Windows
Thunderbird/provider/client combinations still need independent desktop checks.
Native Windows setup support is included from version 0.4.0.

## Install from a checkout

```bash
git clone https://github.com/jamessi1989/thunderbirdmcp.git ~/projects/thunderbirdmcp
cd ~/projects/thunderbirdmcp
uv sync --locked
uv run python scripts/demo.py
uv run tbmail accounts
uv run thunderbird-mcp-index
uv run thunderbird-mcp-setup --apply
```

Keep this checkout and its virtual environment in place: the generated commands
use their absolute paths. If you move the checkout, run setup again. The demo
does not read your real profile, alter client configuration, or send mail.

Setup chooses installed clients automatically. To select clients explicitly:

```bash
uv run thunderbird-mcp-setup --clients claude codex opencode
uv run thunderbird-mcp-setup --clients claude codex opencode --apply
```

The first command previews the change. The second writes it, preserving unrelated
settings and keeping private backups. `--clients none --apply` installs just the
terminal commands. If `tbmail` is not on your shell's PATH, use
`~/.local/bin/tbmail`, add `~/.local/bin` to PATH, or use `uv run tbmail` inside
the checkout. MCP registrations use an absolute command and do not depend on PATH.

## Install a release wheel

A release wheel installs into a separate tool environment; it does not require
keeping a source checkout. Version 0.4.0 is available from
[GitHub Releases](https://github.com/jamessi1989/thunderbirdmcp/releases/tag/v0.4.0):

```bash
uv tool install --python 3.12 https://github.com/jamessi1989/thunderbirdmcp/releases/download/v0.4.0/thunderbird_mcp-0.4.0-py3-none-any.whl
uv tool update-shell
```

Open a new shell if `uv tool update-shell` changes your PATH, then run:

```bash
thunderbird-mcp-setup --apply
tbmail accounts
tbmail reindex
```

The release includes the wheel, a source distribution, and `SHA256SUMS`.
Dependencies are downloaded during installation. The source distribution includes
the demo, tests, and dependency lock. This project is distributed through GitHub
Releases; the instructions do not assume an identically named package on PyPI.

## Connect credentials and check the account

`tbmail accounts` lists Thunderbird account IDs. Replace `account1` below with
the account you want to use. Password accounts use separate IMAP and SMTP
keyring entries:

```bash
tbmail password-set --account account1 --protocol imap
tbmail password-set --account account1 --protocol smtp
tbmail status --account account1 --network
```

The password commands prompt without echoing the value. Use a provider-issued
app password when your provider requires one. Profile discovery reads account
settings; it does not copy passwords out of Thunderbird's password database.
OAuth accounts can use [guided browser enrollment](workflows.md#gmail-api-graph-and-interactive-enrollment)
with a registered app, or the existing [token import setup](../README.md#oauth-accounts).

The network diagnostic verifies TLS, authentication, and SMTP NOOP without sending
mail. Then check local and live search:

```bash
tbmail search --keyword --limit 3
tbmail search --live --account account1 --limit 3
```

Run the following to add local semantic search. The first run can download the
embedding model and take longer than a client tool timeout:

```bash
tbmail reindex --embeddings
```

Reload your running AI client using the
[session reload instructions](../README.md#reload-an-existing-session). Ask it to
search with the thunderbird MCP, read a result, and return its reference. The
same reference can be pasted into a later session.

## Data and configuration paths

| Installation | Default local index |
| --- | --- |
| Source checkout | `mail-index.sqlite` in the checkout |
| Installed package on Linux | `$XDG_DATA_HOME/thunderbird-mcp/mail-index.sqlite`, or `~/.local/share/thunderbird-mcp/mail-index.sqlite` |
| Installed package on macOS | `$XDG_DATA_HOME/thunderbird-mcp/mail-index.sqlite` if set, otherwise `~/Library/Application Support/thunderbird-mcp/mail-index.sqlite` |
| Installed package on Windows | An absolute `XDG_DATA_HOME` if set, otherwise `%LOCALAPPDATA%\thunderbird-mcp\mail-index.sqlite` |

`TB_MAIL_INDEX_DB` overrides the index path. The outbox is stored beside the
index unless `TB_OUTBOX_DB` is set. Mail databases contain private mail and drafts;
they are not part of the public release. Moving between a checkout and a wheel
uses different default data directories: set explicit database paths if you want
both installations to use the same existing index and saved drafts. Use absolute
paths for `TB_PROFILE`, `TB_MAIL_INDEX_DB`, and `TB_OUTBOX_DB` when sharing settings
across sessions; relative paths depend on each process's working directory.

Client configuration paths are listed in the [README](../README.md#start-using-it).
Credentials stay in the system keyring and are not written to those files.

## Native Windows setup

In PowerShell, after installing Python and uv:

```powershell
git clone https://github.com/jamessi1989/thunderbirdmcp.git
Set-Location thunderbirdmcp
uv sync --locked
uv run thunderbird-mcp-setup --clients codex claude opencode
uv run thunderbird-mcp-setup --clients codex claude opencode --apply
uv run tbmail accounts
uv run tbmail reindex
```

Profile discovery uses `%APPDATA%\Thunderbird\profiles.ini`, as described in
[Thunderbird's profile documentation](https://support.mozilla.org/en-US/kb/profiles-where-thunderbird-stores-user-data).
For a different profile, set `$env:TB_PROFILE = 'C:\path\to\profile'` before
starting the CLI or client. Windows setup puts terminal `.cmd` launchers and
Python entry scripts in `%LOCALAPPDATA%\thunderbird-mcp\bin`; add that folder to
PATH or use `uv run tbmail` in the checkout. MCP entries execute the chosen
Python interpreter directly, preserving argument boundaries and the caller's
working directory. They do not depend on a shell or a `.cmd` file association.

Installed-wheel data defaults to
`%LOCALAPPDATA%\thunderbird-mcp\mail-index.sqlite`; the fallback is
`%USERPROFILE%\AppData\Local`. An absolute `XDG_DATA_HOME` or `TB_MAIL_INDEX_DB`
still takes precedence. Source checkouts retain their existing index path.
Config files and backups use the Windows user directory's inherited ACL;
POSIX permission bits do not represent Windows ACL guarantees.

The existing `password-set` and OAuth commands use Python keyring's
[Windows Credential Locker backend](https://keyring.readthedocs.io/en/latest/).
Run them under the same logged-in Windows account as your AI client. CI imports
the native backend but uses a null keyring for application checks; it does not
prove that a particular desktop login can access its credentials.

`scripts/test_windows_install.py --wheel PATH --constraints REQUIREMENTS`
creates a fresh environment, discovers a synthetic Windows profile, exercises
the native launcher and real stdio MCP search/read/reference/draft workflow, and
checks that the source profile is unchanged. Dependency installation can access
the package index; subsequent application checks block external connections.
No live message is sent.

If automatic profile discovery selects the wrong profile, set `TB_PROFILE` to
the directory containing Thunderbird's `prefs.js`. Thunderbird's Troubleshooting
Information page shows its profile directory. Keep that environment setting when
starting both terminal commands and your AI client; setting it in one shell does
not change other already-running processes. Some clients filter inherited
variables: add nonsecret path settings such as `TB_PROFILE` and `TB_MAIL_INDEX_DB`
to the `thunderbird` server's environment configuration when needed, using your
client's supported format. `TB_ACCOUNT` selects a default account; individual
commands can use `--account`.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Profile not found | Set `TB_PROFILE` to the profile directory and run `tbmail accounts`. |
| Local search has no recent messages | Let Thunderbird download the folders, then run `tbmail reindex`; use `--live` for current server mail. |
| Maildir profile | Reindex to discover `cur` and `new` files. Temporary files are excluded. |
| Credential or keyring error | Run from your logged-in desktop session and set credentials for the selected account and protocol. |
| OAuth account cannot sign in | Use `tbmail auth-guide` and `tbmail auth-status`; check app registration, scopes, expiry and provider/tenant policy. |
| Agent cannot see mail tools | Check setup selected that client, then reconnect/reload it. A project-specific MCP entry can override the global registration. |
| Agent sees the old tools | Reconnect after upgrading; compare with `tbmail tools` under the same tool profile. |
| Reindex times out inside an agent | Run `tbmail reindex --embeddings` in a shell or the indexing service. |
| Private IMAP reference expires | Search live mail again after folder moves or account changes. Use local message references for durable citations. |

If setup succeeds or a problem remains, file an
[installation report](https://github.com/jamessi1989/thunderbirdmcp/issues/new?template=installation-feedback.yml).
Share versions, commands, and sanitized errors. Do not attach mail bodies,
database files, credentials, client configuration files, or private profile paths.
