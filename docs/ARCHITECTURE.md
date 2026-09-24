# Jev Spire Brain 架构说明

本文描述当前仓库中的实际实现（2026-09-24），不是未来功能清单。项目的目标是为《杀戮尖塔》提供游戏内实时建议：玩家继续操作，agent 只在 `advise` 模式观察状态并给出当前一步建议。

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
       -> JEV tactical client（候选内局部判断）
       -> check_action() / CommunicationMod 命令
  -> overlay feed（游戏内面板）和 dashboard（浏览器）
```

模型不能直接生成游戏命令。最终命令必须来自本地候选，并通过合法性检查；硬规则优先级高于 GPT 意图，GPT 意图高于 JEV 局部排序，最后才是本地兜底。

## 2. 主要边界

### 状态边界

`spirebrain/driver/live_state.py` 的 `GameSnapshot.from_communication()` 是 CommunicationMod 原始字典进入决策层的唯一归一化入口。它补齐角色、战斗屏幕和稳定 `state_id`，并保留原始字段供兼容代码使用。`run_id` 变化或新局/死亡/通关时，`RunMemory` 清空。

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

GPT 是战略层：在新局、换幕、地图、奖励、商店、精英/Boss、构筑或资源发生重大变化时生成最多 2-5 步的短计划。普通战斗中的每张牌不重复请求 GPT，使用仍有效的计划和本地战术层。

JEV 是战术层：只能在本地已确认合法、且符合战略约束的候选中选择或评分，返回候选 ID、置信度和短判断。JEV 不生成自由格式命令。

`GuideBook` 和 `check_action()` 是硬约束：角色未覆盖、资源不足、无效目标、药水槽已满、明确危险动作等情况直接过滤或降级。GPT 超时、无 Key、无效 JSON、断网时保留缓存计划或退回 JEV+规则；两者都不可用时只显示状态不足，并在 `advise` 模式发送 `wait/state`。

## 4. 游戏内建议与浏览器面板

游戏内面板始终显示当前一步、战略目标、简短原因、来源和合法备用建议。建议状态绑定当前 `state_id`/`plan_id`，旧异步结果不能覆盖新状态。浏览器面板在此基础上展示候选、过滤原因、决策链、provider 延迟、fallback 和玩家采纳记录。完整隐藏思维链不会展示。

## 5. 评测与可行性

当前项目已经具备成为《杀戮尖塔》专用 agent 的工程基础：状态归一化、候选动作、规则约束、双层模型、异步降级、游戏内反馈和结构化轨迹均有明确接口。首版只承诺铁甲战士；其他角色会显示覆盖不足，不套用铁甲战士规则。

回放和单元测试可以衡量建议合法率、场景覆盖、采纳率、JEV/GPT fallback 率、不确定率和决策延迟，但这些指标不等于胜率。只有在相同难度和成对种子的真实对照实验中完成至少 100 对局并报告 95% 置信区间后，才能宣称胜率变化；在此之前只能报告建议质量指标。

## 6. 当前已知边界与后续顺序

1. `stdio.py` 仍同时承担传输生命周期和部分兼容路由；后续可按行为边界拆出 transport/guards/modes，但必须保持现有协议和 `advise` 约束。
2. Python、网页和 Java overlay 的决策点标签还没有完全收敛到单一生成文件，需要契约测试防止漂移。
3. `gamedata.py`、真实 provider、`analysis/*` 的直接测试仍需补齐，尤其是超时、HTTP 错误、无效 JSON 和缓存降级。
4. `deck_policy.synergy_weight` 已删除，因为仓库没有读取点；流派协同规则继续由 `spirebrain/cards/knowledge.py` 管理，避免保留失效配置。
5. 需要建立固定场景回放集，再根据最弱场景改进战斗目标、商店预算和未知卡牌效果模型；不能只凭单局体验调整策略。

## 7. 兼容契约

- 默认模式是 `advise`，实际发送给游戏的命令严格限制为 `wait` 或 `state`。
- 旧的 JEV-only 后端仍可通过 `brain.backend=jev` 或启动参数使用。
- CommunicationMod 原始字典、stdio 命令协议和旧测试入口保持兼容。
- 所有模型结果都必须匹配当前状态代际；过期异步结果直接丢弃。
