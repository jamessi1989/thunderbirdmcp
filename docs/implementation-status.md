# Roadmap implementation record

This record tracks the work requested on 2 November 2026. The comparison in
`docs/comparison.md` remains a historical review of v0.3.1. “Implemented” here
requires a usable code path and meaningful synthetic regression coverage;
desktop/provider verification is recorded separately and is never inferred from
the presence of a tool.

Development uses synthetic messages and mocked services. No real mailbox writes
or deliveries are needed to build and test these changes.

| ID | State | Implementation and validation |
| --- | --- | --- |
| R01 | Implemented | `policy.py`; read-only/draft-only/full, allowlists and operation boundaries. Direct, CLI, stdio and HTTP rejection tests. |
| R02 | Implemented | Revisioned edit/discard, reviewed special-use handoff, UID-bound reconciliation and conflict-aware replacement. Synthetic MIME/IMAP tests; native sends remain Thunderbird-controlled. |
| R03 | Implemented | `mailbox.py`; explicit read/star/keywords/Gmail labels, bounded batches, capability checks and partial outcomes. Synthetic IMAP tests. |
| R04 | Implemented | Live move/copy/archive/trash/restore/delete and ordinary-folder rename/delete; previews, UIDVALIDITY/COPYUID, scoped expunge and restore receipts tested. |
| R05 | Implemented | Forward drafts with explicit recipients and attachment selection; no inferred Bcc or reply threading. MIME regression tests. |
| R06 | Implemented | BYO browser OAuth enrollment/reconnection with PKCE, state/loopback checks and redacted diagnostics. Mock consent lifecycle; live provider consent remains external verification. |
| R07 | Implemented | HTML/plain alternatives, explicit inline parts and account-bound identity/SMTP selection. Frozen MIME/envelope and revision tests. |
| R08 | Implemented | `pagination.py`; expiring stable snapshots, omission/filter/term diagnostics, up to ten live scopes with partial failures. Index-update and principal-isolation tests. |
| R09 | Implemented; desktop verification open | Native Windows launchers, APPDATA discovery and LOCALAPPDATA storage. Actual Windows CI install/stdio workflow passed; interactive Thunderbird/provider verification remains separate. |
| R10 | Implemented | Frozen 50-item/64-MiB plans, collision-free names, no-overwrite writes, checksums and resume/recovery. Synthetic export tests. |
| R11 | Implemented | Durable operation IDs/history, atomic claims, redaction, scoped pagination, cancellation and partial outcomes across delivery/mailbox/export/scheduled jobs. Journal and effect-boundary tests. |
| R12 | Implemented | Durable account/folder/UIDVALIDITY/UID events, baseline/backlog/dedup/resume, bounded poll/IDLE and explicit service runner. Mock protocol tests. |
| R13 | Implemented | Approved exact revision, SMTP/Gmail/Graph transport, due time/timezone, persistent claims, cancellation, policy revocation and no uncertain retry. Synthetic scheduler/delivery tests. |
| R14 | Implemented | Bounded SPF/DKIM/DMARC header evidence, provenance, unknown/conflicting results; no independent verification or trust verdict. Adversarial-header tests. |
| R15 | Implemented | Header-linked local follow-up candidates, selected folders, age/direction/response exclusions and uncertainty. Same-subject false-merge regression test. |
| R16 | Implemented | Native Gmail/Graph search/read/threads/labels/flags/draft/send, separate scoped credentials and immutable Graph IDs. Mock HTTPS contracts; live consent/delivery remains external verification. |
| R17 | Implemented | Authenticated stateless Streamable HTTP, explicit accounts/file roots, principal-owned persisted state, host/origin/body limits and loopback default. Real MCP ASGI boundary tests; no public deployment. |
| R18 | Implemented | Incremental Maildir adapter and identity-verified source reads. Seven adapter regressions plus existing index/message tests pass; existing IDs/reference format retained. |
| R19 | Implemented | Opt-in bounded local Tesseract/Poppler/Pillow OCR. Six default regressions and an opt-in actual-engine test pass; real synthetic PNG/scanned-PDF extraction succeeded. |
| R20 | Integration implemented; desktop verification open | Pinned separate native addon/broker integration, isolated conflicting package environments and three client configurations. Seven helper tests plus actual companion catalogue/stdio validation; native desktop operations not exercised. |

The integrated suite passed **328 tests**, with two expected Linux-run skips:
the native Windows launcher execution and the separately exercised optional
real-OCR smoke. The 78-message/13-case keyword benchmark retained recall,
precision, MRR and top-1 accuracy of **1.0**, including its negative case. The
0.4.0 wheel/sdist build and fresh-venv plus `uv tool install` checks passed on
Linux/Python 3.11: six launchers, three client configurations, 56-tool protocol
discovery, synthetic search/read/reference/resource/attachments/drafts and
unchanged fixture profiles. Application networking was blocked and the keyring
disabled during those install checks. The final publication runs the
[integrated CI matrix](https://github.com/jamessi1989/thunderbirdmcp/actions/workflows/checks.yml).
The macOS release job uses the supported `macos-15-intel` image: GitHub's arm64
runner pool reported capacity failures before acquiring a runner. This changes
the execution architecture, not the required suite/benchmark/install checks;
the current release does not claim a completed Apple Silicon CI run.
The native Windows
isolation check is [CI run 41531190251](https://github.com/jamessi1989/thunderbirdmcp/actions/runs/41531190251),
whose Windows install job passed; the final integrated release has its own CI.

All 20 implementation items now have usable paths. R09 and R20 retain the
separate desktop/provider validation work described below; this is not a claim
of universal provider compatibility. Actual OCR testing used an isolated
Tesseract 4.1.1/English engine and Poppler with synthetic PNG and image-only PDF
documents, not personal mail. The opt-in smoke can be repeated with installed
engines using `TB_RUN_OCR_SMOKE=1` and the OCR test module.

Pending external verification is explicit: real mailbox-provider consent,
delivery/flags/folders/IDLE behavior and interactive Thunderbird addon operations
have not been exercised with the user's mail. Application approval/tenant
policies, installed OCR engines/languages and native addon installation remain
configuration requirements. Synthetic tests are not independent user reports.
