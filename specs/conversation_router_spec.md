# 对话协调层规范（v1）

> 版本：v2.1  
> 状态：Phase 1–3 核心代码已实现，真实飞书分支 E2E 待完成  
> 目标：让用户在飞书中通过自然语言完成调研、讨论、代码检查、草案生成、进度查询和执行授权；保留现有 Project、Approval、Orchestrator 与统一交付状态机。

> 边界：本文件描述“自然语言理解与动作授权”；`conversation_routing_spec.md` 描述“用户/聊天/项目的确定性寻址与权限”，两者不是重复实现。

## 1. 设计结论

对话协调层采用**受约束的动态路由**，不采用 Agent 任意生成节点的完全动态工作流。

```text
飞书消息/卡片
  → Message Ingress（去重、身份、引用、命令）
  → ConversationSession（最近对话、摘要、当前项目）
  → Coordinator Agent（理解意图并提出动作）
  → Policy Gate（目标、权限、副作用、状态校验）
      ├─ 无副作用：调用预定义能力并回复
      └─ 有副作用：创建 PendingAction/使用项目内 Approval，等待确认
  → Delivery Gateway
  → 现有 Orchestrator + delivery 状态机
  → 回执与主动通知
```

核心原则：

1. 原始上下文用于理解，稳定 ID 用于执行寻址。
2. Agent 只返回回答和动作建议，不直接写数据库、不直接推进状态机。
3. 对话可以动态选择能力，但能力集合、权限和副作用边界由代码固定。
4. 无明确授权不得创建项目、修改工作区或控制项目。
5. 项目创建后继续使用现有 Project、Approval、PendingUserInput、FeedbackPacket 和 delivery 状态机。
6. v1 不引入 Topic 自动聚类、Draft 独立表、任意动态图生成或动态裁剪交付状态机。

## 2. 用户场景

v1 必须支持以下连续对话：

```text
用户：仔细看下当前项目，看下还存在什么问题
系统：只读检查代码、文档和测试，返回 Findings；不创建项目

用户：OK，你帮忙修复
系统：引用上一轮 Findings，展示准备修复的范围并等待确认

用户：全部确认
系统：仅当存在唯一 pending action 时确认；否则展示选择，不猜测

用户：先讨论大方向，再讨论细节
系统：记录交互偏好并继续讨论，不进入交付

用户：那就按刚才的方案做
系统：只有能唯一定位当前方案时才生成执行确认；确认后创建 Project
```

其他必备场景：

- 无项目时进行产品调研、技术比较和方向咨询。
- 有项目且 Agent 正在运行时查询进度或询问原因，不把咨询误记为范围变更。
- 用户明确纠正、变更范围、暂停、恢复或取消时，通过确定性控制路径处理。
- 群聊存在多个项目或多个 pending action 时，先选择目标。
- 重复点击、旧卡片和消息重试不得重复创建项目或重复推进状态。

## 3. 范围与非目标

### 3.1 v1 范围

- 一个飞书 chat 对应一个共享 `ConversationSession`。
- 现有 chat-user context 继续保存每个用户的 active project；权限按 `open_id` 校验。
- Session 保存最近对话、滚动摘要、最近产物引用和 pending action 引用。
- Coordinator 在预定义能力之间动态路由。
- 项目创建前的有副作用动作使用会话级 `PendingAction`。
- 项目创建后的确认继续使用现有 `Approval`。
- 内容卡片、决策卡片、即时回执和状态回执分离。

### 3.2 v1 非目标

- 不自动识别并持久化多个 Topic。
- 不实现 Draft/Artifact 多版本管理系统。
- 不跨聊天自动合并上下文。
- 不让 Agent 任意创建工作流节点或直接调用 Orchestrator。
- 不同时允许两个 mutating Project 修改同一绑定目录。
- 不改变现有 delivery 状态机和 workflow profile 的核心语义。
- 不引入新的工作流框架；先复用现有服务、数据库与 Orchestrator。

## 4. 组件职责

### 4.1 Message Ingress

`handle_text_message` 只负责：

1. 按 event/message id 去重。
2. 解析发送者、chat、群聊 @、`parent_id` 和卡片 payload。
3. 保留现有明确命令和问题卡回复。
4. 将其余自由文本交给 ConversationCoordinator。

Handler 不再通过关键词直接决定 correction/scope_change，也不在存在 active project 时无条件创建 PendingUserInput。

### 4.2 ConversationCoordinator

负责：

- 加载 Session、最近消息、摘要、active project 和 pending interaction。
- 调用 Coordinator Agent 并校验独立输出 schema。
- 对无副作用动作调用预定义能力。
- 对有副作用动作交给 Policy Gate。
- 保存本轮输入、回复、摘要和动作结果。

Coordinator 不负责：

- 直接修改 Project/Approval 状态。
- 直接修改正式工作区。
- 直接选择存在歧义的项目或审批。
- 直接调用 `orchestrator.tick()`。

### 4.3 预定义能力

v1 允许动态选择以下能力：

| 能力 | 用途 | 默认副作用 |
|---|---|---|
| `answer` | 解释、讨论、比较、推荐下一步 | 无 |
| `inspect_workspace` | 读取代码、文档、配置，执行受限诊断 | 只读/本地诊断 |
| `research` | 外部资料调研并返回来源 | 外部读取 |
| `query_status` | 查询项目、任务、审批和阻塞 | 无 |
| `create_draft` | 在对话中生成需求/方案/计划草案 | 仅消息/Session |
| `request_clarification` | 给出不超过三个候选项或自由输入 | 无 |
| `propose_start_delivery` | 形成项目创建前的待确认动作 | 创建 PendingAction |
| `resolve_pending_action` | 确认或拒绝唯一会话级待确认动作 | 消费 PendingAction |
| `propose_project_control` | 建议暂停、恢复、取消或切换 | 创建/定位控制动作 |
| `propose_approval_resolution` | 建议确认、驳回或选择项目内审批 | 定位现有 Approval |
| `route_project_input` | 将确认影响执行的输入送入项目 | PendingUserInput/FeedbackPacket |

`inspect_workspace` 的“只读”不等于任意命令：允许读取仓库和执行明确配置的本地测试/静态检查；禁止生产连接、部署、Git 写操作和工作区外扫描。

### 4.4 Policy Gate

Policy Gate 使用确定性代码检查：

- 目标 Project、Approval、PendingAction 是否唯一且有效。
- 用户是否具有所需角色和 chat/project 访问权限。
- 当前状态是否允许该动作。
- 副作用等级是否已获授权。
- 绑定目录是否存在并允许写入。
- 同一目录是否已有 mutating Project。
- 幂等键是否已执行。

Coordinator 的高置信度不能替代上述校验。

### 4.5 Delivery Gateway

所有自然语言和卡片入口共用 Delivery Gateway：

- `start_delivery(pending_action_id, actor)`：校验并消费 PendingAction，创建 Project，调用现有启动路径。
- `resolve_project_approval(approval_id, decision, actor)`：调用现有审批路径。
- `control_project(project_id, action, actor)`：暂停、恢复或取消。
- `submit_project_input(project_id, input, actor)`：只接收已确认影响当前交付的输入。

Gateway 必须幂等；不得在 Handler 和卡片回调中复制业务逻辑。

## 5. 会话状态

### 5.1 ConversationSession

v1 新增一个 chat 级会话状态，建议字段：

```text
id
chat_id                 unique
summary                 滚动摘要，不具备授权效力
recent_turns            最近 10~20 轮 JSON
active_project_id       可空；共享默认项目
latest_artifact         最近 Findings/Draft 的轻量引用 JSON
pending_action_id       可空
status                  active / closed
version                 乐观并发版本
created_at
updated_at
```

每个用户的 active project、角色和个人偏好继续使用现有 chat-user/project-user 关系，不把权限写入共享 Session。

### 5.2 PendingAction

现有 `Approval.project_id` 是必填外键，只适用于项目创建后的审批。项目创建前不能复用它，否则会破坏“确认后才创建 Project”的边界。

v1 新增轻量会话级 PendingAction：

```text
id
session_id
action_type             start_delivery / project_control
target_project_id       可空
payload                 冻结的目标、范围、权限、验收标准
source_message_ids      原始上下文引用
status                  pending / resolved / rejected / expired
idempotency_key         unique
created_by
resolved_by             可空
created_at
resolved_at             可空
expires_at              可空
```

它只表示一个待确认动作，不承担 Topic、Draft 或通用工作流状态。

`start_delivery` payload 至少包含：

```json
{
  "task_type": "feature",
  "objective": "实现自然语言对话协调层",
  "scope": ["自由文本路由", "操作回执"],
  "out_of_scope": ["自动部署", "重写交付状态机"],
  "permissions": ["workspace_write", "local_diagnostics"],
  "acceptance_criteria": ["咨询不会创建项目", "确认后只创建一次项目"],
  "bound_directory": "/allowed/repository"
}
```

PendingAction 确认后 payload 冻结；范围实质变化必须创建新版本/新动作，旧动作标记 expired。

## 6. Coordinator 输出契约

Coordinator 使用独立的 `specs/schemas/conversation_coordinator_output.json`，不得复用 `project_manager_output.json`。

输出同时表达四个维度：

- `intent`：用户要做什么。
- `effect`：最多允许产生什么副作用。
- `commitment`：用户是否已经明确授权。
- `target`：动作指向什么对象。

允许的 effect：

```text
none
read_only
local_diagnostics
conversation_write
workspace_write
workflow_control
external_effect
```

允许的 commitment：

```text
exploring
considering
preferred
authorized
ambiguous
```

Agent 的 `authorized` 只是语义判断。真正执行仍需 Policy Gate 找到唯一目标并验证授权对象。

## 7. 消息路由顺序

```text
1. event/message 去重
2. parent_id 对应的 QuestionInteraction/卡片回复
3. 卡片 action（携带稳定 target id）
4. 明确 slash command
5. 群聊无 @ 且非系统回调 → 忽略
6. 加载 ConversationSession、用户 active project、pending action/approval
7. Coordinator Agent 理解其余自由文本
8. Policy Gate 校验 proposed_action
9. 执行无副作用能力，或创建/展示待确认动作
10. 保存回执与 Session 摘要
```

特殊规则：

- PendingIntent 不能无条件消费任意非命令文本；只有确定在回答当前创建问题时才续接。
- 最近 rejected project 不能无条件劫持下一条消息。
- Agent 运行中也先经过 Coordinator；只有语义确认影响交付后才创建 PendingUserInput。
- 无 active project 时允许自由调研和讨论，不默认返回 `/help`，也不自动创建项目。
- 多个目标候选且动作有副作用时必须展示选择卡。

## 8. 授权与副作用

### 8.1 无需确认

- 普通回答、解释和方案比较。
- 读取已授权工作区中的代码和文档。
- 查询项目状态。
- 生成仅存在于对话中的草案。
- 配置允许的本地静态检查和隔离测试。

### 8.2 需要一次明确确认

- 创建正式 Project。
- 修改代码、文档或配置。
- 将新范围注入正在执行的项目。
- 暂停、恢复或取消项目（明确命令/目标卡片本身可作为确认）。

### 8.3 需要独立高风险确认

- Git push、发布和部署。
- 删除、覆盖或迁移数据。
- 访问生产环境或产生外部写操作。
- 扩大到原 PendingAction 未授权的目录或系统。

用户对某个明确卡片/动作回复“按这个做”可完成一次确认，不得再重复确认；孤立的“OK”“全部确认”“开始吧”在目标不唯一时必须消歧。

## 9. 执行期间对话

| 用户输入 | Coordinator 处理 | 是否影响 delivery |
|---|---|---|
| “现在做到哪了” | `query_status` 并立即回复 | 否 |
| “为什么选择 SQLite” | 解释已有决策 | 否 |
| “如果换 PostgreSQL 会怎样” | 假设性比较 | 否 |
| “改成 PostgreSQL” | 形成范围变更确认 | 确认后是 |
| “刚才说错了，不需要登录” | correction，建议安全暂停/REWORK | 是 |
| “先暂停” | 定位唯一项目并控制 | 是 |

项目输入分类不得仅依赖“改、增加、不要”等关键词。

## 10. 手机交互与回执

交互使用五类消息：内容卡片、决策卡片、输入卡片、状态卡片、结果回执。

规则：

1. 内容卡片和决策卡片分离。
2. 一张决策卡只做一个决定，最多三个主要选项，推荐项排第一并说明影响。
3. 能选择的不要求输入；选项不能覆盖真实答案时保留“其他/补充”。
4. 点击后先给“已收到”回执，真实状态改变后再给“已完成/已开始”回执。
5. 普通过程更新同一状态卡；需要决策、阻塞、失败和完成时主动发新消息。
6. 卡片完成后变为只读结果；旧卡片和重复点击返回“已处理/已过期”，不得抛错。
7. UI 展示用户语言，不暴露 approval id、状态机事件和内部 Agent 名称。

默认通知：只主动发送需要用户操作、执行开始、阻塞/失败、重大阶段完成和最终结果。工具调用和内部重试不推送。

## 11. 幂等、并发与恢复

- 每个入站 event/message 使用稳定幂等键。
- PendingAction 创建键至少包含 session、action type、目标和来源消息。
- PendingAction `pending → resolved/rejected/expired` 使用事务或 CAS，只允许一次。
- 同一个绑定目录同时只允许一个 mutating Project；只读调研可并发。
- 服务重启后从数据库恢复 Session、PendingAction、Project 和 Approval。
- Agent 输出成功但回执失败时不得重新执行动作，只重试通知。
- Coordinator schema 失败自动重试一次；仍失败时提供安全菜单，不默认执行。
- 命令和现有卡片路径在 Coordinator 不可用时继续工作。

## 12. 验收标准

- **AC-COORD-001**：无 active project 时，“帮我分析现有代码”执行只读检查，不创建 Project。
- **AC-COORD-002**：“如果改成 PostgreSQL会怎样”不会创建 PendingUserInput、FeedbackPacket 或 REWORK。
- **AC-COORD-003**：检查结果后说“帮我修复”，系统展示唯一范围的 PendingAction；确认前不修改代码。
- **AC-COORD-004**：确认 PendingAction 后只创建一个 Project；重复点击不创建第二个项目。
- **AC-COORD-005**：存在多个 pending target 时，“全部确认”展示选择，不批量猜测。
- **AC-COORD-006**：等待项目 Approval 时可以继续咨询；咨询不会自动 confirm/reject。
- **AC-COORD-007**：运行中的项目收到状态查询时立即回复，不进入 PendingUserInput。
- **AC-COORD-008**：明确范围变更在确认后才进入 PendingUserInput/FeedbackPacket，并关联目标 Project。
- **AC-COORD-009**：服务重启后 Session 和 PendingAction 可恢复，旧卡片重复点击安全返回。
- **AC-COORD-010**：自然语言和卡片确认共用 Delivery Gateway，并产生相同状态结果。
- **AC-COORD-011**：Coordinator 失败时 `/create`、`/status`、`/confirm` 和现有卡片仍可用。
- **AC-COORD-012**：真实飞书 E2E 覆盖讨论→检查→执行确认→Project 创建，以及咨询/范围变更/重复点击分支。

## 13. 分阶段实施

### Phase 1：只读对话闭环

- ConversationSession 与 Coordinator 独立 schema。
- answer / inspect_workspace / query_status / create_draft。
- 无项目自由文本不再只返回帮助。
- 历史对话回放测试。

### Phase 2：执行授权闭环

- PendingAction、Policy Gate、Delivery Gateway。
- start_delivery 确认卡与幂等处理。
- 自然语言/卡片统一确认路径。

### Phase 3：执行中持续对话

- 咨询、补充、范围变化、纠正和控制动作分流。
- 主动通知与状态卡更新。
- 重启恢复和真实飞书分支 E2E。

### Phase 4：按真实需求扩展

只有数据证明需要时再引入多 Topic、Artifact 多版本、跨聊天上下文和风险驱动的 delivery workflow 裁剪。
