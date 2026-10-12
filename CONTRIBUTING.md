# 开发与验证

项目源码与随附文档采用 [MIT License](LICENSE)，版权署名为 MAC contributors。
第三方依赖及外部 provider 保持各自许可，不因本项目采用 MIT 而改变。

## 安装

按 README 创建 `.venv`，使用 `python -m pip install -e ".[dev]"`。
runtime 依赖单独声明；不安装 dev extra 也应可导入应用与执行 CLI 帮助。
飞书及真实 provider 验证需要你自己的凭据，不应把它们写入 fixture 或提交。

## 修改边界

- MAC 负责项目托管与交互，不重写 provider 的工具/执行循环。
- 涉及 Codex 原生协议时，先对照基准 tag `rust-v0.144.1`、commit
  `44918ea10c0f99151c6710411b4322c2f5c96bea` 的官方源码，不猜测协议。
- 保留其他人的工作区改动；未经授权不重启服务、不清数据、不公开上传。
- 用户历史与选择是权威；旧 AI 判断和测试日志不能变成新的用户授权。

## 自动验证（T1–T3）

```bash
python -m compileall -q src tests scripts
for script in scripts/*.sh; do bash -n "$script"; done
python -m pytest tests/ -q
git diff --check
```

CI 在 macOS 上验证 Python 3.10 与 3.12 的编译、shell 语法、测试及构建。
提交本地检查与 GitHub CI 实际执行是两回事；没有 CI run 时不能声称 CI 通过。
未设立新的 lint 基线；不要以本次清理为理由全仓重新格式化。

## 真实验证与运行服务

测试层级见 [docs/testing-taxonomy.md](docs/testing-taxonomy.md)。T4 是真实本机服务与进程；
T5 是真实飞书 UI 操作，API 注入不能代替它。T6–T9 需要对应的真实 provider、项目和运行
能力，不得根据 FakeRuntime 或脚本终态推断通过。

主服务托管当前执行任务时，先结束任务或获取明确维护窗口，不能用“重复验证”打断它。
源代码修改后的运行部署需 editable reinstall 并使用一次性重启入口；仅创建本地发布候选
不触发部署。隔离目录的构建和安装验证不改变当前主服务环境。
