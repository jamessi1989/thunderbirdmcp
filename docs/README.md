# Thunderbird MCP documentation

Documentation for **[jamessi1989/thunderbirdmcp](https://github.com/jamessi1989/thunderbirdmcp)**:
an MIT-licensed Python email MCP server and terminal client for Thunderbird mail,
Codex, Claude Code, OpenCode and Terminalman.

| Task | Guide |
| --- | --- |
| Understand the project and supported workflows | [Project README](../README.md) |
| Try it with synthetic mail and no credentials | [Demo](demo.md) |
| Install on Linux, macOS or Windows and configure clients | [Installation](installation.md) |
| Draft/edit/send, organize, export, schedule and use providers | [Workflows and limits](workflows.md) |
| Check privacy, read-only access, ChatGPT scope and common questions | [FAQ](faq.md) |
| Add contacts, calendars, tasks, filters and settings | [Optional native integration](native-integration.md) |
| Compare mail MCP approaches and issue-backed feature requests | [Dated comparison](comparison.md) |
| Check implemented roadmap work and remaining verification | [Implementation record](implementation-status.md) |
| See changes by version | [Changelog](../CHANGELOG.md) |
| Run checks or contribute | [Contributing](../CONTRIBUTING.md) |
| Preserve GitHub traffic and release download history | [Repository traffic](repository-traffic.md) |

Download published artifacts from
[GitHub Releases](https://github.com/jamessi1989/thunderbirdmcp/releases).
The active tool schemas are available through `tbmail tools --json`; every MCP
tool also has a terminal route through `tbmail call`. Documentation describes
capabilities and their boundaries separately from synthetic, provider and
desktop validation.
