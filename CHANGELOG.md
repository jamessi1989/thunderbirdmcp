# Changelog

## 0.4.0 — 2026-11-02

This release implements the twenty-item [roadmap](ROADMAP.md), with **56 mail
MCP tools** and the same stdio integrations. See the [workflow guide](docs/workflows.md)
for configuration requirements and bounded behavior. Native desktop features
use a separately configured companion; they are not counted in these 56 tools.

### Added

- Enforced read-only/draft-only/full profiles, tool allowlists, account/file
  boundaries and direct-call protection. The same rules govern services.
- Revision-checked draft editing/discard/reconciliation, forwards with explicit
  recipients/attachments, HTML/plain alternatives, inline parts and configured
  sender/SMTP identities. Existing exact-MIME delivery claims remain shared.
- Bounded live read/star/keyword/Gmail-label changes; copy/move/archive/trash/
  restore and explicitly confirmed scoped permanent deletion; special-use
  discovery and ordinary-folder rename/empty deletion. Previews and per-item
  uncertain outcomes remain visible, including when cancellation is requested.
- Stable search continuation snapshots, omitted-match and filter/term
  diagnostics, bounded live multi-scope search and partial-failure reporting.
- Reviewed batch exports with frozen bytes, collision-free filenames,
  checksums, no overwrite and explicit resume/recovery.
- Durable operation history, cancellation before mail effects, new-mail
  events with resume cursors and optional bounded IMAP IDLE. Explicit foreground
  service commands are installed without enabling background activity.
- Approved revision-bound delivery scheduling through SMTP, Gmail or Graph,
  with timezones, persistent claims and no automatic uncertain-delivery retry.
- BYO browser OAuth enrollment with PKCE, separate Gmail/Graph credentials and
  API adapters for search/read/threads/triage/draft handoff/sending. API scopes
  remain distinct from Gmail's broad IMAP/SMTP scope.
- Authenticated stateless Streamable HTTP, private hash-only bearer config,
  principal-owned persisted state, explicit account/file roots and loopback
  default. Host/Origin and request-size checks apply before dispatch.
- Bounded authentication-header evidence and header-based follow-up candidates;
  neither automatically trusts a sender nor sends reminders.
- Incremental read-only Maildir indexing retaining existing references and IDs.
  Optional Tesseract OCR supports images/scanned PDFs with explicit installed
  languages, no automatic downloads and byte/page/pixel/time limits.
- Native Windows profile/data discovery and launchers, plus a Windows installed
  wheel/stdio CI job. A pinned separate native Thunderbird bridge covers
  contacts, calendars/tasks, filters and settings in an isolated environment.
- `tbmail call` exposes the same typed MCP tools from terminal sessions;
  friendly draft/forward/triage/auth commands and service/HTTP entrypoints are
  included. Documentation now separates capabilities from verification scope.

- Add `save_draft_to_thunderbird` and `tbmail save-draft <draft-id>` to save a local
  draft in the account's Thunderbird IMAP Drafts folder without sending. Bcc,
  captured attachments, and reply threading are preserved. Repeat or interrupted
  saves cannot append automatically again, and the local snapshot cannot be sent
  after it is handed to Thunderbird for editing. Local and cross-account Drafts
  destinations are rejected.

### Verification scope

Regression tests use generated messages, isolated homes and mocked transports.
Actual MCP stdio/HTTP protocol checks cover restrictive profiles, account and
file isolation, draft revisions and cross-session references. Native Windows
CI exercises a wheel installation, command launchers and synthetic mail through
the real stdio protocol. An isolated real Tesseract/Poppler smoke extracted text
from synthetic PNG and image-only PDF documents. The companion's pinned
catalogue/stdio and configuration were checked separately.

Real provider consent, deliveries, mailbox mutations/IDLE and interactive
Thunderbird-addon operations were not performed with the user's mail. Provider
application/tenant consent, installed OCR engines/languages and native addon
installation remain explicit configuration requirements.

## 0.3.1 — 2026-11-02

This release improves installation and makes the mail workflows easier to
evaluate independently. The mail features introduced in 0.3.0 remain available.

### Added

- An MIT license, package metadata, and links to documentation, source, issues,
  and release history. Source archives explicitly include project code,
  documentation, tests, and the dependency lock.
- An offline [synthetic mail demo](docs/demo.md) that runs separate terminal
  processes through attachment search, a reusable reference, message and thread
  reads, attachment text extraction, reply drafting, and an unsent preview.
  Its generated SVG shows selected fields from verified command output.
- A clean-install runner for both a fresh virtual environment and `uv tool
  install`. It exercises packaged commands, configuration setup, user data paths,
  and an actual MCP stdio workflow with generated mail. CI now includes this
  package check using dependencies constrained by the lockfile.
- A dedicated [installation guide](docs/installation.md) covering checkout and
  wheel installation, credentials, data locations, and troubleshooting.
- Contribution guidance and public issue forms for successful installations,
  partial installations, and reproducible bugs. Reports request software
  versions and sanitized steps rather than private mail or credentials.

### Fixed

- Setup works from an installed wheel without requiring a source checkout's
  `.venv`. Existing entrypoint links managed by `uv tool` are preserved, and
  setup remains idempotent.
- Installed packages keep private index and outbox data in the user's data
  directory rather than `site-packages`. Source checkouts retain their existing
  default data location, and explicit database overrides remain available.
- Thunderbird profile discovery includes macOS and accepts literal percent signs
  in profile paths. Missing or malformed profiles produce actionable guidance.
- Package console entrypoints initialize the desktop session environment used
  for keyring access, as the generated launchers already did.

### Verification scope

The demo and installed-package checks use generated mail and isolated homes;
mail application processes block network access and use a disabled keyring.
They do not establish provider authentication or an actual desktop installation.
The v0.3.1 wheel passed both fresh-venv and `uv tool` installation checks on
Linux/Python 3.11, including client setup and an actual MCP search/read/reference,
attachment-search, and unsent-draft workflow with synthetic mail.
The [CI workflow](https://github.com/jamessi1989/thunderbirdmcp/actions/workflows/checks.yml)
runs synthetic tests, the keyword benchmark, builds, and clean-install checks on
Linux and macOS. The release's CI result is separate from the v0.3.0 results
recorded below.

Independent installation reports are invited through the new issue form; no
external testers or their results are implied. A complete macOS desktop
integration, native Windows deployment, and provider browser OAuth consent
remain outside the verified scope. Actual Codex/Claude/OpenCode smoke checks
are opt-in checks with model accounts, not part of synthetic CI.

## 0.3.0 — 2026-11-02

### Added

- Live IMAP search, message and conversation reads, references, and attachment
  access without waiting for Thunderbird's local download. Reads use readonly
  selection and `BODY.PEEK`; references check the account, folder, UIDVALIDITY,
  and UID before returning mail.
- Local text extraction and search for PDF, DOCX, XLSX, PPTX, text, HTML, and XML
  attachments, with explicit unsupported/encrypted/error statuses and resource
  limits. `get_attachment_text` and `tbmail attachment-text` read one attachment
  without exporting it.
- Account selection for search, mail operations, and drafts; scoped password
  storage; and OAuth2 token import, refresh, and XOAUTH2 authentication.
- A synthetic retrieval benchmark and Linux/macOS CI covering the unit suite,
  keyword benchmark, and package build.
- Documentation for reconnecting Claude Code and OpenCode in existing sessions
  and reloading Codex app-server MCP connections while preserving conversations.

### Changed

- Index schema v3 stores attachment extraction metadata and text. Upgrades
  preserve indexed messages and references, rebuild derived search data, and
  backfill attachment text during reindexing.
- Reply drafts infer the original account when possible and reject ambiguous
  account selection. Saved drafts retain the account used when prepared.
- Live references work with the existing read, citation, thread, reply, and
  export tools. They expire if a message moves or its mailbox/account identity
  changes; local references remain the choice for citations across moves.

### Verified

- The 162-test synthetic suite, keyword benchmark, and package builds passed
  the Linux Python 3.10–3.12 and macOS Python 3.12 matrix in
  [GitHub Actions](https://github.com/jamessi1989/thunderbirdmcp/actions/runs/41479433495).
- Maintainer checks on Linux exercised actual Codex, Claude Code, and OpenCode
  with GLM tool calls against synthetic mail, existing-session reconnects,
  readonly live IMAP access with unchanged read flags, and attachment retrieval.
  These checks did not send mail.

### Known boundaries

- macOS CI is synthetic coverage, not verification of a complete Thunderbird
  desktop installation. Windows deployment is unverified.
- OAuth imports tokens from an application you authorize. There is no browser
  consent flow or reuse of Thunderbird's OAuth client registration. Provider
  policies and consent have not been established by mocked protocol tests.
- Live search uses server text filters in one folder; local search supplies
  semantic ranking and extracted attachment text. The small synthetic benchmark
  does not establish superiority over other mail MCPs.
- Messages without Message-ID use a fallback based on indexed headers and
  text. Identical text with different attachments can share a reference;
  numeric IDs do not remove all ambiguity after mailbox compaction.
