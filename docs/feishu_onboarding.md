# Feishu 机器人接入指南（Agent 入职版）

> 目标：让新加入的 Agent 在 5 分钟内理解飞书机器人是怎么工作的，以及如何调试、扩展它。

---

## 1. 一句话介绍

MAC System 通过 **飞书官方 `lark-oapi` SDK 的 WebSocket 长连接** 接收用户消息和卡片按钮事件。

- 不需要公网域名、不需要 ngrok/natapp。
- 用户在飞书里点按钮即可创建项目、查看状态、审批流转。
- 本地启动 API 服务后，WS Client 会在后台自动连上飞书。

---

## 2. 架构说明

```
用户（飞书 App）
    │ 发消息 / 点卡片
    ▼
飞书开放平台
    │ 通过长连接推送事件
    ▼
lark.ws.Client（后台线程）
    │ 解析为 P2ImMessageReceiveV1 / P2CardActionTrigger
    ▼
mac_system.feishu.ws_client
    │ 调度到共享 handlers
    ▼
mac_system.feishu.handlers
    │ 调用 ProjectService / AgentOrchestrator / FeishuBridge
    ▼
项目创建、状态查询、审批响应、Claude Code 执行
```

Webhook 路由 `/feishu/events` 仍然保留：
- 默认走 **WebSocket 长连接**（`FEISHU_TRANSPORT=websocket`）。
- 把环境变量改成 `FEISHU_TRANSPORT=webhook` 可切回 HTTP 回调模式。

---

## 3. 关键文件

| 文件 | 职责 |
|------|------|
| `src/mac_system/feishu/ws_client.py` | WebSocket 长连接客户端，后台线程启动/重连 |
| `src/mac_system/feishu/handlers.py` | 文本消息 + 卡片按钮的统一处理逻辑 |
| `src/mac_system/feishu/menu.py` | 交互式菜单卡片（主菜单、选类型、选项目） |
| `src/mac_system/feishu/bridge.py` | 发送文本/卡片、token 缓存、事件解析 |
| `src/mac_system/feishu/card.py` | 审批确认卡片、输入卡片、状态卡片 |
| `src/mac_system/feishu/models.py` | `TextMessage`、`ButtonAction`、`FeishuEvent` 数据模型 |
| `src/mac_system/api/feishu_routes.py` | Webhook  fallback 路由（复用 handlers） |
| `src/mac_system/api/main.py` | FastAPI lifespan 中启动/停止 WS Client |
| `src/mac_system/config.py` | `FEISHU_TRANSPORT` 等配置 |
| `.env` | 飞书 AppID / Secret / Transport |

---

## 4. 本地开发流程

### 4.1 确认环境变量

`.env` 里至少要有：

```env
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_TRANSPORT=websocket
```

`FEISHU_VERIFICATION_TOKEN` 和 `FEISHU_ENCRYPT_KEY` 在 WebSocket 模式下不需要填。

### 4.2 启动服务

```bash
cd /Users/yihanwang/multi-agent-coding-system/.worktrees/feat-core
.venv/bin/uvicorn mac_system.api.main:app --host 127.0.0.1 --port 8000
```

看到下面日志即表示连上飞书：

```
[Lark] [INFO] connected to wss://msg-frontier.feishu.cn/ws/v2?...
```

### 4.3 验证

在飞书里给机器人发 `/whoami`，应返回你的 `open_id`。

---

## 5. 飞书后台配置清单

1. **创建企业自建应用** → 记录 `App ID` 和 `App Secret`。
2. **应用能力 → 机器人** → 启用。
3. **权限管理** 至少开通：
   - `im:message`
   - `im:message:send_as_bot`
   - `im:message.p2p_msg:readonly`
   - `im:message.group_at_msg:readonly`（如需群聊 @）
4. **事件与回调**：
   - 订阅方式：**使用长连接接收事件**
   - 添加事件：`im.message.receive_v1`、`card.action.trigger`
5. **版本管理与发布** → 创建版本并上线。

> ⚠️ 不发布的话，所有权限/事件配置都不会生效。

---

## 6. 用户交互流程

### 6.1 主菜单

用户发任意消息 → 收到主菜单卡片：

- **创建项目**
- **查看我的项目**
- **绑定到项目**

### 6.2 创建项目

1. 点 **创建项目** → 选类型（feature / bug_fix / documentation）。
2. 选完类型后，机器人提示“请直接回复一句话描述”。
3. 用户回复描述 → 创建项目并自动 `orchestrator.tick()` 驱动状态机。

### 6.3 查看状态

1. 点 **查看我的项目** → 列出该用户已绑定的项目。
2. 点项目 → 收到文本摘要 + 状态卡片。

### 6.4 绑定项目

1. 点 **绑定到项目** → 列出所有项目。
2. 点项目 → 绑定当前用户。

### 6.5 审批/输入

状态机走到需要人工确认或输入的节点时，机器人会自动发送：

- **确认卡片**：用户点“确认/驳回/选项”即可。
- **输入卡片**：提示用户用 `/input <approval_id> <回答>` 提交（下一步会改成表单卡片）。

---

## 7. 文本指令（Fallback）

卡片点不了时，仍然可以用文本：

| 指令 | 说明 |
|------|------|
| `/whoami` | 返回 open_id |
| `/bind <project_id>` | 绑定到项目 |
| `/status <project_id>` | 查询项目状态 |
| `/create <task_type> <描述>` | 创建项目 |
| `/input <approval_id> <回答>` | 提交人工输入 |

---

## 8. 新增指令 / 卡片开发指南

### 8.1 新增一个文本命令

在 `src/mac_system/feishu/handlers.py` 的 `handle_text_message()` 里加分支：

```python
if text.startswith("/mycommand "):
    await _my_command(ctx, open_id, text[len("/mycommand "):].strip())
    return
```

### 8.2 新增一个菜单按钮

1. 在 `src/mac_system/feishu/menu.py` 里改对应卡片，加一个按钮：

```python
_menu_button("我的按钮", "my_action")
```

2. 在 `src/mac_system/feishu/handlers.py` 的 `_handle_menu_action()` 里处理：

```python
if option == "my_action":
    await ctx.bridge.send_text(open_id, "你点击了我的按钮")
    return
```

### 8.3 新增审批卡片按钮类型

如果审批节点需要新的决策按钮，在 `src/mac_system/feishu/card.py` 的 `build_confirmation_card()` 里调整按钮 `value`，然后在 `handle_card_action()` 里处理新的 `action.action`。

---

## 9. 常见问题

### 机器人已发布但发消息没反应

1. 看本地日志有没有 `connected to wss://...`。
2. 如果没有，检查 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`。
3. 如果有连接但没收到事件，检查飞书后台：
   - 订阅方式是否为“长连接”
   - 是否勾选了 `im.message.receive_v1`
   - 应用是否已发布上线

### 用户点卡片按钮报错“出错了，请稍后重试”

1. 确认 `card.action.trigger` 事件已订阅。
2. 看本地日志有没有 `[Feishu WS] received card action`。
3. 如果按钮是菜单按钮，确认 `value.action == "menu"`。

### 服务关了但 WS 线程还在

`lark.ws.Client` 只暴露 `start()`，没有 `stop()`。 lifespan 里的 `stop()` 只是阻止断线重连；进程退出时 daemon 线程会结束。

### 不想在测试时连真实飞书

测试环境会自动跳过 WS 启动（通过检测 `PYTEST_CURRENT_TEST` 环境变量）。

---

## 10. 相关 Spec

详细接口定义见 `specs/feishu_bridge_spec.md`。
