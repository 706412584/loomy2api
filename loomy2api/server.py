"""HTTP gateway: OpenAI-compatible + Anthropic-compatible endpoints."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from . import constants as C
from . import anthropic as anth
from .panel import Panel
from .pool import AccountPool, PoolError
from .upstream import ModelGateway, sse_usage, human_usage

__all__ = ["Gateway", "serve"]


class StreamStalled(Exception):
    """The upstream stopped sending data for longer than we tolerate."""

    def __init__(self, seconds: float, kind: str = "idle"):
        super().__init__(f"upstream stream stalled for {seconds:.0f}s ({kind})")
        self.seconds = seconds
        self.kind = kind


class _ContentRejected:
    """Stand-in for an upstream response when content inspection said no.

    ``_open_stream`` normally hands back the live ``http.client`` response so
    the caller can read and forward the error.  A content rejection is
    terminal — no other account will accept the same body — so it is returned
    as this tiny value object carrying the already-read status and body, which
    the callers handle exactly like a drained response.
    """

    def __init__(self, status: int, data: bytes):
        self.status = status
        self._data = data

    def read(self) -> bytes:
        return self._data

    def close(self) -> None:
        pass


class Logger:
    """Timestamped file+console logger (never raises)."""

    def __init__(self, log_dir: Path, name: str = "gateway.log",
                 console: bool = True):
        self.path = Path(log_dir) / name
        self.console = console
        self._lock = threading.Lock()

    def __call__(self, message: str, *, console: Optional[bool] = None) -> None:
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
        if self.console if console is None else console:
            print(line, flush=True)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception:                               # noqa: BLE001
            pass


class Gateway:
    """Holds the configuration, the account pool and the model catalogue."""

    def __init__(self, cfg, log: Optional[Logger] = None):
        self.cfg = cfg
        self.log = log or Logger(cfg.path("log_dir", "logs"),
                                 console=bool(cfg.get("log_console", True)))
        self.pool = AccountPool(cfg, logger=self.log)
        self.models_client = ModelGateway(cfg)
        self.panel = Panel(self)
        self.models: list = []
        self._models_lock = threading.Lock()
        # Rolling call records for the mobile "记录" tab.  Kept in memory: the
        # gateway is a long-lived process and this is diagnostics, not an audit
        # log — the file log remains the durable record.
        self.calls: list = []
        self._calls_lock = threading.Lock()
        self.totals: Dict[str, Any] = {"requests": 0, "tokens": 0,
                                       "prompt_tokens": 0, "completion_tokens": 0,
                                       "points": 0, "failures": 0}

    # -- call records ----------------------------------------------------

    def record_call(self, *, account: str, model: str, status: int,
                    seconds: float, usage: Optional[Dict[str, Any]] = None,
                    kind: str = "chat", stream: bool = False,
                    error: str = "", loginid: str = "") -> None:
        """Append one request to the rolling record list (bounded, thread-safe)."""
        usage = usage or {}
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        points = int(usage.get("points_consumed") or 0)
        entry = {
            "ts": int(time.time()),
            "account": account,
            "loginid": loginid,
            "model": model,
            "status": status,
            "ok": 200 <= status < 300,
            "kind": kind,
            "stream": bool(stream),
            "seconds": round(float(seconds), 2),
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "tokens": prompt + completion,
            "points": points,
            "error": (error or "")[:300],
        }
        with self._calls_lock:
            self.calls.append(entry)
            if len(self.calls) > 2000:                 # keep the newest 2000
                del self.calls[:-2000]
            self.totals["requests"] += 1
            self.totals["prompt_tokens"] += prompt
            self.totals["completion_tokens"] += completion
            self.totals["tokens"] += prompt + completion
            self.totals["points"] += points
            if not entry["ok"]:
                self.totals["failures"] += 1

    def call_records(self, limit: int = 100, *, status: str = "",
                     account: str = "") -> Dict[str, Any]:
        with self._calls_lock:
            rows = list(self.calls)
        if status == "ok":
            rows = [r for r in rows if r["ok"]]
        elif status == "fail":
            rows = [r for r in rows if not r["ok"]]
        if account:
            rows = [r for r in rows if r["account"] == account
                    or r["loginid"] == account]
        rows.reverse()                                # newest first
        return {"ok": True, "total": len(rows),
                "rows": rows[:max(1, min(int(limit or 100), 2000))],
                "totals": dict(self.totals)}

    # -- model catalogue -------------------------------------------------

    def refresh_models(self) -> list:
        try:
            acc = self.pool.acquire()
            payload = self.models_client.models(acc.session)
            data = payload.get("data") or []
            with self._models_lock:
                self.models = data
            self.log(f"上游模型列表已刷新：{len(data)} 个")
        except Exception as exc:                        # noqa: BLE001
            with self._models_lock:
                have = bool(self.models)
            if not have:
                self.models = [dict(m, object="model", owned_by="loomy")
                               for m in C.FALLBACK_MODELS]
                self.log(f"[warn] 拉取模型列表失败，使用内置清单：{exc}")
        return self.models

    def catalogue(self) -> list:
        with self._models_lock:
            if not self.models:
                self.models = [dict(m, object="model", owned_by="loomy")
                               for m in C.FALLBACK_MODELS]
            return list(self.models)

    def resolve_model(self, requested: str) -> str:
        """Model ids are used **verbatim** — there is no alias table.

        Only the provider prefix is stripped (``imodel/deepseek-…`` →
        ``deepseek-…``), because several clients insist on writing it. Note that
        the upstream itself falls back to its default model when it does not
        recognise an id (measured: ``gpt-4o-mini`` → ``deepseek-v4-flash-0731``,
        HTTP 200), so a typo degrades instead of erroring — always check the
        ``model`` field of the response.
        """
        name = str(requested or "").strip()
        if self.cfg.get("strip_model_prefix", True) and "/" in name:
            name = name.split("/", 1)[1].strip()
        return name or str(self.cfg.get("default_model") or C.DEFAULT_MODEL)

    # -- background ------------------------------------------------------

    def start(self) -> None:
        self.pool.bootstrap()
        self.pool.start()
        self.refresh_models()

    def start_background(self) -> None:
        """Do the slow startup work without blocking the HTTP listener.

        Logging in every expired account and fetching the model catalogue are
        network round-trips; with a large pool this used to delay binding the
        port by minutes, so the app looked stuck on "starting the gateway".
        The listener now binds first and this runs right after it. Requests
        that arrive early still work: ``pool.acquire`` renews a session on
        demand, and ``catalogue()`` falls back to the built-in model list until
        the upstream answers.
        """
        def run() -> None:
            try:
                self.start()
            except Exception as exc:                    # noqa: BLE001
                self.log(f"[warn] 后台启动失败：{exc}")

        threading.Thread(target=run, name="gateway-startup", daemon=True).start()


# ------------------------------------------------------------------ handler


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "loomy2api"
    gateway: Gateway = None                        # injected by serve()

    #: Statuses that implicate the *account* (drop its session / cool it down).
    ACCOUNT_FAULT = (401, 403, 402, 429)
    #: Statuses where retrying is pointless (the request itself is wrong).
    FATAL_REQUEST = (400, 404, 422)

    #: 403 sub-types that are *not* the account's fault — see
    #: :data:`loomy2api.constants.CONTENT_FAULT_MARKERS`.
    CONTENT_FAULT_MARKERS = C.CONTENT_FAULT_MARKERS

    @classmethod
    def _content_fault(cls, status: int, data: bytes) -> bool:
        """True when a 403 came from content inspection, not authentication."""
        if status != 403:
            return False
        body = data[:500].decode("utf-8", "replace")
        return any(marker in body for marker in cls.CONTENT_FAULT_MARKERS)

    def setup(self):                               # noqa: D102
        super().setup()
        self._body_read = False

    def handle_one_request(self):                  # noqa: D102
        # ``setup()`` runs once per *connection*, not per request, and HTTP/1.1
        # keep-alive reuses the connection.  Without this reset the second
        # request on a reused socket saw ``_body_read`` still True, read an
        # empty body, and answered "no such account: (empty)" — the classic
        # "first click fails, second one works" (the error closes the socket,
        # so the retry lands on a fresh connection).
        self._body_read = False
        super().handle_one_request()

    # ------------------------------------------------------------- basics

    def log_message(self, fmt, *args):             # silence default access log
        return

    def _clean_sse_block(self, block: bytes) -> Optional[bytes]:
        """Validate one upstream SSE event; return it, or ``None`` to drop it.

        The gateway passes model output through verbatim, but the upstream
        sometimes injects a fragment that is not valid JSON.  Forwarding it
        corrupts the client's stream — notably tool arguments, which the client
        reassembles from ``input_json_delta`` pieces.  Comment/blank-only blocks
        and the terminal ``[DONE]`` are always forwarded untouched.
        """
        lines = block.split(b"\n")
        data_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith(b"data:"):
                data_lines.append(stripped[5:].strip())
        if not data_lines:
            return block                      # keepalive / comment-only event
        for payload in data_lines:
            if not payload or payload == b"[DONE]":
                continue
            try:
                json.loads(payload)
            except Exception:                 # noqa: BLE001
                return None
        return block

    def _read_chunk(self, conn, resp, idle_seconds: float, *,
                    thinking_seconds: float = 0.0,
                    saw_output: bool = True,
                    content_started: float = 0.0) -> bytes:
        """Read one chunk, aborting if the upstream goes quiet too long.

        ``http.client`` cannot resume a read after a socket timeout, so the
        idle budget is applied as the socket timeout itself rather than by
        polling: a timeout means the upstream really went silent, and the
        caller abandons the connection (retrying when nothing was sent yet).

        ``thinking_seconds`` bounds how long a reasoning-only stream may run.
        A looping model keeps sending reasoning deltas, so this is checked
        between reads, not via the socket timeout.

        ``read1`` rather than ``read``: ``HTTPResponse.read(n)`` is backed by a
        ``BufferedReader`` and blocks until *n* bytes arrive (or EOF), so a
        model that emits a token at a time would reach the client in 4 KiB
        bursts — the "whole sentence appears at once" effect.  ``read1``
        returns as soon as any data is available, preserving the upstream's
        token-by-token cadence.
        """
        start = time.time()
        if (not saw_output and thinking_seconds and content_started
                and time.time() - content_started > thinking_seconds):
            raise StreamStalled(time.time() - content_started, "thinking-only")
        if conn is not None and getattr(conn, "sock", None) is not None:
            conn.sock.settimeout(idle_seconds)
        try:
            read1 = getattr(resp, "read1", None)
            if read1 is not None:
                return read1(4096)
            return resp.read(4096)
        except socket.timeout as exc:
            raise StreamStalled(idle_seconds, "idle") from exc
        except (OSError, ValueError) as exc:
            raise StreamStalled(time.time() - start, f"io:{exc}") from exc

    def _json(self, status: int, obj: Any, extra: Optional[dict] = None) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        for key, value in (extra or {}).items():
            self.send_header(key, str(value))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status: int, message: str, kind: str = "invalid_request_error") -> None:
        # Drain any unread request body and close the connection: on Windows an
        # abort (RST) rather than a clean error surfaces otherwise when we
        # reject before reading the payload (e.g. a 401 from the API-key gate).
        self._drain()
        self.close_connection = True
        self._json(status, {"error": {"message": message, "type": kind, "code": status}})

    def _drain(self) -> None:
        if self._body_read:
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        self._body_read = True
        if length:
            try:
                self.rfile.read(length)
            except Exception:                           # noqa: BLE001
                pass

    def _query(self, name: str, default: str = "") -> str:
        from urllib.parse import parse_qs, urlparse as _urlparse
        values = parse_qs(_urlparse(self.path).query).get(name) or []
        return values[0] if values else default

    def _query_int(self, name: str, default: int) -> int:
        from urllib.parse import parse_qs, urlparse as _urlparse
        values = parse_qs(_urlparse(self.path).query).get(name) or []
        try:
            return int(values[0])
        except (IndexError, ValueError):
            return default

    def _read_body(self) -> bytes:
        if self._body_read:
            return b""
        self._body_read = True
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        return self.rfile.read(length) if length else b""

    def _read_json(self) -> Dict[str, Any]:
        raw = self._read_body()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as exc:                        # noqa: BLE001
            raise ValueError(f"请求体不是合法 JSON / body is not valid JSON: {exc}") from exc

    def _path(self) -> str:
        """Normalise the path, keeping the non-``/v1`` surfaces intact.

        Clients are sloppy about the prefix, so ``/chat/completions`` is
        rewritten to ``/v1/chat/completions``; the panel, its API and the
        health/admin endpoints are exempt.
        """
        path = self.path.split("?", 1)[0]
        while "/v1/v1" in path:
            path = path.replace("/v1/v1", "/v1")
        if path in ("", "/", "/index.html"):
            return "/panel"                       # the panel is the landing page
        exempt = ("/panel", "/api/", "/health", "/admin", "/favicon")
        if not path.startswith(exempt):
            if path != "/v1" and not path.startswith("/v1/"):
                path = "/v1" + path
        return path.rstrip("/") or "/"

    # --------------------------------------------------------------- auth

    def _presented_key(self) -> Tuple[str, str]:
        """Pull the caller's key out of whatever shape they sent it in.

        Real clients are all over the place: ``Authorization: Bearer k``,
        bare ``Authorization: k``, ``x-api-key``, or a query parameter — and
        some paste a key with stray whitespace/quotes. Accept all of them so a
        client is never rejected for cosmetics.
        """
        from urllib.parse import parse_qs, urlparse as _urlparse

        raw = (self.headers.get("Authorization") or "").strip()
        if raw:
            if raw.lower().startswith("bearer "):
                return raw[7:].strip(), "authorization:bearer"
            if raw.lower().startswith("token "):
                return raw[6:].strip(), "authorization:token"
            return raw, "authorization:raw"
        for header in ("x-api-key", "api-key", "apikey", "x-goog-api-key"):
            value = (self.headers.get(header) or "").strip()
            if value:
                return value, f"header:{header}"
        query = parse_qs(_urlparse(self.path).query)
        for name in ("api_key", "apikey", "key", "access_token"):
            if query.get(name):
                return query[name][0].strip(), f"query:{name}"
        return "", "none"

    @staticmethod
    def _mask(value: str) -> str:
        if not value:
            return "(empty)"
        if len(value) <= 12:
            return value[:4] + "…"
        return f"{value[:8]}…{value[-4:]}(len={len(value)})"

    def _is_loopback(self) -> bool:
        """True when the request came from this machine (the phone itself)."""
        try:
            host = str(self.client_address[0])
        except Exception:                               # noqa: BLE001
            return False
        return host in ("127.0.0.1", "::1", "localhost")

    def _authorized(self) -> bool:
        keys = self.gateway.cfg.api_keys
        if not keys:
            return True
        # The config endpoint must stay reachable from loopback: it is the only
        # way to clear or replace a key the user no longer remembers.  Without
        # this exemption a wrong key locks the UI out permanently — the panel
        # cannot even read state to show which key is configured.  Loopback
        # requests cannot come from another device, so this does not weaken the
        # gate for remote callers.
        if self.command == "POST" and self._path() == "/api/panel/config" and self._is_loopback():
            return True
        token, source = self._presented_key()
        token = token.strip().strip('"').strip("'")
        if token and token in keys:
            return True
        # never log the secret itself — just enough to see what arrived
        self.gateway.log(f"[auth] 拒绝 {self.command} {self.path} ← {source} "
                         f"token={self._mask(token)}")
        self._error(401, "无效的 API Key / invalid API key", "authentication_error")
        return False

    # ------------------------------------------------------------ routing

    def do_OPTIONS(self):                             # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):                                # noqa: N802
        self.do_GET()

    def do_GET(self):                                 # noqa: N802
        path = self._path()
        if path in ("/v1/health", "/health"):
            return self._health()
        if path in ("/panel", "/", "/index.html"):
            return self._panel_page()
        if not self._authorized():
            return
        try:
            if path == "/v1/models":
                return self._models()
            if path == "/v1/points":
                return self._points()
            if path in ("/v1/admin/accounts", "/admin/accounts"):
                return self._accounts()
            if path == "/api/panel/state":
                refresh = "refresh=1" in (self.path or "")
                # The mobile UI asks for cached data (`async=1`): with a large
                # pool, waiting for every quota round-trip made the home screen
                # look stuck. An explicit refresh still waits for fresh values.
                wait = "async=1" not in (self.path or "")
                return self._json(200, self.gateway.panel.state(
                    refresh=refresh, wait=wait))
            if path == "/api/panel/logs":
                lines = self._query_int("lines", 200)
                return self._json(200, self.gateway.panel.logs(lines))
            if path == "/api/panel/models":
                refresh = "refresh=1" in (self.path or "")
                return self._json(200, self.gateway.panel.models({"refresh": refresh}))
            if path == "/api/panel/points":
                name = self._query("name")
                return self._json(200, self.gateway.panel.points({"name": name}))
            if path == "/api/panel/records":
                return self._json(200, self.gateway.call_records(
                    limit=self._query_int("limit", 100),
                    status=self._query("status"),
                    account=self._query("account")))
            if path == "/api/panel/export":
                return self._json(200, self.gateway.panel.export_accounts())
            if path == "/favicon.ico":
                return self._error(404, "no favicon")
            return self._error(404, f"未知路径 / unknown path: {self.path}")
        except PoolError as exc:
            return self._error(503, str(exc), "account_pool_error")
        except Exception as exc:                        # noqa: BLE001
            self.gateway.log(f"[error] GET {self.path}: {exc}\n{traceback.format_exc()}")
            return self._error(502, f"上游调用失败 / upstream failure: {exc}", "upstream_error")

    def do_POST(self):                                # noqa: N802
        path = self._path()
        if not self._authorized():
            return
        try:
            if path == "/v1/chat/completions":
                return self._chat()
            if path == "/v1/messages":
                return self._messages()
            if path == "/v1/embeddings":
                return self._passthrough("embeddings")
            if path == "/v1/images/generations":
                return self._passthrough("images/generations")
            if path in ("/v1/admin/accounts/reload", "/admin/accounts/reload"):
                self.gateway.pool.load()
                self.gateway.refresh_models()
                return self._json(200, self.gateway.pool.snapshot())
            if path.startswith("/api/panel/"):
                return self._panel_api(path)
            return self._error(404, f"未知路径 / unknown path: {self.path}")
        except ValueError as exc:
            return self._error(400, str(exc))
        except PoolError as exc:
            return self._error(400, str(exc), "account_pool_error")
        except Exception as exc:                        # noqa: BLE001
            self.gateway.log(f"[error] POST {self.path}: {exc}\n{traceback.format_exc()}")
            try:
                return self._error(502, f"上游调用失败 / upstream failure: {exc}",
                                   "upstream_error")
            except Exception:                           # noqa: BLE001
                return

    # ------------------------------------------------------------- panel

    def _panel_page(self) -> None:
        body = self.gateway.panel.html()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _panel_api(self, path: str) -> None:
        payload = self._read_json()
        panel = self.gateway.panel
        handlers = {
            "/api/panel/points/activate": panel.points_activate,
            "/api/panel/points/onboarding": panel.onboarding,
            "/api/panel/points/onboarding-state": panel.onboarding_state,
            "/api/panel/points/checkin": panel.checkin,
            "/api/panel/accounts/invite-auto": panel.invite_auto,
            "/api/panel/accounts": panel.add_account,
            "/api/panel/accounts/import": panel.import_accounts,
            "/api/panel/accounts/sms": panel.sms_send,
            "/api/panel/accounts/sms-login": panel.sms_login,
            "/api/panel/accounts/update": panel.update_account,
            "/api/panel/accounts/password": panel.set_password,
            "/api/panel/accounts/password/show": panel.show_password,
            "/api/panel/accounts/passwords": panel.set_passwords,
            "/api/panel/accounts/register-sms": panel.register_sms,
            "/api/panel/fysms/balance": panel.fysms_balance,
            "/api/panel/accounts/remove": panel.remove_account,
            "/api/panel/accounts/renew": panel.renew_account,
            "/api/panel/accounts/check": panel.check_accounts,
            "/api/panel/accounts/identity": panel.identity,
            "/api/panel/refresh": lambda _p: panel.refresh(_p),
            "/api/panel/config": panel.update_config,
            "/api/panel/export": lambda _p: panel.export_accounts(),
        }
        handler = handlers.get(path)
        if handler is None:
            return self._error(404, f"未知面板接口 / unknown panel endpoint: {path}")
        result = handler(payload)
        if not isinstance(result, dict):
            result = {"ok": True, "result": result}
        return self._json(200, result)

    # ------------------------------------------------------ simple routes

    def _health(self) -> None:
        gw = self.gateway
        accounts = gw.pool.accounts
        return self._json(200, {
            "status": "ok" if gw.pool.usable() else "degraded",
            "name": "loomy2api",
            "upstream": gw.cfg["upstream"],
            "accounts": len(accounts),
            "usable_accounts": len(gw.pool.usable()),
            "models": len(gw.catalogue()),
            "auth_required": bool(gw.cfg.api_keys),
            "strategy": gw.cfg.get("strategy"),
            "panel": f"http://{gw.cfg['host']}:{gw.cfg['port']}/panel",
        })

    def _models(self) -> None:
        gw = self.gateway
        catalogue = gw.catalogue()
        if not catalogue:
            gw.refresh_models()
            catalogue = gw.catalogue()
        return self._json(200, {"object": "list", "data": list(catalogue)})

    def _points(self) -> None:
        gw = self.gateway
        out = []
        for acc in gw.pool.accounts:
            gw.pool.refresh_quota(acc)
            out.append({
                "name": acc.name,
                "userid": acc.userid,
                "available": acc.available,
                "balance": acc.balance,
                "daily_balance": acc.daily_balance,
                "requests": acc.requests,
                "points_used": acc.points_used,
            })
        gw.pool.save()
        # the same account can appear twice (own session + imported client
        # session) — count its quota only once
        seen: Dict[str, int] = {}
        for item in out:
            key = item["userid"] or item["name"]
            if isinstance(item["available"], int):
                seen[key] = max(seen.get(key, 0), item["available"])
        return self._json(200, {
            "total_available": sum(seen.values()),
            "unique_accounts": len(seen),
            "accounts": out,
        })

    def _accounts(self) -> None:
        return self._json(200, self.gateway.pool.snapshot())

    # ------------------------------------------------------- passthroughs

    def _passthrough(self, suffix: str) -> None:
        gw = self.gateway
        raw = self._read_body()
        acc = gw.pool.acquire()
        status, headers, data = gw.models_client.call(
            acc.session, suffix, json.loads(raw.decode("utf-8")) if raw else {})
        if status >= 400 and not self._content_fault(status, data):
            gw.pool.report_failure(acc, status,
                                   data[:200].decode("utf-8", "replace"))
        elif status < 400:
            gw.pool.report_success(acc)
        self.send_response(status)
        self.send_header("Content-Type",
                         headers.get("Content-Type", "application/json"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---------------------------------------------------------- chat (core)

    def _chat(self) -> None:
        gw = self.gateway
        req = self._read_json()
        model = gw.resolve_model(req.get("model"))
        stream = bool(req.get("stream"))

        # an OpenAI-shaped caller (health probes, protocol bridges) sometimes
        # sends no messages at all; the upstream rejects an empty list outright,
        # so substitute a harmless placeholder turn instead of failing.
        if not req.get("messages"):
            keys = ",".join(sorted(req.keys())) or "-"
            gw.log(f"[warn] /v1/chat/completions 入站 messages 为空/缺失"
                   f"（model={model} stream={stream} keys=[{keys}] ua="
                   f"{(self.headers.get('User-Agent') or '')[:60]}），已补占位 user 消息")
            req["messages"] = [{"role": "user", "content": ""}]

        extra: Dict[str, str] = {}
        for src, dst in (("chat_id", "ChatId"), ("msg_id", "MsgId"),
                         ("turn_id", "TurnId")):
            value = req.pop(src, None)
            if value:
                extra[dst] = value
        purpose = req.pop("request_purpose", None)
        if purpose:
            extra["X-Loomy-Request-Purpose"] = purpose
        req["model"] = model

        if stream:
            return self._chat_stream(req, extra)

        tried: list = []
        last: Tuple[int, bytes] = (0, b"")
        for _ in range(int(gw.cfg.get("max_retries") or 0) + 1):
            try:
                acc = gw.pool.acquire(exclude=tried)
            except PoolError as exc:
                gw.log(f"[warn] {exc}")
                break
            tried.append(acc.name)
            started = time.time()
            status, _hdrs, data = gw.models_client.call(
                acc.session, "chat/completions", req, extra)
            if status == 200:
                try:
                    obj = json.loads(data.decode("utf-8"))
                except Exception:                       # noqa: BLE001
                    obj = None
                usage = (obj or {}).get("usage") or {}
                gw.pool.report_success(acc, usage)
                elapsed = time.time() - started
                gw.record_call(account=acc.name, loginid=acc.loginid, model=model,
                               status=200, seconds=elapsed, usage=usage,
                               kind="chat", stream=False)
                gw.log(f"[call] {acc.name} {model} {elapsed:.1f}s "
                       f"http=200 {human_usage(usage)}")
                return self._json(200, obj)
            last = (status, data)
            gw.record_call(account=acc.name, loginid=acc.loginid, model=model,
                           status=status, seconds=time.time() - started,
                           kind="chat", stream=False,
                           error=data[:300].decode("utf-8", "replace"))
            if self._content_fault(status, data):
                # the request body was rejected by content inspection: the
                # account and its session are fine, and every other account
                # would reject the identical payload too — so stop here.
                gw.log(f"[call] {acc.name} {model} http={status} → 内容审核拒绝"
                       f"（账号无问题，不冷却、不换账号）："
                       f"{data[:200].decode('utf-8', 'replace')}")
                break
            if status in self.ACCOUNT_FAULT:
                gw.log(f"[call] {acc.name} {model} http={status} → 账号问题，换账号重试")
                gw.pool.report_failure(acc, status,
                                       data[:200].decode("utf-8", "replace"))
            else:
                # upstream hiccup (502/504/500) — do NOT punish the account for it
                gw.log(f"[call] {acc.name} {model} http={status} → 上游异常，换账号重试"
                       f"（不冷却该账号）")
            if status in self.FATAL_REQUEST:            # not an account problem
                break
        status, data = last
        try:
            return self._json(status or 502, json.loads(data.decode("utf-8")))
        except Exception:                               # noqa: BLE001
            return self._error(status or 502, data[:500].decode("utf-8", "replace"),
                               "upstream_error")

    def _open_stream(self, req: Dict[str, Any], extra: Optional[Dict[str, str]],
                     *, log_tag: str) -> Tuple[Optional[Any], Optional[Any], Optional[Any]]:
        """Acquire an account and open a 200 stream, retrying other accounts.

        Returns ``(acc, conn, resp)``; ``acc`` is ``None`` when every attempt
        failed (``resp`` then carries the last error response, if any).
        """
        gw = self.gateway
        model = req.get("model")
        tried: list = []
        conn = resp = None

        for _ in range(int(gw.cfg.get("max_retries") or 0) + 1):
            try:
                acc = gw.pool.acquire(exclude=tried)
            except PoolError as exc:
                gw.log(f"[warn] {exc}")
                return None, None, resp
            tried.append(acc.name)
            conn, resp = gw.models_client.stream(acc.session, "chat/completions",
                                                 req, extra)
            if resp.status == 200:
                return acc, conn, resp
            data = resp.read()
            conn.close()
            if self._content_fault(resp.status, data):
                gw.log(f"[{log_tag}] {acc.name} {model} stream http={resp.status}"
                       f" → 内容审核拒绝（账号无问题，不冷却、不换账号）："
                       f"{data[:200].decode('utf-8', 'replace')}")
                return None, None, _ContentRejected(resp.status, data)
            if resp.status in self.ACCOUNT_FAULT:
                gw.log(f"[{log_tag}] {acc.name} {model} stream http={resp.status}"
                       f" → 账号问题，换账号重试")
                gw.pool.report_failure(acc, resp.status,
                                       data[:200].decode("utf-8", "replace"))
            else:
                gw.log(f"[{log_tag}] {acc.name} {model} stream http={resp.status}"
                       f" → 上游异常，换账号重试（不冷却该账号）")
            conn = resp = None
        return None, None, resp

    def _chat_stream(self, req: Dict[str, Any], extra: Dict[str, str]) -> None:
        gw = self.gateway
        model = req.get("model")

        # --- retry loop: while the client has received nothing yet, a stalled
        #     or truncated upstream stream can be retried transparently. -----
        buffer = b""
        usage: Dict[str, Any] = {}
        dropped = 0
        attempts = int(gw.cfg.get("stream_retries") or 0) + 1
        stall_seconds = float(gw.cfg.get("stream_stall_seconds") or 180)
        think_seconds = float(gw.cfg.get("thinking_stall_seconds") or 240)
        disable_thinking = bool(req.pop("_no_thinking", False))

        for attempt in range(attempts):
            body = dict(req)
            if disable_thinking:
                # second attempt onwards: ask the model to skip reasoning so a
                # reasoning loop cannot stall the stream again
                body["enable_thinking"] = False
                body["chat_template_kwargs"] = {"enable_thinking": False}

            acc, conn, resp = self._open_stream(body, extra, log_tag="call")
            if acc is None:
                if resp is None:
                    return self._error(502, "所有账号都失败了 / all accounts failed",
                                       "upstream_error")
                data = resp.read()
                resp.close() if hasattr(resp, "close") else None
                try:
                    return self._json(resp.status, json.loads(data.decode("utf-8")))
                except Exception:                       # noqa: BLE001
                    return self._error(resp.status,
                                       data[:500].decode("utf-8", "replace"),
                                       "upstream_error")

            started = time.time()
            first = attempt == 0
            if first:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.close_connection = True

            saw_content = False          # any text/tool/reasoning forwarded
            saw_output = False           # text or tool (not just reasoning)
            finished = False             # upstream sent finish_reason
            content_started = 0.0        # when the first content block arrived
            stalled: Optional[StreamStalled] = None
            try:
                while True:
                    chunk = self._read_chunk(conn, resp, stall_seconds,
                                             thinking_seconds=think_seconds,
                                             saw_output=saw_output,
                                             content_started=content_started)
                    if not chunk:
                        break
                    buffer += chunk
                    while b"\n\n" in buffer:
                        block, buffer = buffer.split(b"\n\n", 1)
                        out = self._clean_sse_block(block)
                        if out is None:
                            dropped += 1
                            continue
                        if not saw_content:
                            saw_content = True
                            content_started = time.time()
                        if not saw_output and self._block_has_output(out):
                            saw_output = True
                        if self._block_is_final(out):
                            finished = True
                        self._track_tool_args(out)
                        self.wfile.write(out + b"\n\n")
                        self.wfile.flush()
                        sse_usage(out.split(b"\n", 1)[0].strip(), usage)
                if buffer.strip():
                    out = self._clean_sse_block(buffer)
                    if out is not None:
                        self._track_tool_args(out)
                        self.wfile.write(out + b"\n\n")
                        self.wfile.flush()
                        sse_usage(out.split(b"\n", 1)[0].strip(), usage)
                        if not saw_content:
                            saw_content = True
                            content_started = time.time()
                        if self._block_has_output(out):
                            saw_output = True
            except StreamStalled as exc:
                stalled = exc
            except (BrokenPipeError, ConnectionResetError):
                gw.log(f"[call] {acc.name} {model} client disconnected")
                try:
                    conn.close()
                except Exception:                       # noqa: BLE001
                    pass
                return
            finally:
                try:
                    conn.close()
                except Exception:                       # noqa: BLE001
                    pass

            # --- decide: retry (nothing sent yet) or terminate the stream ---
            # A retry is only safe while the client has received nothing at all,
            # so a normal reply that happens to be reasoning-only still counts
            # as "content sent" and must never be replayed.
            can_retry = attempt + 1 < attempts and not saw_content
            if can_retry and stalled is not None:
                disable_thinking = True
                gw.log(f"[call] {acc.name} {model} 流卡住（{stalled.kind} "
                       f"{stalled.seconds:.0f}s）且未输出内容 → 第 {attempt + 2} 次重试")
                buffer = b""
                continue
            if can_retry and not finished:
                gw.log(f"[call] {acc.name} {model} 上游流未产生内容就结束 "
                       f"→ 第 {attempt + 2} 次重试")
                buffer = b""
                continue

            # cannot retry any more (or content already went out): close cleanly
            reason = ""
            if not finished:
                reason = (f"流中断（{stalled.kind}）" if stalled
                          else "上游未发送结束事件")
                args_text = self._open_tool_args()
                broken_tool = False
                if args_text:
                    try:
                        json.loads(args_text)
                    except Exception:                   # noqa: BLE001
                        broken_tool = True
                if broken_tool:
                    reason += "，tool 参数截断"
                gw.log(f"[call] {acc.name} {model} {reason}"
                       f"{'，已输出部分内容' if saw_content else ''} → 补发终止事件")
                self._terminate_sse(reason)
            gw.pool.report_success(acc, usage)
            elapsed = time.time() - started
            gw.record_call(account=acc.name, loginid=acc.loginid, model=model,
                           status=200, seconds=elapsed, usage=usage,
                           kind="chat", stream=True,
                           error="" if finished else reason)
            gw.log(f"[call] {acc.name} {model} {elapsed:.1f}s "
                   f"http=200 {human_usage(usage)} stream"
                   + (f" attempt={attempt + 1}" if attempt else "")
                   + (f" dropped={dropped}" if dropped else ""))
            return

    def _open_tool_args(self) -> str:
        """Concatenated arguments text of the currently open tool call, or "".

        The OpenAI path forwards upstream chunks verbatim; on abnormal stream
        end we need to know whether the last tool call's ``arguments`` were
        fully streamed (valid JSON) or cut mid-write.
        """
        return getattr(self, "_tool_args_buffer", "")

    def _track_tool_args(self, block: bytes) -> None:
        """Accumulate tool-call argument fragments from a forwarded SSE block.

        Remembers the text of the *latest* tool index so a stream cut can be
        judged: valid JSON → clean truncation after a tool; invalid → broken
        tool call.
        """
        for line in block.split(b"\n"):
            if not line.strip().startswith(b"data:"):
                continue
            try:
                obj = json.loads(line.strip()[5:].strip())
            except Exception:                           # noqa: BLE001
                continue
            for call in ((obj.get("choices") or [{}])[0].get("delta") or {}
                         ).get("tool_calls") or []:
                fn = call.get("function") or {}
                args = fn.get("arguments")
                if args is not None:
                    prev = getattr(self, "_tool_args_buffer", "")
                    self._tool_args_buffer = (args if call.get("id")
                                              else prev + args)
                # a new tool call (with id) resets the buffer

    def _block_is_final(self, block: bytes) -> bool:
        """True when an SSE block carries a non-null ``finish_reason``."""
        for line in block.split(b"\n"):
            if not line.strip().startswith(b"data:"):
                continue
            try:
                obj = json.loads(line.strip()[5:].strip())
            except Exception:                           # noqa: BLE001
                continue
            for choice in obj.get("choices") or []:
                if choice.get("finish_reason"):
                    return True
        return False

    def _block_has_output(self, block: bytes) -> bool:
        """True when an SSE block carries real output (text or a tool call).

        Reasoning-only deltas (``reasoning_content``) do **not** count: a model
        that never stops thinking must be treated as stalled.
        """
        if b"tool_calls" in block:
            return True
        for line in block.split(b"\n"):
            if not line.strip().startswith(b"data:"):
                continue
            try:
                obj = json.loads(line.strip()[5:].strip())
            except Exception:                           # noqa: BLE001
                continue
            if obj.get("choices"):
                delta = (obj["choices"][0].get("delta") or {})
                content = delta.get("content")
                if isinstance(content, str) and content:
                    return True
        return False

    def _terminate_sse(self, reason: str) -> None:
        """Emit a terminal event so the client does not see a silent truncation.

        ``finish_reason="error"`` (not "stop"): if the stream died mid tool
        call the client already received half-written tool arguments — a
        "stop" would make it parse that garbage; "error" tells it the
        response is broken and worth retrying.
        """
        try:
            self.wfile.write(
                b"data: " + json.dumps({
                    "choices": [{"index": 0, "delta": {},
                                 "finish_reason": "error"}],
                    "loomy2api_warning": reason,
                }, ensure_ascii=False).encode("utf-8") + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    # -------------------------------------------------- anthropic messages

    def _messages(self) -> None:
        gw = self.gateway
        req = self._read_json()
        payload = anth.anthropic_to_openai(req)
        payload["model"] = gw.resolve_model(payload.get("model"))
        model = payload["model"]
        if req.get("stream"):
            return self._messages_stream(payload, model)

        tried: list = []
        last: Tuple[int, bytes] = (0, b"")
        for _ in range(int(gw.cfg.get("max_retries") or 0) + 1):
            try:
                acc = gw.pool.acquire(exclude=tried)
            except PoolError as exc:
                gw.log(f"[warn] {exc}")
                break
            tried.append(acc.name)
            started = time.time()
            status, _h, data = gw.models_client.call(acc.session, "chat/completions", payload)
            if status == 200:
                obj = json.loads(data.decode("utf-8"))
                usage = obj.get("usage") or {}
                gw.pool.report_success(acc, usage)
                elapsed = time.time() - started
                gw.record_call(account=acc.name, loginid=acc.loginid, model=model,
                               status=200, seconds=elapsed, usage=usage,
                               kind="messages", stream=False)
                gw.log(f"[messages] {acc.name} {model} {elapsed:.1f}s "
                       f"http=200 {human_usage(usage)}")
                return self._json(200, anth.openai_to_anthropic(obj, model))
            last = (status, data)
            gw.record_call(account=acc.name, loginid=acc.loginid, model=model,
                           status=status, seconds=time.time() - started,
                           kind="messages", stream=False,
                           error=data[:300].decode("utf-8", "replace"))
            if self._content_fault(status, data):
                gw.log(f"[messages] {acc.name} {model} http={status} → 内容审核拒绝"
                       f"（账号无问题，不冷却、不换账号）："
                       f"{data[:200].decode('utf-8', 'replace')}")
                break
            if status in self.ACCOUNT_FAULT:
                gw.pool.report_failure(acc, status,
                                       data[:200].decode("utf-8", "replace"))
                gw.log(f"[messages] {acc.name} {model} http={status} → 账号问题，换账号重试")
            else:
                gw.log(f"[messages] {acc.name} {model} http={status}"
                       f" → 上游异常，换账号重试（不冷却该账号）")
            if status in self.FATAL_REQUEST:
                break
        status, data = last
        return self._json(status or 502, {
            "type": "error",
            "error": {"type": "api_error",
                      "message": data[:500].decode("utf-8", "replace")}})

    def _messages_stream(self, payload: Dict[str, Any], model: str) -> None:
        gw = self.gateway

        attempts = int(gw.cfg.get("stream_retries") or 0) + 1
        stall_seconds = float(gw.cfg.get("stream_stall_seconds") or 180)
        think_seconds = float(gw.cfg.get("thinking_stall_seconds") or 240)
        disable_thinking = bool(payload.pop("_no_thinking", False))

        def emit(event: str, data: Dict[str, Any]) -> None:
            self.wfile.write(
                f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                .encode("utf-8"))
            self.wfile.flush()

        for attempt in range(attempts):
            body = dict(payload)
            if disable_thinking:
                body["enable_thinking"] = False
                body["chat_template_kwargs"] = {"enable_thinking": False}

            acc, conn, resp = self._open_stream(body, None, log_tag="messages")
            if acc is None:
                if resp is None:
                    return self._json(502, {"type": "error", "error": {
                        "type": "api_error", "message": "all accounts failed"}})
                data = resp.read()
                resp.close() if hasattr(resp, "close") else None
                return self._json(resp.status, {"type": "error", "error": {
                    "type": "api_error",
                    "message": data[:500].decode("utf-8", "replace")}})

            translator = anth.StreamTranslator(model)
            started = time.time()
            if attempt == 0:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.close_connection = True

            buffer = b""
            saw_content = False
            saw_output = False
            finished = False
            content_started = 0.0
            stalled: Optional[StreamStalled] = None
            try:
                for event, data in translator.start():
                    emit(event, data)
                while True:
                    chunk = self._read_chunk(conn, resp, stall_seconds,
                                             thinking_seconds=think_seconds,
                                             saw_output=saw_output,
                                             content_started=content_started)
                    if not chunk:
                        break
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        line = line.strip()
                        if not line.startswith(b"data:"):
                            continue
                        text = line[5:].strip()
                        if not text or text == b"[DONE]":
                            continue
                        try:
                            obj = json.loads(text)
                        except Exception:               # noqa: BLE001
                            continue
                        choice = (obj.get("choices") or [{}])[0]
                        delta = choice.get("delta") or {}
                        if not saw_output and (delta.get("content")
                                               or delta.get("tool_calls")):
                            saw_output = True
                        if choice.get("finish_reason"):
                            finished = True
                        events = list(translator.feed(obj))
                        for event, data in events:
                            if event != "ping":
                                if not saw_content:
                                    content_started = time.time()
                                saw_content = True
                            emit(event, data)
                for event, data in translator.finish():
                    emit(event, data)
            except StreamStalled as exc:
                stalled = exc
            except (BrokenPipeError, ConnectionResetError):
                gw.log(f"[messages] {acc.name} {model} client disconnected")
                try:
                    conn.close()
                except Exception:                       # noqa: BLE001
                    pass
                return
            finally:
                try:
                    conn.close()
                except Exception:                       # noqa: BLE001
                    pass

            # only safe to replay while the client has received nothing at all
            retryable = not saw_content and attempt + 1 < attempts
            if retryable and stalled is not None:
                disable_thinking = True
                gw.log(f"[messages] {acc.name} {model} 流卡住（{stalled.kind} "
                       f"{stalled.seconds:.0f}s）且无输出 → 第 {attempt + 2} 次重试")
                continue
            if retryable and stalled is None:
                gw.log(f"[messages] {acc.name} {model} 上游流无输出即结束 "
                       f"→ 第 {attempt + 2} 次重试")
                continue

            if stalled is not None:
                # content already reached the client: end the stream properly.
                # A half-written tool_use block must not be reported as a
                # clean end_turn — the client would try to execute a tool
                # call whose arguments are truncated JSON.
                broken = translator.truncated_tool()
                gw.log(f"[messages] {acc.name} {model} 流中断（{stalled.kind} "
                       f"{stalled.seconds:.0f}s）"
                       f"{'，tool 参数截断' if broken else ''} → 补发终止事件")
                self._terminate_anthropic("upstream stream interrupted",
                                          broken_tool=broken)
            gw.pool.report_success(acc, translator.usage)
            elapsed = time.time() - started
            gw.record_call(account=acc.name, loginid=acc.loginid, model=model,
                           status=200, seconds=elapsed, usage=translator.usage,
                           kind="messages", stream=True)
            gw.log(f"[messages] {acc.name} {model} {elapsed:.1f}s "
                   f"http=200 {human_usage(translator.usage)} stream"
                   + (f" attempt={attempt + 1}" if attempt else ""))
            return

    def _terminate_anthropic(self, reason: str, *,
                             broken_tool: bool = False) -> None:
        """Close an Anthropic SSE stream after a mid-stream failure.

        When ``broken_tool`` is set the last tool_use block holds truncated
        arguments — emit ``error`` as stop_reason so the client treats the
        turn as failed (and may retry) instead of parsing the garbage.
        """
        stop = "error" if broken_tool else "end_turn"
        try:
            self.wfile.write(
                f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'type': 'api_error', 'message': reason}}, ensure_ascii=False)}\n\n"
                .encode("utf-8"))
            self.wfile.write(
                f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': stop, 'stop_sequence': None}, 'usage': {'output_tokens': 0}}, ensure_ascii=False)}\n\n"
                .encode("utf-8"))
            self.wfile.write(b"event: message_stop\ndata: {\"type\": \"message_stop\"}\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # Windows lets two processes bind the same port with SO_REUSEADDR, which
    # silently sends requests to an old instance.  Only enable it elsewhere.
    allow_reuse_address = (os.name != "nt")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        #: Live client sockets.  ``server_close()`` only closes the listening
        #: socket; an HTTP/1.1 keep-alive connection would keep being served
        #: by the *old* handler thread (and its old Gateway), so a rebind left
        #: clients reading stale state over a socket that never reconnected.
        self._active = set()
        self._active_lock = threading.Lock()

    def process_request(self, request, client_address):
        with self._active_lock:
            self._active.add(request)
        try:
            super().process_request(request, client_address)
        except Exception:
            with self._active_lock:
                self._active.discard(request)
            raise

    def shutdown_request(self, request):
        with self._active_lock:
            self._active.discard(request)
        super().shutdown_request(request)

    def server_close(self):
        super().server_close()
        with self._active_lock:
            active, self._active = list(self._active), set()
        for conn in active:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except Exception:                        # noqa: BLE001
                pass
            try:
                conn.close()
            except Exception:                        # noqa: BLE001
                pass


def serve(cfg, *, gateway: Optional[Gateway] = None,
          on_server=None) -> Gateway:
    """Run the HTTP server until interrupted.

    ``on_server`` receives the bound server before it starts serving, so an
    embedder (the Android app) can shut it down to rebind on a different
    interface without leaving the old socket in place.
    """
    gw = gateway or Gateway(cfg)

    handler = type("BoundHandler", (Handler,), {"gateway": gw})
    httpd = Server((str(cfg["host"]), int(cfg["port"])), handler)
    if on_server is not None:
        on_server(httpd)
    # Bind first, then log in expired accounts / fetch the catalogue in the
    # background: with a large pool the old synchronous startup kept the port
    # closed for minutes (the app looked stuck on "starting the gateway").
    gw.start_background()
    gw.log(f"loomy2api 已启动 / listening → http://{cfg['host']}:{cfg['port']}"
           f"  (OpenAI: /v1/chat/completions · Anthropic: /v1/messages)"
           f"  账号 {len(gw.pool.accounts)} 个，可用 {len(gw.pool.usable())} 个")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        gw.log("收到中断，退出 / interrupted, shutting down")
    finally:
        gw.pool.stop()
        gw.panel.shutdown()
        gw.pool.flush()
        httpd.server_close()
    return gw
