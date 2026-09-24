# Security Policy

## Scope

Aside MCP Remote can invoke browser automation and search user memory through
the local Aside CLI. Treat the `/mcp` endpoint as access to the connected
Aside account.

## Deployment requirements

- Set a strong `MCP_BEARER_TOKEN`.
- Put the service behind HTTPS and a trusted reverse proxy.
- Do not expose browser profile directories, cookies, memory files, or logs.
- Keep `ASIDE_BIN` and all credentials outside the repository.

## Reporting

Please report suspected vulnerabilities privately to the repository owner
before opening a public issue.
