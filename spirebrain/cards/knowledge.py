"""Card knowledge: what a card *does for a deck*, and when it is worth taking.

Sources, and how much each is trusted
-------------------------------------
* **The game itself** (`gamedata` + `tools/extract_card_meta.py`): cost, type,
  rarity and the real effect text. Trusted absolutely — it is the build the
  player is running.
* **Community consensus**, three independent write-ups that agree with each
  other: Forgotten Arbiter's *Ironclad Win Streak Guide* (the canonical
  defensive-Ironclad strategy: small deck, remove Strikes first, upgrade
  Powers > utility > defence > attacks, rest below ~50% HP), and two tier lists
  built on aggregate run data (win rate / pick rate) — see
  `docs/CARD_STRATEGY.md` for the links and for what each one is worth.
* **Nothing else.** Where a claim is not supported by the text in front of us or
  by those sources, it is not in this file. A wrong card rating is worse than no
  rating: it launders opinion into a number the player cannot audit.

Version discipline: the guide above was written in 2018 and one of its specific
notes (Feel No Pain) is stale, so **card *numbers* were never taken from the
web** — only strategy structure was. Every number in `axes` is a coarse
judgement (0-3) about *role*, not a claim about damage.

The model
---------
A card is described on two independent levels:

* `axes` — how much it contributes to each capability a deck needs. Coarse on
  purpose (0-3); summing them gives a deck profile.
* `tags` — what it *is*, so synergy can be detected structurally instead of by
  memorising card pairs (`{"exhaust"}` + a card that reads "whenever a card is
  Exhausted" is a real synergy; that is a rule, not a table entry).
* `acts` — priority per act, because the same card is a great Act 1 pick and a
  dead Act 3 draw (front-loaded damage early, defence and scaling later).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from spirebrain.cards import meta

#: The capabilities a deck must balance. The community framing is blunt and
#: repeated everywhere: a deck needs damage, defence and scaling, and it loses
#: to whichever it is missing.
AXES = ("damage", "aoe", "block", "scaling", "draw", "energy", "utility")

#: Roughly what a healthy mid-run deck carries per axis, used to turn a profile
#: into "what is this deck short of". Derived from the deck-size guidance
#: (15-30 cards, ~1/3 defence, front-loaded damage early) rather than from a
#: single source, so they are intentionally loose.
NEED = {"damage": 15.0, "aoe": 4.0, "block": 14.0, "scaling": 5.0,
        "draw": 5.0, "energy": 2.0, "utility": 3.0}


@dataclass(frozen=True)
class CardKnowledge:
    tags: frozenset[str] = frozenset()
    axes: Mapping[str, float] = field(default_factory=dict)
    #: Priority per act (0 = avoid, 3 = top pick), index 0-2 = act 1-3.
    acts: tuple[int, int, int] = (1, 1, 1)
    note: str = ""

    def priority(self, act: int) -> int:
        index = max(0, min(2, int(act) - 1))
        return self.acts[index]


def _k(tags: str = "", axes: str = "", acts: str = "1,1,1", note: str = "") -> CardKnowledge:
    """Compact row builder: `_k("exhaust draw", "draw:2,utility:1", "2,3,3", "...")`."""
    parsed_axes: dict[str, float] = {}
    for part in axes.split(","):
        part = part.strip()
        if not part:
            continue
        key, _, value = part.partition(":")
        parsed_axes[key.strip()] = float(value or 1)
    return CardKnowledge(
        tags=frozenset(t for t in tags.split() if t),
        axes=parsed_axes,
        acts=tuple(int(x) for x in acts.split(",")),  # type: ignore[arg-type]
        note=note,
    )


# --------------------------------------------------------------------------- #
# Ironclad — authored against the effect text in this install (75 cards)
# --------------------------------------------------------------------------- #
# Axes are role strength (0-3), not damage numbers. `acts` is pick priority.
IRONCLAD: dict[str, CardKnowledge] = {
    # -- basics ------------------------------------------------------------- #
    "Strike_Red":  _k("strike attack", "damage:1", "2,1,0", "a 6-damage card that stops being worth a draw"),
    "Defend_Red":  _k("defend", "block:1", "2,1,0", "removal target once better block exists"),
    "Bash":        _k("vulnerable attack", "damage:2,utility:1", "3,2,2",
                      "the starter's only Vulnerable source; upgrading it is a common first Rest Site play"),

    # -- common attacks ----------------------------------------------------- #
    "Anger":       _k("attack cheap", "damage:1", "2,1,0", "0-cost damage early; the copy is a liability in a big deck"),
    "Body Slam":   _k("block_payoff attack", "damage:1", "0,2,2", "only with a real block engine (Barricade/Entrench)"),
    "Clash":       _k("conditional attack", "damage:2", "1,0,0", "unplayable the moment a Skill sits in hand"),
    "Cleave":      _k("aoe attack", "aoe:1,damage:1", "2,1,1", "early answer to multi-enemy floors"),
    "Clothesline": _k("weak attack", "damage:2,utility:1", "2,2,2", "damage plus a real defensive debuff"),
    "Headbutt":    _k("attack", "damage:1,draw:1", "1,1,1", "recursion for a key card"),
    "Heavy Blade": _k("strength_payoff attack", "damage:2", "1,2,3", "multiplies Strength; dead without it"),
    "Iron Wave":   _k("defend attack", "damage:1,block:1", "2,2,2", "half a Strike and half a Defend in one card"),
    "Perfected Strike": _k("strike attack strike_payoff", "damage:2", "1,0,0",
                           "wants a Strike-heavy deck; diluted by every better card you add"),
    "Pommel Strike": _k("attack draw", "damage:1,draw:1", "3,2,2", "damage that also cycles; upgrades well"),
    "Sword Boomerang": _k("attack random", "damage:1", "1,1,1", "random targeting is unreliable in focused fights"),
    "Thunderclap": _k("aoe vulnerable attack", "aoe:1,utility:1", "1,1,1", "AoE Vulnerable"),
    "Twin Strike": _k("strength_payoff attack", "damage:2", "2,2,2", "two hits means Strength applies twice"),
    "Wild Strike": _k("status_add attack", "damage:2", "1,1,1", "wants Evolve or Fire Breathing to pay the Wound off"),

    # -- common skills ------------------------------------------------------ #
    "Armaments":   _k("defend upgrade", "block:1,utility:2", "2,2,1",
                      "the workaround for scarce Rest Site upgrades"),
    "Flex":        _k("strength cheap", "damage:1", "1,0,0", "temporary Strength; wants multi-hit attacks"),
    "Havoc":       _k("exhaust", "utility:1", "0,1,1", "plays a card you did not choose; needs an exhaust payoff to be worth the risk"),
    "Shrug It Off": _k("defend draw", "block:2,draw:1", "3,2,2", "block that replaces itself; almost never a bad card"),
    "True Grit":   _k("defend exhaust", "block:2", "2,2,2", "block that also thins the deck"),
    "Warcry":      _k("draw exhaust", "draw:1,utility:1", "1,1,1", "sets up a specific draw"),

    # -- uncommon attacks --------------------------------------------------- #
    "Blood for Blood": _k("self_damage attack", "damage:2", "2,2,2", "cheap once you have paid HP"),
    "Carnage":     _k("attack ethereal", "damage:3", "3,2,1", "big Act 1 number for elite and boss races"),
    "Dropkick":    _k("vulnerable_payoff attack energy", "damage:1,energy:1,draw:1", "1,2,2",
                      "energy and a card when the target is Vulnerable"),
    "Hemokinesis": _k("self_damage attack", "damage:2", "2,2,2", "damage per energy, paid in HP"),
    "Pummel":      _k("strength_payoff exhaust attack", "damage:2", "1,2,2", "four Strength applications"),
    "Rampage":     _k("scaling attack", "damage:2,scaling:1", "1,2,2", "grows every time it is played"),
    "Reckless Charge": _k("status_add attack cheap", "damage:1", "1,1,0", "a 0-cost attack that hands you a Dazed"),
    "Searing Blow": _k("scaling attack upgrade_sink", "damage:3,scaling:1", "0,1,1",
                       "only pays off if you can upgrade it repeatedly"),
    "Sever Soul":  _k("exhaust attack", "damage:2,utility:1", "1,2,2", "turns a hand of Skills into damage"),
    "Uppercut":    _k("weak vulnerable attack", "damage:2,utility:2", "2,3,3", "two debuffs on one card"),
    "Whirlwind":   _k("aoe strength_payoff scaling attack", "aoe:3,scaling:1", "1,2,3",
                      "converts energy and Strength into a full-board swing"),

    # -- uncommon powers ---------------------------------------------------- #
    "Combust":     _k("aoe self_damage", "aoe:2", "1,2,2", "slow AoE that charges HP rent"),
    "Dark Embrace": _k("exhaust draw power", "draw:2", "1,3,3", "draw engine for an exhaust deck"),
    "Evolve":      _k("status_payoff draw power", "draw:2", "1,2,2", "turns Wounds and Burns into cards"),
    "Feel No Pain": _k("exhaust block power", "block:2", "2,3,3", "block engine for an exhaust deck"),
    "Fire Breathing": _k("status_payoff aoe power", "aoe:2", "1,2,2", "turns every status draw into AoE damage"),
    "Inflame":     _k("strength power", "damage:2,scaling:1", "3,3,3", "permanent Strength for one energy"),
    "Metallicize": _k("block power", "block:2", "2,2,1", "free block every turn"),
    "Rupture":     _k("strength self_damage power", "scaling:2", "1,2,2", "Strength from paid HP"),

    # -- uncommon skills ---------------------------------------------------- #
    "Battle Trance": _k("draw cheap", "draw:3", "3,2,1", "three cards for zero energy"),
    "Bloodletting": _k("energy self_damage cheap", "energy:3", "2,3,2", "energy, paid in HP"),
    "Burning Pact": _k("exhaust draw", "draw:2,utility:1", "2,2,2", "cycles and thins at once"),
    "Disarm":      _k("debuff exhaust", "utility:3", "1,2,2", "permanently shrinks a multi-attack enemy"),
    "Dual Wield":  _k("copy", "utility:2", "1,1,1", "copies a payoff card; needs one worth copying"),
    "Entrench":    _k("block block_payoff", "block:2", "0,2,2", "doubles block; wants Barricade or a big block turn"),
    "Flame Barrier": _k("block thorns", "block:2,utility:1", "2,2,2", "block plus punish for multi-hit attackers"),
    "Ghostly Armor": _k("block ethereal", "block:2", "2,2,1", "cheap block that must be played the turn it appears"),
    "Infernal Blade": _k("exhaust", "utility:1", "1,2,1", "a free attack now; random"),
    "Intimidate":  _k("weak aoe exhaust", "utility:2", "1,1,1", "AoE Weak"),
    "Power Through": _k("block status_add", "block:3", "2,2,2", "a large block card paid for with two Wounds"),
    "Rage":        _k("block attack_payoff cheap", "block:2", "1,1,1", "block per Attack in a big Attack turn"),
    "Second Wind": _k("exhaust block", "block:2", "1,2,2", "converts a hand of Skills into block"),
    "Seeing Red":  _k("energy exhaust", "energy:2", "1,2,2", "two energy for one card"),
    "Sentinel":    _k("block exhaust energy", "block:1,energy:1", "1,2,1", "block that pays energy back when exhausted"),
    "Shockwave":   _k("debuff aoe exhaust", "utility:3", "2,3,3", "Weak and Vulnerable on every enemy"),
    "Spot Weakness": _k("strength", "scaling:2", "2,3,3", "three Strength when the enemy telegraphs an attack"),

    # -- rare attacks ------------------------------------------------------- #
    "Bludgeon":    _k("attack burst", "damage:3", "1,2,2", "one huge hit for three energy"),
    "Feed":        _k("heal attack exhaust", "damage:2,scaling:1", "2,3,3", "permanent Max HP on a kill"),
    "Fiend Fire":  _k("exhaust_payoff attack", "damage:3,scaling:1", "1,3,3", "the exhaust deck's finisher; under-drafted by the community"),
    "Immolate":    _k("aoe attack status_add", "aoe:3", "2,3,2", "big AoE that costs you a Burn"),
    "Reaper":      _k("aoe heal attack", "aoe:2", "1,2,3", "heals the run back up in multi-enemy fights"),

    # -- rare powers -------------------------------------------------------- #
    "Barricade":   _k("block scaling power", "block:3,scaling:2", "0,2,3",
                      "block persists; a win condition, but a dead Act 1 draw"),
    "Berserk":     _k("energy self_damage power", "energy:3", "1,1,1", "permanent energy for permanent Vulnerable"),
    "Brutality":   _k("draw self_damage power", "draw:2", "1,2,2", "a card every turn, paid in HP"),
    "Corruption":  _k("exhaust energy power", "energy:3,utility:2", "1,3,3",
                      "Skills become free and exhaust; the exhaust engine's switch"),
    "Demon Form":  _k("strength scaling power", "scaling:3,damage:1", "0,2,3",
                      "wins long fights; a dead card in a short Act 1 one"),
    "Juggernaut":  _k("block_payoff power", "damage:2,scaling:1", "0,2,2", "turns every block gain into damage"),

    # -- rare skills -------------------------------------------------------- #
    "Double Tap":  _k("attack_payoff", "damage:2", "1,2,2", "doubles one Attack this turn"),
    "Exhume":      _k("exhaust_payoff", "utility:2", "0,1,2", "brings an exhausted card back"),
    "Impervious":  _k("block", "block:3", "1,3,3", "30 block on demand; the panic button that saves boss fights"),
    "Limit Break": _k("strength strength_payoff scaling", "scaling:3", "0,3,3",
                      "doubles Strength; worthless before you have any"),
    "Offering":    _k("draw energy self_damage exhaust", "draw:3,energy:2", "3,3,3",
                      "6 HP for a whole turn; the deck's best card by community win rate"),
}


# --------------------------------------------------------------------------- #
# Campfire and card removal: the two card choices that are not "add a card"
# --------------------------------------------------------------------------- #
#: How much an upgrade is worth, as a role judgement (0-3), not a damage delta.
#:
#: Sources: Forgotten Arbiter's guide ("Whirlwind is the highest priority,
#: followed by True Grit and Body Slam. Afterwards, generally Powers > Utility
#: Skills > Defensive Skills > Attacks"; Bash is medium-high because 2 -> 3
#: Vulnerable carries Act 1 elites), and the tier lists, which agree that an
#: upgrade that changes what a card *does* (Limit Break stops exhausting,
#: Corruption gets cheaper) beats one that only adds a number.
UPGRADE_PRIORITY: dict[str, int] = {
    "Whirlwind": 3, "True Grit": 3, "Body Slam": 3, "Limit Break": 3,
    "Corruption": 3, "Demon Form": 3, "Barricade": 3, "Feel No Pain": 3,
    "Bash": 3,          # 2 -> 3 Vulnerable is what makes Act 1 elites killable
    "Uppercut": 3, "Pommel Strike": 3, "Dark Embrace": 3, "Impervious": 3,
    "Shockwave": 2, "Battle Trance": 2, "Offering": 2, "Fiend Fire": 2,
    "Inflame": 2, "Spot Weakness": 2, "Armaments": 2, "Shrug It Off": 2,
    "Flame Barrier": 2, "Power Through": 2, "Metallicize": 2, "Exhume": 2,
    "Second Wind": 2, "Sever Soul": 2, "Reaper": 2, "Feed": 2, "Immolate": 2,
    "Bludgeon": 1, "Heavy Blade": 1, "Twin Strike": 1, "Cleave": 1,
    "Clothesline": 1, "Thunderclap": 1, "Anger": 1, "Iron Wave": 1,
    "Bloodletting": 1, "Seeing Red": 1, "Intimidate": 1, "Disarm": 1,
    "Burning Pact": 1, "Rampage": 1, "Dropkick": 1, "Hemokinesis": 1,
    "Headbutt": 1, "Warcry": 1, "Dual Wield": 1, "Double Tap": 1,
    "Entrench": 1, "Ghostly Armor": 1, "Juggernaut": 1, "Rupture": 1,
    "Evolve": 1, "Fire Breathing": 1, "Brutality": 1, "Berserk": 1,
    "Combust": 1, "Sentinel": 1, "Pummel": 1, "Infernal Blade": 1,
    "Sword Boomerang": 1, "Rage": 1, "Flex": 1,
    # Starter cards are never the upgrade: they are the removal.
    "Strike_Red": 0, "Defend_Red": 0, "Strike_R": 0, "Defend_R": 0,
}

#: Anything in these packages is a removal target before any real card: a Wound
#: or a curse in hand is worse than a Strike.
REMOVAL_ALWAYS = {"CURSE", "STATUS"}

#: The starter cards by name, for snapshots that key cards by display name
#: rather than by id (the tests do, and so does a modded install that reports
#: names instead).
_STARTERS = {"Strike", "Defend", "Strike_R", "Strike_Red", "Defend_R", "Defend_Red"}


def upgrade_value(card_id: str, card_type: str = "", rarity: str = "") -> int:
    """How much this card wants the campfire, with an honest fallback.

    The authored table covers what the sources cover; everything else falls back
    to the guide's structural rule — Powers before Skills before Attacks — rather
    than to a guess about a specific card.
    """
    known = UPGRADE_PRIORITY.get(card_id)
    if known is not None:
        return known
    if rarity == "BASIC":
        return 0
    if card_type == "POWER":
        return 2
    if card_type in ("SKILL", "ATTACK"):
        return 1
    return 0


def removal_value(card_id: str, card_type: str = "", rarity: str = "") -> int:
    """How badly this card should be the one removed (higher = better target).

    Only what the sources actually say is scored, in their order of severity:

    * curses and statuses (4) — a Wound in hand is worse than a Strike;
    * basic **Strikes** (3), then basic **Defends** (2) — "remove Strikes first,
      followed by Defends" is Forgotten Arbiter's rule, close to universal.

    Two wrong versions preceded this one, and both are the two ways a name rule
    goes bad, so they are recorded:

    * `rarity == "BASIC"` classified Bash — a card you never remove — as a target;
    * a bare `"Strike" in card_id` matched **Pommel Strike**, a good card, so a
      deck with no basic Strikes left proposed removing a real attack.

    The rule therefore needs a starter name AND the basic rarity, plus an explicit
    list for snapshots that carry no metadata. Everything else scores 0: a
    defensible "least useful card" ranking needs quality data this project does
    not have, and inventing one is the confident guess this layer exists to avoid.
    """
    if card_type in REMOVAL_ALWAYS:
        return 4
    is_starter = card_id in _STARTERS or (
        rarity == "BASIC" and ("Strike" in card_id or "Defend" in card_id))
    if not is_starter:
        return 0
    return 3 if "Strike" in card_id else 2

#: Cards that actively hurt a deck unless something in the deck pays them off.
#: Not a ban list: each entry names the condition that makes it correct, and
#: `deck._caution_cleared` checks that condition against the real deck.
CAUTION: dict[str, str] = {
    "Clash": "手里有非攻击牌就打不出去",
    "Wild Strike": "每打一次塞一张「伤口」，需要 Evolve / Fire Breathing 兜底",
    "Reckless Charge": "每打一次塞一张「眩晕」，需要异常牌收益",
    "Searing Blow": "吃升级次数，除非打算长期喂篝火，否则是负担",
    "Perfected Strike": "需要打击牌多的卡组，每加一张好牌都在削弱它",
    "Havoc": "打出的是你没挑过的牌，除非有强消耗收益",
    "Berserk": "永久易伤在第二幕之后是真代价",
    "Combust": "每回合掉 1 血，没有回复手段时是慢性自杀",
}


# --------------------------------------------------------------------------- #
# Archetypes and synergy
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Archetype:
    key: str
    label: str
    #: Cards that start the archetype (the signal to commit).
    core: frozenset[str]
    #: Cards that get much better once the archetype is online.
    payoff: frozenset[str]
    tag: str
    note: str


IRONCLAD_ARCHETYPES: tuple[Archetype, ...] = (
    Archetype(
        key="strength", label="力量成长", tag="strength",
        core=frozenset({"Inflame", "Spot Weakness", "Demon Form", "Limit Break"}),
        payoff=frozenset({"Heavy Blade", "Whirlwind", "Twin Strike", "Sword Boomerang",
                          "Pummel", "Bludgeon", "Reaper"}),
        note="先有力量来源，再谈倍数与多段攻击；Limit Break 在力量为 0 时是废牌。",
    ),
    Archetype(
        key="exhaust", label="消耗引擎", tag="exhaust",
        core=frozenset({"Corruption", "Feel No Pain", "Dark Embrace"}),
        payoff=frozenset({"Fiend Fire", "Sever Soul", "Second Wind", "Burning Pact",
                          "True Grit", "Exhume", "Sentinel", "Havoc"}),
        note="三件套凑齐后常能一回合打穿整副牌组；Fiend Fire 是收尾手段。",
    ),
    Archetype(
        key="block", label="格挡堆叠", tag="block",
        core=frozenset({"Barricade", "Entrench", "Metallicize", "Feel No Pain"}),
        payoff=frozenset({"Body Slam", "Juggernaut", "Impervious", "Power Through"}),
        note="需要先有稳定的格挡产量，Barricade 本身不产格挡。",
    ),
    Archetype(
        key="self_damage", label="自伤收益", tag="self_damage",
        core=frozenset({"Rupture", "Blood for Blood"}),
        payoff=frozenset({"Bloodletting", "Hemokinesis", "Offering", "Combust", "Brutality"}),
        note="把掉血当资源；没有 Rupture / Blood for Blood 时自伤只是自伤。",
    ),
    Archetype(
        key="status", label="异常牌转化", tag="status_add",
        core=frozenset({"Evolve", "Fire Breathing"}),
        payoff=frozenset({"Wild Strike", "Reckless Charge", "Power Through", "Immolate"}),
        note="把塞进手里的 Wound / Burn / Dazed 变成抽牌与伤害。",
    ),
)


def archetype_signal(deck_ids: list[str]) -> dict[str, int]:
    """How invested the deck already is in each archetype (core+payoff cards)."""
    counts: dict[str, int] = {}
    present = set(deck_ids)
    for arch in IRONCLAD_ARCHETYPES:
        hits = len(present & (arch.core | arch.payoff))
        core_hits = len(present & arch.core)
        if hits:
            # Core cards count double: they are what makes an archetype real.
            counts[arch.key] = hits + core_hits
    return counts


def knowledge(card_id: str, effect_text: str = "", card_type: str = "") -> CardKnowledge:
    """Authored knowledge when we have it, else a keyword-derived guess.

    The fallback exists so other characters are not silently treated as blank
    cards: a Defect Power that says "Channel" is still recognised as utility.
    Anything derived this way is marked in `note` so a wrong rating is
    traceable to its cause instead of looking hand-checked.
    """
    known = IRONCLAD.get(card_id)
    if known is not None:
        return known
    return derive(effect_text, card_type or meta.type_of(card_id))


#: Keyword → axis/tag rules for cards we have not authored.
_DERIVE_RULES: tuple[tuple[str, str, str], ...] = (
    ("damage to all", "aoe", "2"),
    ("to ALL enemies", "aoe", "2"),
    ("block", "block", "2"),
    ("draw", "draw", "2"),
    ("energy", "energy", "2"),
    ("strength", "scaling", "2"),
    ("dexterity", "block", "1"),
    ("focus", "scaling", "2"),
    ("vulnerable", "utility", "1"),
    ("weak", "utility", "1"),
    ("poison", "scaling", "2"),
    ("channel", "utility", "1"),
    ("exhaust", "utility", "1"),
    ("heal", "utility", "1"),
)


def derive(effect_text: str, card_type: str = "") -> CardKnowledge:
    """A conservative guess for a card we have no authored row for."""
    text = (effect_text or "").lower()
    axes: dict[str, float] = {}
    tags: set[str] = set()
    for needle, axis, weight in _DERIVE_RULES:
        if needle.lower() in text:
            axes[axis] = max(axes.get(axis, 0.0), float(weight))
            tags.add(axis)
    if card_type == "ATTACK":
        axes["damage"] = max(axes.get("damage", 0.0), 1.0)
    elif card_type == "POWER":
        axes["scaling"] = max(axes.get("scaling", 0.0), 1.0)
    return CardKnowledge(tags=frozenset(tags), axes=axes, acts=(1, 1, 1),
                         note="derived from the card's own text (no authored row)")
