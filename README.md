# Aside MCP Remote

Expose the local [`aside mcp`](https://aside.com/) stdio server through a
standard Streamable HTTP endpoint.

```text
MCP client
    │  JSON-RPC over HTTP
    ▼
/mcp  ──  this bridge
    │  JSON-RPC over stdio
    ▼
aside mcp  ──  Aside Browser
```

The bridge keeps one `aside mcp` process per MCP session and forwards
`initialize`, notifications, and tool calls without changing their JSON-RPC
payloads. The exposed tools come from Aside itself, typically:

- `repl` — run Playwright-style JavaScript in the browser
- `exec` — run a browser-agent task
- `memory_search` — search the user's Aside memory

## Run locally

Requirements: Python 3.9 or newer and the `aside` CLI available on `PATH`.

```sh
python3 server.py --host 127.0.0.1 --port 8766
```

If the CLI is installed at a non-standard path:

```sh
ASIDE_BIN=/path/to/aside python3 server.py
```

The demo page is served at `http://127.0.0.1:8766/` and the MCP endpoint is
`http://127.0.0.1:8766/mcp`.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `ASIDE_BIN` | `aside` from `PATH` | Path to the Aside CLI |
| `PORT` | `8766` | Default HTTP port |
| `MCP_REQUEST_TIMEOUT_SECONDS` | `180` | Maximum wait for a tool call |
| `MCP_BEARER_TOKEN` | empty | Optional bearer token for `/mcp` |
| `MCP_CORS_ORIGIN` | empty | Optional CORS origin for browser clients |

For a remote deployment, set `MCP_BEARER_TOKEN`. The MCP endpoint can execute
browser actions and search user memory, so leaving it unauthenticated is only
appropriate for a trusted local POC.

## MCP client example

Configure a Streamable HTTP MCP client with:

```text
http://host.example:8766/mcp
```

When authentication is enabled, send:

```http
Authorization: Bearer <MCP_BEARER_TOKEN>
```

The endpoint uses the standard session flow:

1. `POST /mcp` with `initialize`
2. `POST /mcp` with `notifications/initialized`
3. `POST /mcp` with `tools/list` or `tools/call`
4. `DELETE /mcp` with `Mcp-Session-Id` when the client is done

## Security

This service is a protocol bridge, not an authentication boundary by itself.
Do not commit tokens, browser profiles, cookies, memory files, logs, or `.env`
files. Use HTTPS and a non-empty `MCP_BEARER_TOKEN` before exposing it beyond a
trusted network. Keep the endpoint behind a reverse proxy with rate limits in
production.

## License

MIT. See [LICENSE](LICENSE).
