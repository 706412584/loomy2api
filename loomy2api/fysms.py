"""fysms.cc 接码平台客户端（纯标准库）。

只实现接码注册真正用到的部分：

* :meth:`FysmsClient.login` —— 账号密码登录，维护 ``PHPSESSID`` / ``users_id``
  cookie；
* :meth:`FysmsClient.balance` —— 读取账户余额（元）；
* :meth:`FysmsClient.get_phone` —— 老版虚卡取号（``selectrmb_21`` +
  ``getxksjh``，一次取一个号）；
* :meth:`FysmsClient.get_code` —— 轮询短信，返回短信原文；
* :meth:`FysmsClient.blacklist` —— 号码收不到码时拉黑，避免重复扣费。

``fysms`` 是境内站点，**必须直连**：全局代理 / VPN 会让它超时或返回错误页，
所以这里刻意不使用 ``config.proxy``。
"""

from __future__ import annotations

import http.client
import json
import re
import time
from typing import Dict, List, Optional
from urllib.parse import urlencode, urlparse

__all__ = ["FysmsClient", "FysmsError", "extract_code", "CODE_PRICE"]

FYSMS_INDEX = "https://h5.fysms.cc/index.php?m=home&c=Index"
FYSMS_LOGIN = "https://h5.fysms.cc/index.php?m=user&c=Users&a=login"
FYSMS_CENTRE = "https://h5.fysms.cc/index.php?m=user&c=Users&a=centre"
FYSMS_USER_INDEX = "https://h5.fysms.cc/index.php?m=user&c=Users&a=index"
FYSMS_REFERER = "https://h5.fysms.cc/"

#: 老版虚卡每个号码的价格（元），用于“余额还能取几个号”的估算。
CODE_PRICE = 1.6

#: 短信标题（平台按关键词匹配项目），与参考脚本一致。
DEFAULT_KEYWORD = "讯飞"

_UA = "Mozilla/5.0 (Linux; Android 10)"


class FysmsError(RuntimeError):
    """接码平台返回业务错误 / 网络错误。"""


def extract_code(text: str) -> Optional[str]:
    """从短信原文提取 4-6 位验证码。"""
    text = str(text or "")
    match = re.search(r"验证码[^0-9]{0,6}(\d{4,6})", text)
    if match:
        return match.group(1)
    match = re.search(r"(\d{4,6})", text)
    return match.group(1) if match else None


class FysmsClient:
    """一个登录后的接码会话。线程不安全，按需创建、用完即弃。"""

    def __init__(self, username: str, password: str, *, timeout: float = 30,
                 base: str = "https://h5.fysms.cc"):
        self.username = str(username or "").strip()
        self.password = str(password or "")
        self.timeout = timeout
        #: Only overridden by tests (a local fake platform).
        self.base = base.rstrip("/")
        self.userid = ""
        self.cookies: Dict[str, str] = {}

    # -- plumbing -------------------------------------------------------

    def _request(self, url: str, *, method: str = "GET",
                 body: Optional[bytes] = None,
                 headers: Optional[Dict[str, str]] = None):
        url = self.base + url[len("https://h5.fysms.cc"):] \
            if url.startswith("https://h5.fysms.cc") else url
        parsed = urlparse(url)
        if parsed.scheme == "https":
            conn = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443,
                                               timeout=self.timeout)
        elif parsed.scheme == "http":
            conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80,
                                              timeout=self.timeout)
        else:
            raise FysmsError(f"unsupported scheme: {url}")
        hdrs = {"User-Agent": _UA}
        if self.cookies:
            hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        hdrs.update(headers or {})
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        try:
            conn.request(method, path, body=body, headers=hdrs)
            resp = conn.getresponse()
            data = resp.read()
            for raw in resp.headers.get_all("Set-Cookie") or []:
                pair = raw.split(";", 1)[0]
                if "=" not in pair:
                    continue
                key, value = pair.split("=", 1)
                if value == "deleted":
                    self.cookies.pop(key, None)
                else:
                    self.cookies[key] = value
            return resp.status, data
        except OSError as exc:
            raise FysmsError(f"接码平台连接失败（需直连，不能走代理/VPN）：{exc}") from exc
        finally:
            conn.close()

    def _get_text(self, url: str, params: Optional[Dict[str, object]] = None,
                  *, referer: str = FYSMS_REFERER) -> str:
        target = url
        if params:
            sep = "&" if "?" in target else "?"
            target += sep + urlencode(params)
        status, data = self._request(
            target, headers={"X-Requested-With": "XMLHttpRequest",
                             "Referer": referer})
        if status >= 400:
            raise FysmsError(f"接码平台 HTTP {status}")
        return data.decode("utf-8-sig", "replace").strip("\ufeff \n\r\t")

    # -- account --------------------------------------------------------

    def login(self) -> str:
        """登录并返回 ``users_id``。"""
        if not self.username or not self.password:
            raise FysmsError("未配置 fysms 账号密码")
        body = urlencode({
            "username": self.username,
            "password": self.password,
            "referurl": "/index.php?m=user&c=Users&a=centre",
            "website": "website",
        }).encode()
        status, data = self._request(
            FYSMS_LOGIN, method="POST", body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "X-Requested-With": "XMLHttpRequest",
                     "Referer": FYSMS_LOGIN})
        if status >= 400:
            raise FysmsError(f"fysms 登录 HTTP {status}")
        try:
            payload = json.loads(data.decode("utf-8-sig"))
        except Exception as exc:                     # noqa: BLE001
            raise FysmsError(f"fysms 登录返回无法解析：{data[:120]!r}") from exc
        if payload.get("code") != 1:
            raise FysmsError("fysms 登录失败：" +
                             str(payload.get("msg") or payload)[:120])
        uid = self.cookies.get("users_id")
        if not uid:
            raise FysmsError("fysms 登录成功但未取到 users_id")
        self.userid = uid
        return uid

    def balance(self) -> Optional[float]:
        """账户余额（元）；解析失败返回 ``None``。"""
        status, data = self._request(
            FYSMS_USER_INDEX,
            headers={"X-Requested-With": "XMLHttpRequest",
                     "Referer": FYSMS_REFERER})
        if status >= 400:
            raise FysmsError(f"fysms 余额查询 HTTP {status}")
        text = data.decode("utf-8-sig", "replace")
        # The page sometimes arrives as a JSON-encoded string, so the tags are
        # backslash-escaped: ``<b>38<\\/b>``.  Normalise before matching.
        text = text.replace("\\/", "/").replace('\\"', '"')
        # <span><b>38</b></span>\n<span><b>余额</b></span>
        match = re.search(r"<b>([\d.]+)</b>\s*</span>\s*<span>\s*<b>余额", text, re.S)
        if not match:
            match = re.search(r"<b>([\d.]+)</b>.*?余额", text, re.S)
        if not match:
            return None
        try:
            return float(match.group(1))
        except ValueError:
            return None

    # -- numbers / SMS --------------------------------------------------

    def get_phone(self) -> str:
        """老版虚卡取一个号码。每次调用都会扣一个号码的费用。"""
        head = self._get_text(FYSMS_INDEX, {
            "a": "selectrmb_21", "proid": "wasd]1.5",
            "xuanze": 0, "userid": self.userid,
        })
        if head == "buzu":
            raise FysmsError("fysms 余额不足，无法取号")
        if head not in ("1", "success", "ok", "true"):
            # some versions answer JSON; a failure here means no number
            raise FysmsError(f"fysms 取号失败：{head[:80]}")
        raw = self._get_text(FYSMS_INDEX, {"a": "getxksjh"})
        phone = raw.strip('"').strip()
        if not phone.isdigit():
            raise FysmsError(f"fysms 取号失败：{raw[:80]}")
        return phone

    #: The platform auto-releases an old virtual-card number after 18 polls
    #: ("在此等待18次取码后会自动释放").  Polling past that is pointless and
    #: only wastes time; a timeout never costs money — the fee is charged when
    #: a code is actually returned.
    MAX_POLLS = 18

    def get_code(self, phone: str, keyword: str = DEFAULT_KEYWORD,
                 *, max_wait: int = 150, interval: int = 5,
                 max_polls: int = MAX_POLLS) -> Optional[str]:
        """轮询短信，返回短信原文；超时返回 ``None``。"""
        yzuid = str(int(self.userid) * 2 - 5)
        polls = 0
        waited = 0
        while waited < max_wait and polls < max_polls:
            text = self._get_text(FYSMS_INDEX, {
                "a": "selectrmb", "userid": self.userid, "gjz": keyword,
                "phone": phone, "zuoyong": "11", "yzuid": yzuid,
            })
            polls += 1
            text = text.strip().strip('"')
            if text == "buzu":
                raise FysmsError("fysms 余额不足，无法查询验证码")
            # ``1`` (quoted or not) is the platform's "no SMS yet" marker.
            if text and text != "1":
                return text
            time.sleep(interval)
            waited += interval
        return None

    def blacklist(self, phone: str) -> bool:
        """拉黑收不到短信的号码（失败不抛异常）。"""
        try:
            if self._get_text(FYSMS_INDEX, {
                    "a": "releasephone", "phone": phone, "xiangmuid": ""}) == "success":
                return True
        except Exception:                            # noqa: BLE001
            pass
        try:
            text = self._get_text(FYSMS_INDEX, {
                "a": "sf_lh", "parameter1": phone, "lhhssf": 1})
            return text not in ("buzu", "")
        except Exception:                            # noqa: BLE001
            return False
