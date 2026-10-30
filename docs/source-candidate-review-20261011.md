# 冻结源码候选独立评审记录

评审启动于 2026-10-10，结果归档于 2026-10-11（Asia/Shanghai）。
本记录是已交付候选的外置评审依据，不在原归档内，不改变候选或其 manifest。

## 对象与独立性

- 对象：`dist/mac-system-source-20261010-reviewed.tar.gz`。
- SHA-256：`fdfcb625bd3796b08b5000b2c1b96f406b2f1a4c1a3c1f8e89df8a739d6b7647`。
- 冻结阅读目录：`/private/tmp/mac-release-validation-20261010/reviewed/mac-system-source`。
- 执行：外部临时 Codex CLI，任务名 `source_candidate_independent_review_20261010`，
  `access=read_only`，工具返回 `success=true`，共 11 次只读命令调用。
- 该评审使用与主执行者相同 provider 的不同临时实例，未参与本候选的实现；
  是独立实例审查，不是跨 provider 评审、第三方认证或完整安全证明。
- 限定检查依赖、安装、文档、源码白名单、manifest、wheel/sdist、CI 和脱敏扫描依据。
  不审实时工作区，不运行全量测试，不修改文件、启停服务、选择许可证或上传。

## 独立评审结论

评审者返回的本地交付判断：

> 本地候选阻塞项：未发现。在限定范围内，没有发现要求撤回或重建本份冻结候选的具体缺陷。

> 本份冻结候选可作为本地源码准备成果交付；不等于正式开源、上线或完整安全认证。

下列发现及检查结果据独立评审返回报告归档，不把主执行者的既有测试改称评审者执行。
所有代码行号均指上述冻结目录，不指仍在变化的共享工作区。

### 非阻塞意见

1. **验收版本措辞不准确，低严重度。** 冻结包 `docs/release-readiness.md:55` 的
   “96 个输入文件与 preview manifest 全部一致”与该文档 `:31` 所述最终更新两份文档
   容易混淆。实际执行测试的是 preview；最终沿用其代码和测试输入，仅更新
   `CURRENT_STATE.md` 与 `docs/release-readiness.md`。最终包已由评审者独立核对 manifest。
   此记录澄清该差异，无须重建已交付包。
2. **CI 未固化 runtime-only 验收，低严重度。** `.github/workflows/ci.yml:26`
   安装 dev extra 和 build，`:30` 构建产物，不能自动发现运行依赖误用开发工具。
   既有隔离安装证据使其不阻塞本候选。后续如另行授权，可补干净环境 wheel-only
   安装、模块 CLI 帮助及必要导入检查；模块 CLI 存在，不构成当前入口缺失。
3. **未来依赖解析可重复性有限，低严重度。** `pyproject.toml:7`、`:28` 使用开放
   约束，CI 安装没有使用随包的 `uv.lock`。本次兼容性证据仍有效，但不能保证未来
   重新解析得到相同组合。后续可记录解析版本或明确 CI 的锁定基线，不扩展本轮实现。

## 评审者实际检查

- 只读核对交付源码包 SHA-256；包内 96 个输入文件及 manifest 共 97 个普通文件，
  逐项摘要、冻结目录内容及 manifest 一致，没有额外项、缺失项或非普通归档成员。
  manifest 标记 `local_candidate_only`、`not_selected`。
- 静态检查 `scripts/prepare_source_release.py:14` 的显式清单、`:44` 的文件/父目录
  符号链接防护、`:59` 的快照及 `:74` 的排他输出创建；阅读
  `tests/test_source_release.py:37`、`:63`、`:75`、`:92` 的相应覆盖，未运行测试。
  白名单不能证明允许文件内容不含秘密，文档保留了该限制。
- wheel 依赖元数据与 `pyproject.toml` 一致，40 个 Python 文件与冻结源码逐字节一致，
  RECORD 哈希和长度无异常；sdist 包含对应候选内容及生成的 `PKG-INFO`，文件一致。
  `src/mac_system/__main__.py:18` 提供模块 CLI；wheel 不含维护脚本及示例配置，
  不能把 runtime wheel 安装当作完整托管服务部署。
- README 的 `.venv`、macOS/launchd、外部 CLI 认证及配置前提与脚本相符；相对 Markdown
  链接无缺失。SECURITY 保留了管理页 query token、可信用户部署及隐私边界。
- `.github/workflows/ci.yml:8` 为只读权限，`:20` 起固定 action SHA 且关闭 checkout
  凭据持久化，没有发布、部署或 secret 引用步骤。未联网重新确认 action SHA 来源，
  未执行远端 CI。
- 只核对既有脱敏扫描 JSON 的数量和位置，未读取、输出或验证历史凭据值。

## 复用证据与未执行验证

旧 `docs/project-reset-review.md`、`docs/model-picker-review.md` 及
`/private/tmp/mac-kimi-review.iPO32S/review-round2.md` 只复用其维护行为与验证限制。
旧报告的 586、623、642 项测试及真实协议结果不移植为冻结候选结果，也不代替本轮
依赖、打包或 CI 评审。

以下为主执行者此前提供的冻结候选证据，独立评审者没有重新运行：

- T1：Python 3.10.9 编译、shell 语法及 diff 空白检查通过。
- T2–T3：完整 622 passed、3 warnings，543.36 秒；定向发布/生命周期 43 项通过。
- 隔离构建及 runtime-only 安装、CLI help、39 模块导入、OpenAPI `/health` 路径、
  无 dev/build extra、41 包依赖兼容检查通过。最终 wheel 与已验收 preview wheel
  字节一致。
- 共享工作区 621 passed、1 failed 的时序波动及单项/相关组复跑不替代冻结包证据。

评审者本轮未执行 T1–T9。Python 3.12 远端 CI、T5、完整 T6–T9、本轮主服务重启
均未完成；未绕过此前拒绝的 T5 授权。静态审查、归档比对不是实际服务验收。

## 公开发布边界与本轮处置

- 未选择许可证，未授权公开上传或推送，不宣称正式开源。
- 候选扫描有一个人工脱敏测试样例命中，不宣称零命中；不添加真实密钥白名单。
  全分支历史另有旧备份的两处应用 secret 风险，未轮换、未处理历史，不能公开现有
  Git 历史；禁止 `push --all`、`push --mirror`。这不是冻结无历史源码含真实凭据的结论。
- `SECURITY.md:39` 的公共安全报告入口尚未建立，仍属于后续公开发布准备边界。
- 本轮仅保存独立评审和修正共享工作区的验收说明；不修改交付归档、manifest、CI、
  依赖、运行配置或服务，不干预 digital-human。两项 CI/依赖建议仅记录，不启动新方向。

因此，“本地候选已交付且独立审查依据已补足”成立；“正式开源准备全部完成”不成立。
