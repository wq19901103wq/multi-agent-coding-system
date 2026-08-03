# T5 私聊黑盒与性能复盘（2026-07-30）

## 范围与边界

本轮把 MAC 当作用户实际使用的系统，通过真实飞书私聊发送需求、点击卡片并观察完整状态流转。目标项目只能由 MAC 的 Agent 修改；人工只允许修改 MAC、操作飞书，以及在 MAC 异常时读取目标产物用于定位系统问题。

本轮目标包括：

- 在已有 `coding-agent` 项目上增加可持久化、可审计的计费功能。
- 在已有个人 Agent 项目上改善可用性、界面和文件发送能力。
- 给 MAC 增加持久化 profiling，解释端到端等待时间。
- 验证新项目之外的已有目录迭代、失败回退、重新规划、编码、测试、评审和最终验收。

## 已确认的系统问题

### 1. 用户感知的“慢”混合了四类时间

过去项目只记录总耗时和费用，无法区分：

1. Agent/模型实际执行；
2. 等待用户点击卡片；
3. Coordinator/状态机处理；
4. 进程失控、重试或其他未归因时间。

计费项目的历史 profile 一度显示总历时约 33,595 秒，其中 Agent 执行约 4,706 秒、决策/卡片等待约 28,865 秒、系统与未归因约 24 秒。最大的单项不是模型，而是卡片等待；其中包含测试操作方未及时处理已经出现的卡片。这解释了用户看到“卡片早已弹出但半天没人点”的现象。

修复后，MAC 持久化每个项目和任务的执行起止时间、provider、动态合同长度、system prompt 长度、runtime user prompt 长度和工具调用数，并在 Admin 项目详情页及 JSON profile 接口展示。

本轮进一步把私聊 Coordinator 的每次耗时、尝试次数、成功状态和消息 ID 纳入同一 profile。个人 Agent 项目的 7 个已记录私聊回合分别耗时 50.20、26.30、123.91、149.22、56.86、50.47、33.41 秒；总计 490.36 秒，均值约 70.05 秒。其中 149.22 秒是一次不必要的目录读取导致三次失败，修复首轮权限后，无工具回合仍需 33–57 秒，说明剩余延迟包含模型本身和约 35k 字符 Coordinator system prompt 的首 token 成本，不能归咎于状态机。

### 2. 同一份大合同被重复发送

原运行时把完整动态任务合同嵌入 system prompt 后，又把同一份内容作为 user prompt 发送一次。规划产物、schema、few-shot、事实和历史问答因此重复占用上下文，增加首 token 延迟和模型注意力负担。

修复后：

- 完整合同、schema 和 few-shot 只放在 system prompt；
- runtime user prompt 只保留很短的启动指令；
- 有结构化 WorkItem 时不再重复附加已被吸收的问答历史；
- WorkItem 的带时间戳事实继续作为权威输入。

真实任务已观察到 runtime user prompt 缩短到约 72 字符；复杂 Tester 的 system prompt 仍有约 34k–47k 字符，后续应继续减少不相关产物，而不是删除输出 schema。

### 3. Agent 启动的 Bash 超时后仍在后台运行

Claude Code 的 Bash 工具会创建独立进程组。以前工具表面返回超时后，`pytest`、shell 或目标程序仍可能继续运行；Agent 随后再次启动相同命令，形成重复负载，甚至在 Agent 结束后留下孤儿进程。

修复后，运行时持续发现 Agent 派生的独立进程组，按创建时间执行超时终止，并在 Agent 回合结束时统一清理。真实 Tester 中一个挂起的 pytest 进程组已在 180 秒被终止，Tester 随后继续完成，未留下孤儿进程。

### 4. 工具预算与任务复杂度不匹配

Tester 在 18 次和 30 次工具预算下分别因为预算耗尽失败；第二次重试复用已有证据后用 26 次工具完成。Tester 的配置最终调整为 60 次，为真实 API、浏览器与集成测试留出余量，同时仍由 detached-process guard 限制单条命令。

预算不能替代任务收敛规则。Spec Writer 在已有明确失败栈时仍大量重复读取并耗时约 227 秒，说明提示词还应要求优先消费结构化失败证据，只读取修订所必需的目标文件。

### 5. 权限合同与工作区差异验证曾经错误

- Agent 已有安全注入的 HostAutomation 能力，但合同没有把它作为权威可用能力展示，容易造成重复询问或错误判断“无权限”。
- 恢复任务时曾按整个累计 worktree diff 判断本轮是否修改，导致历史改动掩盖当前空操作。

现已补全能力合同，并只按当前 attempt 的内容差异验证是否真正产生修改。

### 6. 首轮 Coordinator 权限过宽导致无关读取和重试

当项目正等待结构化输入卡时，用户补充本地凭证引用，Coordinator 首轮仍拿到项目读取权限，错误读取了另一个项目的 `.env.example`，越界校验拒绝后连续重试，耗时 149.22 秒。修复后首轮只根据对话、待办卡和显式事实判断；确实需要项目事实时再走受约束的上下文协商。相同能力补充回合在无工具条件下一次成功，且只持久化源文件路径和变量映射，不复制密钥值。

### 7. 同一条消息不能同时选择卡片并补充详情

旧协议只能先选择“引用另一个本地项目凭证”，再单独发送路径与变量映射，造成两次交互。Coordinator 输出现支持 `pending_action_id` 与 `pending_option_key`，可在同一条自然语言消息中原子完成待办选择和结构化详情提交；Handler 仍按精确 action/option ID 校验，不使用关键词路由。

### 8. 质量 Agent 重复发现与当前合同漂移

- Tester 在恢复缺失凭证后没有拿到上一任务的可复用证据，重复执行完整发现和回归。恢复任务现在包含经过脱敏、定长的 continuation 合同，只重跑受阻、失败或未知项。
- Reviewer 和 Acceptance Agent 曾把历史规划文件中的旧 AC 当作当前合同，分别触发约 264/109 秒和 315/88 秒的两轮执行。现在 Orchestrator 编译的 WorkItem AC 集合是唯一当前集合，历史产物只作为证据，不能扩张合同。
- Acceptance Evaluator 的 `ask_user` 按 schema 本来允许省略最终矩阵，但 Orchestrator 仍要求全矩阵，导致两个约 170 秒回合。校验现区分中间提问与最终裁决。
- Acceptance Evaluator 只负责证据审核，权限收敛为 Read、最多 8 次；不得用 Bash 重新执行测试或浏览器验收。

### 9. 系统内部待办不得排在用户问题之后

个人 Agent 验收同时存在记忆序列、键盘路径、WebKit 等系统可补测项和实体手机局域网这一外部事实。旧行为先发手机卡并暂停项目。新合同要求：若 `ask_user` 中还有未被外部问题覆盖的 blocking AC，确定性校验拒绝该输出，先返回系统内部失败/补测；只有内部 blocker 清零后才允许打断用户。缺失 `evidence_source` 的旧 manual AC 也不再默认归用户，默认按 `system_observable` 处理。

### 10. 服务启动脚本缺少启动成功校验

旧脚本经 `nohup bash -c` 间接启动，写入 PID 后立即宣称成功，即使进程随即退出也不报错。新脚本直接调用虚拟环境中的 uvicorn，关闭 stdin，并轮询 PID 与 Admin 健康接口；启动失败会清理 pidfile 并返回非零，不再制造“已启动但网页打不开”的假象。

### 11. 验收失败没有成为下一轮 Coder 的输入

真实浏览器验收已经发现个人 Agent 的流式回复长期不结束，但第一轮返工 Coder 的 `open_feedback` 为空，因此只做了与失败无关的 GIF/SVG 清理。根因不是 Coder 不会修，而是 Acceptance Evaluator 的失败裁决只触发状态回退，没有持久化为 `FeedbackPacket`。修复后，每个失败验收项都带来源任务、验收标准、实际结果和建议回退阶段进入反馈链；Coder 收到后修复了服务端 45 秒 LLM 超时、禁用 SDK 自动重试，以及浏览器端 60 秒 watchdog 和 `[DONE]` 完整性检查。

### 12. Tester 自重试被恢复逻辑重复消费

Tester 完成后，递归自重试与“恢复已完成但尚未应用的任务”发生竞态：同一份失败结果被应用两次，重复创建反馈并耗尽 retry window，却没有真正启动新的 Tester。现在应用完成结果前先更新项目处理水位；同一来源任务的质量反馈也按 source task 幂等。显式从 `REWORK` 回到 `TESTING` 时会重置本轮自重试计数，但不会删除历史反馈。

### 13. Acceptance 的合同、schema、运行时和缓存键不一致

- Evaluator schema 允许没有 Tester 证据的直接 Acceptance 证据，但运行时校验一度强制 `tester_evidence_ids` 非空；现已统一。
- DeepSeek Evaluator 曾产生约 6 万字符输出且耗时数分钟，仍无法稳定满足严格裁决合同；Evaluator 已切到 Codex，并限制为只读证据审核。
- Acceptance 是否需要重跑一度按整个 `agent_config.json` 计算 revision，任何无关 Agent 配置变化都会让昂贵的浏览器验收失效；现在只绑定 Acceptance Agent 自身配置和 prompt。
- Acceptance 失败会直接成为结构化反馈；只有系统可观测 blocker 清零后，才允许询问实体设备等真正外部事实。

### 14. 飞书卡片等待包含操作方延迟

本轮多次出现 Agent 已完成、确认卡片已经显示，但自动化操作方没有及时点击。这部分不是 MAC 模型运行时间，应归入 decision/card wait，并在 profile 中与 Agent execution 分开。后续 T5 执行要求卡片出现后立即截图、识别并点击，同时记录卡片生成时间与点击时间；若操作方未及时响应，必须明确承认，不能把它解释成系统仍在思考。

### 15. 空上下文被错误替换为默认上下文，导致验收复用旧结果

修复流式超时后，Acceptance Agent 新一轮只做了 3 次读取便再次输出旧的“流式请求挂起”结论。实际原因是辅助 Agent 调用显式传入 `context_files=[]` 时，运行器使用 `context_files or defaults`，把合法空列表当成未提供，从而重新注入默认文件；Acceptance 还会收到上一修订的 `ACCEPTANCE_AGENT_OUTPUT.json`，模型直接复制了旧浏览器结果。

修复后，辅助 Agent 严格区分 `None` 和空列表；Acceptance 上下文明确排除上一轮 Acceptance 输出，只允许从带 revision 的证据账本复用仍有效证据。代码、配置或部署变化后，受影响的系统可观测 AC 必须从当前公开入口重新执行。该规则不依赖具体失败文案或关键词，并补了“显式空上下文”和“旧验收产物隔离”两条回归测试。

后续补验又暴露出 retry generation 与代码 revision 被混成同一个缓存键：点击“重新执行验收”虽能强制新 Agent，却会让同一份代码上刚取得的直接通过证据全部失效。现已拆分为稳定的交付 revision 和独立的 retry generation：前者决定证据是否仍有效，后者只决定是否必须启动新执行；账本仅注入当前 revision 的通过证据，失败缺口由最新 Evaluator 结构化反馈传递。这样补验只需执行缺口，不会反复跑完所有已通过路径。

### 16. 移动 WebKit 的真实失败不是“页面偶尔打不开”

个人 Agent 在桌面浏览器正常，但通过局域网 HTTP 从移动 WebKit 打开时，页面脚本直接调用 `crypto.randomUUID()`。非安全上下文中的 WebKit 不保证提供该 API，导致前端初始化中断。Coder 通过 MAC 增加了 `randomUUID -> getRandomValues -> 兼容生成器` 的逐级降级，并补了回归测试。该问题说明响应式截图和桌面 Chromium 不能替代目标浏览器引擎的真实执行。

### 17. Runtime 认可的结果与落盘质量产物曾存在双真相

Agent stdout 已通过 schema 校验时，Orchestrator 仍会复制 Agent 自己写入 worktree 的质量 JSON；一处未转义引号即可让后续 Reviewer 读到坏文件，而任务表却记录为成功。现在 `CODER_OUTPUT.json`、`TESTER_OUTPUT.json`、`REVIEWER_OUTPUT.json` 一律由 Orchestrator 使用已校验对象重新序列化，worktree 文件不再覆盖权威结果。

### 18. 累计子进程预算误杀了合法的真实 API 验证

单条工具进程仍限制在 180 秒、最大 300 秒，但旧实现同时把整个 Agent 回合的所有子进程累计运行时间限制为 300 秒。Tester 连续执行多条真实 DeepSeek、天气 API 和回归测试时，即使每条都正常结束，也会在总和达到 300 秒后被终止并整轮重跑。累计上限已提高到 900 秒，单进程失控保护保持不变。

### 19. 相同未跟踪测试文件导致合并成功后仍被判失败

Tester 新增未跟踪测试文件时，主工作区暂存内容和 Agent 分支可能产生字节完全相同的文件。Git 的 `stash apply -u` 会报告“文件已存在，无法恢复未跟踪文件”，旧逻辑据此把已经成功的 merge 判为失败并重复执行。WorktreeManager 现在只在 stash 没有 tracked 变化、且所有未跟踪 blob 与工作区逐字节相同时，把该冲突认定为冗余恢复；任何不同内容仍保留 stash 并失败，避免吞掉用户改动。

### 20. 通过真实 API 不等于测试断言有效

Tester 的时间、天气和组合工具测试一度只断言回复包含“点/分/天气/温度”等词。Reviewer 正确指出这仍是关键词过拟合：错误时间、错误温度甚至泛泛回复都可能通过。后续质量门要求正式测试解析回复时间并与系统时间比较（误差不超过 60 秒），同时捕获真实天气工具结果并逐字段核对温度、湿度、风速等语义值；临时人工比对只能作为补充证据，不能替代正式断言。

### 21. Coordinator 的相邻 schema 输出导致无谓重试

私聊 Coordinator 有时返回一份语义合理的 clarification 对象或自然语言，但没有使用 Conversation Coordinator 顶层信封。Runtime 对非 JSON 文本已经保留安全只读降级，但 schema-invalid JSON 对象之前被丢弃，下一次重试只看到长校验错误而看不到自己刚才的错误形状。现在 Runtime 同时保留 schema-invalid 的原始结果，Handler 把它作为下一次有界修复证据；该对象不会被直接执行或展示。提示词也明确：询问“为什么慢、为什么这么多轮、为什么没回复”等属于只读解释，只有明确祈使/授权才可提出暂停、恢复、取消或回退。

### 22. Tester 的修改所有权、主目录隔离与重试连续性不完整

Reviewer 要求加强正式测试后，Tester 曾只运行临时诊断却不修改测试文件；另一次则通过主工作区绝对路径直接修改目标仓库，后续新 worktree 从旧 HEAD 启动，又看不到这些未提交修改。修复包含三层：Tester 明确拥有测试文件、测试缺陷必须修改正式测试；Coder 与 Tester 都不再通过 `--add-dir` 暴露主目录；两类写角色执行前后都计算主目录指纹，发现越界写入就禁止合并并保留现场。Tester 自己返回质量失败时复用同一 worktree，不再丢掉已经完成的测试修订。

### 23. 服务停止不等于 Agent 子进程已经停止

旧停止脚本发送 TERM 后立即删除 pidfile 并返回。FastAPI 尚未执行完 lifespan shutdown 时，新服务已经启动，旧 Claude 子进程仍可能继续编辑同一任务。Runtime 现在登记所有活动 Agent 进程，应用关闭时统一取消；停止脚本最多等待 15 秒完成优雅退出，只有指定 uvicorn PID 仍存活时才强制终止。真实重启验证中，旧 Agent 不再残留。

### 24. 最新纠正被历史失败日志淹没

一次运行中纠正虽然由 Coordinator 一次识别并在约 40 秒后送达 Tester，但新任务合同仍达到约 34k 字符：当前纠正排在五轮历史失败的完整工具参数之后，Tester 先继续处理旧时间解析问题，甚至一度删除刚新增的负例。重试合同现只保留当前失败、前两次摘要和最后三个脱敏/截断工具痕迹；完整证据仍保存在任务表，不再重复塞进 prompt。该变更与 72 字符 runtime user prompt 一起减少首 token 和注意力浪费。

### 25. 质量裁决自相矛盾导致空转

Tester 多次给出“所有正式证据通过、没有 blocking failure、没有内部未覆盖项”，却同时输出 `passed=false` / `next_state_event=fail`。旧 Orchestrator 会据此创建没有可执行缺陷的 `test_bug`，再次启动 Tester。现在结构化证据满足完整通过条件时，系统把这种空失败归一为通过；反之，只要存在真实失败、阻塞项或内部覆盖缺口，仍强制进入 REWORK。历史上由空失败生成的 Tester 反馈会在读取 open feedback 前被关闭，人工驳回和真实缺陷不会被清理。

### 26. 零工具 Evaluator 不能只收到证据文件路径

Acceptance Agent 曾从真实浏览器完成 27/27 项验证，但后置 Acceptance Evaluator 仍判失败。根因是 Evaluator 被正确收敛为零工具证据审核角色，却只收到 `CODER_OUTPUT.json`、`TESTER_OUTPUT.json`、`REVIEWER_OUTPUT.json` 等路径标签，并没有收到文件内容；它无法读取路径，也就无法审核 Spec→实现→测试→评审→验收链。Orchestrator 现把当前 Spec 映射和三类质量 Agent 的权威、脱敏、定长输出直接嵌入 Evaluator 合同。零工具角色只消费嵌入证据，不再假设路径可读。

### 27. 验收重试必须按失败阶段归属复用证据

旧的“重新执行验收”统一递增 Acceptance retry generation，因此即使浏览器执行器已 27/27 通过、只是 Evaluator 证据不足，也会再次花数分钟跑完整浏览器、LLM、上传和故障注入。现在 Handler 按结构化阶段结果判断：执行器全项通过且后置 Evaluator 单独失败时，保留新鲜执行器证据，只重跑 Evaluator；执行器自身有失败时才失效并重跑黑盒。判断只使用 `result`、逐项 `criteria` 和执行先后关系，不匹配失败文案或关键词。

### 28. 服务重启退避不能永久累计历史故障

个人 Agent 的一次最终验收在旧代码上 27/27 通过，下一次却只通过 25/27：守护进程历史 `restart_count` 已累积到较高值，子进程被 kill 后等待完整 30 秒，再加启动时间约 35 秒才恢复，违反 30 秒恢复标准。该失败经私聊 Coordinator 原样进入 Coder，而不是由测试脚本绕过。Coder 将退避上限收敛为 15 秒，并增加连续 8 次异常退出的回归，期望序列为 `1、2、4、8、15、15、15、15`；Tester 的 97 项回归和真实故障注入均通过。

### 29. Profiling 必须同时回答“谁慢、慢在哪、成本是否完整”

项目 profile 除了墙钟时间、Agent 执行、卡片等待、系统未归因和 Coordinator 单轮耗时外，现新增按 Agent 类型聚合的任务数、状态分布、总耗时、平均耗时、工具调用数和运行时已报告成本。成本字段明确标识是否只是下限：Codex、DeepSeek 等运行时未返回金额时保持缺失，不用 `$0` 冒充零成本。Admin 页面同时展示各 Agent 聚合与最慢五个任务，便于区分模型执行、重复质量回路和操作方未及时点击卡片。

## 真实黑盒发现：单测通过不代表 CLI 可用

计费实现的数据库、价格快照、聚合、失败降级和三条 LLM 调用链测试通过后，Tester 与 Reviewer 均给出通过。但最终 Acceptance Agent 从隔离 HOME 运行真实 CLI 时发现：

- `/cost` 和所有 `/billing` 命令在产生输出前卡住；
- 调用栈停在 `REPL.__init__ -> indexer.build() -> parse_workspace() -> parse_file()`；
- Coder 上一轮只把 slash command 放到 `run_one()` 本地分发，仍未绕过 `REPL` 构造阶段的全仓索引；
- 因此 AC-BILL-003/004/005/006/010 正确判定失败，并回退到实现计划重新修复。

这证明 T5 的价值是从真实公开入口观察语义结果，而不是只检查代码存在、mock 结果或脚本最终状态。

## 后续验收标准

计费项目只有在隔离 HOME 下从真实可执行入口运行以下命令，均在明确时间上限内输出并以 0 退出，才可完成：

- `/cost`
- `/billing calls`
- `/billing summary --by model`（以及需求要求的会话维度）
- `/billing prices`

个人 Agent 项目已由 MAC 的 Acceptance Agent 从真实浏览器入口验证：稳定启动/自恢复、桌面与 Chromium 移动视口、王芊身份、真实模型对话、时间/天气工具、文件选择与发送、错误提示/重试以及刷新后 23 条历史恢复。首次验收同时发现真实流式回复不结束；该缺陷已通过结构化反馈交给 Coder 修复，75 项目标项目测试和三条真实 DeepSeek 工具闭环已通过，仍需完成修复后的最终 Acceptance。当前稳定入口为 `http://localhost:8000` 和 `http://192.168.2.8:8000`。尚未完成的系统可观测项必须先由 MAC 补测；最后仅实体手机局域网完整对话属于外部事实。

最终还需完成 MAC 全量单元测试、真实飞书 full flow、日志异常扫描、服务无 `--reload` 启动检查，并附两个目标项目的最终 profile。
