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

It also exposes a separate OpenAI-compatible adapter. It does not replace
MCP: `/v1/chat/completions` creates a short-lived Aside MCP session, calls the
`exec` tool, and converts the result to an OpenAI chat response.

```text
OpenAI client
    │  /v1/chat/completions
    ▼
this bridge ── /mcp ── aside mcp ── Aside Browser
```

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
`http://127.0.0.1:8766/mcp`. The OpenAI-compatible base URL is
`http://127.0.0.1:8766/v1`.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `ASIDE_BIN` | `aside` from `PATH` | Path to the Aside CLI |
| `PORT` | `8766` | Default HTTP port |
| `MCP_REQUEST_TIMEOUT_SECONDS` | `180` | Maximum wait for a tool call |
| `MCP_BEARER_TOKEN` | empty | Optional bearer token for `/mcp` |
| `MCP_CORS_ORIGIN` | empty | Optional CORS origin for browser clients |
| `OPENAI_MODEL` | `aside-browser` | Model ID exposed by `/v1` |
| `OPENAI_PROGRESS_MESSAGE` | `요청을 처리하고 있어요.` | First streamed status chunk |

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

## OpenAI-compatible client

List the exposed model:

```sh
curl http://127.0.0.1:8766/v1/models
```

Use the same base URL with an OpenAI SDK or any compatible client:

```sh
curl http://127.0.0.1:8766/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "aside-browser",
    "messages": [{"role": "user", "content": "현재 서울 날씨를 확인해줘"}],
    "stream": true
  }'
```

Streaming sends an immediate progress chunk and a final result chunk as SSE.
Because Aside is an agent rather than a token-generating model, the final
result is delivered as one completed chunk instead of token-by-token output.
The adapter currently accepts chat messages and does not expose OpenAI
function calling; browser work is performed through Aside's `exec` tool.

## Tasks API

Long-running browser work can be started without waiting for Aside to finish.
`POST /v1/tasks` starts `aside exec` and returns the Aside session id as
`taskId` as soon as the session is created. Poll `GET /v1/tasks/{taskId}` for
the result. `/v1/chat/completions` remains synchronous.

```sh
curl http://127.0.0.1:8766/v1/tasks \
  -H 'Content-Type: application/json' \
  -d '{"prompt": "현재 서울 날씨를 확인해줘"}'
```

```json
{"taskId": "ses_01HQ7B", "status": "running"}
```

```sh
curl http://127.0.0.1:8766/v1/tasks/ses_01HQ7B
```

The status response uses `running`, `succeeded`, `failed`, or `interrupted`.
`succeeded` includes Aside's final text in `result`. Pass `account` in the
create body, or `?account=u1` on the status request, when the task belongs to
a non-default Aside profile.

## Security

This service is a protocol bridge, not an authentication boundary by itself.
Do not commit tokens, browser profiles, cookies, memory files, logs, or `.env`
files. `MCP_BEARER_TOKEN` protects `/mcp`, `/v1/chat/completions`, and `/v1/tasks`. Use HTTPS and a
non-empty token before exposing either endpoint beyond a trusted network.
Keep the endpoint behind a reverse proxy with rate limits in production.

## License

MIT. See [LICENSE](LICENSE).

## MCP session lifecycle

Idle MCP sessions expire after 15 minutes (`MCP_SESSION_TTL_SECONDS`, default
`900`). Requests in progress are protected from idle cleanup. Clients must
initialize a new session after expiration. At most 64 HTTP MCP sessions are
retained (`MCP_MAX_SESSIONS`). At capacity, the least recently used session
idle for at least 60 seconds (`MCP_SESSION_PRESSURE_IDLE_SECONDS`) is closed
before admitting a new connection. Running requests and recently used sessions
are protected; HTTP 503 is returned only when no eligible session can be retired.
Requests with an expired session ID receive HTTP 404 so clients can initialize
a new session, as required by the MCP transport protocol. Timeout and explicit DELETE close the child process
and its pipes. SIGTERM also cleans up retained sessions.

## Select an execution model

`POST /v1/chat/completions` accepts an Aside model ID in `model`, including
`provider/model` IDs. `aside-browser` (or omitting `model`) keeps the existing
Aside default. Explicit models are passed to `aside exec --model` and must be
available to the local Aside account. `/v1/models` includes the default alias and models registered in the current
Aside account: configured provider models plus account-authorized chat models
from the local provider catalog. It reads the local Aside configuration on each
request, so configuration changes appear without restarting the bridge. This
is a local catalog, not a live provider availability check. Missing or unreadable
catalogs fall back to the default alias. `ASIDE_HOME` overrides `~/.aside` for
catalog discovery. Credentials and provider URLs are never included in responses. Both streaming and non-streaming
responses report the requested model. Streaming still sends a progress chunk
followed by the completed answer.

```json
{
  "model": "opencodex/litellm/dgx-deepseek-v4-flash-0731",
  "messages": [{"role": "user", "content": "Reply with OK."}]
}
```

The model above is an example; availability depends on your account.
