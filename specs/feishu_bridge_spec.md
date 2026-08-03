# FeishuBridge 规范

> 版本：v2.0
> 用途：定义飞书 SDK 封装层、事件处理、卡片格式和接入方式。

---

## 1. 接入方式

默认使用 **WebSocket 长连接**（`FEISHU_TRANSPORT=websocket`）。

- 本地开发无需公网域名或内网穿透。
- `lark.ws.Client` 在后台线程运行，自动重连。
- Webhook 回调路由 `/feishu/events` 保留作为 fallback，可通过 `FEISHU_TRANSPORT=webhook` 切换。

---

## 2. 依赖

- `lark-oapi` 官方 Python SDK
- 需要配置：`FEISHU_APP_ID`、`FEISHU_APP_SECRET`
- Webhook 模式下可选：`FEISHU_VERIFICATION_TOKEN`、`FEISHU_ENCRYPT_KEY`

---

## 3. 关键模块

```
mac_system.feishu
├── bridge.py       # 发送消息/卡片、token 缓存、事件解析
├── ws_client.py    # WebSocket 长连接客户端
├── handlers.py     # 文本消息 + 卡片按钮统一处理
├── menu.py         # 交互式菜单卡片
├── card.py         # 审批确认/输入/状态卡片
├── crypto.py       # 签名验证、AES 解密（Webhook 模式用）
└── models.py       # TextMessage、ButtonAction、FeishuEvent
```

---

## 4. FeishuBridge 接口

```python
class FeishuBridge:
    async def send_text(self, open_id: str, text: str) -> str:
        """发送文本消息，返回 message_id。"""

    async def send_content_card(
        self,
        open_id: str,
        title: str,
        content: str,
        page: int = 1,
        total: int = 1,
        color: str = "blue",
    ) -> str:
        """发送内容展示卡片，用于长内容分片。"""

    async def send_confirmation_card(
        self,
        open_id: str,
        approval_id: str,
        title: str,
        summary: str,
        details: dict,
        options: list[dict],
        show_actions: bool = True,
    ) -> str:
        """发送确认卡片，返回 message_id。show_actions=False 可用于只展示内容。"""

    async def send_input_card(
        self,
        open_id: str,
        approval_id: str,
        title: str,
        question: str,
        context: str,
    ) -> str:
        """发送输入卡片（澄清/冲突），返回 message_id。"""

    async def send_status_card(
        self,
        open_id: str,
        project_id: str,
        current_state: str,
        display: str,
        recent_events: list[dict],
    ) -> str:
        """发送/更新状态卡片，返回 message_id。"""

    async def update_card(
        self,
        message_id: str,
        card_content: dict,
    ) -> None:
        """更新已有卡片。"""

    def parse_event(self, payload: dict) -> FeishuEvent:
        """解析飞书回调事件。"""

    def parse_button_action(self, payload: dict) -> ButtonAction:
        """解析卡片按钮点击事件。"""

    def parse_text_message(self, payload: dict) -> TextMessage:
        """解析文本消息事件。"""
```

---

## 5. 卡片 Button Value Schema

### 5.1 审批/决策按钮

```json
{
  "approval_id": "appr_20260707_120100_b2c9",
  "action": "confirm",
  "state": "REQUIREMENT_CONFIRM_PENDING"
}
```

对于 `ARCHITECTURE_CONFIRM_PENDING` 的选项按钮：

```json
{
  "approval_id": "appr_...",
  "action": "select_option",
  "state": "ARCHITECTURE_CONFIRM_PENDING",
  "decision_id": "dec_001",
  "option_key": "handwritten"
}
```

### 5.2 菜单按钮

菜单按钮复用审批卡片的 action 结构，但 `action` 固定为 `"menu"`，实际语义放在 `option_key`：

```json
{
  "action": "menu",
  "option_key": "create:feature",
  "state": ""
}
```

常见 `option_key`：

| option_key | 含义 |
|------------|------|
| `show_menu` | 返回主菜单 |
| `create_prompt` | 提示用户输入自然语言描述以创建项目 |
| `list_projects_status` | 列出我的项目（查看状态） |
| `list_projects_bind` | 列出所有项目（绑定） |
| `intent_confirm:<task_type>` | 用户确认自动推断出的任务类型 |
| `status:<project_id>` | 查看指定项目状态 |
| `bind:<project_id>` | 绑定指定项目 |

---

## 6. 卡片示例

### 6.1 确认卡片

```json
{
  "config": {"wide_screen_mode": true},
  "header": {
    "title": {"tag": "plain_text", "content": "需求确认：config-validator CLI"},
    "template": "blue"
  },
  "elements": [
    {"tag": "div", "text": {"tag": "lark_md", "content": "**摘要**：实现一个 JSON 配置校验器 CLI"}},
    {"tag": "div", "text": {"tag": "lark_md", "content": "**目标**：1. 读取 config.json 2. 校验必填/类型/范围 3. 输出 text/json"}},
    {"tag": "div", "text": {"tag": "lark_md", "content": "**模糊点**：范围校验是否支持 float？"}},
    {"tag": "action", "actions": [
      {"tag": "button", "text": {"tag": "plain_text", "content": "确认"}, "type": "primary", "value": {"approval_id": "appr_...", "action": "confirm", "state": "REQUIREMENT_CONFIRM_PENDING"}},
      {"tag": "button", "text": {"tag": "plain_text", "content": "驳回"}, "type": "danger", "value": {"approval_id": "appr_...", "action": "reject", "state": "REQUIREMENT_CONFIRM_PENDING"}}
    ]}
  ]
}
```

### 6.2 输入卡片

```json
{
  "config": {"wide_screen_mode": true},
  "header": {
    "title": {"tag": "plain_text", "content": "需要澄清：范围校验类型"},
    "template": "orange"
  },
  "elements": [
    {"tag": "div", "text": {"tag": "lark_md", "content": "**问题**：范围校验是否支持 float？"}},
    {"tag": "div", "text": {"tag": "lark_md", "content": "请直接回复你的答案。"}}
  ]
}
```

### 6.3 内容展示卡片

用于长内容分片展示（参考 feishu-connect 的“拆段多发”思路，但使用卡片以支持 markdown 渲染）。

```json
{
  "config": {"wide_screen_mode": true},
  "header": {
    "title": {"tag": "plain_text", "content": "请确认需求理解 — config-validator CLI（2/3）"},
    "template": "blue"
  },
  "elements": [
    {"tag": "div", "text": {"tag": "lark_md", "content": "**目标**\n• 读取 config.json\n• 校验必填/类型/范围"}},
    {"tag": "note", "elements": [{"tag": "plain_text", "content": "第 2/3 页"}]}
  ]
}
```

### 6.4 主菜单卡片

```json
{
  "config": {"wide_screen_mode": true},
  "header": {
    "title": {"tag": "plain_text", "content": "MAC System 助手"},
    "template": "blue"
  },
  "elements": [
    {"tag": "div", "text": {"tag": "lark_md", "请选择你要执行的操作："}},
    {"tag": "action", "actions": [
      {"tag": "button", "text": {"tag": "plain_text", "content": "创建项目"}, "type": "primary", "value": {"action": "menu", "option_key": "create_prompt", "state": ""}},
      {"tag": "button", "text": {"tag": "plain_text", "content": "查看我的项目"}, "value": {"action": "menu", "option_key": "list_projects_status", "state": ""}},
      {"tag": "button", "text": {"tag": "plain_text", "content": "绑定到项目"}, "value": {"action": "menu", "option_key": "list_projects_bind", "state": ""}}
    ]}
  ]
}
```

---

## 7. 长内容分片策略

当确认/输入卡片的摘要或上下文超过长度限制时：

1. 先按 Markdown 章节拆成多张**内容展示卡片**（`build_content_card`）顺序发出。
2. 最后发一张**操作卡片**（`build_confirmation_card` / `build_input_card`），只保留标题、必要元数据和操作按钮。
3. 操作按钮必须放在最后一张卡片上，确保用户读完内容后再做决策。

### 7.1 状态流转卡片

Agent 执行完成后发送，文案避免“已完成：需求分析中”的歧义：

```json
{
  "config": {"wide_screen_mode": true},
  "header": {
    "title": {"tag": "plain_text", "content": "状态流转"},
    "template": "blue"
  },
  "elements": [
    {"tag": "div", "text": {"tag": "lark_md", "content": "**config-validator CLI**"}},
    {"tag": "hr"},
    {"tag": "div", "text": {"tag": "lark_md", "content": "✅ 需求分析 已完成"}},
    {"tag": "div", "text": {"tag": "lark_md", "content": "🔄 进入 方案设计中"}},
    {"tag": "hr"},
    {"tag": "div", "text": {"tag": "lark_md", "content": "需求分析师 → 需求分析师"}},
    {"tag": "div", "text": {"tag": "lark_md", "content": "**结论**：完成需求梳理，输出 JSON 配置校验器 CLI 需求文档"}}
  ]
}
```

### 7.2 操作反馈卡片

用户点击确认/驳回等按钮后，立即发送反馈卡片告知已选择的操作：

```json
{
  "config": {"wide_screen_mode": true},
  "header": {
    "title": {"tag": "plain_text", "content": "操作确认 — config-validator CLI"},
    "template": "green"
  },
  "elements": [
    {"tag": "div", "text": {"tag": "lark_md", "content": "你选择了「确认」\n\n项目继续执行。"}}
  ]
}
```

### 7.3 驳回后 interim 消息

驳回交互按节点类型处理：

- 规划确认：点击驳回后展示意见输入卡，原 approval 在用户提交意见前保持 pending；不能先回退状态再等待自由文本。
- 质量确认/验收：点击驳回后立即进入 REWORK，并发送回退目标选择卡。反馈文本是可选项，不能因为没有反馈而不发 REWORK 卡。
- `/reject appr_xxx feedback` 可同时完成驳回和反馈提交。

---

### 7.4 Agent 实时进度卡片

Agent 执行期间，其实时思考过程（thinking/text）和工具调用（tool_use）应通过飞书卡片实时推送，让用户看到 bot 正在做什么，而非等待数分钟后才有响应。

#### 推送策略

- Agent 每产生一次 assistant 事件（含 thinking/text/tool_use），即通过回调推送到飞书
- 连续 thinking 片段合并为一张内容卡片发送，避免刷屏
- 工具调用（Read/Write/Bash/Glob 等）单独发卡片展示工具名和关键参数
- LLM 返回时间较长（>30s）时，发送"思考中…"提示卡片

#### 卡片格式

**思考过程卡片（thinking/text 合并）：**

```json
{
  "config": {"wide_screen_mode": true},
  "header": {
    "title": {"tag": "plain_text", "content": "Agent 思考中 — {stage}"},
    "template": "blue"
  },
  "elements": [
    {"tag": "div", "text": {"tag": "lark_md", "content": "{thinking_content}"}}
  ]
}
```

**工具调用卡片：**

```json
{
  "config": {"wide_screen_mode": true},
  "header": {
    "title": {"tag": "plain_text", "content": "正在执行 — {tool_name}"},
    "template": "purple"
  },
  "elements": [
    {"tag": "div", "text": {"tag": "lark_md", "content": "**工具**: {tool_name}\n**输入**: {tool_input_preview}"}}
  ]
}
```

#### 实现方式

在 `AgentRuntime` 中增加可选的 `progress_callback` 参数，Orchestrator 在创建 runtime 时传入回调函数。**本功能默认关闭**，通过 `DEBUG_AGENT_PROGRESS=1` 环境变量或项目配置中的 `debug_agent_progress: true` 启用。

```python
# runtime.py
async def execute(
    self, ...,
    progress_callback: Callable[[str, str], Awaitable[None]] | None = None,
) -> AgentRuntimeResult:
    """execute 签名新增 progress_callback.

    progress_callback(event_type, content) 在每次收到 assistant 事件时被调用。
    event_type: "thinking" | "text" | "tool_use"
    content: thinking/text 内容字符串，或 tool_use 的 JSON 序列化
    """
```

```python
# orchestrator.py
result = await self.runtime.execute(
    ...,
    progress_callback=self._make_progress_callback(project, task),
)

def _make_progress_callback(self, project, task):
    """返回一个异步回调，将 agent 进度通过 FeishuBridge 推送。"""
    async def cb(event_type: str, content: str):
        if not self.feishu_bridge:
            return
        # 实现：按 event_type 发送对应卡片
    return cb
```

#### 注意事项

- 进度卡片只用于展示，不影响状态机逻辑
- 回调中不应抛出异常（故障可记录日志但不中断 agent 执行）
- 高频 thinking 片段应合并发送（合并间隔 ≤2s），避免飞书限流

交互流程：
1. 规划确认点驳回 → 输入卡 → 提交 feedback → 原子回退 → 新 Planner 轮次。
2. 质量确认点驳回 → 原子进入 REWORK → 立即发送唯一 REWORK approval 卡。
3. REWORK 卡必须携带 project_id、approval_id、source_state 和可选 recommended_target。
4. 同一 project/state 已有 pending approval 时重发同一 approval，不生成新 ID。

操作反馈卡只能在业务事务成功后发送；若只打开规划驳回输入卡，文案应为“请填写修改意见”，不能宣称“已驳回”。

## 8. 事件解析

### 8.1 文本消息

```json
{
  "event": {
    "message": {
      "message_id": "om_...",
      "chat_type": "p2p",
      "sender": {"sender_id": {"open_id": "ou_..."}},
      "content": "{\"text\":\"确认\"}"
    }
  }
}
```

解析结果：

```python
TextMessage(
    open_id="ou_...",
    message_id="om_...",
    text="确认",
    is_command=False,
)
```

### 8.2 按钮点击

```json
{
  "event": {
    "action": {
      "value": {"approval_id": "appr_...", "action": "confirm", "state": "REQUIREMENT_CONFIRM_PENDING"},
      "tag": "button"
    },
    "operator": {"operator_id": {"open_id": "ou_..."}}
  }
}
```

解析结果：

```python
ButtonAction(
    open_id="ou_...",
    approval_id="appr_...",
    action="confirm",
    state="REQUIREMENT_CONFIRM_PENDING",
)
```

---

## 9. 用户-项目绑定

本节只描述飞书侧交互；完整的聊天、用户、项目路由和权限规则以 `conversation_routing_spec.md` 为准。`project_users` 只表示权限，不再承担“当前消息属于哪个项目”的路由职责。

### 9.1 创建项目时绑定

- 从飞书创建项目时，把创建者 `open_id` 写入 `project_users` 表。

### 9.2 后续绑定

- 用户通过菜单 **绑定到项目** → 选择项目 → 创建 `project_users` 记录。
- 跨 chat 接入或接手他人项目走 admin API（如 `POST /admin/projects/{id}/bind-chat`），不再暴露文本命令。

### 9.3 多用户

- `project_users` 表支持多对多。
- MVP 阶段主要由创建者操作。

### 9.4 多聊天与多项目

- 一个聊天可以关联多个项目，一个项目可以关联多个聊天。
- 自由文本根据 `(chat_id, open_id)` 的 active project 路由，不使用用户最近更新项目。
- 卡片和命令优先使用显式 approval_id/project_id。
- 群聊只处理 @机器人、命令、回复机器人消息和卡片动作。
- 无唯一上下文时返回项目选择卡片，禁止猜测。

---

## 10. 文本指令（Fallback）

| 指令 | 说明 |
|------|------|
| `/whoami` | 返回 open_id |
| `/projects` | 列出当前聊天可用项目 |
| `/switch` | 推项目选择卡，点击切换 active project |
| `/switch <project_id>` | 直接切换当前用户在当前聊天中的 active project |
| `/current` | 查看当前 active project 与绑定目录 |
| `/leave-project` | 清除当前项目上下文 |
| `/status <project_id>` | 查询项目状态 |
| `/create [--type=<task_type>] <描述>` | 创建项目；省略 `--type` 时由 IntentRouter 自动分类 |
| `/input <approval_id> <回答>` | 提交人工输入 |
| `/bind <绝对路径>` | 将当前聊天绑定到已有代码仓库 |
| `/bind` | 推目录候选卡（agent 扫描允许根目录 + 推荐 + 手输兜底）；待实现 |
| `/unbind` | 解绑目录；不影响已有项目 |

---

### 中途文本与命令

- 命令始终进入控制通道，优先级高于普通文本；`/cancel`、`/rollback`、`/pause` 可以使正在运行的 Agent 结果失效。
- Agent 运行期间的自由文本必须回复“已保存、稍后处理”，并写入持久化 pending_user_inputs，不能只存在 WebSocket 处理线程内。
- pending input/confirmation 状态下的自然语言必须绑定明确 approval_id；若用户有多个项目或多个候选审批，先让用户选择，禁止猜测。

### 10.1 Agent 主动提问卡（Grill-Me）

所有 Agent 都可以发送主动提问卡。卡片必须包含 Agent/阶段/轮次、问题与原因、blocking 和默认行为、2–5 个选项及推荐理由、自由文本入口，以及“采用推荐项”“接受默认假设并继续”“暂停/取消”等操作。

payload 必须携带 `question_id`、`approval_id`、`project_id`、`resume_state`、task_id 和 round。用户回答后若 Agent 仍不理解，可以生成下一张卡继续 grill-me；旧卡更新为已回答，新卡使用新 question_id，数据库中仍只有一个当前 pending approval。

同一问题投递到多个聊天时，任一合法回答成功后其他卡片更新为已处理；回答必须按 question/approval 显式路由，不受后来 `/use` 切换影响。
- 用户回复问题卡片时，文本事件的 `parent_id` 必须映射到发送卡片时记录的 `Approval.payload.reply_message_ids`；该路径优先于 active project。
- 卡片按钮、slash command 和自动确认使用同一个原子 resolve approval 服务，保证权限、幂等和状态一致性。

## 11. 调试

- 服务启动后检查日志是否有 `connected to wss://...`。
- 收到事件会打印：
  - `[Feishu WS] received text message open_id=... text=...`
  - `[Feishu WS] received card action open_id=... approval_id=... action=...`
- 测试环境下 WS Client 不会启动（检测 `PYTEST_CURRENT_TEST`）。

---

## 12. 扩展指南

新增菜单按钮：

1. 在 `src/mac_system/feishu/menu.py` 添加按钮，option_key 遵循 `命名空间:参数` 风格。
2. 在 `src/mac_system/feishu/handlers.py` 的 `_handle_menu_action()` 中处理。

新增自然语言创建后的确认卡片：

1. 在 `src/mac_system/feishu/menu.py` 添加 `build_intent_confirm_card(task_type)`。
2. 在 `src/mac_system/feishu/handlers.py` 的 `_route_and_create()` 中根据置信度发送该卡片，并在 `_handle_menu_action()` 处理 `intent_confirm:<task_type>`。

新增审批按钮类型：

1. 在 `src/mac_system/feishu/card.py` 调整按钮 `value`。
2. 在 `src/mac_system/feishu/handlers.py` 的 `handle_card_action()` 中处理新的 `action.action`。
