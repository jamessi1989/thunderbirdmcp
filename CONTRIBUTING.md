# Contributing

Installation reports, bug fixes, and reproducible examples are welcome. A
successful installation report is useful: it tells other users which operating
system, Thunderbird version, and terminal client worked together.

## Report an installation

Start with the [installation and client setup instructions](README.md#start-using-it),
then open an [installation feedback issue](https://github.com/jamessi1989/thunderbirdmcp/issues/new?template=installation-feedback.yml).
The [installation guide](docs/installation.md) covers source checkouts, release
wheels, credential setup, and troubleshooting.
Choose success, partial success, or failure, and include:

- The release version or Git commit, operating system and architecture, Python
  and `uv` versions, and Thunderbird version or “not installed.”
- The client and its version, such as Codex, Claude Code, or OpenCode. For a
  model-specific problem, include the provider/model name without account IDs
  or API keys.
- The installation commands and which checks actually ran. An error or a
  skipped check is useful information; a successful install does not imply
  every mail operation was tested.

Public reports should contain sanitized examples. Replace addresses, hostnames,
usernames, and paths that identify you with placeholders. Do not attach your
mail index, original messages, attachments, full client configuration, passwords,
OAuth tokens, or unreviewed client logs. You do not need to connect a real
mailbox or send mail to report an installation.

## Reproduce a bug

Use the [bug report form](https://github.com/jamessi1989/thunderbirdmcp/issues/new?template=bug-report.yml)
with the smallest synthetic example that demonstrates the problem. Describe the
trigger, expected result, actual result, and software versions. For search bugs,
include a fictional query and fictional message or attachment text. For
authentication problems, include the protocol, authentication type, and redacted
error; never include credentials.

If a delivery result is uncertain, inspect its recorded state and the mail
server before trying another send. Reproducing a bug does not require sending
test messages or retrying an ambiguous delivery.

## Work on the code

Use Python 3.10 or later and the checked-in dependency lock:

```bash
git clone https://github.com/jamessi1989/thunderbirdmcp.git
cd thunderbirdmcp
uv sync --locked
uv run python -m unittest discover -s tests -v
uv run python scripts/benchmark_search.py --require-recall 1 --require-precision 1 --require-no-false-positives
uv build
```

The tests create synthetic mailboxes and mock mail transports. They must not
depend on a contributor's Thunderbird profile, keyring, real mail, or paid model
account. When changing behavior, add a regression test for an observable failure
or invariant. Use fictional domains such as `example.test` in fixtures, and
keep generated indexes, exports, logs, and private evaluation data out of Git.

Explain the problem and resulting behavior in a pull request. Include the
relevant validation results and any limitations. For a documentation-only change,
checking commands, links, and claims against implementation is usually enough;
there is no need to rerun the entire unit suite.

## Check an installed package

The clean-install runner builds a wheel and tests it in both a fresh virtual
environment and a separate `uv tool install` environment under temporary homes.
It checks installed commands, client configuration setup, preserved tool links,
and MCP access to generated mail from an unrelated working directory:

```bash
uv run --locked python scripts/test_install.py --json
```

To verify a particular release asset instead, add `--wheel /path/to/package.whl`.
An optional `--constraints /path/to/requirements.txt` constrains the installed
dependencies; CI exports that file from `uv.lock`. Installing dependencies may
need network access. The mailbox checks use synthetic data and do not establish
that a real Thunderbird desktop, provider account, or keyring is configured.

## Try the workflow without an account

After dependencies are installed, the [offline demo](docs/demo.md) runs actual
terminal commands against generated mail and a PDF attachment:

```bash
uv run --locked --offline python scripts/demo.py
```

It uses a temporary home, profile, index, and outbox. It verifies search,
references reused across processes, message and thread reads, attachment text,
reply drafting, and an unsent preview. Its Python processes reject socket
networking and do not use your keyring. The generated terminal illustration is
derived from these checks; it is not a recorded conversation with an AI client.

## Test terminal clients

The opt-in client check uses your configured model accounts and may consume
model credits. It exposes only search, read, and reference tools over a temporary
synthetic mailbox; it does not read your real mail or send messages.

```bash
uv run python scripts/test_clients.py --clients codex
uv run python scripts/test_clients.py --clients claude
uv run python scripts/test_clients.py --clients opencode --opencode-model provider/model
```

Use the [README's client verification section](README.md#check-codex-claude-and-opencode)
for setup and reconnect instructions. Record the client and model versions with
the result. Model-driven checks are separate from synthetic CI; passing one does
not establish the result of the other.

## Describe verification accurately

| Evidence | What it establishes | What it does not establish |
| --- | --- | --- |
| Synthetic unit tests and MCP protocol checks | Covered behavior with generated mail and mocked transports | A working connection to every provider or a complete desktop installation |
| Fresh-wheel installation check | The packaged commands, setup, and synthetic MCP workflow work in an isolated environment | A complete desktop installation or real provider authentication |
| Keyword retrieval benchmark | Results on the published synthetic cases | Better retrieval than another system on real users' mail |
| Actual Codex, Claude Code, and OpenCode smoke checks | Those installed clients can call the exposed tools with the selected models | Every client version, model, plugin, or deployment environment |
| A readonly live IMAP check | The tested account can authenticate and read without changing its read flags | SMTP delivery, OAuth consent, or behavior of other providers |
| An independent installation report | The environment and steps described by that reporter | General support for an operating system or provider |

The recorded v0.3.0 checks include maintainer Linux client smoke tests and
synthetic CI on Linux and macOS. A complete macOS desktop integration and
Windows deployment remain unverified. OAuth token handling has protocol tests;
provider authorization still requires a separately authorized application and
consent. No independent testers or their results are implied by the existence
of issue templates. Refer to the [changelog](CHANGELOG.md) and linked CI runs for
version-specific evidence.
