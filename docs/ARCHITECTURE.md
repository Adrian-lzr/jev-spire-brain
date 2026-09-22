# 架构与代码质量：现状测量 + 重构计划

> 建档 2026-09-22。本文是**测量与计划**，不是宣传：每个数字都在本机跑出来过，
> 每条改造都写了验收方式。没做的部分明确标为"未做"。

## 1. 现状测量（2026-09-22）

### 1.1 体量

| 区域 | 行数 | 文件数 | 说明 |
|---|---|---|---|
| `driver/` | 2391 | 3 | 传输 + 路由 + 玩家识别 |
| `jev_brain/` | 2214 | 7 | 大脑接口 + 状态 + 决策点 |
| `overlay/` | 898 | 4 | 面板服务与网页 |
| `tactical/` | **125** | 3 | 战斗出牌 + HP 预算——**过薄** |
| `cards/` | 新增 | 4 | 卡牌知识层（本次新增） |
| `tests/` | 4597 | 19 | 281 个测试函数 |

单文件最大的四个：`stdio.py` 1147、`decisions.py` 723（含本次新增）、
`witness.py` 713、`doctor.py` 663。

### 1.2 已确认的坏味道（按危害排序）

| # | 问题 | 证据 | 危害 |
|---|---|---|---|
| 1 | `stdio.py` 是巨石：传输、守卫、advise 模式、屏梯子、日志、CLI 全在一个文件 | 1147 行 / `run()`、`_advise`、`_ladder_command`、`_log`、`main` 同文件 | 改任何一处都要读 1000 行；本次两次死亡 bug 都出在这个文件 |
| 2 | 同一件事有两套实现 | `state.card_line` vs `gamedata.card_line`；`decisions._describe` 在 `CardRewardJudge` 与 `BossRelicJudge` 各一份；`_state_from` 的 thin/run 双路径在 7 个决策类里都有 | 修一处漏一处（`card_line` 的差异曾导致效果文本缺失） |
| 3 | 决策点标签三处重复 | `witness.py` 的中文标签 / `dashboard.html` 的 `POINT_META` / `FeedClient.java` 的 `pointZh()` | 跨语言重复，改一个忘两个 → 面板显示英文或空白 |
| 4 | `tactical/` 太薄 | 125 行，`combat_greedy.py` 自述 "Phase 4 upgrade path" | 战斗内出牌（玩家最频繁的决策）没有知识层参与 |
| 5 | 配置里有死项 | `config/strategy.json` 的 `synergy_weight` 全仓库无读取点 | 配置文件说谎：看起来可调，实际无效 |
| 6 | 测试盲区 | `gamedata.py`、`analysis/*`、`openrouter_client.py` 均为 0 直接引用 | 出事只能靠真机发现（本机 `gamedata` 是卡牌文本的唯一来源，却没有测试） |

## 2. 重构计划（按 收益/风险 排序）

### 阶段 A — 拆 `stdio.py`（收益最高，风险中）

**目标**：`stdio.py` 只做"字节流 ↔ 消息"，其余全部搬走。

| 新文件 | 从 `stdio.py` 搬什么 | 验收 |
|---|---|---|
| `driver/transport.py` | `StdioTransport` 的读写循环、`run()`、`_log`、`to_command_line` | `tests/test_stdio.py` 全绿且不改断言 |
| `driver/guards.py` | 停滞守卫、动作上限、屏梯子（`_ladder_command`、`_screen_signature`） | `tests/test_guards.py` 全绿 |
| `driver/modes.py` | advise 模式的轮询/去重/发布（`_advise`、`_poll`、`advised_fp`） | `tests/test_advise.py` 全绿 |
| `driver/encoding.py` | `_scrub_surrogates` + stdin/stderr 的编码策略 | `tests/test_poison.py` 全绿 |

**硬性约束**：纯搬运，**不顺手改行为**。先搬 + 全绿，再谈优化。
搬完 `stdio.py` 应 < 300 行，只留组装与 CLI。

### 阶段 B — 消重（争议最小，风险低）

1. `card_line` 两套 → 以 `gamedata` 版为准，`state` 版删掉并改调用点（3 处）。
2. `_describe` 两份 → 提到 `jev_brain/prompt.py`，两个 Judge 共用。
3. 决策点标签 → **单一来源**：Python 生成一份 JSON（`spirebrain/data/points.json`），
   Java 侧读它、网页侧 fetch 它。这是唯一能真正消灭三处重复的做法；
   在此之前，用一条契约测试钉住"Python 的标签集合 == Java 里硬编码的集合"（可解析
   `FeedClient.java` 文本比对，虽然土但有效）。

### 阶段 C — `_state_from` 双路径收敛

7 个决策类都保留 `thin` / `run` 两条构造路径，是明确的临时补丁（注释自述）。
计划：只保留 `RunContext` 一条路径，`thin` 仅作为 `RunContext` 的构造便捷函数存在。
验收：`tests/test_decisions.py` + `test_new_modules.py` 全绿，
且 `decisions.py` 里 `_state_from` 的调用点从 7 处降为 1 处。

### 阶段 D — 补齐测试盲区

| 模块 | 该测什么 |
|---|---|
| `gamedata.py` | 加载失败时的降级、`lookup_keys` 的 id 优先、`card_effect` 带/不带升级 |
| `openrouter_client.py` | 超时、限流、非 JSON 响应、`MAX_REQUEST_BYTES` 截断 |
| `analysis/*` | 至少一个端到端：给一段 `advice.jsonl`，产出非空统计 |

### 阶段 E — 死配置与文档一致性

1. `strategy.json` 的 `synergy_weight`：要么接进 `deck.grade()` 的权重，要么删除。
   （本次已把协同权重写死在 `knowledge._SYNERGY_RULES`，倾向删除该配置项。）
2. README / ROADMAP / SETUP 里与代码不符的段落用一条检查脚本兜住
   （`doctor` 已经在做类似的事，扩展它）。

## 3. 已知的性能/正确性风险（不是坏味道，但要记账）

* `deck.profile()` 每屏候选都会重新读一遍卡组的效果文本（`gamedata` 是缓存的，
  但 `profile` 每次都重新遍历）。当前卡组 ≤ 40 张、候选 ≤ 4 张，实测无感；
  若将来给战斗内出牌也接入评估，需要加缓存。
* `_scrub_surrogates` 递归整棵消息树，每屏一次。消息约 4–50 KB，实测无感。

## 4. 本次已做（2026-09-22 夜）

* 新增 `spirebrain/cards/`（4 个文件）与 `tools/extract_card_meta.py`。
* `CardRewardJudge` 改造为"模型 + 卡组证据"双边决策（含模型弃权时的本地拍板、
  以及无证据时弃权）。
* `state.deck_keys()` 新增，消除"游戏条目 vs 卡牌 ID"在调用点的重复转换。
* 新增 `tests/test_cards.py`（16 个测试）与全量回归 281 个测试函数。

**未做**：阶段 A–E 全部。它们是大块的、需要单独一轮的机械改造，
不适合和"新增能力"混在同一次提交里——那正是本项目两次线上事故的成因。
