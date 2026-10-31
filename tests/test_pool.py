"""Account-pool tests: strategies, cooldown, persistence, session renewal."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from loomy2api.account import Account
from loomy2api.pool import AccountPool, PoolError
from tests.support import make_config


class TestPool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cfg = make_config(self.dir, "http://127.0.0.1:1/api/v1")

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, accounts):
        (self.dir / "accounts.json").write_text(
            json.dumps({"accounts": accounts}), encoding="utf-8")

    def test_load_and_snapshot(self):
        self._write([
            {"name": "a", "loginid": "13800000001", "password": "p",
             "session": "s1", "expireAt": int(time.time()) + 14 * 86400},
            {"name": "b", "session": "s2", "expireAt": int(time.time()) + 14 * 86400},
        ])
        pool = AccountPool(self.cfg)
        self.assertEqual([a.name for a in pool.accounts], ["a", "b"])
        snap = pool.snapshot()
        self.assertEqual(snap["count"], 2)
        self.assertEqual(snap["usable"], 2)
        self.assertNotIn('"password":', json.dumps(snap))   # secrets stay out

    def test_balance_strategy_prefers_richest(self):
        self._write([
            {"name": "poor", "session": "s1", "expireAt": int(time.time()) + 14 * 86400},
            {"name": "rich", "session": "s2", "expireAt": int(time.time()) + 14 * 86400},
        ])
        pool = AccountPool(self.cfg)
        pool.get("poor").available = 10
        pool.get("rich").available = 900
        self.assertEqual(pool.acquire().name, "rich")

    def test_round_robin_strategy(self):
        self._write([
            {"name": "a", "session": "s1", "expireAt": int(time.time()) + 14 * 86400},
            {"name": "b", "session": "s2", "expireAt": int(time.time()) + 14 * 86400},
        ])
        self.cfg["strategy"] = "round_robin"
        pool = AccountPool(self.cfg)
        self.assertEqual([pool.acquire().name for _ in range(4)], ["a", "b", "a", "b"])

    def test_lru_strategy(self):
        self._write([
            {"name": "old", "session": "s1", "expireAt": int(time.time()) + 14 * 86400},
            {"name": "new", "session": "s2", "expireAt": int(time.time()) + 14 * 86400},
        ])
        self.cfg["strategy"] = "lru"
        pool = AccountPool(self.cfg)
        pool.get("old").last_used = 100
        pool.get("new").last_used = 200
        self.assertEqual(pool.acquire().name, "old")

    def test_cooldown_skips_account_and_recovers(self):
        self._write([
            {"name": "a", "session": "s1", "expireAt": int(time.time()) + 14 * 86400},
            {"name": "b", "session": "s2", "expireAt": int(time.time()) + 14 * 86400},
        ])
        pool = AccountPool(self.cfg)
        pool.report_failure(pool.get("a"), 401, "invalid session")
        self.assertEqual(pool.acquire().name, "b")
        self.assertEqual(pool.usable()[0].name, "b")
        # a 401 clears the session, so it needs credentials to come back
        self.assertFalse(pool.get("a").session_valid)

    def test_exhausted_account_is_skipped(self):
        self._write([
            {"name": "a", "session": "s1", "expireAt": int(time.time()) + 14 * 86400},
            {"name": "b", "session": "s2", "expireAt": int(time.time()) + 14 * 86400},
        ])
        pool = AccountPool(self.cfg)
        pool.get("a").available = 0
        self.assertEqual(pool.acquire().name, "b")

    def test_acquire_raises_when_pool_empty(self):
        self._write([])
        pool = AccountPool(self.cfg)
        with self.assertRaises(PoolError):
            pool.acquire()

    def test_acquire_skips_account_whose_renewal_fails(self):
        """One broken account must not fail the request for the whole pool."""
        self._write([
            # session already gone, so the failed login really makes it unusable
            {"name": "broken", "loginid": "13800000001", "password": "wrong",
             "passwordVerifiedAt": int(time.time()),
             "session": "s1", "expireAt": int(time.time()) - 60},
            {"name": "healthy", "session": "s2",
             "expireAt": int(time.time()) + 14 * 86400},
        ])
        pool = AccountPool(self.cfg)
        pool.get("healthy").available = 1

        class FakeClient:
            def fill(self, account):
                from loomy2api.account import AccountError
                raise AccountError("密码错误", code="220204")

        pool.client = FakeClient()
        self.assertEqual(pool.acquire().name, "healthy")

    def test_acquire_reports_last_failure_when_all_candidates_fail(self):
        self._write([
            {"name": "broken", "loginid": "13800000001", "password": "wrong",
             "session": "expired", "expireAt": int(time.time()) - 60},
        ])
        pool = AccountPool(self.cfg)
        pool.get("broken").login_blocked_until = time.time() + 300
        with self.assertRaises(PoolError) as ctx:
            pool.acquire()
        self.assertIn("密码登录已暂停", str(ctx.exception))

    def test_daily_strategy_orders_by_daily_balance(self):
        """``daily`` = spend the expiring grant first (activity balance)."""
        self._write([
            {"name": "low_daily", "session": "s1",
             "expireAt": int(time.time()) + 14 * 86400},
            {"name": "high_daily", "session": "s2",
             "expireAt": int(time.time()) + 14 * 86400},
        ])
        self.cfg["strategy"] = "daily"
        pool = AccountPool(self.cfg)
        pool.get("low_daily").daily_balance = 100
        pool.get("high_daily").daily_balance = 5000
        pool.get("low_daily").available = 90000   # balance must not win
        self.assertEqual(pool.acquire().name, "high_daily")

    def test_disabled_account_ignored(self):
        self._write([
            {"name": "a", "session": "s1", "expireAt": int(time.time()) + 86400,
             "enabled": False},
            {"name": "b", "session": "s2", "expireAt": int(time.time()) + 14 * 86400},
        ])
        pool = AccountPool(self.cfg)
        self.assertEqual([a.name for a in pool.usable()], ["b"])

    def test_add_remove_persists(self):
        self._write([])
        pool = AccountPool(self.cfg)
        pool.add_account("x", loginid="13800000009", password="pw")
        pool.flush()
        saved = json.loads((self.dir / "accounts.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["accounts"][0]["name"], "x")
        self.assertTrue(pool.remove_account("x"))
        self.assertFalse(pool.remove_account("x"))

    def test_duplicate_add_rejected(self):
        self._write([{"name": "x", "session": "s", "expireAt": int(time.time()) + 9e4}])
        pool = AccountPool(self.cfg)
        with self.assertRaises(PoolError):
            pool.add_account("x")

    def test_report_success_decrements_quota(self):
        self._write([{"name": "a", "session": "s", "expireAt": int(time.time()) + 9e4}])
        pool = AccountPool(self.cfg)
        acc = pool.get("a")
        acc.available = 100
        pool.report_success(acc, {"points_consumed": 3})
        self.assertEqual(acc.points_used, 3)
        self.assertEqual(acc.available, 97)
        self.assertEqual(acc.requests, 1)


class TestSessionLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cfg = make_config(self.dir, "http://127.0.0.1:1/api/v1")

    def tearDown(self):
        self.tmp.cleanup()

    def test_expiring_session_triggers_relogin(self):
        from tests.support import write_accounts

        write_accounts(self.dir / "accounts.json", [
            # password already proven, so the only step left is the re-login
            {"name": "a", "loginid": "13800000001", "password": "pw",
             "passwordVerifiedAt": int(time.time()),
             "session": "stale", "expireAt": int(time.time()) + 3600},   # < 3 days
        ])
        pool = AccountPool(self.cfg)

        calls = []

        class FakeClient:
            def fill(self, account):
                calls.append(account.name)
                account.session = "fresh"
                account.expire_at = int(time.time()) + 14 * 86400
                return account

            def login_by_password(self, loginid, password, identity=None):
                return {"session": "verified", "userid": "uid-a"}

        pool.client = FakeClient()
        pool.acquire()
        self.assertEqual(calls, ["a"])
        self.assertEqual(pool.get("a").session, "fresh")

    def test_verification_login_is_not_repeated(self):
        """The login that proves a password already renews the session."""
        from tests.support import write_accounts

        write_accounts(self.dir / "accounts.json", [
            {"name": "a", "loginid": "13800000001", "password": "pw",
             "session": "stale", "expireAt": int(time.time()) + 3600},
        ])
        pool = AccountPool(self.cfg)
        fills = []

        class FakeClient:
            def fill(self, account):
                fills.append(account.name)
                account.session = "fresh"
                account.expire_at = int(time.time()) + 14 * 86400
                return account

            def login_by_password(self, loginid, password, identity=None):
                return {"session": "verified", "userid": "uid-a"}

        pool.client = FakeClient()
        acc = pool.acquire()
        self.assertEqual(fills, [])                # one login, not two
        self.assertEqual(acc.session, "verified")
        self.assertTrue(acc.password_verified_at)

    def test_failed_renewal_keeps_working_session(self):
        """A rejected password must not kill a request the session can serve.

        Regression: an account 2.9 days from expiry was force-renewed, the
        stored password was wrong, and every request died with
        ``byPwd failed: 密码错误`` although the session was still valid.
        """
        from tests.support import write_accounts

        write_accounts(self.dir / "accounts.json", [
            {"name": "a", "loginid": "13800000001", "password": "wrong",
             "passwordVerifiedAt": int(time.time()),
             "session": "good", "expireAt": int(time.time()) + 2.9 * 86400},
        ])
        pool = AccountPool(self.cfg)

        class FakeClient:
            def fill(self, account):
                from loomy2api.account import AccountError
                raise AccountError("/login/account/byPwd failed: 密码错误",
                                   code="220204")

        pool.client = FakeClient()
        acc = pool.acquire()                       # must not raise
        self.assertEqual(acc.name, "a")
        self.assertEqual(acc.session, "good")      # old session still used
        self.assertFalse(acc.password_verified_at)
        self.assertTrue(acc.login_blocked_until > time.time())

    def test_login_block_stops_retrying_and_expires(self):
        """A rejected password is not retried inside the lockout window."""
        from tests.support import write_accounts

        write_accounts(self.dir / "accounts.json", [
            {"name": "a", "loginid": "13800000001", "password": "wrong",
             "passwordVerifiedAt": int(time.time()),
             "session": "good", "expireAt": int(time.time()) + 2.9 * 86400},
        ])
        pool = AccountPool(self.cfg)
        attempts = []

        class FakeClient:
            def fill(self, account):
                from loomy2api.account import AccountError
                attempts.append(1)
                raise AccountError("/login/account/byPwd failed: 密码错误",
                                   code="220204")

            def login_by_password(self, loginid, password, identity=None):
                from loomy2api.account import AccountError
                attempts.append(1)
                raise AccountError("/login/account/byPwd failed: 密码错误",
                                   code="220204")

            def password_status(self, session, identity=None):
                return True                # don't rotate an unknown password

        pool.client = FakeClient()
        for _ in range(5):
            pool.acquire()                         # renew window each time
        self.assertEqual(len(attempts), 1)         # blocked after the first
        acc = pool.get("a")
        acc.login_blocked_until = time.time() - 1  # lockout over
        pool.acquire()
        self.assertEqual(len(attempts), 2)

    def test_rejected_password_is_repaired_automatically(self):
        """A wrong stored password is rotated through the live session.

        The session is what ``/userinfo/update/pwd`` needs, so a password
        rejected by ``byPwd`` can be fixed unattended instead of parking the
        account until someone notices.
        """
        from tests.support import write_accounts

        write_accounts(self.dir / "accounts.json", [
            {"name": "a", "loginid": "13800000001", "password": "wrong",
             "passwordVerifiedAt": int(time.time()),
             "session": "good", "expireAt": int(time.time()) + 2.9 * 86400},
        ])
        pool = AccountPool(self.cfg)
        acc = pool.get("a")
        rotated = []

        class FakeClient:
            def fill(self, account):
                from loomy2api.account import AccountError
                raise AccountError("/login/account/byPwd failed: 密码错误",
                                   code="220204")

            def set_password(self, session, new_password, identity=None):
                assert session == "good"        # uses the live session
                rotated.append(new_password)

            def login_by_password(self, loginid, password, identity=None):
                # the new password works and hands back a fresh session
                return {"session": "fresh", "userid": "uid-a"}

        pool.client = FakeClient()
        got = pool.acquire()

        self.assertEqual(len(rotated), 1)
        self.assertEqual(got.password, rotated[0])
        self.assertNotEqual(got.password, "wrong")
        self.assertEqual(got.session, "fresh")
        self.assertTrue(got.password_verified_at)
        self.assertFalse(got.login_blocked_until)
        self.assertEqual(got.last_error, "")
        saved = json.loads((self.dir / "accounts.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["accounts"][0]["password"], rotated[0])

    def test_repair_failure_falls_back_to_session_and_blocks(self):
        """If the rotation itself fails, keep the session and stop retrying."""
        from tests.support import write_accounts

        write_accounts(self.dir / "accounts.json", [
            {"name": "a", "loginid": "13800000001", "password": "wrong",
             "passwordVerifiedAt": int(time.time()),
             "session": "good", "expireAt": int(time.time()) + 2.9 * 86400},
        ])
        pool = AccountPool(self.cfg)

        class FakeClient:
            def fill(self, account):
                from loomy2api.account import AccountError
                raise AccountError("密码错误", code="220204")

            def set_password(self, session, new_password, identity=None):
                from loomy2api.account import AccountError
                raise AccountError("update/pwd rejected", code="500")

        pool.client = FakeClient()
        acc = pool.acquire()
        self.assertEqual(acc.session, "good")
        self.assertTrue(acc.login_blocked_until > time.time())

    def test_auto_repair_can_be_disabled(self):
        from tests.support import write_accounts

        write_accounts(self.dir / "accounts.json", [
            {"name": "a", "loginid": "13800000001", "password": "wrong",
             "passwordVerifiedAt": int(time.time()),
             "session": "good", "expireAt": int(time.time()) + 2.9 * 86400},
        ])
        self.cfg["auto_repair_password"] = False
        pool = AccountPool(self.cfg)

        class FakeClient:
            def fill(self, account):
                from loomy2api.account import AccountError
                raise AccountError("密码错误", code="220204")

            def set_password(self, session, new_password, identity=None):
                raise AssertionError("must not rotate")

        pool.client = FakeClient()
        acc = pool.acquire()
        self.assertEqual(acc.password, "wrong")
        self.assertEqual(acc.session, "good")

    def test_credential_error_detection(self):
        from loomy2api.account import AccountError, is_credential_error

        self.assertTrue(is_credential_error(AccountError("x", code="220204")))
        self.assertTrue(is_credential_error(
            AccountError("/login/account/byPwd failed: 密码错误次数过多，请10分钟后登录")))
        self.assertFalse(is_credential_error(AccountError("net", code="NETWORK")))
        self.assertFalse(is_credential_error(PoolError("nope")))

    def test_no_credentials_raises_actionable_error(self):
        from tests.support import write_accounts

        write_accounts(self.dir / "accounts.json", [{"name": "a"}])
        pool = AccountPool(self.cfg)
        with self.assertRaises(PoolError) as ctx:
            pool.acquire()
        self.assertIn("loginid/password", str(ctx.exception))

    def test_tick_refreshes_quota(self):
        from tests.support import write_accounts

        write_accounts(self.dir / "accounts.json", [
            {"name": "a", "session": "s", "expireAt": int(time.time()) + 14 * 86400},
        ])
        pool = AccountPool(self.cfg)

        class FakeGateway:
            def quota(self, session):
                return {"balance": 5, "daily_balance": 6, "available": 11}

        pool.gateway = FakeGateway()
        pool.tick()
        acc = pool.get("a")
        self.assertEqual((acc.balance, acc.daily_balance, acc.available), (5, 6, 11))

    def test_expiring_account_without_password_gets_one(self):
        """A session close to expiry is upgraded before it can die."""
        from tests.support import write_accounts

        write_accounts(self.dir / "accounts.json", [
            # 0.5 days left, no password, no loginid -> cannot renew
            {"name": "a", "loginid": "13800000001", "session": "s",
             "expireAt": int(time.time()) + 12 * 3600},
        ])
        pool = AccountPool(self.cfg)
        acc = pool.get("a")
        self.assertFalse(acc.has_password)

        class FakeClient:
            def __init__(self):
                self.passwords = []

            def set_password(self, session, new_password, identity=None):
                self.passwords.append((session, new_password))

            def login_by_password(self, loginid, password, identity=None):
                # the real client proves a new password with a login
                return {"session": "verified", "userid": "uid-a"}

            def fill(self, account):
                account.session = "renewed"
                account.expire_at = int(time.time()) + 14 * 86400
                return account

        client = FakeClient()
        pool.client = client
        pool.tick()

        self.assertEqual(len(client.passwords), 1)
        self.assertEqual(client.passwords[0][0], "s")
        self.assertTrue(acc.has_password)
        self.assertTrue(acc.password_verified_at)
        # the verification login already returned a fresh 14-day session, so
        # ensure_session has nothing left to renew
        self.assertEqual(acc.session, "verified")
        # persisted, so it survives a restart / travels with an export
        saved = json.loads((self.dir / "accounts.json").read_text(encoding="utf-8"))
        row = next(a for a in saved["accounts"] if a["name"] == "a")
        self.assertEqual(row["password"], acc.password)
        self.assertTrue(row.get("passwordSetAt"))

    def test_password_set_but_login_rejected_is_not_permanent(self):
        """``update/pwd`` can succeed while ``byPwd`` still refuses (0200103)."""
        from tests.support import write_accounts

        write_accounts(self.dir / "accounts.json", [
            {"name": "a", "loginid": "13800000001", "session": "s",
             "expireAt": int(time.time()) + 3600},
        ])
        pool = AccountPool(self.cfg)
        acc = pool.get("a")

        class FakeClient:
            def set_password(self, session, new_password, identity=None):
                return None                      # upstream says "成功"

            def login_by_password(self, loginid, password, identity=None):
                from loomy2api.account import AccountError
                raise AccountError("账号鉴权失败", code="0200103")

        pool.client = FakeClient()
        with self.assertRaises(PoolError) as ctx:
            pool.set_account_password(acc)
        self.assertIn("验证失败", str(ctx.exception))
        # the password is kept (the user may still need it), but the account
        # must not be advertised as permanent
        self.assertTrue(acc.password)
        self.assertFalse(acc.permanent)
        self.assertFalse(acc.password_verified_at)

    def test_unverified_password_is_checked_before_rotation(self):
        """A stored but unproven password is tested, not overwritten."""
        from tests.support import write_accounts

        write_accounts(self.dir / "accounts.json", [
            {"name": "a", "loginid": "13800000001", "password": "pw",
             "session": "s", "expireAt": int(time.time()) + 3600},
        ])
        pool = AccountPool(self.cfg)
        acc = pool.get("a")
        rotated = []

        class FakeClient:
            def login_by_password(self, loginid, password, identity=None):
                return {"session": "verified", "userid": "uid-a"}

            def set_password(self, session, new_password, identity=None):
                rotated.append(new_password)

        pool.client = FakeClient()
        self.assertFalse(pool.ensure_password(acc))
        self.assertEqual(rotated, [])
        self.assertTrue(acc.password_verified_at)
        self.assertEqual(acc.session, "verified")

    def test_upgrade_skips_account_with_upstream_password(self):
        """Never rotate a password the user set but we don't know."""
        from tests.support import write_accounts

        write_accounts(self.dir / "accounts.json", [
            {"name": "a", "loginid": "13800000001", "session": "s",
             "expireAt": int(time.time()) + 12 * 3600},
        ])
        pool = AccountPool(self.cfg)
        acc = pool.get("a")

        class FakeClient:
            def password_status(self, session, identity=None):
                return True                     # upstream already has one

            def set_password(self, session, new_password, identity=None):
                raise AssertionError("must not overwrite")

        pool.client = FakeClient()
        self.assertFalse(pool.ensure_password(acc))
        self.assertFalse(acc.has_password)

    def test_upgrade_skips_client_derived_accounts(self):
        from loomy2api.account import Account

        pool = AccountPool(self.cfg)
        acc = Account(name="desktop", loginid="13800000001",
                      session="s", expire_at=int(time.time()) + 3600)
        acc.persist = False                     # imported from the desktop app
        self.assertFalse(pool.ensure_password(acc))
        self.assertFalse(acc.has_password)

    def test_password_upgrade_waits_until_within_threshold(self):
        """3 days left is too early when the threshold is 1 day."""
        from tests.support import write_accounts

        write_accounts(self.dir / "accounts.json", [
            {"name": "a", "loginid": "13800000001", "session": "s",
             "expireAt": int(time.time()) + 3 * 86400},
        ])
        pool = AccountPool(self.cfg)
        acc = pool.get("a")

        class FakeClient:
            def set_password(self, session, new_password, identity=None):
                raise AssertionError("should not upgrade yet")

        pool.client = FakeClient()
        pool.ensure_session(acc)          # within renew window, outside upgrade window
        self.assertFalse(acc.has_password)

    def test_permanent_account_flag_in_public_view(self):
        from tests.support import write_accounts

        write_accounts(self.dir / "accounts.json", [
            {"name": "a", "loginid": "13800000001", "password": "pw",
             "passwordVerifiedAt": int(time.time()),
             "session": "s1", "expireAt": int(time.time()) + 3600},
            {"name": "b", "session": "s2", "expireAt": int(time.time()) + 3600},
        ])
        pool = AccountPool(self.cfg)
        self.assertTrue(pool.get("a").permanent)
        self.assertFalse(pool.get("b").permanent)
        self.assertTrue(pool.get("a").public_dict()["permanent"])

    def test_quota_refresh_relogins_on_rejected_session(self):
        """A 100002 "登录已失效" payload must re-login, not report "unknown"."""
        from tests.support import write_accounts
        from loomy2api.upstream import UpstreamError

        write_accounts(self.dir / "accounts.json", [
            {"name": "a", "loginid": "13800000001", "password": "pw",
             "passwordVerifiedAt": int(time.time()),
             "session": "dead", "expireAt": int(time.time()) + 11 * 86400},
        ])
        pool = AccountPool(self.cfg)
        acc = pool.get("a")
        calls = []

        class FakeGateway:
            def quota(self, session):
                calls.append(session)
                if session == "dead":
                    raise UpstreamError("登录已失效，请重新登录", 401)
                return {"balance": 100, "daily_balance": 50, "available": 150}

        pool.gateway = FakeGateway()
        pool.client = type("C", (), {
            "fill": lambda self, a: (setattr(a, "session", "fresh"),
                                     setattr(a, "expire_at", int(time.time()) + 14 * 86400),
                                     a)[-1],
        })()
        pool.refresh_quota(acc)
        self.assertEqual(calls, ["dead", "fresh"])
        self.assertEqual(acc.available, 150)
        self.assertTrue(acc.session_valid)

    def test_quota_refresh_does_not_loop_on_failed_relogin(self):
        from tests.support import write_accounts
        from loomy2api.upstream import UpstreamError

        write_accounts(self.dir / "accounts.json", [
            {"name": "a", "loginid": "13800000001", "password": "pw",
             "session": "dead", "expireAt": int(time.time()) + 11 * 86400},
        ])
        pool = AccountPool(self.cfg)
        acc = pool.get("a")
        calls = []

        class FakeGateway:
            def quota(self, session):
                calls.append(session)
                raise UpstreamError("登录已失效", 401)

        class FakeClient:
            def fill(self, account):
                from loomy2api.account import AccountError
                raise AccountError("密码错误", code="220204")

        pool.gateway = FakeGateway()
        pool.client = FakeClient()
        pool.refresh_quota(acc)
        self.assertEqual(len(calls), 1)              # one retry, not a loop
        self.assertIn("relogin", acc.last_error)

    def test_points_records_raises_on_business_error(self):
        """HTTP 200 + code 100002 must not be parsed as a successful empty read."""
        from tests.support import FakeUpstream
        from loomy2api.upstream import ModelGateway, UpstreamError

        upstream = FakeUpstream()
        base = upstream.start()
        try:
            upstream.points_session_invalid = True
            cfg = make_config(self.dir, base)
            gw = ModelGateway(cfg)
            with self.assertRaises(UpstreamError) as ctx:
                gw.quota("whatever")
            self.assertEqual(ctx.exception.status, 401)
        finally:
            upstream.stop()

    def test_tick_clears_session_on_401(self):
        from loomy2api.upstream import UpstreamError
        from tests.support import write_accounts

        write_accounts(self.dir / "accounts.json", [
            {"name": "a", "session": "s", "expireAt": int(time.time()) + 14 * 86400},
        ])
        pool = AccountPool(self.cfg)

        class FakeGateway:
            def quota(self, session):
                raise UpstreamError("nope", 401)

        pool.gateway = FakeGateway()
        pool.tick()
        self.assertFalse(pool.get("a").session_valid)


if __name__ == "__main__":
    unittest.main()
