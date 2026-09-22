"""Deck profile and pick grading — the local half of a card decision.

Why local, when there is a model
--------------------------------
The JEV call answers "how much does this card improve this deck" with a number
and no working. That number was, until now, the whole decision (`argmax` over
the scores, with a 0.55 floor). It cannot notice that the deck is 24 cards with
six Strikes in it, that the player has already committed to an exhaust engine,
or that a card is a trap without its enabler. Those are arithmetic, and
arithmetic should not be outsourced — nor does it cost a request.

So the decision is now two-sided: this module scores each candidate against the
*actual* deck and the *act*, and `decisions.CardRewardJudge` combines that with
the model's judgement (weighted by how confident the model is). Every local
score carries the sentences that produced it, because the panel shows them: a
recommendation the player cannot audit is one they cannot learn from.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from spirebrain.cards import meta
from spirebrain.cards.knowledge import (
    CAUTION,
    IRONCLAD_ARCHETYPES,
    NEED,
    archetype_signal,
    knowledge,
    removal_value,
    upgrade_value,
)

#: Past this size, adding an average card starts to cost more than it gives. The
#: community range for a focused deck is 15-30, with the tighter rule being
#: "only take what you are going to upgrade".
COMFORTABLE_SIZE = 20

# --------------------------------------------------------------------------- #
# Campfire and card removal: the two card choices that are not "add a card"
# --------------------------------------------------------------------------- #
@dataclass
class Choice:
    """One card picked out of the deck, with the reason it was picked."""

    card_id: str
    value: int
    reasons: list[str] = field(default_factory=list)

    def reason_text(self) -> str:
        return "；".join(self.reasons)


def best_upgrade(deck_ids: list[str], act: int = 1,
                 effects: dict[str, str] | None = None) -> Choice | None:
    """Which card deserves the campfire.

    The documented rule is structural, not per-card list: upgrade value comes
    from the upgrade changing what a card *does* (Limit Break stops exhausting,
    Corruption gets cheaper, Bash reaches 3 Vulnerable) rather than from how much
    damage it adds. `knowledge.UPGRADE_PRIORITY` holds the cards the sources name;
    everything else falls back to Powers > Skills > Attacks.

    Deck awareness enters through the same signal the pick grader uses: a card
    that the deck is built around is worth upgrading before a card that merely
    fills a slot.
    """
    if not deck_ids:
        return None
    archetypes = archetype_signal(deck_ids)
    best: Choice | None = None
    for card_id in deck_ids:
        text, card_type = _effect_and_type(card_id, effects)
        info = meta.card(card_id)
        value = upgrade_value(card_id, card_type, str(info.get("rarity", "")))
        if value <= 0:
            continue
        reasons: list[str] = []
        # A card the deck's archetype leans on is the one to invest in.
        for arch_key, hits in archetypes.items():
            if hits < 2:
                continue
            for arch in IRONCLAD_ARCHETYPES:
                if arch.key == arch_key and card_id in (arch.core | arch.payoff):
                    value += 1
                    reasons.append(f"卡组主轴「{arch.label}」的核心")
        if act <= 1 and card_id == "Bash":
            reasons.append("第一幕把「痛击」升到 3 层易伤，精英战最好用")
        elif value >= 3:
            reasons.append("升级会改变这张牌的作用，不只是加数值")
        elif card_type == "POWER":
            reasons.append("能力牌优先：升级收益持续整局")
        if best is None or value > best.value:
            best = Choice(card_id=card_id, value=value, reasons=reasons)
    if best is not None and not best.reasons:
        best.reasons.append("当前卡组里升级收益最高")
    return best


def best_removal(deck_ids: list[str],
                 effects: dict[str, str] | None = None) -> Choice | None:
    """Which card to delete, when the game offers a removal.

    "Remove Strikes first, followed by Defends" is the community's order and it
    is close to universal, because a Strike is nearly a curse once better attacks
    exist. Curses and statuses outrank both: a Wound is worse than a Strike.

    This exists because the grid handler used to take the FIRST card when it had
    no pending intent — and on a card-removal screen that can delete the best
    card in the deck.
    """
    if not deck_ids:
        return None
    best: Choice | None = None
    for card_id in deck_ids:
        _text, card_type = _effect_and_type(card_id, effects)
        info = meta.card(card_id)
        value = removal_value(card_id, card_type, str(info.get("rarity", "")))
        if value <= 0:
            continue
        reasons: list[str] = []
        if card_type in ("CURSE", "STATUS"):
            reasons.append("诅咒/状态牌，留着只会占手牌")
        elif "Strike" in card_id:
            reasons.append("打击：有了更好的攻击牌之后基本等于诅咒")
        elif "Defend" in card_id:
            reasons.append("防御：真正的格挡牌到位后，起手防御最不值钱")
        else:
            reasons.append("这张牌当前收益最低")
        if best is None or value > best.value:
            best = Choice(card_id=card_id, value=value, reasons=reasons)
    return best


_AXIS_ZH = {"damage": "伤害", "aoe": "群伤", "block": "格挡", "scaling": "成长",
            "draw": "抽牌", "energy": "能量", "utility": "功能"}


# --------------------------------------------------------------------------- #
# Synergy: structural rules, not a table of remembered card pairs
# --------------------------------------------------------------------------- #
#: (what the deck already has, what this card is, weight, why it matters)
_SYNERGY_RULES: tuple[tuple[str, str, float, str], ...] = (
    ("exhaust", "exhaust_payoff", 0.30, "卡组有消耗引擎，它是收尾手段"),
    ("exhaust", "exhaust", 0.12, "延续消耗引擎"),
    ("strength", "strength_payoff", 0.30, "卡组在堆力量，它是输出放大器"),
    ("strength", "strength", 0.15, "继续加深力量流"),
    ("block", "block_payoff", 0.28, "卡组有格挡产量，它把格挡换成伤害"),
    ("block", "block", 0.12, "继续加深格挡"),
    ("status_add", "status_payoff", 0.28, "卡组能把异常牌换成收益"),
    ("self_damage", "self_damage", 0.20, "卡组把掉血当资源"),
    ("draw", "draw", 0.10, "补足过牌，关键牌更常出现"),
    ("energy", "energy", 0.10, "补足能量，回合能打满"),
)


def _effect_and_type(card_id: str, effects: dict[str, str] | None) -> tuple[str, str]:
    """Effect text and type for a card, preferring the game's own data.

    `card_id` is coerced to a string because the deck arrives as raw game
    entries: passing one through used to raise `unhashable type: dict` from the
    lookup, deep inside a recommendation. Degrading to "unknown card" is the
    only acceptable failure mode for code on the advice path.
    """
    if not isinstance(card_id, str):
        return "", ""
    text = (effects or {}).get(card_id, "") or ""
    if not text:
        try:
            from spirebrain import gamedata

            text = gamedata.get().card_effect(card_id) or ""
        except Exception:  # noqa: BLE001 - data is optional; authored rows still work
            text = ""
    return text, meta.type_of(card_id)


@dataclass
class DeckProfile:
    """What the deck currently is, per the cards actually in it."""

    cards: list[str] = field(default_factory=list)
    axes: dict[str, float] = field(default_factory=dict)
    archetypes: dict[str, int] = field(default_factory=dict)
    type_counts: dict[str, int] = field(default_factory=dict)
    basics: int = 0
    avg_cost: float = 0.0
    unknown: list[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.cards)

    @property
    def dominant(self) -> str | None:
        """The archetype the deck has most committed to, if any."""
        if not self.archetypes:
            return None
        key, hits = max(self.archetypes.items(), key=lambda kv: kv[1])
        return key if hits >= 2 else None

    def need(self, axis: str) -> float:
        """How badly this deck wants more of `axis` (0 = satisfied, 1 = starving)."""
        target = NEED.get(axis, 1.0)
        if target <= 0:
            return 0.0
        have = self.axes.get(axis, 0.0)
        return max(0.0, min(1.0, (target - have) / target))

    def archetype_label(self, key: str) -> str:
        for arch in IRONCLAD_ARCHETYPES:
            if arch.key == key:
                return arch.label
        return key


def profile(deck_ids: list[str], effects: dict[str, str] | None = None) -> DeckProfile:
    """Build the profile. Unknown cards are recorded, never guessed at silently."""
    prof = DeckProfile(cards=[c for c in deck_ids if c])
    costs: list[float] = []
    for card_id in prof.cards:
        text, card_type = _effect_and_type(card_id, effects)
        info = meta.card(card_id)
        if not info:
            prof.unknown.append(card_id)
        is_basic = meta.rarity(card_id) == "BASIC"
        for axis, value in knowledge(card_id, text, card_type).axes.items():
            # A Strike is not a damage plan and a Defend is not a block engine:
            # the community is blunt that the starters are removal targets ("a
            # 30-card deck with no Strikes beats a 40-card deck with five").
            # Counting them at full value made six Strikes look like a deck that
            # had damage covered, so every real attack scored as redundant.
            prof.axes[axis] = prof.axes.get(axis, 0.0) + (value * 0.5 if is_basic else value)
        if card_type:
            prof.type_counts[card_type] = prof.type_counts.get(card_type, 0) + 1
        if is_basic:
            prof.basics += 1
        cost = info.get("cost")
        if isinstance(cost, int) and cost >= 0:
            costs.append(cost)
    prof.archetypes = archetype_signal(prof.cards)
    prof.avg_cost = round(sum(costs) / len(costs), 2) if costs else 0.0
    return prof


def synergy(deck_ids: list[str], card_id: str,
            effects: dict[str, str] | None = None) -> list[tuple[str, float]]:
    """Reasons this card is better *here* than it is in the abstract."""
    text, card_type = _effect_and_type(card_id, effects)
    mine = knowledge(card_id, text, card_type).tags
    theirs: set[str] = set()
    for other in deck_ids:
        other_text, other_type = _effect_and_type(other, effects)
        theirs |= knowledge(other, other_text, other_type).tags
    return [(reason, weight) for producer, consumer, weight, reason in _SYNERGY_RULES
            if producer in theirs and consumer in mine]


def _archetype_reasons(deck_ids: list[str], card_id: str) -> tuple[list[str], float]:
    """Bonus for deepening the archetype the deck already committed to."""
    present = set(deck_ids)
    reasons: list[str] = []
    bonus = 0.0
    for arch in IRONCLAD_ARCHETYPES:
        if len(present & (arch.core | arch.payoff)) < 2:
            continue          # one card is not a commitment
        if card_id in arch.core:
            bonus += 0.30
            reasons.append(f"推进「{arch.label}」（核心）")
        elif card_id in arch.payoff:
            bonus += 0.18
            reasons.append(f"配合「{arch.label}」")
    return reasons, bonus


def _caution_cleared(deck_ids: list[str], card_id: str,
                     effects: dict[str, str] | None) -> bool:
    """Is a cautioned card's condition actually met here?

    The caution text names its own condition ("take only with Evolve or Fire
    Breathing"). Rather than duplicate that logic per card, ask the deck: if the
    card's structural partner is present, the caution is satisfied.
    """
    partner_of = {
        "Wild Strike": "status_payoff",
        "Reckless Charge": "status_payoff",
        "Havoc": "exhaust_payoff",
        "Body Slam": "block",
        "Perfected Strike": "strike",
    }
    want = partner_of.get(card_id)
    if want is None:
        return False
    theirs: set[str] = set()
    for other in deck_ids:
        other_text, other_type = _effect_and_type(other, effects)
        theirs |= knowledge(other, other_text, other_type).tags
    return want in theirs


@dataclass
class PickGrade:
    """A card's local desirability, with the sentences that produced it."""

    card_id: str
    score: float
    fills: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    caution: str = ""
    dilution: float = 0.0

    @property
    def verdict(self) -> str:
        """Coarse bucket for logging and for the model-facing prompt."""
        if self.caution:
            return "risky"
        if self.score >= 0.62:
            return "strong"
        if self.score >= 0.48:
            return "fine"
        if self.score >= 0.34:
            return "marginal"
        return "poor"

    def reason_text(self) -> str:
        """One player-facing sentence, used by the panel and the advice log."""
        parts = list(self.reasons)
        if self.caution:
            parts.append(f"注意：{self.caution}")
        return "；".join(p for p in parts if p)


def grade(prof: DeckProfile, card_id: str, act: int = 1,
          effects: dict[str, str] | None = None) -> PickGrade:
    """Score one candidate against this deck and this act.

    Shape of the number: covering a real need dominates (the community's own
    rule is "take the card that solves a problem"), then archetype commitment
    and structural synergy, then act timing, minus a discount that grows with
    deck size — an average card is worse in a 28-card deck than in a 15-card one.
    """
    text, card_type = _effect_and_type(card_id, effects)
    know = knowledge(card_id, text, card_type)

    contributions = {axis: v for axis, v in know.axes.items() if v > 0}
    if contributions:
        weight_sum = sum(contributions.values())
        need_fit = sum(prof.need(axis) * v for axis, v in contributions.items()) / weight_sum
    else:
        need_fit = 0.0

    act_priority = know.priority(act) / 3.0
    # `need_fit` alone would love a scaling card in Act 1: a fresh deck scores
    # 1.00 on scaling need, so Demon Form looked like a fine first pick. It is
    # not — it is a dead card until the deck can survive long fights, which is
    # exactly what the per-act table encodes and what the community is emphatic
    # about. So the act priority gates the whole score instead of nudging it.
    gate = 0.55 if act_priority == 0.0 else 1.0
    score = need_fit * 0.55 * gate

    arch_reasons, arch_bonus = _archetype_reasons(prof.cards, card_id)
    syn = synergy(prof.cards, card_id, effects)
    syn_bonus = min(0.35, sum(w for _, w in syn))
    score += min(0.50, arch_bonus + syn_bonus) * gate

    score += act_priority * 0.18
    # Intrinsic quality: the best act for a card is a decent proxy for how good
    # the card is at all (a card that is priority 3 somewhere beats one that is
    # priority 1 everywhere). Without this term a copy of a strong card — say
    # Uppercut — was rejected as "damage already covered" purely because the
    # deck's needs happened to sit elsewhere, which is not how drafting works.
    score += (max(know.acts) / 3.0) * 0.15

    oversize = max(0, prof.size - COMFORTABLE_SIZE)
    dilution = min(0.30, oversize * 0.02) + (0.10 if prof.size >= 28 else 0.0)
    score -= dilution

    caution = CAUTION.get(card_id, "")
    if caution and _caution_cleared(prof.cards, card_id, effects):
        caution = ""            # the condition this caution names is present

    fills = [_AXIS_ZH.get(a, a) for a in
             sorted(contributions, key=lambda k: -know.axes[k])]
    reasons: list[str] = []
    if act_priority == 0.0:
        # Say the timing out loud; a silent low score teaches the player nothing.
        reasons.append(f"第{'一二三'[max(0, min(2, act - 1))]}幕偏弱，通常留到后面")
    if fills:
        head = "、".join(fills[:2])
        reasons.append(f"补足{head}" if need_fit >= 0.45 else f"{head}已有基础")
    reasons.extend(arch_reasons)
    reasons.extend(reason for reason, _ in syn)
    if prof.size >= 22:
        reasons.append(f"卡组已 {prof.size} 张，只拿明显更好的")

    return PickGrade(card_id=card_id, score=max(0.0, min(1.0, score)),
                     fills=fills, reasons=reasons, caution=caution,
                     dilution=round(dilution, 3))
