"""Optional accelerator: the terminal's own MCP server.

MetaTrader 5 build 6060+ ships an HTTP MCP server on ``127.0.0.1`` (terminal on
22346, MetaEditor on 22345). It offers 73 tools, including a strategy tester and
``compile_file``. When it is reachable we use it, because it is faster and runs
inside the already-logged-in terminal.

It is **never** the only route. It needs an API key the user has to copy out of
a dialog by hand, MetaEditor must be running for port 22345, and none of it is
documented by MetaQuotes, so it can change in the next build. The CLI route in
``tester.py`` is the ground floor; this is a lift that may be out of service.

We talk to it as a client, so nothing leaves the machine. The vendor endpoint in
``assistant.ini`` belongs to the terminal's own chat window, which is a different
direction of traffic and is not used here.

Own work.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

__all__ = ["NativeClient", "NativeUnavailable", "native_terminal", "native_editor"]

_PROTOCOL = "2025-06-18"
_PROBE_TTL = 60.0


class NativeUnavailable(RuntimeError):
    """The native server cannot be used right now. Always has a reason."""


@dataclass
class _Probe:
    ok: bool
    reason: str
    at: float


class NativeClient:
    """Minimal Streamable-HTTP MCP client for one of the two terminal servers."""

    def __init__(self, url: str, api_key: str | None, label: str) -> None:
        self.url = url
        self.label = label
        self._key = api_key
        self._session: str | None = None
        self._id = 0
        self._lock = threading.Lock()
        self._probe: _Probe | None = None

    # -- transport --------------------------------------------------------

    def _rpc(self, method: str, params: dict | None = None, *,
             notify: bool = False, timeout: float = 120.0) -> dict:
        if not self._key:
            raise NativeUnavailable(
                "No API key configured for the terminal's built-in MCP server.")
        with self._lock:
            self._id += 1
            message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
            if not notify:
                message["id"] = self._id
            if params is not None:
                message["params"] = params
            headers = {
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": _PROTOCOL,
            }
            if self._session:
                headers["Mcp-Session-Id"] = self._session
            request = urllib.request.Request(
                self.url, data=json.dumps(message).encode(), headers=headers,
                method="POST")
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    if self._session is None:
                        got = response.headers.get("Mcp-Session-Id")
                        if got:
                            self._session = got
                    raw = response.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    raise NativeUnavailable(
                        "The terminal's MCP server rejected the API key (401). "
                        "Copy the current key from Tools -> Options -> MCP into "
                        "MT5_NATIVE_MCP_KEY."
                    ) from exc
                raise NativeUnavailable(
                    f"{self.label} returned HTTP {exc.code}.") from exc
            except OSError as exc:
                raise NativeUnavailable(
                    f"{self.label} is not reachable at {self.url}: {exc}") from exc

        if not raw.strip():
            return {}
        if raw.lstrip().startswith(("event:", "data:")):
            chunks = [line[5:].strip() for line in raw.splitlines()
                      if line.startswith("data:")]
            raw = chunks[-1] if chunks else "{}"
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise NativeUnavailable(
                f"{self.label} sent a response that is not JSON.") from exc

    def _handshake(self) -> None:
        if self._session is not None:
            return
        self._rpc("initialize", {
            "protocolVersion": _PROTOCOL, "capabilities": {},
            "clientInfo": {"name": "mt5-mcp", "version": "1.0"}}, timeout=30)
        self._rpc("notifications/initialized", {}, notify=True, timeout=30)

    # -- public API -------------------------------------------------------

    def available(self, *, force: bool = False) -> tuple[bool, str]:
        """Cheap, cached reachability check for ``mt5_doctor`` and routing."""
        import time
        now = time.time()
        if not force and self._probe and now - self._probe.at < _PROBE_TTL:
            return self._probe.ok, self._probe.reason
        try:
            self._handshake()
            ok, reason = True, f"{self.label} reachable at {self.url}"
        except NativeUnavailable as exc:
            ok, reason = False, str(exc)
        except Exception as exc:  # noqa: BLE001 - never let a probe raise
            ok, reason = False, f"{self.label}: {exc}"
        self._probe = _Probe(ok, reason, now)
        return ok, reason

    def call(self, name: str, arguments: dict | None = None,
             timeout: float = 180.0) -> str:
        """Invoke one native tool and return its text payload."""
        self._handshake()
        response = self._rpc("tools/call",
                             {"name": name, "arguments": arguments or {}},
                             timeout=timeout)
        if "error" in response:
            raise NativeUnavailable(
                f"{name}: {response['error'].get('message', 'unknown error')}")
        result = response.get("result", {})
        text = "".join(part.get("text", "") for part in result.get("content", [])
                       if part.get("type") == "text")
        if not text:
            text = json.dumps(result.get("structuredContent", result))
        if result.get("isError"):
            raise NativeUnavailable(f"{name}: {text[:400]}")
        return text

    def call_json(self, name: str, arguments: dict | None = None,
                  timeout: float = 180.0) -> Any:
        text = self.call(name, arguments, timeout)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"_text": text}

    def list_tools(self) -> list[dict]:
        self._handshake()
        return self._rpc("tools/list", {}).get("result", {}).get("tools", [])


_terminal: NativeClient | None = None
_editor: NativeClient | None = None


def native_terminal() -> NativeClient:
    global _terminal
    if _terminal is None:
        from ..config import get_settings
        settings = get_settings()
        _terminal = NativeClient(settings.native_mcp_terminal_url,
                                 settings.native_mcp_key, "Terminal MCP")
    return _terminal


def native_editor() -> NativeClient:
    global _editor
    if _editor is None:
        from ..config import get_settings
        settings = get_settings()
        _editor = NativeClient(settings.native_mcp_editor_url,
                               settings.native_mcp_key, "MetaEditor MCP")
    return _editor
