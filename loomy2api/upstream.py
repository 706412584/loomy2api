"""Client for the Loomy model gateway (OpenAI-compatible upstream)."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Dict, Optional, Tuple

from .httpc import json_body, open_stream, request as http_request
from . import constants as C

__all__ = ["ModelGateway", "UpstreamError", "build_model_headers",
           "business_error_status"]


class UpstreamError(RuntimeError):
    def __init__(self, message: str, status: int = 0, *, banned: bool = False):
        super().__init__(message)
        self.status = status
        #: ``True`` when the upstream says the account itself is disabled
        #: (banned / suspended) — not recoverable by re-login.
        self.banned = banned


#: The model gateway answers HTTP 200 with a business code when the session is
#: no longer accepted (e.g. ``points/first-login`` → "登录已失效，请重新登录").
#: Treating that as a plain upstream failure skipped the password re-login and
#: left the account stuck until the local 14-day expiry.
SESSION_EXPIRED_MARKERS = ("登录已失效", "重新登录", "session 失效",
                           "登录状态已失效", "token 已失效")


def _check_payload(status: int, payload: Dict[str, Any], data: bytes,
                   path: str) -> None:
    """Raise :class:`UpstreamError` for an error payload.

    A session-expiry message is promoted to ``status=401`` so callers rotate /
    re-login exactly as they do for a real 401.
    """
    code = str(payload.get("code") or "")
    desc = str(payload.get("desc") or payload.get("message") or "")
    if status == 200 and code in ("", "000000", "None"):
        return
    if status == 200 and not desc:
        return
    text = desc or data[:200].decode("utf-8", "replace")
    if any(marker in text for marker in SESSION_EXPIRED_MARKERS):
        raise UpstreamError(f"{path} HTTP {status}: {text}", 401)
    # An upstream-disabled (banned) account is not a transient failure: mark it
    # with a distinct status so callers can flag it instead of cooling down and
    # retrying it forever.
    if any(marker in text for marker in C.BANNED_MARKERS):
        raise UpstreamError(f"{path} HTTP {status}: {text}", 403,
                            banned=True)
    raise UpstreamError(f"{path} HTTP {status}: {text}", status)


def business_error_status(data: bytes) -> Optional[int]:
    """Classify an HTTP-200 body that is really an upstream error.

    The model gateway answers ``200`` with a JSON business envelope
    (``{"code": "100002", "desc": "登录已失效，请重新登录", "data": {}}``) when
    the session is dead.  A streaming caller that treats that as SSE reads no
    ``data:`` lines, reports an "empty stream" and retries the same account
    forever; a non-stream caller would hand the error envelope to the client
    as a successful completion.

    Returns ``401`` for a dead session, ``502`` for any other business error,
    and ``None`` when the body looks like a normal payload.
    """
    if not data or data[:1] not in (b"{", b"["):
        return None
    try:
        payload = json.loads(data.decode("utf-8"))
    except Exception:                                   # noqa: BLE001
        return None
    if not isinstance(payload, dict):
        return None
    # a real completion / catalogue / points payload, not an error envelope
    if payload.get("choices") or payload.get("object") is not None:
        return None
    if payload.get("error"):
        error = payload["error"]
        if isinstance(error, dict):
            kind = str(error.get("type") or error.get("code") or "").lower()
            if kind == "insufficient_quota":
                return 402
        text = json.dumps(error, ensure_ascii=False)
        return 401 if any(m in text for m in SESSION_EXPIRED_MARKERS) else 502
    code = str(payload.get("code") if payload.get("code") is not None else "")
    desc = str(payload.get("desc") or payload.get("message") or "")
    if code in ("", "000000", "None") and desc in ("", "成功", "success", "ok"):
        return None
    text = desc or f"code={code}"
    if any(marker in text for marker in SESSION_EXPIRED_MARKERS):
        return 401
    # an upstream-disabled (banned) account: an account fault, not a payload one
    if any(marker in text for marker in C.BANNED_MARKERS):
        return 403
    return 502


def build_model_headers(session: str, *, version: str = "",
                        extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Headers the model gateway requires.

    * ``Authorization`` **and** ``token`` — the client's provider uses
      ``useSessionAuth``, and the gateway accepts either, so both are sent.
    * ``traceparent`` — without it the upstream hangs until timeout (measured
      by the client authors, documented in their source).
    * ``loomy-version`` — presence-checked only.
    """
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {session}",
        "token": session,
        "traceparent": f"00-{uuid.uuid4().hex}-{uuid.uuid4().hex[:16]}-01",
    }
    if version:
        headers["loomy-version"] = str(version)
    for key, value in (extra or {}).items():
        if value:
            headers[key] = str(value)
    return headers


def _chat_id_for(session: str) -> str:
    """A stable ``chat-<uuid>`` derived from the account session.

    The official client keeps one ChatId per conversation and the gateway has
    no conversation id, so the session is the closest stable seed — turns
    served by the same account stay under one attribution id instead of a new
    "conversation" per request.
    """
    h = hashlib.sha256(session.encode("utf-8")).hexdigest()
    return f"chat-{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _has_image_part(payload: Dict[str, Any]) -> bool:
    """True when any message carries an inline image part."""
    if not isinstance(payload, dict):
        return False
    for message in payload.get("messages") or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") in ("image_url", "input_image"):
                    return True
    return False


def _session_affinity(session: str, payload: Dict[str, Any]) -> str:
    """A stable ``ses_<id>`` for backend stickiness / prefix cache.

    The official opencode path sends ``x-session-affinity: ses_<session id>``
    and it drives which upstream replica serves the turn (and thus whether the
    prompt prefix cache can be reused).  A gateway has no conversation id, so
    we seed with the account session plus the first user message — that stays
    identical across the turns of one conversation and differs between them.
    """
    seed = ""
    if isinstance(payload, dict):
        for message in payload.get("messages") or []:
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                seed = content
            elif isinstance(content, list):
                seed = json.dumps(content, ensure_ascii=False)
            break
    h = hashlib.sha256((session + "\x00" + seed).encode("utf-8")).hexdigest()
    return f"ses_{h[:32]}"


def _sanitize_chat_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Fill empty content / inject a text part so strict upstreams accept it.

    The upstream got stricter in 2026-11: ``content: ""``, ``content: null`` and
    multimodal arrays whose parts are all images are answered with the generic
    400 "请求有误，请重试" (same shape aigw fixed on 2026-11-25).  Copy-on-write:
    the caller's payload is never mutated.
    """
    if not isinstance(payload, dict):
        return payload
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return payload
    changed = False
    out = []
    for message in messages:
        if not isinstance(message, dict):
            out.append(message)
            continue
        content = message.get("content")
        if isinstance(content, str):
            if not content.strip():
                message = {**message, "content": " "}
                changed = True
        elif content is None:
            message = {**message, "content": " "}
            changed = True
        elif isinstance(content, list):
            if not content:
                message = {**message, "content": [{"type": "text", "text": " "}]}
                changed = True
                out.append(message)
                continue
            has_text = any(
                isinstance(p, dict) and p.get("type") == "text"
                and str(p.get("text") or "").strip()
                for p in content)
            if not has_text:
                message = {**message, "content": [
                    {"type": "text", "text": "[image]"}, *content]}
                changed = True
            else:
                cleaned = [
                    p for p in content
                    if not (isinstance(p, dict) and p.get("type") == "text"
                            and not str(p.get("text") or "").strip())]
                if len(cleaned) != len(content):
                    message = {**message, "content": cleaned}
                    changed = True
        out.append(message)
    return {**payload, "messages": out} if changed else payload


class ModelGateway:
    """Thin wrapper around the upstream HTTP API."""

    def __init__(self, cfg):
        self.cfg = cfg

    # -- helpers --------------------------------------------------------

    @property
    def base(self) -> str:
        return str(self.cfg["upstream"]).rstrip("/")

    def _url(self, suffix: str) -> str:
        return f"{self.base}/{suffix.lstrip('/')}"

    def _headers(self, session: str, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        return build_model_headers(session, version=str(self.cfg.get("loomy_version") or ""),
                                   extra=extra)

    def _chat_headers(self, session: str,
                      extra: Optional[Dict[str, str]] = None,
                      payload: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        """Headers for ``/chat/completions`` — attribution + routing.

        The official client sends ``ChatId``/``MsgId``/``TurnId``/
        ``InvokeOrigin``/``X-Loomy-Request-Purpose`` on every chat call and the
        upstream's adjudication layer reads them (reversed from 0.9.39 in
        magpie-loomy-auth 0.1.28).  The *purpose* is also what routes the turn:
        an inline image sent as ``chat.message`` is stalled or answered blind,
        while ``kb.vision`` serves it in ~2 s (measured 2026-11-25), so a
        request carrying an image must be declared as vision.  The
        ``x-session-affinity`` header comes from the opencode chat path and
        decides backend stickiness / prefix-cache reuse.

        A caller-supplied value always wins, so the OpenAI path can forward its
        own ids and purpose.
        """
        msg_id = f"msg-{uuid.uuid4()}"
        purpose = str(self.cfg.get("request_purpose") or "chat.message")
        if payload is not None and _has_image_part(payload):
            purpose = "kb.vision"
        defaults = {
            "ChatId": _chat_id_for(session),
            "MsgId": msg_id,
            "TurnId": msg_id,
            "InvokeOrigin": "user",
            "X-Loomy-Request-Purpose": purpose,
            "x-session-affinity": _session_affinity(session, payload or {}),
        }
        defaults.update(extra or {})
        return self._headers(session, defaults)

    # -- read-only ------------------------------------------------------

    def models(self, session: str) -> Dict[str, Any]:
        # A catalogue read is small and cheap, and the probe fans out across the
        # whole pool from the keeper thread.  Inheriting ``timeout`` (1200s by
        # default for chat) meant a hung upstream could stall the keeper for
        # hours — and with it every session renewal and quota refresh.
        status, _h, data = http_request(
            self._url("models"), method="GET", headers=self._headers(session),
            timeout=float(self.cfg.get("models_timeout") or 45),
            proxy=str(self.cfg.get("proxy") or ""))
        if status != 200:
            raise UpstreamError(f"/models HTTP {status}: "
                                f"{data[:200].decode('utf-8', 'replace')}", status)
        payload = json.loads(data.decode("utf-8"))
        # A dead session answers 200 with ``{"code":"100002","desc":"登录已失效",
        # "data":{}}`` and no ``data`` list.  Without this check the probe read
        # that as a *successful* empty catalogue, removed the account from every
        # routed model and never re-logged it in.  _check_payload promotes the
        # expiry marker to 401, which is what the caller rotates on.
        _check_payload(status, payload, data, "/models")
        return payload

    def points_records(self, session: str, page_size: int = 20) -> Dict[str, Any]:
        url = self._url(f"points/records?record_type=all&page_no=1&page_size={page_size}")
        status, _h, data = http_request(
            url, method="GET", headers=self._headers(session),
            timeout=30, proxy=str(self.cfg.get("proxy") or ""))
        if status != 200:
            raise UpstreamError(f"/points/records HTTP {status}", status)
        payload = json.loads(data.decode("utf-8"))
        # A dead session answers HTTP 200 with ``code=100002 登录已失效`` and an
        # empty ``data``.  Without this check the empty payload parsed into
        # ``balance=None`` and the panel showed the account as "unknown" for
        # ever instead of re-logging it in.
        _check_payload(status, payload, data, "/points/records")
        return payload

    def quota(self, session: str) -> Dict[str, Any]:
        """→ ``{balance, daily_balance, available, ledger_total}``.

        ``ledger_total`` is the lifetime ledger row count the upstream reports
        alongside the balances — a cheap age proxy for telling a fresh number
        from a recycled one (see :attr:`Account.ledger_total`).
        """
        payload = self.points_records(session, page_size=1)
        data = payload.get("data") or {}
        return {
            "balance": data.get("balance"),
            "daily_balance": data.get("dailyBalance"),
            "available": data.get("availableBalance"),
            "ledger_total": data.get("total"),
        }

    def points_overview(self, session: str) -> Dict[str, Any]:
        """Daily grant + invitation/onboarding state (read-only).

        ``POST /points/first-login`` is the *status* call here — it reports the
        cycle and reward state and is idempotent (``alreadyProcessed``), which
        is how the official client reads this panel.  We never submit an
        invitation code, so nothing is mutated.

        The two reads are independent and each is a slow round-trip, so they
        run concurrently; serially this alone took several seconds.
        """
        from concurrent.futures import ThreadPoolExecutor

        def first_login() -> Dict[str, Any]:
            status, _h, data = http_request(
                self._url("points/first-login"), method="POST",
                headers=self._headers(session), body=b"{}",
                timeout=30, proxy=str(self.cfg.get("proxy") or ""))
            if status != 200:
                return {}
            payload = json.loads(data.decode("utf-8"))
            _check_payload(status, payload, data, "/points/first-login")
            return payload.get("data") or {}

        def activation() -> Dict[str, Any]:
            status, _h, data = http_request(
                self._url("points/activation"), method="GET",
                headers=self._headers(session), timeout=20,
                proxy=str(self.cfg.get("proxy") or ""))
            if status != 200:
                return {}
            payload = json.loads(data.decode("utf-8"))
            _check_payload(status, payload, data, "/points/activation")
            return payload.get("data") or {}

        out: Dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_first = pool.submit(first_login)
            f_act = pool.submit(activation)
            # An expired session must reach the caller so it can re-login;
            # everything else degrades to a partial overview.
            try:
                node = f_first.result()
            except UpstreamError as exc:
                if exc.status == 401:
                    raise
                out["error"] = str(exc)
            except Exception as exc:                    # noqa: BLE001
                out["error"] = str(exc)
            else:
                out.update({
                    "daily_quota": node.get("dailyQuota"),
                    "daily_consumed": node.get("dailyConsumed"),
                    "daily_balance": node.get("dailyBalance"),
                    "daily_cycle_date": node.get("dailyCycleDate"),
                    "permanent_balance": node.get("permanentBalance"),
                    "register_reward": node.get("registerReward"),
                    "invitee_reward": node.get("inviteeReward"),
                    "inviter_reward": node.get("inviterReward"),
                    "invitation_codes": node.get("invitationCodes") or [],
                })
            try:
                node = f_act.result()
            except UpstreamError as exc:
                if exc.status == 401:
                    raise
                out.setdefault("error", str(exc))
            except Exception as exc:                    # noqa: BLE001
                out.setdefault("error", str(exc))
            else:
                out["activated"] = node.get("activated")
                out["applied_invitation_code"] = node.get("appliedInvitationCode")
        return out

    def points_first_login(self, session: str, device_id: str) -> Dict[str, Any]:
        """Claim the one-time onboarding (registration) reward.

        Idempotent server-side: once claimed it returns ``alreadyProcessed``.
        """
        body = json.dumps({"deviceId": device_id}).encode("utf-8")
        status, _h, data = http_request(
            self._url("points/first-login"), method="POST",
            headers=self._headers(session), body=body, timeout=30,
            proxy=str(self.cfg.get("proxy") or ""))
        payload = json.loads(data.decode("utf-8"))
        _check_payload(status, payload, data, "/points/first-login")
        return payload.get("data") or {}

    def points_activate(self, session: str, device_id: str,
                        invite_code: str) -> Dict[str, Any]:
        """Submit an invitation code (``{deviceId, inviteCode}``).

        This mutates account state: a code can only be applied once.
        """
        body = json.dumps({"deviceId": device_id,
                           "inviteCode": invite_code}).encode("utf-8")
        status, _h, data = http_request(
            self._url("points/activation"), method="POST",
            headers=self._headers(session), body=body, timeout=30,
            proxy=str(self.cfg.get("proxy") or ""))
        payload = json.loads(data.decode("utf-8"))
        _check_payload(status, payload, data, "/points/activation")
        return payload.get("data") or {}

    def onboarding_tasks(self, session: str) -> Dict[str, Any]:
        """Task list + completion state, read-only."""
        status, _h, data = http_request(
            self._url("onboarding/tasks"), method="GET",
            headers=self._headers(session), timeout=20,
            proxy=str(self.cfg.get("proxy") or ""))
        if status != 200:
            raise UpstreamError(f"/onboarding/tasks HTTP {status}", status)
        return json.loads(data.decode("utf-8")).get("data") or {}

    def onboarding_complete(self, session: str, key: str) -> Dict[str, Any]:
        """Complete one onboarding task: body ``{"key": key}`` (flat).

        Verified live: a nested ``{"element": {"key": ...}}`` is rejected with
        "key 不能为空"; the flat shape returns ``{alreadyCompleted, balance}``.
        """
        body = json.dumps({"key": key}).encode("utf-8")
        status, _h, data = http_request(
            self._url("onboarding/tasks/complete"), method="POST",
            headers=self._headers(session), body=body, timeout=30,
            proxy=str(self.cfg.get("proxy") or ""))
        payload = json.loads(data.decode("utf-8"))
        _check_payload(status, payload, data, "/onboarding/tasks/complete")
        return payload.get("data") or {}

    def points_ledger(self, session: str, page_size: int = 50) -> Dict[str, Any]:
        """Recent points movements, read-only."""
        payload = self.points_records(session, page_size=page_size)
        data = payload.get("data") or {}
        rows = []
        for item in data.get("list") or []:
            rows.append({
                "ledger_id": item.get("ledgerId"),
                "model": item.get("modelName"),
                "direction": item.get("direction"),
                "description": item.get("description"),
                "points": item.get("pointsActual"),
                "source": item.get("consumeSource"),
                "balance_after": item.get("balanceAfter"),
                "created_at": item.get("createdAt"),
                "cycle_date": item.get("dailyCycleDate"),
            })
        return {"total": data.get("total"), "rows": rows}

    # -- request passthrough -------------------------------------------

    def call(self, session: str, suffix: str, payload: Dict[str, Any],
             extra_headers: Optional[Dict[str, str]] = None,
             method: str = "POST") -> Tuple[int, Dict[str, str], bytes]:
        chat = suffix == "chat/completions"
        if chat:
            payload = _sanitize_chat_payload(payload)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload else None
        headers = (self._chat_headers(session, extra_headers, payload)
                   if chat else self._headers(session, extra_headers))
        return http_request(
            self._url(suffix), method=method, headers=headers,
            body=body, timeout=float(self.cfg.get("timeout") or 1200),
            proxy=str(self.cfg.get("proxy") or ""))

    def stream(self, session: str, suffix: str, payload: Dict[str, Any],
               extra_headers: Optional[Dict[str, str]] = None):
        chat = suffix == "chat/completions"
        if chat:
            payload = _sanitize_chat_payload(payload)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = (self._chat_headers(session, extra_headers, payload)
                   if chat else self._headers(session, extra_headers))
        return open_stream(
            self._url(suffix), method="POST",
            headers=headers, body=body,
            timeout=float(self.cfg.get("timeout") or 1200),
            proxy=str(self.cfg.get("proxy") or ""))


def extract_usage(obj: Any) -> Dict[str, Any]:
    """Usage/points accounting that tolerates missing fields."""
    if not isinstance(obj, dict):
        return {}
    usage = obj.get("usage")
    return usage if isinstance(usage, dict) else {}


def sse_usage(line: bytes, sink: Dict[str, Any]) -> None:
    """Fold ``usage`` out of one SSE line into ``sink`` (last write wins)."""
    if not line.startswith(b"data:"):
        return
    chunk = line[5:].strip()
    if not chunk or chunk == b"[DONE]":
        return
    try:
        obj = json.loads(chunk)
    except Exception:                                   # noqa: BLE001
        return
    usage = extract_usage(obj)
    if usage:
        sink.update(usage)


def human_usage(usage: Dict[str, Any]) -> str:
    parts = []
    if usage.get("prompt_tokens") is not None or usage.get("completion_tokens") is not None:
        parts.append(f"tok={usage.get('prompt_tokens')}/{usage.get('completion_tokens')}")
    reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
    if reasoning:
        parts.append(f"think={reasoning}")
    if usage.get("points_consumed") is not None:
        parts.append(f"points={usage['points_consumed']}")
    return " ".join(parts)


def now() -> int:
    return int(time.time())
