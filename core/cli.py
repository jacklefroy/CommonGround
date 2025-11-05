"""Common Ground command-line client.

This module provides a small interactive CLI for connecting to a running
Common Ground backend over its public HTTP/WebSocket API. The goal is to
make it easy to start a run and chat with the Partner agent without needing
the web UI.

Typical usage::

    uv run python -m core.cli --prompt "Help me research ..."

The CLI performs the following steps:

* POST /session to obtain a temporary session id
* Connects to ``/ws/{session_id}``
* Sends a ``start_run`` command (``partner_interaction`` by default)
* Waits for the ``run_ready`` event and then streams responses in the
  terminal while allowing the user to send follow-up prompts.

The CLI is deliberately minimal but supports sending arbitrary JSON payloads
with the ``/raw`` command for advanced scenarios.

Interactive messaging currently targets ``partner_interaction`` (and
``chat_completion``) runs. For other run types, compose the full JSON payload
and send it with ``/raw``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from urllib.parse import urljoin, urlparse

import aiohttp
import websockets


@dataclass
class RunState:
    """Holds state shared between the websocket listener and CLI loop."""

    request_id: str
    run_type: str
    run_id: Optional[str] = None
    ready_event: asyncio.Event = field(default_factory=asyncio.Event)

    def set_run_ready(self, run_id: str) -> None:
        self.run_id = run_id
        if not self.ready_event.is_set():
            self.ready_event.set()


async def create_session(api_base: str) -> str:
    """POST /session and return the session id.

    Args:
        api_base: Base HTTP URL for the Common Ground API.

    Raises:
        RuntimeError: If the API request fails or returns an unexpected payload.
    """

    async with aiohttp.ClientSession() as session:
        url = urljoin(api_base if api_base.endswith("/") else f"{api_base}/", "session")
        async with session.post(url) as response:
            if response.status != 200:
                text = await response.text()
                raise RuntimeError(f"Failed to create session (status {response.status}): {text}")
            payload = await response.json()

    session_id = payload.get("session_id")
    if not session_id:
        raise RuntimeError(f"API response did not contain a session_id: {payload!r}")
    return session_id


def derive_ws_base(api_base: str) -> str:
    """Best-effort conversion from an HTTP base URL to a WebSocket base URL."""

    parsed = urlparse(api_base)
    if parsed.scheme not in {"http", "https", "ws", "wss"}:
        raise ValueError(f"Unsupported API base scheme: {parsed.scheme}")

    if parsed.scheme in {"http", "https"}:
        ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    else:
        ws_scheme = parsed.scheme

    netloc = parsed.netloc
    path = parsed.path.rstrip("/")
    return f"{ws_scheme}://{netloc}{path}"


async def websocket_listener(ws: websockets.WebSocketClientProtocol, state: RunState) -> None:
    """Listen for events from the server and render them to stdout."""

    try:
        async for raw_message in ws:
            try:
                message = json.loads(raw_message)
            except json.JSONDecodeError:
                print(f"[server] {raw_message}", flush=True)
                continue

            event_type = message.get("type", "<unknown>")
            if event_type == "run_ready":
                data = message.get("data", {})
                if data.get("request_id") == state.request_id and data.get("status") == "success":
                    state.set_run_ready(data.get("run_id", ""))
                    run_id = state.run_id or "<unknown>"
                    print(f"\n[system] Run ready (run_id={run_id})\n", flush=True)
                    continue

            if event_type == "llm_chunk" and message.get("chunk_type") == "content":
                agent = message.get("agent_id", "agent")
                content = message.get("data", {}).get("content", "")
                if content:
                    print(f"[{agent}] {content}", flush=True)
                continue

            if event_type == "error":
                agent = message.get("agent_id", "system")
                error_message = message.get("data", {}).get("message", "Unknown error")
                print(f"\n[error:{agent}] {error_message}\n", file=sys.stderr, flush=True)
                continue

            # For other events, pretty-print the JSON payload so advanced users
            # can inspect what's happening.
            pretty = json.dumps(message, indent=2, ensure_ascii=False)
            print(f"\n[event:{event_type}]\n{pretty}\n", flush=True)
    except websockets.ConnectionClosedOK:
        print("\n[system] WebSocket connection closed by server.", flush=True)
    except websockets.ConnectionClosedError as exc:  # pragma: no cover - depends on runtime
        print(f"\n[system] WebSocket closed with error: {exc}", file=sys.stderr, flush=True)


async def send_start_run(ws: websockets.WebSocketClientProtocol, state: RunState, project_id: Optional[str]) -> None:
    payload: Dict[str, Any] = {
        "type": "start_run",
        "data": {
            "request_id": state.request_id,
            "run_type": state.run_type,
        },
    }
    if project_id:
        payload["data"]["project_id"] = project_id

    await ws.send(json.dumps(payload))


def build_message_payload(run_type: str, text: str) -> Dict[str, Any]:
    if run_type in {"partner_interaction", "chat_completion"}:
        return {"prompt": text}

    # For advanced run types the user must provide raw JSON via /raw.
    raise ValueError(
        "Interactive messaging is only implemented for 'partner_interaction' and 'chat_completion'. "
        "Use the /raw command for other run types."
    )


async def send_chat_message(
    ws: websockets.WebSocketClientProtocol,
    state: RunState,
    text: str,
) -> None:
    if not state.run_id:
        raise RuntimeError("Run is not ready yet; cannot send message.")

    message_payload = build_message_payload(state.run_type, text)
    payload = {
        "type": "send_to_run",
        "data": {
            "run_id": state.run_id,
            "message_payload": message_payload,
        },
    }
    await ws.send(json.dumps(payload))


async def send_raw(ws: websockets.WebSocketClientProtocol, raw_json: str) -> None:
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError as exc:  # pragma: no cover - user error
        raise ValueError(f"Invalid JSON: {exc}") from exc

    await ws.send(json.dumps(parsed))


async def interactive_loop(
    ws: websockets.WebSocketClientProtocol,
    state: RunState,
    initial_prompt: Optional[str],
) -> None:
    if initial_prompt:
        await send_chat_message(ws, state, initial_prompt)

    loop = asyncio.get_running_loop()

    while True:
        try:
            user_input = await loop.run_in_executor(None, lambda: input("you> ").strip())
        except (EOFError, KeyboardInterrupt):  # pragma: no cover - interactive exit
            print("\n[system] Exiting.")
            break

        if not user_input:
            continue

        if user_input in {"/quit", "/exit"}:
            print("[system] Quitting chat.")
            break

        if user_input.startswith("/raw "):
            _, raw = user_input.split(" ", 1)
            try:
                await send_raw(ws, raw)
            except ValueError as exc:
                print(f"[system] {exc}")
            continue

        try:
            await send_chat_message(ws, state, user_input)
        except ValueError as exc:
            print(f"[system] {exc}")
        except RuntimeError as exc:
            print(f"[system] {exc}")

    if state.run_id:
        stop_payload = {"type": "stop_run", "data": {"run_id": state.run_id}}
        await ws.send(json.dumps(stop_payload))


async def run_cli(args: argparse.Namespace) -> None:
    api_base = args.api_base.rstrip("/")
    ws_base = args.ws_base.rstrip("/") if args.ws_base else derive_ws_base(api_base)

    print(f"[system] Requesting session from {api_base} ...", flush=True)
    session_id = await create_session(api_base)
    print(f"[system] Session id: {session_id}", flush=True)

    ws_url = f"{ws_base}/ws/{session_id}"
    print(f"[system] Connecting to {ws_url} ...", flush=True)

    async with websockets.connect(ws_url) as ws:
        state = RunState(request_id=args.request_id or str(uuid.uuid4()), run_type=args.run_type)

        listener_task = asyncio.create_task(websocket_listener(ws, state))
        try:
            await send_start_run(ws, state, project_id=args.project_id)
            await asyncio.wait_for(state.ready_event.wait(), timeout=args.ready_timeout)
            await interactive_loop(ws, state, initial_prompt=args.prompt)
        except asyncio.TimeoutError:
            raise RuntimeError(
                "Timed out waiting for run_ready. Ensure the backend is running and reachable."
            )
        finally:
            listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listener_task


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Interact with a Common Ground backend via the command line.")
    parser.add_argument(
        "--api-base",
        default="http://localhost:8000",
        help="Base HTTP URL for the Common Ground API (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--ws-base",
        help="Override the WebSocket base URL. Defaults to the API base converted to ws:// or wss://",
    )
    parser.add_argument(
        "--run-type",
        default="partner_interaction",
        choices=["partner_interaction", "chat_completion", "principal_direct", "fim"],
        help="Run type to request when sending start_run.",
    )
    parser.add_argument(
        "--prompt",
        help="Initial prompt to send immediately after the run is ready.",
    )
    parser.add_argument(
        "--project-id",
        help="Optional project id to associate with the run.",
    )
    parser.add_argument(
        "--request-id",
        help="Custom request id to include in the start_run message (defaults to a random UUID).",
    )
    parser.add_argument(
        "--ready-timeout",
        type=float,
        default=30.0,
        help="Seconds to wait for the run_ready event before giving up.",
    )
    return parser


async def _async_main(args: argparse.Namespace) -> int:
    try:
        await run_cli(args)
        return 0
    except Exception as exc:  # pragma: no cover - top-level safety
        print(f"[system] {exc}", file=sys.stderr)
        return 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(_async_main(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
