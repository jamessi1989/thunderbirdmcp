"""Search, read, reference, and send Thunderbird mail from MCP clients."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("thunderbird-mcp")
except PackageNotFoundError:
    __version__ = "development"
