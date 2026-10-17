"""fysms 接码注册 tests — local fake platform, no network, no money.

Covers the client plumbing (cookies, number, polling, blacklist, code
extraction) and the panel endpoint (refuses without ``confirm``, creates an
account, sets a password, never echoes the platform password).
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from loomy2api.fysms import FysmsClient, FysmsError, extract_code
from loomy2api.smsreg import fysms_status, register_batch
from tests.support import FakeAccountClient, FakeUpstream, make_config, write_accounts


class FakeFysms:
    """Minimal stand-in for h5.fysms.cc."""

    def __init__(self):
        self.username = "user"
        self.password = "pass"
        self.balance = 38.0
        self.phones = ["13800000111", "13800000222"]
        self.sms = "【讯飞】验证码 123456，5 分钟内有效"
        self.login_calls = 0
        self.taken = []
        self.blacklisted = []
        self.code_polls = 0
        self.server = None

    def start(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, body, status=200, cookies=()):
                raw = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                for cookie in cookies:
                    self.send_header("Set-Cookie", cookie)
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                form = parse_qs(self.rfile.read(length).decode())
                if "a=login" in self.path:
                    if (form.get("username", [""])[0] == fake.username
                            and form.get("password", [""])[0] == fake.password):
                        fake.login_calls += 1
                        self._send('\ufeff{"code":1,"msg":"登录成功"}',
                                   cookies=["users_id=86755; path=/",
                                            "PHPSESSID=abc; path=/"])
                    else:
                        self._send('\ufeff{"code":0,"msg":"用户名不正确！"}')
                    return
                self._send("not found", 404)

            def do_GET(self):
                query = parse_qs(urlparse(self.path).query)
                action = (query.get("a") or [""])[0]
                if "c=Users&a=index" in self.path:
                    self._send(
                        '<div class="user-info-bottom"><a href="#"><span><b>%s</b>'
                        '</span><span><b>余额</b></span></a></div>'
                        % (int(fake.balance) if fake.balance.is_integer()
                           else fake.balance))
                    return
                if action == "selectrmb_21":
                    if fake.balance < 1.6:
                        self._send("buzu")
                    else:
                        fake.balance -= 1.6
                        self._send("1")
                    return
                if action == "getxksjh":
                    if not fake.phones:
                        self._send('""')
                        return
                    phone = fake.phones.pop(0)
                    fake.taken.append(phone)
                    self._send('"%s"' % phone)
                    return
                if action == "selectrmb":
                    fake.code_polls += 1
                    self._send('"%s"' % fake.sms)
                    return
                if action == "releasephone":
                    fake.blacklisted.append((query.get("phone") or [""])[0])
                    self._send("success")
                    return
                self._send("1")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return "http://127.0.0.1:%d" % self.server.server_address[1]

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()


class TestExtractCode(unittest.TestCase):
    def test_chinese_sms(self):
        self.assertEqual(extract_code("【讯飞】验证码 123456，5 分钟内有效"), "123456")

    def test_falls_back_to_first_digits(self):
        self.assertEqual(extract_code("code: 4321"), "4321")

    def test_no_code(self):
        self.assertIsNone(extract_code("no digits here"))


class TestFysmsClient(unittest.TestCase):
    def setUp(self):
        self.fake = FakeFysms()
        self.base = self.fake.start()

    def tearDown(self):
        self.fake.stop()

    def test_login_keeps_cookies_and_returns_userid(self):
        client = FysmsClient(self.fake.username, self.fake.password, base=self.base)
        self.assertEqual(client.login(), "86755")
        self.assertEqual(client.cookies.get("PHPSESSID"), "abc")

    def test_login_rejects_bad_credentials(self):
        client = FysmsClient("user", "wrong", base=self.base)
        with self.assertRaises(FysmsError):
            client.login()

    def test_balance(self):
        client = FysmsClient(self.fake.username, self.fake.password, base=self.base)
        client.login()
        self.assertEqual(client.balance(), 38.0)

    def test_take_number_and_code(self):
        client = FysmsClient(self.fake.username, self.fake.password, base=self.base)
        client.login()
        phone = client.get_phone()
        self.assertEqual(phone, "13800000111")
        self.assertAlmostEqual(self.fake.balance, 38.0 - 1.6)
        self.assertEqual(client.get_code(phone), self.fake.sms)

    def test_balance_exhausted(self):
        self.fake.balance = 1.0
        client = FysmsClient(self.fake.username, self.fake.password, base=self.base)
        client.login()
        with self.assertRaises(FysmsError) as ctx:
            client.get_phone()
        self.assertIn("余额不足", str(ctx.exception))

    def test_blacklist(self):
        client = FysmsClient(self.fake.username, self.fake.password, base=self.base)
        client.login()
        self.assertTrue(client.blacklist("13800000111"))
        self.assertEqual(self.fake.blacklisted, ["13800000111"])

    def test_status_spends_nothing(self):
        status = fysms_status(self.fake.username, self.fake.password, self.base)
        self.assertEqual(status["balance"], 38.0)
        self.assertEqual(self.fake.taken, [])


class TestRegisterBatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.fake = FakeFysms()
        self.base = self.fake.start()
        self.upstream = FakeUpstream()
        self.upstream_base = self.upstream.start()
        write_accounts(self.dir / "accounts.json", [])
        self.cfg = make_config(self.dir, self.upstream_base)
        self.cfg["fysms_base"] = self.base
        self.cfg["fysms_max_wait"] = 5

    def tearDown(self):
        self.fake.stop()
        self.upstream.stop()
        self.tmp.cleanup()

    def _gateway(self):
        from loomy2api.server import Gateway
        gw = Gateway(self.cfg)
        gw.pool.client = FakeAccountClient()
        return gw

    def test_register_creates_permanent_account(self):
        gw = self._gateway()
        summary = register_batch(gw, 1, username=self.fake.username,
                                 password=self.fake.password,
                                 max_wait=5)
        self.assertEqual(summary["done"], 1)
        self.assertEqual(summary["failed"], 0)
        row = summary["results"][0]
        self.assertTrue(row["ok"])
        self.assertEqual(row["phone"], "13800000111")
        self.assertTrue(row["permanent"])
        acc = gw.pool.get("13800000111")
        self.assertIsNotNone(acc)
        self.assertTrue(acc.permanent)
        self.assertEqual(acc.loginid, "13800000111")

    def test_unparsable_sms_does_not_create_account(self):
        self.fake.sms = "没有数字的短信"
        gw = self._gateway()
        summary = register_batch(gw, 1, username=self.fake.username,
                                 password=self.fake.password, max_wait=5)
        self.assertEqual(summary["done"], 0)
        self.assertEqual(summary["failed"], 1)
        self.assertIn("验证码", summary["results"][0]["error"])

    def test_timeout_blacklists_number(self):
        self.fake.sms = "1"          # the platform's "nothing yet" marker
        gw = self._gateway()
        summary = register_batch(gw, 1, username=self.fake.username,
                                 password=self.fake.password, max_wait=5)
        self.assertEqual(summary["done"], 0)
        self.assertTrue(summary["results"][0]["blacklisted"])
        self.assertEqual(self.fake.blacklisted, ["13800000111"])

    def test_stops_when_balance_runs_out(self):
        self.fake.balance = 1.7      # enough for exactly one number
        gw = self._gateway()
        summary = register_batch(gw, 3, username=self.fake.username,
                                 password=self.fake.password, max_wait=5)
        self.assertEqual(len(summary["results"]), 2)   # one ok, one "余额不足"
        self.assertEqual(summary["done"], 1)
        self.assertIn("余额不足", summary["results"][1]["error"])


class TestPanelRegisterSms(unittest.TestCase):
    """HTTP-level: confirmation gate + credentials never echoed."""

    def setUp(self):
        from loomy2api.server import Gateway, Handler, Server

        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.fake = FakeFysms()
        self.base = self.fake.start()
        self.upstream = FakeUpstream()
        upstream_base = self.upstream.start()
        write_accounts(self.dir / "accounts.json", [])
        self.cfg = make_config(self.dir, upstream_base)
        self.cfg["fysms_base"] = self.base
        self.cfg["fysms_max_wait"] = 5
        self.gateway = Gateway(self.cfg)
        self.gateway.pool.client = FakeAccountClient()
        handler = type("Bound", (Handler,), {"gateway": self.gateway})
        self.httpd = Server(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.fake.stop()
        self.upstream.stop()
        self.tmp.cleanup()

    def post(self, path, payload):
        request = urllib.request.Request(
            self.url + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        response = urllib.request.urlopen(request, timeout=60)
        return json.loads(response.read())

    def test_balance_endpoint_spends_nothing(self):
        payload = self.post("/api/panel/fysms/balance",
                            {"fysms_username": self.fake.username,
                             "fysms_password": self.fake.password})
        self.assertEqual(payload["balance"], 38.0)
        self.assertEqual(self.fake.taken, [])
        self.assertNotIn("fysms_password", json.dumps(payload))

    def test_register_requires_confirm(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/panel/accounts/register-sms",
                      {"fysms_username": self.fake.username,
                       "fysms_password": self.fake.password, "count": 1})
        self.assertEqual(ctx.exception.code, 400)
        self.assertEqual(self.fake.taken, [])       # nothing was charged

    def test_register_creates_account_and_hides_secret(self):
        payload = self.post("/api/panel/accounts/register-sms", {
            "fysms_username": self.fake.username,
            "fysms_password": self.fake.password,
            "count": 1, "confirm": True,
        })
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["done"], 1)
        # the platform password must never travel back; the *account* password
        # is intentionally returned so the user can write it down
        self.assertNotIn('"fysms_password"', json.dumps(payload))
        self.assertNotIn('"password": "pass"', json.dumps(payload))
        self.assertTrue(any(a["name"] == "13800000111"
                            for a in payload["state"]["accounts"]))
        # credentials are remembered for the next run
        self.assertEqual(self.cfg.get("fysms_username"), self.fake.username)
        self.assertEqual(self.cfg.get("fysms_password"), self.fake.password)
        state = self.gateway.panel.state()
        self.assertTrue(state["config"]["fysms_password_set"])
        self.assertNotIn('"fysms_password"', json.dumps(state))

    def test_register_count_is_bounded(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/panel/accounts/register-sms", {
                "fysms_username": self.fake.username,
                "fysms_password": self.fake.password,
                "count": 99, "confirm": True})
        self.assertEqual(ctx.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
