# Mail workflows and operating limits

This guide describes the roadmap implementation after v0.3.1. The separate
[comparison](comparison.md) is a dated review of that older release, not a claim
that every current competitor is missing these features.

`tbmail tools --json` lists the installed MCP tools and their input schemas.
Every tool is also available from a terminal as
`tbmail call TOOL --args '{"argument":"value"}'`, or `--args-file PATH` / `-`
for JSON from a file or stdin. These calls use the same enforced policy as MCP.
Existing Codex, Claude, OpenCode and Terminalman stdio configuration remains
valid; restart the client to refresh its tool catalogue after upgrading.

## Choose the allowed effects

Set these variables in the MCP server or service environment:

| Variable | Meaning |
| --- | --- |
| `TB_MCP_PROFILE=read-only` | Search, read, references, evidence, events and status. No draft, mailbox, export or delivery effects. |
| `TB_MCP_PROFILE=draft-only` | Read plus local drafting/editing and unsent server-draft handoff. No delivery or ordinary mailbox changes. |
| `TB_MCP_PROFILE=full` | All capabilities; the default for backwards compatibility. |
| `TB_MCP_TOOLS=search_email,get_email,get_email_reference` | Optional comma-separated tool allowlist; an empty value allows no tools. |
| `TB_MCP_ACCOUNTS=account1` | Optional explicit account boundary. Single-account tools resolve that account; mixed-account access fails closed. |
| `TB_MCP_FILE_ROOTS=/absolute/export:/absolute/attachments` | Optional source-attachment and export roots, separated by the platform path separator. |

Profiles remove tools from discovery **and** reject direct/hidden calls and
underlying effects. Services recheck their active policy. Tool annotations alone
are not permissions. An allowlisted composite tool can use its implementation
helpers, but those helpers cannot exceed the profile's capabilities.

## Draft, edit, forward and review

```sh
tbmail identities --account account1
tbmail draft --account account1 --identity identity1 --to recipient@example.test \
  --subject 'Project update' --body-file message.txt --html-file message.html
tbmail drafts DRAFT_ID
tbmail edit-draft DRAFT_ID --revision 1 --changes '{"subject":"Updated project note"}'
tbmail send DRAFT_ID --revision 2 --confirm
```

The body and envelope are frozen in private storage. After an edit, sending or
saving requires the current revision; a stale revision cannot send new content.
Use `inline_attachments:[{"path":"/absolute/logo.png","cid":"logo"}]` with
`html_body` and a plain body to include explicit inline content. Sender identities
must already belong to the selected Thunderbird account; identity-specific SMTP
settings are captured along with From. Reading a preview sends nothing.

`tbmail forward REFERENCE --to recipient@example.test --attachment-id 1`
creates a new conversation. Only selected attachments are included; Bcc and
reply-thread headers are not inferred. `discard-draft` discards the local draft
and reports whether a native server copy remains.

`tbmail save-draft DRAFT_ID --revision N` hands exact unsent MIME to the same
account's configured or discovered IMAP Drafts folder. It preserves Bcc, reply
headers and attachments. The local snapshot then becomes send-locked. After
editing in Thunderbird, obtain a fresh live reference in Drafts and run
`tbmail reconcile-draft DRAFT_ID REFERENCE --revision N`. This imports the native
MIME as a new reviewed revision. Saving a later edit appends the replacement,
then retires only the matching prior UID; it never globally expunges a folder.
The result reports old-copy cleanup failures. Native-linked drafts remain
Thunderbird-controlled for sending. Ambiguous APPEND outcomes are never repeated
automatically; special-use and revision behavior is covered with synthetic IMAP.

## Triage and organize live mail

```sh
tbmail flags LIVE_REFERENCE --unread --star
tbmail flags LIVE_REFERENCE --unread --star --apply
tbmail organize trash LIVE_REFERENCE
tbmail organize trash LIVE_REFERENCE --apply
tbmail organize copy LIVE_REFERENCE --destination Projects --apply
```

Mutations accept up to 100 references in one account and validate endpoint,
folder, UIDVALIDITY and UID. Every item has a preview/completed/failed/uncertain
outcome. IMAP keyword changes check `PERMANENTFLAGS`; Gmail label changes use its
native `X-GM-EXT-1` extension. Reading still uses read-only selection and
`BODY.PEEK`. Archive/trash discover special-use folders, including localized
names; ambiguous discovery requires an explicit destination.

Moves use atomic IMAP MOVE. A server COPYUID receipt produces a new reference
and restore arguments; without a receipt the result explains why safe automatic
undo is unavailable. Permanent `delete` requires `confirm_permanent=true` and
UIDPLUS message-scoped expunge. Folder deletion permits only empty ordinary
folders; INBOX and special-use folders are protected.

## Search continuation and exports

```sh
tbmail call search_page --args '{"query":"invoice","limit":20,"semantic":false}'
tbmail call search_page --args '{"cursor":"CURSOR_FROM_PREVIOUS_RESULT","limit":20}'
tbmail call search_page --args '{"source":"live","scopes":[{"account":"account1","folder":"INBOX"},{"account":"account1","folder":"Projects"}]}'
```

Private snapshots last one hour, retain at most 5,000 results, and remain stable
when new mail arrives. Live fan-out accepts up to ten explicit scopes. Results
report known matches, omitted items, failed scopes, moved/deleted page entries,
supported filters, ignored keyword terms and unmatchable terms. A capped or
deduplicated snapshot is not described as exhaustive. Semantic ranking and IMAP
text matching have different coverage.

`plan_export` takes `items:[{"reference":"...","attachment_id":1}]` and an
absolute `directory`; omit `attachment_id` to export original MIME. It freezes
at most 50 items / 64 MiB privately and returns stable collision-free paths,
byte sizes and SHA256 checksums. Review that result, then call `execute_export`.
Existing files are never overwritten. `resume:true` verifies matching existing
files; `recover_export` verifies a stale interrupted job before releasing its
claim. Keep private export-plan databases secure: they contain captured mail.

## Operations, events and approved schedules

`list_operations`, `get_operation` and `cancel_operation` expose durable job
IDs, progress and partial outcomes. Cancellation stops pending work or asks a
running job to stop between effects; it cannot recall accepted SMTP/API delivery.
Native mailbox changes, exports and outgoing/scheduled delivery use the journal.

`watch_mail` performs one bounded poll or IMAP IDLE wait. Its first observation
baselines existing mail unless `include_existing=true`. `list_mail_events`
returns durable cursors and account/folder/UIDVALIDITY/UID references; folder
recreation is a distinct event. Notifications survive restart without duplicate
UID events. No watcher starts merely because the MCP starts.

```sh
thunderbird-mcp-service watch --account account1 --folder INBOX --idle 30
tbmail call schedule_draft --args '{"draft_id":"DRAFT_ID","revision":1,"due_at":"2026-11-10T09:00:00+01:00","approved":true}'
thunderbird-mcp-service scheduler --interval 30
```

Scheduling requires an explicit approved saved revision and an ISO due time;
IANA timezones are supported for local dates. Set `transport` to `smtp` (default),
`gmail`, or `graph` to select the approved delivery path. Changed revisions fail without
delivery. Claims persist before sending, active/captured policies are checked,
and interrupted or uncertain delivery is not automatically retried. `--once`
supports a supervised one-shot/cron invocation. This project does not install or
enable a background service automatically.

## Gmail API, Graph and interactive enrollment

```sh
tbmail auth-guide gmail --protocol gmail --mode read-only
tbmail auth-login gmail --client-id YOUR_CLIENT_ID --protocol gmail --mode read-only --account account1
tbmail call provider_search --args '{"provider":"gmail","account":"account1","query":"from:sender@example.test"}'
```

Bring your own registered OAuth application. The CLI opens browser consent with
PKCE and state validation through a loopback callback; `--no-browser` prints the
consent URL. `--client-secret` prompts privately when required. Tokens and refresh
credentials stay in the system keyring. Use `auth-status --protocol gmail` or
`graph` for redacted diagnostics. Graph supports `--tenant`; persist the matching
runtime tenant with `TB_GRAPH_TENANT` or the account-specific environment prefix.
Provider application consent, verification and tenant policies are external
requirements; enrollment cannot bypass them.

Native API credentials use separate keyring namespaces from IMAP/SMTP. Gmail
read-only/draft/send permissions apply to the **API adapter**; Gmail IMAP/SMTP
still requires `https://mail.google.com/`. API references work with reading,
threads, citations, attachments and exports. Native provider thread grouping is
reported as provider grouping. Graph requests immutable IDs. Use
`save_draft_to_provider` for an unsent handoff or `send_draft_via_provider` for an
explicitly requested delivery of the reviewed local MIME. Both share the outbox
claim, preventing cross-transport duplicate sends. Gmail/Graph request contracts
and consent are tested with mocks; live provider consent/delivery is not claimed.
The native MIME path caps Gmail at 25 MiB and Graph at 3 MiB; larger Graph uploads
through attachment sessions are outside this adapter. Provider rules may impose
lower limits. The SMTP path retains its existing bounded attachment handling.

## Authenticated HTTP

```sh
thunderbird-mcp-http --auth-file /private/http-auth.json --init-auth --account account1
thunderbird-mcp-http --auth-file /private/http-auth.json
```

The first command prints a random token once and writes only its SHA256 digest
to a mode-0600 file. Store the token in the client's secret configuration and
send `Authorization: Bearer TOKEN` to `http://127.0.0.1:8765/mcp`. The default
profile is read-only. Each configured principal requires explicit account IDs;
file roots default to none. Optional per-principal `tools`, `profile`, and
absolute `file_roots` can narrow access. Rotate tokens in the private file and
restart the server. This is provisioned bearer authentication, not an OAuth
authorization server.

Each HTTP request is independently authenticated and stateless; session IDs are
rejected. Search/export/job state separately retains principal ownership. Hosts
are allowlisted and browser Origins are denied unless explicitly configured in
`allowed_origins`; configure `allowed_hosts` for a reverse proxy. Non-loopback
binding requires TLS certificate/key arguments. MCP bodies are limited to 1 MiB.
No listener is started by installation. Tests exercise real MCP ASGI requests,
account separation, hidden tool denial and file boundaries; no public endpoint
was deployed for validation.

## Evidence and native desktop scope

`authentication_evidence` returns bounded SPF/DKIM/DMARC assertions with header
provenance, conflicts and unknowns. It does not independently verify signatures,
DNS, or sender trust. `follow_up_candidates` uses Message-ID/References/In-Reply-To
links and selected folders, reports exclusions and missing coverage, and sends
no reminders. Same-subject messages are not merged.

## Maildir and optional OCR

Reindexing discovers Thunderbird mbox and Maildir folders beneath `Mail` and
`ImapMail`. Maildir `cur` and `new` files are indexed; `tmp` files are ignored.
Per-file checkpoints avoid reparsing unchanged MIME. Flag filename changes and
new-to-cur moves retain existing message identities; normal refreshes preserve
numeric IDs within the folder. Reads verify source identity again before
returning raw MIME. The adapter never renames, changes flags in, or writes a
mail store. Its additive checkpoint tables are created by explicit reindexing;
the existing index IDs and reference format are retained.

OCR is optional and off by default. Install the `ocr` Python extra (Pillow),
Tesseract with the desired installed language packs, and Poppler's `pdfinfo` /
`pdftoppm` for scanned PDFs. This project downloads no OCR models or languages.
Use `ocr_status` to inspect availability without exposing executable paths.

```sh
TB_OCR=1 TB_OCR_LANGUAGES=eng+spa tbmail reindex --full
tbmail call ocr_status
```

`TB_OCR_MAX_PAGES` defaults to 3 (maximum 5); `TB_OCR_TIMEOUT` defaults to
20 seconds (maximum 30). Inputs are limited to 8 MiB and 12 megapixels; PDF
rendering has a 3,000-pixel edge limit and extracted text is capped at 25,000
characters. Linux subprocesses also receive CPU, memory and file-size limits.
Other operating systems retain byte/page/pixel/time limits but do not claim the
Linux resource controls. Reindex with `--full` after enabling OCR so unchanged
previously indexed files are reconsidered. Missing engines/languages and
scanned documents without extracted text are reported explicitly.

## Native desktop integration

Contacts, calendars/tasks, Thunderbird filters and settings use the separately
pinned [native integration](native-integration.md), with its own environment and
permissions. That companion's tool catalogue is distinct from our mail tools.

Protocol references: [IMAP UIDPLUS](https://www.rfc-editor.org/rfc/rfc4315),
[Gmail IMAP extensions](https://developers.google.com/workspace/gmail/imap/imap-extensions),
[Authentication-Results trust boundaries](https://www.rfc-editor.org/rfc/rfc8601),
[Gmail API scopes](https://developers.google.com/workspace/gmail/api/auth/scopes),
[Graph immutable IDs](https://learn.microsoft.com/en-us/graph/outlook-immutable-id),
[MCP HTTP transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports).
