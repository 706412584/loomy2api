"""Client for the Loomy model gateway (OpenAI-compatible upstream)."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, Optional, Tuple

from .httpc import json_body, open_stream, request as http_request

__all__ = ["ModelGateway", "UpstreamError", "build_model_headers"]


class UpstreamError(RuntimeError):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


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
    raise UpstreamError(f"{path} HTTP {status}: {text}", status)


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

    # -- read-only ------------------------------------------------------

    def models(self, session: str) -> Dict[str, Any]:
        status, _h, data = http_request(
            self._url("models"), method="GET", headers=self._headers(session),
            timeout=float(self.cfg.get("timeout") or 60),
            proxy=str(self.cfg.get("proxy") or ""))
        if status != 200:
            raise UpstreamError(f"/models HTTP {status}: "
                                f"{data[:200].decode('utf-8', 'replace')}", status)
        return json.loads(data.decode("utf-8"))

    def points_records(self, session: str, page_size: int = 20) -> Dict[str, Any]:
        url = self._url(f"points/records?record_type=all&page_no=1&page_size={page_size}")
        status, _h, data = http_request(
            url, method="GET", headers=self._headers(session),
            timeout=30, proxy=str(self.cfg.get("proxy") or ""))
        if status != 200:
            raise UpstreamError(f"/points/records HTTP {status}", status)
        return json.loads(data.decode("utf-8"))

    def quota(self, session: str) -> Dict[str, Any]:
        """→ ``{balance, daily_balance, available}``."""
        payload = self.points_records(session, page_size=1)
        data = payload.get("data") or {}
        return {
            "balance": data.get("balance"),
            "daily_balance": data.get("dailyBalance"),
            "available": data.get("availableBalance"),
        }

    def points_overview(self, session: str) -> Dict[str, Any]:
        """Daily grant + invitation/onboarding state (read-only).

        ``POST /points/first-login`` is the *status* call here — it reports the
        cycle and reward state and is idempotent (``alreadyProcessed``), which
        is how the official client reads this panel.  We never submit an
        invitation code, so nothing is mutated.
        """
        out: Dict[str, Any] = {}
        try:
            status, _h, data = http_request(
                self._url("points/first-login"), method="POST",
                headers=self._headers(session), body=b"{}",
                timeout=30, proxy=str(self.cfg.get("proxy") or ""))
            if status == 200:
                payload = json.loads(data.decode("utf-8"))
                _check_payload(status, payload, data, "/points/first-login")
                node = payload.get("data") or {}
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
        except UpstreamError as exc:
            # An expired session must reach the caller so it can re-login;
            # everything else degrades to a partial overview.
            if exc.status == 401:
                raise
            out["error"] = str(exc)
        except Exception as exc:                        # noqa: BLE001
            out["error"] = str(exc)
        try:
            status, _h, data = http_request(
                self._url("points/activation"), method="GET",
                headers=self._headers(session), timeout=20,
                proxy=str(self.cfg.get("proxy") or ""))
            if status == 200:
                payload = json.loads(data.decode("utf-8"))
                _check_payload(status, payload, data, "/points/activation")
                node = payload.get("data") or {}
                out["activated"] = node.get("activated")
                out["applied_invitation_code"] = node.get("appliedInvitationCode")
        except UpstreamError as exc:
            if exc.status == 401:
                raise
            out.setdefault("error", str(exc))
        except Exception as exc:                        # noqa: BLE001
            out.setdefault("error", str(exc))
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
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload else None
        return http_request(
            self._url(suffix), method=method, headers=self._headers(session, extra_headers),
            body=body, timeout=float(self.cfg.get("timeout") or 1200),
            proxy=str(self.cfg.get("proxy") or ""))

    def stream(self, session: str, suffix: str, payload: Dict[str, Any],
               extra_headers: Optional[Dict[str, str]] = None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return open_stream(
            self._url(suffix), method="POST",
            headers=self._headers(session, extra_headers), body=body,
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
