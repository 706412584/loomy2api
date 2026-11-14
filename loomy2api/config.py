"""Configuration loading.

Precedence: environment variables > config.json > built-in defaults.
Paths in the config are resolved relative to the project root (the directory
holding config.json), so the project can live anywhere.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

from . import constants as C

__all__ = ["Config", "load_config", "PROJECT_ROOT"]

#: Repository root = parent of this package.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: Dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 17890,

    # upstreams -----------------------------------------------------------
    "upstream": C.DEFAULT_MODEL_BASE,
    "account_base": C.DEFAULT_ACCOUNT_BASE,
    "account_appid": C.DEFAULT_ACCOUNT_APPID,
    "access_key_id": "",          # empty → use the client-shipped constant
    "access_key_secret": "",
    "loomy_version": C.DEFAULT_LOOMY_VERSION,
    "proxy": "",                  # "" = direct; e.g. http://127.0.0.1:7877

    # gateway -------------------------------------------------------------
    "api_keys": [],               # [] = no auth; otherwise Bearer / x-api-key
    "default_model": C.DEFAULT_MODEL,
    "timeout": 1200,
    "request_purpose": "chat.message",
    "log_dir": "logs",
    "log_requests": True,
    "strip_model_prefix": True,   # accept "imodel/deepseek-v4-flash-0731"

    # account pool --------------------------------------------------------
    "accounts_file": "accounts.json",
    "strategy": "balance",        # balance | daily | permanent | round_robin | lru | weighted
    #: Retries for a request-level upstream failure (a bad window is ~6 s in
    #: practice).  With the backoff below, 5 tries wait 0.5+1+2+4 = 7.5 s
    #: before giving up, which rides out an observed burst; the per-request
    #: cost is only paid when the upstream is actually failing.
    "max_retries": 4,             # extra attempts on upstream/auth/quota errors
    "stream_retries": 2,          # extra attempts when a stream dies mid-flight
    #: The upstream's model gateway itself returns 502/504/500 in short bursts
    #: (a whole minute can be bad, then it clears).  Rotating accounts does not
    #: help — only waiting does — so a retry after an *upstream* (non-account)
    #: fault pauses first, otherwise every attempt lands inside the same bad
    #: window and the 502 is handed straight back to the client.  The pause
    #: doubles each time, capped at ``upstream_fault_backoff_max``.
    "upstream_fault_backoff": 0.5,
    "upstream_fault_backoff_max": 4.0,
    #: Abort a stream that makes **no progress at all** — not one SSE byte —
    #: for this long.  This is the real stall guard: a reasoning model that
    #: keeps streaming deltas (even pure ``reasoning_content`` with no answer
    #: yet) is making progress and must NOT be killed.  The old rule capped
    #: reasoning-only time at 240s, which cut GLM-5.3-Flash off mid-think and
    #: reached the client as "upstream stream interrupted".
    "stream_progress_stall_seconds": 600,
    #: Fallback for the value above (older configs only carry this one).
    "stream_stall_seconds": 180,
    "cooldown_seconds": 300,
    #: After the account service rejects the stored password (or throttles
    #: logins), don't attempt another password login for this long.  The
    #: existing session keeps working meanwhile; retrying only extends the
    #: upstream lockout ("密码错误次数过多，请10分钟后登录").
    "login_blocked_seconds": 600,
    #: The upstream answers a *read* (points/models) with a transient 401
    #: ("登录已失效") or "系统内部错误" when a burst of requests hits it — the
    #: session is fine.  Retry the read this many extra times (with
    #: ``auth_retry_pause`` between) before believing the session is dead, so a
    #: single blip does not kick off a re-login (and the "顶号" cascade it
    #: causes).  Must be >= 1 — see ``quota_auth_faults_before_relogin``.
    "auth_retry_attempts": 2,
    "auth_retry_pause": 1.5,
    #: How many *consecutive* 401s the read path must see before it drops the
    #: session and re-logs in.  Guards against the burst-invalidated session
    #: that the retries above cannot absorb.
    "quota_auth_faults_before_relogin": 2,
    #: When the stored password is rejected but the session still works, set a
    #: new password through that session automatically (and store it), so the
    #: account keeps renewing itself without manual intervention.  Rotating a
    #: password while someone else is logged in with the same account kicks
    #: that session ("顶号") — the mobile build defaults this to off.
    "auto_repair_password": True,
    #: Minimum hours between automatic password repairs for one account.  A
    #: repair rotates the upstream password and logs the account out on every
    #: other device, so this caps the damage a failing repair loop could do.
    "password_repair_interval_hours": 24,
    #: Verify every stored password once at startup and repair the rejected
    #: ones while their session still works.  The keeper only renews sessions
    #: that are about to expire, so without this an invalidated password goes
    #: unnoticed until the account dies at expiry.
    "password_audit_on_startup": True,
    #: Give password-less accounts a random password shortly before their
    #: session expires.  Same "顶号" trade-off as above; also defaults to off
    #: on mobile, where the account list is shared with the phone's own client.
    "auto_set_password": True,
    #: Never spend the permanent balance: only accounts that still hold part
    #: of today's daily grant are routed to.  The grant is lost at the cycle
    #: reset while the permanent balance carries over, so this maximises what
    #: is actually used — at the cost of failing once every grant is spent.
    "daily_only": False,
    #: Skip accounts the upstream has cut back to its free tier when routing.
    #: The upstream scopes its model catalogue per account and silently swaps
    #: in a free model for any id an account lacks, so a downgraded account
    #: answers every request — just with worse output and zero points spent.
    #: Excluding them keeps paid traffic on the accounts that still have the
    #: entitlements.  Turn this off to keep using the free tier as a fallback.
    "exclude_downgraded": True,
    #: Minutes between automatic model-entitlement probes.  Entitlements
    #: change without any traffic, so a list captured at startup goes stale:
    #: recovered accounts would never come back and newly downgraded ones
    #: would keep taking paid work.  0 disables the periodic probe.
    "model_probe_minutes": 60,
    "session_renew_before_days": 3,
    #: Upgrade a password-less account this many days before its session
    #: expires: set a password through the still-valid session, so it can
    #: re-login on its own from then on ("permanent").
    "password_set_before_days": 1,
    "quota_refresh_minutes": 30,
    "sessions_from_client": True, # also honour the desktop client's session
    "client_root": "",            # override the client's data dir (tests)

    # SMS registration (fysms 接码平台) -------------------------------------
    # Credentials are written here only when the user enters them in the
    # panel / CLI; the API never returns the password.
    "fysms_username": "",
    "fysms_password": "",
    "fysms_keyword": "讯飞",      # SMS title/keyword used to match the code
    "fysms_max_wait": 120,        # seconds to wait for one SMS
    #: The "lite" Android build ships without SMS registration; it sets this
    #: to false so the endpoint and the panel button disappear together.
    "sms_registration": True,

    # identity_mode: per_account → each account gets its own devid / campus id;
    #                client       → mirror the shipped client exactly.
    "identity_mode": "per_account",
}

ENV_MAP = {
    "LOOMY_HOST": "host",
    "LOOMY_PORT": "port",
    "LOOMY_UPSTREAM": "upstream",
    "LOOMY_ACCOUNT_BASE": "account_base",
    "LOOMY_AK_ID": "access_key_id",
    "LOOMY_AK_SECRET": "access_key_secret",
    "LOOMY_API_KEYS": "api_keys",
    "LOOMY_DEFAULT_MODEL": "default_model",
    "LOOMY_PROXY": "proxy",
    "LOOMY_ACCOUNTS_FILE": "accounts_file",
    "LOOMY_LOG_DIR": "log_dir",
    "LOOMY_STRATEGY": "strategy",
}


class Config(dict):
    """A dict with attribute access plus a few derived helpers."""

    def __getattr__(self, item: str) -> Any:          # pragma: no cover
        try:
            return self[item]
        except KeyError as exc:
            raise AttributeError(item) from exc

    # -- derived -------------------------------------------------------

    @property
    def ak_id(self) -> str:
        return str(self.get("access_key_id") or C.DEFAULT_ACCESS_KEY_ID)

    @property
    def ak_secret(self) -> str:
        return str(self.get("access_key_secret") or C.DEFAULT_ACCESS_KEY_SECRET)

    @property
    def api_keys(self) -> List[str]:
        keys = self.get("api_keys") or []
        return [str(k) for k in keys if k]

    def path(self, key: str, default: str = "") -> Path:
        raw = str(self.get(key) or default)
        p = Path(raw)
        return p if p.is_absolute() else PROJECT_ROOT / p


def _coerce(cfg: Dict[str, Any], key: str, value: str) -> Any:
    base = DEFAULTS.get(key)
    if key in ("host", "upstream", "proxy") or isinstance(base, str):
        return value
    if isinstance(base, int):
        try:
            return int(value)
        except ValueError:
            return base
    if isinstance(base, list) and key == "api_keys":
        return [v.strip() for v in value.split(",") if v.strip()]
    return value


def load_config(path: str | os.PathLike | None = None) -> Config:
    cfg = Config(DEFAULTS)

    config_path = Path(path) if path else PROJECT_ROOT / "config.json"
    if config_path.exists():
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as exc:                      # noqa: BLE001
            raise RuntimeError(f"config.json 解析失败 / parse error: {exc}") from exc
        if isinstance(raw, dict):
            cfg.update({k: v for k, v in raw.items() if not k.startswith("_")})

    for env_key, cfg_key in ENV_MAP.items():
        value = os.environ.get(env_key)
        if value:
            cfg[cfg_key] = _coerce(cfg, cfg_key, value)

    return cfg
