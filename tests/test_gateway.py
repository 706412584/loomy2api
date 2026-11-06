"""End-to-end gateway tests against a local fake upstream (hermetic).

Covers: OpenAI chat (stream + non-stream), Anthropic /v1/messages (both),
model catalogue, quota endpoint, passthroughs, API-key auth and account
rotation when one account's session is rejected.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from loomy2api.server import Gateway, Handler, Server
from tests.support import FakeUpstream, free_port, make_config, write_accounts


class _Harness:
    """Shared fixture: fake upstream + a real gateway on an ephemeral port.

    Deliberately not a ``TestCase`` — the auth tests below need the same
    fixture without inheriting the unauthenticated test cases.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.upstream = FakeUpstream()
        base = self.upstream.start()
        write_accounts(self.dir / "accounts.json", [
            {"name": "a", "session": "s1",
             "expireAt": int(__import__("time").time()) + 14 * 86400},
            {"name": "b", "session": "s2",
             "expireAt": int(__import__("time").time()) + 14 * 86400},
        ])
        self.cfg = make_config(self.dir, base)
        self.gateway = Gateway(self.cfg)
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

    def post(self, path, payload, *, stream=False, headers=None, timeout=30):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST")
        response = urllib.request.urlopen(request, timeout=timeout)
        body = response.read()
        return response.status, body, response

    def get(self, path, headers=None):
        request = urllib.request.Request(self.base + path, headers=headers or {})
        response = urllib.request.urlopen(request, timeout=30)
        return response.status, json.loads(response.read())

    def sse(self, body: bytes):
        """Parse an SSE payload into ``[(event, data)]``."""
        events = []
        for block in body.decode("utf-8").split("\n\n"):
            event, data = None, None
            for line in block.splitlines():
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: "):
                    data = line[6:]
            if event or data:
                events.append((event, data))
        return events

    # -- tests ----------------------------------------------------------


class GatewayTestCase(_Harness, unittest.TestCase):
    """OpenAI/Anthropic surface, no gateway-side auth configured."""

    def test_health(self):
        status, payload = self.get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["accounts"], 2)
        self.assertEqual(payload["usable_accounts"], 2)
        self.assertFalse(payload["auth_required"])

    def test_models_lists_the_upstream_catalogue(self):
        _status, payload = self.get("/v1/models")
        ids = [m["id"] for m in payload["data"]]
        self.assertIn("fake-model", ids)
        self.assertTrue(all(m.get("owned_by") != "alias" for m in payload["data"]))

    def test_chat_non_stream(self):
        status, body, _ = self.post("/v1/chat/completions", {
            "model": "imodel/fake-model",
            "messages": [{"role": "user", "content": "ping"}],
        })
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["model"], "fake-model")     # prefix stripped
        self.assertEqual(payload["choices"][0]["message"]["content"], "echo:ping")
        self.assertEqual(payload["usage"]["points_consumed"], 1)

    def test_model_name_is_passed_through_verbatim(self):
        """No alias table: an unknown name goes upstream as-is (so a typo is a
        400 from the upstream, not a silent redirect to another model)."""
        _s, body, _ = self.post("/v1/chat/completions", {
            "model": "gpt-4o-mini", "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(json.loads(body)["model"], "gpt-4o-mini")

    def test_missing_model_falls_back_to_default(self):
        _s, body, _ = self.post("/v1/chat/completions", {
            "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(json.loads(body)["model"], self.cfg["default_model"])

    def test_chat_stream(self):
        _s, body, _ = self.post("/v1/chat/completions", {
            "model": "fake-model", "stream": True,
            "messages": [{"role": "user", "content": "hi"}]})
        text = body.decode("utf-8")
        self.assertIn("data: ", text)
        self.assertIn('"content": "hello"', text)
        self.assertIn('"points_consumed": 1', text)
        self.assertTrue(text.rstrip().endswith("data: [DONE]"))

    def test_messages_non_stream(self):
        _s, body, _ = self.post("/v1/messages", {
            "model": "fake-model", "max_tokens": 32,
            "messages": [{"role": "user", "content": "hi"}]})
        payload = json.loads(body)
        self.assertEqual(payload["type"], "message")
        self.assertEqual(payload["content"][0]["type"], "text")
        self.assertEqual(payload["content"][0]["text"], "echo:hi")
        self.assertEqual(payload["usage"]["output_tokens"], 5)

    def test_messages_stream(self):
        _s, body, _ = self.post("/v1/messages", {
            "model": "fake-model", "stream": True, "max_tokens": 32,
            "messages": [{"role": "user", "content": "hi"}]})
        events = self.sse(body)
        names = [e for e, _ in events]
        self.assertEqual(names[0], "message_start")
        self.assertEqual(names[-1], "message_stop")
        self.assertIn("content_block_delta", names)
        deltas = [json.loads(d) for e, d in events if e == "content_block_delta"]
        self.assertTrue(any(d["delta"].get("type") == "thinking_delta"
                            and d["delta"].get("thinking") == "think " for d in deltas))
        self.assertTrue(any(d["delta"].get("type") == "text_delta"
                            and d["delta"].get("text") == "hello" for d in deltas))

    def test_points_endpoint(self):
        _s, payload = self.get("/v1/points")
        self.assertEqual(payload["total_available"], 300)     # 150 × 2 accounts
        self.assertEqual(len(payload["accounts"]), 2)
        self.assertEqual(payload["unique_accounts"], 2)

    def test_points_dedupes_same_user(self):
        """Two sessions of one account must not double-count the quota."""
        self.gateway.pool.get("a").userid = "u1"
        self.gateway.pool.get("b").userid = "u1"
        _s, payload = self.get("/v1/points")
        self.assertEqual(payload["total_available"], 150)
        self.assertEqual(payload["unique_accounts"], 1)

    def test_embeddings_and_images_passthrough(self):
        _s, body, _ = self.post("/v1/embeddings", {"model": "fake-model", "input": ["a"]})
        self.assertEqual(json.loads(body)["data"][0]["embedding"], [0.1])
        _s, body, _ = self.post("/v1/images/generations", {"model": "fake-model",
                                                          "prompt": "cat"})
        self.assertIn("url", json.loads(body)["data"][0])

    def test_rotation_on_rejected_session(self):
        # make the pick deterministic: the richest account is tried first
        self.gateway.pool.get("a").available = 1000
        self.gateway.pool.get("b").available = 10
        self.upstream.valid_sessions = ["s2"]         # account "a" is now invalid
        status, body, _ = self.post("/v1/chat/completions", {
            "model": "fake-model", "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["choices"][0]["message"]["content"], "echo:x")
        # the rejected account is cooled down and its dead session dropped
        acc_a = self.gateway.pool.get("a")
        self.assertFalse(acc_a.session_valid)
        self.assertGreater(acc_a.failures, 0)
        self.assertTrue(acc_a.in_cooldown)

    def test_upstream_5xx_does_not_cool_down_the_account(self):
        """A 504/502 from the upstream is not the account's fault: retry on
        another account but keep the first one healthy (it did nothing wrong)."""
        self.upstream.reject_with = {"fake-model": (504, "upstream hiccup")}
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/v1/chat/completions", {
                "model": "fake-model", "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(ctx.exception.code, 504)
        self.assertEqual(self.gateway.pool.usable(), self.gateway.pool.accounts)
        for acc in self.gateway.pool.accounts:
            self.assertFalse(acc.in_cooldown,
                             "5xx must not put an account in cooldown")
            self.assertTrue(acc.session_valid, "5xx must not drop the session")

    def test_content_rejection_keeps_accounts_healthy(self):
        """A 403 from the provider's content inspection is *not* an auth error.

        Regression: it used to be classified as an account fault, so a single
        rejected payload wiped every session in the pool (each retry hit the
        next account with the same body and got the same 403) and left the pool
        needing manual SMS re-login.
        """
        self.upstream.content_reject = ["fake-model"]
        self.upstream.calls.clear()
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/v1/chat/completions", {
                "model": "fake-model", "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(ctx.exception.code, 403)
        # tried once only: rotating cannot help when the payload is the problem
        self.assertEqual([c["path"] for c in self.upstream.calls],
                         ["/api/v1/chat/completions"],
                         "content rejection must not be retried on other accounts")
        self.assertEqual(self.gateway.pool.usable(), self.gateway.pool.accounts)
        for acc in self.gateway.pool.accounts:
            self.assertTrue(acc.session_valid,
                            "content rejection must not drop the session")
            self.assertFalse(acc.in_cooldown,
                             "content rejection must not cool the account down")

    def test_content_rejection_keeps_sessions_in_streaming_path(self):
        """Same guarantee on the streaming path, which is where it bit us."""
        self.upstream.content_reject = ["fake-model"]
        self.upstream.calls.clear()
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/v1/chat/completions", {
                "model": "fake-model", "stream": True,
                "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(ctx.exception.code, 403)
        self.assertEqual([c["path"] for c in self.upstream.calls],
                         ["/api/v1/chat/completions"])
        for acc in self.gateway.pool.accounts:
            self.assertTrue(acc.session_valid)
            self.assertFalse(acc.in_cooldown)

    def test_rotation_when_account_quota_exhausted(self):
        self.upstream.exhausted_sessions = ["s1", "s2"]
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/v1/chat/completions", {
                "model": "fake-model", "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(ctx.exception.code, 402)

    def test_stream_falls_back_to_healthy_account(self):
        self.upstream.valid_sessions = ["s2"]
        _s, body, _ = self.post("/v1/chat/completions", {
            "model": "fake-model", "stream": True,
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertIn("hello", body.decode("utf-8"))

    def test_dead_session_200_json_rotates_to_healthy_account(self):
        """A dead session answers 200 + JSON (code 100002), not 401.

        It must be treated as an account failure and the request retried on a
        healthy account — not parsed as an empty SSE stream.
        """
        self.gateway.pool.get("a").available = 1000      # picked first
        self.gateway.pool.get("b").available = 10
        self.upstream.dead_sessions_200 = ["s1"]
        _s, body, _ = self.post("/v1/chat/completions", {
            "model": "fake-model", "stream": True,
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertIn("hello", body.decode("utf-8"))
        acc_a = self.gateway.pool.get("a")
        self.assertFalse(acc_a.session_valid)
        self.assertTrue(acc_a.in_cooldown)

    def test_dead_session_200_json_rotates_non_stream(self):
        self.gateway.pool.get("a").available = 1000
        self.gateway.pool.get("b").available = 10
        self.upstream.dead_sessions_200 = ["s1"]
        _s, body, _ = self.post("/v1/chat/completions", {
            "model": "fake-model",
            "messages": [{"role": "user", "content": "ping"}]})
        payload = json.loads(body)
        self.assertEqual(payload["choices"][0]["message"]["content"], "echo:ping")
        self.assertFalse(self.gateway.pool.get("a").session_valid)

    def test_empty_stream_retry_switches_account(self):
        """An empty stream must not be retried on the same account."""
        self.gateway.pool.get("a").available = 1000
        self.gateway.pool.get("b").available = 10
        self.upstream.empty_stream_sessions = ["s1"]
        _s, body, _ = self.post("/v1/chat/completions", {
            "model": "fake-model", "stream": True,
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertIn("hello", body.decode("utf-8"))
        # both accounts were contacted, i.e. the retry moved on
        tokens = [c["token"] for c in self.upstream.calls
                  if c["path"].endswith("/chat/completions")]
        self.assertIn("s2", tokens)

    def test_empty_stream_is_not_recorded_as_success(self):
        self.upstream.empty_stream_sessions = ["s1", "s2"]
        _s, body, _ = self.post("/v1/chat/completions", {
            "model": "fake-model", "stream": True,
            "messages": [{"role": "user", "content": "hi"}]})
        for acc in self.gateway.pool.accounts:
            self.assertGreater(acc.failures, 0)
        # the client still gets a terminal event, not a silent truncation
        # (an empty stream is not an account fault, so no cooldown)
        self.assertIn(b"[DONE]", body)
        self.assertIn(b'"finish_reason": "error"', body)
        for acc in self.gateway.pool.accounts:
            self.assertFalse(acc.in_cooldown)

    def test_messages_retry_does_not_repeat_message_start(self):
        """One response must carry exactly one message_start.

        The translator used to be started before the upstream said anything,
        so each failed attempt emitted a complete empty message and the retry
        opened a second one inside the same response.
        """
        self.upstream.empty_stream_sessions = ["s1"]
        self.gateway.pool.get("a").available = 1000
        self.gateway.pool.get("b").available = 10
        _s, body, _ = self.post("/v1/messages", {
            "model": "fake-model", "stream": True, "max_tokens": 32,
            "messages": [{"role": "user", "content": "hi"}]})
        names = [e for e, _ in self.sse(body)]
        self.assertEqual(names.count("message_start"), 1)
        self.assertEqual(names.count("message_stop"), 1)
        self.assertIn("content_block_delta", names)

    def test_messages_all_empty_streams_close_with_one_error(self):
        """No attempt produced output: one message_start, an error, one stop."""
        self.upstream.empty_stream_sessions = ["s1", "s2"]
        _s, body, _ = self.post("/v1/messages", {
            "model": "fake-model", "stream": True, "max_tokens": 32,
            "messages": [{"role": "user", "content": "hi"}]})
        events = self.sse(body)
        names = [e for e, _ in events]
        self.assertEqual(names.count("message_start"), 1)
        self.assertEqual(names.count("message_stop"), 1)
        self.assertIn("error", names)

    def test_bad_json_returns_400(self):
        request = urllib.request.Request(self.base + "/v1/chat/completions",
                                         data=b"{not json",
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(ctx.exception.code, 400)
        self.assertIn("JSON", ctx.exception.read().decode("utf-8"))

    def test_unknown_path_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/v1/nope", {})
        self.assertEqual(ctx.exception.code, 404)

    def test_admin_accounts(self):
        _s, payload = self.get("/v1/admin/accounts")
        self.assertEqual(payload["count"], 2)
        self.assertNotIn('"password":', json.dumps(payload))


class ServeCallbackTestCase(unittest.TestCase):
    """``serve(on_server=...)`` hands the bound server to the embedder.

    The Android app uses it to close the old listener before rebinding on
    another interface (loopback <-> LAN); without the callback it cannot tell
    when the socket is actually bound.
    """

    def test_on_server_receives_bound_server(self):
        from loomy2api.server import serve

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        upstream = FakeUpstream()
        base = upstream.start()
        self.addCleanup(upstream.stop)
        cfg = make_config(Path(tmp.name), base)
        cfg["port"] = 0
        seen = {}

        def on_server(httpd):
            seen["server"] = httpd
            # serve_forever() starts right after the callback returns, so stop
            # the loop from a separate thread instead of blocking here.
            threading.Thread(target=httpd.shutdown, daemon=True).start()

        serve(cfg, on_server=on_server)
        self.assertIsNotNone(seen.get("server"))
        self.assertGreater(seen["server"].server_address[1], 0)


class InstanceLockTestCase(unittest.TestCase):
    """Only one gateway may serve an account pool.

    Measured on Windows: ``bind(("0.0.0.0", P))`` and ``bind(("127.0.0.1", P))``
    both succeed, so a second ``serve`` used to come up alongside the first and
    the two pools invalidated each other's sessions mid-request.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "gateway.lock"

    def _locked_by_another_process(self, script: str) -> "subprocess.Popen":
        """Start a child that holds the lock until its stdin closes."""
        child = subprocess.Popen(
            [sys.executable, "-c", script, str(self.path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            cwd=str(Path(__file__).resolve().parent.parent))
        self.addCleanup(lambda: (child.stdin.close(), child.stdout.close(),
                                 child.wait(timeout=10)))
        self.assertEqual(child.stdout.readline().strip(), "locked")
        return child

    def test_second_lock_is_refused_and_reports_the_holder(self):
        from loomy2api.server import AlreadyRunning, InstanceLock

        holder = self._locked_by_another_process(
            "import sys\n"
            "from pathlib import Path\n"
            "from loomy2api.server import InstanceLock\n"
            "lk = InstanceLock(Path(sys.argv[1])); lk.acquire()\n"
            "print('locked', flush=True)\n"
            "sys.stdin.read()\n")

        loser = InstanceLock(self.path)
        with self.assertRaises(AlreadyRunning) as ctx:
            loser.acquire()
        # the loser must be able to name the process holding the lock
        self.assertEqual(loser.holder(), str(holder.pid))
        self.assertEqual(ctx.exception.args[0], str(holder.pid))

    def test_lock_is_reusable_after_release(self):
        from loomy2api.server import InstanceLock

        first = InstanceLock(self.path)
        first.acquire()
        first.release()
        second = InstanceLock(self.path)
        second.acquire()
        self.assertEqual(second.holder(), str(os.getpid()))
        second.release()

    def test_same_process_can_reacquire_for_in_place_restart(self):
        """The Android watchdog restarts the gateway in place.

        ``mobile_main.restart_gateway`` closes the listener and calls ``serve``
        again from the same process, possibly before the previous thread has
        released the lock.  A plain cross-process lock would refuse that and
        the app could never come back.
        """
        from loomy2api.server import InstanceLock

        first = InstanceLock(self.path)
        first.acquire()
        second = InstanceLock(self.path)          # same pid -> allowed
        second.acquire()
        second.release()
        # still held by the first, so a third in-process acquire is fine ...
        third = InstanceLock(self.path)
        third.acquire()
        third.release()
        # ... and a different-looking process would still be refused
        first.release()
        fourth = InstanceLock(self.path)
        fourth.acquire()
        fourth.release()

    def test_holder_ignores_whitespace_and_padding(self):
        from loomy2api.server import InstanceLock

        lock = InstanceLock(self.path)
        self.path.write_bytes(b"12345\n\x00\x00\x00")
        self.assertEqual(lock.holder(), "12345")
        self.path.write_bytes(b"not-a-pid\n")
        self.assertEqual(lock.holder(), "")

    def test_serve_refuses_a_busy_port(self):
        """A second gateway must not silently share a port.

        Two processes on the same port is the failure that broke client
        requests: the kernel happily lets ``0.0.0.0:P`` and ``127.0.0.1:P``
        coexist, so the check has to enumerate listeners rather than probe.
        Needs a real child process — an in-process duplicate is allowed on
        purpose (the Android watchdog restarts the gateway in place).
        """
        from loomy2api.server import AlreadyRunning, serve

        port = free_port()
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import socket,time;"
             "s=socket.socket();"
             "s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,0);"
             f"s.bind(('0.0.0.0',{port}));s.listen(5);"
             "print('bound',flush=True);time.sleep(60)"],
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (holder.kill(), holder.stdout.close(),
                                 holder.wait(timeout=10)))
        self.assertEqual(holder.stdout.readline().strip(), "bound")

        upstream = FakeUpstream()
        base = upstream.start()
        self.addCleanup(upstream.stop)
        cfg = make_config(Path(self.tmp.name), base)
        cfg["host"], cfg["port"] = "127.0.0.1", port

        with self.assertRaises(AlreadyRunning) as ctx:
            serve(cfg)
        self.assertIn(str(port), str(ctx.exception))
        self.assertIn(str(holder.pid), str(ctx.exception))


class AuthTestCase(_Harness, unittest.TestCase):
    """Same gateway, but with a gateway-side API key configured."""
    def setUp(self):
        super().setUp()
        self.gateway.cfg["api_keys"] = ["secret-key"]

    def test_missing_key_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/v1/chat/completions",
                      {"model": "fake-model",
                       "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(ctx.exception.code, 401)

    def test_bearer_key_accepted(self):
        status, _body, _ = self.post(
            "/v1/chat/completions",
            {"model": "fake-model", "messages": [{"role": "user", "content": "x"}]},
            headers={"Authorization": "Bearer secret-key"})
        self.assertEqual(status, 200)

    def test_x_api_key_accepted(self):
        status, _body, _ = self.post(
            "/v1/chat/completions",
            {"model": "fake-model", "messages": [{"role": "user", "content": "x"}]},
            headers={"x-api-key": "secret-key"})
        self.assertEqual(status, 200)

    def test_health_stays_public(self):
        status, _payload = self.get("/health")
        self.assertEqual(status, 200)

    def test_bare_authorization_without_bearer(self):
        """Some clients send the raw key as the Authorization value."""
        status, _body, _ = self.post(
            "/v1/chat/completions",
            {"model": "fake-model", "messages": [{"role": "user", "content": "x"}]},
            headers={"Authorization": "secret-key"})
        self.assertEqual(status, 200)

    def test_query_string_key(self):
        status, _body, _ = self.post(
            "/v1/chat/completions?api_key=secret-key",
            {"model": "fake-model", "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(status, 200)

    def test_key_with_stray_whitespace_or_quotes(self):
        # (a raw newline can't be sent by urllib and no real client does it,
        #  but trailing spaces and quotes from copy-paste are common)
        for raw in (' "secret-key" ', "bearer secret-key ", " secret-key\t"):
            status, _body, _ = self.post(
                "/v1/chat/completions",
                {"model": "fake-model", "messages": [{"role": "user", "content": "x"}]},
                headers={"Authorization": raw})
            self.assertEqual(status, 200, f"rejected: {raw!r}")

    def test_wrong_key_rejected_and_logged(self):
        buf = io.StringIO()
        original = self.gateway.log
        self.gateway.log = lambda msg, **kw: (buf.write(str(msg) + "\n"), original(msg))[1]
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.post("/v1/chat/completions",
                          {"model": "fake-model",
                           "messages": [{"role": "user", "content": "x"}]},
                          headers={"Authorization": "Bearer wrong-key-123456"})
        finally:
            self.gateway.log = original
        self.assertEqual(ctx.exception.code, 401)
        logged = buf.getvalue()
        self.assertIn("[auth] 拒绝", logged)
        self.assertIn("authorization:bearer", logged)
        self.assertIn("wrong-ke…", logged)          # masked, not the full secret
        self.assertNotIn("wrong-key-123456", logged)


class StreamReadTestCase(unittest.TestCase):
    """The upstream SSE reader must not buffer into fixed-size blocks."""

    class _FakeResponse:
        def __init__(self):
            self.calls = []

        def read(self, n=-1):
            self.calls.append(("read", n))
            return b"whole-block"

        def read1(self, n=-1):
            self.calls.append(("read1", n))
            return b"one-token"

    def test_read_chunk_uses_read1(self):
        """``read(n)`` on a BufferedReader blocks until n bytes or EOF, which
        makes token-by-token output arrive as whole sentences.  ``read1``
        returns whatever is available, preserving the upstream cadence."""
        resp = self._FakeResponse()
        chunk = Handler._read_chunk(None, None, resp, 30)
        self.assertEqual(chunk, b"one-token")
        self.assertEqual(resp.calls, [("read1", 4096)])

    def test_read_chunk_falls_back_when_read1_missing(self):
        class _Plain:
            def read(self, n=-1):
                return b"legacy"

        self.assertEqual(Handler._read_chunk(None, None, _Plain(), 30), b"legacy")


if __name__ == "__main__":
    unittest.main()
