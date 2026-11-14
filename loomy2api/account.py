"""Account model + iFlytek account-service client.

One :class:`Account` is one Loomy/iFlytek account: phone number, password,
and the 14-day session that the account service hands out.  The session is
exactly what the model gateway wants as a Bearer token, so "logging in
server-side" is all that stands between us and a usable API key.

Login paths (both fully server-side, no desktop client involved):

* password — ``/login/account/getPuKey`` returns a 1024-bit RSA public key
  **and** an ``rcode``; the password is RSA/PKCS#1 v1.5 encrypted and posted to
  ``/login/account/byPwd``.  No captcha is involved: ``rcode`` is a server
  nonce, so this is fully automatable.
* SMS — ``/login/phone/sendMsgCode`` → ``msgid``, then
  ``/login/phone/checkCode``.  Needs a human to read the code once.

Each account carries a per-account **identity** (see :func:`new_identity`): the
only device-ish fields the protocol actually carries are ``devid``, ``ua``,
``modelid``/``version`` in the request envelope plus a per-request random
``traceid``.  Binding a distinct identity per account keeps accounts visually
separate instead of all announcing ``devid=web``; it does **not** change the
network origin, which is what most risk control looks at.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from . import constants as C
from .crypto import rsa_encrypt
from .httpc import request as http_request
from .signer import build_headers

__all__ = ["Account", "AccountClient", "AccountError", "new_identity",
           "IDENTITY_FIELDS", "generate_password"]


class AccountError(RuntimeError):
    """Raised for account-service business errors."""

    def __init__(self, message: str, code: str = "", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


#: Account-service codes meaning "the credentials are wrong / logins are
#: throttled".  Retrying cannot fix either; only a new password or waiting can.
CREDENTIAL_ERROR_CODES = frozenset({
    "0200103",   # 账号鉴权失败 / password rejected
    "220204",    # 密码错误
    "220205",    # 密码错误次数过多，请10分钟后登录
})


def is_credential_error(exc: BaseException) -> bool:
    """True when ``exc`` says the stored password (not the session) is bad."""
    if not isinstance(exc, AccountError):
        return False
    if str(exc.code) in CREDENTIAL_ERROR_CODES:
        return True
    text = str(exc)
    return "密码错误" in text or "鉴权失败" in text


# --------------------------------------------------------------------- identity

#: Fields that make up an account identity, i.e. the values a request envelope
#: announces about "which device is this".
IDENTITY_FIELDS = ("devid", "ua", "modelid", "version", "campus_device_id")


def new_identity(mode: str = "per_account", *, rng=None) -> Dict[str, Any]:
    """Build a device identity for one account.

    ``mode``:

    * ``per_account`` (default) — a distinct ``devid`` per account
      (``web-<16 hex>``), a distinct promotions ``campus_device_id``. ``ua`` /
      ``modelid`` / ``version`` stay the real client's values, because a
      made-up UA is more conspicuous than a repeated one.
    * ``client`` — mirror the shipped client exactly (``devid: "web"``), for
      when you want byte-identical requests.

    ``traceid`` is deliberately *not* part of the identity: the real client
    generates a fresh one per request.
    """
    ident: Dict[str, Any] = {
        "devid": C.DEVICE_ID if mode == "client" else f"web-{uuid.uuid4().hex[:16]}",
        "ua": C.CLIENT_UA,
        "modelid": C.WEB_MODEL_ID,
        "version": C.CLIENT_VERSION,
        "campus_device_id": f"{C.CAMPUS_DEVICE_ID_PREFIX}{uuid.uuid4()}",
        "created_at": int(time.time()),
    }
    if mode == "client":
        ident["campus_device_id"] = ""
    return ident


@dataclass
class Account:
    """A single account plus its cached session/ quota state."""

    name: str
    loginid: str = ""            # phone number used to log in
    password: str = ""
    enabled: bool = True
    persist: bool = True         # False for accounts derived from the client

    session: str = ""
    userid: str = ""
    expire_at: int = 0           # unix seconds
    obtained_at: int = 0
    #: unix seconds when :meth:`AccountClient.set_password` last succeeded.
    #: A stored password makes the account renewable forever ("permanent").
    password_set_at: int = 0
    #: unix seconds when that password was proven by an actual ``byPwd`` login.
    #: The account service can report success while the password is not usable
    #: yet (``0200103``), so a password is only trusted after this check.
    password_verified_at: int = 0
    #: unix seconds of the last automatic password repair (any outcome).
    #: Repairing rotates the upstream password, which logs the account out on
    #: every other device ("顶号"), so it is capped to once per day per account
    #: — a failing repair must not retry in a loop.
    password_repaired_at: int = 0

    #: per-account device identity (devid / ua / campus device id / …)
    identity: Dict[str, Any] = field(default_factory=dict)

    #: Invitation state, cached from ``points/first-login`` / activation.
    #: ``None`` means "not known yet" (never queried) — the UI shows "未知"
    #: rather than claiming the account is unactivated.
    activated: Optional[bool] = None
    applied_invitation_code: str = ""

    #: Total lifetime ledger rows reported by ``/points/records`` — a cheap
    #: proxy for how old/busy the account is.  A freshly registered number has
    #: a handful of rows; a recycled number that already belonged to someone
    #: else has thousands.  ``None`` until the quota has been read once.
    ledger_total: Optional[int] = None
    #: ``True`` when this phone number was already an existing Loomy account
    #: before we registered it.  Detected at registration time: the one-time
    #: reward call answers ``alreadyProcessed`` (someone else already claimed
    #: it).  ``None`` = unknown (account added before this check existed, or
    #: imported via session).
    recycled: Optional[bool] = None
    #: ``True`` when the upstream reported the account itself as disabled
    #: (banned / suspended) — see ``constants.BANNED_MARKERS``.  Not recoverable
    #: by re-login, so the account is flagged and dropped from routing.
    banned: bool = False
    #: unix seconds when the ban was first observed.
    banned_at: int = 0

    #: Model ids this account is entitled to, as last reported by its own
    #: ``/models`` call.  The catalogue is **per-account**: the upstream
    #: silently serves a different (cheaper/default) model when an account
    #: asks for one it does not have, answering HTTP 200 with the substituted
    #: id — so routing a request to the wrong account degrades output without
    #: any error.  Distinct from "not fetched yet": the empty tuple means the
    #: account returned no usable catalogue, ``None`` means unknown.
    models: Optional[Tuple[str, ...]] = None
    #: unix seconds when :attr:`models` was last confirmed upstream.
    models_updated_at: int = 0
    #: Guards read-modify-write on :attr:`models`.  The probe thread rewrites
    #: it while concurrent requests may drop an id after observing a silent
    #: substitution; without a shared lock one update silently undoes another.
    models_lock: threading.Lock = field(default_factory=threading.Lock,
                                        repr=False, compare=False)

    # quota cache (filled from /points/records)
    balance: Optional[int] = None
    daily_balance: Optional[int] = None
    available: Optional[int] = None
    multiplier: float = 0.0      # not used for routing, informational
    quota_updated_at: int = 0

    # A quota rejection survives restarts so the pool cannot hammer the same
    # account every five minutes. Other runtime counters remain in memory.
    cooldown_until: float = 0.0
    cooldown_reason: str = "transient"
    #: Don't attempt a password login before this time.  Set when the account
    #: service rejects the stored password (or rate-limits logins): retrying
    #: inside the window only extends the upstream lockout, while the session
    #: we already have usually keeps working.
    login_blocked_until: float = 0.0
    #: When the quota was last *attempted* (successful or not).  The panel
    #: polls every 10 s; without this a failing account would be re-probed
    #: (and possibly re-logged in) on every poll.
    quota_checked_at: float = 0.0
    #: Consecutive 401/403 rejections seen by the *quota* read path.  The
    #: upstream emits transient ones under burst load; only a run of them means
    #: the session is really dead.  Reset to 0 on any successful read.
    quota_auth_faults: int = 0
    last_used: float = 0.0
    requests: int = 0
    points_used: int = 0
    failures: int = 0
    last_error: str = ""
    source: str = "config"       # config | client

    # ------------------------------------------------------------------
    @property
    def session_valid(self) -> bool:
        return bool(self.session) and (self.expire_at == 0 or self.expire_at > time.time())

    @property
    def days_left(self) -> Optional[float]:
        if not self.expire_at:
            return None
        return (self.expire_at - time.time()) / 86400

    @property
    def has_password(self) -> bool:
        """A stored password means the session can always be renewed."""
        return bool(self.loginid and self.password)

    @property
    def permanent(self) -> bool:
        """Upgraded account: it survives session expiry via password login.

        Requires the password to have been proven by a real login — the
        account service sometimes accepts ``update/pwd`` yet rejects the
        password at ``byPwd`` (``0200103``).
        """
        return self.has_password and bool(self.password_verified_at)

    @property
    def in_cooldown(self) -> bool:
        return self.cooldown_until > time.time()

    def cooldown(self, seconds: float, reason: str = "") -> None:
        self.cooldown_until = time.time() + max(1.0, float(seconds))
        self.cooldown_reason = "transient"
        self.failures += 1
        if reason:
            self.last_error = reason

    def clear_cooldown(self, *, quota: bool = False) -> None:
        """Clear a short cooldown, or an explicitly requested quota reset."""
        if not quota and self.in_cooldown and self.cooldown_reason == "quota":
            return
        self.cooldown_until = 0.0
        self.cooldown_reason = "transient"

    # ------------------------------------------------------------------
    @classmethod
    def from_dict(cls, raw: Dict[str, Any], index: int = 0) -> "Account":
        name = str(raw.get("name") or raw.get("loginid") or f"account{index + 1}")
        identity = raw.get("identity")
        return cls(
            name=name,
            loginid=str(raw.get("loginid") or raw.get("phone") or ""),
            password=str(raw.get("password") or ""),
            enabled=raw.get("enabled", True) is not False,
            session=str(raw.get("session") or ""),
            userid=str(raw.get("userid") or ""),
            expire_at=int(raw.get("expireAt") or raw.get("expire_at") or 0),
            obtained_at=int(raw.get("obtainedAt") or raw.get("obtained_at") or 0),
            password_set_at=int(raw.get("passwordSetAt")
                                or raw.get("password_set_at") or 0),
            password_verified_at=int(raw.get("passwordVerifiedAt")
                                     or raw.get("password_verified_at") or 0),
            password_repaired_at=int(raw.get("passwordRepairedAt") or 0),
            identity=dict(identity) if isinstance(identity, dict) else {},
            activated=(raw.get("activated") if isinstance(raw.get("activated"), bool)
                       else None),
            applied_invitation_code=str(raw.get("appliedInvitationCode")
                                        or raw.get("applied_invitation_code") or ""),
            ledger_total=(int(raw["ledgerTotal"]) if raw.get("ledgerTotal") is not None
                          else (int(raw["ledger_total"]) if raw.get("ledger_total") is not None
                                else None)),
            recycled=(raw.get("recycled") if isinstance(raw.get("recycled"), bool) else None),
            models=(tuple(str(m) for m in raw["models"])
                    if isinstance(raw.get("models"), list) else None),
            models_updated_at=int(raw.get("modelsUpdatedAt") or 0),
            banned=raw.get("banned") is True,
            banned_at=int(raw.get("bannedAt") or raw.get("banned_at") or 0),
            available=(int(raw["available"]) if raw.get("available") is not None else None),
            balance=(int(raw["balance"]) if raw.get("balance") is not None else None),
            daily_balance=(int(raw["dailyBalance"])
                           if raw.get("dailyBalance") is not None else None),
            quota_updated_at=int(raw.get("quotaUpdatedAt") or 0),
            cooldown_until=float(raw.get("cooldownUntil")
                                 or raw.get("cooldown_until") or 0),
            cooldown_reason=str(raw.get("cooldownReason")
                                or raw.get("cooldown_reason") or "transient"),
        )

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "name": self.name,
            "enabled": self.enabled,
        }
        if self.loginid:
            out["loginid"] = self.loginid
        if self.password:
            out["password"] = self.password
        if self.password_set_at:
            out["passwordSetAt"] = self.password_set_at
        if self.password_verified_at:
            out["passwordVerifiedAt"] = self.password_verified_at
        if self.password_repaired_at:
            out["passwordRepairedAt"] = self.password_repaired_at
        if self.session:
            out["session"] = self.session
        if self.userid:
            out["userid"] = self.userid
        if self.expire_at:
            out["expireAt"] = self.expire_at
        if self.obtained_at:
            out["obtainedAt"] = self.obtained_at
        if self.identity:
            out["identity"] = self.identity
        # Cache the last known quota so a cold start (especially on mobile,
        # where the first panel load used to block on upstream calls) can
        # render immediately and only refresh what is actually stale.
        if self.available is not None:
            out["available"] = self.available
        if self.balance is not None:
            out["balance"] = self.balance
        if self.daily_balance is not None:
            out["dailyBalance"] = self.daily_balance
        if self.quota_updated_at:
            out["quotaUpdatedAt"] = self.quota_updated_at
        # Invitation state is cached so the account list can show it without
        # spending a round-trip per account.
        if self.activated is not None:
            out["activated"] = bool(self.activated)
        if self.applied_invitation_code:
            out["appliedInvitationCode"] = self.applied_invitation_code
        if self.ledger_total is not None:
            out["ledgerTotal"] = self.ledger_total
        if self.recycled is not None:
            out["recycled"] = bool(self.recycled)
        # Stored so a cold start can route by model before the first /models
        # probe finishes; refreshed whenever the account answers.
        if self.models is not None:
            out["models"] = list(self.models)
            if self.models_updated_at:
                out["modelsUpdatedAt"] = self.models_updated_at
        if self.cooldown_until > time.time():
            out["cooldownUntil"] = self.cooldown_until
            out["cooldownReason"] = self.cooldown_reason
        if self.banned:
            out["banned"] = True
            if self.banned_at:
                out["bannedAt"] = self.banned_at
        return out

    def identity_view(self) -> Dict[str, Any]:
        """Identity as shown in the panel / admin API (nothing secret in it)."""
        ident = self.identity or {}
        return {
            "devid": ident.get("devid", ""),
            "ua": ident.get("ua", ""),
            "modelid": ident.get("modelid", ""),
            "version": ident.get("version", ""),
            "campus_device_id": ((ident.get("campus_device_id") or "")[:32] + "…"
                                 if len(ident.get("campus_device_id") or "") > 32
                                 else ident.get("campus_device_id", "")),
            "created_at": ident.get("created_at", 0),
            "bound": bool(ident),
        }

    def public_dict(self) -> Dict[str, Any]:
        """Status view without secrets (used by /admin/accounts)."""
        out = {
            "name": self.name,
            "loginid": self.loginid,
            "userid": self.userid,
            "enabled": self.enabled,
            "has_password": self.has_password,
            "permanent": self.permanent,
            "password_set_at": self.password_set_at,
            "password_verified_at": self.password_verified_at,
            "password_ok": bool(self.password_verified_at),
            "session": (self.session[:8] + "…") if self.session else "",
            "session_days_left": (round(self.days_left, 2) if self.days_left is not None else None),
            "expire_at": self.expire_at,
            "balance": self.balance,
            "daily_balance": self.daily_balance,
            "available": self.available,
            "quota_updated_at": self.quota_updated_at,
            "requests": self.requests,
            "points_used": self.points_used,
            "failures": self.failures,
            "in_cooldown": self.in_cooldown,
            "cooldown_until": self.cooldown_until if self.in_cooldown else 0,
            "cooldown_reason": self.cooldown_reason if self.in_cooldown else "",
            "cooldown_seconds_left": (round(self.cooldown_until - time.time(), 1)
                                      if self.in_cooldown else 0),
            "source": self.source,
            "last_error": self.last_error,
            "activated": self.activated,
            "applied_invitation_code": self.applied_invitation_code,
            "obtained_at": self.obtained_at,
            "ledger_total": self.ledger_total,
            "recycled": self.recycled,
            "banned": self.banned,
            "banned_at": self.banned_at,
            # What this account can actually serve: the upstream scopes its
            # catalogue per account and silently swaps in a default otherwise.
            "models": list(self.models) if self.models is not None else None,
            "models_updated_at": self.models_updated_at,
            "identity": self.identity_view(),
        }
        return out


class AccountClient:
    """Talks to ``account_base`` (login + userinfo) on behalf of an account."""

    def __init__(self, cfg):
        self.cfg = cfg

    # -- plumbing -------------------------------------------------------

    def identity_mode(self) -> str:
        return str(self.cfg.get("identity_mode") or "per_account")

    def _base(self, identity: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        ident = dict(identity or {}) or new_identity(self.identity_mode())
        return {
            "appid": self.cfg.get("account_appid") or C.DEFAULT_ACCOUNT_APPID,
            "modelid": str(ident.get("modelid") or C.WEB_MODEL_ID),
            "version": str(ident.get("version") or C.CLIENT_VERSION),
            "devid": str(ident.get("devid") or C.DEVICE_ID),
            "ua": str(ident.get("ua") or C.CLIENT_UA),
            # the real client regenerates traceid on every single request
            "traceid": uuid.uuid4().hex,
        }

    def call(self, path: str, body: Optional[dict] = None,
             *, identity: Optional[Dict[str, Any]] = None,
             timeout: float = 30) -> Dict[str, Any]:
        body_str = json.dumps(body, ensure_ascii=False) if body else ""
        headers = build_headers(
            self.cfg.ak_id, self.cfg.ak_secret,
            method="POST", path=path, body=body_str,
        )
        url = f"{str(self.cfg['account_base']).rstrip('/')}{path}"
        try:
            status, _hdrs, data = http_request(
                url, method="POST", headers=headers,
                body=body_str.encode("utf-8") if body_str else None,
                timeout=timeout, proxy=str(self.cfg.get("proxy") or ""),
            )
        except Exception as exc:                        # noqa: BLE001
            raise AccountError(f"account service unreachable: {exc}",
                               code="NETWORK", retryable=True) from exc

        try:
            payload = json.loads(data.decode("utf-8"))
        except Exception:                               # noqa: BLE001
            raise AccountError(
                f"account service returned HTTP {status}: "
                f"{data[:200].decode('utf-8', 'replace')}", code=f"HTTP_{status}",
                retryable=status >= 500)

        code = str(payload.get("code") or payload.get("errorCode") or "")
        if code and code != "000000":
            msg = payload.get("desc") or payload.get("message") or code
            raise AccountError(f"{path} failed: {msg}", code=code,
                               retryable=code in ("100001", "100002"))
        if status >= 400 and "message" in payload:
            raise AccountError(f"{path} HTTP {status}: {payload['message']}",
                               code=f"HTTP_{status}", retryable=status >= 500)
        return payload

    def ensure_identity(self, account: "Account") -> Dict[str, Any]:
        """Give the account an identity if it does not have one yet."""
        if not account.identity:
            account.identity = new_identity(self.identity_mode())
        return account.identity

    def rebind_identity(self, account: "Account") -> Dict[str, Any]:
        """Generate a fresh identity for the account (new "device")."""
        account.identity = new_identity(self.identity_mode())
        return account.identity

    # -- login ----------------------------------------------------------

    def password_status(self, session: str,
                        identity: Optional[Dict[str, Any]] = None) -> bool:
        """True when the account already has a login password upstream.

        The account service exposes this as ``pwdstatus`` (1 = set).  A
        password is what makes unattended re-login possible, so the pool uses
        this to decide whether an account still needs one.
        """
        payload = self.call("/userinfo/query/getStatus",
                            {"base": self._base(identity),
                             "param": {"session": session}},
                            identity=identity)
        data = payload.get("data") or {}
        return bool(int(data.get("pwdstatus") or 0))

    def set_password(self, session: str, new_password: str,
                     identity: Optional[Dict[str, Any]] = None) -> None:
        """Set the account's login password using a valid session.

        ``POST /userinfo/update/pwd`` only needs the session — no SMS, no old
        password.  The password is RSA/PKCS#1 v1.5 encrypted and sent as
        ``npwd`` (the field name matters: anything else is rejected as an
        invalid parameter).  There is no endpoint to clear it again.
        """
        if not session:
            raise AccountError("设置密码需要有效 session", code="NO_SESSION")
        pukey, rcode = self.get_public_key(identity)
        payload = self.call("/userinfo/update/pwd", {
            "base": self._base(identity),
            "param": {
                "session": session,
                "npwd": rsa_encrypt(pukey, new_password),
                "rcode": rcode,
            },
        }, identity=identity)
        if str(payload.get("code") or "") != "000000":
            raise AccountError(
                f"set password failed: {payload.get('desc') or payload}",
                code=str(payload.get("code") or "SET_PASSWORD"))

    def get_public_key(self, identity: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
        """→ ``(pukey_b64, rcode)``.  Zero side effects: a good first probe."""
        payload = self.call("/login/account/getPuKey", {"base": self._base(identity)})
        data = payload.get("data") or {}
        pukey, rcode = data.get("pukey") or "", data.get("rcode") or ""
        if not pukey:
            raise AccountError("getPuKey returned no public key", code="NO_PUKEY")
        return pukey, rcode

    def login_by_password(self, loginid: str, password: str,
                          identity: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        """Full password login → ``{session, userid, phone}``."""
        pukey, rcode = self.get_public_key(identity)
        encrypted = rsa_encrypt(pukey, password)
        payload = self.call("/login/account/byPwd", {
            "base": self._base(identity),
            "param": {
                "loginid": loginid,
                "password": encrypted,
                "rcode": rcode,
                "type": 1,
                "expire": C.SESSION_EXPIRE_SECONDS,
            },
        })
        return _extract_session(payload)

    def send_sms_code(self, phone: str,
                      identity: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.call("/login/phone/sendMsgCode", {
            "base": self._base(identity),
            "param": {"ccode": "86", "phone": phone, "expire": 300},
        })

    def login_by_sms(self, phone: str, code: str, msgid: str,
                     identity: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        payload = self.call("/login/phone/checkCode", {
            "base": self._base(identity),
            "param": {"ccode": "86", "phone": phone, "mcode": code, "msgid": msgid,
                      "expire": C.SESSION_EXPIRE_SECONDS},
        })
        return _extract_session(payload)

    def query_base_info(self, session: str,
                        identity: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.call("/userinfo/query/baseInfo",
                         {"base": self._base(identity), "param": {"session": session}})

    def logout(self, session: str,
               identity: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.call("/login/account/logout",
                         {"base": self._base(identity), "param": {"session": session}})

    # -- convenience ----------------------------------------------------

    def fill(self, account: Account) -> Account:
        """Log the account in (password path) and refresh its session fields.

        Also binds the account's own identity — created on first login, then
        reused for every later request so the account keeps looking like one
        consistent device.
        """
        if not account.loginid or not account.password:
            raise AccountError(
                f"account {account.name}: loginid/password missing",
                code="NO_CREDENTIALS")
        identity = self.ensure_identity(account)
        got = self.login_by_password(account.loginid, account.password, identity)
        account.session = got["session"]
        account.userid = got.get("userid", "")
        account.obtained_at = int(time.time())
        account.expire_at = account.obtained_at + C.SESSION_EXPIRE_SECONDS
        return account


#: Password alphabet for auto-generated credentials.  The account service
#: rejects "too simple" values (20012), so every generated password mixes
#: upper/lower case and digits and is 16 characters long.
_PWD_LOWER = "abcdefghijkmnpqrstuvwxyz"
_PWD_UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ"
_PWD_DIGITS = "23456789"


def generate_password(length: int = 16) -> str:
    """Random password that passes the account service's complexity check."""
    if length < 8:
        length = 8
    chars = [secrets.choice(_PWD_DIGITS), secrets.choice(_PWD_UPPER),
             secrets.choice(_PWD_LOWER)]
    pool = _PWD_LOWER + _PWD_UPPER + _PWD_DIGITS
    chars += [secrets.choice(pool) for _ in range(length - len(chars))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def _extract_session(payload: Dict[str, Any]) -> Dict[str, str]:
    """Pull session/userid/phone out of whatever shape the server returns."""
    node = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(node, dict):
        raise AccountError(f"unexpected login payload: {payload}")
    session = str(node.get("session") or "")
    if not session:
        raise AccountError(f"login succeeded but returned no session: {payload}")
    userid = str(node.get("userid") or node.get("userId") or "")
    phone = str(node.get("phone") or node.get("loginid") or "")
    return {"session": session, "userid": userid, "phone": phone}
