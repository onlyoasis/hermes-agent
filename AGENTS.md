# Hermes Agent 开发规则

<!-- project-knowledge-sync:start -->
## 项目资料

- 项目详情：`/Users/lzc/Projects/project-registry/docs/projects/hermes-agent.md`。需要背景、运行或发布信息时查阅相关章节，以当前源码和运行证据为准。
- 每次项目修改均同步详情的最新变更摘要与 `/Users/lzc/Projects/project-registry/docs/changes/<id>/YYYY-MM.md`；事实变化时更新对应章节，区分代码、测试、部署和线上验证。
- 所有项目统一管理，不按公司/个人区分；AgentOrg 是用户自己的 OPC 项目管理库。文档库流程见 `/Users/lzc/Projects/project-registry/docs/project-knowledge-library.md`。
- 更新发布状态时按 `/Users/lzc/Projects/project-registry/docs/project-documentation-standard.md` 的口径记录证据；仅保存非敏感事实。
<!-- project-knowledge-sync:end -->

本文件是 AGENTS.md 与 CLAUDE.md 共用的开发入口。架构、扩展方法、贡献准则及历史陷阱完整保留在 [开发手册](docs/agent-development-manual.md)，按任务检索对应章节，不默认全文加载。

## 当前现场

- 确认仓库、分支和未提交改动，保留用户工作；真实源码、测试及 runtime 高于旧文档。
- 项目状态入口：[统一详情](/Users/lzc/Projects/project-registry/docs/projects/hermes-agent.md)。改变功能、架构、运行路径或发布状态后更新相关事实，代码、测试、部署与线上结果分开记录。

## 持续约束

- 保持会话 prompt cache：会话中不改历史上下文、不换工具集、不重载记忆或重建 system prompt；上下文压缩是例外。改变提示状态的命令默认下一会话生效，即时失效须显式 `--now`。
- 核心保持精简，新增能力优先 CLI + skill、按服务启用的工具或 plugin。产品可扩展，但避免把每轮都会付费的工具 schema 无限制放大。
- 配置与身份隔离使用 `get_hermes_home()`；显示路径使用 `display_hermes_home()`。禁止在代码或测试中硬编码个人 `~/.hermes` 路径。
- Python 测试只通过 `scripts/run_tests.sh` 运行，不直接调用 pytest；它负责环境与进程隔离。测试不得写真实 Hermes home，Profile 测试同时隔离 `Path.home()` 和 `HERMES_HOME`。
- 新依赖设置上下界，Git 依赖固定 commit，Actions 固定 SHA，CI pip 固定版本；修改依赖后更新锁文件，具体规则见手册 Dependency Pinning Policy。
- 新能力接入既有 setup/config UX。跨工具集的工具名引用不能硬写在 schema 中，应按实际可用工具动态补充。
- 网关审批/控制命令必须绕过 base adapter 和 gateway runner 两层消息守卫，不能因 agent 忙碌而排队失效。
- 不以修复为名覆盖其他分支工作；发布前检查分支差异和近期修复，禁止照抄历史手册中的破坏性重置命令。

## 按任务读取

| 任务 | 开发手册章节 |
| --- | --- |
| 功能设计、贡献与 PR 评审 | What Hermes Is、Contribution Rubric |
| Agent 循环、CLI、TUI | AIAgent Class、CLI Architecture、TUI Architecture、TypeScript Style |
| 新工具、工具分组、委派 | Adding New Tools、Toolsets、Delegation |
| 新配置、主题或 Profile | Adding Configuration、Skin/Theme System、Profiles |
| 插件和技能 | Plugins、Skills、Curator |
| 定时任务、协作队列 | Cron、Kanban |
| Gateway、显示或运行故障 | Important Policies、Known Pitfalls 及相关模块章节 |
| 测试或依赖变更 | Testing、Development Environment、Dependency Pinning Policy |

## 验证

- Python 入口：`scripts/run_tests.sh <测试文件或目录>`。完整范围与前端测试按手册 Testing 及当前 CI 分类器选择。
- 重试后通过但标为 FLAKY 的测试仍需记录并处理；不要用固定目录计数、版本字面量或上游模型快照替代行为断言。
- 调用实际模块和真实解析链验证；测试数据、运行产物与缓存使用机器规则指定的隔离外盘路径。
- 架构与工作流变化更新详细手册相应章节，根文件只维护持续约束和读取入口。
