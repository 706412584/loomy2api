"""Web control panel: HTML page + JSON API.

The page is a single self-contained HTML file (no CDN, no build step) served at
``/panel``; the JSON API lives under ``/api/panel/*`` and reuses the gateway's
API-key gate when ``api_keys`` is configured.

Endpoints
---------
``GET  /panel``                       the page
``GET  /api/panel/state``             accounts + quota + totals (``?refresh=1``
                                      forces a quota refresh)
``POST /api/panel/refresh``           refresh every account's quota
``POST /api/panel/accounts``          add an account (optionally log in now)
``POST /api/panel/accounts/update``   patch loginid / password / enabled
``POST /api/panel/accounts/remove``   delete an account
``POST /api/panel/accounts/renew``    force re-login + quota refresh
``POST /api/panel/accounts/check``    health-check the pool, repairing what
                                      can be repaired (``force`` = re-login
                                      every account, ``names`` = subset)
``POST /api/panel/accounts/identity`` inspect / regenerate the account identity
``GET  /api/panel/logs``              tail of the gateway log
"""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait as wait_futures
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import constants as C
from .account import Account, AccountError
from .model_events import is_free
from .pool import PoolError
from .upstream import UpstreamError

__all__ = ["Panel"]

WEB_DIR = Path(__file__).resolve().parent / "web"
PANEL_HTML = WEB_DIR / "index.html"

#: points multiplier shipped inside the upstream model ``name``, e.g.
#: ``DeepSeek V4 Flash 0731（x3.0）`` / ``GLM 5.3 Flash(x0.8)``
_MULTIPLIER_RE = re.compile(r"[（(]\s*(x?\s*[\d.]+)\s*[）)]")


def parse_multiplier(name: str) -> Optional[float]:
    """Points multiplier for a model, or ``None`` when the upstream omits it."""
    match = _MULTIPLIER_RE.search(str(name or ""))
    if not match:
        return None
    try:
        return float(match.group(1).replace("x", "").replace(" ", ""))
    except ValueError:
        return None


def pretty_model_name(name: str) -> str:
    """Strip the multiplier suffix so the table can show it in its own column."""
    return _MULTIPLIER_RE.sub("", str(name or "")).strip(" ·-") or str(name or "")


#: Hidden phone number for the panel: 138****0000
def mask_phone(value: str) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) < 7:
        return str(value or "")
    return f"{digits[:3]}****{digits[-4:]}"


#: The 8 onboarding tasks the mobile client submits one by one via
#: ``POST /onboarding/tasks/complete`` — same keys / order / rewards as the
#: Android app (q1/Y; static list).
ONBOARDING_TASKS = [
    ("first_message", "首次对话", 500),
    ("pick_skill", "选择技能", 1000),
    ("generate_ppt", "生成 PPT", 1500),
    ("set_schedule", "设置日程", 1000),
    ("install_skill", "安装技能", 1500),
    ("configure_remote", "配置远程", 1000),
    ("create_soul", "创建人格", 1500),
    ("share_soul", "分享人格", 2000),
]


class Panel:
    """Stateful facade over the account pool for the web UI."""

    def __init__(self, gateway):
        self.gw = gateway
        self.log = gateway.log
        # Serialize paid SMS-registration batches (see register_sms).
        self._sms_lock = threading.Lock()
        # Quota refreshes continue after the HTTP response: on a phone a large
        # pool would otherwise make every panel read wait for N round-trips.
        self._refresh_pool = ThreadPoolExecutor(max_workers=8,
                                                thread_name_prefix="quota")
        self._refreshing: Dict[str, Any] = {}
        self._refresh_lock = threading.Lock()

    # ------------------------------------------------------------ helpers

    def shutdown(self) -> None:
        """Stop the background quota refresher (called on gateway shutdown)."""
        self._refresh_pool.shutdown(wait=False, cancel_futures=True)

    @property
    def pool(self):
        return self.gw.pool

    def html(self) -> bytes:
        try:
            return PANEL_HTML.read_bytes()
        except OSError:                                  # pragma: no cover
            return b"<h1>loomy2api</h1><p>panel asset missing</p>"

    def _account_view(self, acc: Account) -> Dict[str, Any]:
        view = acc.public_dict()
        view["loginid_masked"] = mask_phone(acc.loginid)
        view["points_used"] = acc.points_used
        view["mode"] = "password" if acc.has_password else (
            "session" if acc.session else "empty")
        return view

    def _resolve_account(self, name: Any) -> Account:
        """Find an account by name, reloading the pool once on a miss.

        The in-memory pool can lag behind ``accounts.json`` (an external
        edit, or a request that raced the gateway's startup).  Without this,
        the first click after such a change answered "no such account" and the
        second one worked — the reload made the account appear.
        """
        wanted = str(name or "").strip()
        acc = self.pool.get(wanted) if wanted else None
        if acc is None and wanted:
            try:
                self.pool.load()
            except Exception as exc:                     # noqa: BLE001
                self.log(f"[panel] 重载账号池失败：{exc}")
            acc = self.pool.get(wanted)
        if acc is None:
            raise PoolError(f"没有名为 {wanted or '(空)'} 的账号 / "
                            f"no such account: {wanted or '(empty)'}")
        return acc

    def _quota_stale(self, acc: Account) -> bool:
        minutes = float(self.gw.cfg.get("quota_refresh_minutes") or 30)
        # Key off the last *attempt*, not the last success: a failing account
        # would otherwise look stale forever and be re-probed (and possibly
        # re-logged in) on every 10 s poll.
        last = max(acc.quota_updated_at or 0, int(acc.quota_checked_at or 0))
        return (time.time() - last) > minutes * 60

    def _refresh_all(self, only_stale: bool = True,
                     *, wait: bool = True) -> None:
        """Refresh quota for the accounts that need it, concurrently.

        The upstream call is a network round-trip with a 30 s timeout, so doing
        it serially made a cold panel load take 30 s *per unreachable account*
        (on a phone that reads as "stuck loading accounts").  They are
        independent, so fan them out and wait only as long as the slowest one.

        ``wait=False`` (used by the regular panel reads) only *schedules* the
        refresh and returns the cached values immediately. An account already
        being refreshed is not queued twice, so the 10 s UI poll cannot pile
        up work.
        """
        # A flagged (banned) account is left alone: the upstream has disabled
        # it, so re-reading its quota only re-confirms the ban and logs noise.
        pending = [a for a in self.pool.accounts
                   if a.session_valid and not a.banned
                   and not (only_stale and not self._quota_stale(a))]
        if not pending:
            return

        futures = []
        with self._refresh_lock:
            for acc in pending:
                fut = self._refreshing.get(acc.name)
                if fut is None or fut.done():
                    fut = self._refresh_pool.submit(self._refresh_one, acc)
                    self._refreshing[acc.name] = fut
                futures.append(fut)

        if wait:
            wait_futures(futures)
            self.pool.save()

    def _refresh_one(self, acc: Account) -> None:
        try:
            self.pool.refresh_quota(acc)
            # Persist without blocking: save() is throttled to one write / 5 s.
            self.pool.save()
        except Exception as exc:                         # noqa: BLE001
            self.log(f"[panel] 刷新 {acc.name} 额度失败：{exc}")

    # --------------------------------------------------------------- state

    def state(self, *, refresh: bool = False, wait: bool = True) -> Dict[str, Any]:
        self._refresh_all(only_stale=not refresh, wait=wait)
        accounts = [self._account_view(a) for a in self.pool.accounts]

        # one account can hold two sessions (ours + the client's) — count once
        seen: Dict[str, int] = {}
        for acc in self.pool.accounts:
            if isinstance(acc.available, int):
                key = acc.userid or acc.name
                seen[key] = max(seen.get(key, 0), acc.available)

        return {
            "ok": True,
            "now": int(time.time()),
            "totals": {
                "accounts": len(accounts),
                "usable": len(self.pool.usable()),
                "available": sum(seen.values()),
                "unique_accounts": len(seen),
                "requests": sum(a["requests"] for a in accounts),
                "points_used": sum(a["points_used"] for a in accounts),
                # gateway-wide counters (survive account edits; reset on restart)
                "total_requests": self.gw.totals.get("requests", 0),
                "tokens": self.gw.totals.get("tokens", 0),
                "failures": self.gw.totals.get("failures", 0),
                # The ids the pool as a whole can serve.  The UI compares each
                # account against this to spot a downgrade: an account missing
                # any paid model here has been cut back to the free tier.
                #
                # Empty until a real probe has run.  ``catalogue()`` answers a
                # built-in guess before that, and diffing accounts against a
                # guess marked the whole pool as downgraded on the first page
                # load after every restart.
                "model_ids": ([str(m.get("id")) for m in self.gw.catalogue()
                               if m.get("id")]
                              if self.gw.models_confirmed else []),
            },
            "config": {
                "upstream": self.gw.cfg["upstream"],
                "strategy": self.gw.cfg.get("strategy"),
                "daily_only": self.pool.daily_only(),
                "exclude_downgraded": self.pool.exclude_downgraded(),
                "default_model": self.gw.cfg.get("default_model"),
                "identity_mode": self.gw.cfg.get("identity_mode"),
                "models": len(self.gw.catalogue()),
                "auth_required": bool(self.gw.cfg.api_keys),
                "quota_refresh_minutes": self.gw.cfg.get("quota_refresh_minutes"),
                "host": self.gw.cfg.get("host"),
                "port": self.gw.cfg.get("port"),
                "api_keys": list(self.gw.cfg.api_keys),
                "proxy": self.gw.cfg.get("proxy") or "",
                "log_requests": bool(self.gw.cfg.get("log_requests", True)),
                "timeout": self.gw.cfg.get("timeout"),
                "max_retries": self.gw.cfg.get("max_retries"),
                "cooldown_seconds": self.gw.cfg.get("cooldown_seconds"),
                # stream watchdog: seconds without ANY upstream byte before a
                # stream is aborted (a long reasoning model is not a stall).
                "stream_progress_stall_seconds": self.gw.cfg.get(
                    "stream_progress_stall_seconds"),
                "stream_stall_seconds": self.gw.cfg.get("stream_stall_seconds"),
                "session_renew_before_days": self.gw.cfg.get("session_renew_before_days"),
                "password_set_before_days": self.gw.cfg.get("password_set_before_days"),
                "auto_set_password": bool(self.gw.cfg.get("auto_set_password", True)),
                "auto_repair_password": bool(self.gw.cfg.get("auto_repair_password", True)),
                "sessions_from_client": bool(self.gw.cfg.get("sessions_from_client", True)),
                # fysms 接码: expose the username and whether a password is
                # stored — never the password itself.
                "fysms_username": self.gw.cfg.get("fysms_username") or "",
                "fysms_password_set": bool(self.gw.cfg.get("fysms_password")),
                "fysms_keyword": self.gw.cfg.get("fysms_keyword") or "讯飞",
                "fysms_max_wait": self.gw.cfg.get("fysms_max_wait") or 120,
                "sms_registration": bool(self.gw.cfg.get("sms_registration", True)),
            },
            "accounts": accounts,
        }

    def update_config(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Persist the subset of settings the mobile UI exposes.

        Only whitelisted keys are written, so a client cannot rewrite the
        upstream endpoints or the accounts file path through this endpoint.
        ``strategy``/``default_model`` apply to the live pool immediately;
        ``host``/``port`` need a restart (the server is already bound).
        """
        editable = {
            "strategy", "default_model", "api_keys", "proxy", "log_requests",
            "timeout", "max_retries", "cooldown_seconds",
            "stream_progress_stall_seconds", "stream_stall_seconds",
            "session_renew_before_days", "password_set_before_days",
            "quota_refresh_minutes", "sessions_from_client", "identity_mode",
            "daily_only", "exclude_downgraded",
            "auto_repair_password", "auto_set_password",
            "fysms_username", "fysms_password", "fysms_keyword",
            "fysms_max_wait",
        }
        changes = {k: v for k, v in payload.items() if k in editable}

        if "strategy" in changes:
            strategy = str(changes["strategy"])
            if strategy not in ("balance", "round_robin", "lru", "weighted",
                                "daily", "permanent"):
                raise PoolError(f"未知调度策略 / unknown strategy: {strategy}")
        if "api_keys" in changes:
            raw = changes["api_keys"]
            if isinstance(raw, str):
                # tolerate a JSON-encoded list (some clients stringify it) as
                # well as the plain "a,b" form; an empty value means "no auth"
                text = raw.strip()
                if text.startswith("["):
                    try:
                        import json as _json
                        raw = _json.loads(text)
                    except Exception:                     # noqa: BLE001
                        raw = [v for v in text.strip("[]").split(",")]
                else:
                    raw = text.split(",")
            changes["api_keys"] = [str(v).strip() for v in (raw or []) if str(v).strip()]
        for flag in ("log_requests", "sessions_from_client", "daily_only",
                     "exclude_downgraded", "auto_repair_password",
                     "auto_set_password"):
            if flag in changes:
                # ``bool("false")`` is True — a string from the UI or an env
                # var would silently lock the switch on.
                value = changes[flag]
                if isinstance(value, str):
                    value = value.strip().lower() not in ("", "0", "false", "no", "off")
                changes[flag] = bool(value)
        for num_key in ("timeout", "max_retries", "cooldown_seconds",
                        "stream_progress_stall_seconds", "stream_stall_seconds",
                        "session_renew_before_days", "password_set_before_days",
                        "quota_refresh_minutes", "fysms_max_wait"):
            if num_key in changes:
                try:
                    changes[num_key] = int(changes[num_key])
                except (TypeError, ValueError):
                    raise PoolError(f"{num_key} 必须是整数") from None
        # A stall guard below the platform's own timeouts would reintroduce the
        # bug this key exists to fix, so keep it sane.
        for guard in ("stream_progress_stall_seconds", "stream_stall_seconds"):
            if guard in changes and changes[guard] < 30:
                raise PoolError(f"{guard} 至少 30 秒 / must be at least 30s")

        self.gw.cfg.update(changes)
        self._write_config(changes)
        self.log(f"[panel] 配置已更新：{', '.join(sorted(changes)) or '（无变化）'}")
        return {"ok": True, "changed": sorted(changes), "state": self.state()}

    def _write_config(self, changes: Dict[str, Any]) -> None:
        """Merge changes into the on-disk config.json (best effort).

        The file lives next to accounts.json (both are per-install state); the
        in-memory config is already updated, so a write failure only means the
        change is not persisted across a restart.
        """
        import json as _json
        cfg_file = Path(self.gw.cfg.get("_config_file")
                        or self.pool._file.parent / "config.json")
        try:
            existing = {}
            if cfg_file.exists():
                existing = _json.loads(cfg_file.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
            existing.update(changes)
            cfg_file.write_text(_json.dumps(existing, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        except Exception as exc:                          # noqa: BLE001
            self.log(f"[panel] 写入 config.json 失败：{exc}")

    def export_accounts(self) -> Dict[str, Any]:
        """Accounts in the same shape the import endpoint accepts.

        Sessions are included because this is a local-only export; callers are
        expected to treat the result as a secret (the mobile UI warns).
        """
        items = []
        for acc in self.pool.accounts:
            if not acc.persist:
                continue
            out = {
                "name": acc.name,
                "loginid": acc.loginid,
                "session": acc.session,
                "userid": acc.userid,
                "enabled": acc.enabled,
                "expireAt": acc.expire_at,
                "obtainedAt": acc.obtained_at,
                "identity": acc.identity,
            }
            if acc.password:
                out["password"] = acc.password
            if acc.password_set_at:
                out["passwordSetAt"] = acc.password_set_at
            if acc.password_verified_at:
                out["passwordVerifiedAt"] = acc.password_verified_at
            items.append(out)
        return {"ok": True, "version": 1, "exported_at": int(time.time()),
                "accounts": items}

    # --------------------------------------------------------------- writes

    def refresh(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._refresh_all(only_stale=False, wait=True)
        self.pool.save()
        return self.state()

    def add_account(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        loginid = str(payload.get("loginid") or "").strip()
        password = str(payload.get("password") or "")
        session = str(payload.get("session") or "").strip()
        if not name:
            raise PoolError("账号名不能为空 / name is required")
        if not loginid and not session:
            raise PoolError("需要手机号和密码，或直接提供一个 session / "
                            "provide phone+password, or a session")
        if loginid and not password and not session:
            raise PoolError("只给手机号时还需要密码 / password required with phone")

        existed = self.pool.get(name) is not None
        acc = self.pool.add_account(name, loginid=loginid, password=password,
                                    session=session)
        result: Dict[str, Any] = {"ok": True, "name": acc.name,
                                  "identity": acc.identity_view()}
        if loginid and password and payload.get("login", True):
            try:
                self.pool.ensure_session(acc, force=True)
                self.pool.refresh_quota(acc)
                result["logged_in"] = True
                result["userid"] = acc.userid
            except (AccountError, PoolError) as exc:
                result["logged_in"] = False
                result["error"] = str(exc)
                # Roll the new account back on a failed first login.  Leaving
                # it in the pool with no session made a retry fail with
                # "account already exists", so users had to tap "add" several
                # times (and the web panel had the same problem).
                if not existed and not acc.session_valid:
                    self.pool.remove_account(name)
                    result["ok"] = False
                    result["rolled_back"] = True
                    self.log(f"[panel] 添加 {name} 登录失败，已回滚（{exc}）")
                    result["state"] = self.state()
                    return result
            self.pool.save(force=True)
        self.pool.save(force=True)
        result["state"] = self.state()
        return result

    def import_accounts(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Bulk import from the Android app's export file (accounts array).

        Accepts either ``{"accounts": [...]}`` or a bare list.  Fields mirror
        the export: name/loginid/session/userid/expireAt/obtainedAt/identity.
        Skips accounts whose name already exists (``skip_existing``), otherwise
        updates the session of an existing entry.  ``expireAt``/``obtainedAt``
        are normalised: the export stores milliseconds, the pool wants seconds.
        """
        items = payload.get("accounts")
        if not isinstance(items, list):
            if isinstance(payload.get("list"), list):
                items = payload["list"]
            else:
                raise PoolError("需要 accounts 数组 / expected {accounts: [...]}")

        mode = str(payload.get("mode") or "skip")     # skip | update
        added, updated, skipped = [], [], []
        #: name → why it was skipped, so the UI can explain instead of
        #: silently reporting "0 added, 0 updated".
        reasons: Dict[str, str] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            raw = dict(item)
            for key in ("expireAt", "obtainedAt"):
                try:
                    v = int(raw.get(key) or 0)
                except (TypeError, ValueError):
                    v = 0
                if v > 10**11:                        # ms → s
                    raw[key] = v // 1000
            name = str(raw.get("name") or raw.get("loginid") or "").strip()
            session = str(raw.get("session") or "").strip()
            if not name or not session:
                label = name or "<unnamed>"
                skipped.append(label)
                reasons[label] = ("缺少 session（该条目无法用于登录；"
                                  "请在原项目重新导出含 session 的 accounts.json）"
                                  if name else "缺少 name 和 session")
                continue
            existing = self.pool.get(name)
            if existing is not None:
                if mode == "update":
                    existing.session = session
                    existing.userid = str(raw.get("userid") or existing.userid)
                    existing.expire_at = int(raw.get("expireAt") or 0)
                    existing.obtained_at = int(raw.get("obtainedAt") or 0)
                    if raw.get("identity"):
                        existing.identity = dict(raw["identity"])
                    if raw.get("password"):
                        existing.password = str(raw["password"])
                        existing.password_set_at = int(
                            raw.get("passwordSetAt") or raw.get("password_set_at") or 0)
                        existing.password_verified_at = int(
                            raw.get("passwordVerifiedAt")
                            or raw.get("password_verified_at") or 0)
                    updated.append(name)
                else:
                    skipped.append(name)
                    reasons[name] = "账号已存在（mode=skip）"
                continue
            self.pool.add_account(
                name, loginid=str(raw.get("loginid") or ""),
                password=str(raw.get("password") or ""),
                session=session, userid=str(raw.get("userid") or ""),
                identity=dict(raw["identity"]) if raw.get("identity") else None)
            acc = self.pool.get(name)
            if acc is not None and raw.get("expireAt"):
                acc.expire_at = int(raw["expireAt"])
            if acc is not None and raw.get("obtainedAt"):
                acc.obtained_at = int(raw["obtainedAt"])
            if acc is not None and raw.get("password"):
                acc.password_set_at = int(
                    raw.get("passwordSetAt") or raw.get("password_set_at") or 0)
                acc.password_verified_at = int(
                    raw.get("passwordVerifiedAt")
                    or raw.get("password_verified_at") or 0)
            added.append(name)

        # Never let a fully-rejected import rewrite the pool file: if nothing
        # was added or updated, the in-memory pool is unchanged anyway, and a
        # save here would only risk clobbering a file the user just edited.
        if added or updated:
            self.pool.save(force=True)
        else:
            self.log("[panel] 导入未产生任何变更，跳过写盘")
        self.log(f"[panel] 批量导入：新增 {len(added)} 更新 {len(updated)} "
                 f"跳过 {len(skipped)}")
        if skipped and not added and not updated:
            detail = next(iter(reasons.values()), "")
            return {"ok": True, "added": added, "updated": updated,
                    "skipped": skipped, "reasons": reasons,
                    "message": f"{len(skipped)} 条被跳过：{detail}",
                    "state": self.state()}
        return {"ok": True, "added": added, "updated": updated,
                "skipped": skipped, "reasons": reasons, "state": self.state()}

    def sms_send(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Step 1 of SMS login: send the code, hand back the ``msgid``."""
        phone = str(payload.get("loginid") or payload.get("phone") or "").strip()
        if not phone:
            raise PoolError("需要手机号 / phone is required")
        result = self.pool.client.send_sms_code(phone)
        node = result.get("data") if isinstance(result.get("data"), dict) else result
        msgid = str((node or {}).get("msgid") or "")
        if not msgid:
            raise PoolError(f"上游未返回 msgid / no msgid returned: {result}")
        self.log(f"[panel] 已向 {mask_phone(phone)} 下发验证码")
        return {"ok": True, "msgid": msgid, "phone": phone}

    def sms_login(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Step 2 of SMS login: exchange the code for a session."""
        name = str(payload.get("name") or "").strip()
        phone = str(payload.get("loginid") or payload.get("phone") or "").strip()
        code = str(payload.get("code") or "").strip()
        msgid = str(payload.get("msgid") or "").strip()
        if not phone or not code or not msgid:
            raise PoolError("需要手机号、验证码和 msgid / phone, code and msgid required")
        if not name:
            name = phone

        got = self.pool.client.login_by_sms(phone, code, msgid)
        acc = self.pool.get(name)
        if acc is None:
            acc = self.pool.add_account(name, loginid=phone)
        else:
            # Re-login of an *existing* account: the old session's failures and
            # the password lockout refer to a credential that is about to be
            # replaced (or was already rejected), so carrying them over would
            # keep the account benched right after a successful login.
            acc.login_blocked_until = 0.0
            acc.cooldown_until = 0.0
            acc.last_error = ""
            # A successful SMS login is proof the account works again; a stale
            # ban flag would keep it out of routing no matter what.
            acc.banned = False
            acc.banned_at = 0
            if phone and not acc.loginid:
                acc.loginid = phone
        acc.session = got["session"]
        acc.userid = got.get("userid", "")
        acc.obtained_at = int(time.time())
        acc.expire_at = acc.obtained_at + C.SESSION_EXPIRE_SECONDS
        try:
            self.pool.refresh_quota(acc)
        except Exception as exc:                         # noqa: BLE001
            self.log(f"[panel] {name} 额度刷新失败：{exc}")
        self.pool.save(force=True)
        self.log(f"[panel] {name} 验证码登录成功 userid={acc.userid}")
        return {"ok": True, "name": acc.name, "userid": acc.userid,
                "state": self.state()}

    def set_password(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Set one account's upstream login password via its live session.

        This is the "upgrade to permanent" action: the account service accepts
        ``/userinfo/update/pwd`` with nothing but a valid session, so an
        account added through SMS can become self-renewing.  An empty
        ``password`` means "generate a strong one"; the chosen value is
        returned so the user can write it down.
        """
        name = str(payload.get("name") or "").strip()
        acc = self._resolve_account(name)
        password = str(payload.get("password") or "").strip()
        if password:
            self._validate_password(password)
        pwd = self.pool.set_account_password(acc, password)
        self.log(f"[panel] {acc.name} 已设置登录密码并验证通过")
        return {"ok": True, "name": acc.name, "password": pwd,
                "verified": True, "generated": not bool(password),
                "state": self.state()}

    @staticmethod
    def _validate_password(password: str) -> None:
        has_letter = any(ch.isalpha() for ch in password)
        has_digit = any(ch.isdigit() for ch in password)
        if len(password) < 8 or not (has_letter and has_digit):
            raise PoolError("密码至少 8 位且需同时包含字母和数字 / "
                            "password needs 8+ chars with letters and digits")

    def show_password(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Return the stored login password for one account.

        Passwords live in ``accounts.json``; the regular state response never
        carries them (the panel can be bound to 0.0.0.0).  The UI asks for one
        explicitly when the user taps "显示密码", so the secret only travels on
        that request.
        """
        acc = self._resolve_account(str(payload.get("name") or "").strip())
        if not acc.password:
            return {"ok": False, "name": acc.name,
                    "error": "该账号还没有保存密码"}
        return {"ok": True, "name": acc.name, "password": acc.password,
                "password_set_at": acc.password_set_at}

    def set_passwords(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """One-click: give every password-less account a generated password.

        Accounts are independent, so the upstream calls run concurrently.  An
        account without a usable session is reported as skipped — it cannot be
        upgraded until it logs in again.
        """
        payload = payload or {}
        manual = str(payload.get("password") or "").strip()
        if manual:
            self._validate_password(manual)
        only = {str(n) for n in (payload.get("names") or [])}
        # "permanent" means the password was proven by a real login; an account
        # with a stored but unverified password is still a target (it is first
        # verified, and only rotated when that fails).
        skipped = [a.name for a in self.pool.accounts if a.enabled and a.permanent]
        targets = [a for a in self.pool.accounts
                   if a.enabled and not a.permanent
                   and (not only or a.name in only)]
        results: list = []
        if targets:
            def upgrade(acc: Account) -> Dict[str, Any]:
                """Upgrade one account; never raises (the batch must finish)."""
                if not acc.session_valid:
                    return {"name": acc.name, "ok": False,
                            "error": "没有有效 session，请先续期或验证码登录"}
                # A stored password that was never proven: test it first and
                # keep it when it works, instead of rotating blindly.
                if acc.password and not acc.password_verified_at:
                    if self.pool.verify_password(acc):
                        return {"name": acc.name, "ok": True,
                                "password": acc.password, "verified": True}
                # A manual password is an explicit request to (re)set it, so
                # skip the "already has one upstream" guard in that case.
                if not manual:
                    try:
                        if self.pool.client.password_status(
                                acc.session, acc.identity or None):
                            return {"name": acc.name, "ok": False,
                                    "skipped": True,
                                    "error": "上游已有密码，跳过（如需更换请单个重设）"}
                    except Exception:                    # noqa: BLE001
                        pass                             # offline: proceed
                last: Optional[Exception] = None
                # Upstream 5xx / timeouts are transient; retry once so one
                # flaky account does not abort the whole batch.
                for attempt in range(2):
                    try:
                        pwd = self.pool.set_account_password(acc, manual)
                        return {"name": acc.name, "ok": True, "password": pwd,
                                "verified": True}
                    except AccountError as exc:
                        last = exc
                        transient = exc.retryable or str(exc.code).startswith("HTTP_5")
                        if attempt == 0 and transient:
                            time.sleep(0.8)
                            continue
                        break
                    except Exception as exc:             # noqa: BLE001
                        last = exc
                        break
                return {"name": acc.name, "ok": False,
                        "error": str(last)[:200] if last else "设置失败"}

            with ThreadPoolExecutor(max_workers=min(4, len(targets))) as pool:
                results = list(pool.map(upgrade, targets))
            self.pool.save(force=True)
        ok = sum(1 for r in results if r.get("ok"))
        failed = sum(1 for r in results if not r.get("ok") and not r.get("skipped"))
        self.log(f"[panel] 一键设置密码：成功 {ok}/{len(results)}，失败 {failed}"
                 f"（跳过 {len(skipped)} 个已有密码）")
        return {"ok": True, "results": results, "done": ok,
                "failed": failed, "total": len(results), "skipped": skipped,
                "state": self.state()}

    # ------------------------------------------------------ SMS registration

    def _fysms_credentials(self, payload: Dict[str, Any]) -> tuple:
        """Use the submitted credentials, else the stored ones.

        A successful login with newly entered credentials persists them to
        config.json (the reference script did the same), so the user only has
        to type them once.
        """
        username = str(payload.get("fysms_username") or "").strip()
        password = str(payload.get("fysms_password") or "")
        if not username or not password:
            username = str(self.gw.cfg.get("fysms_username") or "").strip()
            password = str(self.gw.cfg.get("fysms_password") or "")
        if not username or not password:
            raise PoolError("需要 fysms 账号和密码 / fysms username and password required")
        return username, password

    def _remember_fysms(self, username: str, password: str) -> None:
        changes = {}
        if self.gw.cfg.get("fysms_username") != username:
            changes["fysms_username"] = username
        if self.gw.cfg.get("fysms_password") != password:
            changes["fysms_password"] = password
        if changes:
            self.gw.cfg.update(changes)
            self._write_config(changes)

    def fysms_balance(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Read-only: login + balance. Takes no number, spends no money."""
        if not self.gw.cfg.get("sms_registration", True):
            raise PoolError("此版本未包含接码注册 / SMS registration is disabled "
                            "in this build")
        username, password = self._fysms_credentials(payload or {})
        from .fysms import CODE_PRICE
        from .smsreg import fysms_status

        status = fysms_status(username, password,
                              str(self.gw.cfg.get("fysms_base") or ""))
        self._remember_fysms(username, password)
        balance = status.get("balance")
        result = {
            "ok": True,
            "username": username,
            "userid": status.get("userid"),
            "balance": balance,
            "price": CODE_PRICE,
            "affordable": (int(balance // CODE_PRICE) if isinstance(balance, (int, float))
                           else None),
            "password_saved": bool(self.gw.cfg.get("fysms_password")),
        }
        self.log(f"[panel] fysms 余额：{balance} 元（{username}）")
        return result

    def register_sms(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self.gw.cfg.get("sms_registration", True):
            raise PoolError("此版本未包含接码注册 / SMS registration is disabled "
                            "in this build")
        """Take a number, log in by SMS and upgrade it to a permanent account.

        This **spends money** (one code per number), so it refuses to run
        without an explicit ``confirm: true``.  Requests are serialized with a
        lock: two concurrent batches would both take numbers and could trip
        the platform's anti-abuse rules.
        """
        if not payload.get("confirm"):
            raise PoolError(
                "接码注册会按号码收费，需要 confirm=true 确认 / "
                "this spends money; pass confirm=true to proceed")
        username, password = self._fysms_credentials(payload)
        try:
            count = int(payload.get("count") or 1)
        except (TypeError, ValueError):
            raise PoolError("数量必须是整数") from None
        if count < 1 or count > 20:
            raise PoolError("一次最多注册 20 个 / count must be between 1 and 20")

        manual = str(payload.get("password") or "").strip()
        if manual:
            self._validate_password(manual)
        keyword = str(payload.get("keyword") or
                      self.gw.cfg.get("fysms_keyword") or "讯飞").strip()
        try:
            max_wait = int(payload.get("max_wait") or
                           self.gw.cfg.get("fysms_max_wait") or 120)
        except (TypeError, ValueError):
            max_wait = 120
        max_wait = max(30, min(max_wait, 300))

        # Invitation donors: existing accounts whose spare codes the new
        # numbers get bound to.  Capped at five because each bound pair costs
        # an upstream activation round-trip per new number.
        raw_targets = payload.get("invite_targets") or []
        if not isinstance(raw_targets, list):
            raise PoolError("invite_targets 必须是账号名数组")
        invite_targets = [str(t).strip() for t in raw_targets if str(t or "").strip()]
        if len(invite_targets) > 5:
            raise PoolError("最多选择 5 个绑定邀请码的账号 / at most 5 inviters")

        from .fysms import CODE_PRICE
        from .smsreg import collect_invite_codes, register_batch

        # Resolve the deliverable codes *before* spending: if none of the
        # selected accounts can hand out a code, fail now instead of buying
        # numbers that then cannot be bound.
        if invite_targets and not collect_invite_codes(self.gw, invite_targets):
            raise PoolError("所选账号都没有可用邀请码，请重新选择 / "
                            "none of the selected accounts has a spare invite code")

        if not self._sms_lock.acquire(blocking=False):
            raise PoolError("已有接码注册任务在进行 / another SMS batch is running")
        try:
            self.log(f"[panel] 接码注册开始：{count} 个（标题 {keyword}，"
                     f"每号约 {CODE_PRICE} 元）"
                     + (f"，随机绑定邀请码（{len(invite_targets)} 个候选账号）"
                        if invite_targets else ""))
            summary = register_batch(
                self.gw, count,
                username=username, password=password,
                keyword=keyword, max_wait=max_wait,
                account_password=manual,
                checkin=bool(payload.get("checkin")),
                onboarding=bool(payload.get("onboarding")),
                invite_targets=invite_targets,
            )
            self._remember_fysms(username, password)
        finally:
            self._sms_lock.release()

        # Never leak the fysms password back to the client.
        summary["username"] = username
        summary["state"] = self.state()
        self.log(f"[panel] 接码注册完成：成功 {summary['done']}/{summary['requested']}，"
                 f"余额 {summary.get('balance_before')} → {summary.get('balance_after')}")
        return summary

    def update_account(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        fields: Dict[str, Any] = {}
        if "loginid" in payload:
            fields["loginid"] = payload["loginid"]
        if payload.get("password"):
            fields["password"] = payload["password"]
        if "enabled" in payload:
            fields["enabled"] = bool(payload["enabled"])
        acc = self.pool.update_account(name, **fields)
        if payload.get("login") and acc.loginid and acc.password:
            try:
                self.pool.ensure_session(acc, force=True)
                self.pool.refresh_quota(acc)
            except (AccountError, PoolError) as exc:
                self.log(f"[panel] {name} 重新登录失败：{exc}")
            self.pool.save(force=True)
        return {"ok": True, "name": acc.name, "state": self.state()}

    def remove_account(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        removed = self.pool.remove_account(name)
        if not removed:
            raise PoolError(f"没有名为 {name} 的账号 / no such account: {name}")
        self.log(f"[panel] 已删除账号 {name}")
        return {"ok": True, "removed": name, "state": self.state()}

    def check_accounts(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Health-check (and repair) the pool; ``force`` re-logs everyone.

        Without ``force`` only accounts that look broken are touched: an
        invalid/missing session, an expired one, or one the upstream rejects.
        ``names`` limits the run to specific accounts.
        """
        payload = payload or {}
        force = bool(payload.get("force"))
        names = payload.get("names")
        if isinstance(names, str):
            names = [names]
        self.log(f"[panel] 账号池检测开始（force={force}, "
                 f"{len(names) if names else '全部'}）")
        results = self.pool.check_all(force=force, names=names)
        ok = sum(1 for r in results if r.get("ok"))
        repaired = sum(1 for r in results if r.get("action") == "relogin"
                       and r.get("ok"))
        failed = [r for r in results if not r.get("ok")]
        self.log(f"[panel] 账号池检测完成：正常 {ok}/{len(results)}，"
                 f"重登修复 {repaired}，异常 {len(failed)}")
        return {"ok": True, "total": len(results), "healthy": ok,
                "repaired": repaired, "failed": len(failed),
                "results": results, "state": self.state()}

    def renew_account(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        acc = self._resolve_account(name)
        result: Dict[str, Any] = {"ok": True, "name": name}
        try:
            self.pool.ensure_session(acc, force=True)
            self.pool.refresh_quota(acc)
            acc.cooldown_until = 0.0
            result["userid"] = acc.userid
            result["session_days_left"] = acc.days_left
        except (AccountError, PoolError) as exc:
            result["ok"] = False
            result["error"] = str(exc)
        self.pool.save(force=True)
        result["state"] = self.state()
        return result

    def identity(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Inspect (``regenerate: false``) or rebind (``regenerate: true``)."""
        name = str(payload.get("name") or "").strip()
        acc = self._resolve_account(name)
        if payload.get("regenerate"):
            self.pool.rebind_identity(name)
            self.log(f"[panel] {name} 已重新绑定设备标识 "
                     f"devid={acc.identity.get('devid')}")
        elif not acc.identity:
            self.pool.client.ensure_identity(acc)
            self.pool.save(force=True)
        return {"ok": True, "name": name, "identity": acc.identity_view(),
                "state": self.state()}

    def models(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Catalogue with the points multiplier pulled out of the upstream name.

        ``refresh`` used to re-probe the whole pool synchronously: one panel
        click became N upstream round-trips (minutes on a phone) and benched
        every account whose session was rejected along the way.  It now just
        kicks the keeper, which probes on its own thread and cadence.
        """
        if (payload or {}).get("refresh"):
            try:
                self.pool.request_model_probe()
            except Exception as exc:                     # noqa: BLE001
                self.log(f"[panel] 请求刷新模型列表失败：{exc}")
        items = []
        for raw in self.gw.catalogue():
            name = str(raw.get("name") or raw.get("id") or "")
            caps = raw.get("capabilities") or {}
            items.append({
                "id": raw.get("id"),
                "name": pretty_model_name(name),
                "multiplier": parse_multiplier(name),
                "context_length": raw.get("context_length"),
                "max_output_tokens": raw.get("max_output_tokens"),
                "type": raw.get("type"),
                "reasoning": bool(caps.get("reasoning")),
                "vision": bool(caps.get("vision")),
                "function_calling": bool(caps.get("function_calling")),
                "input_modalities": caps.get("input_modalities") or [],
                "is_default": bool(raw.get("is_default_model")),
                "default_model": raw.get("id") == self.gw.cfg.get("default_model"),
            })
        items.sort(key=lambda m: (m["multiplier"] is None, m["multiplier"] or 0, m["id"] or ""))
        return {"ok": True, "count": len(items), "models": items}

    def _pick_account(self, name: Any = None) -> Account:
        """Resolve an account by name, else the first usable, else the first."""
        wanted = str(name or "").strip()
        acc = self.pool.get(wanted) if wanted else None
        if acc is None:
            usable = self.pool.usable()
            acc = usable[0] if usable else (self.pool.accounts[0] if self.pool.accounts else None)
        if acc is None or not acc.session:
            raise PoolError("没有可用账号 / no usable account")
        return acc

    def points(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Daily grant, invitation codes and recent ledger (read-only)."""
        payload = payload or {}
        acc = self._pick_account(payload.get("name"))
        gw = self.gw.models_client
        ledger = {"total": 0, "rows": []}
        want_ledger = bool(payload.get("ledger", True))
        # The overview and the ledger are independent round-trips; running
        # them concurrently halves the panel's load time on a slow upstream.
        if want_ledger:
            with ThreadPoolExecutor(max_workers=2) as pool:
                f_overview = pool.submit(
                    self._with_session, acc, lambda s: gw.points_overview(s))
                f_ledger = pool.submit(
                    gw.points_ledger, acc.session,
                    page_size=int(payload.get("size") or 50))
                overview = f_overview.result()
                try:
                    ledger = f_ledger.result()
                except Exception as exc:                 # noqa: BLE001
                    self.log(f"[panel] 读取积分流水失败：{exc}")
        else:
            overview = self._with_session(acc, lambda s: gw.points_overview(s))
        # Cache the invitation state on the account so the list can render it.
        if "activated" in overview:
            acc.activated = bool(overview.get("activated"))
        if overview.get("applied_invitation_code"):
            acc.applied_invitation_code = str(overview["applied_invitation_code"])
        return {"ok": True, "account": acc.name, "overview": overview,
                "ledger": ledger}

    def _with_session(self, acc: Account, call):
        """Run ``call(session)``; on a rejected session re-login and retry once.

        The upstream can reject a session that the local 14-day expiry still
        considers valid (e.g. it was invalidated server-side).  Password
        accounts can recover silently, so a 401 must never surface as a plain
        upstream failure — that is what made "每日签到" fail on an otherwise
        healthy account.
        """
        try:
            return self._call_with_retry(acc, call)
        except UpstreamError as exc:
            if getattr(exc, "banned", False):
                self.pool.mark_banned(acc, str(exc))
                raise PoolError(f"账号 {acc.name} 已被上游停用（封禁）") from exc
            if exc.status != 401:
                raise
            if not acc.has_password:
                acc.session, acc.expire_at = "", 0
                self.pool.save(force=True)
                raise PoolError(
                    f"账号 {acc.name} 的 session 已失效且没有密码，无法自动重登"
                    f"（请用验证码重新登录）") from exc
            self.log(f"[panel] {acc.name} session 被上游拒绝，正在用密码重登")
            try:
                self.pool.ensure_session(acc, force=True)
            except (AccountError, PoolError) as relogin:
                raise PoolError(
                    f"账号 {acc.name} session 失效，密码重登失败：{relogin}") from exc
            return call(acc.session)

    def _call_with_retry(self, acc: Account, call):
        """Run ``call`` against a possibly-blip session, retrying transient faults.

        The panel's per-account actions (签到 / 新手任务 / 查积分) hit the upstream
        one account at a time, but it still answers a burst with a fake 401 or
        "系统内部错误" (see ``AccountPool._read_quota_with_retry``).  Retrying here
        keeps a blip from forcing a re-login — which is what the caller does on
        a 401, and which invalidates the session a parallel request is using.
        """
        attempts = max(0, int(self.gw.cfg.get("auth_retry_attempts") or 0))
        pause = float(self.gw.cfg.get("auth_retry_pause") or 0)
        last: Optional[UpstreamError] = None
        for i in range(attempts + 1):
            try:
                return call(acc.session)
            except UpstreamError as exc:
                if not self.pool._is_transient_read_fault(exc):
                    raise
                last = exc
                if i < attempts and pause:
                    time.sleep(pause)
        assert last is not None
        raise last

    def points_activate(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Submit an invitation code for one account (mutates account state)."""
        code = str(payload.get("code") or "").strip()
        if not code:
            raise PoolError("需要邀请码 / invite code is required")
        acc = self._pick_account(payload.get("name"))
        dev = (acc.identity or {}).get("campus_device_id") or ""
        data = self._with_session(
            acc, lambda s: self.gw.models_client.points_activate(s, dev, code))
        # Remember the outcome so the account list can show "已激活 / 已用码"
        # without another upstream round-trip.
        acc.activated = bool(data.get("activated", True))
        acc.applied_invitation_code = str(data.get("appliedInvitationCode") or code)
        self.pool.save(force=True)
        self.log(f"[panel] {acc.name} 提交邀请码 {code}："
                 f"activated={data.get('activated')} "
                 f"already={data.get('alreadyActivated')}")
        return {"ok": True, "account": acc.name, "code": code, "result": data,
                "points": self.points({"name": acc.name, "ledger": False})}

    def onboarding(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """One-click onboarding: submit all 8 tasks via the dedicated endpoint.

        Mirrors the mobile client's "一键新手任务" (`q1/s;->j`): each task key
        goes through ``POST /onboarding/tasks/complete`` and the response
        reports ``success``/``completed`` plus awarded ``points``.  Each task
        is idempotent server-side, so re-running only reports state.
        """
        payload = payload or {}
        acc = self._pick_account(payload.get("name"))
        gw = self.gw.models_client

        # The eight tasks are independent and idempotent server-side, so submit
        # them concurrently.  Serially this took ~2 minutes per account (each
        # call is a round-trip); in parallel it is one round-trip plus the
        # slowest task.  The upstream tolerates this — the official client also
        # fires the batch at once.
        # A stale session would otherwise fail all eight tasks; recover once
        # before fanning out.
        try:
            self._with_session(acc, lambda s: gw.onboarding_complete(s, "first_message"))
        except UpstreamError as exc:
            raise PoolError(f"新手任务失败：{exc}") from exc

        def run_one(task):
            key, label, reward = task
            row: Dict[str, Any] = {"key": key, "name": label, "expected": reward}
            try:
                data = gw.onboarding_complete(acc.session, key)
                # live shape: {alreadyCompleted: bool, balance: <permanent>}
                row.update({"ok": True,
                            "already": bool(data.get("alreadyCompleted")),
                            "balance": data.get("balance")})
            except Exception as exc:                     # noqa: BLE001
                row.update({"ok": False, "error": str(exc)[:200]})
            return row

        with ThreadPoolExecutor(max_workers=min(8, len(ONBOARDING_TASKS))) as pool:
            results = list(pool.map(run_one, ONBOARDING_TASKS))

        # "gained" = the jump in the permanent balance reported by the tasks
        # themselves; avoids two extra quota round-trips.
        balances = [r["balance"] for r in results
                    if isinstance(r.get("balance"), (int, float))]
        gained = 0
        if len(balances) >= 2:
            gained = max(0, int(max(balances) - min(balances)))

        self.log(f"[panel] {acc.name} 新手任务完成：gained={gained} "
                 f"ok={sum(1 for r in results if r.get('ok'))}/{len(ONBOARDING_TASKS)}")
        return {"ok": True, "account": acc.name, "gained": gained,
                "tasks": results,
                "points": self.points({"name": acc.name, "ledger": False})}

    def onboarding_state(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Read-only task list + completion state (``GET /onboarding/tasks``)."""
        acc = self._pick_account((payload or {}).get("name"))
        data = self._with_session(
            acc, lambda s: self.gw.models_client.onboarding_tasks(s))
        return {"ok": True, "account": acc.name, "tasks": data}

    def invite_auto(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Bind an invitation code to every account that has none.

        The pool is self-sufficient: every account owns a long-term code
        (``maxUses=1000``) plus one-shot codes, so an unbound account takes a
        spare code from *another* account. Binding is sequential — the upstream
        counts activations per account, and a parallel burst looks abusive.
        """
        payload = payload or {}
        # ``applied_invitation_code`` is only learned from the points overview
        # (never from login), so accounts that were never inspected still show
        # a blank here. Ask the upstream once for the ones we do not know
        # about instead of risking a double activation.
        unknown = [a for a in self.pool.accounts
                   if a.enabled and a.session
                   and not a.applied_invitation_code and a.activated is None]
        if unknown:
            def sync(acc):
                try:
                    overview = self.gw.models_client.points_overview(acc.session)
                    acc.activated = bool(overview.get("activated"))
                    acc.applied_invitation_code = str(
                        overview.get("applied_invitation_code") or "")
                except Exception:                    # noqa: BLE001
                    # Offline or a transient failure: mark as inspected so the
                    # next run does not re-query, but leave the code blank —
                    # the activation call itself is the source of truth.
                    if acc.activated is None:
                        acc.activated = False

            with ThreadPoolExecutor(max_workers=min(8, len(unknown))) as pool:
                list(pool.map(sync, unknown))
            self.pool.save(force=True)

        accs = [a for a in self.pool.accounts
                if a.enabled and a.session and not a.applied_invitation_code]
        if not accs:
            return {"ok": True, "bound": 0, "skipped": len(self.pool.accounts),
                    "results": [], "message": "所有账号均已绑定邀请码"}

        def spare_codes() -> List[Tuple[str, str]]:
            """(code, owner) pairs still usable, donors first."""
            out: List[Tuple[str, str]] = []
            for acc in self.pool.accounts:
                if acc.name in {a.name for a in accs}:
                    continue                      # never bind to itself
                if not acc.session:
                    continue
                try:
                    codes = self.gw.models_client.points_overview(acc.session).get(
                        "invitation_codes") or []
                except Exception:                  # noqa: BLE001
                    continue
                for c in codes:
                    if (c.get("status") == "active"
                            and int(c.get("maxUses") or 0) > int(c.get("usedCount") or 0)):
                        code = str(c.get("inviteCode") or "")
                        if code:
                            # Long-term codes last: put them first so one-shot
                            # spares stay available for later accounts.
                            out.append((0 if int(c.get("maxUses") or 0) >= 1000 else 1,
                                        code, acc.name))
            out.sort(key=lambda t: t[0])
            return [(code, owner) for _, code, owner in out]

        donors = spare_codes()
        results: List[Dict[str, Any]] = []
        donor_idx = 0
        for acc in accs:
            if donor_idx >= len(donors):
                results.append({"account": acc.name, "ok": False,
                                "error": "池内没有可用邀请码"})
                continue
            code, owner = donors[donor_idx]
            donor_idx += 1
            try:
                activated = self.points_activate({"name": acc.name, "code": code})
                results.append({"account": acc.name, "ok": True, "code": code,
                                "from": owner,
                                "already": bool((activated.get("result") or {})
                                                .get("alreadyActivated"))})
                self.log(f"[panel] {acc.name} 自动绑定邀请码 {code}（来自 {owner}）")
            except Exception as exc:                # noqa: BLE001
                results.append({"account": acc.name, "ok": False,
                                "code": code, "error": str(exc)[:200]})
                self.log(f"[panel] {acc.name} 自动绑定邀请码失败：{exc}")

        self.pool.save(force=True)
        ok = sum(1 for r in results if r.get("ok"))
        return {"ok": True, "bound": ok, "failed": len(results) - ok,
                "results": results,
                "message": f"绑定成功 {ok}/{len(results)}"}

    def checkin(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Daily check-in — the mobile client's use of ``points/first-login``.

        Idempotent per daily cycle: a repeat call comes back with
        ``alreadyProcessed`` and today's balances.
        """
        payload = payload or {}
        acc = self._pick_account(payload.get("name"))
        dev = (acc.identity or {}).get("campus_device_id") or ""
        data = self._with_session(
            acc, lambda s: self.gw.models_client.points_first_login(s, dev))
        already = bool(data.get("alreadyProcessed"))
        self.log(f"[panel] {acc.name} 签到：{'今日已领' if already else '已签到'} "
                 f"balance={data.get('currentBalance') or data.get('permanentBalance')}")
        return {"ok": True, "account": acc.name, "already": already,
                "code": data.get("code"),
                "current_balance": data.get("currentBalance"),
                "permanent_balance": data.get("permanentBalance"),
                "daily_balance": data.get("dailyBalance"),
                "daily_quota": data.get("dailyQuota"),
                "daily_cycle_date": data.get("dailyCycleDate"),
                "desc": data.get("desc"),
                "points": self.points({"name": acc.name, "ledger": False})}

    def model_events(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Entitlement history for the last 7 days.

        ``accounts.json`` only knows the *current* catalogue, so a downgrade
        that later reverses would otherwise be invisible.  This answers both
        the raw transitions and, per account, the current state plus how many
        times it has moved each way.
        """
        payload = payload or {}
        days = float(payload.get("days") or 7)
        account = str(payload.get("name") or "").strip()
        log = self.pool.model_events
        events = log.events(account=account, days=days,
                            limit=int(payload.get("limit") or 500))
        summary = log.summary(days=days)
        if account:
            summary = {k: v for k, v in summary.items() if k == account}
        # Same rule the panel uses: an account is downgraded when it lacks a
        # paid model the pool as a whole can serve.  Counting entries would
        # miss the free-tier splits the upstream keeps making.
        # Before the first probe the catalogue is the built-in FALLBACK guess;
        # diffing against it marked the whole pool as downgraded — the same bug
        # ``state()`` already guards against.
        paid_universe = set()
        if self.gw.models_confirmed:
            paid_universe = {str(m.get("id")) for m in self.gw.catalogue()
                             if m.get("id") and not is_free(str(m.get("id")))}
        downgraded_now = set()
        if paid_universe:
            for acc in self.pool.accounts:
                if not acc.enabled or acc.models is None or not acc.models:
                    continue
                if any(m not in acc.models for m in paid_universe):
                    downgraded_now.add(acc.name)
        return {
            "ok": True,
            "days": days,
            "retention_days": log.retention / 86400,
            "path": str(log.path),
            "events": events,
            "summary": summary,
            # convenience for the UI's "currently downgraded" filter
            "downgraded_now": sorted(downgraded_now),
        }

    def logs(self, lines: int = 200) -> Dict[str, Any]:
        path = self.gw.log.path
        lines = max(10, min(int(lines or 200), 2000))
        try:
            content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            content = []
        return {"ok": True, "path": str(path), "lines": content[-lines:]}
