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

    def test_daily_only_skips_accounts_without_activity_balance(self):
        """'Only use the activity balance' must never fall back to permanent."""
        self._write([
            {"name": "permanent_only", "session": "s1",
             "expireAt": int(time.time()) + 14 * 86400},
            {"name": "has_daily", "session": "s2",
             "expireAt": int(time.time()) + 14 * 86400},
        ])
        self.cfg["daily_only"] = True
        pool = AccountPool(self.cfg)
        pool.get("permanent_only").available = 90000
        pool.get("permanent_only").daily_balance = 0
        pool.get("has_daily").available = 5000
        pool.get("has_daily").daily_balance = 5000
        self.assertEqual(pool.acquire().name, "has_daily")

    def test_daily_only_fails_instead_of_spending_permanent(self):
        self._write([
            {"name": "rich", "session": "s1", "expireAt": int(time.time()) + 14 * 86400},
        ])
        self.cfg["daily_only"] = True
        pool = AccountPool(self.cfg)
        pool.get("rich").available = 90000
        pool.get("rich").daily_balance = 0
        with self.assertRaises(PoolError) as ctx:
            pool.acquire()
        self.assertIn("活动余额", str(ctx.exception))
        self.assertEqual(pool.get("rich").available, 90000)   # untouched

    def test_daily_only_filters_usable(self):
        self._write([
            {"name": "a", "session": "s1", "expireAt": int(time.time()) + 14 * 86400},
            {"name": "b", "session": "s2", "expireAt": int(time.time()) + 14 * 86400},
        ])
        pool = AccountPool(self.cfg)
        pool.get("a").daily_balance = 0
        pool.get("b").daily_balance = 5000
        self.assertEqual(len(pool.usable()), 2)
        self.cfg["daily_only"] = True
        self.assertEqual([a.name for a in pool.usable()], ["b"])

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

    def test_report_success_ages_the_daily_grant(self):
        """``daily_only`` reads ``daily_balance``: it must not stay stale.

        Leaving it untouched kept routing to an account whose grant was spent
        until the next 30-minute refresh, and the upstream answers those with
        an empty stream (a silent truncation for the client).
        """
        self._write([{"name": "a", "session": "s", "expireAt": int(time.time()) + 9e4}])
        pool = AccountPool(self.cfg)
        acc = pool.get("a")
        acc.available = 5000
        acc.daily_balance = 5000
        acc.balance = 0
        pool.report_success(acc, {"points_consumed": 30})
        self.assertEqual(acc.daily_balance, 4970)
        self.assertEqual(acc.balance, 0)
        self.assertEqual(acc.available, 4970)

    def test_daily_grant_drains_before_permanent_balance(self):
        self._write([{"name": "a", "session": "s", "expireAt": int(time.time()) + 9e4}])
        pool = AccountPool(self.cfg)
        acc = pool.get("a")
        acc.available = 1100          # 100 daily + 1000 permanent
        acc.daily_balance = 100
        acc.balance = 1000
        pool.report_success(acc, {"points_consumed": 150})
        self.assertEqual(acc.daily_balance, 0)     # grant fully consumed
        self.assertEqual(acc.balance, 950)         # overflow hits permanent
        self.assertEqual(acc.available, 950)

    def test_exhausted_grant_drops_out_of_daily_only_routing(self):
        self._write([
            {"name": "spent", "session": "s1", "expireAt": int(time.time()) + 9e4},
            {"name": "fresh", "session": "s2", "expireAt": int(time.time()) + 9e4},
        ])
        self.cfg["daily_only"] = True
        pool = AccountPool(self.cfg)
        spent, fresh = pool.get("spent"), pool.get("fresh")
        for a, avail, daily in ((spent, 5000, 100), (fresh, 5000, 5000)):
            a.available, a.daily_balance, a.balance = avail, daily, 0
        # burn the small grant; permanent points must NOT keep it eligible
        pool.report_success(spent, {"points_consumed": 100})
        self.assertEqual(spent.daily_balance, 0)
        self.assertEqual(pool.acquire().name, "fresh")


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

    def test_forced_renew_reports_a_rejected_password(self):
        """Panel "续期重登" must fail loudly when nothing was renewed.

        With ``auto_repair_password`` off and the stored password already
        rejected, a forced renewal could not fix anything — yet it returned the
        still-valid session, so the panel said "操作完成" and the user never
        learned the password was dead.
        """
        from tests.support import write_accounts
        from loomy2api.account import AccountError

        self.cfg["auto_repair_password"] = False
        write_accounts(self.dir / "accounts.json", [
            {"name": "a", "loginid": "13800000001", "password": "wrong",
             "passwordSetAt": int(time.time()) - 25 * 86400,
             "session": "good", "expireAt": int(time.time()) + 2.7 * 86400},
        ])
        pool = AccountPool(self.cfg)

        class FakeClient:
            def fill(self, account):
                raise AccountError("/login/account/byPwd failed: 密码错误",
                                   code="220204")

        pool.client = FakeClient()
        with self.assertRaises(PoolError) as ctx:
            pool.ensure_session(pool.get("a"), force=True)
        message = str(ctx.exception)
        self.assertIn("续期失败", message)
        self.assertIn("重设密码", message)          # actionable next step
        self.assertIn("auto_repair_password", message)

    def test_failed_relogin_keeps_the_previous_session_record(self):
        """Clearing the session before trying the re-login lost it forever.

        A rejected quota read whose re-login also fails left the account with
        neither a session nor a usable password, and no record of the session
        it had used — making it impossible to tell when it actually died.
        """
        from tests.support import write_accounts
        from loomy2api.account import AccountError
        from loomy2api.upstream import UpstreamError

        self.cfg["auto_repair_password"] = False
        self.cfg["quota_auth_faults_before_relogin"] = 1
        write_accounts(self.dir / "accounts.json", [
            {"name": "a", "loginid": "13800000001", "password": "wrong",
             "session": "old-session", "expireAt": int(time.time()) + 9 * 86400},
        ])
        pool = AccountPool(self.cfg)

        class FakeGateway:
            def quota(self, session):
                raise UpstreamError("登录已失效", 401)

        class FakeClient:
            def fill(self, account):
                raise AccountError("/login/account/byPwd failed: 密码错误",
                                   code="220204")

        pool.gateway = FakeGateway()
        pool.client = FakeClient()
        acc = pool.get("a")
        before_expiry = acc.expire_at
        pool.refresh_quota(acc)
        self.assertEqual(acc.session, "old-session")   # not erased
        self.assertEqual(acc.expire_at, before_expiry)  # untouched
        self.assertIn("relogin", acc.last_error)       # reason recorded

    def test_implicit_renew_stays_quiet_on_a_rejected_password(self):
        """Routing/keeper renewal keeps the working session — no exception.

        Only an explicit panel/CLI renewal may fail; a background refresh must
        not take a request down while the session it holds still works.
        """
        from tests.support import write_accounts
        from loomy2api.account import AccountError

        self.cfg["auto_repair_password"] = False
        write_accounts(self.dir / "accounts.json", [
            {"name": "a", "loginid": "13800000001", "password": "wrong",
             "passwordSetAt": int(time.time()) - 25 * 86400,
             "session": "good", "expireAt": int(time.time()) + 2.9 * 86400},
        ])
        pool = AccountPool(self.cfg)

        class FakeClient:
            def fill(self, account):
                raise AccountError("/login/account/byPwd failed: 密码错误",
                                   code="220204")

        pool.client = FakeClient()
        acc = pool.ensure_session(pool.get("a"))    # implicit, must not raise
        self.assertEqual(acc.session, "good")

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

    def test_auto_repair_rotates_the_password_once_a_day(self):
        """A rejected password is replaced while the session still works.

        Repair rotates the upstream password, which logs the account out on
        every other device — so a second attempt inside the window must be
        refused even when the first one failed.
        """
        from tests.support import write_accounts

        write_accounts(self.dir / "accounts.json", [
            {"name": "a", "loginid": "13800000001", "password": "wrong",
             "passwordVerifiedAt": int(time.time()),
             "session": "good", "expireAt": int(time.time()) + 9 * 86400},
        ])
        pool = AccountPool(self.cfg)
        rotates = []

        class FakeClient:
            def fill(self, account):
                from loomy2api.account import AccountError
                raise AccountError("密码错误", code="220204")

            def set_password(self, session, new_password, identity=None):
                rotates.append(new_password)

            def login_by_password(self, loginid, password, identity=None):
                return {"session": "fresh", "userid": "u"}

        pool.client = FakeClient()
        acc = pool.get("a")

        self.assertTrue(pool._repair_password(acc, reason="test"))
        self.assertEqual(len(rotates), 1)
        self.assertTrue(acc.password_repaired_at > 0)
        self.assertNotEqual(acc.password, "wrong")

        # Second attempt inside the window is refused, no second rotation.
        self.assertFalse(pool._repair_password(acc, reason="test"))
        self.assertEqual(len(rotates), 1)

    def test_startup_audit_repairs_a_rejected_password(self):
        """The keeper only renews expiring sessions, so a password that the
        upstream invalidated sat dead until the session finally expired.

        The startup audit proves every stored password and rotates the ones
        the upstream rejects, while the session is still able to do it.
        """
        from tests.support import write_accounts

        write_accounts(self.dir / "accounts.json", [
            # 10 days left: far outside the renewal window, so only the audit
            # will look at it.
            {"name": "stale", "loginid": "13800000001", "password": "dead",
             "session": "s1", "expireAt": int(time.time()) + 10 * 86400},
            # Already proven: the audit must leave it alone.
            {"name": "ok", "loginid": "13800000002", "password": "good",
             "passwordVerifiedAt": int(time.time()),
             "session": "s2", "expireAt": int(time.time()) + 10 * 86400},
        ])
        pool = AccountPool(self.cfg)
        rotated = []

        class FakeClient:
            def login_by_password(self, loginid, password, identity=None):
                from loomy2api.account import AccountError
                if password == "dead":
                    raise AccountError("密码错误", code="220204")
                return {"session": "fresh", "userid": "u"}

            def set_password(self, session, new_password, identity=None):
                rotated.append(session)

        pool.client = FakeClient()
        pool._audit_passwords_once()

        stale, ok = pool.get("stale"), pool.get("ok")
        self.assertEqual(rotated, ["s1"])              # only the stale one
        self.assertTrue(stale.password_verified_at > 0)   # proven after rotation
        self.assertNotEqual(stale.password, "dead")       # a new one was generated
        self.assertEqual(ok.password, "good")             # untouched
        self.assertTrue(ok.password_verified_at > 0)

    def test_startup_audit_can_be_disabled(self):
        from tests.support import write_accounts

        write_accounts(self.dir / "accounts.json", [
            {"name": "stale", "loginid": "13800000001", "password": "dead",
             "session": "s1", "expireAt": int(time.time()) + 10 * 86400},
        ])
        self.cfg["password_audit_on_startup"] = False
        pool = AccountPool(self.cfg)

        class FakeClient:
            def login_by_password(self, *a, **k):
                raise AssertionError("audit must not run")

        pool.client = FakeClient()
        pool._audit_passwords_once()                   # no exception

    def test_repair_stamp_is_persisted(self):
        from tests.support import write_accounts

        write_accounts(self.dir / "accounts.json", [
            {"name": "a", "loginid": "13800000001", "password": "wrong",
             "session": "good", "expireAt": int(time.time()) + 9 * 86400},
        ])
        pool = AccountPool(self.cfg)
        pool.get("a").password_repaired_at = int(time.time())
        pool.save(force=True)
        again = AccountPool(self.cfg)
        self.assertTrue(again.get("a").password_repaired_at > 0)

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
        # and no "已设置密码" stamp either: leaving it made the panel promise
        # automatic renewal for a credential the upstream already rejected
        self.assertEqual(acc.password_set_at, 0)

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

    def test_auto_set_password_can_be_disabled(self):
        """Mobile turns it off: a password-less account is left alone."""
        from tests.support import write_accounts

        write_accounts(self.dir / "accounts.json", [
            {"name": "a", "loginid": "13800000001", "session": "s",
             "expireAt": int(time.time()) + 12 * 3600},
        ])
        self.cfg["auto_set_password"] = False
        pool = AccountPool(self.cfg)

        class FakeClient:
            passwords = []

            def set_password(self, session, new_password, identity=None):
                self.passwords.append((session, new_password))

        client = FakeClient()
        pool.client = client
        pool.tick()
        self.assertEqual(client.passwords, [])
        self.assertFalse(pool.get("a").has_password)
        # the still-valid session is kept rather than failing the account
        self.assertTrue(pool.get("a").session_valid)

    def test_rejected_session_schedules_background_relogin(self):
        """A 401 puts the account in cooldown and repairs it out of band."""
        import time as _time
        from tests.support import write_accounts

        write_accounts(self.dir / "accounts.json", [
            {"name": "a", "loginid": "13800000001", "password": "pw",
             "passwordVerifiedAt": int(_time.time()),
             "session": "dead", "expireAt": int(_time.time()) + 86400},
        ])
        pool = AccountPool(self.cfg)
        acc = pool.get("a")

        class FakeClient:
            def fill(self, account):
                account.session = "fresh"
                account.expire_at = int(_time.time()) + 14 * 86400
                return account

        pool.client = FakeClient()
        pool.gateway = type("G", (), {
            "quota": lambda self, s: {"balance": 1, "daily_balance": 1,
                                      "available": 2}})()
        pool.report_failure(acc, 401, "登录已失效")
        pool.wait_recovery(5)
        self.assertFalse(acc.in_cooldown, "background recovery must clear it")
        self.assertEqual(acc.session, "fresh")

    def test_transient_401_is_retried_not_relogged_in(self):
        """A single fake 401 must be absorbed by the retry, not drop the session.

        The upstream emits transient "登录已失效" under burst load while the
        session is fine; reacting to one kicks off a re-login that invalidates
        the session a parallel request is still using (the "顶号" storm).
        """
        from tests.support import write_accounts
        from loomy2api.upstream import UpstreamError

        write_accounts(self.dir / "accounts.json", [
            {"name": "a", "loginid": "13800000001", "password": "pw",
             "passwordVerifiedAt": int(time.time()),
             "session": "s", "expireAt": int(time.time()) + 11 * 86400},
        ])
        pool = AccountPool(self.cfg)
        acc = pool.get("a")
        calls = []

        class FakeGateway:
            def quota(self, session):
                calls.append(session)
                if len(calls) == 1:                  # first read blips
                    raise UpstreamError("登录已失效，请重新登录", 401)
                return {"balance": 100, "daily_balance": 50, "available": 150}

        pool.gateway = FakeGateway()
        pool.client = type("C", (), {
            "fill": lambda self, a: self.fail("re-login must not run"),
        })()
        pool.refresh_quota(acc)
        self.assertEqual(calls, ["s", "s"])          # retried the same session
        self.assertEqual(acc.available, 150)
        self.assertEqual(acc.quota_auth_faults, 0)
        self.assertTrue(acc.session_valid)

    def test_business_error_200_is_retried(self):
        """A "系统内部错误" arrives as HTTP-200 + a business code (status 200).

        It must be treated as a transient read fault, not a verdict — missing
        this branch left the retry to cover only 401s and let the error through.
        """
        from tests.support import write_accounts
        from loomy2api.upstream import UpstreamError

        write_accounts(self.dir / "accounts.json", [
            {"name": "a", "session": "s",
             "expireAt": int(time.time()) + 14 * 86400},
        ])
        pool = AccountPool(self.cfg)
        acc = pool.get("a")
        calls = []

        class FakeGateway:
            def quota(self, session):
                calls.append(session)
                if len(calls) == 1:
                    # exactly how upstream._check_payload raises for
                    # "HTTP 200: 系统内部错误"
                    raise UpstreamError("/points/records HTTP 200: 系统内部错误", 200)
                return {"balance": 7, "daily_balance": 3, "available": 10}

        pool.gateway = FakeGateway()
        pool.refresh_quota(acc)
        self.assertEqual(calls, ["s", "s"])           # retried
        self.assertEqual(acc.available, 10)
        self.assertEqual(acc.last_error, "")

    def test_consecutive_401s_relogin_on_rejected_session(self):
        """A run of 401s is a dead session and must re-login, not report "unknown"."""
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
        pool.refresh_quota(acc)                      # 2 rejections → faults=1, no relogin
        self.assertTrue(acc.session_valid)
        self.assertEqual(acc.quota_auth_faults, 1)
        pool.refresh_quota(acc)                      # faults=2 → relogin fires
        self.assertIn("fresh", calls)
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
        pool.refresh_quota(acc)                      # retries, no relogin yet
        self.assertEqual(len(calls), 3)              # 1 + auth_retry_attempts
        self.assertEqual(acc.quota_auth_faults, 1)
        pool.refresh_quota(acc)                      # now it tries to re-login
        self.assertEqual(len(calls), 6)              # one more retry batch
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

    def test_tick_keeps_the_session_record_when_it_cannot_relogin(self):
        """Without a password there is no way back, so do not erase the session.

        Clearing it achieved nothing — routing already treats a rejected
        session as unusable — and it destroyed the only evidence that the
        account ever worked, leaving an operator with no idea when it died.
        """
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
        self.assertTrue(pool.get("a").session_valid)   # one blip, kept
        pool.tick()
        acc = pool.get("a")
        self.assertEqual(acc.session, "s")             # record preserved
        self.assertIn("无法自动重登", acc.last_error)
        self.assertNotEqual(acc.last_error, "")


class TestModelEntitlements(unittest.TestCase):
    """The upstream scopes /models per account and silently substitutes a
    default for ids an account lacks.  Routing must therefore only pick
    accounts whose own catalogue lists the requested model."""

    FUTURE = int(time.time()) + 14 * 86400

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cfg = make_config(self.dir, "http://127.0.0.1:1/api/v1")

    def tearDown(self):
        self.tmp.cleanup()

    def _pool(self, accounts):
        (self.dir / "accounts.json").write_text(
            json.dumps({"accounts": accounts}), encoding="utf-8")
        return AccountPool(self.cfg)

    def test_unknown_catalogue_allows_any_model(self):
        """An account whose list was never fetched must stay routable."""
        pool = self._pool([{"name": "a", "session": "s", "expireAt": self.FUTURE}])
        acc = pool.get("a")
        self.assertIsNone(acc.models)
        self.assertTrue(pool.serves_model(acc, "GLM-5.3-Flash"))

    def test_limited_account_is_skipped_for_every_model_by_default(self):
        """A downgraded account is kept out of scheduling entirely.

        It could serve the free model correctly, but letting it take work that
        a full-entitlement account can do better is exactly what the
        ``exclude_downgraded`` default avoids.
        """
        pool = self._pool([
            {"name": "limited", "session": "s1", "expireAt": self.FUTURE},
            {"name": "full", "session": "s2", "expireAt": self.FUTURE},
        ])
        pool.get("limited").models = ("spark-x2.6",)
        pool.get("limited").available = 900              # richest, would win
        pool.get("full").models = ("spark-x2.6", "GLM-5.3-Flash")
        pool.get("full").available = 10
        self.assertEqual(pool.acquire(model="GLM-5.3-Flash").name, "full")
        self.assertEqual(pool.acquire(model="spark-x2.6").name, "full")

    def test_limited_account_serves_the_free_model_when_the_switch_is_off(self):
        self.cfg["exclude_downgraded"] = False
        pool = self._pool([
            {"name": "limited", "session": "s1", "expireAt": self.FUTURE},
            {"name": "full", "session": "s2", "expireAt": self.FUTURE},
        ])
        pool.get("limited").models = ("spark-x2.6",)
        pool.get("limited").available = 900
        pool.get("full").models = ("spark-x2.6", "GLM-5.3-Flash")
        pool.get("full").available = 10
        self.assertEqual(pool.acquire(model="GLM-5.3-Flash").name, "full")
        self.assertEqual(pool.acquire(model="spark-x2.6").name, "limited")

    def test_no_entitled_account_fails_loudly(self):
        """Never fall back to an account that would answer with another model:
        HTTP 200 + a substituted id is worse than an explicit error.

        The id must be one the pool knows about — otherwise it is passed
        through verbatim (see ``test_unknown_model_passes_through``).
        """
        pool = self._pool([
            {"name": "limited", "session": "s1", "expireAt": self.FUTURE},
            {"name": "rich", "session": "s2", "expireAt": self.FUTURE},
        ])
        pool.get("limited").models = ("spark-x2.6",)
        pool.get("rich").models = ("spark-x2.6", "Kimi-k2.6")
        pool.get("rich").enabled = False                 # entitlement exists, unusable
        with self.assertRaises(PoolError) as ctx:
            pool.acquire(model="Kimi-k2.6")
        self.assertIn("Kimi-k2.6", str(ctx.exception))

    def test_withdrawn_model_is_not_treated_as_unknown(self):
        """Once every account loses an id, it must still be rejected.

        ``/models`` is the only source of truth, so a withdrawn entitlement
        makes the id vanish from every catalogue.  Reading that as "the caller
        invented this name" would pass it upstream, where the upstream answers
        200 with a substituted model — the exact silent downgrade this whole
        mechanism exists to prevent.  The persisted history remembers it.
        """
        pool = self._pool([{"name": "a", "session": "s", "expireAt": self.FUTURE}])
        pool._seen_models.update({"spark-x2.6", "GLM-5.3-Flash"})
        pool.get("a").models = ("spark-x2.6",)         # entitlement withdrawn
        with self.assertRaises(PoolError) as ctx:
            pool.acquire(model="GLM-5.3-Flash")
        self.assertIn("GLM-5.3-Flash", str(ctx.exception))
        # An id never seen anywhere is still forwarded verbatim.
        self.assertEqual(pool.acquire(model="gpt-4o-mini").name, "a")

    def test_known_models_survive_a_reload(self):
        pool = self._pool([{"name": "a", "session": "s", "expireAt": self.FUTURE}])
        pool._seen_models.update({"GLM-5.3-Flash"})
        pool.save(force=True)
        again = AccountPool(self.cfg)
        self.assertEqual(again._seen_models, {"GLM-5.3-Flash"})

    def test_unknown_model_passes_through(self):
        """An id no account has ever listed is forwarded verbatim.

        It may be a typo, a model added upstream since the last probe, or a
        caller-specific id — refusing it here turned pass-through requests into
        502s.  The upstream decides, and ``note_model_substitution`` reports
        whatever it actually serves.
        """
        pool = self._pool([{"name": "a", "session": "s", "expireAt": self.FUTURE}])
        pool.get("a").models = ("spark-x2.6",)
        self.assertEqual(pool.acquire(model="gpt-4o-mini").name, "a")

    def test_empty_model_does_not_filter(self):
        pool = self._pool([{"name": "a", "session": "s", "expireAt": self.FUTURE}])
        pool.get("a").models = ("spark-x2.6",)
        self.assertEqual(pool.acquire().name, "a")

    def test_probe_records_ids_and_unions_catalogues(self):
        pool = self._pool([
            {"name": "limited", "session": "s1", "expireAt": self.FUTURE},
            {"name": "full", "session": "s2", "expireAt": self.FUTURE},
        ])

        class FakeGateway:
            def models(self, session):
                if session == "s1":
                    return {"data": [{"id": "spark-x2.6", "name": "free"}]}
                return {"data": [{"id": "spark-x2.6"}, {"id": "GLM-5.3-Flash"}]}

        pool.gateway = FakeGateway()
        result = pool.probe_models()
        self.assertEqual(sorted(result["models"]), ["GLM-5.3-Flash", "spark-x2.6"])
        self.assertEqual(result["probed"], 2)
        self.assertEqual(pool.get("limited").models, ("spark-x2.6",))
        self.assertEqual(pool.model_stats(), {"spark-x2.6": 2, "GLM-5.3-Flash": 1})

    def test_probe_treats_200_login_expired_as_a_rejected_session(self):
        """The upstream answers /models with HTTP 200 + ``code 100002``.

        Reading that as a *successful empty catalogue* recorded ``models=()``,
        dropped the account from every routed model and never re-logged it in —
        the exact opposite of what a dead session needs.  It must raise, keep
        the previous list and start a recovery.
        """
        from loomy2api.upstream import ModelGateway

        pool = self._pool([{"name": "a", "loginid": "13800000001", "password": "p",
                            "session": "s", "expireAt": self.FUTURE}])
        pool.get("a").models = ("GLM-5.3-Flash",)

        class Expiring:
            def models(self, session):
                # exactly the shape the fake upstream now emits
                from loomy2api.upstream import _check_payload
                payload = {"code": "100002", "desc": "登录已失效，请重新登录",
                           "data": {}}
                data = json.dumps(payload).encode()
                _check_payload(200, payload, data, "/models")
                return payload

        pool.gateway = Expiring()
        result = pool.probe_models()
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(pool.get("a").models, ("GLM-5.3-Flash",))  # kept
        self.assertEqual(pool.get("a").session, "s")                # kept too

    def test_probe_keeps_previous_list_on_failure(self):
        """A network blip is not evidence that an entitlement was withdrawn."""
        pool = self._pool([{"name": "a", "session": "s", "expireAt": self.FUTURE}])
        pool.get("a").models = ("GLM-5.3-Flash",)
        recovered = []
        pool.schedule_recovery = lambda acc: recovered.append(acc.name)

        class Broken:
            def models(self, session):
                raise RuntimeError("boom")

        pool.gateway = Broken()
        result = pool.probe_models()
        self.assertEqual(result["failed"], ["a: boom"])
        self.assertEqual(pool.get("a").models, ("GLM-5.3-Flash",))
        # A timeout says nothing about the session: re-logging in would be a
        # 顶号 against whoever else is using the account.
        self.assertEqual(recovered, [])

    def test_probe_never_relogs_or_drops_the_session(self):
        """A rejected catalogue read must not touch the credentials.

        ``report_failure`` treats a 401 as proof the session is dead: it wipes
        it and starts a login.  Applying that from a *probe* destroyed accounts
        that were merely unreadable at that moment, and burned a 顶号 login on
        a read that has no bearing on whether real requests still work.  The
        probe cools the account down and leaves the session alone.
        """
        from loomy2api.upstream import UpstreamError

        pool = self._pool([{"name": "a", "loginid": "13800000001", "password": "p",
                            "session": "s", "expireAt": self.FUTURE}])
        recovered = []
        pool.schedule_recovery = lambda acc: recovered.append(acc.name)

        class Expired:
            def models(self, session):
                raise UpstreamError("/models HTTP 200: 登录已失效，请重新登录", 401)

        pool.gateway = Expired()
        pool.probe_models()
        acc = pool.get("a")
        self.assertEqual(recovered, [])
        self.assertEqual(acc.session, "s")           # untouched
        self.assertTrue(acc.in_cooldown)             # benched, not destroyed

    def test_empty_catalogue_means_serve_nothing(self):
        """A well-formed empty list is data, not a failure: the account may use
        no model at all, so routing must skip it and the probe must not retry
        it as if the call had failed."""
        pool = self._pool([{"name": "a", "session": "s", "expireAt": self.FUTURE}])
        pool.schedule_recovery = lambda acc: self.fail("must not re-login")

        class Empty:
            def models(self, session):
                return {"object": "list", "data": []}

        pool.gateway = Empty()
        result = pool.probe_models()
        self.assertEqual(result["failed"], [])
        self.assertEqual(pool.get("a").models, ())
        with self.assertRaises(PoolError):
            pool.acquire(model="spark-x2.6")

    def test_models_survive_a_save_and_reload(self):
        pool = self._pool([{"name": "a", "session": "s", "expireAt": self.FUTURE}])
        pool.get("a").models = ("spark-x2.6", "GLM-5.3-Flash")
        pool.save(force=True)
        again = AccountPool(self.cfg)
        self.assertEqual(again.get("a").models, ("spark-x2.6", "GLM-5.3-Flash"))


class TestDowngradeExclusion(unittest.TestCase):
    """Downgraded accounts are excluded from scheduling by default."""

    FUTURE = int(time.time()) + 14 * 86400

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cfg = make_config(self.dir, "http://127.0.0.1:1/api/v1")

    def tearDown(self):
        self.tmp.cleanup()

    def _pool(self, accounts, **cfg):
        (self.dir / "accounts.json").write_text(
            json.dumps({"accounts": accounts}), encoding="utf-8")
        self.cfg.update(cfg)
        return AccountPool(self.cfg)

    def _two(self):
        return [
            {"name": "full", "session": "s1", "expireAt": self.FUTURE},
            {"name": "cut", "session": "s2", "expireAt": self.FUTURE},
        ]

    def test_downgraded_account_is_skipped_by_default(self):
        pool = self._pool(self._two())
        pool.get("full").models = ("spark-x2.6", "GLM-5.3-Flash")
        pool.get("cut").models = ("spark-x2.6-low", "spark-x2.6-4b")
        pool.get("cut").available = 900          # richest, would win
        pool.get("full").available = 10
        self.assertTrue(pool.exclude_downgraded())
        self.assertTrue(pool.is_downgraded(pool.get("cut")))
        self.assertFalse(pool.is_downgraded(pool.get("full")))
        names = [a.name for a in pool.usable()]
        self.assertEqual(names, ["full"])

    def test_cooled_down_account_still_gets_a_self_heal_attempt(self):
        """A model entitlement must not short-circuit the re-login fallback.

        Raising "no account can serve this model" before trying the enabled
        accounts turned a 300-second cooldown into a hard 400 for small pools,
        even though a re-login would have brought the account straight back.
        """
        pool = self._pool([{"name": "only", "session": "s1",
                            "expireAt": self.FUTURE}])
        pool.get("only").models = ("spark-x2.6", "GLM-5.3-Flash")
        pool.get("only").cooldown(300, "冷却中")      # not usable right now
        pool._seen_models.update({"GLM-5.3-Flash"})
        self.assertEqual(pool.acquire(model="GLM-5.3-Flash").name, "only")

    def test_no_entitled_account_at_all_still_fails_clearly(self):
        """Once the self-heal set is empty too, the model error is correct."""
        pool = self._pool([{"name": "a", "session": "s1",
                            "expireAt": self.FUTURE}])
        pool.get("a").models = ("spark-x2.6",)
        pool._seen_models.update({"GLM-5.3-Flash"})
        with self.assertRaises(PoolError) as ctx:
            pool.acquire(model="GLM-5.3-Flash")
        self.assertIn("GLM-5.3-Flash", str(ctx.exception))

    def test_preference_falls_back_instead_of_failing_the_request(self):
        """Entitlements are per account: "everyone lacks some paid model" is
        the normal state, not an outage.

        Filtering on that and then finding nobody must fall back to the
        unfiltered set — ``serves_model`` has already guaranteed each candidate
        can answer the requested id, so failing here would break working
        traffic for a scheduling preference.
        """
        pool = self._pool([
            {"name": "a", "session": "s1", "expireAt": self.FUTURE},
            {"name": "b", "session": "s2", "expireAt": self.FUTURE},
        ])
        # Each holds a *different* paid model, so each looks "downgraded".
        pool.get("a").models = ("GLM-5.3-Flash",)
        pool.get("b").models = ("DeepSeek-V4.1-Flash",)
        self.assertTrue(pool.is_downgraded(pool.get("a")))
        self.assertTrue(pool.is_downgraded(pool.get("b")))
        self.assertEqual(sorted(x.name for x in pool.usable()), ["a", "b"])

    def test_switch_off_lets_the_free_tier_back_in(self):
        pool = self._pool(self._two(), exclude_downgraded=False)
        pool.get("full").models = ("spark-x2.6", "GLM-5.3-Flash")
        pool.get("cut").models = ("spark-x2.6-low",)
        self.assertFalse(pool.exclude_downgraded())
        self.assertEqual(sorted(a.name for a in pool.usable()), ["cut", "full"])

    def test_free_only_pool_has_nobody_downgraded(self):
        """With no paid model anywhere, the free tier is all there is."""
        pool = self._pool([
            {"name": "a", "session": "s1", "expireAt": self.FUTURE},
            {"name": "b", "session": "s2", "expireAt": self.FUTURE},
        ])
        pool.get("a").models = ("spark-x2.6",)
        pool.get("b").models = ("spark-x2.6-low",)
        self.assertFalse(pool.is_downgraded(pool.get("a")))
        self.assertFalse(pool.is_downgraded(pool.get("b")))
        self.assertEqual(sorted(a.name for a in pool.usable()), ["a", "b"])

    def test_unknown_catalogue_is_never_downgraded(self):
        pool = self._pool(self._two())
        pool.get("full").models = ("GLM-5.3-Flash",)
        self.assertIsNone(pool.get("cut").models)
        self.assertFalse(pool.is_downgraded(pool.get("cut")))
        self.assertEqual(sorted(a.name for a in pool.usable()), ["cut", "full"])


class TestModelEventLog(unittest.TestCase):
    """The 7-day entitlement history."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cfg = make_config(self.dir, "http://127.0.0.1:1/api/v1")

    def tearDown(self):
        self.tmp.cleanup()

    def _pool(self, accounts):
        (self.dir / "accounts.json").write_text(
            json.dumps({"accounts": accounts}), encoding="utf-8")
        return AccountPool(self.cfg)

    def test_probe_records_downgrade_and_recovery(self):
        pool = self._pool([{"name": "a", "session": "s",
                            "expireAt": int(time.time()) + 14 * 86400}])
        pool.get("a").models = ("spark-x2.6", "GLM-5.3-Flash")

        class Cut:
            def models(self, session):
                return {"data": [{"id": "spark-x2.6"}]}

        class Restored:
            def models(self, session):
                return {"data": [{"id": "spark-x2.6"}, {"id": "GLM-5.3-Flash"}]}

        pool.gateway = Cut()
        pool.probe_models()
        pool.gateway = Restored()
        pool.probe_models()

        events = pool.model_events.events(days=7)
        # Both transitions land in the same second; order by content, not time.
        by_kind = {e["event"]: e for e in events}
        self.assertEqual(sorted(by_kind), ["downgrade", "recover"])
        self.assertEqual(by_kind["downgrade"]["lost"], ["GLM-5.3-Flash"])
        self.assertEqual(by_kind["recover"]["gained"], ["GLM-5.3-Flash"])
        summary = pool.model_events.summary()
        self.assertEqual(summary["a"]["downgrades"], 1)
        self.assertEqual(summary["a"]["recovers"], 1)
        self.assertEqual(summary["a"]["event"], "recover")

    def test_free_model_rename_is_not_an_event(self):
        """The upstream splits/renames its free ids constantly."""
        pool = self._pool([{"name": "a", "session": "s",
                            "expireAt": int(time.time()) + 14 * 86400}])
        pool.get("a").models = ("spark-x2.6",)

        class Split:
            def models(self, session):
                return {"data": [{"id": "spark-x2.6-low"},
                                 {"id": "spark-x2.6-4b"}]}

        pool.gateway = Split()
        pool.probe_models()
        self.assertEqual(pool.model_events.events(days=7), [])

    def test_periodic_probe_is_rate_limited(self):
        """Entitlements change without traffic, so the keeper re-probes."""
        pool = self._pool([{"name": "a", "session": "s",
                            "expireAt": int(time.time()) + 14 * 86400}])
        calls = []

        class Counting:
            def models(self, session):
                calls.append(session)
                return {"data": [{"id": "spark-x2.6"}]}

        pool.gateway = Counting()
        pool.cfg["model_probe_minutes"] = 60
        self.assertTrue(pool.maybe_probe_models())
        self.assertEqual(len(calls), 1)
        self.assertFalse(pool.maybe_probe_models())   # inside the window
        self.assertEqual(len(calls), 1)
        pool._last_model_probe = time.time() - 61 * 60
        self.assertTrue(pool.maybe_probe_models())
        self.assertEqual(len(calls), 2)

    def test_periodic_probe_can_be_disabled(self):
        pool = self._pool([{"name": "a", "session": "s",
                            "expireAt": int(time.time()) + 14 * 86400}])
        pool.cfg["model_probe_minutes"] = 0
        self.assertFalse(pool.maybe_probe_models())

    def test_events_expire_after_the_retention_window(self):
        from loomy2api.model_events import ModelEventLog

        path = self.dir / "model_events.jsonl"
        log = ModelEventLog(path, retention=7 * 86400)
        stale = int(time.time() - 8 * 86400)
        fresh = int(time.time())
        log.append({"at": stale, "account": "a", "event": "downgrade",
                    "lost": ["GLM-5.3-Flash"], "gained": [],
                    "from": ["GLM-5.3-Flash"], "to": []})
        log.append({"at": fresh, "account": "b", "event": "recover",
                    "lost": [], "gained": ["GLM-5.3-Flash"],
                    "from": [], "to": ["GLM-5.3-Flash"]})
        log._last_prune = 0.0            # force a prune pass
        log.append({"at": fresh + 1, "account": "c", "event": "downgrade",
                    "lost": [], "gained": [], "from": [], "to": []})
        # Assert on the file itself: ``events(days=7)`` filters by time anyway,
        # so it cannot prove the 8-day-old row was actually rewritten away.
        on_disk = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual([r["account"] for r in on_disk], ["b", "c"])


if __name__ == "__main__":
    unittest.main()
