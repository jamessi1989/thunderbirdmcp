# Thunderbird email MCP: frequently asked questions

These answers describe **jamessi1989/thunderbirdmcp**, the MIT-licensed Python
mail MCP and `tbmail` terminal client. Start with the
[installation guide](installation.md), [synthetic demo](demo.md), or
[workflow reference](workflows.md). The [changelog](../CHANGELOG.md) identifies
which release contains each feature.

## What does Thunderbird MCP do?

It lets compatible AI clients search and read mail, retrieve attachments, cite
messages, prepare and send drafts, and organize mailboxes. It combines a local
search index with live IMAP/SMTP and optional Gmail API/Microsoft Graph paths.
It also provides explicit scheduled sending, new-mail events, export plans and
operation history. All exposed MCP tools can be called with `tbmail call`.

## Does it work with Codex, Claude Code, OpenCode and GLM?

Yes. The setup command registers local MCP entries for Codex, Claude Code and
OpenCode. OpenCode's MCP configuration applies to GLM and its other supported
models. Terminalman sessions use those clients' existing MCP connections or the
`tbmail` shell command. The repository includes an opt-in
[actual client test](../README.md#test-actual-client-tool-calls); it uses synthetic
mail but may consume your model account's credits.

## Can I connect ChatGPT directly to my local mailbox?

Finding this project through ChatGPT search does not grant access to mail.
Local Codex and other terminal clients launch its stdio MCP on your machine.
The project also offers [authenticated Streamable HTTP](workflows.md#authenticated-http)
for compatible remote clients, using provisioned bearer tokens and explicit
account/file boundaries. It does not publish a hosted ChatGPT app or provide an
OAuth authorization server for automatic ChatGPT connector enrollment. Direct
ChatGPT web integration has not been verified.

## Can it search PDF attachments and Office documents?

Yes. The local index searches extracted text from supported PDF, DOCX, XLSX,
PPTX, text, HTML and XML attachments. You can also read an attachment's text
without exporting the file. Filenames remain searchable when extraction is
unsupported. Size, page, archive and time limits are reported; encrypted or
unsupported files are not silently treated as successfully extracted. See
[search and indexing](../README.md#search-and-indexing) for the current limits.
Scanned PDFs and images can use optional local Tesseract OCR; install its engine,
language packs and the documented Python/PDF dependencies explicitly. OCR is off
by default and does not download models. `ocr_status` reports availability.

## Does mail leave my computer?

The index, draft store, semantic retrieval and document extraction are local.
Live mail operations communicate with the selected mail provider. Message text
returned to a hosted AI client is then processed by that client's model
provider. Optional model downloads and OAuth consent also require network
access. Local storage does not make a hosted model session offline.

## Can I use it in read-only mode?

Yes. Set `TB_MCP_PROFILE=read-only` in the server environment. `draft-only` adds
draft preparation/editing and unsent draft handoff; `full` is the default for
compatibility. Tool allowlists, account restrictions and file roots are also
available. These are enforced restrictions, including for scheduled work;
read/write annotations alone are not the access control.

## Will reading a message mark it as read?

Ordinary message reads leave read/unread flags unchanged. IMAP reads use
read-only selection and `BODY.PEEK`. Changing read/unread, starred state,
keywords or supported Gmail labels is an explicit mutation through the triage
tools. Preview a change before applying it.

## Can it send replies, forwards and scheduled email?

Yes. Prepare and inspect a saved draft, then explicitly send the reviewed
revision. Drafts support To/Cc/Bcc, attachments, HTML with a plain alternative,
inline content and configured sender identities. Forwards require selected
recipients and attachments. Scheduling requires an approved saved revision,
due time and an explicitly running scheduler. Delivery claims prevent concurrent
resends; an uncertain result requires investigation rather than an automatic
retry. See [draft workflows](workflows.md#draft-edit-forward-and-review).

## Can I continue editing a draft in Thunderbird?

`save_draft_to_thunderbird` saves an unsent draft into the same account's IMAP
Drafts folder. Its local snapshot becomes send-locked. After editing in
Thunderbird, explicit reconciliation imports the current server version for
review and checked updates. Sending a handed-off native draft remains in
Thunderbird. Local Folders and cross-account draft destinations are unsupported.

## Does it support Gmail, Outlook and multiple accounts?

Configured Thunderbird IMAP accounts can use the standard mail path. Native
Gmail API and Microsoft Graph adapters offer provider-specific searches and
permissions, with separate credential namespaces. Bring your own OAuth app;
provider consent, tenant policy and app verification still apply. Select the
Thunderbird account ID for each operation. Native adapter tests use mocked
provider contracts, so provider authentication/delivery is not claimed verified
for every account. See [provider enrollment](workflows.md#gmail-api-graph-and-interactive-enrollment).

## Can I reuse an email reference in another coding session?

Yes. Local `thunderbird://message/...` references can be pasted into another
project or Terminalman conversation using the same index. They survive ordinary
reindexing and folder changes. IMAP references contain account, endpoint,
folder, UIDVALIDITY and UID; search again after a move or folder replacement.
References are identifiers, not public links or permission to access mail.

## Does local indexing support Thunderbird Maildir storage?

Yes. It reads downloaded mbox and Maildir mail beneath the selected profile's
mail directories. Maildir indexing handles `cur`/`new` files incrementally and
ignores `tmp`; it does not rename or change files in Thunderbird's store. Live
IMAP and native provider search are available when a message is not downloaded.

## Can it manage contacts, calendars, tasks and Thunderbird settings?

Through the optional [native companion integration](native-integration.md).
That separately maintained add-on/broker runs in its own environment and uses
the MCP name `thunderbird-native`. The helper pins its source, preserves this
project's `thunderbird` entry and defaults to native read-only tools. Native
desktop behavior has a different verification scope from our mail tools.

## How does it compare with other email MCP servers?

Its main combination is local mail and document retrieval, reusable references,
reviewable outgoing mail, multiple transports and a terminal interface.
The [comparison](comparison.md) reviews five projects and links their source and
issue evidence; its baseline is explicitly v0.3.1. The
[implementation record](implementation-status.md) tracks subsequent work. No
shared competitor benchmark establishes this project as the most accurate,
fastest or “best” MCP. The included synthetic benchmark is a regression test.

## Is this the same project as U-C4N/Thunderbird-MCP?

No. This repository is **jamessi1989/thunderbirdmcp**, importing
`thunderbird_mcp`. U-C4N's native bridge imports `tbmcp` and is an optional
companion. Both use the distribution name `thunderbird-mcp`; follow the explicit
GitHub checkout/release instructions and keep their environments separate.
Neither project is implied to be an official Mozilla or AI-client product.

## What does it cost, and where can I report a problem?

The source is available under [MIT](../LICENSE). Your mail provider, hosting or
AI model account can have its own charges. Report reproducible issues through
the [GitHub issue forms](https://github.com/jamessi1989/thunderbirdmcp/issues/new/choose),
using synthetic examples and sanitized errors. Never attach private mailbox
databases, tokens, client configuration or real mail to a public issue.
