# thunderbird-mcp

Search, read, reference, and send your Thunderbird mail from Terminalman,
Claude Code, Codex, OpenCode, or a normal shell. Search uses a private local
SQLite index; sending uses your Thunderbird account's SMTP settings and system
keyring. IMAP handles Sent copies and requested folder operations.

## Start using it

```bash
git clone https://github.com/jamessi1989/thunderbirdmcp.git ~/projects/thunderbirdmcp
cd ~/projects/thunderbirdmcp
uv sync --locked
uv run thunderbird-mcp-index
uv run thunderbird-mcp-setup --apply
```

Setup installs `tbmail`, `thunderbird-mail`, `thunderbird-mcp`, and
`thunderbird-mcp-setup` in `~/.local/bin`. It registers the MCP in the global
configuration of installed Claude Code, Codex, OpenCode, and Antigravity clients.
Use `--clients claude codex` to select clients, or `--clients none` to install
only commands. Run setup without `--apply` to inspect changes. Existing settings
are preserved and changed files receive private timestamped backups. Repeating
setup is safe. MCP launchers use absolute paths and preserve your current directory.

Restart the agent in an existing Terminalman session to load its new MCP tools;
the Terminalman server itself does not need a restart. `tbmail` is immediately
available through the shell, including from an already running agent.

You can then ask:

- “Search my mail for the cloud-provider invoice from May.”
- “Read that conversation and give me a reference I can use in another session.”
- “Prepare a reply to this mail saying the migration is complete.”
- “Send draft `<draft-id>`.”

## Terminal commands

```bash
tbmail status --network                 # Verify TLS/login; sends no mail
tbmail search 'from:alex storage' --limit 5
tbmail search 'folder:INBOX after:2026-11-01' --keyword
tbmail read 12345
tbmail thread 12345 --bodies
tbmail reference 12345                  # Markdown citation with a durable URI
tbmail attachments 12345
tbmail export 12345 ./message.eml
tbmail export-attachment 12345 1 ./attachment.pdf
tbmail reindex                         # Refresh downloaded mail
tbmail --json search 'invoice'          # Structured output for scripts/agents
```

Pass a returned `thunderbird://message/...` reference in place of a numeric ID
to read, thread, reference, reply, attachment, and export commands. Stable
references use Message-ID, with a content hash fallback, and survive reindexing
and folder changes. Integer IDs also stay stable during ordinary refreshes, but
use the URI for references saved in another project or conversation. The search
web app has a **Copy mail reference** button.

For rare messages without Message-ID, the fallback hashes the indexed headers
and text. Messages with identical indexed text but different attachments can
share that fallback reference; use a local numeric ID to select the exact copy.

```bash
tbmail draft --to colleague@example.org --subject 'Migration update' \
  --body-file ./reply.txt --attach ./report.pdf
tbmail reply 'thunderbird://message/...' --body 'Thanks, I will check.'
tbmail drafts
tbmail drafts <draft-id>
tbmail send <draft-id>                  # Show the saved preview
tbmail send <draft-id> --confirm        # Submit exactly that saved draft
```

Drafts support To/Cc/Bcc and captured local attachments. Reply drafts preserve
In-Reply-To and References, honor Reply-To, and exclude your address from reply-all.
Drafts are local to this application; they do not appear in Thunderbird Drafts.
`--body-file -` reads standard input. Use `tbmail --help` for all commands.

## Sending and retry behavior

Each draft stores its exact MIME message, envelope, and account settings in a
private outbox database. Delivery atomically claims that draft. Concurrent or
repeated sends cannot submit it again. SMTP acceptance is recorded before the
Sent copy is saved, so failure to save the copy never causes another delivery.

Results distinguish `prepared`, `sending`, `sent`, `partially_sent`, `failed`,
and `uncertain`. An interrupted or uncertain send needs a delivery check; do not
create another draft automatically. `send_draft(retry_failed=True)` permits a
retry only after a known failure before delivery. Direct `send_email` defaults
to a preview and accepts an `idempotency_key` for programmatic retries. Prefer
the saved-draft workflow. Sending tests use a mocked SMTP server; live diagnostics
authenticate and issue NOOP without sending anything.

## MCP interface

| Workflow | Tools |
| --- | --- |
| Health and search | `mail_status`, `search_email`, `list_folders`, `reindex` |
| Read and cite | `get_email`, `get_thread`, `get_email_reference` |
| Original mail and attachments | `list_attachments`, `export_email`, `export_attachment` |
| Prepare and send | `draft_email`, `draft_reply`, `get_draft`, `list_drafts`, `send_draft`, `send_email` |
| Organize | `imap_list_folders`, `create_folder`, `move_emails`, `undo_move` |

Resources: `thunderbird://status` and `thunderbird://message/{key}`.
Prompts: `find_mail(query)` and `reply_to_mail(reference, instructions)`.
Tools have read/write annotations. Mail and attachment content is treated as
untrusted data. The server uses stdio; protocol stdout contains only MCP messages.

## Search and indexing

Search combines FTS5 keywords, typo correction, multilingual local embeddings,
recency, and optional conversation deduplication. Filters include `from:`, `to:`,
`folder:`, `after:YYYY-MM-DD`, and `before:YYYY-MM-DD`; `before:` is exclusive.
The separate `since`/`until` parameters accept dates, with `until` including that
whole day. Empty search lists recent mail. `semantic=False`/`--keyword` uses only
keywords; search diagnostics explain when semantic search is unavailable.

```bash
uv run thunderbird-mcp-embed            # Incremental semantic vectors
uv run thunderbird-mail-web            # Default: http://127.0.0.1:3458
```

The index reads Thunderbird's **downloaded mbox mail**. Thunderbird must sync
new messages and folders before local search can find them. Refreshing this
index does not fetch mail from IMAP. MIME attachment exports likewise require
the local original. Reads do not change read/unread flags. Bodies are indexed
up to 50,000 characters, with truncation reported. Export original `.eml` files
for complete local content. Exports do not overwrite existing files or write
inside Thunderbird mail storage.

Schema upgrades retain existing messages. Refreshes preserve IDs, skip expunged
messages, and commit each folder atomically; if Thunderbird changes a folder
during parsing, its previous index is kept for the next refresh. Threads use
Message-ID/References/In-Reply-To relationships rather than merging unrelated
messages with the same subject.

Systemd units in `systemd/` refresh the index and embeddings ten minutes after each completed run
and run the existing authenticated web app. The web app loads no external page
assets and renders mail as plain text. It has keyboard navigation (`/`, `j`,
`k`, `o`) and optional cross-encoder reranking. Web credentials use the
`thunderbird-mail-web` keyring service. The supplied service binds to loopback.
To use a private network address, set `MAIL_WEB_HOST` in a local systemd override.
Adjust `WorkingDirectory` if the checkout is outside `~/projects/thunderbirdmcp`.

## Configuration

Thunderbird's selected profile and default account supply hostnames, ports,
identity, TLS mode, and Sent folder. Passwords stay in the system keyring:

```bash
uv run python -m keyring set thunderbird-mcp <imap-username>
```

Environment overrides:

| Variable | Meaning |
| --- | --- |
| `TB_PROFILE`, `TB_ACCOUNT` | Thunderbird profile path and account ID |
| `TB_MAIL_INDEX_DB` | Index path; default `mail-index.sqlite` in this checkout |
| `TB_OUTBOX_DB` | Draft/delivery database; default beside the index |
| `TB_FROM` | Sending identity, e.g. `Name <you@example.org>` |
| `TB_IMAP_HOST`, `TB_IMAP_PORT`, `TB_IMAP_USER` | IMAP connection |
| `TB_SMTP_HOST`, `TB_SMTP_PORT`, `TB_SMTP_USER` | SMTP connection |
| `TB_IMAP_TLS`, `TB_SMTP_TLS` | `ssl` or `starttls`, with certificate verification |
| `TB_IMAP_KEYRING_SERVICE`, `TB_SMTP_KEYRING_SERVICE` | Keyring service names; default `thunderbird-mcp` |
| `TB_IMAP_PASSWORD`, `TB_SMTP_PASSWORD` | Optional environment credentials; prefer keyring |
| `TB_SENT_FOLDER`, `TB_IMAP_ACCOUNT_DIR` | Sent folder and local account-directory mapping |
| `TB_NETWORK_TIMEOUT` | Connection timeout in seconds; default 15 |
| `TB_EMBED_THREADS` | Local model CPU threads per process; default 2 |
| `MAIL_WEB_HOST`, `MAIL_WEB_PORT` | Web bind address and port |

Sending supports international display names and ASCII mailbox addresses.
Outgoing attachments are capped at 25 MiB total; local MIME export at 100 MiB.
IMAP operations target the configured account even when the index contains
messages from several profiles/accounts. Never put credentials, exported mail,
or index/outbox databases in source control.

## Development and verification

```bash
uv sync --locked
uv run python -m unittest discover -s tests -v
uv run python -m compileall -q thunderbird_mcp tests
```

The tests use synthetic mailboxes and mocked mail transports. They exercise
schema upgrades, reference stability, reply headers, search behavior, attachment
exports, exact draft delivery, duplicate/ambiguous-send prevention, client-config
preservation, and a real MCP stdio client/server handshake. No tests send mail.

The dependency lock is checked in and the MCP SDK is constrained to its supported
v1 API. Keep `backups/`, `exports/`, private retrieval evaluation data, logs,
and private SQLite files out of Git. Published tests contain synthetic mail only.
