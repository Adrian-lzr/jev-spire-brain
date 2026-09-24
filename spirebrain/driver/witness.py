"""Advisor mode, second half: what did the PLAYER actually do?

The project exists to help a human play, not to play for them. In advisor mode
the brain runs its whole pipeline — JEV call, gate, tactical layer — but the
answer leaves as a *recommendation to the player* instead of a command to the
game. That creates one question auto-play never had to ask: **did the player take
the advice?**

It needs no new game API. CommunicationMod re-reports the whole state after every
action, so the player's choice is *observable as a difference*: the card that
left the hand, the gold that went down, the card that joined the deck, the relic
that appeared. This module does exactly one thing — turn two states into the
action that happened in between — and it is deliberately honest about the cases
where the evidence does not single out an action: those come back `unobserved`,
never as a guess.

The matching rule is one idea used twice. Both sides of the comparison are
reduced to an **action key** — a small tuple like `("play", "strike", "cultist")`
or `("node", "3,7")` — the advice through `advice_key(game, command)` and the
player through a detector. Equal keys are a match. `None` inside a key means
*unknown*, and unknown never manufactures a mismatch: a target we could not
identify compares equal to any target. That is the difference between measuring
agreement and inventing it.

Two honest limitations, both measured rather than assumed:

* **Advice lag.** If the player acts before our recommendation lands, the state
  change we then see belongs to their earlier decision. The verdict for that pair
  is meaningless and shows up as `unobserved` more often than the game strictly
  requires. Better a low sample count than a wrong one.
* **Concealed actions.** Potions, and choices that leave no numeric trace, are
  not detectable. They are `unobserved` by construction, not by oversight.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #
MATCH = "match"
MISMATCH = "mismatch"
UNOBSERVED = "unobserved"
VERDICTS = (MATCH, MISMATCH, UNOBSERVED)

# Screen type -> the map symbol whose node leads there. Used to infer a route
# choice: the game does not report "you took the elite", it reports that we are
# now in an elite fight, and the offered nodes say which symbol that was.
SCREEN_SYMBOL = {
    "COMBAT": "M",
    "MONSTER": "M",
    "ELITE": "E",
    "REST": "R",
    "SHOP_SCREEN": "$",
    "SHOP": "$",
    "TREASURE": "T",
    "CHEST": "T",
    "EVENT": "?",
    "BOSS": "B",
}

# Which decision point a screen belongs to, in the router's own vocabulary.
SCREEN_POINT = {
    "MAP": "map",
    "CARD_REWARD": "card_reward",
    "EVENT": "event",
    "REST": "rest",
    "SHOP_SCREEN": "shop",
    "SHOP": "shop",
    "BOSS_REWARD": "boss_relic",
    "COMBAT": "combat",
    "GRID": "grid",
    "CARD_SELECT": "grid",
    "HAND_SELECT": "grid",
}

# Chinese labels for the dashboard. The live console stays ASCII (it is captured
# into the mod's error log), so this table is for the page, not the terminal.
POINT_LABELS = {
    "map": "地图选路",
    "card_reward": "卡牌奖励",
    "event": "事件",
    "rest": "篝火",
    "shop": "商店",
    "boss_relic": "Boss 遗物",
    "combat": "战斗",
    "combat_risk": "战斗风险",
    "grid": "选卡格",
    "navigation": "过场",
}

SYMBOL_LABELS = {
    "M": "普通战斗", "E": "精英战", "R": "篝火", "$": "商店",
    "T": "宝箱", "?": "未知事件", "B": "Boss",
}


def _get(obj, *names, default=None):
    """First present, non-None value among `names`. Never raises.

    Same contract as the router's own `_get`: a hostile or partial state must
    degrade to "unknown", because this module runs on live game payloads we do
    not control.
    """
    if not isinstance(obj, dict):
        return default
    for name in names:
        value = obj.get(name)
        if value is not None:
            return value
    return default


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _screen(game) -> str:
    return str(_get(game, "screen_type", "screen", default="")).upper()


def _name_of(obj) -> str:
    """Language-independent card/relic identity where possible.

    `id` before `name`, for the reason `state.lookup_keys` documents: this
    machine's game runs in Chinese, so display names differ between states of
    the same install and are a bad key.
    """
    if isinstance(obj, str):
        return obj
    return str(_get(obj, "id", "card_id", "relic_id", "name", default="?"))


def _cards_of(obj) -> list[str]:
    return [_name_of(c) for c in (obj or []) if c is not None]


def _card_key(obj) -> str:
    """Identity that includes the upgrade state.

    Necessary, not fussy: an upgraded card keeps its `id` and display name, so a
    deck diff by name cannot see an upgrade at all — and "the player upgraded a
    card at the rest site" is exactly the action a rest-site verdict has to
    detect. `upgrades` is an int count in CommunicationMod's payload; some
    hand-built states use a bool.
    """
    name = _name_of(obj)
    up = _get(obj, "upgrades", default=None)
    if up is None:
        up = 1 if _get(obj, "is_upgraded", default=False) else 0
    return f"{name}+{_int(up)}" if _int(up) else name


def _card_keys(obj) -> list[str]:
    return [_card_key(c) for c in (obj or []) if c is not None]


# --------------------------------------------------------------------------- #
# The advice and its verdict
# --------------------------------------------------------------------------- #
@dataclass
class Advice:
    """One recommendation, plus everything needed to adjudicate it later."""

    point: str                      # router vocabulary: combat / map / ...
    screen: str                     # the raw screen it was made on
    command: dict                   # exactly what auto-play would have sent
    key: tuple                      # normalised action key (None = don't care)
    label: str = ""                 # Chinese, for the player
    reason: str = ""
    confidence: float = 0.0
    fallback: bool = False
    act: int = 0
    floor: int = 0
    message: int = 0                # transport message index that produced it
    state_id: str = ""               # exact state this recommendation belongs to
    source_type: str = ""            # gpt_strategy | jev_tactical | guide_rule | rule_fallback
    source: str = ""                 # human-auditable guide source, when known
    guide_rules: list[dict] = field(default_factory=list)
    strategic_goal: str = ""
    plan_id: str = ""
    brain_source: str = ""
    jev_confidence: float = 0.0
    alternative_command: dict | None = None
    alternative_label: str = ""
    alternative_reason: str = ""
    alternative_condition: str = ""
    uncertain: bool = False
    candidates: list[dict] = field(default_factory=list)
    candidate_id: str = ""
    long_term_goal: str = ""
    brain_backend: str = ""
    brain_latency_ms: int = 0
    brain_request_id: str = ""
    brain_error: str = ""
    status: str = "ready"             # fast_advice | model_ready | unavailable
    decision_id: str = ""
    request_id: str = ""
    first_advice_latency_ms: int | None = None
    decision_latency_ms: int | None = None
    publish_latency_ms: int | None = None


@dataclass
class Outcome:
    """What the player did with that advice."""

    advice: Advice
    verdict: str
    acted_key: tuple | None = None
    acted_label: str = ""
    evidence: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Action keys
# --------------------------------------------------------------------------- #
def _keys_match(advice_key: tuple, acted_key: tuple) -> bool:
    """Equal, where `None` on either side means "unknown, do not judge".

    Length mismatch is a mismatch of kind (play vs end), which is a real
    disagreement — only *elements* may be excused, never the verb.
    """
    if not advice_key or not acted_key:
        return False
    if len(advice_key) != len(acted_key):
        return False
    for a, b in zip(advice_key, acted_key):
        if a is None or b is None:
            continue          # unknown evidence never manufactures a mismatch
        if a != b:
            return False
    return True


def _hand(game) -> list[str]:
    combat = _get(game, "combat", default=game)
    return _cards_of(_get(combat, "hand", default=[]))


def _monsters(game) -> list[dict]:
    combat = _get(game, "combat", default=game)
    out = []
    for m in (_get(combat, "monsters", default=[]) or []):
        out.append({"name": str(_get(m, "name", default="enemy")),
                    "hp": _int(_get(m, "current_hp", "hp", default=0))})
    return out


def _energy(game) -> int | None:
    combat = _get(game, "combat", default=game)
    player = _get(combat, "player", default=combat)
    value = _get(player, "energy", default=None)
    return None if value is None else _int(value)


def _node_of(game) -> list[dict]:
    """The offered map nodes, in the same order the router enumerated them."""
    raw = _get(_get(game, "map", default=game), "next_nodes", default=[]) or []
    if not raw:
        raw = _get(_get(game, "screen_state", "screen", default={}),
                   "next_nodes", default=[]) or []
    out = []
    for i, n in enumerate(raw):
        out.append({"id": f"{_get(n, 'x', default=i)},{_get(n, 'y', default=0)}",
                    "symbol": str(_get(n, "symbol", default="?"))})
    return out


def _screen_items(game) -> list[dict]:
    """Card-reward / boss-reward / shop / grid contents, in router order."""
    screen = _get(game, "screen_state", "screen", default=game)
    return [x for x in (_get(screen, "cards", default=[]) or []) if x is not None]


def advice_key(game, command: dict, point: str = "") -> tuple:
    """The advice side of the comparison: the action, normalised.

    `point` defaults to the screen's own point, which is how the router decided,
    so the two sides of a later comparison cannot disagree about what kind of
    question this was.
    """
    verb = str(_get(command, "command", default="")).lower()
    point = point or SCREEN_POINT.get(_screen(game), "")

    if verb == "play":
        hand = _hand(game)
        idx = _int(_get(command, "card", "card_index", default=-1))
        card = hand[idx].lower() if 0 <= idx < len(hand) else None
        mons = _monsters(game)
        tidx = _get(command, "target", "target_index", default=None)
        target = (mons[_int(tidx)]["name"].lower()
                  if tidx is not None and 0 <= _int(tidx) < len(mons) else None)
        return ("play", card, target)

    if verb == "end":
        return ("end",)

    if verb in ("return", "cancel"):
        return ("skip",)

    if verb == "confirm":
        return ("confirm",)

    if verb == "proceed":
        return ("proceed",)

    if verb == "choose":
        idx = _get(command, "choice", "index", default=None)
        if idx is None:
            return ("choose", None)
        idx = _int(idx)
        if point == "map":
            nodes = _node_of(game)
            return ("node", nodes[idx]["id"] if 0 <= idx < len(nodes) else None)
        if point == "card_reward":
            cards = _screen_items(game)
            return ("take", _name_of(cards[idx]).lower() if 0 <= idx < len(cards) else None)
        if point == "boss_relic":
            relics = _get(_get(game, "screen_state", "screen", default={}),
                          "relics", default=[]) or []
            return ("relic", _name_of(relics[idx]).lower() if 0 <= idx < len(relics) else None)
        if point == "rest":
            rest = [str(x) for x in (_get(_get(game, "screen_state", "screen",
                                               default={}),
                                          "rest_options", default=[]) or [])]
            option = rest[idx].lower() if 0 <= idx < len(rest) else None
            return ("rest", option)
        if point == "shop":
            items = _shop_items(game)
            return ("buy", items[idx]["name"].lower() if 0 <= idx < len(items) else None)
        if point == "grid":
            cards = _screen_items(game)
            return ("grid", _name_of(cards[idx]).lower() if 0 <= idx < len(cards) else None)
        # Event options are text we cannot key on reliably; the detector reports
        # whether the option list shrank, and by how many. Index equality is the
        # only honest comparison there.
        return ("choose", idx)

    return ("verb", verb or None)


def _shop_items(game) -> list[dict]:
    """Shelf contents in the order the router built them: cards, relics, potions."""
    screen = _get(game, "screen_state", "screen", default=game)
    out: list[dict] = []
    for kind, key in (("card", "cards"), ("relic", "relics"), ("potion", "potions")):
        for item in (_get(screen, key, default=[]) or []):
            out.append({"name": str(_get(item, "id", "card_id", "name", default=kind)),
                        "price": _int(_get(item, "price", default=0))})
    return out


# --------------------------------------------------------------------------- #
# Detectors — one per decision point. Each returns (acted_key, label, evidence)
# or (None, "", evidence) when the evidence does not single out an action.
# --------------------------------------------------------------------------- #
def _infer_combat(prev, cur) -> tuple[tuple | None, str, dict]:
    a_hand, b_hand = _hand(prev), _hand(cur)
    a_mons, b_mons = _monsters(prev), _monsters(cur)
    a_energy, b_energy = _energy(prev), _energy(cur)

    def counts(names):
        out: dict[str, int] = {}
        for n in names:
            out[n] = out.get(n, 0) + 1
        return out

    before, after = counts(a_hand), counts(b_hand)
    gone = {k: v - after.get(k, 0) for k, v in before.items() if v > after.get(k, 0)}
    evidence = {
        "hand_lost": gone,
        "energy_spent": (None if a_energy is None or b_energy is None
                         else a_energy - b_energy),
        "monsters_hp_before": [m["hp"] for m in a_mons],
        "monsters_hp_after": [m["hp"] for m in b_mons],
    }

    if gone:
        # Played cards leave the hand. If several left at once (a card that
        # discards, a reshuffle) the identity is not established — say so.
        if len(gone) > 1 or sum(gone.values()) > 1:
            evidence["ambiguous"] = "more than one card left the hand"
            return None, "", evidence
        card = next(iter(gone))
        target = None
        # The key is lower-cased for comparison; the label keeps the game's own
        # spelling, or the panel shows "建议 … Cultist / 你做了 … cultist" and
        # looks like it is talking about two different things.
        display = None
        hurt = [i for i, (x, y) in enumerate(zip(a_mons, b_mons)) if y["hp"] < x["hp"]]
        if len(a_mons) == len(b_mons) and len(hurt) == 1:
            display = b_mons[hurt[0]]["name"]
            target = display.lower()
        elif len(hurt) > 1:
            evidence["ambiguous"] = "several monsters lost HP"
        label = f"出「{card}」" + (f" → {display}" if display else "")
        return ("play", card.lower(), target), label, evidence

    # No card left the hand. A turn boundary is the one action that is still
    # identifiable, and only when the state actually carries a turn number.
    # `turn` sits on the game state in the live payload, but a hand-built state
    # may nest it under `combat`, so look in both before giving up.
    def _turn(game):
        top = _get(game, "turn", "turn_num", default=None)
        if top is not None:
            return _int(top)
        inner = _get(_get(game, "combat", default={}), "turn", "turn_num",
                     default=None)
        return None if inner is None else _int(inner)

    a_turn, b_turn = _turn(prev), _turn(cur)
    if a_turn is not None and b_turn is not None and a_turn != b_turn:
        evidence["turn"] = f"{a_turn} -> {b_turn}"
        return ("end",), "结束回合", evidence

    if a_hand or b_hand:
        evidence["why"] = "no action visible yet"
    return None, "", evidence


def _infer_card_reward(prev, cur) -> tuple[tuple | None, str, dict]:
    offered = [_name_of(c) for c in _screen_items(prev)]
    before = counts_of(_cards_of(_get(prev, "deck", default=[])))
    after = counts_of(_cards_of(_get(cur, "deck", default=[])))
    gained = [n for n in offered if after.get(n, 0) > before.get(n, 0)]
    evidence = {"offered": offered, "deck_gained": gained}
    if len(gained) == 1:
        return ("take", gained[0].lower()), f"拿了「{gained[0]}」", evidence
    if len(gained) > 1:
        evidence["ambiguous"] = "several offered cards appeared in the deck"
        return None, "", evidence
    # Nothing joined the deck: skipping is the only way that happens here (a
    # reward screen does not advance any other way). Safe to call it.
    return ("skip",), "跳过奖励", evidence


def counts_of(names: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for n in names:
        out[n] = out.get(n, 0) + 1
    return out


def _infer_map(prev, cur) -> tuple[tuple | None, str, dict]:
    nodes = _node_of(prev)
    symbol = SCREEN_SYMBOL.get(_screen(cur))
    evidence = {"offered": [n["id"] + n["symbol"] for n in nodes],
                "arrived_screen": _screen(cur)}
    if symbol is None:
        evidence["why"] = "the new screen does not identify a node type"
        return None, "", evidence
    candidates = [n["id"] for n in nodes if n["symbol"] == symbol]
    evidence["candidates"] = candidates
    if len(candidates) == 1:
        return ("node", candidates[0]), f"走了 {SYMBOL_LABELS.get(symbol, symbol)}", evidence
    # Several nodes lead to the same kind of room: the type is known, which node
    # it was is not. Return a key that only claims the type — honest, and still
    # enough to judge a route recommendation, which is about the type.
    if candidates:
        evidence["ambiguous"] = "several offered nodes have this symbol"
        return ("node_type", symbol), f"走了 {SYMBOL_LABELS.get(symbol, symbol)}", evidence
    evidence["why"] = "the room we arrived in was not among the offered nodes"
    return None, "", evidence


def _infer_boss_reward(prev, cur) -> tuple[tuple | None, str, dict]:
    before = set(_cards_of(_get(prev, "relics", default=[])))
    after = set(_cards_of(_get(cur, "relics", default=[])))
    gained = sorted(after - before)
    evidence = {"relics_gained": gained}
    if len(gained) == 1:
        return ("relic", gained[0].lower()), f"拿了遗物「{gained[0]}」", evidence
    if not gained:
        return ("skip",), "跳过了遗物", evidence
    evidence["ambiguous"] = "several relics appeared at once"
    return None, "", evidence


def _infer_rest(prev, cur) -> tuple[tuple | None, str, dict]:
    hp_before = _int(_get(prev, "current_hp", "hp", default=0))
    hp_after = _int(_get(cur, "current_hp", "hp", default=0))
    options = [str(o) for o in (_get(_get(prev, "screen_state", "screen", default={}),
                                     "rest_options", default=[]) or [])]
    prev_keys = _card_keys(_get(prev, "deck", default=[]))
    cur_keys = _card_keys(_get(cur, "deck", default=[]))
    upgraded = [k for k in cur_keys if k not in prev_keys and "+" in k]
    evidence = {"hp": [hp_before, hp_after], "rest_options": options}
    if hp_after > hp_before:
        return ("rest", _option_named(options, "rest")), "休息（回血）", evidence
    if upgraded:
        evidence["upgraded"] = upgraded
        return ("rest", _option_named(options, "smith")), "升级卡牌", evidence
    evidence["why"] = "neither HP nor the deck changed"
    return None, "", evidence


def _option_named(options: list[str], needle: str) -> str | None:
    for o in options:
        if needle in o.lower():
            return o.lower()
    return None


def _infer_shop(prev, cur) -> tuple[tuple | None, str, dict]:
    gold_before = _int(_get(prev, "gold", default=0))
    gold_after = _int(_get(cur, "gold", default=0))
    shelf_before = _shop_items(prev)
    shelf_after = {i["name"] for i in _shop_items(cur)}
    spent = gold_before - gold_after
    evidence = {"gold": [gold_before, gold_after], "spent": spent}
    if spent > 0:
        matches = [i for i in shelf_before
                   if i["price"] == spent and i["name"] not in shelf_after]
        if len(matches) == 1:
            return ("buy", matches[0]["name"].lower()), \
                   f"买了「{matches[0]['name']}」（{spent} 金）", evidence
        if len(matches) > 1:
            evidence["ambiguous"] = "several items cost exactly that much"
            return None, "", evidence
        purge = _int(_get(_get(prev, "screen_state", "screen", default={}),
                          "purge_cost", default=0))
        if purge and spent == purge:
            return ("buy", "purge"), f"删了 1 张牌（{spent} 金）", evidence
        evidence["why"] = "gold moved by an amount no shelf item explains"
        return None, "", evidence
    if spent == 0 and _screen(cur) != _screen(prev):
        return ("skip",), "离开商店", evidence
    evidence["why"] = "the shop has not been acted in yet"
    return None, "", evidence


def _infer_grid(prev, cur) -> tuple[tuple | None, str, dict]:
    prev_keys = _card_keys(_get(prev, "deck", default=[]))
    cur_keys = _card_keys(_get(cur, "deck", default=[]))
    prev_names = _cards_of(_get(prev, "deck", default=[]))
    cur_names = _cards_of(_get(cur, "deck", default=[]))
    lost = [c for c in prev_names if c not in cur_names]
    upgraded = [k for k in cur_keys if k not in prev_keys and "+" in k]
    evidence = {"deck_lost": lost, "deck_upgraded": upgraded}
    if len(lost) == 1:
        return ("grid_removed", lost[0].lower()), f"移除了「{lost[0]}」", evidence
    if len(upgraded) == 1:
        return ("grid", upgraded[0].split("+")[0].lower()), \
               f"升级为「{upgraded[0]}」", evidence
    if len(lost) > 1 or len(upgraded) > 1:
        evidence["ambiguous"] = "several cards changed at once"
        return None, "", evidence
    evidence["why"] = "the deck is unchanged"
    return None, "", evidence


def _infer_event(prev, cur) -> tuple[tuple | None, str, dict]:
    def options(game):
        raw = _get(_get(game, "screen_state", "screen", default={}),
                   "options", default=[]) or []
        return [str(_get(o, "label", "text", default=f"option {i}"))
                for i, o in enumerate(raw)]

    before, after = options(prev), options(cur)
    evidence = {"options_before": before, "options_after": after}
    if before and after and len(after) < len(before):
        # One option was consumed. Which one is deducible when exactly one
        # disappeared from the visible list.
        missing = [o for o in before if o not in after]
        if len(missing) == 1:
            return ("option", missing[0].lower()), f"选了「{missing[0]}」", evidence
    evidence["why"] = ("event outcomes leave no reliable trace; "
                       "only an option list that shrinks is evidence")
    return None, "", evidence


DETECTORS = {
    "combat": _infer_combat,
    "card_reward": _infer_card_reward,
    "map": _infer_map,
    "boss_relic": _infer_boss_reward,
    "rest": _infer_rest,
    "shop": _infer_shop,
    "grid": _infer_grid,
    "event": _infer_event,
}


# --------------------------------------------------------------------------- #
# Labels for the player
# --------------------------------------------------------------------------- #
def label_for(game, command: dict, point: str = "") -> str:
    """One Chinese line a player can act on: what to do, in the game's own words."""
    verb = str(_get(command, "command", default="")).lower()
    point = point or SCREEN_POINT.get(_screen(game), "")
    if verb == "play":
        hand = _hand(game)
        idx = _int(_get(command, "card", "card_index", default=-1))
        card = hand[idx] if 0 <= idx < len(hand) else "?"
        mons = _monsters(game)
        tidx = _get(command, "target", "target_index", default=None)
        suffix = ""
        if tidx is not None and 0 <= _int(tidx) < len(mons):
            suffix = f" → {mons[_int(tidx)]['name']}"
        return f"出「{card}」{suffix}"
    if verb == "potion":
        slot = _int(_get(command, "slot", "choice", default=-1), -1)
        potions = _get(game, "potions", default=[]) or []
        name = _name_of(potions[slot]) if 0 <= slot < len(potions) else f"第 {slot + 1} 槽药水"
        return f"使用「{name}」"
    if verb == "end":
        return "结束回合"
    if verb == "return":
        return {"card_reward": "跳过这张牌", "shop": "离开商店"}.get(point, "跳过")
    if verb == "confirm":
        return "确认这个选择"
    if verb == "proceed":
        return "继续"
    if verb == "choose":
        idx = _get(command, "choice", "index", default=None)
        if command.get("name"):
            return f"选「{command['name']}」"
        if idx is None:
            return "选一个"
        idx = _int(idx)
        if point == "map":
            nodes = _node_of(game)
            if 0 <= idx < len(nodes):
                sym = nodes[idx]["symbol"]
                return f"走 {SYMBOL_LABELS.get(sym, sym)}"
            return "选一条路"
        if point == "card_reward":
            cards = _screen_items(game)
            return f"拿「{_name_of(cards[idx])}」" if 0 <= idx < len(cards) else "拿一张牌"
        if point == "boss_relic":
            relics = _get(_get(game, "screen_state", "screen", default={}),
                          "relics", default=[]) or []
            return f"拿遗物「{_name_of(relics[idx])}」" if 0 <= idx < len(relics) else "拿一件遗物"
        if point == "rest":
            rest = [str(x) for x in (_get(_get(game, "screen_state", "screen", default={}),
                                          "rest_options", default=[]) or [])]
            if 0 <= idx < len(rest):
                opt = rest[idx].lower()
                return "休息（回血）" if "rest" in opt else "升级一张牌"
            return "选一项"
        if point == "shop":
            items = _shop_items(game)
            if 0 <= idx < len(items):
                return f"买「{items[idx]['name']}」（{items[idx]['price']} 金）"
            return "买一件"
        if point == "grid":
            cards = _screen_items(game)
            return f"选第 {idx + 1} 张" + (
                f"（{_name_of(cards[idx])}）" if 0 <= idx < len(cards) else "")
        return "选一个选项"
    return verb or "?"


# --------------------------------------------------------------------------- #
# The tracker
# --------------------------------------------------------------------------- #
class PlayerTracker:
    """One pending recommendation, adjudicated against the next state change.

    Deliberately *not* adjudicated on every message: the transport polls, so most
    messages carry the same state and prove nothing. `resolve` returns None for
    those and the advice stays pending — which is also what keeps a slow player
    from being scored as a mismatch for not having acted yet.
    """

    def __init__(self) -> None:
        self.pending: Advice | None = None
        self.tally = {MATCH: 0, MISMATCH: 0, UNOBSERVED: 0}
        self.superseded = 0
        self.history: list[Outcome] = []
        # The state the pending advice was made on. The transport sets it right
        # after `note`, which keeps the tracker free of state plumbing.
        self._prev_game: Any = None

    # -- lifecycle --------------------------------------------------------- #
    def note(self, advice: Advice) -> None:
        """Adopt a new recommendation, retiring an unanswered one.

        Same point + same action key is a *re-statement* (a re-advise on a
        re-reported state), not a new question: the old one is simply replaced
        without touching the counters, or a polling loop would inflate the
        superseded count into meaninglessness.
        """
        old = self.pending
        if old is not None and (old.point, old.key) != (advice.point, advice.key):
            self.superseded += 1
        self.pending = advice

    def resolve(self, cur) -> Outcome | None:
        """Adjudicate the pending advice against the state we just received."""
        advice = self.pending
        if advice is None:
            return None
        detector = DETECTORS.get(advice.point)
        if detector is None:
            return None
        prev = self._prev_game
        if prev is None:
            return None
        acted_key, label, evidence = detector(prev, cur)

        if acted_key is None:
            # Nothing identifiable happened. If the screen family changed the
            # question is over and the pair goes down as unobserved; otherwise
            # the player may still be deciding, so the advice stays pending.
            if SCREEN_POINT.get(_screen(cur)) != advice.point:
                return self._finish(advice, UNOBSERVED, None, "", evidence)
            return None

        verdict = MATCH if _keys_match(advice.key, acted_key) else MISMATCH
        return self._finish(advice, verdict, acted_key, label, evidence)

    def _finish(self, advice: Advice, verdict: str, acted_key, label,
                evidence) -> Outcome:
        outcome = Outcome(advice=advice, verdict=verdict, acted_key=acted_key,
                          acted_label=label, evidence=evidence)
        self.tally[verdict] = self.tally.get(verdict, 0) + 1
        self.history.append(outcome)
        self.pending = None
        return outcome

    def remember_state(self, game) -> None:
        self._prev_game = game

    # -- reporting --------------------------------------------------------- #
    @property
    def judged(self) -> int:
        """Pairs where we could actually tell what the player did."""
        return self.tally[MATCH] + self.tally[MISMATCH]

    @property
    def agreement(self) -> float | None:
        """Advice that was followed / advice we could judge. None = no data yet.

        None, not 0.0: "no measurable agreement yet" and "the player ignores
        everything I say" are different facts and must not render the same.
        """
        return (self.tally[MATCH] / self.judged) if self.judged else None
