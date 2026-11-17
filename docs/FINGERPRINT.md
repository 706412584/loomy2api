# 讯飞 Loomy 指纹对齐调查报告

> 仅调查与建议，不改代码。后续是否修改、如何修改由项目负责人决定。
>
> 撰写日期：2026-11-17。基于本机 loomy2api commit f83feef。

## 1. 目的

`loomy2api` 把若干讯飞星火 Loomy 账号的额度暴露为 OpenAI/Anthropic 兼容 API。
上游（`loomyad.xunfei.cn` 模型网关 + `account.xfinfr.com` 账号服务）有
凭证/签名校验，但设备指纹要求相对宽松。本报告盘点当前网关的指纹覆盖度。

## 2. 两条链路的指纹要求

### 2.1 模型网关（`https://loomyad.xunfei.cn/api/v1`）

代码：`loomy2api/upstream.py::build_model_headers`

| 字段 | 当前值 / 策略 | 备注 |
|---|---|---|
| `Authorization` | `Bearer <session>` | 主鉴权 |
| `token` | `<session>` | 上游 `useSessionAuth` 接受任一 |
| `traceparent` | `00-<32hex>-<16hex>-01` | **必需**，缺了上游挂起到超时 |
| `loomy-version` | `0.9.38` | **仅检查存在性**，不校验具体值 |
| `Accept` / `Content-Type` | `application/json` | 标准 |

**关键发现**：
- `loomy-version` 仅检查"存在"，不检查值——意味着 `0.9.38` 写死也没关系，
  上游不会因版本过时而拒绝。
- `traceparent` 是 W3C trace context 标准格式，每次请求随机生成即可，
  上游不关联到设备。

### 2.2 账号服务（`https://account.xfinfr.com`）

代码：`loomy2api/signer.py::build_headers`

| 字段 | 当前值 / 策略 |
|---|---|
| `Authorization` | `account <access_key_id>:<hmac_sha1_signature>` |
| `Date` | RFC 7231 GMT 时间 |
| `Nonce` | UUID v4 |
| `Content-Type` | `application/json` |
| `Content-MD5` | body 非空时计算 |

签名基于 `(method, path, query, body, content_type, date, nonce, custom_headers)`，
使用 access key secret 作 HMAC-SHA1。

access key id/secret 来自客户端 `resources/.env.prod`（用 `LOOMYENC1` magic +
固定 passphrase 简单混淆，已逆向），是**所有客户端共享的同一对**——
不是设备/账号维度。

## 3. 设备/平台指纹现状

代码：`loomy2api/constants.py`

| 字段 | 当前值 | 用途 |
|---|---|---|
| `DEVICE_ID` | `"web"` | 设备标识 |
| `CLIENT_UA` | `"Loomy\|Desktop\|Electron\|macOS"` | 客户端类型串（含平台） |
| `CLIENT_VERSION` | `"1.0.0"` | 客户端版本 |
| `WEB_MODEL_ID` | `"Web"` | web 入口模型 id |
| `CAMPUS_DEVICE_ID_PREFIX` | `"loomy-campus-"` | 任务推广用，由机器指纹派生 |

**关键观察**：
- `CLIENT_UA` 写死 `macOS`，但用户在 Windows 跑。上游**没有因此拒绝**——
  说明平台标识不参与风控。
- `DEVICE_ID = "web"` 表示客户端自带 web 模式，模型网关按 web 通道处理。
- `loomy-campus-fp-<sha256(machineId)>` 这个**真正设备绑定的字段**只出现在
  `/points/activation` 和 `/points/first-login` 两个任务端点，**从不出现在
  chat 请求里**。

## 4. 与灵犀（WPS）的指纹要求对比

| 维度 | 灵犀 | 讯飞 |
|---|---|---|
| 设备 ID 是否参与风控 | **强**（无 `x-kso-device-id` 判为网页端） | **弱**（仅任务端点用，chat 不查） |
| 一设备多账号 | **会顶号** | 未见限制 |
| UA 平台标识 | 必须匹配 | 写死 macOS 也能跑 |
| 请求签名 | 无（Cookie 鉴权） | HMAC-SHA1 |
| trace 头 | 不需要 | **必需**（缺了挂起） |
| 版本号校验 | 严格（UA + x-kso-app-version 一致性） | 仅查存在 |
| 心跳 | 疑似需要 | 未见要求 |

**结论**：讯飞的指纹门槛显著低于灵犀。当前 loomy2api 的指纹策略已经够用，
**没有迫切需要补齐的项**。

## 5. 已知可能过时的项

| 字段 | 当前值 | 风险 |
|---|---|---|
| `DEFAULT_LOOMY_VERSION` | `0.9.38` | 仅查存在，不查值，**无风险**（除非上游改校验逻辑） |
| `CLIENT_VERSION` | `1.0.0` | 同上，仅查存在 |
| `CLIENT_UA` | macOS | 当前未因此失败，但若上游加平台校验会出错 |
| access key id/secret | 客户端共享 | 客户端轮换后需要重新提取（这是设计如此，不是 bug） |

## 6. 待逆向分析清单（可选）

下列项优先级低，因为当前指纹已经够用：

1. **客户端最新版本号**：仅 `loomy-version` 头部的"存在性"校验会不会改成
   "值校验"。看 `resources/.env.prod` 的 `LOOMY_VERSION` 字段。
2. **客户端是否会轮换 access key pair**：当前用 0.9.38 的 .env.prod 提取的
   id/secret，客户端升级后是否变化。需要对比多个版本的 .env.prod。
3. **平台标识（`CLIENT_UA`）**：客户端在 Windows 上是不是 `Loomy|Desktop|Electron|Windows`，
   上游会不会校验。

## 7. 风险点

| 异常类型 | 上游反应 | 当前覆盖度 |
|---|---|---|
| 缺 `traceparent` | 挂起到超时 | 已覆盖 |
| 缺 `loomy-version` | 拒绝 | 已覆盖 |
| 签名错 | 401 | 已覆盖（HMAC-SHA1 + Date + Nonce + Content-MD5） |
| session 过期 | 401 → 重新登录 | 已覆盖（账号池轮换） |
| 同 session 多 IP | 未见限制 | 无需处理 |
| access key 被吊销 | 全池失败 | **风险**：客户端轮换后必须重新提取 |

## 8. 建议方案（**仅建议，待项目负责人决定**）

### 8.1 不必动的事

- `CLIENT_UA` 写死 macOS——实测可用，改了反而可能引入变量。
- `DEFAULT_LOOMY_VERSION = "0.9.38"`——上游不查值。
- 设备 ID 策略——上游不查 chat 请求的设备指纹。

### 8.2 可以考虑的事

- **access key pair 提取自动化**：客户端升级后从新的 .env.prod 重新提取，
  当前是手动流程。可以写个脚本：`find Loomy/resources/.env.prod -exec
  python tools/extract_keys.py`。优先级低，access key 似乎多年没变。

- **traceparent 关联**：当前每次请求生成独立 traceparent，没有跨请求的
  trace id 串联。上游如果用 trace id 做异常检测（同一 trace 跨多账号），
  当前实现是天然规避的。**保持现状**。

### 8.3 不要做的事

- 不要把 `DEVICE_ID` 从 `"web"` 改成机器指纹——上游 web 模式是合法的，
  改成设备指纹反而画蛇添足。
- 不要给 chat 请求加 `loomy-campus-fp-*`——那是任务端点专用的字段，
  加到 chat 反而异常。

## 9. 行动建议

**当前无需行动**。loomy2api 的指纹策略已经足够，讯飞上游对设备/平台
标识的校验明显弱于灵犀。

如果一定要做：
1. 下载最新客户端，对比 `.env.prod` 的 access key pair 是否变化（成本最低）
2. 抓一次真实客户端 chat 请求，对比 header 集合是否有缺失
3. 确认 `CLIENT_UA` 在 Windows 客户端实际是不是 `Loomy|Desktop|Electron|Windows`

是否动手、动哪些，由项目负责人决定。本报告仅列出调查结论。
