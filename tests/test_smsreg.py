"""fysms 接码注册 tests — local fake platform, no network, no money.

Covers the client plumbing (cookies, number, polling, blacklist, code
extraction) and the panel endpoint (refuses without ``confirm``, creates an
account, sets a password, never echoes the platform password).
"""

from __future__ import annotations

import base64
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

from loomy2api.fysms import (CHANNEL_ID, CODE_PRICE, FysmsClient, FysmsError,
                             extract_code, read_captcha)
from loomy2api.smsreg import fysms_status, register_batch
from tests.support import FakeAccountClient, FakeUpstream, make_config, write_accounts


def _captcha_svg(text: str) -> str:
    """A base64 SVG captcha shaped like the platform's (plaintext ``<text>``)."""
    svg = ("<svg xmlns='http://www.w3.org/2000/svg'>"
           + "".join("<text x='10' y='20'>%s</text>" % ch for ch in text)
           + "</svg>")
    return "data:image/svg+xml;base64," + base64.b64encode(
        svg.encode("utf-8")).decode()


class FakeFysms:
    """Minimal stand-in for the new fyapi.fysms.com JSON API (v1)."""

    CAPTCHA = "AB12"

    def __init__(self):
        self.username = "user"
        self.password = "pass"
        self.balance = 38.0
        self.frozen = 0.0
        self.phones = ["13800000111", "13800000222"]
        self.sms = "【讯飞】验证码 123456，5 分钟内有效"
        self.login_calls = 0
        self.taken = []          # phones actually fetched
        self.released = []       # order_no submitted for release
        self.code_polls = 0
        self.fetch_requests = []
        self.server = None
        self._order_seq = 0

    def _json(self, handler, obj, status=200):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)

    def start(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _payload(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    return json.loads(raw.decode("utf-8"))
                except Exception:                        # noqa: BLE001
                    return {}

            def _auth_ok(self):
                token = (self.headers.get("Authorization") or "").replace(
                    "Bearer ", "", 1)
                return token == fake.token

            def do_GET(self):
                path = urlparse(self.path).path
                query = parse_qs(urlparse(self.path).query)
                if path.endswith("/captcha/image"):
                    scene = (query.get("scene") or ["login"])[0]
                    fake.last_captcha_scene = scene
                    fake._json(self, {"code": 0, "message": "ok", "data": {
                        "captcha_key": "k-" + scene,
                        "image": _captcha_svg(fake.CAPTCHA),
                        "expires_in": 300}})
                    return
                if not self._auth_ok():
                    fake._json(self, {"code": 401, "message": "缺少访问令牌",
                                      "data": None})
                    return
                if path.endswith("/user/balance"):
                    fake._json(self, {"code": 0, "message": "ok", "data": {
                        "balance": fake.balance, "frozen_balance": fake.frozen}})
                    return
                if path.endswith("/user/profile"):
                    fake._json(self, {"code": 0, "message": "ok", "data": {
                        "user_id": 86755, "username": fake.username,
                        "balance": fake.balance}})
                    return
                if path.endswith("/sms/orders"):
                    fake._json(self, {"code": 0, "message": "ok", "data": {
                        "list": fake.orders(), "total": len(fake.orders())}})
                    return
                fake._json(self, {"code": 404, "message": "not found",
                                  "data": None})

            def do_POST(self):
                path = urlparse(self.path).path
                body = self._payload()
                if path.endswith("/auth/login"):
                    if (body.get("username") == fake.username
                            and body.get("password") == fake.password
                            and body.get("captcha_code") == fake.CAPTCHA):
                        fake.login_calls += 1
                        fake.token = "tok-%d" % fake.login_calls
                        fake._json(self, {"code": 0, "message": "登录成功", "data": {
                            "user_id": 86755, "username": fake.username,
                            "api_token": fake.token}})
                    else:
                        fake._json(self, {"code": 1001,
                                          "message": "用户名或密码错误"})
                    return
                if not self._auth_ok():
                    fake._json(self, {"code": 401, "message": "缺少访问令牌",
                                      "data": None})
                    return
                if path.endswith("/sms/fetch"):
                    fake.fetch_requests.append(body)
                    qty = max(1, int(body.get("quantity") or 1))
                    if not body.get("keyword"):
                        fake._json(self, {"code": 400,
                                          "message": "通道2必须提供 keyword"})
                        return
                    if fake.balance - fake.frozen < CODE_PRICE * qty:
                        fake._json(self, {"code": 402, "message": "余额不足"})
                        return
                    if not fake.phones:
                        fake._json(self, {"code": 503, "message": "号码不可用"})
                        return
                    phone = fake.phones.pop(0)
                    fake.taken.append(phone)
                    fake.frozen = round(fake.frozen + CODE_PRICE * qty, 2)
                    fake._order_seq += 1
                    order = {"order_no": "FY%03d" % fake._order_seq,
                             "phone": phone, "amount": CODE_PRICE, "status": 1}
                    fake.open_orders[order["order_no"]] = order
                    fake._json(self, {"code": 0, "message": "取号成功",
                                      "data": {"orders": [order], **order}})
                    return
                if path.endswith("/sms/query"):
                    fake.code_polls += 1
                    order_no = body.get("order_no")
                    if fake.sms and fake.sms != "1":
                        # delivering a code converts the freeze into a charge
                        fake.frozen = round(max(0.0, fake.frozen - CODE_PRICE), 2)
                        fake.balance = round(fake.balance - CODE_PRICE, 2)
                        fake._json(self, {"code": 0, "message": "ok", "data": {
                            "order_no": order_no, "status": 2,
                            "content": fake.sms}})
                    else:
                        fake._json(self, {"code": 0, "message": "还未收到验证码",
                                          "data": {"order_no": order_no,
                                                   "status": 1}})
                    return
                if path.endswith("/sms/release"):
                    order_no = body.get("order_no")
                    fake.released.append(order_no)
                    order = fake.open_orders.pop(order_no, None)
                    if order:
                        # release refunds the frozen amount
                        fake.frozen = round(max(0.0, fake.frozen - CODE_PRICE), 2)
                    fake._json(self, {"code": 409,
                                      "message": "已提交释放申请，3 分钟后自动处理",
                                      "data": None})
                    return
                fake._json(self, {"code": 404, "message": "not found",
                                  "data": None})

        self.token = ""
        self.open_orders = {}
        self.last_captcha_scene = ""
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return "http://127.0.0.1:%d" % self.server.server_address[1]

    def orders(self):
        return list(self.open_orders.values())

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


class TestReadCaptcha(unittest.TestCase):
    def test_reads_plaintext_svg(self):
        self.assertEqual(read_captcha(_captcha_svg("PRHD")), "PRHD")

    def test_rejects_non_svg(self):
        with self.assertRaises(FysmsError):
            read_captcha("data:image/png;base64,AAAA")


class TestFysmsClient(unittest.TestCase):
    def setUp(self):
        self.fake = FakeFysms()
        self.base = self.fake.start()

    def tearDown(self):
        self.fake.stop()

    def test_login_returns_userid_and_keeps_token(self):
        client = FysmsClient(self.fake.username, self.fake.password, base=self.base)
        self.assertEqual(client.login(), "86755")
        self.assertTrue(client.token)
        self.assertEqual(self.fake.last_captcha_scene, "login")

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
        # taking a number only freezes the fee; nothing is charged yet
        self.assertAlmostEqual(self.fake.balance, 38.0)
        self.assertAlmostEqual(self.fake.frozen, CODE_PRICE)
        self.assertTrue(client.last_order_no)
        self.assertEqual(client.get_code(phone), self.fake.sms)
        self.assertAlmostEqual(self.fake.balance, 38.0 - CODE_PRICE)

    def test_fetch_uses_keyword_channel(self):
        client = FysmsClient(self.fake.username, self.fake.password, base=self.base)
        client.login()
        client.get_phone("讯飞")
        req = self.fake.fetch_requests[-1]
        self.assertEqual(req["channel_id"], CHANNEL_ID)
        self.assertEqual(req["keyword"], "讯飞")
        self.assertTrue(req["idempotency_key"])

    def test_balance_exhausted(self):
        self.fake.balance = 1.0
        client = FysmsClient(self.fake.username, self.fake.password, base=self.base)
        client.login()
        with self.assertRaises(FysmsError) as ctx:
            client.get_phone()
        self.assertIn("余额不足", str(ctx.exception))

    def test_release_refunds_the_freeze(self):
        client = FysmsClient(self.fake.username, self.fake.password, base=self.base)
        client.login()
        client.get_phone()
        self.assertAlmostEqual(self.fake.frozen, CODE_PRICE)
        self.assertTrue(client.blacklist())
        self.assertEqual(self.fake.released, [client.last_order_no])
        self.assertAlmostEqual(self.fake.frozen, 0.0)

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

    def test_timeout_releases_the_order(self):
        self.fake.sms = "1"          # the platform's "nothing yet" marker
        gw = self._gateway()
        summary = register_batch(gw, 1, username=self.fake.username,
                                 password=self.fake.password, max_wait=5)
        self.assertEqual(summary["done"], 0)
        self.assertTrue(summary["results"][0]["blacklisted"])
        self.assertEqual(self.fake.released, ["FY001"])
        # a timeout never charges: the fee is taken when a code is returned
        self.assertEqual(summary["charged"], 0.0)
        self.assertAlmostEqual(self.fake.frozen, 0.0)

    def test_polling_stops_at_platform_release_limit(self):
        """Polling past the platform's auto-release point is pointless."""
        self.fake.sms = "1"
        client = FysmsClient(self.fake.username, self.fake.password,
                             base=self.base)
        client.login()
        phone = client.get_phone()
        self.assertIsNone(client.get_code(phone, max_wait=999, interval=0))
        self.assertEqual(self.fake.code_polls, FysmsClient.MAX_POLLS)

    def test_stops_when_balance_runs_out(self):
        self.fake.balance = 1.2      # enough for exactly one code (1.18)
        gw = self._gateway()
        summary = register_batch(gw, 3, username=self.fake.username,
                                 password=self.fake.password, max_wait=5)
        self.assertEqual(len(summary["results"]), 2)   # one ok, one "余额不足"
        self.assertEqual(summary["done"], 1)
        self.assertIn("余额不足", summary["results"][1]["error"])

    # -- invitation binding -------------------------------------------------

    def _donor(self, name="donor1", session="donorsess", codes=None):
        """Put a donor account in the pool and give its session some codes."""
        write_accounts(self.dir / "accounts.json", [
            {"name": name, "loginid": name, "session": session, "enabled": True}])
        self.upstream.invite_codes[session] = codes if codes is not None else [
            {"inviteCode": "ABC123", "status": "active",
             "maxUses": 20, "usedCount": 0}]
        return name

    def test_register_binds_invite_from_selected_donor(self):
        self._donor()
        gw = self._gateway()
        summary = register_batch(gw, 1, username=self.fake.username,
                                 password=self.fake.password, max_wait=5,
                                 invite_targets=["donor1"])
        row = summary["results"][0]
        self.assertTrue(row["ok"])
        self.assertTrue(row["invite"]["ok"])
        self.assertEqual(row["invite"]["code"], "ABC123")
        self.assertEqual(row["invite"]["from"], "donor1")
        self.assertEqual(row["invite"]["invitee_reward"], 5000)
        # the code really went upstream, for the *new* account's session
        self.assertEqual(len(self.upstream.activations), 1)
        self.assertEqual(self.upstream.activations[0]["code"], "ABC123")
        acc = gw.pool.get("13800000111")
        self.assertEqual(acc.applied_invitation_code, "ABC123")

    def test_register_without_targets_binds_nothing(self):
        gw = self._gateway()
        summary = register_batch(gw, 1, username=self.fake.username,
                                 password=self.fake.password, max_wait=5)
        self.assertNotIn("invite", summary["results"][0])
        self.assertEqual(self.upstream.activations, [])

    def test_invite_failure_does_not_break_registration(self):
        self._donor(codes=[])          # donor exists but has no usable code
        gw = self._gateway()
        summary = register_batch(gw, 1, username=self.fake.username,
                                 password=self.fake.password, max_wait=5,
                                 invite_targets=["donor1"])
        self.assertTrue(summary["results"][0]["ok"])   # registration unaffected
        self.assertEqual(summary["invite_codes"], 0)
        self.assertEqual(self.upstream.activations, [])

    def test_exhausted_and_used_codes_are_skipped(self):
        self._donor(codes=[
            {"inviteCode": "DEAD01", "status": "exhausted",
             "maxUses": 1, "usedCount": 1},
            {"inviteCode": "USED01", "status": "active",
             "maxUses": 1, "usedCount": 1},
            {"inviteCode": "GOOD01", "status": "active",
             "maxUses": 20, "usedCount": 3}])
        gw = self._gateway()
        summary = register_batch(gw, 1, username=self.fake.username,
                                 password=self.fake.password, max_wait=5,
                                 invite_targets=["donor1"])
        self.assertEqual(summary["invite_codes"], 1)   # only GOOD01 qualifies
        self.assertEqual(summary["results"][0]["invite"]["code"], "GOOD01")


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

    def test_register_rejects_more_than_five_inviters(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/panel/accounts/register-sms", {
                "fysms_username": self.fake.username,
                "fysms_password": self.fake.password,
                "count": 1, "confirm": True,
                "invite_targets": ["a", "b", "c", "d", "e", "f"]})
        self.assertEqual(ctx.exception.code, 400)
        self.assertEqual(self.fake.taken, [])       # rejected before spending

    def test_register_binds_invite_via_panel(self):
        self.gateway.pool.add_account("donor1", loginid="donor1",
                                      session="donorsess")
        self.upstream.invite_codes["donorsess"] = [
            {"inviteCode": "PNL001", "status": "active",
             "maxUses": 20, "usedCount": 0}]
        payload = self.post("/api/panel/accounts/register-sms", {
            "fysms_username": self.fake.username,
            "fysms_password": self.fake.password,
            "count": 1, "confirm": True,
            "invite_targets": ["donor1"]})
        self.assertTrue(payload["ok"])
        row = payload["results"][0]
        self.assertEqual(row["invite"]["code"], "PNL001")
        self.assertEqual(row["invite"]["from"], "donor1")
        self.assertEqual(self.upstream.activations[0]["code"], "PNL001")

    def test_register_rejects_donors_with_no_usable_code(self):
        """A bad donor selection must fail before any number is bought."""
        self.gateway.pool.add_account("donor1", loginid="donor1",
                                      session="donorsess")
        self.upstream.invite_codes["donorsess"] = []      # nothing to hand out
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/panel/accounts/register-sms", {
                "fysms_username": self.fake.username,
                "fysms_password": self.fake.password,
                "count": 1, "confirm": True,
                "invite_targets": ["donor1"]})
        self.assertEqual(ctx.exception.code, 400)
        self.assertEqual(self.fake.taken, [])             # nothing was spent


if __name__ == "__main__":
    unittest.main()
