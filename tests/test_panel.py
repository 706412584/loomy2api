"""Web control panel tests — page, state, and every write endpoint."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from loomy2api.server import Gateway, Handler, Server
from tests.support import (FakeAccountClient, FakeUpstream, make_config,
                           write_accounts)


class PanelHarness:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.upstream = FakeUpstream()
        base = self.upstream.start()
        write_accounts(self.dir / "accounts.json", [
            {"name": "a", "loginid": "13800000001", "password": "pw",
             "passwordVerifiedAt": int(time.time()),
             "session": "s1", "userid": "u1",
             "expireAt": int(time.time()) + 14 * 86400,
             "identity": {"devid": "web-aaaaaaaaaaaaaaaa", "ua": "UA",
                          "modelid": "Web", "version": "1.0.0",
                          "campus_device_id": "loomy-campus-fixed",
                          "created_at": 1700000000}},
        ])
        self.cfg = make_config(self.dir, base)
        self.gateway = Gateway(self.cfg)
        self.gateway.pool.client = FakeAccountClient()
        self.gateway.pool.bootstrap()
        self.gateway.refresh_models()
        handler = type("Bound", (Handler,), {"gateway": self.gateway})
        self.httpd = Server(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.upstream.stop()
        self.tmp.cleanup()

    # -- helpers --------------------------------------------------------

    def get(self, path, headers=None):
        request = urllib.request.Request(self.base + path, headers=headers or {})
        response = urllib.request.urlopen(request, timeout=30)
        return response.status, response

    def get_json(self, path, headers=None):
        status, response = self.get(path, headers)
        return status, json.loads(response.read())

    def post(self, path, payload, headers=None):
        request = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST")
        response = urllib.request.urlopen(request, timeout=60)
        return response.status, json.loads(response.read())


class TestPanelPage(PanelHarness, unittest.TestCase):
    def test_page_is_served(self):
        status, response = self.get("/panel")
        body = response.read().decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("loomy2api", body)
        self.assertIn("/api/panel/state", body)
        self.assertIn("text/html", response.headers["Content-Type"])

    def test_root_serves_the_page(self):
        status, response = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("loomy2api", response.read().decode("utf-8"))

    def test_health_advertises_the_panel(self):
        _s, payload = self.get_json("/health")
        self.assertTrue(payload["panel"].endswith("/panel"))


class TestPanelState(PanelHarness, unittest.TestCase):
    def test_state_lists_accounts_with_quota(self):
        _s, payload = self.get_json("/api/panel/state")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["totals"]["accounts"], 1)
        self.assertEqual(payload["totals"]["available"], 150)
        account = payload["accounts"][0]
        self.assertEqual(account["name"], "a")
        self.assertEqual(account["available"], 150)
        self.assertEqual(account["balance"], 100)
        self.assertEqual(account["daily_balance"], 50)
        self.assertEqual(account["loginid_masked"], "138****0001")
        self.assertEqual(account["mode"], "password")
        self.assertNotIn('"password":', json.dumps(payload))

    def test_state_reports_identity(self):
        _s, payload = self.get_json("/api/panel/state")
        identity = payload["accounts"][0]["identity"]
        self.assertTrue(identity["bound"])
        self.assertEqual(identity["devid"], "web-aaaaaaaaaaaaaaaa")

    def test_state_reports_config(self):
        _s, payload = self.get_json("/api/panel/state")
        cfg = payload["config"]
        self.assertEqual(cfg["strategy"], "balance")
        self.assertEqual(cfg["identity_mode"], "per_account")
        self.assertFalse(cfg["auth_required"])

    def test_repeated_state_does_not_hit_upstream_again(self):
        self.get_json("/api/panel/state")
        before = len([c for c in self.upstream.calls if "points" in c["path"]])
        self.get_json("/api/panel/state")
        after = len([c for c in self.upstream.calls if "points" in c["path"]])
        self.assertEqual(before, after)          # quota cache is reused

    def test_refresh_flag_forces_upstream_call(self):
        self.get_json("/api/panel/state")
        before = len([c for c in self.upstream.calls if "points" in c["path"]])
        self.get_json("/api/panel/state?refresh=1")
        after = len([c for c in self.upstream.calls if "points" in c["path"]])
        self.assertEqual(after, before + 1)


class TestPanelWrites(PanelHarness, unittest.TestCase):
    def test_add_account_with_password_logs_in_and_binds_identity(self):
        status, payload = self.post("/api/panel/accounts", {
            "name": "new", "loginid": "13900000002", "password": "pw", "login": True})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["logged_in"])
        self.assertTrue(payload["identity"]["devid"].startswith("web-"))
        saved = json.loads((self.dir / "accounts.json").read_text(encoding="utf-8"))
        entry = [a for a in saved["accounts"] if a["name"] == "new"][0]
        self.assertEqual(entry["identity"]["devid"], payload["identity"]["devid"])
        self.assertIn("new", self.gateway.pool.client.fills)

    def test_add_account_with_session_only(self):
        _s, payload = self.post("/api/panel/accounts", {
            "name": "shared", "session": "borrowed-session"})
        self.assertTrue(payload["ok"])
        self.assertNotIn("logged_in", payload)
        acc = self.gateway.pool.get("shared")
        self.assertEqual(acc.session, "borrowed-session")
        self.assertTrue(acc.identity)          # identity bound at creation

    def test_add_without_credentials_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/panel/accounts", {"name": "x"})
        self.assertEqual(ctx.exception.code, 400)

    def test_duplicate_name_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/panel/accounts", {"name": "a", "session": "s"})
        self.assertEqual(ctx.exception.code, 400)

    def test_renew_refreshes_session_and_clears_cooldown(self):
        self.gateway.pool.get("a").cooldown(600, "test")
        _s, payload = self.post("/api/panel/accounts/renew", {"name": "a"})
        self.assertTrue(payload["ok"])
        acc = self.gateway.pool.get("a")
        self.assertFalse(acc.in_cooldown)
        self.assertAlmostEqual(acc.days_left, 14, delta=0.1)
        self.assertEqual(self.gateway.pool.client.fills, ["a"])

    def test_renew_unknown_account_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/panel/accounts/renew", {"name": "nope"})
        self.assertEqual(ctx.exception.code, 400)

    def test_daily_only_can_be_toggled_from_the_panel(self):
        _s, payload = self.post("/api/panel/config", {"daily_only": True})
        self.assertIn("daily_only", payload["changed"])
        self.assertTrue(payload["state"]["config"]["daily_only"])
        self.assertTrue(self.gateway.pool.daily_only())
        _s, payload = self.post("/api/panel/config", {"daily_only": False})
        self.assertFalse(payload["state"]["config"]["daily_only"])

    def test_check_pool_repairs_expired_session(self):
        """A session the pool cannot schedule for renewal gets re-logged in."""
        self.gateway.pool.add_account("b", loginid="13900000009", password="pw")
        acc = self.gateway.pool.get("b")
        acc.session, acc.expire_at = "stale", 0        # imported without expiry
        _s, payload = self.post("/api/panel/accounts/check", {})
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["repaired"], 1)
        self.assertEqual(payload["failed"], 0)
        row = next(r for r in payload["results"] if r["name"] == "b")
        self.assertTrue(row["ok"])
        self.assertEqual(row["action"], "relogin")
        self.assertAlmostEqual(acc.days_left, 14, delta=0.1)
        self.assertIn("b", self.gateway.pool.client.fills)

    def test_check_pool_leaves_healthy_accounts_alone(self):
        _s, payload = self.post("/api/panel/accounts/check", {})
        row = payload["results"][0]
        self.assertEqual(row["action"], "quota")
        self.assertTrue(row["ok"])
        self.assertEqual(self.gateway.pool.client.fills, [])

    def test_check_pool_force_relogins_everyone(self):
        _s, payload = self.post("/api/panel/accounts/check", {"force": True})
        row = payload["results"][0]
        self.assertEqual(row["action"], "relogin")
        self.assertEqual(self.gateway.pool.client.fills, ["a"])

    def test_check_pool_reports_failure_without_aborting(self):
        self.gateway.pool.add_account("b", loginid="13900000009", password="pw")
        self.gateway.pool.get("b").session, self.gateway.pool.get("b").expire_at = "", 0

        original = self.gateway.pool.client.fill

        def fill(account):
            if account.name == "b":
                from loomy2api.account import AccountError
                raise AccountError("密码错误", code="220204")
            return original(account)

        self.gateway.pool.client.fill = fill
        _s, payload = self.post("/api/panel/accounts/check", {})
        self.assertEqual(payload["total"], 2)          # batch finished
        self.assertEqual(payload["healthy"], 1)
        self.assertEqual(payload["failed"], 1)
        bad = next(r for r in payload["results"] if r["name"] == "b")
        self.assertFalse(bad["ok"])
        self.assertIn("密码错误", bad["error"])

    def test_rebind_identity_changes_devid(self):
        before = self.gateway.pool.get("a").identity["devid"]
        _s, payload = self.post("/api/panel/accounts/identity",
                                {"name": "a", "regenerate": True})
        after = payload["identity"]["devid"]
        self.assertNotEqual(before, after)
        self.assertTrue(after.startswith("web-"))
        self.assertEqual(self.gateway.pool.client.rebinds, ["a"])

    def test_identity_inspect_does_not_regenerate(self):
        before = self.gateway.pool.get("a").identity["devid"]
        _s, payload = self.post("/api/panel/accounts/identity", {"name": "a"})
        self.assertEqual(payload["identity"]["devid"], before)

    def test_toggle_enabled(self):
        _s, payload = self.post("/api/panel/accounts/update",
                                {"name": "a", "enabled": False})
        self.assertTrue(payload["ok"])
        self.assertFalse(self.gateway.pool.get("a").enabled)
        self.assertEqual(payload["state"]["totals"]["usable"], 0)

    def test_update_password_forces_new_login(self):
        _s, payload = self.post("/api/panel/accounts/update",
                                {"name": "a", "password": "new-pw", "login": True})
        acc = self.gateway.pool.get("a")
        self.assertEqual(acc.password, "new-pw")
        self.assertTrue(acc.session_valid)
        self.assertEqual(self.gateway.pool.client.fills, ["a"])

    def test_set_password_upgrades_account(self):
        # account "a" already has a password; add one without
        self.gateway.pool.add_account("b", loginid="13900000009", session="s2")
        _s, payload = self.post("/api/panel/accounts/password", {"name": "b"})
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["generated"])
        self.assertGreaterEqual(len(payload["password"]), 8)
        acc = self.gateway.pool.get("b")
        self.assertEqual(acc.password, payload["password"])
        self.assertTrue(acc.permanent)
        self.assertTrue(acc.password_set_at)
        self.assertTrue(acc.session_valid)          # password set via live session
        self.assertEqual(self.gateway.pool.client.passwords[-1][0], "s2")

        # the generated password is persisted and exported
        saved = json.loads((self.dir / "accounts.json").read_text(encoding="utf-8"))
        row = next(a for a in saved["accounts"] if a["name"] == "b")
        self.assertEqual(row["password"], payload["password"])
        _s, export = self.get_json("/api/panel/export")
        row = next(a for a in export["accounts"] if a["name"] == "b")
        self.assertEqual(row["password"], payload["password"])
        self.assertTrue(row.get("passwordSetAt"))

    def test_set_password_with_explicit_value(self):
        self.gateway.pool.add_account("b", loginid="13900000009", session="s2")
        _s, payload = self.post("/api/panel/accounts/password",
                                {"name": "b", "password": "MyPass1234"})
        self.assertEqual(payload["password"], "MyPass1234")
        self.assertFalse(payload["generated"])
        self.assertEqual(self.gateway.pool.get("b").password, "MyPass1234")

    def test_set_password_rejects_weak_value(self):
        self.gateway.pool.add_account("b", loginid="13900000009", session="s2")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/panel/accounts/password", {"name": "b", "password": "short"})
        self.assertEqual(ctx.exception.code, 400)

    def test_set_password_requires_a_session(self):
        self.gateway.pool.add_account("b", loginid="13900000009")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/panel/accounts/password", {"name": "b"})
        self.assertEqual(ctx.exception.code, 400)
        body = json.loads(ctx.exception.read().decode("utf-8"))
        self.assertIn("session", json.dumps(body, ensure_ascii=False))

    def test_batch_set_passwords(self):
        self.gateway.pool.add_account("b", loginid="13900000009", session="s2")
        self.gateway.pool.add_account("c", loginid="13900000010", session="s3")
        _s, payload = self.post("/api/panel/accounts/passwords", {})
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["done"], 2)       # "a" already had a password
        self.assertEqual(payload["total"], 2)
        self.assertTrue(self.gateway.pool.get("b").permanent)
        self.assertTrue(self.gateway.pool.get("c").permanent)
        self.assertEqual(len(self.gateway.pool.client.passwords), 2)

    def test_batch_set_passwords_with_manual_password(self):
        self.gateway.pool.add_account("b", loginid="13900000009", session="s2")
        self.gateway.pool.add_account("c", loginid="13900000010", session="s3")
        _s, payload = self.post("/api/panel/accounts/passwords",
                                {"password": "SharedPass123"})
        self.assertEqual(payload["done"], 2)
        self.assertEqual(self.gateway.pool.get("b").password, "SharedPass123")
        self.assertEqual(self.gateway.pool.get("c").password, "SharedPass123")
        self.assertTrue(self.gateway.pool.get("b").permanent)

    def test_batch_set_passwords_continues_after_one_failure(self):
        """A single failing account must not abort the batch."""
        self.gateway.pool.add_account("b", loginid="13900000009", session="s2")
        self.gateway.pool.add_account("c", loginid="13900000010", session="s3")

        class Client(FakeAccountClient):
            def set_password(self, session, new_password, identity=None):
                if session == "s2":
                    raise AccountError("upstream exploded", code="HTTP_500")
                super().set_password(session, new_password, identity)

        from loomy2api.account import AccountError
        self.gateway.pool.client = Client()
        _s, payload = self.post("/api/panel/accounts/passwords", {})
        by_name = {r["name"]: r for r in payload["results"]}
        self.assertFalse(by_name["b"]["ok"])
        self.assertTrue(by_name["c"]["ok"])
        self.assertEqual(payload["failed"], 1)
        self.assertTrue(self.gateway.pool.get("c").permanent)

    def test_batch_set_passwords_rejects_weak_manual_password(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/panel/accounts/passwords", {"password": "short"})
        self.assertEqual(ctx.exception.code, 400)

    def test_keepalive_reuses_connection_with_body(self):
        """Two POSTs on one keep-alive socket must both carry their body.

        ``setup()`` runs per connection; before the per-request reset the
        second request saw the first one's ``_body_read`` flag and was handled
        with an empty body ("no such account: (empty)").
        """
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            for name in ("a", "a"):
                conn.request("POST", "/api/panel/accounts/password/show",
                             body=json.dumps({"name": name}),
                             headers={"Content-Type": "application/json"})
                resp = conn.getresponse()
                payload = json.loads(resp.read())
                self.assertEqual(resp.status, 200)
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["password"], "pw")
        finally:
            conn.close()

    def test_show_password(self):
        _s, payload = self.post("/api/panel/accounts/password/show", {"name": "a"})
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["password"], "pw")

    def test_show_password_for_account_without_one(self):
        self.gateway.pool.add_account("b", loginid="13900000009", session="s2")
        _s, payload = self.post("/api/panel/accounts/password/show", {"name": "b"})
        self.assertFalse(payload["ok"])
        self.assertIn("密码", payload["error"])

    def test_renew_reloads_pool_when_account_is_missing_in_memory(self):
        """A stale in-memory pool must not answer "no such account"."""
        # add the account to the file only, then force the in-memory pool to
        # forget it (simulates an external edit)
        from tests.support import write_accounts
        write_accounts(self.dir / "accounts.json", [
            {"name": "a", "loginid": "13800000001", "password": "pw",
             "session": "s1", "userid": "u1",
             "expireAt": int(time.time()) + 14 * 86400},
            {"name": "b", "loginid": "13900000009", "password": "pw2",
             "session": "s2", "userid": "u2",
             "expireAt": int(time.time()) + 14 * 86400},
        ])
        self.gateway.pool.remove_account("b")      # in-memory only
        _s, payload = self.post("/api/panel/accounts/renew", {"name": "b"})
        self.assertNotIn("no such account", payload.get("error", ""))

    def test_batch_set_passwords_skips_known_upstream_password(self):
        """Never rotate a password the user set but the pool doesn't store."""
        self.gateway.pool.add_account("b", loginid="13900000009", session="s2")

        class Client(FakeAccountClient):
            def password_status(self, session, identity=None):
                return session == "s2"          # only "b" already has one

        self.gateway.pool.client = Client()
        _s, payload = self.post("/api/panel/accounts/passwords", {})
        self.assertEqual(payload["done"], 0)
        row = next(r for r in payload["results"] if r["name"] == "b")
        self.assertFalse(row["ok"])
        self.assertTrue(row.get("skipped"))
        self.assertFalse(self.gateway.pool.get("b").has_password)

    def test_state_exposes_permanent_flag(self):
        _s, payload = self.get_json("/api/panel/state")
        account = next(a for a in payload["accounts"] if a["name"] == "a")
        self.assertTrue(account["permanent"])
        self.assertTrue(account["has_password"])

    def test_remove_account(self):
        _s, payload = self.post("/api/panel/accounts/remove", {"name": "a"})
        self.assertTrue(payload["ok"])
        self.assertIsNone(self.gateway.pool.get("a"))
        saved = json.loads((self.dir / "accounts.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["accounts"], [])

    def test_remove_unknown_account_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/panel/accounts/remove", {"name": "nope"})
        self.assertEqual(ctx.exception.code, 400)

    def test_unknown_panel_endpoint_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/panel/nonsense", {})
        self.assertEqual(ctx.exception.code, 404)

    def test_logs_endpoint(self):
        self.gateway.log("panel test line")
        _s, payload = self.get_json("/api/panel/logs?lines=20")
        self.assertTrue(payload["ok"])
        self.assertTrue(any("panel test line" in line for line in payload["lines"]))


class TestPanelAuth(PanelHarness, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.gateway.cfg["api_keys"] = ["panel-key"]

    def test_state_requires_key(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get_json("/api/panel/state")
        self.assertEqual(ctx.exception.code, 401)

    def test_state_with_key(self):
        _s, payload = self.get_json("/api/panel/state",
                                    headers={"Authorization": "Bearer panel-key"})
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["config"]["auth_required"])

    def test_writes_require_key(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/panel/accounts/remove", {"name": "a"})
        self.assertEqual(ctx.exception.code, 401)
        self.assertIsNotNone(self.gateway.pool.get("a"))

    def test_page_itself_stays_public(self):
        status, _response = self.get("/panel")
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
