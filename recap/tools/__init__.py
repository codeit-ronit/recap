"""Tools: how RECAP reaches the infrastructure, and the security boundary in front of it.

    registry.py    trusted tool catalog (names, risk tiers, argument rules) from policies.yaml
    gateway.py     ToolGateway: every call passes policy, budget, bounds, evidence and audit
    mcp_server.py  MCP server that exposes the environment as tools (an untrusted surface)
    mcp_client.py  McpToolClient: the ToolTransport that speaks real MCP to that server

See README.md in this folder for the read path, the write path and the trust model.
"""
