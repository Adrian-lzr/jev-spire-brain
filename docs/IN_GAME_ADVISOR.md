# 游戏内实时攻略顾问

运行 `python start.py`，然后通过 ModTheSpire 启动游戏。勾选 BaseMod、一个 CommunicationMod 和 SpireBrainOverlay。默认 `advise` 模式只发送 `wait` / `state`，由玩家操作角色。首次配置可运行 `python start.py --setup`。

建议直接出现在游戏内面板：当前一步（含卡牌、敌人或药水）、简短原因、攻略规则 / JEV 判断 / 规则兜底。浏览器详细记录通过 `python start.py --browser` 打开；两种面板读取同一个本地事件服务。

攻略文件为 `config/guide_rules.json`。每条规则包含唯一 `id`、`characters`、`scenes`、`priority`、`when`、`effect`、`reason` 和 `source`。条件使用 `hp_ratio__lt`、`deck_size__gte` 等声明式比较，不执行代码。数值以游戏状态为准；地图预计损血只是策略估计。首版只对铁甲战士应用这些专属规则，其他角色显示覆盖不足。

状态变化后立即更新面板状态；能确定的规则建议先显示。模型在后台运行，只有结果的状态指纹和局数版本仍匹配时才显示，过期结果会被丢弃。每一步显示前核对当前选项、能量、目标及药水可用性。无法核对时显示暂无可靠建议；缺少伤害或特殊效果数据时明确提示不确定性。

验证命令：

```powershell
uv run --with pytest python -m pytest -q
python -m spirebrain.doctor
python -X utf8 java/build.py --install
python -X utf8 java/build.py --check http://127.0.0.1:8787
```

最后一条运行游戏面板自己的读取器，检查其收到的建议、原因和来源，不替代游戏画面的人工验收。真机验收需玩家进行一局铁甲战士，抽样检查地图、奖励、事件、火堆、商店、遗物、战斗和快速连续操作；核对每次状态更新后旧建议消失、游戏与浏览器建议一致，且每次启动只有一个 agent。游戏未运行时不能声称已完成这一验收。
