# Jev Spire Brain 架构说明

本文描述当前仓库中的实际实现（2026-09-30），不是未来功能清单。项目的目标是为《杀戮尖塔》提供游戏内实时建议：玩家继续操作，agent 只在 `advise` 模式观察状态并给出当前一步建议。

## 1. 运行链路

```text
CommunicationMod
  -> stdio 兼容层
  -> GameSnapshot / normalize_game_state
  -> SpireBrainAgent
       -> GuideBook 硬规则与角色覆盖检查
       -> ActionBroker 合法候选和过滤诊断
       -> 场景处理器（地图、选牌、事件、火堆、商店、战斗、遗物）
       -> StrategicOrchestrator（GPT 战略计划，异步/缓存）
          -> offline strategy RAG（来源引用的铁甲战士软证据）
       -> JEV tactical client（候选内局部判断）
       -> check_action() / CommunicationMod 命令
  -> overlay feed（游戏内面板）和 dashboard（浏览器）
```

模型不能直接生成游戏命令。最终命令必须来自本地候选，并通过合法性检查；硬规则优先级高于 GPT 意图，GPT 意图高于 JEV 局部排序，最后才是本地兜底。

## 2. 主要边界

### 状态边界

`spirebrain/driver/live_state.py` 的 `GameSnapshot.from_communication()` 是 CommunicationMod 原始字典进入决策层的唯一归一化入口。它补齐角色、战斗屏幕和稳定 `state_id`，并保留原始字段供兼容代码使用。`run_id` 变化或新局/死亡/通关时，`RunMemory` 清空。
菜单到新局的边界由 `SpireBrainAgent.begin_new_run()` 统一清理；它同时推进
`run_epoch`、清空计划/场景临时状态并使旧异步任务失效，即使新局复用了相同
seed 或 `run_id` 也不会继承旧记忆。

### 配置边界

`spirebrain/runtime_config.py` 提供统一解析结果，所有启动器、agent、doctor 和 dashboard 都使用它。优先级固定为：

```text
命令行 > 进程环境变量 > 项目 .env > config/strategy.json > 默认值
```

配置结果携带来源和 `config_id` 指纹。API Key 只从环境变量或 `.env` 读取，不能进入 public config、决策轨迹或普通日志。dashboard 显示实际生效的 JEV/GPT 后端、来源和指纹，保存后提示重启 agent。

### 场景边界

`spirebrain/driver/scenes.py` 的 `SceneRouter` 统一屏幕到处理器的映射，并保留 GRID/CARD_SELECT/HAND_SELECT 兼容判断。场景处理器负责生成该场景的语义候选，不负责绕过统一执行校验。

### 决策证据边界

`spirebrain/driver/trace.py` 写入 `logs/decision_trace.jsonl`。每条记录包含 schema 版本、`run_id/state_id/plan_id`、场景、规则 ID、候选和过滤原因、provider、延迟、最终候选、来源、兜底、不确定性及玩家实际行动。记录只保存结构化短理由，不保存隐藏思维链、完整请求或密钥；写入失败不会阻塞游戏。

## 3. GPT、JEV 与规则的职责

### 实机阶段模式：JEV-only + 局后战略复盘

对于需要稳定实时响应的实机运行，推荐使用阶段化模式。启动命令加入
`--jev-only` 后，live `stdio` 会把战略规划器设为 `planning_enabled=False`：
游戏过程中只运行本地规则、合法候选和 JEV 局部判断，战略 provider 请求数必须为
零；JEV 仍然可以在当前合法候选内工作。`advise` 仍是默认模式，发往游戏的命令
仍严格限制为 `wait/state`。离线 replay 会显式标记为 `synthetic`，不会被当作实机局。

```text
实机状态 -> 规则/候选 -> JEV -> check_action -> 建议与 trace
                                                |
                                                v
                                  run_ended 后显式局后复盘
                                  -> pending experience
                                  -> 人工审核
                                  -> fixed-response replay
                                  -> active soft preference
```

可以用下面的入口安装或预览该模式（只有显式 `--setup` 才会写入外部
CommunicationMod 配置）：

```text
python start.py --jev-only --advise --no-browser
python start.py --jev-only --advise --setup
# optional hybrid mode; strategic requests run only at explicit phase checkpoints
python start.py --strategic-online --advise --no-browser
python start.py --strategic-online --advise --setup
```

实机运行结束后，先按 `run_id` 和 `run_epoch` 检查完整轨迹；只有同一代同时存在
真实 `run_started`、带 `victory/death/game_over` 标签的 `run_ended`，复盘才会被标为
`real_game` 并允许生成经验提案。仅退回菜单或中途退出的局仍可查看，但结果保持
`unknown`，不能进入经验晋升：

```text
python -m spirebrain.analysis.postmortem --input logs/decision_trace.jsonl \
  --run-id <run_id> --run-epoch <run_epoch>
python -m spirebrain.analysis.postmortem --input logs/decision_trace.jsonl \
  --run-id <run_id> --run-epoch <run_epoch> --propose-experiences \
  --ledger logs/strategy_experiences.jsonl
```

`--propose-experiences` 是唯一的局后战略模型入口，一次复盘最多发起一次请求；
没有完整实机生命周期、密钥或有效响应时会失败而不生成经验。提案默认是
`pending_review`，必须经过人工批准、带 SHA-256 的固定响应回放且确认零非法动作、
零候选集变化和零硬约束违规后才能 `active`。激活内容只作为脱敏、限长的 JEV
软提示，绝不会改变候选集合、合法性或生成游戏命令；可随时撤回。

在兼容的 hybrid 运行中，GPT 仍是阶段战略服务：在新局/换幕、路线与构筑选择、进入商店、战斗遭遇开始、敌群变化或 HP 进入危险区等战略检查点生成最多 2-5 步的短计划。同一楼层商店购买导致的金币/货架变化不会重复请求 GPT；当前策略继续指导余下选择，候选签名变化则使旧的具体商品偏好失效。战斗中的手牌、能量、目标或动画变化由本地战术层及时处理，不因 `expires_after` 的动作计数而再次请求 GPT。相同阶段的模型失败有界冷却，新的战略检查点仍可立即重试。实机阶段化模式通过 `--jev-only` 关闭这条在线路径，把同一战略 provider 的一次调用推迟到局后复盘。

具体候选引用仍绑定生成计划时的实体签名；跨状态只允许保留能匹配当前候选签名的偏好，战略目标与资源约束可在同一幕内复用。`advise` 的异步动作若已过期会丢弃；同局同幕的战略计划可单独提交，并先按新状态重绑候选，因此慢模型不能发布旧动作，也不必把仍有用的战略目标一并丢掉。

`advise` 的主循环是阶段服务的唯一 owner：它在状态轮询边界调用 `prepare_advice_strategy()`，由 `StrategicOrchestrator` 判断是否到达新战略检查点、排队请求并提交已完成的计划。后台建议 clone 收到冻结的 `PhaseStrategySnapshot`，其战略规划开关关闭，只能基于该快照运行规则、战斗战术和 JEV；它不会因为手牌/能量变化自行再发 GPT 请求。GPT 计划在仍适用的阶段返回后，owner 重新绑定当前合法候选，并让最新状态再计算一次建议；原建议动作仍按状态代际失效。计划尚未返回时，快速规则建议和战术 worker 不等待 GPT 网络请求。

战略请求会附带一个本地、可复现的 `knowledge_context`。它来自
`spirebrain/data/strategy_knowledge.jsonl` 的短篇来源引用断言，当前只覆盖铁甲战士；
检索使用标准库 lexical TF-IDF-like 评分，不在游戏运行时联网。RAG 只能提供流派、路线和
资源分配的软证据及带来源的基础机制事实，不能提供卡牌具体伤害推算、协议命令或候选 ID；
本机游戏数据和硬规则优先级更高。每条知识保留游戏范围、claim 类型、许可、适用条件、例外
和内容哈希；Wiki 机制只作为需由本机版本继续校验的证据。

战略 provider 按 endpoint 后缀使用对应协议：官方 `/v1/responses` 使用 Responses
请求体，OpenAI-compatible `/chat/completions` 使用 Chat Completions。Dashboard 的测试按钮
复用同一客户端并验证结构化战略计划；HTTP 200 但无效响应会显示失败。该探针会发送一次真实
模型请求，可能计费，且只证明该配置能返回计划，不代表当前游戏阶段、Agent 截止时间或候选绑定均已验证。

JEV 是战术层：只能在本地已确认合法、且符合战略约束的候选中选择或评分，返回候选 ID、置信度和短判断。JEV 不生成自由格式命令。

`GuideBook` 和 `check_action()` 是硬约束：角色未覆盖、资源不足、无效目标、药水槽已满、明确危险动作等情况直接过滤或降级。GPT 超时、无 Key、无效 JSON、断网时保留缓存计划或退回 JEV+规则；两者都不可用时只显示状态不足，并在 `advise` 模式发送 `wait/state`。

## 4. 游戏内建议与浏览器面板

游戏内面板始终显示当前一步、战略目标、简短原因、来源和合法备用建议。建议状态绑定当前 `state_id`/`plan_id`，旧异步结果不能覆盖新状态。浏览器面板在此基础上展示候选、过滤原因、决策链、provider 延迟、fallback 和玩家采纳记录。完整隐藏思维链不会展示。

## 5. 评测与可行性

当前项目已经具备成为《杀戮尖塔》专用 agent 的工程基础：状态归一化、候选动作、规则约束、双层模型、异步降级、游戏内反馈和结构化轨迹均有明确接口。首版只承诺铁甲战士；其他角色会显示覆盖不足，不套用铁甲战士规则。

回放和单元测试可以衡量建议合法率、场景覆盖、采纳率、JEV/GPT fallback 率、不确定率和决策延迟，但这些指标不等于胜率。只有在相同难度和成对种子的真实对照实验中完成至少 100 对局并报告 95% 置信区间后，才能宣称胜率变化；在此之前只能报告建议质量指标。

## 6. 外部架构参考

本节依据 2026-09-29 查阅的公开代码与 README，仅借鉴职责划分和评测方法，不把外部结果视为本项目验证。

- [AttemorySystem/spire-agent](https://github.com/AttemorySystem/spire-agent/tree/8fcaa767479f62aa2528d2e9be260de97172c6b0)：其 `GameAgent` 只执行固定的状态→路由→决策→校验→执行事务；地图、构筑、战斗由分开的 agent/tool 负责，并记录可回放运行。借鉴宏观战略与本地专用战术分离、校验留在本地、慢任务的结果需可追踪。它包含独立的模拟器和 Mod 补丁，不适合直接移植到当前 CommunicationMod 助手。
- [Jialeiv/sts-rl-agent](https://github.com/Jialeiv/sts-rl-agent/tree/039c199933b7d3a2e5d8fde51ae77a9175e1031f)：把非战斗构筑策略与模拟器 MCTS 战斗控制分开，并报告固定 50 个用于验证/评估的 A0 铁甲战士 seeds；该集合不是未触碰的最终测试集，作者也记录了多种战斗策略实验的负结果。借鉴“按决策时间尺度选策略”和固定输入比较，不移植其依赖 `sts_lightspeed` 的网络/MCTS，也不引用其指标作为本项目效果。
- [AlayaLab/AgenticSTS](https://github.com/AlayaLab/AgenticSTS/tree/e93a7b166b0c4b8721bd802d1e9d6731eecbffcf)：README 的分层与路由说明（[L66-L76](https://github.com/AlayaLab/AgenticSTS/blob/e93a7b166b0c4b8721bd802d1e9d6731eecbffcf/README.md#L66-L76)）把触发式战略指南和高频战斗处理区分开；`v2_engine.py` 的 `_get_v2_tier()`（[L423-L470](https://github.com/AlayaLab/AgenticSTS/blob/e93a7b166b0c4b8721bd802d1e9d6731eecbffcf/AgenticSTS/src/brain/v2_engine.py#L423-L470)）按是否简单战斗或重规划选择模型层级。该项目面向《杀戮尖塔 2》，其 README 报告的每局调用量未经本项目独立复现；只借鉴显式触发与快慢层路由，不引入多级 provider、自动技能写入或外部游戏数据。

因此，本项目保留现有 GPT/JEV/规则与 CombatTacticalEngine，只把 GPT 请求约束到战略检查点；当前游戏动作仍由场景候选、JEV/本地战术与 `check_action()` 处理。回归测试证明调用频率与过期结果的行为契约，不证明实际游戏胜率提升。

这一划分适用于本项目的延迟约束：战略计划是有作用范围和失效条件的阶段服务，普通轮询与战斗出牌由本地快速层响应。异步计划尚未返回时，当前合法候选仍能产生规则建议；计划到达后只提交同一运行中仍适用的战略目标和可按签名重新绑定的偏好。这样降低逐动作网络调用的依赖，但不把慢模型响应误当成实时控制，也不保证其内容正确。

## 7. 当前已知边界与后续顺序

1. `stdio.py` 仍同时承担传输生命周期和部分兼容路由；后续可按行为边界拆出 transport/guards/modes，但必须保持现有协议和 `advise` 约束。
2. Python、网页和 Java overlay 目前通过共享 JSON fixture 和静态消费者检查约束字段、空值、默认值与枚举；Java 游戏依赖缺失时仍不会伪造编译通过。
3. `gamedata.py` 和真实 provider 的真机测试仍需补齐；离线 provider、超时、HTTP 错误、无效 JSON、缓存降级和 RAG 检索已有 fixture 覆盖。
4. `deck_policy.synergy_weight` 已删除，因为仓库没有读取点；流派协同规则继续由 `spirebrain/cards/knowledge.py` 管理，避免保留失效配置。
5. 战斗搜索已覆盖可见确定性卡牌×目标分支，但力量、虚弱、复杂 Power、随机效果和遗物链仍会降级；需要继续用固定回放集评估目标、商店预算和未知效果模型。

## 8. 兼容契约

- 默认模式是 `advise`，实际发送给游戏的命令严格限制为 `wait` 或 `state`。
- 旧的 JEV-only 后端仍可通过 `brain.backend=jev` 或启动参数使用。
- CommunicationMod 原始字典、stdio 命令协议和旧测试入口保持兼容。
- 所有模型结果都必须匹配当前状态代际；过期异步结果直接丢弃。

## 9. 离线回放与指标

机器相关路径通过 `spirebrain.environment.EnvironmentAdapter` 注入。它承载
`LOCALAPPDATA`/`APPDATA`、`STS_GAME_DIR` 和 Steam 根目录；生产调用默认读取
当前进程环境，离线测试显式传入临时目录，因此不需要开发者的游戏安装。卡牌
本地化回放使用 `tests/fixtures/gamedata/localization.json`。

`python tools/verify_offline.py` 是 CI 与干净检出的独立检查入口：它分别运行
测试、Python 编译、跨语言静态契约、合成指标、四模式评测、回放和本地策略 RAG 检查，即使前一项失败也会继续
输出后续结果。Java 构建仍需本地游戏依赖，CI 不伪造构建通过。

`python -m spirebrain.analysis.replay --input tests/fixtures/replay` 将合成
CommunicationMod 消息送入真实 stdio/advisor 管道，强制使用本地 mock，检查
`advise` 是否只输出 `wait`/`state`，并报告建议与规则回退数量。fixture 明确标记
为 `synthetic`，不能当作真实玩家记录。

`python -m spirebrain.analysis.metrics --input <trace.jsonl>` 只读取 JSONL，输出
场景覆盖、合法率、模型请求数、回退率及生成/发布延迟 p50/p95。缺失输入会返回
非零退出码；没有真实 `win/loss/death` 标签时结果为 `unknown`，不会从采纳率、
模拟伤害或合成 HP 推断胜率。决策事件使用 `decision_id`，模型请求使用
`request_id`，两者不可混计。
# Runtime decision boundaries

## Unified trace and replay

Decision evidence uses schema version 2. Every event has nullable `run_id`,
`run_epoch`, `decision_id`, `state_id`, `request_id`, `plan_id`,
`advice_revision`, `config_id`, `event_type`, and `timestamp` fields; the old
`ts` field remains for compatibility. `DecisionTrace.find_decision()` rebuilds
the input, provider request/response, decision, advice revisions, expiry and
outcome groups for one decision ID. GPT and JEV logging adapters emit
`provider_request` and `provider_response` events into this same trace while
retaining their legacy JSONL files.

Offline replay uses fixed structured responses and synthetic fault fixtures. The
four-mode evaluator (`rules`, `jev`, `strategic`, `full`) runs distinct local
stacks with countable scripted providers; it never calls a real provider.
Adoption and real game outcomes remain unknown unless explicitly observed.
For one trace decision, `python -m spirebrain.analysis.replay --input
<decision_trace.jsonl> --decision-id <id>` verifies the content-addressed
evidence; adding `--execute` rehydrates the owner-loop state and executable
candidate blob, reruns local reconciliation and legality checks, and performs
zero provider calls. Missing decisions, state blobs or candidate blobs fail
instead of producing an empty success report.

The CommunicationMod payload is normalized into a semantic state projection in
`spirebrain/driver/decision_state.py`. `state_id` identifies the game state;
`recommendation_key` additionally includes the legal candidate view and is used
only to suppress duplicate advice. Timestamps, animation counters, UUIDs and
transport metadata do not trigger a new recommendation.

Candidates retain their legacy index-shaped `candidate_id` for protocol
compatibility, but also carry a stable `candidate_signature`. Strategic plans
bind preferences to signatures at generation time; a changed hand, target or
shop shelf therefore falls back to current legal candidates instead of reusing
an index with a new meaning.

Each decision has a shared `DecisionBudget` (tracked project config uses
`decision_budget_ms=12000`, `max_model_calls=4`; environment overrides may
lower it). GPT and JEV adapters reserve remaining time before
network I/O, so a later tactical call cannot inherit an exhausted deadline.
The real OpenRouter JEV client retains a 2500 ms provider-local ceiling and at
most one retry. Authentication failures stop immediately. Timeout, HTTP,
rate-limit, parse/validation, network and budget-exhaustion categories are
recorded without credentials. Java overlay compilation is a local-only check
when game JARs are installed; CI runs the static JSON contract checker instead.

Dashboard publication is a side channel: SSE subscribers have bounded queues
and the cross-process `BridgeFeed` uses one bounded worker queue. Slow or
unavailable browsers therefore cannot block the CommunicationMod loop. Provider
logs, traces, feed journals and in-process history catch write failures and
apply bounded retention where applicable.

# Decision finalization and evidence

The router has one finalization boundary, `SpireBrainAgent._finalize_and_record()`.
It performs strategic arbitration and the final `check_action()` call, then
creates a `FinalDecision`. `_record()` only serializes that object to history,
trace and overlay; it does not plan, replace commands or infer facts.

`RunSession` is mutable live state, while `DecisionContext` captures an
immutable `RunSessionSnapshot` (`run_id`, `run_epoch`, seed and mode). This
prevents a later run from rewriting an earlier decision's identity. The same
decision carries `state_id`, `decision_id`, `request_id`, `plan_id` and
`advice_revision` for replay joins.

Confidence is layered and nullable: provider/model confidence (when a provider
actually supplies one), local/JEV confidence, and `selection_basis` are emitted
separately. A rule fallback does not get a fabricated combined probability.
Combat evidence is structured in `combat_facts` (for example
`lethal_confirmed`, target identity and incoming damage); risk handling never
parses the Chinese display reason to decide whether a kill is safe.

# Run memory event boundary

`RunMemory` is owned by the main game loop. Its mutable state is updated through
the normalized state observation and typed event methods; strategic workers get
an immutable `RunMemorySnapshot` and return only a proposal/response. The main
loop checks `run_id`, `run_epoch`, `state_id`, generation and candidate validity
before committing a plan or publishing it.

The event kinds are intentionally separate:

| kind | meaning |
| --- | --- |
| `advice_history` | a recommendation was generated/displayed |
| `execution_attempt` | a command reached the CommunicationMod wire |
| `fact_memory` | a player action was inferred from a later state |
| `result_record` | a game result such as victory/death was observed |
| `deviation_record` | observed action differs from the advice, with evidence |
| `unobserved_record` | the state transition cannot identify an action |

`reason` remains a human-readable decision explanation and is never stored as a
`result`. A player deviation preserves the observed state and evidence before
invalidating the strategic plan. There is at most one running worker and one
latest pending task; uncancellable requests may finish, but their epoch/key
checks prevent them from changing the current panel, memory or plan.
