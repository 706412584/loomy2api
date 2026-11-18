# 讯飞 Loomy 指纹对齐调查报告

> 仅调查与建议，不改代码。后续是否修改、如何修改由项目负责人决定。
>
> 撰写日期：2026-11-17。
> 基线：本机 loomy2api commit f83feef。
> 逆向对象：`D:\app\_reverse\loomy\Loomy-Setup-0.9.39.exe`（477 MB，NSIS），
> 真实客户端 **版本 0.9.39**。

## 1. 目的

`loomy2api` 把若干讯飞星火 Loomy 账号的额度暴露为 OpenAI/Anthropic 兼容 API。
上游（`loomyad.xunfei.cn` 模型网关 + `account.xfinfr.com` 账号服务）有
凭证/签名校验，但设备指纹要求相对宽松。本报告基于**真实客户端 0.9.39
的逆向结果**，盘点当前网关的指纹覆盖度。

## 2. 实测版本信息

来自 `app/resources/package.json` 与 `electron/utils/app-version.js`：

| 字段 | 实测值 |
|---|---|
| 应用版本 | **0.9.39** |
| Electron | `electron.app.getVersion()` = `package.json.version` |
| access key pair 来源 | `resources/.env.prod`（用 `LOOMYENC1` + 固定 passphrase 混淆） |

**`loomy-version` 头的值就是 `package.json.version`**（`electron/utils/app-version.js` 实测）：
```js
export function getLoomyVersion() {
  return readElectronAppVersion() || readPackageVersion()
}
```

## 3. 实测确认：已对齐的指纹

下列字段当前 loomy2api 的值**与真实客户端 0.9.39 完全一致**：

| 字段 | 实测值（真实客户端） | 当前值（loomy2api） | 状态 |
|---|---|---|---|
| `VITE_XFYUN_ACCESS_KEY_ID` | `2thryby66wxi53sk` | `2thryby66wxi53sk` | ✅ |
| `VITE_XFYUN_ACCESS_KEY_SECRET` | `zsak6eadrbawz683wf5r3m2snrwj868r` | 同 | ✅ |
| `VITE_XFYUN_APP_ID` | `GM3LOOMY` | `GM3LOOMY` | ✅ |
| `WEB_MODEL_ID` | `Web` | `Web` | ✅ |
| `WEB_DEVICE_ID` | `web` | `web` | ✅ |
| `CLIENT_VERSION` | `1.0.0`（写死） | `1.0.0` | ✅ |
| `CLIENT_UA` | `Loomy\|Desktop\|Electron\|macOS`（**Windows 上也写死 macOS**） | 同 | ✅ |
| `SESSION_EXPIRE_SECONDS` | `14 * 24 * 3600` | `14 * 24 * 3600` | ✅ |
| `traceparent` 格式 | `00-<32hex>-<16hex>-01` | 同 | ✅ |
| `loomy-version` 头格式 | 当前包版本号 | `0.9.38` | ⚠️ 见 4.1 |

**关键实测发现**：
- 真实客户端在 Windows 上**确实写死 `Loomy|Desktop|Electron|macOS`**——
  不是 loomy2api 抄错，是客户端本来就这么写。
- access key pair 是**所有客户端共享的同一对**，不是设备/账号维度。
- 真实客户端 `version: '1.0.0'` 是写死的，与 `package.json.version` 无关
  （`account-service.js` 第 443 行）。

## 4. 唯一明确差异

### 4.1 ✅ `DEFAULT_LOOMY_VERSION` 已对齐（2026-11-18）

| 项 | 值 |
|---|---|
| 真实客户端 0.9.39 发的 `loomy-version` | `0.9.39` |
| loomy2api 当前写的 `DEFAULT_LOOMY_VERSION` | `0.9.39` |

**风险评估**：低。`loomy2api/upstream.py` 注释写"presence-checked only"——
上游**只检查存在性，不校验具体值**。实测 loomy2api 用 0.9.38 也能跑通。

**已处理**：2026-11-18 把 `DEFAULT_LOOMY_VERSION` 改为 `0.9.39`，
并在 `/chat/completions` 上补齐 `ChatId`/`MsgId`/`TurnId`/`InvokeOrigin`/
`X-Loomy-Request-Purpose`（见 `upstream.py` 的 `_chat_headers`）。

## 5. 客户端有但 loomy2api 未使用的可选请求头

实测 `electron/utils/request-headers.js`，真实客户端在 chat 请求里可能带：

| 头 | 用途 | 当前 loomy2api 状态 |
|---|---|---|
| `ChatId` | 会话 ID | 未发 |
| `MsgId` | 消息 ID | 未发 |
| `TurnId` | 轮 ID（≤256 字符） | 未发 |
| `X-Loomy-Request-Purpose` | 请求目的（17 种枚举） | 部分支持 |
| `InvokeOrigin` | 仅 `user`/`system` | 未发 |
| `X-Loomy-Source: glasses` | 特定 appId（眼镜翻译）才发 | 不需要 |

**实测结论**：这些都是**可选**字段。loomy2api 不发这些头也能跑通——上游
不强制要求。当前无缺陷。

## 6. 不需要做的事

| 项 | 实测结论 |
|---|---|
| 把 `CLIENT_UA` 从 macOS 改成 Windows | **不要改**——真实客户端在 Windows 上也是 macOS |
| 把 `DEVICE_ID` 改成机器指纹 | **不要改**——真实客户端用 `web` |
| 给 chat 请求加 `loomy-campus-fp-*` | **不要加**——那是任务端点专用字段 |
| 升级 access key pair | **不需要**——0.9.39 与 0.9.38 完全相同 |

## 7. 行动建议

**当前无需行动**。loomy2api 的指纹策略已经与真实客户端**高度对齐**：
- 8 个核心字段（access key、app id、UA、device id、model id、session 时长、
  traceparent 格式、签名机制）**全部一致**
- 唯一差异是 `loomy-version` 值 0.9.38 vs 0.9.39，**上游不查值，无影响**

**可选行动**（按优先级）：

| 优先级 | 项 | 工作量 | 必要性 |
|---|---|---|---|
| **可选** | 把 `DEFAULT_LOOMY_VERSION` 改为 `0.9.39` | 1 行 | 低 |
| **可选** | 给 chat 请求加 `InvokeOrigin: user` 头 | ~3 行 | 低 |
| **不做** | 其他字段/行为对齐 | — | 不需要 |

## 8. 实测数据来源

- 真实客户端 0.9.39 安装包：`D:\app\_reverse\loomy\Loomy-Setup-0.9.39.exe`
- 解包：`7z x` → `app-64.7z` → `app/resources/app.asar` → `@electron/asar extract`
- `.env.prod` 解密：`loomy2api/crypto.py::decrypt_client_env`
- 关键文件：
  - `electron/utils/app-version.js` —— `loomy-version` 头生成
  - `electron/utils/request-headers.js` —— 完整 chat 请求头
  - `electron/xfyun/account-service.js` —— 账号服务字段、session 时长、UA
  - `electron/__tests__/request-headers.test.js` —— 字段格式实测断言

是否动手、动哪些，由项目负责人决定。本报告仅列出调查结论。
