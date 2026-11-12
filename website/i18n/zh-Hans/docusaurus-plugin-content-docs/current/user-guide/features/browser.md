---
title: 浏览器自动化
description: 使用 Camofox 进行网页交互、表单填写、快照读取和视觉分析。
sidebar_label: Browser
sidebar_position: 5
---

# 浏览器自动化

Hermes Agent 使用 [Camofox](https://github.com/jo-inc/camofox-browser) 提供浏览器自动化。Camofox 是一个自托管 Node.js 服务，封装了支持指纹伪装的 Firefox 分支 Camoufox。

页面以无障碍树快照表示。交互元素会获得 `@e1`、`@e2` 等引用，Agent 使用这些引用点击元素和输入文本。

## 功能

- 导航、读取快照、点击、输入、滚动、后退和按键操作。
- 从无障碍树中提取图片引用。
- 截取屏幕并使用视觉模型分析。
- 通过 `browser_console(expression=...)` 按需执行 JavaScript。
- 复用持久或由外部应用管理的 Camofox profile。
- 通过经过身份验证的 Camofox evaluate 填充凭据保险库，且不向模型返回秘密值。

Camofox 是唯一的浏览器后端，不再有浏览器引擎或云提供商选择。浏览器工具始终保留在工具 schema 中；未配置 `CAMOFOX_URL` 或服务不可达时，调用会返回配置或连接错误。

## 配置

运行 `hermes tools`，打开 **Browser Automation → Camofox**。在 Node.js 可用时，配置流程会安装 `@askjo/camofox-browser` npm 包并提示输入服务地址。

启动已安装的服务：

```bash
npx @askjo/camofox-browser
```

首次运行会下载 Camoufox 引擎。在 `~/.hermes/.env` 中配置控制 API：

```bash
CAMOFOX_URL=http://localhost:9377
```

对于需要身份验证的远程服务，还需设置：

```bash
CAMOFOX_API_KEY=your-token
```

Hermes 会将该令牌作为 `Authorization: Bearer ...` 发送到 Camofox API。请将它保存在 `.env` 中，不要写入 `config.yaml` 或提示词。

### Docker

也可以直接运行 Camofox，而不安装 npm 包：

```bash
docker run -d \
  --name camofox-browser \
  --restart unless-stopped \
  -p 9377:9377 \
  -e CAMOFOX_PORT=9377 \
  jo-inc/camofox-browser
```

[Camofox 仓库](https://github.com/jo-inc/camofox-browser)还提供 `make up`、`make down`、`make reset`、`make fetch` 以及架构和版本覆盖选项。

### VNC 实时查看

若要查看有头浏览器，请发布 VNC 和 noVNC 端口并启用 VNC：

```bash
mkdir -p ~/.camofox-docker
docker run -d \
  --name camofox-browser \
  --restart unless-stopped \
  -p 9377:9377 \
  -p 6080:6080 \
  -p 5901:5900 \
  -e CAMOFOX_PORT=9377 \
  -e ENABLE_VNC=1 \
  -e VNC_BIND=0.0.0.0 \
  -e VNC_RESOLUTION=1920x1080 \
  -v ~/.camofox-docker:/root/.camofox \
  jo-inc/camofox-browser
```

打开 `http://localhost:6080` 使用 noVNC，或将 VNC 客户端连接到 `localhost:5901`。当 Camofox 健康检查响应公布 VNC 端口时，Hermes 会在导航结果中包含实时查看 URL。

### 从 Docker 打开宿主机回环页面

`CAMOFOX_URL` 指向宿主机发布的控制 API，但 `http://127.0.0.1:3000` 等页面地址必须从 Camofox 容器内部解析。在 `~/.hermes/config.yaml` 中启用回环地址重写：

```yaml
browser:
  camofox:
    rewrite_loopback_urls: true
    loopback_host_alias: host.docker.internal
```

等价的环境设置为 `CAMOFOX_REWRITE_LOOPBACK_URLS=true` 和 `CAMOFOX_LOOPBACK_HOST_ALIAS=host.docker.internal`。

只有主机为 `localhost`、`127.0.0.1` 或 `::1` 的导航 URL 会被重写，`CAMOFOX_URL` 本身不变。Camofox 直接运行在宿主机时请保持禁用。如果容器平台需要，请将 `host.docker.internal` 替换为可达的 LAN 地址。

## 持久会话

默认情况下，每个任务使用随机 Camofox 身份，Cookie 和登录状态不会在 Agent 重启后保留。在 `~/.hermes/config.yaml` 中启用托管持久化：

```yaml
browser:
  camofox:
    managed_persistence: true
```

修改后请重启 Hermes。

启用后，Hermes 会：

- 根据当前 Hermes profile 派生确定性的 `userId`；
- 跨任务和进程启动复用该身份；
- 清理时跳过破坏性的服务端会话删除。

不同 Hermes profile 使用不同 Camofox 身份。Hermes 将 profile 级身份状态保存在 `$HERMES_HOME/browser_auth/camofox/`，实际浏览器 profile 数据保存在 Camofox 服务端。

Camofox 服务端必须实现按 `userId` 持久化 profile。如果服务端始终为每个身份创建全新浏览器上下文，Hermes 无法强制其持久化。若要完全重置 profile，请删除其服务端数据及相应的 Hermes 身份状态。

## 外部管理的会话

当其他应用拥有可见的 Camofox 浏览器时，Hermes 可以共享其身份，而不是创建隔离 profile。

| 配置项 | 环境变量 | 作用 |
|--------|----------|------|
| `browser.camofox.user_id` | `CAMOFOX_USER_ID` | 使用外部管理的 Camofox `userId`。 |
| `browser.camofox.session_key` | `CAMOFOX_SESSION_KEY` | 设置创建或接管标签页时使用的 `listItemId`，默认为每任务值。 |
| `browser.camofox.adopt_existing_tab` | `CAMOFOX_ADOPT_EXISTING_TAB` | 创建新标签页前查找已有标签页。 |

```yaml
browser:
  camofox:
    user_id: shared-camofox
    session_key: visible-tab
    adopt_existing_tab: true
```

设置 `user_id` 后，清理是非破坏性的：Hermes 不调用 `DELETE /sessions/<user_id>`，因此其他应用的标签页、Cookie 和 profile 会保留。

启用标签页接管后，首次浏览器操作会调用 `GET /tabs?userId=<user_id>`。Hermes 优先选择 `listItemId` 与 `session_key` 匹配的最新标签页，然后选择该用户最新的标签页。若没有标签页或请求失败，下次操作会创建标签页。填充 `tab_id` 后不再接管；如果外部应用关闭该标签页，后续调用会返回 Camofox 错误，而不会静默接管其他标签页。

Camofox 不协调多个客户端之间的标签页焦点。请在外部协调所有权，例如在 Hermes 操作标签页时暂停其他应用。

## 可用工具

| 工具 | 用途 |
|------|------|
| `browser_navigate` | 在任务的 Camofox 标签页中打开 URL，并尽可能返回紧凑快照。 |
| `browser_snapshot` | 读取无障碍树和元素引用。 |
| `browser_click` | 点击快照中的元素引用。 |
| `browser_type` | 根据元素引用替换输入框的值。 |
| `browser_scroll` | 向上或向下滚动。 |
| `browser_back` | 在当前标签页中后退。 |
| `browser_press` | 按下 `Enter`、`Escape` 等按键。 |
| `browser_get_images` | 从快照条目提取图片 URL 和替代文本。 |
| `browser_vision` | 截图并使用配置的视觉模型分析。 |
| `browser_console` | 执行提供的 JavaScript 表达式；不支持控制台日志捕获。 |

简单信息检索请使用 `web_search` 或 `web_extract`。需要交互、动态页面状态或截图时再使用浏览器工具。

### 快照限制

超过 `browser.snapshot_threshold`（默认 15,000 字符）的快照会按行边界截断。Hermes 将完整快照保存到 `$HERMES_HOME/cache/web/`，并返回可供 `read_file` 分页读取的路径，不调用辅助 LLM 进行摘要。

### JavaScript 与私网策略

导航和 JavaScript 执行均应用 Hermes 的 URL 安全策略。即使明确允许私有 URL，云元数据端点仍始终被阻止。

`browser.allow_private_urls` 控制策略适用时对私有/内部页面 URL 的访问。JavaScript 执行前会检查 URL 形状的字面量，执行后会在返回内容前再次检查当前页面。

对于不可信页面，可启用更严格的限制：

```yaml
browser:
  restrict_evaluate: true
```

这会在 `browser_console(expression=...)` 中拒绝 Cookie、Web Storage、剪贴板和网络请求等常见敏感原语。由于它也会阻止正常 DOM 提取，因此默认关闭。`browser.allow_unsafe_evaluate` 可显式覆盖此拒绝列表，但不会覆盖 URL 安全策略。

凭据保险库通过经过身份验证的 Camofox evaluate 填充，并将每次填充绑定到保险库条目保存的来源。秘密值仅写入页面，不会出现在工具结果中。请参阅[凭据保险库](credential-vault.md)。

## 会话生命周期

- 每个任务都有独立的 Hermes 到 Camofox 会话映射。
- 临时会话会在清理时关闭。
- 托管和外部管理的会话使用非破坏性清理。
- 非活跃会话会在 `browser.inactivity_timeout`（默认 120 秒）后回收。
- 进程关闭时也会执行清理。

模型无需显式关闭标签页。持久 profile 会有意保留；删除其服务端数据是独立的管理操作。

## 限制

- 交互基于无障碍树，而不是像素坐标。
- Camofox REST API 不提供控制台日志捕获、原始 CDP、原生对话框、HAR 捕获或会话录制接口。
- 大型快照会按上述方式截断。
- 浏览器工具集不提供文件下载。
- 成功调用需要运行中的 Camofox 服务和已配置的 `CAMOFOX_URL`。
