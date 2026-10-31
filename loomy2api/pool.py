"""Multi-account pool: session lifecycle, quota tracking, rotation.

Design notes
------------
* One account = one Loomy/iFlytek account = one 14-day session.  Sessions are
  refreshed automatically (password login) before they expire, so a pool keeps
  working unattended.
* Routing strategies:

  ``balance``      pick the account with the most available points (default)
  ``round_robin``  cycle in order
  ``lru``          least recently used

* An account that answers ``401/403`` or reports an exhausted quota is put in
  a cooldown and skipped until it expires; the gateway then retries the request
  on the next account.
* The pool state (sessions, quota) is written back to ``accounts.json``
  atomically.  Passwords live in the same file — keep it out of git
  (``.gitignore`` already covers it).
"""

from __future__ import annotations

import glob
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from . import constants as C
from .account import (Account, AccountClient, AccountError, generate_password,
                      is_credential_error)
from .upstream import ModelGateway, UpstreamError

__all__ = ["AccountPool", "PoolError"]


class PoolError(RuntimeError):
    pass


class AccountPool:
    def __init__(self, cfg, logger=None):
        self.cfg = cfg
        self.log = logger or (lambda msg: None)
        self.client = AccountClient(cfg)
        self.gateway = ModelGateway(cfg)
        self._lock = threading.RLock()
        self._save_lock = threading.Lock()
        self._accounts: List[Account] = []
        self._rr_index = 0
        self._rr_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_save = 0.0
        self._dirty = False
        # One login/refresh at a time per account: bootstrap, the keeper and
        # incoming requests now run concurrently, and two logins for the same
        # account would invalidate each other's session.
        self._account_locks: Dict[str, threading.Lock] = {}
        self._account_locks_guard = threading.Lock()
        self._file = cfg.path("accounts_file", "accounts.json")
        self.load()

    def _account_lock(self, name: str) -> threading.Lock:
        with self._account_locks_guard:
            lock = self._account_locks.get(name)
            if lock is None:
                lock = self._account_locks[name] = threading.Lock()
            return lock

    # ------------------------------------------------------------ storage

    def load(self) -> None:
        raw: object = {}
        if self._file.exists():
            try:
                raw = json.loads(self._file.read_text(encoding="utf-8"))
            except Exception as exc:                    # noqa: BLE001
                raise PoolError(f"{self._file} 解析失败 / parse error: {exc}") from exc

        items = raw.get("accounts") if isinstance(raw, dict) else raw
        accounts: List[Account] = []
        for idx, item in enumerate(items or []):
            if isinstance(item, dict):
                accounts.append(Account.from_dict(item, idx))

        if self.cfg.get("sessions_from_client", True):
            accounts.extend(self._client_accounts(existing=accounts))

        with self._lock:
            # keep runtime stats across reloads for same-named accounts
            previous = {a.name: a for a in self._accounts}
            for acc in accounts:
                old = previous.get(acc.name)
                if old is not None:
                    acc.requests, acc.points_used, acc.failures = (
                        old.requests, old.points_used, old.failures)
                    acc.last_used, acc.last_error = old.last_used, old.last_error
                    if acc.expire_at == 0 and old.expire_at:
                        acc.session, acc.expire_at, acc.userid = (
                            old.session, old.expire_at, old.userid)
            self._accounts = accounts

        self.log(f"账号池载入 {len(accounts)} 个账号"
                 + (f"（{', '.join(a.name for a in accounts)}）" if accounts else ""))

    def _client_accounts(self, existing: Sequence[Account]) -> List[Account]:
        """Import sessions from any installed desktop client (read-only).

        These are *derived* accounts: they are never written back to
        accounts.json (the client owns that file), and an import that collides
        with a configured account by session or by name is skipped.
        """
        pattern = os.path.join(str(self.cfg.get("client_root") or C.CLIENT_PUBLIC_ROOT),
                               "*", "userData", "auth-session.json")
        found: List[Account] = []
        known_sessions = {a.session for a in existing if a.session}
        known_names = {a.name for a in existing}
        # Same *account* reached through two different sessions (the client's
        # session vs. ours) must not become two pool entries: match on userid.
        known_users = {a.userid for a in existing if a.userid}
        for path in glob.glob(pattern):
            try:
                data = json.loads(Path(path).read_text(encoding="utf-8"))
            except Exception:                           # noqa: BLE001
                continue
            session = str(data.get("session") or "")
            userid = str(data.get("userid") or "")
            if not session or session in known_sessions:
                continue
            if userid and userid in known_users:
                continue
            name = f"desktop-{str(data.get('phone') or 'unknown')[-4:]}"
            if name in known_names:
                continue
            created = int(data.get("updatedAt") or 0)
            # the client stores milliseconds since epoch; sessions last 14 days
            expire = (created // 1000 + C.SESSION_EXPIRE_SECONDS) if created else 0
            found.append(Account(
                name=name,
                loginid=str(data.get("phone") or ""),
                session=session, userid=userid,
                expire_at=expire, obtained_at=created // 1000, source="client",
                persist=False,
            ))
            known_sessions.add(session)
            known_names.add(name)
            if userid:
                known_users.add(userid)
            self.log(f"发现桌面客户端登录态：{path}")
        return found

    def save(self, *, force: bool = False) -> None:
        now = time.time()
        with self._lock:
            if not force and (now - self._last_save) < 5:
                self._dirty = True
                return
            self._dirty = False
            self._last_save = now
            payload = {"accounts": [a.to_dict() for a in self._accounts if a.persist]}
        # Batch actions (e.g. setting passwords concurrently) save from several
        # threads at once; they must not race on the same temp file.
        with self._save_lock:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._file.with_suffix(self._file.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, self._file)

    def flush(self) -> None:
        """Force a write if a throttled save is pending."""
        if self._dirty:
            self.save(force=True)

    # ------------------------------------------------------------ access

    @property
    def accounts(self) -> List[Account]:
        with self._lock:
            return list(self._accounts)

    def get(self, name: str) -> Optional[Account]:
        for acc in self.accounts:
            if acc.name == name:
                return acc
        return None

    def add_account(self, name: str, loginid: str = "", password: str = "",
                    session: str = "", userid: str = "",
                    identity: Optional[Dict] = None) -> Account:
        if self.get(name):
            raise PoolError(f"账号 {name} 已存在 / account already exists")
        acc = Account(name=name, loginid=loginid, password=password,
                      session=session, userid=userid)
        # bind a device identity at creation time, so the account always looks
        # like one consistent device from its very first request
        acc.identity = dict(identity) if identity else self.client.ensure_identity(acc)
        if session and not acc.expire_at:
            acc.obtained_at = int(time.time())
            acc.expire_at = acc.obtained_at + C.SESSION_EXPIRE_SECONDS
        with self._lock:
            self._accounts.append(acc)
        self.save(force=True)
        return acc

    def update_account(self, name: str, **fields) -> Account:
        """Patch an existing account (loginid / password / enabled).

        ``password_set_at`` is cleared when the password changes so the
        "permanent" badge reflects the new credential's status.
        """
        acc = self.get(name)
        if acc is None:
            raise PoolError(f"没有名为 {name} 的账号 / no such account: {name}")
        for key in ("loginid", "password"):
            if key in fields and fields[key] is not None:
                setattr(acc, key, str(fields[key]))
                if key == "password":
                    # a new credential means the old session belongs to the old
                    # login: force a fresh login on next use
                    acc.session, acc.expire_at = "", 0
                    acc.password_set_at = 0
                    acc.password_verified_at = 0
        if "enabled" in fields and fields["enabled"] is not None:
            acc.enabled = bool(fields["enabled"])
        self.save(force=True)
        return acc

    def rebind_identity(self, name: str) -> Account:
        """Give the account a brand-new device identity (and log it in again)."""
        acc = self.get(name)
        if acc is None:
            raise PoolError(f"没有名为 {name} 的账号 / no such account: {name}")
        self.client.rebind_identity(acc)
        if acc.loginid and acc.password:
            self.ensure_session(acc, force=True)
        self.save(force=True)
        return acc

    def remove_account(self, name: str) -> bool:
        with self._lock:
            before = len(self._accounts)
            self._accounts = [a for a in self._accounts if a.name != name]
            removed = len(self._accounts) != before
        if removed:
            self.save(force=True)
        return removed

    # ------------------------------------------------------ session keep

    def set_account_password(self, acc: Account,
                             password: str = "") -> str:
        """Set (or generate) the account's login password upstream.

        The account service only needs a valid session for this, so an account
        that was added via SMS can be upgraded to a password account at any
        time.  The password is persisted in ``accounts.json`` and therefore
        travels with exports.  Returns the password that was set.
        """
        if not acc.session or not acc.session_valid:
            raise PoolError(
                f"账号 {acc.name} 没有有效 session，无法设置密码"
                f"（请先续期或用验证码重新登录）")
        if not acc.persist:
            raise PoolError(
                f"账号 {acc.name} 来自桌面客户端，密码无法保存到本机账号文件；"
                f"请在面板里用「导入账号」把它复制为独立账号后再设置密码")
        if not acc.loginid:
            raise PoolError(
                f"账号 {acc.name} 没有手机号，密码登录无从使用；请先补充 loginid")
        new_password = password or generate_password()
        self.client.set_password(acc.session, new_password, acc.identity or None)
        acc.password = new_password
        acc.password_set_at = int(time.time())
        acc.password_verified_at = 0

        # "update/pwd succeeded" is not proof the password works: the service
        # has been observed to accept it while ``byPwd`` still answers
        # ``0200103 账号鉴权失败``.  Prove it with a real login before calling
        # the account permanent — otherwise it silently fails at expiry.
        try:
            got = self.client.login_by_password(
                acc.loginid, new_password, acc.identity or None)
        except AccountError as exc:
            self.save(force=True)
            raise PoolError(
                f"账号 {acc.name} 密码已设置，但用新密码登录验证失败：{exc}。"
                f"该账号仍不是永久账号，请在面板重试或检查密码规则") from exc
        acc.password_verified_at = int(time.time())
        acc.login_blocked_until = 0.0
        # A successful login hands back a fresh session; keep it.
        if got.get("session"):
            acc.session = got["session"]
            acc.userid = got.get("userid") or acc.userid
            acc.obtained_at = int(time.time())
            acc.expire_at = acc.obtained_at + C.SESSION_EXPIRE_SECONDS
        self.save(force=True)
        self.log(f"账号 {acc.name} 已设置登录密码并验证通过（升级为可自动续期账号）")
        return new_password

    def verify_password(self, acc: Account) -> bool:
        """Prove the stored password works by logging in with it.

        Returns True and refreshes the session on success; on failure the
        password stays stored but is no longer treated as permanent.  Never
        raises: callers use this as a health check.
        """
        if not (acc.loginid and acc.password):
            return False
        try:
            got = self.client.login_by_password(
                acc.loginid, acc.password, acc.identity or None)
        except Exception as exc:                         # noqa: BLE001
            acc.password_verified_at = 0
            if is_credential_error(exc):
                acc.login_blocked_until = time.time() + float(
                    self.cfg.get("login_blocked_seconds") or 600)
            self.log(f"账号 {acc.name} 密码验证失败：{exc}")
            self.save()
            return False
        acc.password_verified_at = int(time.time())
        acc.login_blocked_until = 0.0
        if got.get("session"):
            acc.session = got["session"]
            acc.userid = got.get("userid") or acc.userid
            acc.obtained_at = int(time.time())
            acc.expire_at = acc.obtained_at + C.SESSION_EXPIRE_SECONDS
        self.save()
        return True

    def _repair_password(self, acc: Account, *, reason: object) -> bool:
        """Replace a rejected password while the session is still usable.

        ``/userinfo/update/pwd`` only needs a valid session, so an account
        whose *stored* password no longer works can be fixed unattended
        instead of waiting for a human to notice.

        This deliberately bypasses the "don't overwrite an upstream password"
        guard in :meth:`ensure_password`: that guard protects a password we
        have never seen, while here we just proved the one we store is
        rejected.  Returns True only when the new password was set *and*
        proven by a real login (which also yields a fresh session).
        """
        if not bool(self.cfg.get("auto_repair_password", True)):
            return False
        if not acc.persist or not acc.loginid or not acc.session_valid:
            return False
        self.log(f"账号 {acc.name} 密码被上游拒绝（{reason}），"
                 f"用现有 session 自动改密")
        try:
            self.set_account_password(acc)
        except Exception as exc:                         # noqa: BLE001
            # Best effort: a failed repair must never break the request that
            # triggered it.  The caller falls back to the existing session.
            self.log(f"账号 {acc.name} 自动改密失败：{exc}")
            return False
        self.log(f"账号 {acc.name} 自动改密成功（新密码已写入 accounts.json）")
        return True

    def ensure_password(self, acc: Account, *, force: bool = False) -> bool:
        """Give a password-less account one while its session still works.

        Called by the keeper shortly before a session expires: without a
        password the account would need an SMS code to come back, which the
        gateway cannot do unattended.  Returns True when a password was set.
        """
        if acc.has_password and not force:
            if acc.password_verified_at:
                return False
            # A stored but unproven password: verify it with a real login
            # before spending a rotation on it.
            if self.verify_password(acc):
                return False
            # The stored password is rejected.  The session may still be good,
            # so rotate the password instead of giving up on the account.
            return self._repair_password(acc, reason="密码验证失败")
        if not acc.session_valid:
            return False
        # Derived accounts (imported from the desktop client) are never written
        # back to accounts.json, so a password set here would be forgotten on
        # restart and rotated again — leave the client's account untouched.
        if not acc.persist:
            return False
        # Never overwrite a password the user set elsewhere: if the account
        # already has one upstream but we don't know it, rotating would break
        # their other login.  The explicit "set password" action can still do
        # it on purpose.
        status = getattr(self.client, "password_status", None)
        if status is not None:
            try:
                if status(acc.session, acc.identity or None):
                    self.log(f"账号 {acc.name} 上游已有密码但本地未保存，"
                             f"跳过自动设置（可在面板手动设置）")
                    return False
            except Exception:                            # noqa: BLE001
                pass                                     # offline: try anyway
        try:
            self.set_account_password(acc)
            return True
        except (AccountError, PoolError) as exc:
            self.log(f"账号 {acc.name} 自动设置密码失败：{exc}")
            return False

    def ensure_session(self, acc: Account, *, force: bool = False) -> Account:
        """Log in / refresh the account session when needed.

        A password-less account cannot renew itself, so before its session
        expires the keeper upgrades it: ``password_set_before_days`` (default
        1) controls how close to expiry that happens.  Once the password is
        stored the account behaves like a permanent one — every later expiry
        is handled by a password login.

        Serialized per account: bootstrap, the keeper and request-time
        ``acquire`` can now run at the same time, and two concurrent logins
        would each invalidate the other's session.
        """
        with self._account_lock(acc.name):
            return self._ensure_session_locked(acc, force=force)

    @staticmethod
    def _login_blocked(acc: Account) -> bool:
        return acc.login_blocked_until > time.time()

    def _login_block_error(self, acc: Account) -> PoolError:
        left = max(1, int(acc.login_blocked_until - time.time()))
        return PoolError(
            f"账号 {acc.name} 的密码被上游拒绝，密码登录已暂停 {left}s"
            f"（session 仍有效时不受影响；请在面板重新设置密码）")

    def _ensure_session_locked(self, acc: Account, *, force: bool) -> Account:
        renew_days = float(self.cfg.get("session_renew_before_days") or 3)
        # 0 disables the automatic upgrade (the account then needs an SMS login)
        upgrade_days = float(self.cfg.get("password_set_before_days")
                             if self.cfg.get("password_set_before_days") is not None
                             else 1)
        needs = force or not acc.session_valid
        if not needs and acc.days_left is not None and acc.days_left < renew_days:
            if not acc.has_password and (
                    upgrade_days <= 0 or acc.days_left > upgrade_days):
                # Still too early to burn the upgrade (or it is disabled);
                # wait until the account is within the configured window.
                return acc
            if not force and self._login_blocked(acc):
                # The stored password was just rejected: retrying now would
                # only extend the upstream lockout.  The session is still
                # valid, so keep using it until the block expires.
                return acc
            needs = True
            self.log(f"账号 {acc.name} session 剩余 {acc.days_left:.1f} 天，提前续期")
        if not needs:
            return acc
        if not force and self._login_blocked(acc):
            # An explicit ``force`` (panel "renew", CLI login) is the user's
            # own decision, so it is allowed through.
            raise self._login_block_error(acc)
        if not acc.has_password or not acc.password_verified_at:
            # last chance: the session still works, so we can set (or prove)
            # a password now and make every future expiry automatic
            if acc.session_valid:
                self.ensure_password(acc)
            # ensure_password may just have proven the password wrong, which
            # starts the block — do not immediately log in again.  A session
            # that still works is worth more than the failed renewal.
            if not force and self._login_blocked(acc):
                if acc.session_valid:
                    self.log(f"账号 {acc.name} 沿用现有 session"
                             f"（剩余 {(acc.days_left or 0):.1f} 天）")
                    return acc
                raise self._login_block_error(acc)
            if (not force and acc.password_verified_at and acc.session_valid
                    and (acc.days_left is None or acc.days_left >= renew_days)):
                # The password was just set (or proven) and its verification
                # login already handed back a fresh session — renewing again
                # would only invalidate it.
                return acc
        if not (acc.loginid and acc.password):
            raise PoolError(
                f"账号 {acc.name} 的 session 不可用且没有账号密码，无法自动重登"
                f"（请在 accounts.json 补 loginid/password，或用 `loomy2api login` 短信登录）")
        try:
            self.client.fill(acc)
        except AccountError as exc:
            # A renewal is an optimisation, not a requirement: as long as the
            # session we already hold still works, a failed re-login must not
            # take the request down with it.
            if is_credential_error(exc):
                acc.password_verified_at = 0
                # A rejected password can be replaced through the still-valid
                # session, which makes the account self-healing again.
                if self._repair_password(acc, reason=exc):
                    acc.last_error = ""
                    self.save()
                    return acc
                acc.login_blocked_until = time.time() + float(
                    self.cfg.get("login_blocked_seconds") or 600)
                acc.last_error = f"login: {exc}"
                self.log(f"账号 {acc.name} 密码被上游拒绝（{exc}），"
                         f"暂停密码登录 {int(acc.login_blocked_until - time.time())}s")
            else:
                acc.last_error = f"login: {exc}"
                self.log(f"账号 {acc.name} 续期失败：{exc}")
            self.save()
            if acc.session_valid:
                self.log(f"账号 {acc.name} 沿用现有 session"
                         f"（剩余 {(acc.days_left or 0):.1f} 天）")
                return acc
            raise PoolError(
                f"账号 {acc.name} 的 session 已失效且无法重新登录：{exc}"
                f"（请在面板重新设置密码或短信登录）") from exc
        # a successful password login is the strongest proof the password works
        if acc.has_password:
            acc.password_verified_at = int(time.time())
        acc.login_blocked_until = 0.0
        acc.last_error = ""
        self.save()
        self.log(f"账号 {acc.name} 登录成功 userid={acc.userid} "
                 f"session={acc.session[:8]}… 剩余 {(acc.days_left or 0):.1f} 天")
        return acc

    def refresh_quota(self, acc: Account, *, relogin: bool = True) -> Account:
        """Refresh one account's points; never raises.

        This runs from the panel, the CLI and the background keeper — any of
        which can be offline, behind a dead proxy, or talking to an upstream
        that is simply down. A failed refresh must degrade to "quota unknown",
        not crash the caller (a socket/DNS error is *not* an UpstreamError).

        A session the upstream rejects is re-obtained when possible: the
        points endpoint answers HTTP 200 with ``code=100002 登录已失效`` for a
        dead session, so without this the account would sit at "quota unknown"
        forever while its stored password was perfectly good.
        """
        if not acc.session and not (relogin and acc.has_password):
            return acc
        acc.quota_checked_at = time.time()
        try:
            quota = self.gateway.quota(acc.session)
        except UpstreamError as exc:
            if exc.status not in (401, 403):
                acc.last_error = f"quota: {exc}"
                self.log(f"账号 {acc.name} 额度查询失败：{exc}")
                return acc
            self.log(f"账号 {acc.name} session 被上游拒绝（HTTP {exc.status}）")
            if not (relogin and acc.has_password):
                acc.session, acc.expire_at = "", 0
                acc.last_error = f"quota: {exc}"
                return acc
            acc.session, acc.expire_at = "", 0
            try:
                self.ensure_session(acc)
                quota = self.gateway.quota(acc.session)
            except (AccountError, PoolError, UpstreamError) as relogin_exc:
                acc.last_error = f"quota: {exc}; relogin: {relogin_exc}"
                self.log(f"账号 {acc.name} 额度查询失败，重登也没成功：{relogin_exc}")
                return acc
            except Exception as relogin_exc:             # noqa: BLE001
                acc.last_error = f"quota: {exc}; relogin: {relogin_exc}"
                self.log(f"账号 {acc.name} 额度查询异常（网络？）：{relogin_exc}")
                return acc
        except Exception as exc:                        # noqa: BLE001
            acc.last_error = f"quota: {exc}"
            self.log(f"账号 {acc.name} 额度查询异常（网络？）：{exc}")
            return acc
        acc.balance = quota.get("balance")
        acc.daily_balance = quota.get("daily_balance")
        acc.available = quota.get("available")
        acc.quota_updated_at = int(time.time())
        acc.quota_checked_at = acc.quota_updated_at
        acc.last_error = ""
        return acc

    # ------------------------------------------------------- health check

    def check_account(self, acc: Account, *, force: bool = False) -> Dict[str, Any]:
        """Probe one account and repair it when possible.

        Used by the panel's "check the pool" action.  Unlike ``acquire`` this
        is explicit: the user asked for a verdict, so it never raises and
        reports every step that was attempted.

        Order matters:

        1. a missing expiry timestamp is treated as "needs a login" — a
           session without ``expire_at`` cannot be scheduled for renewal, and
           it is how imported/hand-edited rows end up never renewing;
        2. a live session gets its quota read (cheap, no side effects);
        3. if the quota read is rejected (401) or the session is missing, log
           in again — which also rotates a rejected password when possible;
        4. finally, read the quota again so the caller gets a real balance.
        """
        result: Dict[str, Any] = {
            "name": acc.name,
            "ok": False,
            "action": "none",
            "before_days": acc.days_left,
            "error": "",
        }
        if not acc.enabled:
            result["action"] = "skipped"
            result["error"] = "账号已禁用"
            return result
        if not (acc.loginid and acc.password):
            result["action"] = "skipped"
            result["error"] = "没有账号密码，无法自动重登（需要短信登录）"
            return result

        needs_login = force or not acc.session_valid or acc.days_left is None
        if not needs_login:
            try:
                self.gateway.quota(acc.session)
            except UpstreamError as exc:
                if exc.status in (401, 403):
                    needs_login = True
                    result["error"] = "session 被上游拒绝"
                else:
                    result["error"] = f"额度查询失败：{exc}"
            except Exception as exc:                     # noqa: BLE001
                result["error"] = f"额度查询异常：{exc}"

        if needs_login:
            try:
                self.ensure_session(acc, force=True)
                result["action"] = "relogin"
                result["error"] = ""
            except (AccountError, PoolError) as exc:
                result["error"] = str(exc)
                self.save()
                return result
        else:
            result["action"] = "quota"

        self.refresh_quota(acc)
        result["after_days"] = acc.days_left
        result["available"] = acc.available
        result["ok"] = bool(acc.session_valid) and acc.available != 0
        if not result["ok"] and not result["error"]:
            result["error"] = "额度为 0" if acc.available == 0 else "session 仍不可用"
        self.save()
        return result

    def check_all(self, *, force: bool = False,
                  names: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
        """Health-check every enabled account, concurrently.

        Each probe is a couple of network round-trips, so a 40-account pool
        would take minutes serially.  Results keep the pool's order.
        """
        wanted = set(names) if names else None
        targets = [a for a in self.accounts
                   if a.enabled and (wanted is None or a.name in wanted)]
        if not targets:
            return []
        with ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
            results = list(pool.map(
                lambda a: self._check_guarded(a, force), targets))
        self.save(force=True)
        return results

    def _check_guarded(self, acc: Account, force: bool) -> Dict[str, Any]:
        try:
            return self.check_account(acc, force=force)
        except Exception as exc:                         # noqa: BLE001
            # One account must never abort the batch.
            return {"name": acc.name, "ok": False, "action": "error",
                    "error": str(exc)}

    # ------------------------------------------------------------ routing

    def usable(self, exclude: Sequence[str] = ()) -> List[Account]:
        now = time.time()
        rows = [a for a in self.accounts
                if a.enabled and not a.in_cooldown and a.name not in exclude
                and a.session_valid and a.available != 0]
        if self.daily_only():
            # Only the daily grant may be spent.  An account whose grant is
            # used up is skipped entirely, so the permanent balance survives
            # the cycle reset instead of being drained by routing.
            return [a for a in rows if (a.daily_balance or 0) > 0]
        return rows

    def daily_only(self) -> bool:
        """True when routing must stay inside the daily (activity) grant."""
        return bool(self.cfg.get("daily_only"))

    def _ordered(self, candidates: Sequence[Account]) -> List[Account]:
        """Order candidates by the configured strategy (best first)."""
        strategy = str(self.cfg.get("strategy") or "balance").lower()
        if strategy == "round_robin":
            with self._rr_lock:
                start = self._rr_index % max(1, len(candidates))
                self._rr_index = (start + 1) % max(1, len(candidates))
            return list(candidates[start:]) + list(candidates[:start])
        if strategy == "lru":
            return sorted(candidates, key=lambda a: a.last_used or 0)
        if strategy in ("daily", "activity"):
            # Spend the expiring daily grant first: an unused daily balance is
            # lost at the cycle reset, while the permanent balance carries over.
            return sorted(candidates, key=lambda a: (
                a.daily_balance if a.daily_balance is not None else -1,
                a.available if a.available is not None else 0,
                -a.last_used), reverse=True)
        if strategy == "permanent":
            # The opposite: drain permanent points first and keep the daily
            # grant for accounts that are about to reset.
            return sorted(candidates, key=lambda a: (
                a.balance if a.balance is not None else -1,
                -a.last_used), reverse=True)
        if strategy == "weighted":
            # Random, but richer accounts are picked more often: spreads load
            # without letting a low-balance account run dry early. The mobile
            # UI has offered this option since the strategy list grew.
            # Weighted-random permutation (Efraimidis–Spirakis), so a failing
            # top pick can still fall through to the next account.
            import random as _random
            return sorted(candidates, key=lambda a: _random.random() **
                          (1.0 / max(1, a.available if a.available is not None else 1)))
        # balance
        return sorted(candidates, key=lambda a: (
            a.available if a.available is not None else 10**9,
            -a.last_used), reverse=True)

    def acquire(self, exclude: Sequence[str] = ()) -> Account:
        """Return the next usable account whose session is guaranteed valid.

        A candidate whose renewal fails is skipped and the next one is tried:
        one account with a rejected password must never fail the whole request
        while the pool still holds healthy accounts.
        """
        candidates = self.usable(exclude)
        if not candidates and self.daily_only():
            # "Only spend the activity balance" is an explicit instruction, so
            # falling back to permanent points would be worse than failing.
            raise PoolError(
                "没有账号还有活动余额（已开启「只用活动余额」；"
                "关闭它或等待每日额度重置）")
        if not candidates:
            # nothing healthy: give single-account setups a chance to self-heal
            all_enabled = [a for a in self.accounts if a.enabled and a.name not in exclude]
            if not all_enabled:
                raise PoolError("账号池里没有可用账号（accounts.json 为空或全部禁用）")
            candidates = all_enabled
            self.log("没有健康账号，尝试对现有账号续期/重登")

        last: Optional[Exception] = None
        for acc in self._ordered(candidates):
            try:
                self.ensure_session(acc)
            except (PoolError, AccountError) as exc:
                last = exc
                self.log(f"账号 {acc.name} 不可用，换下一个：{exc}")
                continue
            if self.daily_only() and not (acc.daily_balance or 0) > 0:
                last = PoolError(f"账号 {acc.name} 活动余额已用尽")
                continue
            acc.last_used = time.time()
            return acc
        raise PoolError(f"没有可用账号：{last}" if last else "没有可用账号")

    def report_success(self, acc: Account, usage: Optional[Dict] = None) -> None:
        acc.requests += 1
        points = (usage or {}).get("points_consumed")
        if isinstance(points, (int, float)):
            spent = int(points)
            acc.points_used += spent
            if acc.available is not None:
                acc.available = max(0, int(acc.available) - spent)
            # Also age the activity grant.  Routing under ``daily_only`` reads
            # ``daily_balance``; leaving it stale kept picking an account whose
            # grant was already spent until the next 30-minute refresh, and the
            # upstream answers those requests with an empty stream.  The daily
            # grant is consumed first, so only the overflow touches the
            # permanent balance.
            if acc.daily_balance is not None:
                used_daily = min(spent, int(acc.daily_balance))
                acc.daily_balance = int(acc.daily_balance) - used_daily
                if acc.balance is not None:
                    acc.balance = max(0, int(acc.balance) - (spent - used_daily))
        acc.last_error = ""
        self.save()

    @staticmethod
    def is_content_rejection(status: int, reason: str = "") -> bool:
        """True when a 403 is the provider's *content inspection*, not auth.

        Such a reply says nothing about the account: the session is still
        valid, so dropping it would force a manual re-login, and cooling the
        account down is pointless because every other account would reject the
        identical request body too.
        """
        if status != 403:
            return False
        return any(marker in (reason or "") for marker in C.CONTENT_FAULT_MARKERS)

    def report_failure(self, acc: Account, status: int, reason: str = "") -> None:
        acc.failures += 1
        acc.last_error = reason or f"HTTP {status}"
        if self.is_content_rejection(status, reason):
            # the payload was rejected, not the credentials — keep the session
            # and leave the account usable for the next (different) request
            self.log(f"账号 {acc.name} HTTP {status} 内容审核拒绝（保留 session，"
                     f"不冷却）：{acc.last_error}")
            self.save()
            return
        if status in (401, 403):
            acc.session, acc.expire_at = "", 0
        cooldown = float(self.cfg.get("cooldown_seconds") or 300)
        acc.cooldown(cooldown, acc.last_error)
        self.log(f"账号 {acc.name} 失败 HTTP {status} → 冷却 {cooldown:.0f}s（{acc.last_error}）")
        self.save()

    def snapshot(self) -> Dict:
        return {
            "strategy": self.cfg.get("strategy"),
            "count": len(self.accounts),
            "usable": len(self.usable()),
            "accounts": [a.public_dict() for a in self.accounts],
        }

    # --------------------------------------------------------- background

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="account-keeper",
                                        daemon=True)
        self._thread.start()
        self.log("账号守护线程已启动（续期 + 额度刷新）")

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        interval = max(60, int(float(self.cfg.get("quota_refresh_minutes") or 30) * 60))
        while not self._stop.is_set():
            if self._stop.wait(interval):
                break
            try:
                self.tick()
            except Exception as exc:                    # noqa: BLE001
                self.log(f"账号守护线程异常：{exc}")

    def _maintain(self, acc: Account, upgrade_days: float) -> None:
        try:
            if (upgrade_days > 0 and not acc.has_password and acc.session_valid
                    and acc.days_left is not None
                    and acc.days_left <= upgrade_days):
                self.ensure_password(acc)
            self.ensure_session(acc)
            self.refresh_quota(acc)
        except (AccountError, PoolError) as exc:
            self.log(f"账号 {acc.name} 维护失败：{exc}")
        except Exception as exc:                        # noqa: BLE001
            self.log(f"账号 {acc.name} 维护异常：{exc}")

    def tick(self) -> None:
        """One maintenance pass: renew sessions, refresh quota.

        Password-less accounts are upgraded shortly before their session
        expires (``password_set_before_days``) so they never fall back to
        needing a human-read SMS code.

        Accounts are independent and each step is a network round-trip, so
        they run concurrently; a pool of N accounts used to take N times the
        per-account latency (minutes on a phone).
        """
        upgrade_days = float(self.cfg.get("password_set_before_days")
                             if self.cfg.get("password_set_before_days") is not None
                             else 1)
        targets = [a for a in self.accounts if a.enabled]
        if not targets:
            return
        with ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
            list(pool.map(lambda a: self._maintain(a, upgrade_days), targets))
        self.save()

    def bootstrap(self) -> None:
        """Login/Fill any account that has no usable session.

        Concurrent for the same reason as :meth:`tick`: expired sessions in a
        large pool would otherwise be re-logged in one by one.
        """
        targets = [a for a in self.accounts
                   if a.enabled and not a.session_valid]
        if not targets:
            return

        def login(acc: Account) -> None:
            try:
                self.ensure_session(acc)
            except Exception as exc:                    # noqa: BLE001
                self.log(f"账号 {acc.name} 启动登录失败：{exc}")

        with ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
            list(pool.map(login, targets))
        self.save()
