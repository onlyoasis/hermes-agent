# jev-skill-router

可选的 Jev（TypeSafe）技能推荐插件。在用户轮次开始前，用两阶段
Choice/Noul 问题判断"当前请求是否适合加载某个**已可加载**的技能"，
至多推荐一个技能，以固定模板注入**本轮**的 user message API 副本
（不改 system prompt、不进入后续轮次）。

设计蓝图：`docs/jev-skill-recommendation-plan.md`。本目录是方案 §7
D1 阶段的离线实现：全部逻辑经真实函数 + 假响应测试验证，**尚未**
接通真实 API，也未在生产 shared install 上启用。

## 安全边界（方案 §1/§3/§4 的硬约束）

- 默认双重关闭：插件未加入 `plugins.enabled` 不加载；加载后
  `mode` 默认 `off`，`off` 下不读目录、不发请求、不写记录。
- 启用必填（方案 §4「缺失则不启用」）：`mode` 为 `shadow/recommend`
  时必须显式配置有限正值 `max_input_tokens` 与 `daily_budget_usd`
  （NaN/Infinity 拒绝），且必须显式声明目录外发范围——
  `outbound_catalog` 精确 ID 列表**或** `outbound_catalog_full: true`
  二选一；缺失/冲突/非法一律降回 `off` 并记录可诊断错误。
- `shadow` 只写本地审计记录，绝不注入任何内容；`recommend` 才注入，
  且只注入固定模板 + 白名单候选 ID，绝不透传厂商自由文本。
- 零权限提升：只注册 `pre_llm_call` / `on_session_end` 两个 hook，
  不注册工具，不改变技能加载、审批或任何工具权限。
- 外发最小化：只发当前请求文本 + 技能 `id/name/description`；
  技能正文只有在 `outbound_body_skills` 显式列出时才发送截断摘录。
- 目录不完整（任一扫描根失败）→ 整轮 `unavailable`，不断言"无适用
  技能"；非法响应（未知/缺失选项/缺问题/越界数值/分布和偏离 1/
  choice 非最高概率）整体拒收。
- 预算按请求记账：每个请求发送前**单独**原子预留（跨进程文件锁），
  返回后按实际 usage 结算；阶段二预算不足即不发、不产生建议。请求
  失败或缺 usage 时按预留额保守计费；账本损坏/不可写一律 fail
  closed（拒绝新花费）。
- 每条失败路径（超时/429/529/认证失败/非法响应/预算耗尽/内部异常）
  都退回原流程，返回 `None`，不影响宿主轮次。
- 注入前终检：候选在编译后若被禁用/修改/删除，建议立即丢弃。

## 模块

| 文件 | 职责 |
| --- | --- |
| `plugin.yaml` | standalone 插件清单，声明两个 hook |
| `catalog.py` | 编译当前 Profile 可加载技能目录（复用 `agent.skill_utils` 的禁用/平台/环境过滤与扫描优先级）；外发子集；正文摘录；注入前终检 |
| `questions.py` | 两阶段问题定义（`skill-routing-v1`）与答案整体校验、短名单排序 |
| `client.py` | TypeSafe `/v1/systemone` 薄适配器：单次 POST、无自动重试、类型化错误、结构校验、transport seam |
| `policy.py` | 配置读取与校验（坏字段降级为安全默认 + 可诊断日志）、轮次闸门（表面/去重）、每日预算账本（逐请求 reserve/settle、跨进程文件锁、fail closed）、建议契约校验、审计记录 |
| `__init__.py` | `register()` 只挂 hook；编排：闸门→目录→显式技能检查→单请求模式闸门→两阶段（每阶段独立预留/结算，deadline 切分 60%/剩余）→阈值判定→终检→注入或只记录 |

## 启用与配置

config.yaml（当前 Profile）：

```yaml
plugins:
  enabled:
    - jev-skill-router        # 不加则插件根本不加载
  entries:
    jev-skill-router:
      mode: shadow            # off | shadow | recommend
      allowed_platforms: [telegram]   # 空 = 禁止所有表面
      # api_key_env: TYPESAFE_API_KEY
      # model: jev-1.13.0
      # base_url: https://api.typesafe.ai
      # shortlist_size: 3
      # total_deadline_ms: 2000
      # max_requests_per_turn: 2      # < 2 时整轮跳过（v1 无单阶段判定路径）
      # ── 以下三项为 shadow/recommend 启用必填，缺失/非法（含 NaN/Infinity）
      #    则保持 off（方案 §4「缺失则不启用」）：
      max_input_tokens: 20000        # 外发体积上限（字节级保守估算，硬上界）
      daily_budget_usd: 0.5          # 每日预算（reserve/settle 账本）
      # ── 目录外发范围必须显式声明，二选一：
      outbound_catalog_full: true    # 显式允许完整目录；换用下行则为精确子集
      # outbound_catalog: [a, b]      # 精确列出获准外发元数据的技能 ID（互斥）
      # outbound_body_skills: [a]     # 默认从不外发正文（独立白名单）
      # body_excerpt_chars: 600
      # min_any_match: 0.5   # 以下三个阈值为未校准初始值，冻结前需 D2 评测
      # min_fit: 0.5
      # min_choice_prob: 0.5
```

API key 只从环境变量（默认 `TYPESAFE_API_KEY`）读取，不写入
config、日志或审计。显式启用但缺 key：每轮跳过并记录
`missing_api_key`，日志给出可诊断告警。

表面判定只用宿主标记（gateway `HERMES_SESSION_PLATFORM`、
`HERMES_SESSION_SOURCE`、`HERMES_CRON_SESSION`、hook 的
`parent_session_id`），绝不做文本猜身份：cron/kanban/tool/scheduler
来源、委派子代理、缺 session/turn id、平台不在允许集 → 全部跳过。
v1 仅覆盖 Gateway 平台表面；CLI 与 approval/控制命令是否进入同一路径
留待 D3 在 shared install 实证后再放开。

## 数据落盘

均在当前 Profile home（`get_hermes_home()`）下：

- `jev-skill-router/decisions.jsonl` — 每轮一条审计：请求 ID、
  profile、模式、模型、问题版本、目录修订、候选 ID、状态/原因码、
  耗时、usage。**不含**用户原文、技能正文、密钥。
- `jev-skill-router/usage/YYYY-MM-DD.json` — 每日预算账本
  （reserved/spent/requests）。**每个请求**发送前保守预留：完整
  请求体（state+questions 序列化 JSON）的 **UTF-8 字节数 + 1024
  tokens 固定协议余量**——字节级 BPE 每 token 至少 1 字节，字节数
  是语言无关的 token 硬上界（代码、随机 ID、生僻文字均不会击穿）；
  返回后按实际 usage 结算，请求失败或 usage 缺失/非法时按预留额
  保守计费。
  读改写经 usage 目录下的 `.ledger.lock` 建议性文件锁串行化
  （跨进程/线程，对齐 `gateway/status.py` 惯例）；账本损坏或不可写
  时拒绝新预留（fail closed），落盘值按 12 位小数舍入使
  reserve/settle 配对精确归零。

## 决策状态

- `suggested` — 两阶段通过全部阈值（含 Choice 分布整体校验与
  argmax 一致性）且终检通过。
- `abstain` — 模型明确表示不需要技能（`none` 胜出或低于阈值）。
- `skipped` — 确定性跳过（mode/表面/去重/显式提及/预算/超体积/
  `max_requests_per_turn<2` 等），零外发零花费。
- `unavailable` — 无法下结论（目录不完整、服务错误、非法响应），
  **不得**当作"无适用技能"。

## 测试

```bash
scripts/run_tests.sh tests/plugins/test_jev_skill_router.py
```

109 个用例：目录编译/过滤/不完整/终检、两阶段问题与 Choice 分布
整体拒收（缺选项/和偏离 1/choice 非 argmax，并列 argmax 放行）、
client 错误映射与单次调用、usage 非法拒收、配置降级、闸门顺序、
预算账本（逐请求 reserve/settle、并发不超上限、损坏/不可写 fail
closed、失败保守计费、浮点精确归零）、建议契约、off/shadow/
recommend 端到端（假响应）、各失败路径回退、阶段二预算不足不发、
单请求模式整轮跳过、真实 PluginManager 发现路径注册与 hook 调用；
边界返修新增——启用必填 fail closed（缺限/预算/NaN/Infinity 保持
off）、目录外发声明（未声明/互斥冲突/非布尔拒绝，子集保持
coverage 语义）、字节级保守估算（纯 ASCII/高 CJK/混合/空载荷/
同字节同预留）与 `max_input_tokens` 边界（恰好放行、超 1 拒发）、
manifest 不误署他人。
测试隔离 `HERMES_HOME`，全程零网络。

## 未完成项（后续阶段）

- **D2 评测**：阈值 `min_any_match/min_fit/min_choice_prob` 是未校准
  初始值（0.5），需用真实 API + 本地样本做校准后才可冻结；未校准
  前不建议把 `mode` 开到 `recommend`。
- **D3 shared install 实证**：gateway 审批/控制命令是否产生用户轮次、
  各平台表面标记的真实取值，需在 shared install 环境验证后再扩
  `allowed_platforms`；本仓库不做生产接入。
- **真实 API 联调**：`client.py` 的 default transport 走真实网络前，
  需要外发授权与凭据；当前只有离线假响应验证。
- CLI 表面、跨轮缓存（方案明确 v1 不做）。
