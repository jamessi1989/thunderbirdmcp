# Optional native Thunderbird desktop integration

Contacts, calendars, tasks, message filters and Thunderbird settings use a
separate native MCP: [U-C4N/Thunderbird-MCP](https://github.com/U-C4N/Thunderbird-MCP).
Our mail server remains `thunderbird`; the companion is registered as
`thunderbird-native`. This implements roadmap R20 through an integration, not
through new IMAP methods or an add-on maintained by this project.

The reviewed pin is **1.4.0**, commit
[`cddefb8010fa39ac85bf4787c3449dca45de2770`](https://github.com/U-C4N/Thunderbird-MCP/tree/cddefb8010fa39ac85bf4787c3449dca45de2770).
Upstream is [MIT licensed](https://github.com/U-C4N/Thunderbird-MCP/blob/cddefb8010fa39ac85bf4787c3449dca45de2770/LICENSE).
Its own add-on connects Thunderbird to a local broker; native functions require
that add-on and a running compatible Thunderbird. Upstream requires Thunderbird
128+ and Python 3.11+. See its
[architecture](https://github.com/U-C4N/Thunderbird-MCP/blob/cddefb8010fa39ac85bf4787c3449dca45de2770/docs/ARCHITECTURE.md)
and [validation record](https://github.com/U-C4N/Thunderbird-MCP/blob/cddefb8010fa39ac85bf4787c3449dca45de2770/docs/VALIDATION-1.4.md).

## Install in a separate environment

Run from this project's checkout or unpacked source distribution:

```sh
git clone https://github.com/U-C4N/Thunderbird-MCP ../thunderbird-native
git -C ../thunderbird-native checkout --detach cddefb8010fa39ac85bf4787c3449dca45de2770
uv venv --python 3.11 ../thunderbird-native/.venv
uv pip install --python ../thunderbird-native/.venv/bin/python ../thunderbird-native
```

On Windows use `../thunderbird-native/.venv/Scripts/python.exe` for the final
command and subsequent native Python commands.

Both projects use the distribution name `thunderbird-mcp` and executable name
`thunderbird-mcp`, but different Python modules and MCP SDK major versions.
Keep these environments separate; installing one into the other replaces the
package. The companion commands below use its absolute/explicit interpreter and
`-m tbmcp` to avoid that name collision. See upstream's
[package metadata](https://github.com/U-C4N/Thunderbird-MCP/blob/cddefb8010fa39ac85bf4787c3449dca45de2770/pyproject.toml).

Build the pinned add-on and follow the printed manual installation instructions:

```sh
../thunderbird-native/.venv/bin/python -I -m tbmcp install-addon --manual
```

Use the intended Thunderbird profile when installing the add-on. The upstream
automatic installer can restart Thunderbird; this project's helper only edits
client configuration and never invokes that installer.

## Register alongside the mail MCP

Replace `/absolute/path/to/profile` with the intended Thunderbird profile folder
containing `prefs.js`. Preview the client changes:

```sh
uv run python scripts/setup_native_bridge.py \
  --upstream ../thunderbird-native \
  --profile /absolute/path/to/profile \
  --clients codex claude opencode
```

Repeat with `--apply` to write the entries and retain private backups of changed
client files. The helper checks the Git pin, clean checkout and installed Python
code, preserves existing `thunderbird` entries and unrelated configuration, and
registers only `contacts,calendar,filters,accounts,settings,admin`. It never prints
existing client configuration or copies bridge credentials into it.

Read-only tools are the default. Add `--allow-writes --apply` to enable native
changes while retaining upstream's confirmation gates. This native server has
its **own policy and profile-wide access**: this mail MCP's account restrictions,
allowlist and draft-only mode do not constrain it. Native configuration disables
inherited `TBMCP_YOLO`, unsafe preference writes and extra tool selection.

The helper uses the clients' documented local MCP formats:
[Codex](https://learn.chatgpt.com/docs/extend/mcp),
[Claude Code](https://code.claude.com/docs/en/mcp), and
[OpenCode](https://opencode.ai/docs/mcp-servers/).
OpenCode configuration also applies when GLM is the selected model. Reconnect
the new server in an existing session, or resume the conversation after restarting
the client. Project-level client settings may override these user entries.

## Verify the desktop connection

After the add-on is installed and Thunderbird is running, call
`thunderbird-native`'s `tb_status`. Inspect `calendar_list`, `addressbook_list`,
`filter_list` and `pref_get` before making changes. Native IDs and our mail
references are separate namespaces; do not pass one server's identifiers to the
other. Use
[upstream's tool reference](https://github.com/U-C4N/Thunderbird-MCP/blob/cddefb8010fa39ac85bf4787c3449dca45de2770/docs/TOOL-REFERENCE.md)
for required arguments.

Validation on 2 November 2026: seven synthetic setup regressions pass. An isolated
Linux/Python 3.11 installation of the pinned companion advertised 34 read-only
desktop/admin tools, or 72 with native writes enabled. Our MCP client completed
stdio initialization, listed the 34 tools and rejected an omitted write tool.
All three client configurations applied idempotently to temporary homes; the
synthetic profile stayed unchanged. These checks **do not verify a real add-on
connection or native calendar/contact/filter mutations**. Windows daemon startup
also remains subject to the upstream
[open issue #8](https://github.com/U-C4N/Thunderbird-MCP/issues/8).

To remove the integration, remove only the `thunderbird-native` client entries
and uninstall the companion add-on in Thunderbird. This leaves the mail MCP's
entry, index and references intact. Keep bridge state and profile files private;
they are user data, not project source.
