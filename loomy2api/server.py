"""HTTP gateway: OpenAI-compatible + Anthropic-compatible endpoints."""

from __future__ import annotations

import errno
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
from .model_events import is_free
from . import anthropic as anth
from .panel import Panel
from .pool import AccountPool, PoolError
from .upstream import (ModelGateway, business_error_status, human_usage,
                       sse_usage)

__all__ = ["AlreadyRunning", "Gateway", "InstanceLock", "serve"]


class StreamStalled(Exception):
    """The upstream stopped sending data for longer than we tolerate."""

    def __init__(self, seconds: float, kind: str = "idle"):
        super().__init__(f"upstream stream stalled for {seconds:.0f}s ({kind})")
        self.seconds = seconds
        self.kind = kind


def _stream_socket(resp, conn=None):
    """The live socket behind a streaming response, or ``None``.

    ``http.client`` hands the socket over to the response once
    ``getresponse()`` has run, so ``conn.sock`` is already ``None`` by the time
    the stream is read — setting a timeout there silently did nothing, which
    made the "upstream went quiet" guard unreachable.  The socket lives on
    ``resp.fp.raw._sock``.
    """
    sock = getattr(conn, "sock", None)
    if sock is not None:
        return sock
    raw = getattr(getattr(resp, "fp", None), "raw", None)
    return getattr(raw, "_sock", None)


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
        #: True once ``self.models`` holds a real upstream union.  Until then
        #: ``catalogue()`` answers the built-in FALLBACK list, which is a
        #: *guess* — the panel must not diff accounts against it, or every
        #: account looks downgraded on the first page load after a restart.
        self._models_confirmed = False
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
        """Union every account's catalogue into the advertised model list.

        The upstream scopes ``/models`` **per account**: an account without an
        entitlement simply does not see that id, and asking for it anyway gets
        HTTP 200 with a *substituted* default model.  Probing a single account
        (as this used to) therefore advertised whatever that one account had —
        in practice one account that had been downgraded to a free model hid
        every paid model the rest of the pool could still serve.

        Side effect: each account's own ids are recorded on ``Account.models``,
        which is what routing filters on (``Pool.serves_model``).
        """
        try:
            result = self.pool.probe_models()
            # Startup also counts as a probe: without this the keeper would
            # fan out over the whole pool again minutes later.
            self.pool._last_model_probe = time.time()
            union = result["models"]
            data = sorted(union.values(), key=lambda m: str(m.get("id") or ""))
            with self._models_lock:
                if data:
                    self.models = data
                    self._models_confirmed = True
            counts = self.pool.model_stats()
            self.log(f"上游模型列表已刷新：{len(data)} 个"
                     f"（探测 {result['probed']}/{result['probed'] + len(result['failed'])} 个账号，"
                     f"权限分布 {counts}）")
        except Exception as exc:                        # noqa: BLE001
            with self._models_lock:
                have = bool(self.models)
            if not have:
                self.models = [dict(m, object="model", owned_by="loomy")
                               for m in C.FALLBACK_MODELS]
                self.log(f"[warn] 拉取模型列表失败，使用内置清单：{exc}")
        return self.models

    @property
    def models_confirmed(self) -> bool:
        """True once the advertised catalogue came from the upstream."""
        with self._models_lock:
            return self._models_confirmed

    def catalogue(self) -> list:
        with self._models_lock:
            if not self.models:
                self.models = [dict(m, object="model", owned_by="loomy")
                               for m in C.FALLBACK_MODELS]
            return list(self.models)

    def note_model_substitution(self, acc, requested: str, served: str) -> None:
        """Handle the upstream answering a different model than was asked.

        Its ``/models`` list is per account and it *substitutes* the default
        (HTTP 200, no error) when the account lacks the requested id.  Treat
        that as proof this account's cached catalogue is stale: drop the id so
        routing stops sending it here, and leave a log line — otherwise the
        degradation is invisible from the outside.
        """
        served = str(served or "")
        if not served or not requested or served == requested:
            return
        if is_free(requested):
            # The free tier is renamed/split constantly (``spark-x2.6`` →
            # ``spark-x2.6-low``).  Recording those as downgrades would fill
            # the history with noise and bury the real signal.
            return
        with acc.models_lock:
            if acc.models is None or requested not in acc.models:
                return
            new = tuple(m for m in acc.models if m != requested)
            # This is the strongest evidence available: a real request came
            # back with a different model.  Record it as a downgrade so the
            # history shows the actual moment the entitlement was lost, not
            # just whatever a later /models probe happens to report.
            self.pool.model_events.record(acc.name, "downgrade", acc.models, new)
            acc.models = new
            self.log(f"[warn] {acc.name} 请求 {requested} 但上游返回 {served}"
                     f"（静默降级；已从该账号的模型清单移除 {requested}）")

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
    #: Statuses that are the upstream's *own* fault, not the account's.  They
    #: arrive in short bursts, so a retry should pause (back off) rather than
    #: immediately pick another account and land in the same bad window.
    UPSTREAM_FAULT = (500, 502, 503, 504)

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

    def _upstream_backoff(self, attempt: int) -> float:
        """Pause before retrying an *upstream* fault (500/502/503/504).

        The upstream's own gateway returns these in short bursts that clear on
        their own, and a different account does not dodge them, so waiting is
        the only thing that helps.  The delay doubles per attempt:
        ``base * 2**attempt`` capped at ``upstream_fault_backoff_max``.
        """
        base = float(self.gateway.cfg.get("upstream_fault_backoff") or 0)
        if base <= 0:
            return 0.0
        cap = float(self.gateway.cfg.get("upstream_fault_backoff_max") or base)
        return min(base * (2 ** attempt), cap)

    @staticmethod
    def _sleep_backoff(delay: float) -> None:
        if delay > 0:
            time.sleep(delay)

    def _log_giveup(self, kind: str, model: str, status: int, attempts: int,
                    last: str = "") -> None:
        """Record a request the gateway finally gave up on.

        The per-attempt lines ("等 Xs 后重试") make a recovered burst visible,
        but the *failure* path — every account exhausted, a 502 handed to the
        client — used to write nothing at all, so "客户端收到 502" left no
        trace in the log and could not be diagnosed after the fact.
        """
        self.gateway.record_call(account="-", loginid="", model=model,
                                 status=status or 502, seconds=0.0, kind=kind,
                                 stream=kind.endswith("stream"),
                                 error=f"gave up after {attempts} attempts: {last}"[:300])
        self.gateway.log(f"[{kind}] 放弃 {model}：{attempts} 次尝试后仍失败 → "
                         f"返回 {status or 502} 给客户端"
                         + (f"（最后错误：{last[:160]}）" if last else ""))

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

    def _read_chunk(self, conn, resp, progress_seconds: float, *,
                    last_progress: float = 0.0) -> bytes:
        """Read one chunk, aborting if the upstream makes no progress.

        ``http.client`` cannot resume a read after a socket timeout, so the
        budget is applied as the socket timeout itself rather than by polling:
        a timeout means the upstream really went silent, and the caller
        abandons the connection (retrying when nothing was sent yet).

        ``progress_seconds`` bounds the *total* time without any progress.  A
        reasoning model that keeps streaming deltas is making progress even
        though it has not emitted an answer yet, so the deadline is only hit
        when **nothing at all** arrives — a genuine stall.  ``last_progress``
        is the timestamp of the previous byte, so the caller's budget spans
        the whole progress gap rather than just one read.
        """
        if last_progress and progress_seconds:
            idle = time.time() - last_progress
            if idle > progress_seconds:
                raise StreamStalled(idle, "no-progress")
        sock = _stream_socket(resp, conn)
        if sock is not None:
            try:
                sock.settimeout(progress_seconds or 180)
            except OSError:
                pass
        try:
            read1 = getattr(resp, "read1", None)
            if read1 is not None:
                return read1(4096)
            return resp.read(4096)
        except socket.timeout as exc:
            raise StreamStalled(progress_seconds, "no-progress") from exc
        except (OSError, ValueError) as exc:
            raise StreamStalled(time.time() - (last_progress or time.time()),
                                f"io:{exc}") from exc

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
            "/api/panel/model-events": panel.model_events,
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
        body = json.loads(raw.decode("utf-8")) if raw else {}
        acc = gw.pool.acquire(model=str(body.get("model") or ""))
        status, headers, data = gw.models_client.call(acc.session, suffix, body)
        if status == 200:
            # a dead session answers 200 with a JSON envelope on these
            # endpoints too — surface it as the error it is
            fault = business_error_status(data)
            if fault is not None:
                status = fault
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
        pool_error = ""
        for attempt in range(int(gw.cfg.get("max_retries") or 0) + 1):
            try:
                acc = gw.pool.acquire(exclude=tried, model=model)
            except PoolError as exc:
                gw.log(f"[warn] {exc}")
                # Only a refusal *before any upstream call* is a verdict on
                # the model; after that, the accounts were simply exhausted
                # and the real upstream status must survive.
                if not tried:
                    pool_error = str(exc)
                break
            tried.append(acc.name)
            started = time.time()
            status, _hdrs, data = gw.models_client.call(
                acc.session, "chat/completions", req, extra)
            if status == 200:
                # 200 + business envelope = a dead session, not a completion
                fault = business_error_status(data)
                if fault is not None:
                    gw.record_call(account=acc.name, loginid=acc.loginid,
                                   model=model, status=fault,
                                   seconds=time.time() - started,
                                   kind="chat", stream=False,
                                   error=data[:300].decode("utf-8", "replace"))
                    gw.log(f"[call] {acc.name} {model} http=200 业务错误 "
                           f"code={fault} → "
                           f"{'session 失效，换账号并后台重登' if fault == 401 else '上游异常，换账号重试'}"
                           f"：{data[:160].decode('utf-8', 'replace')}")
                    last = (fault, data)
                    if fault in self.ACCOUNT_FAULT:
                        gw.pool.report_failure(
                            acc, fault, data[:200].decode("utf-8", "replace"))
                    continue
                try:
                    obj = json.loads(data.decode("utf-8"))
                except Exception:                       # noqa: BLE001
                    obj = None
                usage = (obj or {}).get("usage") or {}
                gw.pool.report_success(acc, usage)
                elapsed = time.time() - started
                gw.note_model_substitution(acc, model, (obj or {}).get("model"))
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
            elif status in self.UPSTREAM_FAULT:
                # upstream hiccup (502/504/500) — do NOT punish the account for
                # it, but do wait: another account hits the same bad window.
                delay = self._upstream_backoff(attempt)
                gw.log(f"[call] {acc.name} {model} http={status}"
                       f" → 上游异常，等 {delay:.1f}s 后重试（不冷却该账号）")
                self._sleep_backoff(delay)
                continue
            else:
                # upstream hiccup the account is not to blame for
                gw.log(f"[call] {acc.name} {model} http={status} → 上游异常，换账号重试"
                       f"（不冷却该账号）")
            if status in self.FATAL_REQUEST:            # not an account problem
                break
        if not last[0] and pool_error:
            # Nothing was ever sent upstream: the pool refused before any call.
            # That is a request-level verdict (no account is entitled to this
            # model), not an upstream failure — 502 with an empty message told
            # the caller nothing.
            return self._error(400, pool_error, "account_pool_error")
        status, data = last
        if status >= 400:
            self._log_giveup("call", model, status,
                             len(tried), data[:160].decode("utf-8", "replace"))
        try:
            return self._json(status or 502, json.loads(data.decode("utf-8")))
        except Exception:                               # noqa: BLE001
            return self._error(status or 502, data[:500].decode("utf-8", "replace"),
                               "upstream_error")

    def _open_stream(self, req: Dict[str, Any], extra: Optional[Dict[str, str]],
                     *, log_tag: str,
                     exclude: Tuple[str, ...] = (),
                     pool_error: Optional[list] = None,
                     ) -> Tuple[Optional[Any], Optional[Any], Optional[Any]]:
        """Acquire an account and open a 200 stream, retrying other accounts.

        ``exclude`` carries accounts the caller already found unusable in an
        earlier attempt (a dead session, an empty stream): without it the
        stream retry loop re-picked the same highest-balance account every
        time.

        Returns ``(acc, conn, resp)``; ``acc`` is ``None`` when every attempt
        failed (``resp`` then carries the last error response, if any).

        ``pool_error`` is an optional one-element list the caller passes in to
        receive the reason the pool refused — without it, "no account is
        entitled to this model" reached the client as an empty 502.
        """
        gw = self.gateway
        model = req.get("model")
        tried: list = list(exclude)
        conn = resp = None

        for attempt in range(int(gw.cfg.get("max_retries") or 0) + 1):
            try:
                acc = gw.pool.acquire(exclude=tried, model=model)
            except PoolError as exc:
                gw.log(f"[warn] {exc}")
                # Only the *first* attempt says anything about the model: once
                # an upstream call has been attempted and failed, the accounts
                # are merely exhausted, and "no account is entitled to it"
                # would misattribute an upstream outage to the pool.
                if pool_error is not None and not tried:
                    pool_error.append(str(exc))
                return None, None, resp
            tried.append(acc.name)
            conn, resp = gw.models_client.stream(acc.session, "chat/completions",
                                                 req, extra)
            if resp.status == 200:
                # A dead session still answers 200 — with a JSON business
                # envelope and no SSE at all.  Returning it as a stream is what
                # made the gateway retry the same account three times and log
                # "上游流无输出即结束" instead of failing the account over.
                ctype = (resp.getheader("Content-Type") or "").lower()
                if "text/event-stream" in ctype:
                    return acc, conn, resp
                data = resp.read()
                conn.close()
                fault = business_error_status(data)
                if fault is None:
                    # 200 + a non-SSE body (e.g. a JSON completion for a
                    # streaming request): not something we can relay as a
                    # stream — treat it as an upstream anomaly.
                    gw.log(f"[{log_tag}] {acc.name} {model} stream http=200 "
                           f"但响应不是 SSE（Content-Type={ctype or '?'}），"
                           f"换账号重试：{data[:160].decode('utf-8', 'replace')}")
                    conn = resp = _ContentRejected(resp.status, data)
                    continue
                gw.log(f"[{log_tag}] {acc.name} {model} stream http=200 "
                       f"业务错误 code={fault} → "
                       f"{'session 失效，换账号并后台重登' if fault == 401 else '上游异常，换账号重试'}"
                       f"：{data[:160].decode('utf-8', 'replace')}")
                if fault in self.ACCOUNT_FAULT:
                    gw.pool.report_failure(acc, fault,
                                           data[:200].decode("utf-8", "replace"))
                conn = resp = None
                continue
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
            elif resp.status in self.UPSTREAM_FAULT:
                # the upstream's own gateway is flaking, and another account
                # hits the same bad window — pause before retrying, otherwise
                # every attempt burns inside the burst and this 502 is served.
                delay = self._upstream_backoff(attempt)
                gw.log(f"[{log_tag}] {acc.name} {model} stream http={resp.status}"
                       f" → 上游异常，等 {delay:.1f}s 后重试（不冷却该账号）")
                conn = resp = None
                self._sleep_backoff(delay)
                continue
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
        progress_seconds = float(
            gw.cfg.get("stream_progress_stall_seconds")
            or gw.cfg.get("stream_stall_seconds") or 600)
        disable_thinking = bool(req.pop("_no_thinking", False))
        # accounts this request already found unusable: a stream retry must
        # land on a different account, otherwise a dead session (or a model
        # that keeps returning an empty stream) is replayed on every attempt
        failed_accounts: list = []

        for attempt in range(attempts):
            body = dict(req)
            if disable_thinking:
                # second attempt onwards: ask the model to skip reasoning so a
                # reasoning loop cannot stall the stream again
                body["enable_thinking"] = False
                body["chat_template_kwargs"] = {"enable_thinking": False}

            pool_error: list = []
            acc, conn, resp = self._open_stream(
                body, extra, log_tag="call", exclude=tuple(failed_accounts),
                pool_error=pool_error)
            if acc is None:
                if attempt > 0:
                    # SSE headers already went out on the first attempt; a
                    # second HTTP response would be appended to the body.
                    self._log_giveup("call", model, 200, attempts)
                    self._terminate_sse("所有账号都失败了 / all accounts failed")
                    return
                if resp is None:
                    if pool_error:
                        # The pool refused before anything was sent: tell the
                        # caller which model it cannot serve, not a bare 502.
                        self._log_giveup("call", model, 400, attempts, pool_error[0])
                        return self._error(400, pool_error[0], "account_pool_error")
                    self._log_giveup("call", model, 502, attempts)
                    return self._error(502, "所有账号都失败了 / all accounts failed",
                                       "upstream_error")
                data = resp.read()
                resp.close() if hasattr(resp, "close") else None
                self._log_giveup("call", model, resp.status, attempts,
                                 data[:160].decode("utf-8", "replace"))
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
            saw_done = False             # upstream sent [DONE]
            finished = False             # upstream sent finish_reason
            last_progress = time.time()  # every upstream byte resets this
            stalled: Optional[StreamStalled] = None
            try:
                while True:
                    chunk = self._read_chunk(conn, resp, progress_seconds,
                                             last_progress=last_progress)
                    if not chunk:
                        break
                    last_progress = time.time()
                    buffer += chunk
                    while b"\n\n" in buffer:
                        block, buffer = buffer.split(b"\n\n", 1)
                        out = self._clean_sse_block(block)
                        if out is None:
                            dropped += 1
                            continue
                        if self._block_is_done(out):
                            saw_done = True
                        if not saw_content:
                            saw_content = True
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
                        if self._block_is_done(out):
                            saw_done = True
                        if not saw_content:
                            saw_content = True
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
                failed_accounts.append(acc.name)
                gw.log(f"[call] {acc.name} {model} 流卡住（{stalled.kind} "
                       f"{stalled.seconds:.0f}s）且未输出内容 → 第 {attempt + 2} 次重试"
                       f"（换账号）")
                buffer = b""
                continue
            if can_retry and not saw_done and not finished:
                # An empty stream is almost always a dead session answered
                # with a business error, or a model refusing this account.
                # Retrying the *same* account just repeats it.
                failed_accounts.append(acc.name)
                gw.pool.report_no_output(acc, "上游流无输出即结束")
                gw.log(f"[call] {acc.name} {model} 上游流无输出即结束 "
                       f"→ 第 {attempt + 2} 次重试（换账号）")
                buffer = b""
                continue

            # cannot retry any more (or content already went out): close cleanly
            reason = ""
            if not finished and not saw_done:
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
            if saw_content:
                gw.pool.report_success(acc, usage)
            else:
                # nothing reached the client: this is a failed attempt, not a
                # successful 0.0s call.  Recording it as success kept a broken
                # account "healthy" and out of cooldown.
                gw.pool.report_no_output(acc, reason or "上游流无输出即结束")
            elapsed = time.time() - started
            gw.record_call(account=acc.name, loginid=acc.loginid, model=model,
                           status=200 if saw_content else 502, seconds=elapsed,
                           usage=usage, kind="chat", stream=True,
                           error="" if finished or saw_done else reason)
            gw.log(f"[call] {acc.name} {model} {elapsed:.1f}s "
                   f"http={'200' if saw_content else '502'} {human_usage(usage)} stream"
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

    def _block_is_done(self, block: bytes) -> bool:
        """True for the terminal ``data: [DONE]`` block."""
        for line in block.split(b"\n"):
            if line.strip() == b"data: [DONE]":
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
        pool_error = ""
        for attempt in range(int(gw.cfg.get("max_retries") or 0) + 1):
            try:
                acc = gw.pool.acquire(exclude=tried, model=model)
            except PoolError as exc:
                gw.log(f"[warn] {exc}")
                # Only a refusal *before any upstream call* is a verdict on
                # the model; after that, the accounts were simply exhausted
                # and the real upstream status must survive.
                if not tried:
                    pool_error = str(exc)
                break
            tried.append(acc.name)
            started = time.time()
            status, _h, data = gw.models_client.call(acc.session, "chat/completions", payload)
            if status == 200:
                fault = business_error_status(data)
                if fault is not None:
                    gw.record_call(account=acc.name, loginid=acc.loginid,
                                   model=model, status=fault,
                                   seconds=time.time() - started,
                                   kind="messages", stream=False,
                                   error=data[:300].decode("utf-8", "replace"))
                    gw.log(f"[messages] {acc.name} {model} http=200 业务错误 "
                           f"code={fault} → "
                           f"{'session 失效，换账号并后台重登' if fault == 401 else '上游异常，换账号重试'}"
                           f"：{data[:160].decode('utf-8', 'replace')}")
                    last = (fault, data)
                    if fault in self.ACCOUNT_FAULT:
                        gw.pool.report_failure(
                            acc, fault, data[:200].decode("utf-8", "replace"))
                    continue
                obj = json.loads(data.decode("utf-8"))
                gw.note_model_substitution(acc, model, obj.get("model"))
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
            elif status in self.UPSTREAM_FAULT:
                delay = self._upstream_backoff(attempt)
                gw.log(f"[messages] {acc.name} {model} http={status}"
                       f" → 上游异常，等 {delay:.1f}s 后重试（不冷却该账号）")
                self._sleep_backoff(delay)
                continue
            else:
                gw.log(f"[messages] {acc.name} {model} http={status}"
                       f" → 上游异常，换账号重试（不冷却该账号）")
            if status in self.FATAL_REQUEST:
                break
        if not last[0] and pool_error:
            return self._error(400, pool_error, "account_pool_error")
        status, data = last
        if status >= 400:
            self._log_giveup("messages", model, status,
                             len(tried), data[:160].decode("utf-8", "replace"))
        return self._json(status or 502, {
            "type": "error",
            "error": {"type": "api_error",
                      "message": data[:500].decode("utf-8", "replace")}})

    def _messages_stream(self, payload: Dict[str, Any], model: str) -> None:
        gw = self.gateway

        attempts = int(gw.cfg.get("stream_retries") or 0) + 1
        progress_seconds = float(
            gw.cfg.get("stream_progress_stall_seconds")
            or gw.cfg.get("stream_stall_seconds") or 600)
        disable_thinking = bool(payload.pop("_no_thinking", False))
        failed_accounts: list = []

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

            pool_error: list = []
            acc, conn, resp = self._open_stream(
                body, None, log_tag="messages", exclude=tuple(failed_accounts),
                pool_error=pool_error)
            if acc is None:
                if attempt > 0:
                    # the SSE response is already open; a JSON error appended
                    # to it would corrupt the stream.  Open the message first
                    # so the error lands inside a well-formed message.
                    for event, data in anth.StreamTranslator(model).start():
                        emit(event, data)
                    self._log_giveup("messages-stream", model, 200, attempts)
                    self._terminate_anthropic("all accounts failed")
                    return
                if resp is None:
                    if pool_error:
                        self._log_giveup("messages-stream", model, 400, attempts,
                                         pool_error[0])
                        return self._json(400, {"type": "error", "error": {
                            "type": "account_pool_error",
                            "message": pool_error[0]}})
                    self._log_giveup("messages-stream", model, 502, attempts)
                    return self._json(502, {"type": "error", "error": {
                        "type": "api_error", "message": "all accounts failed"}})
                data = resp.read()
                resp.close() if hasattr(resp, "close") else None
                self._log_giveup("messages-stream", model, resp.status, attempts,
                                 data[:160].decode("utf-8", "replace"))
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
            saw_done = False
            finished = False
            started_stream = False       # message_start already emitted
            last_progress = time.time()  # every upstream byte resets this
            stalled: Optional[StreamStalled] = None
            try:
                while True:
                    chunk = self._read_chunk(conn, resp, progress_seconds,
                                             last_progress=last_progress)
                    if not chunk:
                        break
                    last_progress = time.time()
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        line = line.strip()
                        if not line.startswith(b"data:"):
                            continue
                        text = line[5:].strip()
                        if not text:
                            continue
                        if text == b"[DONE]":
                            saw_done = True
                            continue
                        try:
                            obj = json.loads(text)
                        except Exception:               # noqa: BLE001
                            continue
                        # Defer message_start until the upstream actually said
                        # something: emitting it up front meant a failed
                        # attempt still sent a complete (empty) message, and a
                        # retry then produced a second message_start inside the
                        # same response.
                        if not started_stream:
                            for event, data in translator.start():
                                emit(event, data)
                            started_stream = True
                        choice = (obj.get("choices") or [{}])[0]
                        if choice.get("finish_reason"):
                            finished = True
                        events = list(translator.feed(obj))
                        for event, data in events:
                            if event != "ping":
                                saw_content = True
                            emit(event, data)
                if started_stream:
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
                failed_accounts.append(acc.name)
                gw.log(f"[messages] {acc.name} {model} 流卡住（{stalled.kind} "
                       f"{stalled.seconds:.0f}s）且无输出 → 第 {attempt + 2} 次重试"
                       f"（换账号）")
                continue
            if retryable and stalled is None and not saw_done and not finished:
                failed_accounts.append(acc.name)
                gw.log(f"[messages] {acc.name} {model} 上游流无输出即结束 "
                       f"→ 第 {attempt + 2} 次重试（换账号）")
                continue

            reason = ""
            if stalled is not None and not started_stream:
                # nothing was emitted yet: open the message so the error event
                # lands inside a well-formed message
                for event, data in translator.start():
                    emit(event, data)
                started_stream = True
            if stalled is not None:
                # content already reached the client: end the stream properly.
                # A half-written tool_use block must not be reported as a
                # clean end_turn — the client would try to execute a tool
                # call whose arguments are truncated JSON.
                broken = translator.truncated_tool()
                reason = (f"upstream stream interrupted ({stalled.kind})"
                          + ("，tool 参数截断" if broken else ""))
                gw.log(f"[messages] {acc.name} {model} 流中断（{stalled.kind} "
                       f"{stalled.seconds:.0f}s）"
                       f"{'，tool 参数截断' if broken else ''} → 补发终止事件")
                self._terminate_anthropic("upstream stream interrupted",
                                          broken_tool=broken)
            elif not started_stream:
                # every attempt came back empty: the client is holding an SSE
                # response that will never carry a message.  Open the message
                # and close it with an error so it retries instead of showing
                # an empty answer.
                reason = "upstream stream produced no output"
                for event, data in translator.start():
                    emit(event, data)
                self._terminate_anthropic(reason)
            if saw_content:
                gw.pool.report_success(acc, translator.usage)
            else:
                gw.pool.report_no_output(acc, reason or "上游流无输出即结束")
            elapsed = time.time() - started
            gw.record_call(account=acc.name, loginid=acc.loginid, model=model,
                           status=200 if saw_content else 502, seconds=elapsed,
                           usage=translator.usage, kind="messages", stream=True,
                           error=reason)
            gw.log(f"[messages] {acc.name} {model} {elapsed:.1f}s "
                   f"http={'200' if saw_content else '502'} "
                   f"{human_usage(translator.usage)} stream"
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
        # Initialized *before* super(): a failed bind makes socketserver call
        # server_close() from inside __init__, and server_close() needs these.
        # Without them the real OSError ("port in use") was swallowed by an
        # AttributeError that hid why startup failed.
        #: Live client sockets.  ``server_close()`` only closes the listening
        #: socket; an HTTP/1.1 keep-alive connection would keep being served
        #: by the *old* handler thread (and its old Gateway), so a rebind left
        #: clients reading stale state over a socket that never reconnected.
        self._active = set()
        self._active_lock = threading.Lock()
        super().__init__(*args, **kwargs)

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


class AlreadyRunning(RuntimeError):
    """Another gateway instance holds the single-instance lock."""


class InstanceLock:
    """Cross-process guard against two gateways sharing one account pool.

    A port check is not enough on Windows: ``bind(("0.0.0.0", P))`` and
    ``bind(("127.0.0.1", P))`` *both succeed* (measured — Windows does not
    consider a wildcard and a specific address to conflict), so a second
    ``serve`` silently stole a share of the connections while sharing the same
    ``accounts.json``.  Two pools logging into the same accounts invalidate
    each other's sessions, which is what broke client requests mid-stream.

    Where it lives: next to the accounts file, so two checkouts/ports pointing
    at the same pool still collide, while unrelated deployments do not.
    """

    #: Byte offset of the locked region.  Kept past the PID text because
    #: ``msvcrt.locking`` blocks *reads* of the locked range too, and the
    #: loser needs to read the holder's PID out of the same file.
    _LOCK_OFFSET = 64
    #: Held at ``_LOCK_OFFSET`` while the instance runs.
    _LOCK_BYTE = b"\0"

    #: PID currently holding the lock in *this* process, plus a re-entry depth
    #: so the Android watchdog can call serve() again while the previous
    #: instance is still tearing down.  0 = not held here.
    _held_by_pid = 0
    _depth = 0
    _held_lock = threading.Lock()

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = None
        self._reentrant = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        # Same process holding it?  This is not a second gateway: the Android
        # watchdog restarts the gateway in place (close the listener, call
        # serve() again) and the previous thread may not have released yet.
        # Refusing here would make that restart impossible.
        if type(self)._held_by_pid == os.getpid():
            with type(self)._held_lock:
                type(self)._depth += 1
            self._reentrant = True
            return

        self._reentrant = False
        try:
            self._fh = open(self.path, "a+b")
            self._lock()
        except OSError as exc:
            if self._fh is not None:
                try:
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None
            if exc.errno in (errno.EACCES, errno.EAGAIN,
                             getattr(errno, "EDEADLK", 0)):
                raise AlreadyRunning(self.holder()) from exc
            raise
        # Written after locking, so the loser can read it (it sits outside the
        # locked byte).  The file itself is kept: the OS lock dies with the
        # process, but the text is what tells the next starter who was there.
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(f"{os.getpid()}\n".encode())
        self._fh.flush()
        with type(self)._held_lock:
            type(self)._held_by_pid = os.getpid()
            type(self)._depth = 1

    def _lock(self) -> None:
        if os.name == "nt":
            import msvcrt
            self._fh.seek(self._LOCK_OFFSET)
            self._fh.write(self._LOCK_BYTE)          # the byte must exist
            self._fh.flush()
            self._fh.seek(self._LOCK_OFFSET)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release(self) -> None:
        with type(self)._held_lock:
            if type(self)._held_by_pid == os.getpid():
                type(self)._depth -= 1
                if type(self)._depth > 0:
                    # Still held by another serve() in this process.
                    self._reentrant = False
                    return
                type(self)._held_by_pid = 0
        self._reentrant = False
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(self._LOCK_OFFSET)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    def holder(self) -> str:
        """PID recorded by the current holder, or an empty string.

        Only the first line counts: past it lie the padding byte the lock
        region needs on Windows.
        """
        try:
            raw = Path(self.path).read_bytes()
        except OSError:
            return ""
        first = raw.split(b"\n", 1)[0].split(b"\0", 1)[0]
        try:
            text = first.decode("ascii").strip()
        except UnicodeDecodeError:
            return ""
        return text if text.isdigit() else ""

    def error_message(self) -> str:
        pid = self.holder()
        who = f"（PID {pid}）" if pid else ""
        return (f"已经有一个 loomy2api 网关在运行{who}，同一个账号池只能由一个网关持有"
                f"：两个实例会互相顶掉 session，表现为客户端请求中途静默中断。\n"
                f"请先停止它（stop.cmd / 一键停止.bat），或用 --port 换一个端口。"
                f"\n锁定文件：{self.path}")


def listening_pids(port: int) -> "set[int]":
    """PIDs listening on ``port``, whatever address they bound.

    A bind-probe cannot answer this on Windows: ``bind(("0.0.0.0", P))``
    succeeds even while another process listens on ``127.0.0.1:P``, so the
    kernel will not tell us the port is taken.  Enumerating listeners is the
    only reliable check.
    """
    found: "set[int]" = set()
    try:
        import subprocess
        if os.name == "nt":
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                                 capture_output=True, text=True, timeout=5).stdout
            for line in out.splitlines():
                parts = line.split()
                if (len(parts) >= 5 and parts[3] == "LISTENING"
                        and parts[1].endswith(f":{port}")
                        and parts[4].isdigit()):
                    found.add(int(parts[4]))
        else:
            out = subprocess.run(["ss", "-lptnH"], capture_output=True,
                                 text=True, timeout=5).stdout
            for line in out.splitlines():
                if f":{port} " in line + " " and "pid=" in line:
                    chunk = line.split("pid=", 1)[1].split(",", 1)[0]
                    if chunk.isdigit():
                        found.add(int(chunk))
    except Exception:                                    # noqa: BLE001
        pass                                             # diagnostics only
    return found


def error_owner(port: int) -> str:
    """Best-effort " (PID 1234)" for whoever is listening on ``port``."""
    pids = sorted(listening_pids(port) - {os.getpid()})
    return f"（PID {pids[0]}）" if pids else ""


def serve(cfg, *, gateway: Optional[Gateway] = None,
          on_server=None) -> Gateway:
    """Run the HTTP server until interrupted.

    ``on_server`` receives the bound server before it starts serving, so an
    embedder (the Android app) can shut it down to rebind on a different
    interface without leaving the old socket in place.
    """
    lock = InstanceLock(Path(cfg.path("accounts_file", "accounts.json"))
                        .with_name("gateway.lock"))
    try:
        lock.acquire()
    except AlreadyRunning as exc:
        raise AlreadyRunning(lock.error_message()) from exc

    try:
        return _serve_locked(cfg, gateway=gateway, on_server=on_server)
    finally:
        lock.release()


def _serve_locked(cfg, *, gateway: Optional[Gateway] = None,
                  on_server=None) -> Gateway:
    gw = gateway or Gateway(cfg)

    # Refuse before binding: the kernel would happily give us 127.0.0.1:P while
    # another process listens on 0.0.0.0:P, so "bind succeeded" means nothing.
    port = int(cfg["port"])
    if port:
        others = sorted(listening_pids(port) - {os.getpid()})
        if others:
            raise AlreadyRunning(
                f"端口 {port} 已被占用（PID {', '.join(map(str, others))}）。"
                f"这不一定是同一个账号池：不同绑定地址（0.0.0.0 与 127.0.0.1）"
                f"可以同时监听，谁收到请求是不确定的。\n"
                f"请先停止它（stop.cmd / 一键停止.bat），或用 --port 换一个端口。")

    handler = type("BoundHandler", (Handler,), {"gateway": gw})
    try:
        httpd = Server((str(cfg["host"]), port), handler)
    except OSError as exc:
        # Lost a race with something that started between the check and here.
        if exc.errno in (errno.EADDRINUSE, errno.EACCES):
            raise AlreadyRunning(
                f"端口 {port} 已被占用{error_owner(port)}，"
                f"请先停止原来的实例，或用 --port 换一个端口。") from exc
        raise
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
