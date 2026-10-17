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
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from . import constants as C
from .account import (Account, AccountClient, AccountError, generate_password)
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
        self._file = cfg.path("accounts_file", "accounts.json")
        self.load()

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
            self.log(f"账号 {acc.name} 密码验证失败：{exc}")
            self.save()
            return False
        acc.password_verified_at = int(time.time())
        if got.get("session"):
            acc.session = got["session"]
            acc.userid = got.get("userid") or acc.userid
            acc.obtained_at = int(time.time())
            acc.expire_at = acc.obtained_at + C.SESSION_EXPIRE_SECONDS
        self.save()
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
        """
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
            needs = True
            self.log(f"账号 {acc.name} session 剩余 {acc.days_left:.1f} 天，提前续期")
        if not needs:
            return acc
        if not acc.has_password or not acc.password_verified_at:
            # last chance: the session still works, so we can set (or prove)
            # a password now and make every future expiry automatic
            if acc.session_valid:
                self.ensure_password(acc)
        if not (acc.loginid and acc.password):
            raise PoolError(
                f"账号 {acc.name} 的 session 不可用且没有账号密码，无法自动重登"
                f"（请在 accounts.json 补 loginid/password，或用 `loomy2api login` 短信登录）")
        self.client.fill(acc)
        # a successful password login is the strongest proof the password works
        if acc.has_password:
            acc.password_verified_at = int(time.time())
        self.save()
        self.log(f"账号 {acc.name} 登录成功 userid={acc.userid} "
                 f"session={acc.session[:8]}… 剩余 {(acc.days_left or 0):.1f} 天")
        return acc

    def refresh_quota(self, acc: Account) -> Account:
        """Refresh one account's points; never raises.

        This runs from the panel, the CLI and the background keeper — any of
        which can be offline, behind a dead proxy, or talking to an upstream
        that is simply down. A failed refresh must degrade to "quota unknown",
        not crash the caller (a socket/DNS error is *not* an UpstreamError).
        """
        if not acc.session:
            return acc
        try:
            quota = self.gateway.quota(acc.session)
        except UpstreamError as exc:
            if exc.status in (401, 403):
                acc.session, acc.expire_at = "", 0
                self.log(f"账号 {acc.name} session 被上游拒绝（HTTP {exc.status}）")
            else:
                acc.last_error = f"quota: {exc}"
                self.log(f"账号 {acc.name} 额度查询失败：{exc}")
            return acc
        except Exception as exc:                        # noqa: BLE001
            acc.last_error = f"quota: {exc}"
            self.log(f"账号 {acc.name} 额度查询异常（网络？）：{exc}")
            return acc
        acc.balance = quota.get("balance")
        acc.daily_balance = quota.get("daily_balance")
        acc.available = quota.get("available")
        acc.quota_updated_at = int(time.time())
        return acc

    # ------------------------------------------------------------ routing

    def usable(self, exclude: Sequence[str] = ()) -> List[Account]:
        now = time.time()
        return [a for a in self.accounts
                if a.enabled and not a.in_cooldown and a.name not in exclude
                and a.session_valid and a.available != 0]

    def acquire(self, exclude: Sequence[str] = ()) -> Account:
        """Return the next account to use (session guaranteed valid)."""
        candidates = self.usable(exclude)
        if not candidates:
            # nothing healthy: give single-account setups a chance to self-heal
            all_enabled = [a for a in self.accounts if a.enabled and a.name not in exclude]
            if not all_enabled:
                raise PoolError("账号池里没有可用账号（accounts.json 为空或全部禁用）")
            candidates = all_enabled
            self.log("没有健康账号，尝试对现有账号续期/重登")

        strategy = str(self.cfg.get("strategy") or "balance").lower()
        if strategy == "round_robin":
            with self._rr_lock:
                acc = candidates[self._rr_index % len(candidates)]
                self._rr_index = (self._rr_index + 1) % max(1, len(candidates))
        elif strategy == "lru":
            acc = min(candidates, key=lambda a: a.last_used or 0)
        elif strategy in ("daily", "activity"):
            # Spend the expiring daily grant first: an unused daily balance is
            # lost at the cycle reset, while the permanent balance carries over.
            acc = max(candidates, key=lambda a: (
                a.daily_balance if a.daily_balance is not None else -1,
                a.available if a.available is not None else 0,
                -a.last_used))
        elif strategy == "permanent":
            # The opposite: drain permanent points first and keep the daily
            # grant for accounts that are about to reset.
            acc = max(candidates, key=lambda a: (
                a.balance if a.balance is not None else -1,
                -a.last_used))
        else:  # balance
            acc = max(candidates, key=lambda a: (a.available if a.available is not None else 10**9,
                                                 -a.last_used))

        self.ensure_session(acc)
        acc.last_used = time.time()
        return acc

    def report_success(self, acc: Account, usage: Optional[Dict] = None) -> None:
        acc.requests += 1
        points = (usage or {}).get("points_consumed")
        if isinstance(points, (int, float)):
            acc.points_used += int(points)
            if acc.available is not None:
                acc.available = max(0, int(acc.available) - int(points))
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

    def tick(self) -> None:
        """One maintenance pass: renew sessions, refresh quota.

        Password-less accounts are upgraded shortly before their session
        expires (``password_set_before_days``) so they never fall back to
        needing a human-read SMS code.
        """
        upgrade_days = float(self.cfg.get("password_set_before_days")
                             if self.cfg.get("password_set_before_days") is not None
                             else 1)
        for acc in self.accounts:
            if not acc.enabled:
                continue
            try:
                if (upgrade_days > 0 and not acc.has_password and acc.session_valid
                        and acc.days_left is not None
                        and acc.days_left <= upgrade_days):
                    self.ensure_password(acc)
                self.ensure_session(acc)
                self.refresh_quota(acc)
            except (AccountError, PoolError) as exc:
                self.log(f"账号 {acc.name} 维护失败：{exc}")
            except Exception as exc:                    # noqa: BLE001
                self.log(f"账号 {acc.name} 维护异常：{exc}")
        self.save()

    def bootstrap(self) -> None:
        """Login/Fill any account that has no usable session."""
        for acc in self.accounts:
            if acc.enabled and not acc.session_valid:
                try:
                    self.ensure_session(acc)
                except Exception as exc:                # noqa: BLE001
                    self.log(f"账号 {acc.name} 启动登录失败：{exc}")
        self.save()
