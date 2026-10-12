# 发布准备清单

准备日期：2026-10-10；Kimi 评审与修订：2026-10-11；代码整理与提交准备：2026-10-12。
本清单保留各冻结候选的历史证据；最新验收以
[本轮评审记录](architecture-review-20261012.md) 为准。提交源码不等于上线部署。
用户于 2026-10-12 另行选择并授权落实 [MIT License](../LICENSE)，版权署名为
MAC contributors；历史评审和旧候选中的未选许可状态不追溯改写。

## 已准备

- 将直接使用的 `httpx` 纳入 runtime 依赖；移除未被源码使用的 GitPython 与 pexpect。
- Kimi 复核后补齐 `python-dotenv`、`pycryptodome`、`SQLAlchemy` 及 Python 3.10 的
  `tomli` 直接依赖；只调整锁文件的本项目依赖元数据，没有升级第三方锁定版本。
- 安装步骤使用标准 `.venv`；维护脚本兼容已有 worktree 环境，不自动迁移现有服务。
- 新安装默认监听 loopback；健康预检不依赖管理页 token。
- 清除四个依赖已废弃阶段状态机的示例/拒绝流程脚本；不把它们作为 Root 验收。
- 过期部署 watchdog 只保留无副作用退役入口；本机旧内容留在忽略的审计目录。
- 补齐凭据/SQLite 临时文件忽略规则、安全隐私说明、贡献说明和最小权限 CI。
- `CURRENT_STATE.md` 不再用旧重构列表误导当前进度。

## 源码候选包

```bash
python scripts/prepare_source_release.py --output dist/mac-system-source.tar.gz
```

脚本不执行 Git、不停止服务、不调用 provider，也不上传。输出存在时拒绝覆盖。
包内 `PUBLIC_MANIFEST.json` 列出每个输入文件的 SHA-256，便于核对评审版本。

包括 MIT 许可文件、Python 源码与测试、通用维护/导入/评估脚本、示例配置、白名单中的产品/开发/评审文档、CI 及
存在时的 `uv.lock`。明确排除真实 `.env`、秘密存储、数据库/日志、用户项目、工作树、
Git 历史、个人 AGENTS 指令、本机事故/用户案例文档与指定项目的一次性切换脚本。
不携带第三方 provider 源码、CLI 或个人认证状态。
新生成的候选清单声明 `license_status: selected`、`license: MIT`，并记录 LICENSE 的 SHA-256。
wheel/sdist 通过 `pyproject.toml` 声明 MIT 并包含 LICENSE；第三方依赖保持各自许可。

脚本默认从所在目录的源码生成候选（包含相关未跟踪源码），不等于现有 HEAD 的
`git archive`。原交付的 `dist/mac-system-source-20261010-reviewed.tar.gz` 复用
2026-10-10 22:44 的 preview 冻结源码，仅更新本清单与 `CURRENT_STATE.md`，现保持不变。
本轮新修订 `dist/mac-system-source-20261011-kimi-reviewed.tar.gz` 复用原交付的冻结
源码，只增加依赖元数据、六项依赖回归、评审记录及相应打包/状态文档。
上述两份历史归档保持不变，当时没有混入共享工作区的未验收改动。
2026-10-12 另行冻结并验证当前源码，覆盖前轮之后的 model/list、原生压缩、
跨线程重置和操作 ID 维护差异，再提取纯展示与 ID 工具。
交付候选以包内 manifest 为准；本轮提交只包含经核对的项目源码、测试与配套文档，
不使用 `git add .` 混入本机案例、数据或指定项目的一次性脚本。
安全扫描仍必须在包内容上执行，白名单不能证明代码里没有秘密。

## 发布前扫描

使用经过校验的 Gitleaks 工具，检查候选文件与计划保留的历史；扫描输出需脱敏：

```bash
gitleaks dir /path/to/extracted/mac-system-source --redact
gitleaks git . --log-opts=HEAD --redact
gitleaks git . --log-opts=--all --redact
```

第三项用于发现备份风险，不表示所有分支都允许发布。不要把含明文秘密的 JSON 报告或
旧备份一起上传；不为“通过扫描”添加真实密钥白名单。旧历史发现真实凭据时保持禁止
发布，凭据轮换和历史处理另行确认，不自动执行。

## 验证证据

### 原 2026-10-10 冻结候选（复用证据）

- T1：Python 3.10.9 编译 `src`、`tests`、`scripts`，全部 shell 语法检查通过。
  根工作区 `git diff --check` 通过；执行验收的 preview 96 个输入文件与其 manifest 一致。
  最终包沿用其代码和测试输入，仅更新 `CURRENT_STATE.md` 与本清单；不是全部最终文件原样受测。
- T2–T3：冻结候选完整 `pytest tests/ -q` 为 **622 passed、3 warnings**，耗时 543.36 秒。
  使用独立临时数据库、项目目录和秘密存储路径，不读取主服务 `.env` 或调用真实 provider。
  三条警告是 Starlette/httpx 与 `jsonschema.RefResolver` 的弃用提示，没有借机改依赖架构。
- 定向发布/生命周期回归 43 项通过；候选白名单、SHA-256 manifest、相对文档链接、
  wheel/sdist 内容边界及 CI 固定 action SHA/只读权限已核对。
- 隔离构建 sdist/wheel、仅安装 runtime wheel 成功。CLI 帮助、39 个模块导入、
  OpenAPI `/health` 路径及没有 dev/build extra 的检查通过；`uv pip check` 检查 41 个包，
  全部兼容。这不是应用 lifespan 或真实服务验收。
- 上述验收阶段为本地自审，前轮独立维护评审不覆盖本轮开源准备。2026-10-11 已补足
  最终包的独立只读审查，未发现本地候选交付阻塞项；评审对象、检查归属和三项低风险
  意见当时另附外置评审记录。随后补充的
  [Kimi 评审与修订记录](source-candidate-kimi-review-20261011.md) 已纳入候选。同 provider 不同实例
  的审查不是第三方认证，未重跑上述验收。CI 尚未上传运行，Python 3.12 未在本轮执行。
  原交付包保持不变，包内自审说明反映打包时状态，由此外置记录补充。

### 2026-10-11 Kimi 最小修订

真实 Kimi 独立只读评审与清理边界见
[Kimi 评审与修订记录](source-candidate-kimi-review-20261011.md)。仅验收隔离的冻结
修订，不将原 622 项结果或共享工作区的测试结果冒充本修订证据。

- T1：Python 3.10.9 编译、shell 语法、F401/F841 检查及离线锁文件检查通过。
- T2–T3：定向依赖/打包/生命周期回归 49 passed；最终全量 **628 passed、3 warnings**，
  耗时 291.05 秒。使用独立数据库、项目和秘密存储，不调用真实 provider。
- 首次沙箱全量受进程枚举权限阻断，未计为通过；获批后的首次全量为 627 passed、
  1 failed，失败项是 `test_repeated_reconnects_use_bounded_native_continuations[10]`
  的 1 秒等待超时。未修改 runtime 或放宽测试；相关组 60 passed，最终全量通过。
  保留全部结果，不能由一次复跑证明永不波动。
- 干净环境仅安装新 wheel 的 runtime 依赖：39 个子模块导入、四项直接依赖导入、
  CLI/账号导入帮助、OpenAPI `/health`、wheel 元数据检查通过；42 个安装包兼容，
  没有 dev/build extra，不启动应用 lifespan 或 provider。
- Kimi 第二次只读复核未发现新阻塞项。98 个输入的允许差异核对通过；40 个源码
  文件和全部第三方锁定条目不变，相对文档链接可解析。
- 修订目录的完整脱敏扫描仍为一个人工 Authorization 脱敏测试样例，不是零命中。
  最终归档、wheel/sdist 核对及脱敏扫描结果留在宿主忽略的审计目录，摘要另行交付。

### 历史非候选工作区与扫描边界

共享工作区另一轮全量测试为 621 passed、1 failed；失败项
`test_superseded_owner_review_closes_card_when_human_input_predates_it` 单独复跑通过，
相关 Owner/report 组 14 项通过。不据此宣称最新工作区全绿；本轮没有修改该波动项。
前轮维护修复的 586 项通过也不充当本轮证据。

Gitleaks 8.30.1 的下载归档 SHA-256 已与官方发布元数据核对；扫描启用完整脱敏，
未新增白名单，也未使用 ignore 文件或 `gitleaks:allow` 跳过命中。结果不是零命中：

| 范围 | 命中 | 人工核对与处理 |
| --- | --- | --- |
| 候选目录 | 1 | `tests/test_redaction.py:26`，命令 Authorization 脱敏测试样例，不调用 provider |
| 可发布文件工作区快照 | 1 | 同一测试样例；不是实际 `.env`、数据库或整个宿主目录扫描 |
| HEAD 可达历史 | 1 | `tests/test_project_service.py:677` 的固定测试合同标识，64 位十六进制值 |
| 全部分支历史 | 3 | 上述测试值及 `.env.bak.20260711_182329` 两处应用 secret；按真实凭据风险处理 |

没有输出或打包命中值，脱敏审计报告留在宿主忽略目录。未验证旧 secret 当前是否有效，
也未轮换凭据或改写历史。全部本地分支不能整体公开；本轮仅核对并推送当前分支
的可达历史，最新扫描范围见本轮评审记录。扫描与人工核对均不是完整安全证明。

T5 真实飞书 UI、完整 T6–T9 和停服重启演练不属于本轮新增验收，不宣称通过。
为保护正在执行的项目，不安装到主服务虚拟环境、不重启主服务。

## 当前发布边界

- 项目源码与随附文档采用 MIT；LICENSE、包元数据与新生成的源码候选清单保持一致。
- 用户已授权 Kimi 复核后提交并普通推送当前分支；现有 origin 已公开，不另建仓库或修改可见性。
- 不改写历史、不轮换凭据、不启动或重启服务，也不执行维护 CLI。
- 旧备份分支不是候选；禁止 `push --all`、`push --mirror`。
- 仅拆分纯进度展示与无状态 ID 工具，不拆分 Root/Owner 执行职责，不改写原生执行协议或引入新工作流。
