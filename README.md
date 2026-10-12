# MAC System

**在飞书里持续开发、修改和调试本机项目。**

MAC 是 coding-agent 的移动端项目宿主：把用户消息送入持久的 Project Lead 对话，把执行进度、
问题、审批和交付结果投影为飞书卡片，并管理项目目录、会话恢复、用量记录和本机服务。
默认执行后端为 Codex，也提供 Kimi 项目主会话和可选的外部 coding-agent CLI 接口。

MAC **不是另一套 Agent 工作流框架**：不设置固定的需求→架构→编码→测试阶段，不增加
Coordinator，也不重写 provider 自己的模型、工具、上下文压缩或子 Agent 执行循环。

## 导航

- [能做什么](#能做什么)
- [架构与核心概念](#架构与核心概念)
- [安装与首次启动](#安装与首次启动)
- [配置参考](#配置参考)
- [飞书接入](#飞书接入)
- [使用示例](#使用示例)
- [飞书命令速查](#飞书命令速查)
- [Project Owner 与执行后端](#project-owner-与执行后端)
- [运行维护与安全](#运行维护与安全)
- [常见问题与排障](#常见问题与排障)
- [代码结构](#代码结构)
- [开发与验证](#开发与验证)
- [文档导航](#文档导航)
- [使用许可](#使用许可)

## 能做什么

| 场景 | MAC 的行为 |
| --- | --- |
| 从零开发项目 | 在系统管理目录创建项目，由完整 coding agent 直接读写、运行和调试 |
| 维护已有项目 | 打开白名单内的本机目录，后续消息在该目录持续迭代 |
| 运行中补充要求 | 将消息送入当前执行；默认 Codex 使用原生 `turn/steer`，不另开一条并行任务 |
| 多轮协作 | 一轮结束后保留项目和对话，下一条消息继续同一 thread |
| 提问与审批 | 将原生问题、命令、文件和额外权限审批呈现为可操作卡片 |
| 按需调用其他助手 | 提供实际可用的外部 CLI 能力，由执行 Agent 按任务需要选择，不按关键词路由 |
| 查看进度和费用 | 记录执行事件、provider 活动和用量；卡片按时间顺序展示实际状态 |
| 重启后继续 | 持久保存项目、会话关联和卡片状态，恢复时先检查已完成工作，避免重复执行 |

默认 Codex Root 从第一轮起拥有正常 coding tools、skills 和原生多 Agent 能力。
子 Agent 的创建、消息、等待和打断由 Codex 原生工具处理，MAC 只承接公开事件与交互。

**适用边界：** 当前托管脚本面向 macOS 的用户级 `launchd`，不是开箱即用的跨平台部署方案，
也不承诺多租户管理安全。请只给可信用户使用，并按实际环境验证飞书和 provider。

## 架构与核心概念

```text
飞书用户消息 / 卡片操作
          │
          ▼
WebSocket 接入 → 项目选择与持久化 → Project Lead runtime
                                      │
                                      ▼
                              持久 Project Lead Thread
                              默认 Codex，负责实际执行
                                      │
                                      ▼
                            原生执行事件 / 问题 / 审批
                                      │
                                      ▼
                         NotificationOutbox / CardProjection
                                      │
                                      ▼
                             飞书进度卡 / 交互卡 / 结果卡

隔离的 Project Owner：审阅公开结果、辅助已有授权内的选择；不读源码、不编码。
```

| 概念 | 含义 | 什么操作会改变它 |
| --- | --- | --- |
| Project | 长期工作单元，关联固定目录、执行状态和管理历史 | `/create`、`/open` 创建或打开；`/use` 切换 |
| 工作目录 | Agent 实际读写的文件位置 | 随 Project 切换；`/info` 查看真实路径 |
| Thread | 当前项目的持久 provider 对话 | 正常多轮复用；`/new` 新建对话 |
| Turn | 一次执行轮次 | 空闲时发送消息开始新一轮；运行中补充进入当前轮 |

当前项目选择以“机器人账号 + 飞书聊天 + 用户”为边界。不同项目或聊天不会自动共享对话。
即使打开同一个目录，不同聊天也各自保留项目与 thread；**目录文件仍是共享资源**，需要用
Git/worktree 等边界避免并发修改。`/close` 只关闭当前选择，不删除项目文件。

详细职责与协议基准见 [架构文档](docs/architecture.md) 和
[交互规范](docs/interaction-spec.md)。

## 安装与首次启动

### 1. 准备环境

- macOS，且具备当前用户的 `launchd` 会话；不要以 root 运行。
- Python **3.10 或以上**、Git。
- 已安装并完成认证的 coding-agent CLI；默认需要可用的 Codex。
- 可管理的飞书应用，以及可访问飞书和所选 provider 的网络。

MAC 不附带外部 CLI、认证数据、模型额度或第三方源码。CLI 应在**运行服务的同一系统用户**
下完成认证；终端能执行，并不意味着 GUI `launchd` 下也能找到它。
在 macOS 上，MAC 会优先选择 ChatGPT 内置的兼容 Codex binary；其他安装可通过 `CODEX_BIN`
显式指定。不要随意换成与已有 thread 存储不兼容的旧版本。

### 2. 获取源码

使用已发布的 MIT 源码版 `v0.1.0`：

```bash
git clone --branch v0.1.0 --depth 1 https://github.com/wq19901103wq/multi-agent-coding-system.git
cd multi-agent-coding-system
```

按 tag 克隆用于固定版本运行，通常处于 detached HEAD；需要开发时再自行选择工作分支。
也可以从 [v0.1.0 发布页](https://github.com/wq19901103wq/multi-agent-coding-system/releases/tag/v0.1.0)
下载 `mac-system-source.tar.gz`、`release-provenance.json` 和 `SHA256SUMS`，先校验再解压：

```bash
shasum -a 256 -c SHA256SUMS
tar -xzf mac-system-source.tar.gz
cd mac-system-source
```

将三个附件放在同一目录执行校验。源码包不包含 `.git`、本机凭据、数据库、用户项目或个人
provider 会话；`release-provenance.json` 说明制包来源。包内 manifest 的候选状态是制包时的
历史快照，公开发布情况以发布页和随附 provenance 为准。
如需开发分支，请明确选择分支，不要假设仓库默认分支与上述发布版本一致。

### 3. 安装依赖

在仓库根目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
test -e .env || cp .env.example .env
```

`[dev]` 包含测试等开发依赖；仅运行服务可用 `python -m pip install -e .`。
最后一条命令只在 `.env` 不存在时复制，避免覆盖已有凭据。
托管脚本优先使用根目录 `.venv`，不存在时才兼容旧的 `.worktrees/feat-core/.venv`。

### 4. 填写最小配置

编辑 `.env`，至少填写下面两项飞书凭据和两个独立的强随机 token：

```dotenv
FEISHU_APP_ID="你的飞书应用 App ID"
FEISHU_APP_SECRET="你的飞书应用 App Secret"
FEISHU_TRANSPORT=websocket
ADMIN_TOKEN="单独生成的管理页随机 token"
CONTROL_API_TOKEN="单独生成的控制接口随机 token"
MCS_HOST=127.0.0.1
MCS_PORT=18080
PROJECT_LEAD_PROVIDER=codex
PROJECT_OWNER_PROVIDER=codex
FEISHU_TEST_MODE=false
```

这里的中文字符串是占位说明，必须替换。可以在本机生成两个随机值，再分别填入 token 字段：

```bash
python -c 'import secrets; print(secrets.token_urlsafe(32)); print(secrets.token_urlsafe(32))'
```

`.env` 同时被 Python 配置读取和 shell 加载，带空格或特殊字符的值须正确引用。
不要提交 `.env`，不要把实际 token 贴进聊天、截图或日志。
`.env.example` 开头的 `ANTHROPIC_*`/DeepSeek 配置只服务于可选 Claude Code 后端，
**不是默认 Codex 或 Owner 的必填项**。

### 5. 接入飞书并启动

先按[飞书接入](#飞书接入)启用机器人、配置长连接事件并发布应用，再执行：

```bash
./scripts/start_server.sh --check
./scripts/start_server.sh
```

`--check` 只检查本机启动文件、shell 配置语法和依赖，不启停服务；
**它不验证飞书凭据、模型认证或额度**。正式启动会安装/使用用户级服务
`com.mac-system.dev`，日志默认写入 `server_launchctl.log`。
同一系统用户的托管入口使用同一个服务 label，不要从多个 checkout 同时启动不同实例。

### 6. 确认服务可用

以下命令只读检查，不会触发重启：

```bash
lsof -Pi :18080 -sTCP:LISTEN
curl --fail http://127.0.0.1:18080/health
tail -n 80 server_launchctl.log
```

使用自定义端口时替换 `18080`。健康接口返回 `status` 和各机器人账号的 `feishu_ws` 状态；
`connected` 表示近期观察到连接，`reconnecting` 表示重连中，`unknown` 表示状态缺失或过期。
HTTP 200 只证明健康接口可响应，不能代替飞书发消息、点卡片或真实 provider 验收。

浏览器打开管理页时使用：

```text
http://127.0.0.1:18080/admin/projects?token=<ADMIN_TOKEN>
```

将占位符替换为本机配置值，不分享完整 URL。最后在机器人私聊发送 `/help` 和 `/info`，
核对可用命令、当前项目和真实目录。

## 配置参考

完整模板见 [.env.example](.env.example)。下面只列出部署和使用中最常见的字段。

### 必填与安全配置

| 变量 | 默认/示例 | 用途与注意事项 |
| --- | --- | --- |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 空 | 单机器人凭据；多机器人见下节 |
| `ADMIN_TOKEN` | 空，部署前应填写 | `/admin` 管理页及操作的 query token；为空时管理页无认证 |
| `CONTROL_API_TOKEN` | 空，部署前应填写 | REST `/projects`、`/test` 的 bearer token；建议独立于管理 token |
| `MAC_ALLOWED_BIND_ROOTS` | 空 | 可绑定的本机目录根，逗号分隔绝对路径；空值禁止绑定 |
| `FEISHU_TEST_MODE` | `false` | 测试注入入口开关；生产不要持久启用 |

REST 控制接口使用 `Authorization: Bearer <CONTROL_API_TOKEN>`。
未设置 control token 时实现会回退到 admin token；两者都为空则拒绝控制请求。
不要依赖这个回退来省略独立配置，更不能据此认为未设 token 的管理页也安全。

### 服务与数据

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MCS_HOST` | `127.0.0.1` | 托管脚本的监听地址；不要未经安全配置改为公网监听 |
| `MCS_PORT` | `18080` | 服务端口 |
| `DATABASE_URL` | `sqlite:///./mac_system.db` | 项目、消息、交互和会话关联的持久存储 |
| `PROJECTS_DIR` | `./projects` | 系统创建项目的父目录 |
| `AGENT_CONFIG_PATH` | `./agent_config.json` | coding-agent 能力与配置文件位置 |
| `HTTP_PROXY` / `HTTPS_PROXY` | 空 | 可选出站代理；被 coding-agent 子进程继承 |
| `MAC_PUBLIC_BASE_URL` | 空 | 飞书文件链接使用的可信 HTTPS origin，不是管理页公开开关 |
| `MAC_FILE_LINK_SECRET` | 空 | 文件链接签名密钥；空时复用 control token，可单独配置以便轮换 |

### 执行与审阅

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PROJECT_LEAD_PROVIDER` | `codex` | 默认项目执行后端 |
| `CODEX_BIN` | 自动选择 | 可显式指定兼容的 Codex 可执行文件 |
| `PROJECT_OWNER_PROVIDER` | `codex` | 隔离 Owner 的执行适配器；其他实现须满足相同契约 |
| `PROJECT_OWNER_WAKEUP_SECONDS` | `15` | Owner 唤醒检查间隔，不是完成时间保证 |
| `PROJECT_LEAD_SILENCE_TIMEOUT_SECONDS` | `300` | 模型流无活动监测；原生工具和审批等待可更久 |
| `PROJECT_LEAD_FAILOVER_PROVIDER` | 空 | 默认关闭故障接管；明确启用 Kimi 接管前须配置认证并接受其边界 |

不要通过降低超时把正常审批等待当作故障，也不要把可选接管理解为无限自动重试。

## 飞书接入

MAC 默认通过 `lark-oapi` 的 **WebSocket 长连接**接收消息和卡片事件，
不需要为了接收飞书事件公开本机 HTTP 端口或设置公网回调地址。

1. 在飞书应用后台启用机器人能力，获取 App ID 与 App Secret。
2. 配置长连接事件接收，订阅 `im.message.receive_v1` 和 `card.action.trigger`。
3. 按应用后台要求开启消息读取、机器人发消息等权限；常用发送权限为 `im:message:send_as_bot`。
4. 发布应用版本，并确认测试用户在应用可用范围内。
5. 填写 `.env` 后启动 MAC，检查 `/health` 中连接状态，再私聊发送 `/help`。

事件订阅、权限和应用可用范围缺一不可。能收到消息但不能点卡片时，优先检查卡片事件；
能建立连接但不能发卡片时，优先检查发送权限。详细路径见
[飞书接入说明](docs/feishu_onboarding.md)。

### 可选：多个机器人

```dotenv
FEISHU_BOT_ACCOUNTS_JSON='{"default":{"app_id":"cli_xxx","app_secret":"替换为凭据"},"research":{"app_id":"cli_yyy","app_secret":"替换为凭据"}}'
FEISHU_DEFAULT_BOT_ACCOUNT_ID=default
```

使用真正的应用凭据替换示例。未配置 JSON 时，旧的 `FEISHU_APP_ID`/`FEISHU_APP_SECRET`
作为默认账号兼容。每个项目固定归属一个机器人账号，聊天选择、去重和通知按账号隔离；
每个机器人有独立的 WebSocket worker 进程，但它们共用同一个 MAC 服务。

## 使用示例

下面的文本在**飞书聊天中发送**，不是终端命令。正常工作直接说需求，不必选择研发阶段。

### 新项目与多轮迭代

```text
/create 做一个本地待办网页，支持新增、完成和删除；先实现最小可用版本
```

也可以直接发送需求；当前没有项目时会自动新建。创建卡会显示实际工作目录。
之后继续发送：

```text
加一个按状态筛选的入口，保留现有数据格式
检查刚才的修改，给出实际运行结果和还没验证的部分
```

项目空闲时会在同一 thread 开始下一轮；运行中追加内容则进入当前轮。
若想控制权限，可以明确说“先只读排查，不改文件、不重启服务”。

### 打开已有目录

部署前在 `.env` 只允许必要的目录，例如：

```dotenv
MAC_ALLOWED_BIND_ROOTS="/Users/yourname/workspace,/Users/yourname/another-repo"
```

替换为服务账号可访问的真实绝对路径；已有服务读取新配置需要授权维护窗口，不能为此打断任务。
随后在飞书发送：

```text
/open /Users/yourname/workspace/my-project
/info
先只读检查启动失败的原因，列出证据，不要改配置或重启
```

- 必须落在允许的根目录内；空白名单会拒绝绑定。
- 已有 Git 仓库必须使用仓库根目录，不能把仓库子目录当成独立项目。
- **绑定不是纯只读操作**：目标不存在时会创建目录；非 Git 目录或没有 HEAD 的仓库会初始化
  Git 并创建初始提交。因此，维护现有项目时先确认路径正确且仓库状态符合预期。
- 在同一聊天重新打开同一目录会恢复该目录原有项目与对话；不同聊天不会自动复用。
- `/open` 不复制文件到 `projects/`，Agent 直接操作绑定目录。

### 运行中补充与交互卡

```text
补充一下：不要升级依赖，只修这个报错
```

默认 Codex 会把补充送入同一 turn，不另开一轮重复执行。
出现问题卡时，逐项选择或填写后点击**提交**；只点选项不等于答案已发送。
回复单个问题卡也可作为自由文本回答；多个待答问题请在原卡逐项填写。
未引用问题卡的新消息属于补充，不会自动替你回答或关闭旧问题。

命令、文件和额外权限审批也通过卡片处理。先核对目录、动作和影响，再批准；
已处理卡显示结果，历史进度不会因后续执行而被反复改写。

### 查看、切换与恢复

```text
/proj
/use 项目名或ID前缀
/info
```

切换项目会一起切换工作目录和 thread。想换一段干净对话但保留文件时使用 `/new`；
想停止当前一轮使用 `/stop`；执行卡住且普通停止无法完成时再考虑 `/reset`。
不要把这些项目控制命令当作宿主服务重启命令。

## 飞书命令速查

| 命令 | 常用别名 | 作用 |
| --- | --- | --- |
| `/create [需求]` | `/c` | 新建并执行；不带需求时提示输入 |
| `/open [绝对目录]` | `/o`、`/bind`、`/b` | 打开已有目录；不带路径时显示目录卡 |
| `/proj` | `/ls` | 查看项目列表 |
| `/use [项目名或ID前缀]` | `/s` | 切换项目；不带参数时显示选择卡 |
| `/info` | `/i` | 查看当前项目、真实目录和状态 |
| `/close` | `/unbind`、`/u` | 关闭当前项目选择，不删除文件；可用 `/use` 返回 |
| `/agents` | `/a` | 查看外部 coding-agent 的配置、可用性和能力 |
| `/agent [codex或kimi]` | — | 查看或切换项目执行助手；不是调用外部审阅任务 |
| `/model [模型名]` | — | 查看 Codex 模型选项或设置模型；`/model default` 恢复默认 |
| `/owner [on或off]` | `/ow` | 查看、开启或关闭当前项目 Owner |
| `/new` | `/n` | 保留项目文件，开始新对话；不清空项目管理历史 |
| `/pause` | `/p` | 将项目标为暂停，保留会话；要中断当前执行请用 `/stop` |
| `/resume` | `/r` | 恢复暂停项目；再次发送任务也会恢复 |
| `/stop` | `/x` | 取消当前执行，保留项目、目录和对话 |
| `/reset` | — | 强制清理当前项目卡住的执行与子进程，取消旧队列和 Owner 续推 |
| `/menu` | `/m` | 打开操作菜单 |
| `/help` | `/h` | 查看当前版本的全部快捷命令 |

`/reset` 保留文件、provider 和原生会话历史，**不自动重跑任务**；成功后发新消息继续。
只有确认退出与清理完成才报告成功，清理超时会保持项目隔离，可再执行 `/reset`。
它依赖 MAC/飞书通道仍可响应，不能修复整机、宿主事件循环冻结或 provider 网络故障。

`/model` 只在项目空闲时切换；实际更换模型后，下一条消息开启新对话，文件和 Owner 模型不变。
当前其他 provider 不支持该选择器；模型列表获取失败会明确提示，不返回猜测列表。

## Project Owner 与执行后端

### Project Owner 做什么

Owner 是与执行 Agent 隔离的用户项目代理，不是 Coordinator、Reviewer 或 Tester：

- 普通真人消息直接进入 Root，不经过 Owner 前置分类。
- 原生问题和审批卡始终先展示；Owner 根据真人原话、历史选择和已有授权判断能否可靠代答，
  不能代答时原卡继续等用户。
- 结果展示后独立审阅公开汇报，判断是否补足当前工作、提出明确建议或让用户选择下一步。
- 只读取带来源的项目原则、公开管理历史和汇报，不读源码、不编码、不调度工具任务。
- 具体命令和一次性选择留在历史；泛化原则与旧 AI 判断不能冒充新授权。

用户问题、选项、实际选择和上下文会持久记录。需要新偏好、授权、费用、外部事实，或涉及
公开发布、外发、删除、覆盖等边界时，不应仅凭 AI 建议自动执行。
用 `/owner off` 关闭当前项目的代答与结果审阅，用 `/owner on` 恢复；
关闭 Owner 不关闭 Root 的正常问题和审批能力。

### 区分三种 provider 行为

1. **项目执行助手**：默认 Codex；`/agent` 查看当前执行者，`/agent kimi` 或
   `/agent codex` 显式切换后续消息。Claude 尚未接入项目主会话。
2. **外部 CLI 任务**：Root 按实际能力调用 Claude Code、Kimi、Pi 或外部 Codex；
   用 `/agents` 查看当前可用情况，不是所有配置项都默认启用或可用。
3. **故障接管**：默认关闭。用户明确启用后，可进行一次公开的 Codex→Kimi 接管；
   先确认旧执行与子进程停止，再保存目标会话和公开进度并告知用户，不静默或循环换助手。

不要假设切换 provider 会迁移完整原生对话或保证所有工具能力相同。
外部 CLI 的安装、认证、费用、可用模型和许可均由对应 provider 决定。

## 运行维护与安全

### 本地数据

| 路径 | 内容 | 能否随源码公开 |
| --- | --- | --- |
| `.env`、`.mac-secrets.json` | 凭据和本机秘密配置 | 不能 |
| `mac_system.db` 及 WAL/SHM | 项目、真人消息、选择、汇报和会话关联 | 不能 |
| `projects/`、绑定目录 | 用户创建或维护的项目 | 必须单独确认，不属于 MAC 源码 |
| `.mac/` | 运行状态、连接状态和维护尝试记录 | 不能当作公开源码 |
| `server_*.log`、个人 CLI 会话 | 运行证据与可能的个人信息 | 分享前须审查和脱敏 |

只给可信用户使用，尽量用受限系统账号与最小目录白名单。
默认保持 `127.0.0.1` 监听；远程访问须另行配置 TLS、反向代理和访问控制。
文件分享链接有签名，但并不意味着任意项目文件适合外发。
飞书与所选 provider 会收到完成任务所需的数据，目录绑定和审批不能消除这种传输。

### 更新与重启

文档修改不需要重启。源码部署更新应先结束任务或取得明确维护窗口，再在正确虚拟环境
editable reinstall，并使用一次性重启入口：

```bash
source .venv/bin/activate
python -m pip install -e .
bash scripts/restart_server_once.sh "your-authorized-operation-id"
```

最后一行中的 `your-authorized-operation-id` 是占位符，须替换为此次已获授权维护的固定 ID，
以英文字母或数字开头，仅使用英文字母、数字、下划线和短横线，最多 96 个字符，不要原样粘贴。
同一次维护无论成功、失败或被中断，都必须复用同一 ID，不能换 ID 来自动重试停服。
入口先预检，通过后才启停；`.mac/restart_operations/` 保存尝试记录与锁，
不要为重试删记录或清锁。检查失败时先只读调查，不自动调用启停脚本。

验证/生产**禁止不受限的 `uvicorn --reload`**，Agent 写项目文件会引发反复重启和数据库冲突。
确需开发热更新时只监听 `src/`。历史日期限定 watchdog 已停用，不要把它们当作常规启动入口。
详情与隐私边界见 [SECURITY.md](SECURITY.md)。

## 常见问题与排障

先检查状态再采取动作，尤其不要用反复重启掩盖问题。

| 现象 | 优先检查 | 注意事项 |
| --- | --- | --- |
| `--check` 失败 | `.env`、`.venv/bin/uvicorn`、`logging.yaml`、模板文件和端口配置 | 预检失败不应触发启停 |
| 启动后没有监听 | `server_launchctl.log`、实际端口、`launchd` 状态 | 先确认占用端口的进程身份，不强杀未知进程 |
| HTTP 正常但机器人不回复 | `feishu_ws`、应用发布/可用范围、消息事件与权限 | `/health` 的 `status=ok` 不代表飞书全链路正常 |
| 收到消息但卡片操作无效 | `card.action.trigger`、待答卡是否已提交/处理、最新日志 | 旧卡可能已结束；不要重复批准已执行动作 |
| CLI 在终端可用，服务里不可用 | 服务账号的认证、可执行路径、启动环境和出站代理 | `launchd` 不继承交互 shell 的完整 PATH；Codex 可用 `CODEX_BIN` 指定 |
| `/open` 被拒绝 | 白名单、绝对路径、Git 根目录与系统权限 | 不要为排障开放整个磁盘 |
| 项目似乎卡住 | `/info`、最新进度的最后活动、待答问题/审批、provider 错误 | 等待输入不等于挂死；必要时 `/stop`，再考虑 `/reset` |
| 重启后反复恢复同一轮 | 执行终态、卡片发送状态、最新日志与维护记录 | 已完成动作不能因结果发送失败而重新执行 |
| 管理页/控制 API 认证失败 | query token 与 bearer token 是否用对、配置是否被正确加载 | 不用清空 token 或公开监听绕过认证 |

可用的只读检查入口：

```bash
./scripts/start_server.sh --check
lsof -Pi :18080 -sTCP:LISTEN
curl --fail http://127.0.0.1:18080/health
launchctl print "gui/$(id -u)/com.mac-system.dev"
tail -n 80 server_launchctl.log
```

进一步诊断时依次核对：消息是否收到 → 当前项目/目录 → thread 与执行状态 → provider 事件
→ outbox 与飞书卡片。日志、数据库和卡片应能对应同一轮，不能只看脚本显示 `completed`。
提交问题时提供版本/分支、脱敏后的现象、时间、最小复现步骤和已做检查；不要附真实 token、
数据库或个人对话全文，也不要直接在公开 issue 粘贴敏感现场。

## 代码结构

```text
src/mac_system/
├── agents/
│   ├── orchestrator.py        项目执行生命周期与后端选择
│   ├── root_runtime.py        Codex app-server 薄客户端
│   ├── kimi_runtime.py        Kimi 项目主会话适配器
│   └── runtime.py             可选外部 coding-agent CLI
├── core/
│   ├── root_agent.py          持久 Root Thread 与公开事件投影
│   ├── root_progress.py       纯内存进度模型与展示
│   ├── root_sessions.py       会话 identity / dynamic tool 幂等
│   ├── project_service.py     项目、目录与管理历史持久化
│   ├── project_owner.py       隔离 Owner 的公开上下文与用户权威
│   └── notification_service.py  通知 outbox 与发送
├── feishu/                    WebSocket、消息、菜单与卡片
├── api/                       FastAPI、控制接口与管理页
├── utils/                     无宿主状态的 ID、用量和脱敏
├── config.py                  配置读取与默认值
├── database.py                数据库初始化与会话
└── models.py                  持久数据模型

tests/                         单元与组件集成回归
scripts/                       启动、一次性维护、验证与源码制包工具
docs/                          架构、交互、测试与发布文档
```

## 开发与验证

在独立开发环境安装 `[dev]` 后，从仓库根目录执行：

```bash
source .venv/bin/activate
python -m compileall -q src tests scripts
for script in scripts/*.sh; do bash -n "$script"; done
python -m pytest tests/ -q
git diff --check
```

`git diff --check` 仅适用于 Git checkout；从源码附件解压且没有 `.git` 时跳过这一项。
这些检查不要求为了验证而重启现有服务。涉及真实服务、飞书或模型时，应使用明确的验证环境
和凭据，不把 `FEISHU_TEST_MODE=1` 持久写进 `.env`。

测试报告使用 [T1–T9 术语](docs/testing-taxonomy.md)，不要笼统声称“全链路通过”：

| 层级 | 验证内容 |
| --- | --- |
| T1 | 编译、导入和静态架构边界 |
| T2 | 单元测试 |
| T3 | 数据库、协议、outbox 与卡片组件集成 |
| T4 | 本机真实 API、进程与重启恢复 |
| T5 | 已登录飞书 UI 中真实发送消息、点击和查看卡片 |
| T6 | 真实外部模型/provider |
| T7 | 完整新项目交付 |
| T8 | 已有目录迭代、修改和 debug |
| T9 | 生成项目的真实网页/API/文件能力与稳定性 |

FakeRuntime、注入接口、脚本终态或健康接口不能替代 T5–T9。
涉及 Codex thread、turn、steer、审批、提问或 multi-agent 时，先对照基准
`rust-v0.144.1` / `44918ea10c0f99151c6710411b4322c2f5c96bea` 的官方源码，
不自行设计相似协议。贡献规则见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 文档导航

| 文档 | 何时阅读 |
| --- | --- |
| [飞书接入](docs/feishu_onboarding.md) | 配置机器人、多账号和消息通道 |
| [架构](docs/architecture.md) | 了解 runtime、Root、Owner 与持久化职责 |
| [设计原则](docs/mac-design-principles.md) | 理解不重写 provider 执行循环等边界 |
| [交互规范](docs/interaction-spec.md) | 核对项目、消息、审批和卡片的预期行为 |
| [安全与隐私](SECURITY.md) | 部署、目录权限、凭据、历史数据和发布前检查 |
| [贡献指南](CONTRIBUTING.md) | 修改范围、安装和开发验证 |
| [测试分层](docs/testing-taxonomy.md) | 准确报告已验证内容与尚未证明的部分 |
| [长对话维护](docs/long-conversation-maintenance.md) | 处理长期会话与恢复边界 |
| [发布准备](docs/release-readiness.md) | 查看源码制包清单和冻结候选的历史证据 |
| [架构整理与复核](docs/architecture-review-20261012.md) | 查看代码整理及真实 Kimi 复核记录 |

发布与开发验收应注明对应版本，旧候选报告不能代替新版本的实际验证。
推送前扫描拟发布 tree 与 Git 历史；本地备份分支可能含旧秘密，禁止
`git push --all`、`git push --mirror`，也不要把用户项目与运行数据一起上传。

## 使用许可

MAC 项目源码与随附文档采用 [MIT License](LICENSE)，版权署名为 MAC contributors。
商用、修改与再分发须遵守许可中的版权与许可声明保留要求。
第三方依赖及外部 provider 保持各自的使用许可；本项目的 MIT 许可不替代其条款、
账户规则或模型服务费用。
