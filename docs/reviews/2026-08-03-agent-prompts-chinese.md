# 个人 Agent 项目：子 Agent 提示词中文版审计

> 项目：`proj_20260803_092543_64a30535b9de5b53`  
> 时间范围：2026-08-03 09:26—15:30  
> 共 84 轮：29 次创建 Agent，55 次续接 Agent。  
> 本文是便于产品审查的中文转述；英文原始记录另行保留，避免翻译掩盖原始证据。

## 一、所有 Codex 子 Agent 共同收到的系统提示词

你是一个由 MAC 协调器控制、任务边界明确的编码 Agent。

- 权限由本轮任务指定。
- 直接在提供的代码仓库中工作，并在当前线程完成目标。
- 根据需要调查、实现和验证。
- 不要直接询问用户；缺少外部事实时，把明确的阻塞原因返回给协调器。
- 不要决定整个项目的工作流程。
- 只读权限不得修改交付文件。
- 只在当前仓库、仓库声明的工具链以及有限的可执行程序路径内查找信息。
- 不得递归扫描用户主目录、系统目录、其他仓库或无关磁盘来寻找运行环境、依赖、凭证或证据。
- 优先使用仓库配置、任务中明确给出的路径、PATH 中的程序和小范围探测。
- 信息不足时向协调器报告环境缺失，不要自行扩大搜索范围。
- 同一时间只运行一个资源消耗较大的验证命令，并等待结果。
- 如果暂时没有输出，应检查已有进程和结果文件，不得重复启动等价验证。
- 最终消息应简洁说明结果、修改文件、验证情况和剩余阻塞。
- 协调器之后可能继续向同一个线程发送补充信息。

## 二、84 轮实际任务提示词的中文逐轮说明

1. **durable_forensics（只读）**：检查项目代码和持久化数据，定位旧助手消息污染用户身份、关系聚合遗漏、测试与部署方式，以及如何复用现有浏览器会话；不得修改文件。
2. **baseline_blackbox（只读）**：独立检查当前服务，建立身份污染、关系查询、用户隔离和既有黑盒能力的基线；不得清空记忆。
3. **implement_identity_relation_fix（写入）**：修复助手历史污染身份和关系聚合不完整；不得针对具体姓名、问题或关键词写特例；增加测试但暂不部署。
4. **implement_identity_relation_fix（续接）**：追加修复跨用户全文索引泄露、Agent 年龄问题被误判为用户记忆问题，以及临时数据库测试不稳定。
5. **security_code_review（只读）**：第一次安全审查，检查姓名/问题/关键词特例、数据丢失、兼容性、信任边界、关系遗漏、别名去重和全文索引隔离。
6. **independent_test_review（只读）**：第一次独立测试，覆盖身份污染、关系完整性、跨用户隔离和 Agent/用户身份判断。
7. **implement_identity_relation_fix（续接）**：根据第一次安全审查的五项失败继续修改，包括分句归属、别名冲突、旧生日数据、关系分类和全文索引旧记录。
8. **security_code_review（续接）**：第二次复审，重新检查上轮五项问题。
9. **implement_identity_relation_fix（续接）**：根据第二次复审继续修复混合问题丢失、多个实体与别名处理等回归。
10. **security_code_review（续接）**：第三次复审，重新验证混合 Agent/用户问题、多个实体和别名边界。
11. **independent_verification_r2（只读）**：第二轮独立验证，运行身份、关系、隔离和完整测试。
12. **implement_identity_relation_fix（续接）**：根据第三次复审，要求把有限的身份完整性规则改为逐分句分类和覆盖检查。
13. **security_code_review（续接）**：第四次复审，检查身份问题和开放工具/动作问题是否能够组合处理。
14. **implement_identity_relation_fix（续接）**：根据第四次复审，要求分析句子内部残余内容，不能依赖有限连接词。
15. **security_code_review（续接）**：第五次复审，检查连接在身份问题后的其他诉求、组合路由以及中英文实体边界。
16. **independent_verification_r3（只读）**：第三轮独立验证，运行对抗性身份、历史、关系和隔离测试。
17. **implement_identity_relation_fix（续接）**：根据第五次复审，要求增加身份证据账本，保留已识别谓词片段并处理未覆盖部分。
18. **security_code_review（续接）**：第六次复审，检查谓词片段、残余内容、终止条件和混合文字实体。
19. **implement_identity_relation_fix（续接）**：根据第六次复审，要求从数据模型和解析器层面替换隐藏的中文上下文关键词列表。
20. **implement_identity_relation_fix（续接）**：加入用户提出的通用语义架构约束，要求停止根据反例扩充正则、关键词和连接词列表。
21. **security_code_review（只读）**：按照新的动态语义架构审查，不允许新增正则、关键词、连接词或手工分类表。
22. **independent_verification_dynamic（只读）**：独立验证动态语义架构，检查是否仍存在依赖反例的确定性分类器。
23. **implement_identity_relation_fix（续接）**：根据动态架构审查失败，要求统一使用结构化语义决策，继续移除正则、关键词和手工分类。
24. **security_code_review_r2（只读）**：第二轮动态架构审查，要求模型负责语义识别，确定性代码只负责权限、数据约束和执行边界。
25. **independent_verification_dynamic_r2（只读）**：第二轮动态架构独立验证，检查结构化语义决策和确定性执行边界。
26. **implement_identity_relation_fix（续接）**：根据第二轮动态审查失败继续修改，同时禁止删除合法记忆或恢复关键词路由。
27. **security_code_review_r3（只读）**：第三轮动态架构审查，继续检查模型语义识别与确定性约束的职责分离。
28. **relation_memory_completeness_audit（只读）**：单独审计关系记忆完整性、别名和关系数据迁移。
29. **independent_verification_dynamic_r3（只读）**：第三轮动态架构独立验证。
30. **implement_identity_relation_fix（续接）**：汇总第三轮证据，要求一次完成确认流程、语义处理和关系数据方面的通用修复。
31. **relation_memory_completeness_audit（续接）**：要求输出 161 条候选关系的逐条处理结果，供写入 Agent 制作迁移。
32. **relation_memory_completeness_audit（续接）**：要求 Agent 自行裁决剩余六条关系记录，不询问用户。
33. **implement_identity_relation_fix（续接）**：把审计得到的 161 条关系记录及哈希交给写入 Agent，要求落实迁移。
34. **implement_identity_relation_fix（续接）**：进一步解释 161 条关系数据的离线提取方式，要求不要把它误当成运行时语义识别。
35. **security_code_review_r4（只读）**：第四轮安全/架构审查，继续检查统一语义决策和确定性约束。
36. **relation_ledger_verification（只读）**：验证关系账本、别名和迁移产物。
37. **independent_verification_dynamic_r4（只读）**：第四轮动态架构独立验证。
38. **relation_ledger_verification（续接）**：由于结果被截断，要求补充所有阻塞问题、损坏的别名文件、哈希差异和影响。
39. **independent_verification_dynamic_r4（续接）**：由于结果被截断，要求补充产品阻塞、环境阻塞和实际运行过的测试。
40. **implement_identity_relation_fix（续接）**：汇总第四轮全部反馈，要求修复工具调用一一匹配、关系账本、别名文件和其他问题。
41. **implement_identity_relation_fix（续接）**：原生会话失败后恢复同一工作区，继续第四轮修复。
42. **security_code_review_r5（只读）**：第五轮安全审查，检查工具调用账本、关系和别名修复。
43. **independent_verification_dynamic_r5（只读）**：第五轮动态独立验证。
44. **relation_ledger_verification_r2（只读）**：第二轮关系账本与别名验证。
45. **independent_verification_dynamic_r5（续接）**：要求补充被截断的测试结果、命令、数量，以及为什么 95 项通过不能证明整体通过。
46. **relation_ledger_verification_r2（续接）**：要求补充所有别名保存失败、缺失记录、影响和修复要求。
47. **security_code_review_r5（续接）**：要求补充第三项之后的所有安全阻塞和测试要求。
48. **implement_identity_relation_fix（续接）**：根据第五轮失败，追加工具调用前置条件、外部执行授权和其他安全修复；仍禁止部署。
49. **implement_identity_relation_fix（续接）**：原生会话再次失败后恢复同一工作区，并合并关系审计给出的完整别名证据。
50. **security_code_review_r6（只读）**：第六轮安全审查，检查工具批量验证、SSRF 防护、最小化实体检索和别名恢复。
51. **independent_verification_dynamic_r6（只读）**：第六轮动态独立验证。
52. **relation_alias_verification_r3（只读）**：第三轮关系和别名专项验证。
53. **security_code_review_r6（续接）**：要求补充工具批次原子性之后的所有安全阻塞。
54. **security_code_review_r6（续接）**：再次要求把 SSRF、确认冲突、批量副作用等所有阻塞压缩成编号列表。
55. **security_code_review_r6（续接）**：第三次要求补充第十项之后的全部阻塞。
56. **independent_verification_dynamic_r6（续接）**：要求补充全部产品失败、测试数量、环境限制和部署阻塞。
57. **security_code_review_r6（续接）**：第四次要求只返回第十项之后的阻塞。
58. **implement_identity_relation_fix（续接）**：把第六轮安全反馈全部交给写入 Agent，包括 URL/SSRF、确认碰撞、批量副作用等；仍禁止部署。
59. **security_code_review_r7（只读）**：第七轮安全审查，检查固定目标连接、禁用代理和重定向、对端校验等。
60. **relation_alias_verification_r4（只读）**：第四轮关系和别名验证。
61. **independent_verification_dynamic_r7（只读）**：第七轮动态独立验证。
62. **security_code_review_r7（续接）**：要求补充协议消息问题之后的全部安全阻塞。
63. **security_code_review_r7（续接）**：再次要求只返回第五项以后的阻塞，同时说明无法确认外部结果时允许“未知、禁止自动重试、人工检查”。
64. **independent_verification_dynamic_r7（续接）**：要求补充三个 manifest 不匹配之后的所有部署阻塞。
65. **implement_identity_relation_fix（续接）**：把第七轮 11 项反馈全部交给写入 Agent，同时明确不建设两阶段提交或分布式事务。
66. **implement_identity_relation_fix（续接）**：Codex 服务输出中断后恢复同一工作区，继续第七轮 11 项修复。
67. **security_code_review_r8（只读）**：第八轮安全审查，检查有序确认、稳定幂等键、外部结果未知状态、协议持久化、全文索引来源和别名清单。
68. **independent_writable_verifier_r8（可写隔离环境）**：第八轮完整验证，要求建立 Python 3.11 以上环境并运行约 409 项测试及安全、状态、记忆专项测试。
69. **security_code_review_r8（续接）**：要求补充人工检查问题之后的全部第八轮阻塞。
70. **security_code_review_r8（续接）**：再次要求只返回第四项之后的阻塞。
71. **independent_writable_verifier_r8（续接）**：要求说明为什么只运行了 20 项测试、完整测试是否执行以及所有环境阻塞。
72. **implement_identity_relation_fix（续接）**：第八轮安全审查和验证均判定失败，要求继续修复确认、幂等、协议、全文索引和测试问题；仍禁止部署。
73. **implement_identity_relation_fix（续接）**：1200 秒超时后恢复工作区，并纠正两项失败其实是旧测试合同，不得恢复关键词兼容逻辑。
74. **independent_writable_verifier_r8（续接）**：在新版本上重跑第八轮验证，使用可写工作区、Python 3.12 和独立临时目录。
75. **security_code_review_r9（只读）**：第九轮安全审查，继续检查统一语义决策、幂等和外部未知状态。
76. **security_code_review_r9（续接）**：要求补充旧协议回填之后的全部第九轮阻塞。
77. **security_code_review_r9（续接）**：再次要求只返回第四项之后的内容。
78. **independent_writable_verifier_r8（续接）**：要求补充目标测试数量、语义测试和完整测试结果，以及为什么验证仍未结束。
79. **independent_writable_verifier_r8（续接）**：再次要求补充测试不能结束的原因和最低重跑要求。
80. **implement_identity_relation_fix（续接）**：根据第九轮失败，追加确认事务、协议轮次、旧数据回填和测试迁移；仍禁止部署。
81. **independent_writable_verifier_r8（续接）**：在第九轮实现后再次运行完整验证。
82. **security_code_review_r10（只读）**：第十轮安全审查，检查确认事务、协议轮次、旧数据回填、工具执行边界和测试迁移。
83. **security_code_review_r10（续接）**：要求补充全文记忆搜索旁路之后的全部第十轮阻塞。
84. **security_code_review_r10（续接）**：再次要求只返回第六项之后的阻塞。

## 三、从提示词本身可以直接看出的偏差

1. 从第 5 轮开始，安全 Reviewer 实际上取得了项目路线控制权。
2. 第 7—19 轮围绕 Reviewer 制造的解析反例连续返工六次，迟迟没有回到真实产品入口。
3. 第 31—40 轮开始处理 161 条关系数据、哈希和账本，任务规模明显超过最初的用户可见缺陷。
4. 第 48 轮之后，主线转向外部执行授权、SSRF、批量原子性、幂等和协议恢复。
5. 第 53—57、62—64、69—71、76—79、83—84 轮大量用于追问“被截断的剩余阻塞”，说明 Agent 输出合同和上下文传递本身已经失败。
6. 系统直到第 65 轮才明确阻止两阶段提交等过度设计，边界设置明显过晚。
7. 84 轮里没有一轮真正完成“部署后从用户网页连续体验身份、记忆、语气、工具和发文件”的核心闭环。

因此，这不是单个 Agent 偶尔跑偏，而是 Coordinator 缺少目标优先级、审查预算、退出条件和面向用户的验收主线。
