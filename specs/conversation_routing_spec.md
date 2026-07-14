# 会话、用户与项目路由规范

> 版本：v1.1
> 用途：定义群聊、私聊、用户和项目之间的关系，以及消息如何被确定性地路由到项目状态机。

## 核心原则

1. 项目状态是 **project-scoped**，不属于任何单一聊天。
2. 一个聊天可以关联多个项目；一个项目也可以向多个聊天投递通知。
3. 每个 `(chat_id, open_id)` 最多选择一个当前项目 `active_project_id`。
4. 按钮、approval、显式命令必须携带 project_id 或可唯一反查 project_id 的 approval_id。
5. 自由文本只有在当前会话上下文能唯一确定项目时才可入队；禁止使用“用户最近更新的项目”猜测。
6. 项目权限、聊天可见性、当前对话上下文和通知订阅是四个不同概念，不得共用一张绑定表表达。

## 关系模型

### ProjectUser

表示用户对项目的权限，不负责消息路由。

| 字段 | 含义 |
|---|---|
| `project_id` | 项目 |
| `open_id` | 飞书用户 |
| `role` | `owner` / `editor` / `reviewer` / `viewer` |
| `created_at` | 授权时间 |

权限建议：

- owner：全部操作，包括 cancel、rollback、最终验收和成员管理。
- editor：追加需求、回答澄清、要求 REWORK。
- reviewer：查看并处理被授权的质量门。
- viewer：只读。

### ProjectChat

表示项目在哪些聊天中可见以及向哪里发送通知。

| 字段 | 含义 |
|---|---|
| `project_id` | 项目 |
| `chat_id` | 群聊或私聊会话 ID |
| `chat_type` | `group` / `p2p` |
| `relation` | `origin` / `subscribed` |
| `notification_level` | `all` / `approvals` / `result` / `none` |
| `created_by` | 建立关联的用户 |

`Project.feishu_chat_id` 可在迁移期保留为 origin chat 快照，但不得继续作为唯一聊天关系。

### ChatUserContext

表示某用户在某聊天中当前正在讨论哪个项目。

| 字段 | 含义 |
|---|---|
| `chat_id` | 当前聊天 |
| `open_id` | 当前用户 |
| `active_project_id` | 当前选择的项目，可为空 |
| `selected_at` | 选择时间 |
| `expires_at` | 可选的上下文过期时间 |

群聊上下文必须按用户隔离。一个成员执行 `/use A` 不得改变群内其他成员的 active project。

## 项目选择命令

| 命令 | 行为 |
|---|---|
| `/projects` | 列出当前聊天关联且用户有权访问的项目 |
| `/use <project_id>` | 设置当前 `(chat_id, open_id)` 的 active project |
| `/current` | 显示当前项目 |
| `/leave-project` | 清除当前会话上下文 |
| `/status [project_id]` | 有参数时查指定项目，无参数时查当前项目 |
| `/create ...` | 创建后关联当前聊天，并把创建者的 active project 切换到新项目 |

`/use` 只切换对话上下文，不暂停、恢复或改变任何项目状态。

## 消息路由优先级

收到消息后按以下顺序确定项目：

1. 卡片按钮中的 approval_id/project_id。
2. slash command 的显式 project_id。
3. 被回复的机器人消息中保存的 project_id。
4. `(chat_id, open_id)` 的 active project。
5. 若不存在唯一项目，返回项目选择卡片，不执行状态转换，不创建 pending input。

每一步都必须验证 ProjectUser 权限和 ProjectChat 关系。项目不属于当前聊天时，用户可使用显式命令操作，但系统应提示消息将作用于其他会话来源的项目；敏感操作可要求先 `/use` 或私聊确认。

对于 Agent 问题卡，步骤 3 必须采用确定性链路：`parent_id → Approval.payload.reply_message_ids → question_interaction_id → project_id`。它优先于 active project，且只能命中仍为 pending 的 approval/interaction。若卡片在原群聊中发送，群成员可按该群的 ProjectChat 关系获得隐式操作权限；私聊仍按 ProjectUser 校验。

## 聊天目录与项目上下文

聊天目录绑定和 active project 是两类独立上下文：

- `ChatDirectoryBinding(chat_id)` 决定该聊天之后新建项目的默认代码仓库，群成员共享；
- `ChatUserContext(chat_id, open_id)` 决定某个用户当前正在讨论哪个既有项目，群成员互不覆盖；
- `/use` 不改变目录绑定，`/bind-dir` 也不改变已有项目或其他成员的 active project。

一个聊天最多有一个默认目录，但可以关联多个项目；一个仓库也可承载多个项目。项目元数据必须按 `.mac/projects/{project_id}` 隔离，不能把“一个聊天”“一个项目”“一个仓库”视为同一实体。

## 群聊规则

群聊只处理以下输入：

- 明确 @机器人；
- slash command；
- 回复机器人项目消息；
- 点击交互卡片。

其他群聊文本忽略，避免把日常讨论误判为项目反馈。

群聊中的自由文本路由到发送者自己的 active project。若该用户未选择项目，即使群内只有一个项目，也应返回确认/选择提示，而不是静默绑定。

## 私聊规则

私聊使用相同的 ChatUserContext 模型，不把“用户最近活跃项目”当作当前项目。首次只有一个可访问项目时，可以提供一键选择，但必须产生显式上下文记录。

用户在多个私聊或设备中发送消息时，以飞书 chat_id 区分会话；卡片操作仍由 approval_id 确定项目，不受后来 `/use` 切换影响。

## 多聊天通知策略

一次项目状态转换只执行一次，随后向 ProjectChat 订阅者广播。建议默认：

| 内容 | 默认投递 |
|---|---|
| Agent 开始/普通进度 | origin chat |
| 汇报卡片 | origin chat；可订阅 `all` |
| 待审批 | 有权限 reviewer/owner 的私聊；origin chat 发只读提示 |
| 状态转换 | origin chat 和 `all` 订阅 |
| 最终结果 | 所有 `all` / `result` 订阅 |
| 敏感错误、成本、内部路径 | owner 私聊 |

同一 approval 可投递到多个聊天，但数据库中只有一条 pending Approval。任一合法响应成功后，其他聊天的卡片更新为“已由某用户处理”；更新失败不影响已提交状态。

## 与项目状态机的边界

会话路由层只生成带明确 project_id 的事件：

```text
Feishu event
  -> conversation scope
  -> project scope
  -> permission check
  -> command / user_input / approval event
  -> project state machine
  -> commit
  -> notification fan-out
```

切换聊天或 active project 不产生项目状态转换。项目状态机不知道“当前聊天”，只处理经过验证的 actor、project_id、event 和 payload。

## 中途输入与切换

1. pending user input 创建时立即固化 project_id、source_chat_id、sender_id 和 observed_state。
2. 用户随后 `/use` 切换项目，不得移动已经排队的输入。
3. Agent 完成后只消费属于该 project_id 的输入。
4. 群聊中不同成员可同时选择不同项目，互不影响。
5. 若输入到达时 active project 已终止，保留审计记录并提示用户重新选择。
6. scope change、correction 等影响状态的输入必须再次验证发送者角色。

## 歧义和错误处理

- 无 active project：返回 `/projects` 选择卡片。
- active project 已失效或无权访问：清除上下文并要求重选。
- 多个 pending input approval：必须通过 approval_id 选择，禁止按时间猜测。
- 卡片来自旧状态：返回已过期以及当前项目状态。
- 群聊用户没有项目权限：不泄露项目详情，只提示无权限。
- 同名项目：界面同时展示短 ID、状态和创建时间。

## 迁移要求

1. 停止以 `Project.feishu_chat_id` 作为唯一投递地址。
2. 停止 `_find_active_project_for_user` 的“最近更新项目”回退。
3. 将现有 ProjectUser 保留为权限关系，并补充 role。
4. 从现有项目的 feishu_chat_id 生成 origin ProjectChat。
5. 创建项目后生成创建者在来源聊天中的 ChatUserContext。
