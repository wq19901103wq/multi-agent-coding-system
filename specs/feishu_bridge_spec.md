# FeishuBridge 规范

> 版本：v1.0  
> 用途：定义飞书 SDK 封装层的接口和卡片格式。

## 依赖

- `lark-oapi` 官方 Python SDK
- 需要配置：`FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_VERIFICATION_TOKEN`、`FEISHU_ENCRYPT_KEY`（可选）

## 接口

```python
class FeishuBridge:
    async def send_text(self, open_id: str, text: str) -> str:
        """发送文本消息，返回 message_id。"""

    async def send_confirmation_card(
        self,
        open_id: str,
        approval_id: str,
        title: str,
        summary: str,
        details: dict,
        options: list[dict],
    ) -> str:
        """发送确认卡片，返回 message_id。"""

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

## 卡片 Button Value Schema

卡片按钮的 `value` 字段必须包含：

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

## 确认卡片示例

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

## 输入卡片示例

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

## 事件解析

### 文本消息

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

### 按钮点击

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

## 用户-项目绑定

### 创建项目时绑定

- 若从飞书创建项目，记录创建者 `open_id` 到 `project_users`

### 后续绑定

- 用户发送 `/bind {project_id}`
- 系统校验 `project_id` 存在
- 创建 `project_users` 记录

### 一个项目多个用户

- `project_users` 表支持多对多
- MVP 阶段主要由创建者操作
- 飞书回调时，用 `open_id` 查询绑定关系，找到 `project_id`
