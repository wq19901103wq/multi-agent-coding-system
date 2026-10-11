# MAC 当前状态

更新时间：2026-10-12（Asia/Shanghai）

## 当前实现

- MAC 是 coding-agent 的移动端项目宿主，不包含固定研发阶段状态机。
- 每个项目绑定持久 Project Lead thread；默认 Codex，按统一生命周期接口接入。
- 原生 steer、问题、审批、子 Agent 活动投影到飞书卡片与持久 outbox。
- Project Owner 独立审阅用户可见结果；依据真人来源理解方向，不读源码或代替测试。
- 项目隔离、重启恢复、费用记录以及明确授权的一次性故障接管已有实现与回归测试。
- 维护入口使用持久操作 ID 防止重放；过期部署 watchdog 已停用。

## 本轮整理与验证

- `core/root_progress.py` 承接纯内存进度模型、用量摘要和展示函数；Root 仍负责
  thread/turn 生命周期、事件解释、时间线轮换与 Owner/outbox，不另造执行框架。
- ID 生成移到 `utils/identifiers.py`；原入口保留显式兼容导出，没有修改 ID 格式。
- 复用前轮清理与依赖修订，补齐源码包中被测试实际导入的通用维护入口。
- 冻结 r2 候选 T2–T3 全量 **717 passed、3 warnings**，定向回归 76 passed。
  T1 编译、shell 语法及变更源码 F401/F841 检查通过。
- 真实 Kimi 只读静态评审未发现提交阻塞；其指出的候选文档死链已修复。
  评审对象、安装验证与限制见 [本轮评审记录](docs/architecture-review-20261012.md)。

前轮 622/628 项验收和波动记录保留在
[历史 Kimi 记录](docs/source-candidate-kimi-review-20261011.md) 与
[发布准备清单](docs/release-readiness.md)，不充当本轮新增验收。
测试使用独立临时数据库、项目目录和秘密存储，不调用真实 provider。
没有安装或重启主服务，也没有为重复验证打断 digital-human。

用户已明确授权在 Kimi 复核后提交并推送现有分支；这不授权推送旧备份分支、
改写历史、上线部署或选择许可证。许可证仍未选择，不宣称已正式开源。
T5 真实飞书 UI 和完整 T6–T9 不属于本轮验收，自动测试不替代这些黑盒检查。
