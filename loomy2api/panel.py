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
``POST /api/panel/accounts/identity`` inspect / regenerate the account identity
``GET  /api/panel/logs``              tail of the gateway log
"""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Optional

from . import constants as C
from .account import Account, AccountError
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

    # ------------------------------------------------------------ helpers

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
        view["mode"] = "password" if (acc.loginid and acc.password) else (
            "session" if acc.session else "empty")
        return view

    def _quota_stale(self, acc: Account) -> bool:
        minutes = float(self.gw.cfg.get("quota_refresh_minutes") or 30)
        return (time.time() - (acc.quota_updated_at or 0)) > minutes * 60

    def _refresh_all(self, only_stale: bool = True) -> None:
        """Refresh quota for the accounts that need it, concurrently.

        The upstream call is a network round-trip with a 30 s timeout, so doing
        it serially made a cold panel load take 30 s *per unreachable account*
        (on a phone that reads as "stuck loading accounts").  They are
        independent, so fan them out and wait only as long as the slowest one.
        """
        pending = [a for a in self.pool.accounts
                   if a.session_valid and not (only_stale and not self._quota_stale(a))]
        if not pending:
            return

        def refresh_one(acc: Account) -> None:
            try:
                self.pool.refresh_quota(acc)
            except Exception as exc:                     # noqa: BLE001
                self.log(f"[panel] 刷新 {acc.name} 额度失败：{exc}")

        with ThreadPoolExecutor(max_workers=min(8, len(pending))) as pool:
            list(pool.map(refresh_one, pending))
        self.pool.save()

    # --------------------------------------------------------------- state

    def state(self, *, refresh: bool = False) -> Dict[str, Any]:
        self._refresh_all(only_stale=not refresh)
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
            },
            "config": {
                "upstream": self.gw.cfg["upstream"],
                "strategy": self.gw.cfg.get("strategy"),
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
                "session_renew_before_days": self.gw.cfg.get("session_renew_before_days"),
                "sessions_from_client": bool(self.gw.cfg.get("sessions_from_client", True)),
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
            "session_renew_before_days", "quota_refresh_minutes",
            "sessions_from_client", "identity_mode",
        }
        changes = {k: v for k, v in payload.items() if k in editable}

        if "strategy" in changes:
            strategy = str(changes["strategy"])
            if strategy not in ("balance", "round_robin", "lru", "weighted"):
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
        for flag in ("log_requests", "sessions_from_client"):
            if flag in changes:
                changes[flag] = bool(changes[flag])
        for num_key in ("timeout", "max_retries", "cooldown_seconds",
                        "session_renew_before_days", "quota_refresh_minutes"):
            if num_key in changes:
                try:
                    changes[num_key] = int(changes[num_key])
                except (TypeError, ValueError):
                    raise PoolError(f"{num_key} 必须是整数") from None

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
            items.append(out)
        return {"ok": True, "version": 1, "exported_at": int(time.time()),
                "accounts": items}

    # --------------------------------------------------------------- writes

    def refresh(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._refresh_all(only_stale=False)
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
                    updated.append(name)
                else:
                    skipped.append(name)
                    reasons[name] = "账号已存在（mode=skip）"
                continue
            self.pool.add_account(
                name, loginid=str(raw.get("loginid") or ""),
                session=session, userid=str(raw.get("userid") or ""),
                identity=dict(raw["identity"]) if raw.get("identity") else None)
            acc = self.pool.get(name)
            if acc is not None and raw.get("expireAt"):
                acc.expire_at = int(raw["expireAt"])
            if acc is not None and raw.get("obtainedAt"):
                acc.obtained_at = int(raw["obtainedAt"])
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

    def renew_account(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        acc = self.pool.get(name)
        if acc is None:
            raise PoolError(f"没有名为 {name} 的账号 / no such account: {name}")
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
        acc = self.pool.get(name)
        if acc is None:
            raise PoolError(f"没有名为 {name} 的账号 / no such account: {name}")
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
        """Catalogue with the points multiplier pulled out of the upstream name."""
        if (payload or {}).get("refresh"):
            try:
                self.gw.refresh_models()
            except Exception as exc:                     # noqa: BLE001
                self.log(f"[panel] 刷新模型列表失败：{exc}")
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
        overview = gw.points_overview(acc.session)
        ledger = {"total": 0, "rows": []}
        if payload.get("ledger", True):
            try:
                ledger = gw.points_ledger(acc.session,
                                          page_size=int(payload.get("size") or 50))
            except Exception as exc:                     # noqa: BLE001
                self.log(f"[panel] 读取积分流水失败：{exc}")
        return {"ok": True, "account": acc.name, "overview": overview,
                "ledger": ledger}

    def points_activate(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Submit an invitation code for one account (mutates account state)."""
        code = str(payload.get("code") or "").strip()
        if not code:
            raise PoolError("需要邀请码 / invite code is required")
        acc = self._pick_account(payload.get("name"))
        dev = (acc.identity or {}).get("campus_device_id") or ""
        data = self.gw.models_client.points_activate(acc.session, dev, code)
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
        results = []
        gained = 0
        # capture balance before the run so "gained" is a real delta
        try:
            balance_before = int(gw.quota(acc.session).get("balance") or 0)
        except Exception:                                # noqa: BLE001
            balance_before = None
        for key, label, _reward in ONBOARDING_TASKS:
            row: Dict[str, Any] = {"key": key, "name": label,
                                   "expected": _reward}
            try:
                data = gw.onboarding_complete(acc.session, key)
                # live shape: {alreadyCompleted: bool, balance: <permanent>}
                row.update({"ok": True,
                            "already": bool(data.get("alreadyCompleted")),
                            "balance": data.get("balance")})
            except Exception as exc:                     # noqa: BLE001
                row.update({"ok": False, "error": str(exc)[:200]})
            results.append(row)
        # points actually gained = balance delta across the run
        gained = 0
        try:
            balance_now = int(gw.quota(acc.session).get("balance") or 0)
            if balance_before is not None:
                gained = max(0, balance_now - balance_before)
        except Exception:                                # noqa: BLE001
            pass
        self.log(f"[panel] {acc.name} 新手任务完成：gained={gained} "
                 f"ok={sum(1 for r in results if r.get('ok'))}/8")
        return {"ok": True, "account": acc.name, "gained": gained,
                "tasks": results,
                "points": self.points({"name": acc.name, "ledger": False})}

    def onboarding_state(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Read-only task list + completion state (``GET /onboarding/tasks``)."""
        acc = self._pick_account((payload or {}).get("name"))
        data = self.gw.models_client.onboarding_tasks(acc.session)
        return {"ok": True, "account": acc.name, "tasks": data}

    def checkin(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Daily check-in — the mobile client's use of ``points/first-login``.

        Idempotent per daily cycle: a repeat call comes back with
        ``alreadyProcessed`` and today's balances.
        """
        payload = payload or {}
        acc = self._pick_account(payload.get("name"))
        dev = (acc.identity or {}).get("campus_device_id") or ""
        data = self.gw.models_client.points_first_login(acc.session, dev)
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

    def logs(self, lines: int = 200) -> Dict[str, Any]:
        path = self.gw.log.path
        lines = max(10, min(int(lines or 200), 2000))
        try:
            content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            content = []
        return {"ok": True, "path": str(path), "lines": content[-lines:]}
