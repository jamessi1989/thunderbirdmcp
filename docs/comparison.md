# Mail MCP comparison

**Historical baseline:** this page compares v0.3.1. The subsequent 0.4.0 work
implements the broader workflows in the [implementation record](implementation-status.md)
and [workflow guide](workflows.md). The older “missing” cells below describe the
reviewed release, not the current tool catalogue. Competitor snapshots have not
been rerun or refreshed for this implementation update.

Reviewed **2 November 2026**. This compares our published
[v0.3.1 release](https://github.com/jamessi1989/thunderbirdmcp/releases/tag/v0.3.1)
with five public projects. Our baseline has **23 tools**. An unreleased
`save_draft_to_thunderbird` change was present locally during this review; it is
treated as work in progress, not as part of that release or as two-way draft sync.
This research did not validate or publish that working change.

Our clearest strengths are local semantic retrieval, searchable PDF/Office
attachment text, reusable message references, and persistent delivery state.
The other projects provide useful examples of broader mailbox triage, editable
server drafts, account enrollment, and automation. These are source and issue
comparisons; we did not run competing servers or establish a retrieval-quality
ranking. See the [missing-feature roadmap](../ROADMAP.md) for proposed priorities.

## Projects and snapshots

| Project | Source snapshot | Approach and relevant scope |
| --- | --- | --- |
| [This project](https://github.com/jamessi1989/thunderbirdmcp) | [v0.3.1 / `99cabc2`](https://github.com/jamessi1989/thunderbirdmcp/tree/99cabc2fb2c0dc3037de38a6f67f4d635e054946) | Independent downloaded-mbox index plus direct IMAP/SMTP. Local semantic/document search, references, account selection, local drafts, sending, and move/undo. [Tool interface][ours-tools] |
| [U-C4N/Thunderbird-MCP](https://github.com/U-C4N/Thunderbird-MCP) | [`cddefb8`](https://github.com/U-C4N/Thunderbird-MCP/commit/cddefb8010fa39ac85bf4787c3449dca45de2770), not archived | Thunderbird add-on and broker; native mail, saved drafts, contacts, calendars, filters and settings. Current README lists 124 tools; the About description still says 112. [Architecture][U-architecture], [catalogue][U-readme] |
| [marlinjai/email-mcp](https://github.com/marlinjai/email-mcp) | [`3261b81`](https://github.com/marlinjai/email-mcp/commit/3261b81221b395801cff30fa7a18484f80fd7b1b), not archived | Gmail API, Microsoft Graph and IMAP backends, multiple accounts, browser enrollment, server drafts and composition. Its catalogue advertises 36 tools. [README][M-readme], [sending tools][M-sending] |
| [codefuturist/email-mcp](https://github.com/codefuturist/email-mcp) | [`7d31cd5`](https://github.com/codefuturist/email-mcp/commit/7d31cd5eac8788bd69237c18e3ee1e1fe2816ab7), not archived | IMAP/SMTP, SQLite caching, mailbox operations, scheduling, watcher notifications, and local or HTTP transport. [Registration][C-tools], [cache][C-cache], [HTTP server][C-http] |
| [honest-magic/mail-mcp](https://github.com/honest-magic/mail-mcp) | [`3621ef9`](https://github.com/honest-magic/mail-mcp/commit/3621ef92e96a1387dbff797c28d630259991b5a4), not archived | IMAP/SMTP with tool restrictions, optional write confirmation, server drafts and attachment extraction. [Server][H-server], [mail implementation][H-mail] |
| [GongRzhe/Gmail-MCP-Server](https://github.com/GongRzhe/Gmail-MCP-Server) | [`a890d19`](https://github.com/GongRzhe/Gmail-MCP-Server/commit/a890d19189bbc1325b8728fab830fc278cfd8804), **archived 3 March 2026** | Gmail API mail, drafts, attachments, labels and filters. Useful historical demand evidence; one credential configuration per process. [Handlers][G-tools] |

Counts describe catalogue breadth, not reliability, installation success, or
retrieval accuracy. Current source takes precedence over a stale repository
description. Competitor tests and README verification claims are maintainer
reports, not checks reproduced in this review.

## Capability comparison

“Partial” means we have some of the workflow, not that the whole capability is
missing. The last column gives verified source examples; omission of a project
does not establish that it lacks the feature.

| Capability | Our released v0.3.1 | Examples in other projects |
| --- | --- | --- |
| Local/offline retrieval | Downloaded-mbox FTS and multilingual embeddings; PDF/Office text is indexed. [Search][ours-search] | U-C4N uses Thunderbird/Gloda search and excludes PDF/OCR content search from its attachment workflow. Codefuturist has a default-on SQLite cache, offline reads and body/attachment-filename FTS. Offline access is not unique to us. [Native workflow][U-workflows], [cache behavior][C-cache-docs], [FTS schema][C-cache] |
| Multiple accounts | Account selection and account-bound references/drafts; live search handles one account/folder per call. [Interface][ours-tools] | Marlin, codefuturist and honest-magic expose accounts. Gong uses one credential configuration per process. [Marlin][M-readme], [codefuturist][C-tools], [honest-magic][H-server], [Gong][G-tools] |
| Read-only mode and selected tools | Missing server-wide policy; annotations and sending instructions are not an enforced allowlist. [Registration][ours-tools] | U-C4N and honest-magic restrict tools. Codefuturist gates mailbox-write registration, with a background-service caveat in issue #79 below. [U-C4N server][U-server], [honest-magic][H-server], [codefuturist startup][C-app] |
| Draft lifecycle | Persistent local create/read/list/send with exact previews; no edit/discard or server-draft workflow in the release. | U-C4N has native draft updates with revision checks; marlin exposes server draft updates; codefuturist and honest-magic publish IMAP drafts. These are different levels of integration, not proof of universal conflict-safe synchronization. [Native drafts][U-workflows], [marlin][M-sending], [codefuturist][C-drafts], [honest-magic][H-mail] |
| Compose and forward | Replies/reply-all, To/Cc/Bcc and attachments; plain-text composition and no dedicated forward tool or per-draft identity picker. | Marlin supports forwarding and rich composition. Gong constructs HTML/multipart messages. [Marlin sending][M-sending], [Gong MIME][G-mime] |
| Triage and organization | Folder list/create and indexed-message bulk moves with undo. Missing flag setters, copy, folder rename/delete, and dedicated trash/delete tools. | U-C4N has native triage tools; codefuturist and honest-magic expose flags and batch mutations. [Native catalogue][U-readme], [bulk tools][C-bulk], [honest-magic][H-server] |
| Account enrollment | Scoped keyring credentials and OAuth import/refresh; no browser consent flow. | Marlin and Gong provide browser flows. Marlin's shared Google application still has an open verification/onboarding issue. [Marlin OAuth][M-oauth], [Gong authentication][G-tools], [marlin #1](https://github.com/marlinjai/email-mcp/issues/1) |
| Search continuation and explanation | Bounded results, filters and semantic diagnostics; no continuation cursor. | U-C4N documents snapshot pagination, coverage explanations and follow-up candidates. [Search workflow][U-workflows] |
| Attachment export | Individual no-overwrite exports plus bounded text extraction. | Codefuturist supports a file-only download response; U-C4N implements batch export plans with per-file outcomes. File export is already covered by us; reviewed multi-message export is a gap. [Download tool][C-attachments], [batch exports][U-attachments] |
| Operation history | Partial: saved delivery states and move undo logs. | U-C4N exposes a broader operation journal, cancellation and conflict-aware undo. [Operation workflow][U-workflows] |
| Live notifications | Periodic local indexing; no IMAP IDLE/event delivery interface. | Codefuturist provides watcher tools and notifications. [Watcher][C-watcher], [notification payload][C-notifier] |
| Scheduled delivery | Missing. | Codefuturist starts a persistent scheduler; its read-only interaction needs care. [Startup][C-app], [#79](https://github.com/codefuturist/email-mcp/issues/79) |
| Remote MCP | stdio. Our ordinary mail web UI is separate from MCP transport. | Codefuturist has Streamable HTTP and bearer-token controls. Marlin still uses stdio despite an open HTTP request. [HTTP implementation][C-http], [marlin transport][M-server], [marlin #43](https://github.com/marlinjai/email-mcp/issues/43) |
| Contacts/calendar/Thunderbird settings | Missing; broader than mail-only workflows. | U-C4N has native toolsets in these areas. A calendar bug is still open; tool presence does not establish every path works. [Catalogue][U-readme], [#19](https://github.com/U-C4N/Thunderbird-MCP/issues/19) |

## Issue evidence

We enumerated **90 non-PR issue entries** across the five repositories, including
closed and unlabeled issues. We then read relevant bodies/comments and checked
current implementation paths. This is a bounded review, not a census of all mail
MCPs or a measurement of demand among our own users.

| Tracker | Non-PR issue inventory at review | Reading scope |
| --- | --- | --- |
| [U-C4N](https://github.com/U-C4N/Thunderbird-MCP/issues?q=is%3Aissue) | 3: 2 open, 1 closed; all unlabeled | All three threads and linked fixes. All were bugs; no standalone feature requests were found. |
| [Gong](https://github.com/GongRzhe/Gmail-MCP-Server/issues?q=is%3Aissue) | 48: 38 open, 10 closed; 45 unlabeled | Eleven selected bodies and their 19 comments, plus source inspection. |
| [marlin](https://github.com/marlinjai/email-mcp/issues?q=is%3Aissue) | 6: 3 open, 3 closed; all unlabeled | All six bodies and 13 comments. Vendor outreach in #25 was excluded from demand ranking. |
| [codefuturist](https://github.com/codefuturist/email-mcp/issues?q=is%3Aissue) | 33: 30 open, 3 closed; 23 unlabeled | Selected feature/reliability threads and source paths; 73 PR entries excluded from the issue count. |
| [honest-magic](https://github.com/honest-magic/mail-mcp/issues?q=is%3Aissue) | 0 issues, 0 PRs | Capability evidence only; no issue-backed demand can be inferred. |

Issue state is a snapshot. Open entries below had no `state_reason`; closed
entries show GitHub's reason where relevant. A request, a bug report, a proposed
PR and a shipped implementation are different evidence.

| Evidence | State | Finding and implication for our roadmap |
| --- | --- | --- |
| [Gong #80: enabled-tools option](https://github.com/GongRzhe/Gmail-MCP-Server/issues/80) | Open | Requests a smaller tool surface to reduce context. No selector was found in HEAD. Supports tool profiles; server-enforced read-only behavior is our additional design proposal. |
| [codefuturist #79: read-only still starts scheduled sending](https://github.com/codefuturist/email-mcp/issues/79) | Open | A source-analysis report, not a reproduced delivery. Startup still lacks the read-only check around background services. Future automation must obey the same policy as tools. [Startup][C-app] |
| [marlin #1: Google application verification](https://github.com/marlinjai/email-mcp/issues/1) | Open | Shared-app consent can reject accounts outside the test-user list. A wizard needs BYO-app setup and actionable errors; bundled client credentials alone do not solve this. |
| [marlin #10: narrower Gmail scopes and BYO credentials](https://github.com/marlinjai/email-mcp/issues/10) | Closed / completed | Verified implemented in its configuration and scope selection. Its restricted mode includes `gmail.modify`, so it is not read-only. [Configuration][M-oauth], [scope selection][M-scopes] |
| [marlin #3: hardcoded Drafts folder](https://github.com/marlinjai/email-mcp/issues/3) | Closed / completed | The adapter now resolves Drafts before APPEND; tests cover namespaced/localized folders. This is a regression requirement for our draft integration, not a current competitor defect. [Adapter][M-imap] |
| [codefuturist #96: Unicode draft subjects](https://github.com/codefuturist/email-mcp/issues/96) | Open | Current source encodes the subject with `libmime.encodeWords`. Add fidelity tests to our draft workflow; do not label this specific encoding defect unresolved. [Header construction][C-imap] |
| [Gong #66: replies lose threading](https://github.com/GongRzhe/Gmail-MCP-Server/issues/66) | Open | The title overstates the diagnosis: MIME generation accepts reply headers, but read results omit RFC Message-ID. Our reply headers are already covered; the later send-as identity request remains useful. [MIME][G-mime], [read handler][G-tools] |
| [codefuturist #17: notification identity fields](https://github.com/codefuturist/email-mcp/issues/17) | Open | Partly addressed: payload has UID, folder and attachment presence but lacks Message-ID. Our future event design should carry resolvable account-bound references. [Payload][C-notifier] |
| [codefuturist #69: sender authentication signals](https://github.com/codefuturist/email-mcp/issues/69) | Open | Requests compact SPF/DKIM/DMARC evidence. No dedicated tool was found in current registration. A proposed summary must preserve provenance and uncertainty. [Registration][C-tools] |
| [marlin #43: remote HTTP](https://github.com/marlinjai/email-mcp/issues/43) | Open | Current server remains stdio. Remote MCP is a separate deployment feature requiring its own account/session and filesystem boundaries. [Transport][M-server] |
| [U-C4N #8: Windows daemon console](https://github.com/U-C4N/Thunderbird-MCP/issues/8) | Open | Its proposed fix remains unmerged. This supports native Windows startup testing; the specific daemon-flag bug does not apply to our architecture. [PR #9](https://github.com/U-C4N/Thunderbird-MCP/pull/9) |
| [U-C4N #19: calendar argument validation](https://github.com/U-C4N/Thunderbird-MCP/issues/19) | Open | The missing argument remains at HEAD and the proposed fix is unmerged. Calendar expansion needs actual tool-path tests; this is a bug, not evidence of a new calendar feature request. [PR #20](https://github.com/U-C4N/Thunderbird-MCP/pull/20) |
| [U-C4N #3: search failures](https://github.com/U-C4N/Thunderbird-MCP/issues/3) | Closed / completed | Addressed through merged PR #7 and v1.3 changes. Earlier PR #4 was closed unmerged. Preserve our filtering/protocol tests and add continuation/coverage semantics; do not repeat the old failure as current. [Merged fix](https://github.com/U-C4N/Thunderbird-MCP/pull/7) |

## Needs already covered, and misleading issue states

- **Account selection:** Gong [#19](https://github.com/GongRzhe/Gmail-MCP-Server/issues/19)
  remains open; our account routing and frozen outgoing identity already cover
  much of that need. Unified live fan-out is a separate extension.
- **Retry protection:** codefuturist [#62](https://github.com/codefuturist/email-mcp/issues/62)
  reports ambiguous retries. Its current send interface lacks a caller
  idempotency key. We already persist draft claims, accepted/uncertain outcomes,
  and optional direct-send keys. SMTP Message-ID alone is not deduplication.
  [Their send path][C-smtp], [our delivery implementation][ours-outgoing].
- **Saving attachments to files:** codefuturist [#12](https://github.com/codefuturist/email-mcp/issues/12)
  is open, but a constrained file-only response exists. Gong
  [#32](https://github.com/GongRzhe/Gmail-MCP-Server/issues/32) is also open despite
  an implemented download tool. Our individual export workflow already exists.
  [Codefuturist tool][C-attachments], [Gong handler][G-tools].
- **HTML composition:** Gong [#24](https://github.com/GongRzhe/Gmail-MCP-Server/issues/24)
  is closed and implemented. It is a real capability gap for our plain-text
  composer. [MIME construction][G-mime].
- **Refresh persistence:** Gong [#50](https://github.com/GongRzhe/Gmail-MCP-Server/issues/50)
  is closed/completed, but the inspected authentication code does not verify the
  requested refresh-persistence fix. Our keyring refresh flow already persists
  tokens. [Their authentication][G-tools], [our OAuth implementation][ours-oauth].
- **Duplicate closures:** codefuturist [#103](https://github.com/codefuturist/email-mcp/issues/103)
  and [#104](https://github.com/codefuturist/email-mcp/issues/104) were closed as
  duplicates of open [#102](https://github.com/codefuturist/email-mcp/issues/102).
  The case-sensitive subject-prefix checks remain in source; those closures are
  not fixes. Our reply prefix check is already case-insensitive. [Their SMTP code][C-smtp].

## What the evidence supports

Prioritize complete drafting, ordinary triage, forwarding, guided sign-in and
enforced tool profiles before expanding into a full Thunderbird desktop API.
This is our prioritization judgment, not a vote count or a claim that every
competitor issue affects our code. Keep the retrieval/reference and delivery
features we already have while filling the specific workflow gaps.

Granular Gmail permissions require an architectural distinction: ordinary
Gmail IMAP/SMTP OAuth uses `https://mail.google.com/`. Gmail API scopes are not
interchangeable with that transport; a consent-wizard toggle alone cannot
provide API-only read/send permissions. [Google XOAUTH2 scopes](https://developers.google.com/workspace/gmail/imap/xoauth2-protocol#oauth_20_scopes),
[Gmail API scopes](https://developers.google.com/workspace/gmail/api/auth/scopes).

[ours-tools]: https://github.com/jamessi1989/thunderbirdmcp/blob/v0.3.1/thunderbird_mcp/server.py
[ours-search]: https://github.com/jamessi1989/thunderbirdmcp/blob/v0.3.1/thunderbird_mcp/search.py
[ours-outgoing]: https://github.com/jamessi1989/thunderbirdmcp/blob/v0.3.1/thunderbird_mcp/outgoing.py
[ours-oauth]: https://github.com/jamessi1989/thunderbirdmcp/blob/v0.3.1/thunderbird_mcp/oauth.py
[U-readme]: https://github.com/U-C4N/Thunderbird-MCP/blob/cddefb8010fa39ac85bf4787c3449dca45de2770/README.md
[U-architecture]: https://github.com/U-C4N/Thunderbird-MCP/blob/cddefb8010fa39ac85bf4787c3449dca45de2770/docs/ARCHITECTURE.md
[U-workflows]: https://github.com/U-C4N/Thunderbird-MCP/blob/cddefb8010fa39ac85bf4787c3449dca45de2770/docs/WORKFLOWS-1.4.md
[U-server]: https://github.com/U-C4N/Thunderbird-MCP/blob/cddefb8010fa39ac85bf4787c3449dca45de2770/src/tbmcp/server.py
[U-attachments]: https://github.com/U-C4N/Thunderbird-MCP/blob/cddefb8010fa39ac85bf4787c3449dca45de2770/src/tbmcp/tools/attachments.py
[M-readme]: https://github.com/marlinjai/email-mcp/blob/3261b81221b395801cff30fa7a18484f80fd7b1b/README.md
[M-sending]: https://github.com/marlinjai/email-mcp/blob/3261b81221b395801cff30fa7a18484f80fd7b1b/src/tools/sending.ts
[M-server]: https://github.com/marlinjai/email-mcp/blob/3261b81221b395801cff30fa7a18484f80fd7b1b/src/server.ts
[M-oauth]: https://github.com/marlinjai/email-mcp/blob/3261b81221b395801cff30fa7a18484f80fd7b1b/src/oauth-config.ts
[M-scopes]: https://github.com/marlinjai/email-mcp/blob/3261b81221b395801cff30fa7a18484f80fd7b1b/src/providers/gmail/auth.ts
[M-imap]: https://github.com/marlinjai/email-mcp/blob/3261b81221b395801cff30fa7a18484f80fd7b1b/src/providers/imap/adapter.ts
[C-tools]: https://github.com/codefuturist/email-mcp/blob/7d31cd5eac8788bd69237c18e3ee1e1fe2816ab7/src/tools/register.ts
[C-cache]: https://github.com/codefuturist/email-mcp/blob/7d31cd5eac8788bd69237c18e3ee1e1fe2816ab7/src/cache/schema.ts
[C-cache-docs]: https://github.com/codefuturist/email-mcp/blob/7d31cd5eac8788bd69237c18e3ee1e1fe2816ab7/README.md#L774
[C-http]: https://github.com/codefuturist/email-mcp/blob/7d31cd5eac8788bd69237c18e3ee1e1fe2816ab7/src/cli/http.ts
[C-app]: https://github.com/codefuturist/email-mcp/blob/7d31cd5eac8788bd69237c18e3ee1e1fe2816ab7/src/app.ts
[C-bulk]: https://github.com/codefuturist/email-mcp/blob/7d31cd5eac8788bd69237c18e3ee1e1fe2816ab7/src/tools/bulk.tool.ts
[C-drafts]: https://github.com/codefuturist/email-mcp/blob/7d31cd5eac8788bd69237c18e3ee1e1fe2816ab7/src/tools/drafts.tool.ts
[C-watcher]: https://github.com/codefuturist/email-mcp/blob/7d31cd5eac8788bd69237c18e3ee1e1fe2816ab7/src/tools/watcher.tool.ts
[C-notifier]: https://github.com/codefuturist/email-mcp/blob/7d31cd5eac8788bd69237c18e3ee1e1fe2816ab7/src/services/notifier.service.ts
[C-attachments]: https://github.com/codefuturist/email-mcp/blob/7d31cd5eac8788bd69237c18e3ee1e1fe2816ab7/src/tools/attachments.tool.ts
[C-imap]: https://github.com/codefuturist/email-mcp/blob/7d31cd5eac8788bd69237c18e3ee1e1fe2816ab7/src/services/imap.service.ts
[C-smtp]: https://github.com/codefuturist/email-mcp/blob/7d31cd5eac8788bd69237c18e3ee1e1fe2816ab7/src/services/smtp.service.ts
[H-server]: https://github.com/honest-magic/mail-mcp/blob/3621ef92e96a1387dbff797c28d630259991b5a4/src/index.ts
[H-mail]: https://github.com/honest-magic/mail-mcp/blob/3621ef92e96a1387dbff797c28d630259991b5a4/src/services/mail.ts
[G-tools]: https://github.com/GongRzhe/Gmail-MCP-Server/blob/a890d19189bbc1325b8728fab830fc278cfd8804/src/index.ts
[G-mime]: https://github.com/GongRzhe/Gmail-MCP-Server/blob/a890d19189bbc1325b8728fab830fc278cfd8804/src/utl.ts
