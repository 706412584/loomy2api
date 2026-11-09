"""fysms.cc 接码平台客户端（纯标准库）。

平台在 2026-11 换成了一套新的 JSON REST API：老接口
``h5.fysms.cc/index.php?a=...``（表单 + cookie）已整体废弃，现在全部走
``https://fyapi.fysms.com/api/v1``，用 ``Authorization: Bearer <api_token>``
鉴权，登录还必须带图形验证码。

本模块只实现接码注册真正用到的部分，并对外保留老版的五个方法名，这样
:mod:`loomy2api.smsreg` 的调用点不用改：

* :meth:`FysmsClient.login` —— 解图形验证码 + 登录，取回 ``api_token``；
* :meth:`FysmsClient.balance` —— 账户余额（元）；
* :meth:`FysmsClient.get_phone` —— 关键词通道取一个号（**冻结**单价）；
* :meth:`FysmsClient.get_code` —— 轮询订单的短信，返回短信原文；
* :meth:`FysmsClient.blacklist` —— 号码收不到码时提交释放申请（全额退回）。

两个容易踩的点：

* 平台**每次登录都会轮换 `api_token`**，所以一批注册只登录一次、复用 token；
* ``fysms`` 是境内站点，**必须直连**：全局代理 / VPN 会让它超时或返回错误页，
  所以这里刻意不使用 ``config.proxy``（``httpc.request`` 默认就是直连）。
"""

from __future__ import annotations

import base64
import json
import re
import time
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

from .httpc import request as _http

__all__ = ["FysmsClient", "FysmsError", "extract_code", "read_captcha",
           "CODE_PRICE", "CHANNEL_ID", "FYSMS_API", "DEFAULT_KEYWORD"]

#: 新 API 的默认根地址（测试可用 ``base=`` 指向本地假平台）。
FYSMS_API = "https://fyapi.fysms.com"
#: API 版本前缀。
API_PREFIX = "/api/v1"

#: 默认取号通道：通道二（关键词模式）。通道一（id=1）是项目模式，需要
#: ``project_id``；通道二直接传短信关键词，和老逻辑一一对应。
CHANNEL_ID = 6
#: 通道二单价（元），用于“余额还能取几个号”的估算，以及余额不足预判。
CODE_PRICE = 1.18

#: 短信标题（平台按关键词匹配通道）——通道二取号时作为 ``keyword`` 传入。
DEFAULT_KEYWORD = "讯飞"

#: 平台在订单超时后自动释放；老版是 18 次轮询，这里按秒计。
DEFAULT_MAX_POLLS = 18

_UA = "Mozilla/5.0 (Linux; Android 10)"
#: 图形验证码是 SVG，字符以明文 ``<text>`` 节点写在里面，直接正则读即可。
_CAPTCHA_RE = re.compile(r"<text[^>]*>([^<]+)</text>")


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


def read_captcha(image: str) -> str:
    """从验证码图片（base64 SVG）里读出字符。

    平台的验证码是 SVG，字符没有栅格化，``<text>`` 节点的文本就是答案本身，
    因此不需要 OCR；若哪天换成位图，这里必须改为人工 / OCR 兜底。
    """
    payload = str(image or "")
    if payload.startswith("data:"):
        payload = payload.split(",", 1)[-1]
    try:
        svg = base64.b64decode(payload).decode("utf-8", "replace")
    except Exception as exc:                            # noqa: BLE001
        raise FysmsError(f"验证码解码失败：{exc}") from exc
    chars = _CAPTCHA_RE.findall(svg)
    code = "".join(chars).strip()
    if not code:
        raise FysmsError("验证码不是明文 SVG，无法自动识别（需要改为人工输入）")
    return code


class FysmsClient:
    """一个登录后的接码会话。线程不安全，按需创建、用完即弃。"""

    def __init__(self, username: str, password: str, *, timeout: float = 30,
                 base: str = FYSMS_API, channel_id: int = CHANNEL_ID):
        self.username = str(username or "").strip()
        self.password = str(password or "")
        self.timeout = timeout
        #: Only overridden by tests (a local fake platform).
        self.base = str(base or FYSMS_API).rstrip("/")
        self.channel_id = int(channel_id or CHANNEL_ID)
        self.token = ""
        self.userid = ""
        #: 关键词通道取号时用的短信标题；由 ``get_phone`` 记录，``get_code`` 复用。
        self.keyword = DEFAULT_KEYWORD
        #: 最近一次取号得到的订单号（新 API 的交易单元）。
        self.last_order_no = ""
        self.last_phone = ""

    # -- plumbing -------------------------------------------------------

    def _request(self, path: str, *, method: str = "GET",
                 payload: Optional[Dict[str, Any]] = None,
                 query: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = self.base + path
        if query:
            url += ("&" if "?" in url else "?") + urlencode(query)
        headers = {"User-Agent": _UA, "Accept": "application/json"}
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            status, _hdrs, data = _http(url, method=method, headers=headers,
                                        body=body, timeout=self.timeout)
        except OSError as exc:
            raise FysmsError(
                f"接码平台连接失败（需直连，不能走代理/VPN）：{exc}") from exc
        text = data.decode("utf-8", "replace")
        try:
            obj = json.loads(text)
        except Exception as exc:                        # noqa: BLE001
            snippet = text.strip()[:160] or "(空响应)"
            raise FysmsError(
                f"接码平台响应无法解析（HTTP {status}，{path}）：{snippet}") from exc
        if not isinstance(obj, dict):
            raise FysmsError(f"接码平台响应格式异常（{path}）：{text[:160]}")
        return obj

    def _api(self, path: str, *, method: str = "GET",
             payload: Optional[Dict[str, Any]] = None,
             query: Optional[Dict[str, Any]] = None) -> Any:
        """Call an endpoint and return ``data``; raise on any non-zero code.

        The new API always answers HTTP 200 and reports the real outcome in
        ``code`` (``0`` = success), so the body must be inspected, not the
        status.
        """
        obj = self._request(path, method=method, payload=payload, query=query)
        code = obj.get("code")
        if code not in (0, "0"):
            message = str(obj.get("message") or obj)[:200]
            raise FysmsError(f"接码平台错误 {code}（{path}）：{message}")
        return obj.get("data")

    # -- account --------------------------------------------------------

    def login(self) -> str:
        """解图形验证码并登录，返回 ``user_id`` 字符串。"""
        if not self.username or not self.password:
            raise FysmsError("未配置 fysms 账号密码")

        captcha = self._request(f"{API_PREFIX}/captcha/image",
                                query={"scene": "login"}).get("data") or {}
        captcha_key = str(captcha.get("captcha_key") or "")
        if not captcha_key:
            raise FysmsError("接码平台未返回验证码标识")
        captcha_code = read_captcha(captcha.get("image") or "")

        data = self._api(f"{API_PREFIX}/auth/login", method="POST", payload={
            "username": self.username,
            "password": self.password,
            "captcha_key": captcha_key,
            "captcha_code": captcha_code,
        }) or {}
        token = str(data.get("api_token") or "")
        if not token:
            raise FysmsError(f"登录成功但未返回 api_token：{str(data)[:160]}")
        self.token = token
        self.userid = str(data.get("user_id") or "")
        return self.userid

    def balance(self) -> Optional[float]:
        """账户余额（元）；解析失败返回 ``None``。"""
        data = self._api(f"{API_PREFIX}/user/balance") or {}
        try:
            return float(data.get("balance"))
        except (TypeError, ValueError):
            return None

    # -- numbers / SMS --------------------------------------------------

    def get_phone(self, keyword: str = "") -> str:
        """关键词通道取一个号。**会冻结一个号码的费用**，收到码才真正扣掉。

        取号成功时记录 ``last_order_no``，供 :meth:`get_code` /
        :meth:`blacklist` 使用——新 API 的交易单元是订单号，不是手机号。
        """
        self.keyword = str(keyword or self.keyword or DEFAULT_KEYWORD).strip()
        payload = {
            "channel_id": self.channel_id,
            "quantity": 1,
            # 每次取号都要一个唯一幂等键；用 uuid 而不是时间戳，避免并发撞键。
            "idempotency_key": "loomy-" + uuid.uuid4().hex,
            "keyword": self.keyword,
        }
        try:
            data = self._api(f"{API_PREFIX}/sms/fetch", method="POST",
                             payload=payload) or {}
        except FysmsError as exc:
            if "余额" in str(exc) or "金额" in str(exc):
                raise FysmsError(f"fysms 余额不足，无法取号：{exc}") from exc
            raise
        order = data
        orders = data.get("orders") if isinstance(data.get("orders"), list) else []
        if orders and isinstance(orders[0], dict):
            order = orders[0]
        phone = str(order.get("phone") or "").strip()
        self.last_order_no = str(order.get("order_no") or data.get("order_no") or "")
        if not phone:
            raise FysmsError(f"fysms 取号失败：{str(data)[:160]}")
        self.last_phone = phone
        return phone

    #: The platform auto-releases an old order after a while; polling past that
    #: is pointless and only wastes time.  A timeout never costs money — the
    #: fee is only charged when a code is actually returned.
    MAX_POLLS = DEFAULT_MAX_POLLS

    def get_code(self, phone: str = "", keyword: str = "",
                 *, max_wait: int = 150, interval: int = 5,
                 max_polls: int = MAX_POLLS) -> Optional[str]:
        """轮询订单短信，返回短信原文；超时返回 ``None``。

        ``phone`` 仅为兼容老调用签名保留，新 API 用 ``order_no`` 定位订单。
        """
        order_no = self.last_order_no
        if not order_no:
            raise FysmsError("尚未取号（缺少 order_no），无法查询验证码")
        kw = str(keyword or self.keyword or DEFAULT_KEYWORD).strip()
        polls = 0
        waited = 0
        while waited < max_wait and polls < max_polls:
            polls += 1
            try:
                data = self._api(f"{API_PREFIX}/sms/query", method="POST",
                                 payload={"order_no": order_no, "keyword": kw}) or {}
            except FysmsError as exc:
                if "余额" in str(exc):
                    raise FysmsError(f"fysms 余额不足，无法查询验证码：{exc}") from exc
                raise
            content = data.get("content") or data.get("code_text") or data.get("sms")
            if content:
                return str(content)
            if data.get("blocked"):
                raise FysmsError(
                    "短信被运营商/卡商屏蔽，本单已自动退款："
                    + str(data.get("notice") or "")[:120])
            time.sleep(interval)
            waited += interval
        return None

    def blacklist(self, phone: str = "") -> bool:
        """提交释放申请（收不到短信时用），成功返回 ``True``；失败不抛异常。

        新 API 里没有独立的“拉黑”概念，最接近的是释放订单：平台会在几分钟内
        处理并**全额退回**冻结的号码费。

        注意：该接口成功时返回的 **不是** ``code 0``，而是 ``code 409``
        （``已提交释放申请，3 分钟后自动处理``），所以这里不能走 :meth:`_api`。
        """
        order_no = self.last_order_no
        if not order_no:
            return False
        try:
            obj = self._request(f"{API_PREFIX}/sms/release", method="POST",
                                payload={"order_no": order_no})
        except Exception:                                # noqa: BLE001
            return False
        code = obj.get("code")
        message = str(obj.get("message") or "")
        return code in (0, "0", 409, "409") or "释放" in message

    # -- introspection (used by the panel / CLI) ------------------------

    def profile(self) -> Dict[str, Any]:
        """账号资料（含余额、会员等级、点数）。"""
        return self._api(f"{API_PREFIX}/user/profile") or {}

    def channels(self) -> List[Dict[str, Any]]:
        """可用的取号通道列表。"""
        data = self._api(f"{API_PREFIX}/channels") or {}
        rows = data.get("list") if isinstance(data, dict) else data
        return rows if isinstance(rows, list) else []

    def orders(self, *, date: str = "today", status: str = "",
               page: int = 1, page_size: int = 20) -> List[Dict[str, Any]]:
        """订单列表（默认今天的）。"""
        query: Dict[str, Any] = {"date": date, "page": page,
                                 "page_size": page_size}
        if status:
            query["status"] = status
        data = self._api(f"{API_PREFIX}/sms/orders", query=query) or {}
        rows = data.get("list") if isinstance(data, dict) else data
        return rows if isinstance(rows, list) else []
