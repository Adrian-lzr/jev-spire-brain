"""Candidate enumeration and the final GPT/JEV/legality reconciliation."""

from __future__ import annotations

from typing import Any

from spirebrain.driver.legality import check_action

from .protocol import ActionCandidate, ExecutionDecision, StrategicPlan


def _get(obj: Any, *names: str, default=None):
    if not isinstance(obj, dict):
        return default
    for name in names:
        if obj.get(name) is not None:
            return obj[name]
    return default


def _screen(game: dict) -> str:
    screen = str(_get(game, "screen_type", "screen", default="")).upper()
    if screen == "NONE" and str(_get(game, "room_phase", default="")).upper() == "COMBAT":
        return "COMBAT"
    return screen


def _label(obj: Any, default: str = "item") -> str:
    if isinstance(obj, str):
        return obj
    return str(_get(obj, "id", "card_id", "relic_id", "name", "label", "text",
                   default=default))


def _entity_id(obj: Any, *, kind: str, slot: int | str = "") -> str:
    """Stable object identity with a conservative slot fallback.

    Display names are not identities: two Strike cards, two same-name enemies
    and two copies of a shop item can coexist.  CommunicationMod does not
    expose an instance id on every version, so the live slot is retained as a
    last-resort disambiguator and included in the candidate signature.
    """
    if isinstance(obj, dict):
        # ``id``/``card_id`` is usually a definition id, not an instance id;
        # only fields explicitly documented as unique may stand alone.
        explicit_instance = _get(obj, "instance_id", "entity_id", "unique_id", default=None)
        if explicit_instance is not None and str(explicit_instance).strip():
            return f"{kind}:instance:{explicit_instance}"
        definition = _get(obj, "id", "card_id", "relic_id", "potion_id", default=None)
        if definition is not None and str(definition).strip():
            return f"{kind}:{definition}:slot:{slot}"
        label = _label(obj, kind)
    else:
        label = _label(obj, kind)
    return f"{kind}:{label}:slot:{slot}"


def _upgrade_state(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    value = _get(item, "upgrades", "upgrade_level", default=None)
    if value is None:
        value = 1 if item.get("is_upgraded") else 0
    try:
        return f"+{max(0, int(value))}"
    except (TypeError, ValueError):
        return str(value)


def _item_unavailable(item: Any) -> bool:
    """CommunicationMod versions use several names for a vanished shelf item."""
    if item is None:
        return True
    if not isinstance(item, dict):
        return False
    if item.get("is_available") is False or item.get("available") is False:
        return True
    return any(bool(item.get(key)) for key in (
        "disabled", "purchased", "sold", "removed", "unavailable",
    ))


def _live_monsters(game: dict) -> list[tuple[int, dict]]:
    combat = _get(game, "combat", "combat_state", default={}) or {}
    out = []
    for index, monster in enumerate(_get(combat, "monsters", default=[]) or []):
        if not isinstance(monster, dict):
            continue
        if monster.get("is_gone") or monster.get("half_dead"):
            continue
        try:
            hp = int(monster.get("current_hp", monster.get("hp", 0)) or 0)
        except (TypeError, ValueError):
            hp = 0
        if hp > 0:
            out.append((index, monster))
    return out


def _add(out: list[ActionCandidate], game: dict, candidate: ActionCandidate,
         forbidden_indices: set[int] | None = None,
         diagnostics: list[dict] | None = None) -> None:
    def reject(reason: str) -> None:
        if diagnostics is not None and len(diagnostics) < 200:
            diagnostics.append({"candidate_id": candidate.candidate_id,
                                "label": candidate.label, "reason": reason})

    if not candidate.legal:
        reject("候选已标记为非法")
        return
    if forbidden_indices and str(candidate.command.get("command", "")).lower() == "choose":
        try:
            choice = int(candidate.command.get("choice", candidate.command.get("index", -1)))
        except (TypeError, ValueError):
            choice = -1
        if choice in forbidden_indices:
            reject("被攻略硬规则禁止")
            return
    legal, reason = check_action(game, candidate.command)
    if legal:
        out.append(candidate)
    else:
        reject(reason)


def _reject(diagnostics: list[dict], candidate_id: str, label: str, reason: str) -> None:
    if len(diagnostics) < 200:
        diagnostics.append({"candidate_id": candidate_id, "label": label, "reason": reason})


def _screen_items(game: dict) -> dict:
    screen = _get(game, "screen_state", "screen", default=game) or {}
    return screen if isinstance(screen, dict) else {}


def potion_purchase_allowed(game: dict, item: dict, state: dict) -> bool:
    """Reject a potion purchase when the live state says the slots are full."""
    if item.get("potion_slots_full") or item.get("potion_full"):
        return False
    capacity = _get(state, "potion_capacity", "potion_slots", default=None)
    if capacity is None:
        capacity = _get(game, "potion_capacity", "potion_slots", default=None)
    if isinstance(capacity, dict):
        capacity = _get(capacity, "capacity", "max", "slots", default=None)
    elif isinstance(capacity, (list, tuple)):
        capacity = len(capacity)
    if capacity is None:
        return True
    try:
        capacity = int(capacity)
    except (TypeError, ValueError):
        return True
    owned = _get(game, "potions", default=[]) or []
    occupied = 0
    for potion in owned:
        if potion is None:
            continue
        if isinstance(potion, dict) and (potion.get("empty") or potion.get("is_empty")):
            continue
        occupied += 1
    return occupied < max(0, capacity)


def build_action_candidates(game: dict, *,
                            forbidden_indices: set[int] | None = None,
                            diagnostics: list[dict] | None = None) -> list[ActionCandidate]:
    """Enumerate only actions that the current CommunicationMod state accepts."""
    screen = _screen(game)
    out: list[ActionCandidate] = []
    diagnostics = diagnostics if diagnostics is not None else []
    state = _screen_items(game)

    if screen == "MAP":
        map_state = _get(game, "map", default={}) or {}
        nodes = _get(map_state, "next_nodes", default=None)
        if nodes is None:
            nodes = _get(state, "next_nodes", default=[])
        for index, node in enumerate(nodes or []):
            symbol = str(_get(node, "symbol", default="?"))
            node_id = f"{_get(node, 'x', default=index)},{_get(node, 'y', default=0)}"
            _add(out, game, ActionCandidate(
                candidate_id=f"map:{index}", kind="map", label=f"路线 {symbol} ({node_id})",
                command={"command": "choose", "choice": index},
                entity_id=f"map:{node_id}:{symbol}",
                validity={"slot": index, "node_id": node_id, "symbol": symbol},
                goal_tags=["advance", symbol],
                risk_tags=["elite"] if symbol == "E" else [],
                description=f"选择地图节点 {symbol}，位置 {node_id}。",
            ), forbidden_indices, diagnostics)
        return out

    if screen in {"CARD_REWARD", "BOSS_REWARD", "EVENT", "REST", "GRID", "CARD_SELECT", "HAND_SELECT"}:
        key = "relics" if screen == "BOSS_REWARD" else (
            "options" if screen == "EVENT" else "rest_options" if screen == "REST" else "cards"
        )
        items = _get(state, key, default=[]) or []
        for index, item in enumerate(items):
            if isinstance(item, dict) and item.get("disabled"):
                _reject(diagnostics, f"{screen.lower()}:{index}", _label(item), "当前界面已禁用")
                continue
            label = _label(item, f"选项 {index}")
            _add(out, game, ActionCandidate(
                candidate_id=f"{screen.lower()}:{index}",
                kind=screen.lower(), label=label,
                command={"command": "choose", "choice": index},
                entity_id=_entity_id(item, kind=screen.lower(), slot=index),
                upgrade_state=_upgrade_state(item),
                validity={"slot": index},
                goal_tags=["choose"], description=str(_get(item, "description", "text", default=label)),
            ), forbidden_indices, diagnostics)
        if screen in {"CARD_REWARD", "EVENT", "GRID", "CARD_SELECT", "HAND_SELECT"}:
            _add(out, game, ActionCandidate(
                candidate_id=f"{screen.lower()}:leave", kind="leave", label="跳过/离开",
                command={"command": "return"}, goal_tags=["preserve_resources"],
            ), forbidden_indices, diagnostics)
        return out

    if screen in {"SHOP", "SHOP_SCREEN"}:
        try:
            gold = int(_get(game, "gold", default=0) or 0)
        except (TypeError, ValueError):
            gold = 0
        index = 0
        for kind, key in (("card", "cards"), ("relic", "relics"), ("potion", "potions")):
            for item in (_get(state, key, default=[]) or []):
                candidate_id = f"shop:item:{index}"
                label = _label(item, kind)
                if _item_unavailable(item):
                    _reject(diagnostics, candidate_id, label, "商品已售出、禁用或不可用")
                    index += 1
                    continue
                try:
                    raw_price = _get(item, "price", "cost", default=None)
                    if raw_price is None:
                        _reject(diagnostics, candidate_id, label, "商品价格缺失")
                        index += 1
                        continue
                    price = int(raw_price)
                except (TypeError, ValueError):
                    _reject(diagnostics, candidate_id, label, "商品价格格式无效")
                    index += 1
                    continue
                if price > gold:
                    _reject(diagnostics, candidate_id, label, "金币不足")
                elif kind == "potion" and not potion_purchase_allowed(game, item, state):
                    _reject(diagnostics, candidate_id, label, "药水槽已满")
                else:
                    tags = ["spend_gold", kind]
                    if kind == "potion":
                        tags.append("combat_resource")
                    _add(out, game, ActionCandidate(
                        candidate_id=f"shop:item:{index}", kind=kind,
                        label=f"{label}（{price} 金）",
                        command={"command": "choose", "choice": index}, cost=price,
                        entity_id=_entity_id(item, kind=kind, slot=index),
                        validity={"slot": index, "price": price},
                        goal_tags=tags,
                        risk_tags=["gold_commitment"],
                        description=str(_get(item, "description", default=label)),
                    ), forbidden_indices, diagnostics)
                index += 1
        purge_cost = _get(state, "purge_cost", default=None)
        if purge_cost is not None:
            try:
                purge_cost = int(purge_cost)
            except (TypeError, ValueError):
                purge_cost = None
        if purge_cost is not None and purge_cost <= gold:
            _add(out, game, ActionCandidate(
                candidate_id=f"shop:purge:{index}", kind="purge", label=f"删牌（{purge_cost} 金）",
                command={"command": "choose", "choice": index}, cost=purge_cost,
                entity_id=f"shop:purge:slot:{index}",
                validity={"slot": index, "price": purge_cost},
                goal_tags=["thin_deck", "spend_gold"], risk_tags=["gold_commitment"],
            ), forbidden_indices, diagnostics)
        elif _get(state, "purge_cost", default=None) is not None:
            _reject(diagnostics, f"shop:purge:{index}", "删牌", "删牌费用无效或金币不足")
        _add(out, game, ActionCandidate(
            candidate_id="shop:leave", kind="leave", label="离开商店",
            command={"command": "return"}, goal_tags=["preserve_resources"],
        ), forbidden_indices, diagnostics)
        return out

    if screen == "COMBAT":
        combat = _get(game, "combat", "combat_state", default={}) or {}
        hand = _get(combat, "hand", default=[]) or []
        live = _live_monsters(game)
        for index, card in enumerate(hand):
            if not isinstance(card, dict):
                _reject(diagnostics, f"combat:play:{index}", f"卡牌 {index}", "手牌数据无效")
                continue
            if card.get("is_playable") is False:
                _reject(diagnostics, f"combat:play:{index}", _label(card), "游戏标记为当前不可打出")
                continue
            # A missing cost is not evidence of a zero-cost card.  Only accept
            # it when CommunicationMod explicitly marked the card playable;
            # otherwise leave the end-turn candidate as the honest fallback.
            if card.get("cost") is None and card.get("is_playable") is not True:
                _reject(diagnostics, f"combat:play:{index}", _label(card), "费用未知且未标记可打出")
                continue
            targeted = bool(card.get("has_target", str(card.get("type", "")).upper() == "ATTACK"))
            targets = live if targeted else [(None, {})]
            if targeted and not targets:
                _reject(diagnostics, f"combat:play:{index}", _label(card), "没有有效敌方目标")
            for target, monster in targets:
                command = {"command": "play", "card": index}
                if target is not None:
                    command["target"] = target
                identity = _label(card, f"卡牌 {index}")
                tags = [str(card.get("type", "card")).lower()]
                if card.get("damage"):
                    tags.append("damage")
                if card.get("block"):
                    tags.append("block")
                _add(out, game, ActionCandidate(
                    candidate_id=f"combat:play:{index}:{target if target is not None else 'none'}",
                    kind="play", label=f"出 {identity}" + (f" → {_label(monster, '目标')}" if target is not None else ""),
                    command=command, cost=card.get("cost"), target=target,
                    entity_id=_entity_id(card, kind="card", slot=index),
                    upgrade_state=_upgrade_state(card),
                    validity={"hand_slot": index,
                              "target_entity_id": (_entity_id(monster, kind="enemy", slot=target)
                                                    if target is not None else "")},
                    goal_tags=tags,
                    uncertainty="牌面效果未完整解析" if card.get("damage") is None and card.get("block") is None else "",
                    description=str(card.get("description", identity)),
                ), forbidden_indices, diagnostics)
        for slot, potion in enumerate(_get(game, "potions", default=[]) or []):
            if not isinstance(potion, dict) or not potion.get("can_use"):
                _reject(diagnostics, f"combat:potion:{slot}", _label(potion, "药水"), "药水当前不可用")
                continue
            targeted = bool(potion.get("requires_target"))
            targets = live if targeted else [(None, {})]
            if targeted and not targets:
                _reject(diagnostics, f"combat:potion:{slot}", _label(potion), "药水缺少有效目标")
            for target, monster in targets:
                command = {"command": "potion", "action": "use", "slot": slot}
                if target is not None:
                    command["target"] = target
                _add(out, game, ActionCandidate(
                    candidate_id=f"combat:potion:{slot}:{target if target is not None else 'none'}",
                    kind="potion", label=f"使用 {_label(potion, '药水')}" +
                    (f" → {_label(monster, '目标')}" if target is not None else ""),
                    command=command, target=target, goal_tags=["potion", "survive"],
                    entity_id=_entity_id(potion, kind="potion", slot=slot),
                    validity={"potion_slot": slot,
                              "target_entity_id": (_entity_id(monster, kind="enemy", slot=target)
                                                    if target is not None else "")},
                    risk_tags=["consume_resource"],
                ), forbidden_indices, diagnostics)
        _add(out, game, ActionCandidate(
            candidate_id="combat:end", kind="end", label="结束回合",
            command={"command": "end"}, goal_tags=["preserve_energy"],
        ), forbidden_indices, diagnostics)
        return out

    return out


def candidate_for_command(candidates: list[ActionCandidate], command: dict) -> ActionCandidate | None:
    for candidate in candidates:
        if candidate.command == command:
            return candidate
    return None


def _constraint_allows(candidate: ActionCandidate, constraints: dict,
                       game: dict) -> bool:
    """Apply safe, optional resource constraints from a strategic plan.

    Constraints are advisory model output, so malformed values simply do not
    narrow the legal set.  They can never make an illegal candidate legal; that
    boundary was already enforced by ``build_action_candidates``.
    """
    if not isinstance(constraints, dict):
        return True
    try:
        cost = float(candidate.cost or 0)
    except (TypeError, ValueError):
        cost = 0.0
    try:
        gold = float(game.get("gold", 0) or 0)
    except (TypeError, ValueError):
        gold = 0.0
    for key in ("reserve_gold", "min_gold_after"):
        if constraints.get(key) is not None:
            try:
                if gold - cost < float(constraints[key]):
                    return False
            except (TypeError, ValueError):
                pass
    if constraints.get("max_cost") is not None:
        try:
            if cost > float(constraints["max_cost"]):
                return False
        except (TypeError, ValueError):
            pass
    avoid = constraints.get("avoid_risk_tags", constraints.get("avoid_risks", []))
    if isinstance(avoid, str):
        avoid = [avoid]
    if isinstance(avoid, list) and set(map(str, avoid)) & set(candidate.risk_tags):
        return False
    required = constraints.get("require_goal_tags", constraints.get("required_goal_tags", []))
    if isinstance(required, str):
        required = [required]
    if isinstance(required, list) and required:
        if not set(map(str, required)).issubset(set(candidate.goal_tags)):
            return False
    return True


def reconcile(*, game: dict, candidates: list[ActionCandidate], fallback: dict,
              plan: StrategicPlan | None, state_id: str, jev_confidence: float = 0.0,
              reason: str = "") -> tuple[dict, ExecutionDecision]:
    """Apply a plan preference without ever bypassing legality."""
    legal = [c for c in candidates if c.legal]
    fallback_candidate = candidate_for_command(legal, fallback)
    selected = fallback_candidate
    source = ("jev_tactical" if float(jev_confidence or 0.0) >= 0.60
              else "rule_fallback")

    # The orchestrator decides how long a short plan remains valid.  Combat
    # turns intentionally reuse the plan between GPT calls; the current state
    # ID is still attached to the execution record for late-result auditing.
    if plan is not None:
        constrained = {
            c.candidate_id: c for c in legal
            if _constraint_allows(c, plan.resource_constraints, game)
        }
        # A plan can be reused across a combat turn, but an index-based ID is
        # not an identity.  Require the generation-time signature to match;
        # mismatches deliberately fall back to current local/JEV candidates.
        bound = getattr(plan, "candidate_bindings", {}) or {}
        avoid_bound = getattr(plan, "avoid_bindings", {}) or {}
        def binding_matches(candidate_id: str, candidate: ActionCandidate, bindings: dict) -> bool:
            # Plans produced by the current planner always carry bindings. An
            # old in-memory plan without them is treated as legacy-compatible;
            # once a binding exists, an ID alone is never sufficient.
            if not getattr(plan, "bindings_bound", False):
                return True
            return bindings.get(candidate_id) == candidate.candidate_signature
        preferred = [constrained[candidate_id] for candidate_id in plan.preferred_candidates
                     if candidate_id in constrained
                     and binding_matches(candidate_id, constrained[candidate_id], bound)
                     and candidate_id not in plan.avoid_candidates]
        # A tactical JEV selection is accepted when it stays inside the GPT
        # preference set. Otherwise GPT's first explicit preference wins.
        if fallback_candidate in preferred:
            selected = fallback_candidate
            source = "jev_tactical" if float(jev_confidence or 0.0) >= 0.60 else "gpt_strategy"
        elif preferred:
            selected = preferred[0]
            source = "gpt_strategy"
        selected_is_avoided = (selected is not None
                               and selected.candidate_id in plan.avoid_candidates
                               and binding_matches(selected.candidate_id, selected, avoid_bound))
        if (selected is None or selected_is_avoided
                or not _constraint_allows(selected, plan.resource_constraints, game)):
            selected = next((c for c in legal
                             if (c.candidate_id not in plan.avoid_candidates
                                 or not binding_matches(c.candidate_id, c, avoid_bound))
                             and _constraint_allows(c, plan.resource_constraints, game)), None)
            if selected is not None:
                source = "gpt_strategy"

    if selected is None:
        # A handler may have returned a command that is not represented by a
        # candidate (risk posture, navigation, or an incomplete state). Preserve
        # only protocol-safe non-actions or a command that passes the same
        # legality predicate.  The final agent check repeats this boundary.
        fallback_verb = str(fallback.get("command", "")).lower()
        fallback_ok, _ = check_action(game, fallback)
        if fallback_ok or fallback_verb in {"state", "wait", "(posture only)"}:
            selected_command = dict(fallback)
        else:
            selected_command = {"command": "state", "reason_source": "rule_constraint",
                                "reason": "当前候选为空或回退动作已失效。"}
    else:
        selected_command = dict(selected.command)

    alternative = None
    if selected is not None:
        # Alternatives are player-visible recommendations too: apply the same
        # plan bindings, avoid list and resource constraints as the primary
        # choice. Never show a fallback that the strategic layer would reject.
        alternative = next((c for c in legal
                            if c.candidate_id != selected.candidate_id
                            and (not plan or
                                 ((c.candidate_id not in plan.avoid_candidates)
                                  or not binding_matches(c.candidate_id, c, avoid_bound)))
                            and _constraint_allows(c,
                                                   plan.resource_constraints if plan else {},
                                                   game)), None)
    if source == "gpt_strategy":
        selection_basis = "strategic_preference"
    elif source == "jev_tactical":
        selection_basis = "jev_tactical_within_strategy"
    else:
        selection_basis = "strategic_context_local_choice" if plan else "local_fallback"
    detail = ExecutionDecision(
        primary_candidate=selected,
        alternative_candidate=alternative,
        strategic_goal=(plan.current_objective if plan else ""),
        reason=(plan.reason if source == "gpt_strategy" and plan and plan.reason else reason),
        source_type=source,
        state_id=state_id,
        plan_id=plan.plan_id if plan else "",
        jev_confidence=float(jev_confidence or 0.0),
        local_confidence=(float(jev_confidence or 0.0)
                          if float(jev_confidence or 0.0) > 0 else None),
        selection_basis=selection_basis,
        uncertain=bool((selected and selected.uncertainty) or (plan and plan.uncertainty)),
        candidates=[c.model_dict() for c in legal],
    )
    return selected_command, detail
