# MAC System 产品需求

> 版本：v1.1
> 用途：为跨状态机、会话路由和多 Agent 协作能力提供稳定 REQ/AC，详细实现约束由各专项 spec 定义。

## 多 Agent 主动澄清与 Grill-Me

### REQ-COLLAB-001：所有 Agent 均可在完成前请求用户输入

Planner、Coder、Tester、Reviewer、Acceptance Evaluator 和 Project Manager 在信息不足、存在多种合理选择、风险需要确认或完成标准不明确时，都必须能够暂停当前轮次并请求用户输入，而不是猜测后继续。

请求至少支持：单选/多选、自由文本、采用推荐项、接受默认假设后继续、连续多轮追问，以及暂停/取消/范围变更。

### REQ-COLLAB-002：交互必须持久化且可恢复

问题、选项、推荐理由、用户回答、轮次和恢复目标必须写入数据库。服务重启、聊天切换或 active project 切换不能丢失或移动已提交回答。

### REQ-COLLAB-003：问题必须说明影响

每个问题必须说明为什么要问、不回答的默认行为、不同选择影响哪些需求/方案/Spec/代码/测试，以及是否阻塞当前轮次。

### REQ-COLLAB-004：退出澄清循环必须明确

只有用户明确继续、所有 blocking question 已回答、用户接受已展示的默认假设，或用户取消/暂停项目时，才能退出输入循环。不得因超时静默采用高风险假设。

### REQ-COLLAB-005：问题与产物可追踪

问题和回答必须有稳定 `question_id/input_id`，关联 task、round、artifact version 及受影响的 REQ/AC/DEC/SPEC/feedback ID。后续 Agent 能看到已解决问题，避免重复询问。

## 各 Agent 必须支持的提问场景

| Agent | 必须考虑 ask_user 的场景 | 典型选项/输入 | 回答后恢复 |
|---|---|---|---|
| Planner / Specify | 目标、范围、角色、用户流程、业务规则、功能/异常行为、约束、非功能指标或验收标准不明确 | 范围、优先级、产品行为、默认假设、自由描述 | PLANNING_REQUIREMENTS |
| Planner / Architecture | 多个方案均合理；成本/性能/安全取舍需业务决定；外部系统能力未知 | 方案 A/B/C、推荐方案、约束补充 | PLANNING_ARCHITECTURE |
| Planner / Specs | 拆分粒度、兼容/迁移策略、接口契约或边界行为不明确 | 拆分方案、错误语义、兼容范围 | PLANNING_IMPLEMENTATION_SPECS |
| Coder | Spec 与代码现状冲突；破坏性迁移；公共 API 变化；安全敏感实现；必须扩大范围 | 保守/完整实现、迁移方式、是否允许依赖或接口变化 | CODING |
| Tester | 环境不可用；预期矛盾；破坏性测试；凭证/数据缺失；flaky 结果需判定 | mock/真实环境、期望结果、隔离/跳过 | TESTING |
| Reviewer | blocking issue 的产品意图不清；修复会改变范围；风险接受需 owner 决策 | 修复/接受风险/回退上游/补测试 | REVIEW 或 REWORK 目标 |
| Acceptance Evaluator | AC 证据不足；人工体验/业务结果必须由用户判断；是否接受已知限制 | 接受/拒绝/有条件接受、补充证据 | ACCEPTANCE_PENDING 或上游 |
| Project Manager | 消息无法唯一路由；多个项目/approval 候选；命令或跨 Agent 意见冲突 | 选择项目、approval、冲突处置 | 原控制/路由位置 |

Agent 不应为低价值信息重复打断用户。能够由已确认产物、代码、测试或既有回答唯一推导的问题应自行解决；只有会实质改变范围、契约、风险、成本或验收结果的问题才设为 blocking。

## 统一交付状态机

### REQ-FLOW-001：所有任务类型共用一个生命周期

Feature、Bug Fix、Documentation 以及未来的 Refactor/Migration 必须复用同一个交付生命周期：阶段执行、用户澄清、产物确认、质量失败回退、最终验收、合并和终态。不得为每个 task type 复制一份完整状态机。

Feature 的规划阶段固定为 `Specify → Design → Plan`。Specify 在同一个 `REQUIREMENTS.json` 和同一次人工确认中覆盖需求、产品行为与验收标准；不得再插入独立的“需求 Spec / Product Spec”状态。Plan 指面向实现的 Spec/任务拆分，不是第二份产品需求。

### REQ-FLOW-002：任务差异由 Profile 表达

Workflow Profile 只描述阶段顺序、Agent/Prompt stage、展示名称、可用回退目标和任务类型特有的阶段路由。`complete / fail / ask_user / confirm / reject` 的含义由通用状态机统一定义，Profile 不得覆盖这些核心语义。

### REQ-FLOW-003：保留业务可观测性

内部复用不能把界面退化为只有“执行中/等待中”。状态、日志、卡片和 API 必须同时呈现 task type 与具体业务阶段；Tester/Reviewer 提问后必须恢复到原 Agent 阶段，不能因为共享输入态而错误回到 Coder 或 Planner。

### AC-FLOW

- **AC-FLOW-001**：代码库只有一份生命周期模板；三类现有任务均由该模板与各自 Profile 成功生成。
- **AC-FLOW-002**：修改质量失败或人工确认的通用语义时，只需修改一处，三类任务测试同时生效。
- **AC-FLOW-003**：新增仅由现有阶段类型组成的任务类型时，只新增 Profile 和对应 Agent 配置，不修改 Orchestrator 的 task-type 分支。
- **AC-FLOW-004**：Feature/Bug Fix/Documentation 的正常、reject、REWORK、ask_user 和验收路径均保持可执行。

### 提问优先级与打断策略

- `blocking`：不回答就可能产生错误需求、错误契约、破坏性修改、安全风险或不可判定的验收结果；必须暂停原 Agent。
- `decision`：多个方案都可实现，但选择会显著影响成本、兼容性、交付范围或用户体验；默认等待用户，只有用户预先授权的低风险默认项才可自动采用。
- `non_blocking`：Agent 已有安全默认值，只需把假设和影响告知用户；不得创建阻塞状态。
- `informational`：普通进度、发现或建议，进入消息流或最终报告，不使用 `ask_user`。

同一 Agent 应先合并同主题问题再提问。除非上一轮回答产生新的、此前无法预见的分支，否则不得把一个可一次回答的问题拆成多轮。

## 验收标准

- **AC-COLLAB-001**：任一 Agent 输出 `ask_user` 后，项目进入可恢复输入边界；数据库存在一条 pending input approval；飞书展示问题、影响、推荐项、选项和自由输入入口。
- **AC-COLLAB-002**：用户回答固化到原 project/task/round；Agent 新一轮 prompt 能读取该回答。
- **AC-COLLAB-003**：连续三轮问答不会创建重复 pending approval，不会丢失历史回答。
- **AC-COLLAB-004**：选项和自由文本均可提交，且自由文本不会进入同一用户其他聊天中的项目。
- **AC-COLLAB-005**：等待输入时服务重启，恢复后复用同一 pending question，仍可回答并恢复 Agent。
- **AC-COLLAB-006**：存在未回答 blocking question 时不得 complete；非阻塞问题只有在用户接受默认假设后才能继续。
- **AC-COLLAB-007**：Coder、Tester、Reviewer 分别至少有一个主动提问真实场景，回答恢复给原 Agent，而非统一交给 Planner。
- **AC-COLLAB-008**：Architecture、Specs、Acceptance Evaluator 和 Project Manager 分别存在可执行的主动提问用例；回答均恢复到发问任务的后继轮次。
- **AC-COLLAB-009**：`non_blocking` 和 `informational` 消息不会创建 pending approval；同主题重复问题会被幂等键去重。
- **AC-COLLAB-010**：用户可选择“按推荐项继续”“本项目后续同类问题采用此默认值”或自由输入；授权范围必须持久化且可撤销。

## 多会话与多项目

### REQ-CONV-001：会话与项目是多对多关联，不是同一概念

一个私聊或群聊可以管理多个项目；同一项目也可以绑定多个已授权会话。系统必须保存显式 `conversation_id ↔ project_id` 绑定和每个会话的 active project，不得假设“一个聊天框就是一个项目”。

### REQ-CONV-002：写操作必须唯一寻址

回答问题、确认、驳回、暂停、恢复、取消和普通范围变更必须唯一定位到 project 及 pending interaction。存在多个候选时，Project Manager 只能展示选择器，不得猜测目标。

### REQ-CONV-003：群聊和私聊权限隔离

群聊成员身份、私聊身份、项目角色和操作权限分别校验。来自某个会话的文本不能自动注入另一个未绑定项目；敏感问题可以要求转到已授权私聊回答，但仍绑定原 question。

### REQ-CONV-004：切换只改变会话焦点

`/projects` 展示当前会话可访问项目，`/switch` 推选择卡、`/switch <project>` 直接切换 active project。切换不会暂停项目、迁移 pending question 或改变其他会话的 active project。

### AC-CONV

- **AC-CONV-001**：同一群聊创建两个项目后，无项目 ID 的写命令会在歧义时要求选择；选择后只影响目标项目。
- **AC-CONV-002**：同一用户在私聊与群聊分别切换 active project，两处焦点互不覆盖。
- **AC-CONV-003**：同一项目向两个授权会话发送同一 question，任一处回答后另一处卡片失效，且只记录一次回答。
- **AC-CONV-004**：未授权群成员不能确认、取消或回答项目问题；只读可见性按项目策略控制。
- **AC-CONV-005**：Agent 运行中收到文本时，系统先完成 project/interaction 路由再持久化；无法唯一路由的文本停留在待选择区，不进入任何 Agent 上下文。

### REQ-CONV-005：聊天可绑定已有代码目录

一个聊天可绑定一个本地 Git 仓库；后续新项目自动继承该目录。同一仓库允许存在多个 MAC 项目，但状态、需求、方案和实现计划必须按 `project_id` 隔离。解绑或切换目录只影响后续项目。

### AC-DIRECTORY

- **AC-DIR-001**：`/bind <绝对路径>`、`/current`、`/unbind` 可完成目录绑定、查询和解绑。`/current` 同时显示 active project 与绑定目录。
- **AC-DIR-002**：同一聊天创建的两个项目继承同一代码仓库，但使用不同的 `.mac/projects/{project_id}`。
- **AC-DIR-003**：不同聊天的目录绑定和 active project 独立。
- **AC-DIR-004**：回复 Agent 问题卡片按 `parent_id → approval → interaction → project` 路由，不受 `/use` 切换影响。

## 专项规范

- 状态循环：`state_machine_semantics.md`
- 调度恢复：`orchestrator_spec.md`
- 飞书交互：`feishu_bridge_spec.md`
- 会话目录绑定：`session_binding_spec.md`
- 多问题与回复卡片：`multi_question_ask_user_spec.md`
- 多聊天路由：`conversation_routing_spec.md`
- 输入持久化：`project_service_spec.md`
- 追踪关系：`artifact_traceability_spec.md`
