# Common Ground CLI Guide

The Common Ground repository ships with an experimental command-line
client (`common-ground-cli`) so you can interact with a running backend
without using the browser UI. This guide walks through installation,
configuration, and day-to-day usage.

> **Note**
> The CLI connects to an already running Common Ground backend. You can
> start one locally with Docker (`docker compose up` from the
> `deployment/` directory) or by running the Python backend and Next.js
> frontend manually as described in the main [README](../README.md#🚀-getting-started).

## Prerequisites

- Python 3.12+
- [`uv`](https://github.com/astral-sh/uv) for dependency management
  (`pip install uv`)
- A running Common Ground backend reachable over HTTP/WebSocket

If you are using the Docker deployment the backend listens on
`http://localhost:8000` by default, which is what the CLI expects.

## Installing dependencies

Inside the `core/` directory install the Python environment and ensure
the CLI entry point is available:

```bash
cd core
uv venv
uv sync
```

This step only needs to be performed once per clone. Afterwards you can
invoke the CLI from anywhere with `uv run common-ground-cli`.

## Quick start

With the backend running, issue your first prompt:

```bash
uv run common-ground-cli --prompt "Help me explore the repository layout"
```

The client will:

1. Call `POST /session` to create a short-lived session ID.
2. Connect to `ws://localhost:8000/ws/{session_id}`.
3. Send a `start_run` command for a `partner_interaction` run.
4. Stream the Partner agent’s responses to your terminal.

After the initial response you can keep chatting at the interactive
`you>` prompt. Each line you type becomes a follow-up message that is
sent back to the same run.

### Sample session

```
[system] Requesting session from http://localhost:8000 ...
[system] Session id: 6d7bf4f7-...
[system] Connecting to ws://localhost:8000/ws/6d7bf4f7-...

[system] Run ready (run_id=partner-run-123)

[partner] Absolutely—here’s an overview of the repo structure...
you> What commands are available?
[partner] You can run docker compose up, uv run run_server.py, ...
you> /quit
[system] Quitting chat.
```

Type `/quit` or `/exit` to close the session. The CLI will send a
`stop_run` request before disconnecting.

## Runtime options

The CLI exposes several switches for advanced scenarios:

| Option | Description |
| --- | --- |
| `--api-base URL` | HTTP base URL for REST calls (`http://localhost:8000` by default). |
| `--ws-base URL` | Override the WebSocket base URL if it differs from the API host. |
| `--run-type TYPE` | Run type to start. `partner_interaction` (default) and `chat_completion` support interactive prompts. |
| `--prompt TEXT` | Initial message to send immediately after the run is ready. |
| `--project-id ID` | Associate the run with an existing project UUID. |
| `--request-id ID` | Provide a custom request identifier (defaults to a random UUID). |
| `--ready-timeout SECONDS` | How long to wait for the backend to acknowledge the run before aborting (default 30 seconds). |

### Sending raw JSON commands

If you need to issue advanced commands (e.g., custom run payloads), type
`/raw {"type": "send_to_run", ...}` at the prompt. The CLI validates the
string as JSON and forwards it verbatim to the backend.

### Switching hosts

When tunnelling or connecting to a remote server, update the base URLs:

```bash
uv run common-ground-cli --api-base https://demo.example.com --ws-base wss://demo.example.com --prompt "Hello"
```

`--ws-base` is optional—the CLI derives a WebSocket scheme (`ws://` or
`wss://`) from `--api-base` automatically.

## Troubleshooting

- **Timeout waiting for `run_ready`:** confirm the backend is reachable,
  the WebSocket port is open, and you have authenticated the Gemini
  bridge if required.
- **`Invalid JSON` errors:** only appear when using `/raw`; make sure the
  payload is valid JSON.
- **Connection closed immediately:** the server logs in `core/logs/` (or
  your Docker console) typically explain why the run ended early.

For additional details inspect the CLI source at
[`core/cli.py`](../core/cli.py).
