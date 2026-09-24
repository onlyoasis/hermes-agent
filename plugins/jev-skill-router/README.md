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
| `__init__.py` | `register()` 只挂 hook；编排：闸门→目录→显式技能检查→单请求模式闸门→两阶段（每阶段独立预留/结算，deadline 切分 60%/剩余）→阈值判定→终检→注入或只记录；`_run_two_phase` 的 `client_factory` 接缝（默认真实 JevClient，评测注入 scripted 应答者，其余路径不变）；审计记录含逐调用 `usage_all` |
| `evaluate.py` | 可复现离线评测 harness（方案 §6-§7 D2）：仓库外中文金标案例加载/校验（split×origin、改写组不得跨 split、金标 ID 必须在冻结目录内）、8 类互斥结果标签（分母神圣）、Wilson 95% CI、nearest-rank 百分位、确定性关键词基线臂、scripted/live 双应答者（live 需 `--acknowledge-cost` + 凭据） |
| `scripts/jev_skill_eval.py`（仓库根） | CLI 入口：`--cases/--responses/--skills-dir/--out-dir`，scripted 自检或显式 live 真实调用 |

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
scripts/run_tests.sh tests/plugins/test_jev_skill_evaluate.py
```

router 109 个用例：目录编译/过滤/不完整/终检、两阶段问题与 Choice
分布整体拒收（缺选项/和偏离 1/choice 非 argmax，并列 argmax 放行）、
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
evaluate 34 个用例：案例加载与完整性（split/origin/重复
case_id/kind 金标约束/改写组跨 split 拒绝）、响应加载（未知 case
id / 非 JevError 错误名拒绝）、8 标签分类矩阵（含 boundary_violation
最高优先）、Wilson CI 与分母保持聚合、关键词基线（显式提及闸门/
bigram 命中/min_score/字母序平局）、scripted 端到端（真实
`_route_turn` 管线 + client 接缝注入，逐 split 精确计数、17 次结算
账本、输出无用户文本、两次运行标签一致、split 过滤）、失败诚实
（缺 scripted 响应 → runner_error 保留分母）与 live 双守卫（缺
acknowledgement 或凭据在任何目录创建前拒绝）。
测试隔离 `HERMES_HOME`，全程零网络。

## 离线评测（方案 §6-§7，D2 harness）

```bash
# synthetic 自检（仓库内 fixtures，仅验证 harness 本身）：
python scripts/jev_skill_eval.py \
  --cases tests/plugins/jev_eval_fixtures/cases.json \
  --responses tests/plugins/jev_eval_fixtures/responses.json \
  --skills-dir tests/plugins/jev_eval_fixtures/skills \
  --out-dir <外盘路径>/eval-syn

# 真实 D2 金标集（案例文件放仓库外，仅传路径）：
python scripts/jev_skill_eval.py \
  --cases /path/to/jev-cases-300.json \
  --skills-dir /path/to/frozen-skills \
  --out-dir <外盘路径>/eval-d2 --responder live --acknowledge-cost
```

- 案例文件 schema `jev-skill-eval-cases-v1`：每条含
  `case_id/group_id/split(dev|threshold_validation|final_test)/
  origin(natural|synthetic)/user_message/expectation
  (kind=recommend|no_skill|explicit_skill|unsupported + skill_id +
  forbidden_skill_ids)`；同义改写组不得跨 split（防近重复泄漏），
  recommend/explicit 金标必须存在于冻结评测目录。
- 三臂设计：**A 现有 Agent** 离线不可观测 → 报告固定
  `unverified`，绝不模拟为通过；**B 关键词基线** 确定性本地算法
  （ASCII 词 + CJK bigram 对 id/name/description，同一显式提及
  闸门，字母序平局）；**C Jev 路由** 驱动真实生产 `_route_turn`
  管线（隔离 HERMES_HOME、shadow 配置、client 接缝注入应答者）。
- 8 类互斥标签（分母神圣）：`correct / wrong_load / extra_load /
  missed / unavailable_failure / infra_skipped / boundary_violation
  （最高优先，零容忍）/ runner_error`；逐 split×origin 分组（空组
  显式 denominator=0，合成样本绝不并入自然样本）；准确率附
  Wilson 95% CI；延迟 nearest-rank p50/p95/max（全部案例与发送
  路径子集）。
- 计量复用生产记账：calls/cost 用 BudgetLedger 日账本快照差值，
  tokens 用审计记录新增的逐调用 `usage_all`；scripted 模式
  `latency_valid/usage_valid/cost_valid=false` 并注明"仅验证管线，
  非模型质量证据"。
- `--responder live` 为独立显式入口：必须同时
  `--acknowledge-cost` 且 API key 环境变量存在，缺任一在任何输出
  目录创建之前拒绝；绝不混入默认单元测试。
- 报告 `report.json` 含 git rev、插件版本、问题版本、目录修订
  （n4-…）、阈值/预算、输入文件 SHA256、技能清单与最终账本；
  `cases.jsonl` 逐案例结果；两者均不含用户原文。

## 未完成项（后续阶段）

- **D2 真实评测数据**：harness 已就绪，但尚无 300 条自然中文金标
  样本（需用户真实案例脱敏 + 独立人工审阅金标）；阈值
  `min_any_match/min_fit/min_choice_prob` 是未校准初始值（0.5），
  需用真实 API + 样本校准后才可冻结；未校准前不建议把 `mode` 开到
  `recommend`。A 臂（现有主 Agent 行为）证据也需 D2 阶段采集。
- **D3 shared install 实证**：gateway 审批/控制命令是否产生用户轮次、
  各平台表面标记的真实取值，需在 shared install 环境验证后再扩
  `allowed_platforms`；本仓库不做生产接入。
- **真实 API 联调**：`client.py` 的 default transport 走真实网络前，
  需要外发授权与凭据；当前只有离线假响应验证。
- CLI 表面、跨轮缓存（方案明确 v1 不做）。
