"""接码注册：fysms 取号 → 讯飞短信登录 → 自动设密码。

一次注册 = 一次取号（收费）+ 一条短信。所有入口都必须显式 ``confirm``，
避免误点直接扣费。单号失败只记录，不中断整批。
"""

from __future__ import annotations

import random
import time
from typing import Any, Callable, Dict, List, Optional

from .account import Account, AccountError
from .fysms import DEFAULT_KEYWORD, FYSMS_API, FysmsClient, extract_code
from .pool import PoolError

__all__ = ["register_one", "register_batch", "fysms_status",
           "usable_invite_codes", "collect_invite_codes"]


def _log(gw, message: str) -> None:
    try:
        gw.log(f"[sms-register] {message}")
    except Exception:                                # noqa: BLE001
        pass


def usable_invite_codes(gw, donor: Account) -> List[Dict[str, Any]]:
    """The invitation codes ``donor`` can still hand out.

    A code is usable while it is ``active`` and has uses left.  The pool is
    self-sufficient: every account owns one long-term code (``maxUses=1000``)
    plus an assortment of one-shot spares, so a freshly registered number can
    always be bound to *some* existing account's code.
    """
    overview = gw.models_client.points_overview(donor.session)
    out: List[Dict[str, Any]] = []
    for c in overview.get("invitation_codes") or []:
        code = str(c.get("inviteCode") or "")
        if not code or str(c.get("status") or "") != "active":
            continue
        max_uses = int(c.get("maxUses") or 0)
        used = int(c.get("usedCount") or 0)
        if max_uses > used:
            out.append({"code": code, "max_uses": max_uses, "used": used,
                        "remaining": max_uses - used,
                        "long_term": max_uses >= 1000})
    return out


def collect_invite_codes(gw, targets: List[str]) -> List[tuple]:
    """(code, owner) pairs the given pool accounts can still hand out.

    Called *before* any number is bought: a selection whose donors have no
    usable code should be visible up front, not discovered after ``count``
    numbers have already been paid for.
    """
    out: List[tuple] = []
    for name in targets or []:
        donor = gw.pool.get(str(name))
        if donor is None or not donor.session:
            continue
        try:
            for c in usable_invite_codes(gw, donor):
                out.append((c["code"], donor.name))
        except Exception:                            # noqa: BLE001
            continue
    return out


def _apply_code(gw, acc: Account, code: str, owner: str) -> Dict[str, Any]:
    """Submit ``code`` for ``acc`` and record the outcome on the account."""
    dev = (acc.identity or {}).get("campus_device_id") or ""
    try:
        data = gw.models_client.points_activate(acc.session, dev, code)
    except Exception as exc:                         # noqa: BLE001
        return {"ok": False, "code": code, "from": owner, "error": str(exc)[:160]}
    acc.activated = bool(data.get("activated", True))
    acc.applied_invitation_code = str(data.get("appliedInvitationCode") or code)
    return {"ok": True, "code": code, "from": owner,
            "already": bool(data.get("alreadyActivated")),
            "invitee_reward": data.get("inviteeReward"),
            "inviter_reward": data.get("inviterReward"),
            "permanent_balance": data.get("permanentBalance")}


def fysms_status(username: str, password: str, base: str = "") -> Dict[str, Any]:
    """Login + balance only (no number taken, no money spent)."""
    client = FysmsClient(username, password, base=base or FYSMS_API)
    uid = client.login()
    balance = client.balance()
    return {"ok": True, "userid": uid, "balance": balance}


def register_one(gw, client: FysmsClient, *,
                 keyword: str = DEFAULT_KEYWORD,
                 max_wait: int = 150,
                 password: str = "",
                 checkin: bool = False,
                 onboarding: bool = False) -> Dict[str, Any]:
    """Register a single account. Never raises; returns a result row.

    ``password`` empty means "generate one and prove it with a real login".
    """
    row: Dict[str, Any] = {"ok": False, "phone": "", "stage": "take_number"}
    pool = gw.pool

    # 1. number (the platform freezes one code's fee until a code arrives)
    try:
        phone = client.get_phone(keyword)
    except Exception as exc:                         # noqa: BLE001
        row.update(error=f"取号失败：{exc}")
        return row
    row["phone"] = phone
    row["stage"] = "send_code"

    # 2. iFlytek SMS code
    try:
        payload = pool.client.send_sms_code(phone)
        node = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        msgid = str((node or {}).get("msgid") or "")
        if not msgid:
            raise RuntimeError(f"上游未返回 msgid：{payload}")
    except Exception as exc:                         # noqa: BLE001
        row.update(error=f"发码失败：{exc}")
        _log(gw, f"{phone} 发码失败，号码已取但未使用：{exc}")
        return row

    # 3. wait for the SMS and extract the code
    row["stage"] = "wait_sms"
    try:
        sms = client.get_code(phone, keyword, max_wait=max_wait)
    except Exception as exc:                         # noqa: BLE001
        sms = None
        row["sms_error"] = str(exc)[:200]
    if not sms:
        row["stage"] = "timeout"
        # The fee is only charged when a code is actually returned, so a
        # timeout costs nothing; ``blacklist`` here submits the order's release
        # request, which refunds the frozen amount.
        row["blacklisted"] = client.blacklist(phone)
        row.update(error="超时未收到短信（未扣码费）" +
                         ("，订单已提交释放" if row["blacklisted"] else "，订单将自动过期"))
        return row
    row["sms"] = sms[:120]
    code = extract_code(sms)
    if not code:
        row.update(stage="parse_code", error=f"未解析出验证码：{sms[:80]}")
        return row
    row["code"] = code

    # 4. exchange the code for a Loomy session
    row["stage"] = "login"
    try:
        got = pool.client.login_by_sms(phone, code, msgid)
    except Exception as exc:                         # noqa: BLE001
        row.update(error=f"讯飞登录失败：{exc}")
        return row

    # 5. persist the account
    row["stage"] = "save"
    try:
        acc = pool.get(phone)
        if acc is None:
            acc = pool.add_account(phone, loginid=phone,
                                   session=got["session"],
                                   userid=got.get("userid", ""))
        else:
            acc.session = got["session"]
            acc.userid = got.get("userid") or acc.userid
            acc.obtained_at = int(time.time())
            from . import constants as C
            acc.expire_at = acc.obtained_at + C.SESSION_EXPIRE_SECONDS
            acc.enabled = True
        pool.save(force=True)
    except Exception as exc:                         # noqa: BLE001
        row.update(error=f"保存账号失败：{exc}")
        return row

    row.update(ok=True, name=acc.name, userid=acc.userid, stage="done")

    # the upstream sometimes rejects the first request right after login
    time.sleep(0.6)

    # 6. tell a fresh number from a recycled one.  ``points/first-login`` is
    # the one-time reward call: a fresh account answers with the 15000 signup
    # bonus, while a number that was already a Loomy account before we bought
    # it answers ``alreadyProcessed`` (someone else claimed it) and yields
    # nothing.  Run it here so the batch result marks the dud numbers instead
    # of the user discovering the zero balance hours later.  It also claims
    # the bonus for genuinely fresh numbers, so a later ``checkin`` is just a
    # re-read.
    row["stage"] = "first_login"
    first_login_data: Optional[Dict[str, Any]] = None
    try:
        dev = (acc.identity or {}).get("campus_device_id") or ""
        first_login_data = gw.models_client.points_first_login(acc.session, dev)
        recycled = bool(first_login_data.get("alreadyProcessed"))
        acc.recycled = recycled
        row["recycled"] = recycled
        if recycled:
            row["recycled_note"] = "回收号：新手奖励已被领取（无 15000 永久）"
            _log(gw, f"{phone} 判定为回收号（alreadyProcessed），无注册奖励")
    except Exception as exc:                         # noqa: BLE001
        row["first_login_error"] = str(exc)[:160]

    # 7. upgrade to a permanent account (set + prove the password)
    row["stage"] = "set_password"
    try:
        row["password"] = pool.set_account_password(acc, password)
        row["permanent"] = True
    except (PoolError, AccountError) as exc:
        row["password_error"] = str(exc)[:200]
        _log(gw, f"{phone} 自动设置密码失败：{exc}")

    # 8. quota / daily check-in / onboarding
    try:
        pool.refresh_quota(acc)
    except Exception:                                # noqa: BLE001
        pass
    if checkin:
        if first_login_data is not None:
            row["checkin"] = not bool(first_login_data.get("alreadyProcessed"))
        else:
            try:
                dev = (acc.identity or {}).get("campus_device_id") or ""
                data = gw.models_client.points_first_login(acc.session, dev)
                row["checkin"] = not bool(data.get("alreadyProcessed"))
            except Exception as exc:                 # noqa: BLE001
                row["checkin_error"] = str(exc)[:160]
    if onboarding:
        done = 0
        for key in ("first_message", "pick_skill", "generate_ppt", "set_schedule",
                    "install_skill", "configure_remote", "create_soul", "share_soul"):
            try:
                data = gw.models_client.onboarding_complete(acc.session, key)
                if not data.get("alreadyCompleted"):
                    done += 1
            except Exception:                        # noqa: BLE001
                pass
        row["onboarding_done"] = done

    row["stage"] = "done"
    pool.save(force=True)
    _log(gw, f"{phone} 注册成功"
              f"{'（永久账号）' if row.get('permanent') else '（未设密码）'}"
              f"{'（回收号，无注册奖励）' if row.get('recycled') else ''}")
    return row


def register_batch(gw, count: int, *, username: str, password: str,
                   keyword: str = DEFAULT_KEYWORD, max_wait: int = 150,
                   account_password: str = "",
                   checkin: bool = False, onboarding: bool = False,
                   invite_targets: Optional[List[str]] = None,
                   on_progress: Optional[Callable[[Dict[str, Any]], None]] = None
                   ) -> Dict[str, Any]:
    """Register ``count`` accounts sequentially. Stops early when money runs out.

    ``invite_targets`` names existing pool accounts whose spare invitation
    codes the freshly registered numbers are bound to (one chosen at random
    per new account, ~5000 permanent points for each side).
    """
    client = FysmsClient(username, password,
                         base=str(gw.cfg.get("fysms_base") or FYSMS_API))
    client.login()
    balance = client.balance()

    # Resolve the deliverable codes once, before spending anything: a bad
    # selection (donor with no spare code) should be reported up front rather
    # than after ``count`` numbers have been bought.
    targets = [str(t) for t in (invite_targets or []) if str(t or "").strip()][:5]
    executed = collect_invite_codes(gw, targets)

    results: List[Dict[str, Any]] = []
    for index in range(max(1, int(count))):
        row = register_one(gw, client, keyword=keyword, max_wait=max_wait,
                           password=account_password,
                           checkin=checkin, onboarding=onboarding)
        if row.get("ok") and executed:
            acc = gw.pool.get(row.get("phone") or "")
            if acc is not None:
                code, owner = random.choice(executed)
                row["invite"] = _apply_code(gw, acc, code, owner)
                gw.pool.save(force=True)
        row["index"] = index + 1
        row["total"] = count
        results.append(row)
        if on_progress is not None:
            try:
                on_progress(row)
            except Exception:                        # noqa: BLE001
                pass
        # a missing number means the account balance is gone — stop spending
        if not row.get("phone") and "余额不足" in str(row.get("error") or ""):
            break
        # a code that never arrived means the order was released and refunded;
        # keep going on the next number rather than aborting the batch

    ok = [r for r in results if r.get("ok")]
    try:
        balance_after = client.balance()
    except Exception:                                # noqa: BLE001
        balance_after = None
    charged = (round(balance - balance_after, 2)
               if isinstance(balance, (int, float))
               and isinstance(balance_after, (int, float)) else None)
    return {
        "ok": True,
        "requested": count,
        "done": len(ok),
        "failed": len(results) - len(ok),
        "balance_before": balance,
        "balance_after": balance_after,
        # The platform only charges when a code is returned, so this is the
        # real cost of the batch (not count × price).
        "charged": charged,
        "invite_targets": targets,
        "invite_codes": len(executed),
        "results": results,
    }
