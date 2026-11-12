# QQ Bot

通过**官方 QQ Bot API（v2）**将 Hermes 接入 QQ——支持私聊（C2C）、群组 @-提及、频道及直接消息，。

## 概述

QQ Bot 适配器使用[官方 QQ Bot API](https://bot.q.qq.com/wiki/develop/api-v2/) 实现以下功能：

- 通过持久 **WebSocket** 连接至 QQ Gateway（网关）接收消息
- 通过 **REST API** 发送文本和 Markdown 回复
- 下载并处理图片、语音消息及文件附件
- 在 QQ 为语音消息提供其内置 `asr_refer_text` 文本时将其用作消息内容

## 前提条件

1. **QQ Bot 应用** — 在 [q.qq.com](https://q.qq.com) 注册：
   - 创建新应用并记录您的 **App ID** 和 **App Secret**
   - 启用所需 intent（意图）：C2C 消息、群组 @-消息、频道消息
   - 在沙盒模式下配置机器人以进行测试，或发布至生产环境

2. **依赖项** — 适配器需要 `aiohttp` 和 `httpx`：
   ```bash
   pip install aiohttp httpx
   ```

## 配置

### 交互式设置

```bash
hermes gateway setup
```

从平台列表中选择 **QQ Bot** 并按提示操作。

### 手动配置

在 `~/.hermes/.env` 中设置所需环境变量：

```bash
QQ_APP_ID=your-app-id
QQ_CLIENT_SECRET=your-app-secret
```

## 环境变量

| 变量 | 描述 | 默认值 |
|---|---|---|
| `QQ_APP_ID` | QQ Bot App ID（必填） | — |
| `QQ_CLIENT_SECRET` | QQ Bot App Secret（必填） | — |
| `QQ_ALLOWED_USERS` | 允许私聊访问的用户 OpenID 列表（逗号分隔） | 开放（所有用户） |
| `QQ_GROUP_ALLOWED_USERS` | 允许群组访问的群组 OpenID 列表（逗号分隔） | — |
| `QQ_ALLOW_ALL_USERS` | 设为 `true` 以允许所有私聊 | `false` |
| `QQ_PORTAL_HOST` | 覆盖 QQ portal 主机（沙盒路由设为 `sandbox.q.qq.com`） | `q.qq.com` |

## 高级配置

如需精细控制，可在 `~/.hermes/config.yaml` 中添加平台设置：

```yaml
platforms:
  qqbot:
    enabled: true
    extra:
      app_id: "your-app-id"
      client_secret: "your-secret"
      markdown_support: true       # enable QQ markdown (msg_type 2). Config-only; no env-var equivalent.
      dm_policy: "open"          # open | allowlist | disabled
      allow_from:
        - "user_openid_1"
      group_policy: "open"       # open | allowlist | disabled
      group_allow_from:
        - "group_openid_1"
```

## 语音消息

语音消息以音频文件形式下载。当 QQ 自身的语音识别返回文本（附件上的 `asr_refer_text`）时，该文本用作消息内容；否则音频作为文件附件说明交给 Agent，由 Agent 自行转录或处理。

## 故障排查

### 机器人立即断开连接（快速断连）

通常原因如下：
- **App ID / Secret 无效** — 在 q.qq.com 仔细核对您的凭据
- **缺少权限** — 确保机器人已启用所需 intent
- **仅限沙盒的机器人** — 若机器人处于沙盒模式，只能接收来自 QQ 沙盒测试频道的消息

### 消息未送达

- 在 q.qq.com 验证机器人的 **intent** 是否已启用
- 若私聊访问受限，检查 `QQ_ALLOWED_USERS`
- 对于群组消息，确保机器人被 **@提及**（群组策略可能需要加入白名单）

### 连接错误

- 确保已安装 `aiohttp` 和 `httpx`：`pip install aiohttp httpx`
- 检查与 `api.sgroup.qq.com` 及 WebSocket gateway 的网络连通性
- 查看 gateway 日志以获取详细错误信息和重连行为