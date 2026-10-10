"""MCP server for the Custom Domain API (``custom-domain-mcp``)."""

from custom_domain_mcp.server import __version__, build_server, main

__all__ = ["__version__", "build_server", "main"]
