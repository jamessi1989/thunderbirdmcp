# Feature roadmap and implementation status

The R01–R20 scope below was defined against v0.3.1. Implementation is tracked in
the [current implementation record](docs/implementation-status.md), with usable
commands and operating limits in the [workflow guide](docs/workflows.md).
The tables retain the original requirements and issue evidence; their “missing”
descriptions are the historical starting point, not a current inventory.

Reviewed **2 November 2026**, against published **v0.3.1**. The
[comparison and issue evidence](docs/comparison.md) covers five competing MCPs
and their issue trackers. These priorities are our judgment about completing
ordinary mail workflows and making installation dependable; they are not release
commitments or measured demand from our own users.

**Existing work:** an unreleased `save_draft_to_thunderbird` implementation was
already in progress at review time. It hands an unsent local draft to the same
account's IMAP Drafts folder and prevents sending its stale local snapshot.
Review and release that work before starting another handoff implementation.
It does not supply two-way draft editing or synchronization.

## P0: Complete everyday mail workflows

| ID | Missing or partial capability | Evidence and completion criteria |
| --- | --- | --- |
| R01 | **Enforced read-only and tool profiles.** The release registers all tools; annotations are advisory. | [Gong #80](https://github.com/GongRzhe/Gmail-MCP-Server/issues/80) requests selectable tools; [codefuturist #79](https://github.com/codefuturist/email-mcp/issues/79) shows why background actions also matter. Offer read-only, draft-only and full profiles plus an allowlist; enforce the policy at dispatch/operation boundaries and test omitted-tool calls. Future scheduled work must obey it too. |
| R02 | **Complete draft lifecycle.** Local create/read/list/send exists; Thunderbird handoff is **in progress**. Edit/discard, revision checks and reconciliation remain gaps. | Native editable drafts are [capability evidence](https://github.com/U-C4N/Thunderbird-MCP/blob/cddefb8010fa39ac85bf4787c3449dca45de2770/docs/WORKFLOWS-1.4.md#saved-drafts). [Marlin #3](https://github.com/marlinjai/email-mcp/issues/3) and [codefuturist #96](https://github.com/codefuturist/email-mcp/issues/96) motivate special-use/localized folder and Unicode tests. Preserve Bcc, attachments, reply headers and exact previews; require a fresh revision after external edits. |
| R03 | **Read/unread, flags, tags and keywords.** Searching unread mail exists; changing its state does not. | Capability parity with [bulk triage tools](https://github.com/codefuturist/email-mcp/blob/7d31cd5eac8788bd69237c18e3ee1e1fe2816ab7/src/tools/bulk.tool.ts). Add explicit mutations with bounded batches and per-message outcomes. Reading must continue to leave flags unchanged. Detect supported IMAP keywords; Gmail labels need provider-aware handling. |
| R04 | **Organize live results and finish mailbox operations.** Current move/undo needs indexed IDs; archive/trash are manual folder moves. Copy and folder rename/delete are missing. | Broader operations exist in the [Thunderbird catalogue](https://github.com/U-C4N/Thunderbird-MCP/blob/cddefb8010fa39ac85bf4787c3449dca45de2770/README.md#toolsets). Accept account-bound live references with UIDVALIDITY checks; discover special folders; preview batches and preserve undo where possible. Keep trash/restore distinct from explicitly requested permanent deletion. |
| R05 | **Prepare forwards.** No dedicated forwarding tool exists. | [Marlin sending tools](https://github.com/marlinjai/email-mcp/blob/3261b81221b395801cff30fa7a18484f80fd7b1b/src/tools/sending.ts) provide capability evidence. Add `draft_forward` with explicit recipients and attachment selection, then use the saved-draft send path. Do not infer Bcc or treat forwarding as replying in the original thread. |
| R06 | **Guided account enrollment and reconnection.** Password/token import, account selection and refresh already exist. | [Marlin #1](https://github.com/marlinjai/email-mcp/issues/1) demonstrates consent restrictions; [#10](https://github.com/marlinjai/email-mcp/issues/10) requests BYO OAuth setup. Add browser consent with PKCE where supported, BYO-client configuration and useful redacted diagnostics. Shared-app verification is an external dependency, not something a wizard bypasses. |

## P1: Broader workflows and dependable operation

| ID | Missing or partial capability | Evidence and completion criteria |
| --- | --- | --- |
| R07 | **Rich composition and sender identity selection.** Our composer is plain text; account/From configuration is not a per-draft identity picker. | [Gong #24](https://github.com/GongRzhe/Gmail-MCP-Server/issues/24) is implemented HTML support; a comment on [#66](https://github.com/GongRzhe/Gmail-MCP-Server/issues/66) requests send-as identities. Add HTML with a plain-text alternative, explicit inline attachments and selection among configured identities. The reviewed MIME and sender must remain the ones sent. |
| R08 | **Search pagination and coverage.** Result limits and semantic diagnostics exist, but no continuation cursor or unified live multi-folder/account query. | [U-C4N search workflows](https://github.com/U-C4N/Thunderbird-MCP/blob/cddefb8010fa39ac85bf4787c3449dca45de2770/docs/WORKFLOWS-1.4.md#search-and-follow-up-candidates) provide capability evidence; [#3](https://github.com/U-C4N/Thunderbird-MCP/issues/3) is a fixed historical regression. Add stable continuation, omitted-match counts, supported-filter and ignored/unmatchable query-term explanations, bounded fan-out and partial-failure reporting. |
| R09 | **Native Windows installation and real desktop/provider verification.** Linux/macOS synthetic CI is already present. | [U-C4N #8](https://github.com/U-C4N/Thunderbird-MCP/issues/8) illustrates a Windows startup failure, not a defect in our code. Add native launchers, profile discovery and credential integration, then independent desktop/provider checks. This is partly a validation gap, not a missing MCP tool. |
| R10 | **Batch attachment/message export.** Individual no-overwrite exports already work. | [Native attachment tools](https://github.com/U-C4N/Thunderbird-MCP/blob/cddefb8010fa39ac85bf4787c3449dca45de2770/src/tbmcp/tools/attachments.py) provide capability evidence. Produce a reviewable file plan with stable names, collision handling, checksums, per-file outcomes and resume support; avoid returning bulk base64 through the model. |
| R11 | **Unified operation history and cancellation.** Delivery state and move undo logs are existing foundations. | [Operation-history workflow](https://github.com/U-C4N/Thunderbird-MCP/blob/cddefb8010fa39ac85bf4787c3449dca45de2770/docs/WORKFLOWS-1.4.md#operation-history) provides capability evidence. Expose operation IDs, status and partial outcomes across longer jobs. Cancel pending work without implying that accepted SMTP delivery can be undone. |
| R12 | **Durable new-mail events.** Periodic indexing does not provide an IMAP IDLE/notification API. | [Codefuturist #17](https://github.com/codefuturist/email-mcp/issues/17) requests usable message identity in events and is partly addressed. Add account/folder/UIDVALIDITY/UID plus a resolvable reference, deduplication and a resume cursor. These identity/resume details are our design proposal, not all demands made in the issue. |
| R13 | **Scheduled sending.** No autonomous delivery scheduler exists. | Capability parity with [codefuturist startup](https://github.com/codefuturist/email-mcp/blob/7d31cd5eac8788bd69237c18e3ee1e1fe2816ab7/src/app.ts). Require an explicitly approved saved draft and due time, persisted jobs, timezone handling and cancellation. Reuse the delivery claim/idempotency path and respect R01; never automatically retry uncertain delivery. |
| R14 | **Authentication-evidence summary.** No compact sender-authentication tool exists. | [Codefuturist #69](https://github.com/codefuturist/email-mcp/issues/69) requests SPF/DKIM/DMARC signals. Return bounded header evidence, provenance and unknown/conflicting states. Authentication evidence alone must not be presented as proof that a message is trustworthy. |
| R15 | **Follow-up candidates.** Threads are available, but there is no explicit unanswered-mail workflow. | [U-C4N follow-up workflow](https://github.com/U-C4N/Thunderbird-MCP/blob/cddefb8010fa39ac85bf4787c3449dca45de2770/docs/WORKFLOWS-1.4.md#search-and-follow-up-candidates) provides capability evidence. Use header relationships and selected folders; return reasons, exclusions and uncertainty. Avoid merging unrelated same-subject mail or silently sending reminders. |

## P2: Optional architecture and desktop expansion

| ID | Proposed extension | Evidence and boundary |
| --- | --- | --- |
| R16 | **Native Gmail API/Microsoft Graph adapters and granular permissions.** | [Marlin #10](https://github.com/marlinjai/email-mcp/issues/10) is a fulfilled request for narrower Gmail permissions/BYO credentials. Ordinary Gmail IMAP/SMTP OAuth still needs the mail scope; API-only scopes require an API path. Keep account/reference semantics coherent across backends. [Google transport scopes](https://developers.google.com/workspace/gmail/imap/xoauth2-protocol#oauth_20_scopes) |
| R17 | **Authenticated remote MCP transport.** | [Marlin #43](https://github.com/marlinjai/email-mcp/issues/43) is an open request; [codefuturist implements HTTP](https://github.com/codefuturist/email-mcp/blob/7d31cd5eac8788bd69237c18e3ee1e1fe2816ab7/src/cli/http.ts). Define authentication, session/account isolation and file-export boundaries. The existing mail web UI does not supply this transport. |
| R18 | **Local Maildir indexing.** | An architectural gap in our downloaded-mbox index, not an issue-backed request in this sample. Live IMAP already offers access without that index. Add a storage adapter with incremental updates and stable references before advertising local Maildir support. |
| R19 | **OCR for scanned documents/images.** | Our extraction currently needs text-bearing documents. No direct OCR request was found in the reviewed issue evidence, and the inspected competitors do not establish OCR parity. Treat it as an optional retrieval extension with explicit language/model/download and resource limits. |
| R20 | **Contacts, calendars/tasks, message filters and Thunderbird settings.** | These are [native Thunderbird toolsets](https://github.com/U-C4N/Thunderbird-MCP/blob/cddefb8010fa39ac85bf4787c3449dca45de2770/README.md#toolsets), not IMAP/SMTP mail primitives. Decide whether to add a Thunderbird extension bridge or integrate a separate MCP. [Calendar issue #19](https://github.com/U-C4N/Thunderbird-MCP/issues/19) motivates validation; it is not a new feature request. |

## Existing capabilities to retain

Do not put already-covered needs back on the missing-feature list:

- Account selection, account isolation and frozen outgoing identities address
  much of [Gong #19](https://github.com/GongRzhe/Gmail-MCP-Server/issues/19).
- Saved drafts, duplicate-send protection and uncertain-delivery handling address
  the retry problem in [codefuturist #62](https://github.com/codefuturist/email-mcp/issues/62).
- Attachment-to-file exports address the main need in
  [codefuturist #12](https://github.com/codefuturist/email-mcp/issues/12).
- OAuth refresh persistence and reply headers already exist; onboarding,
  identity selection and server draft editing are the remaining extensions.
- Local semantic/document search and reusable references remain central to the
  terminal workflow. No shared competitor benchmark has established a quality winner.

The proposed first implementation sequence is **R01 → finish the R02 handoff →
R03/R04 → R05/R06**, followed by fuller draft editing/reconciliation and the P1
work selected from actual usage feedback. Preserve existing safeguards and
verify feature completion through the MCP protocol and relevant provider behavior,
not just the presence of a new tool name.
