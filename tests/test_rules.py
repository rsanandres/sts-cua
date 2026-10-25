import pytest
from coopstates import NODES, card, combat, event_state, lobby, map_state, rewards_state, treasure_state

from sts_cua import rules
from sts_cua.actions import Key, Wait
from sts_cua.coop import summarize
from sts_cua.rules import decide, majority, map_tiebreak


def ruling(raw):
    return decide(summarize(raw))


# ---- majority over the other players' votes (2 to 4 players)

@pytest.mark.parametrize("votes, expected", [
    ([(3, 4)], [(3, 4)]),                                   # 2 players: follow the one teammate
    ([(3, 4), (3, 4)], [(3, 4)]),                           # 3 players, agreed
    ([(3, 4), (1, 4)], [(3, 4), (1, 4)]),                   # 3 players, split: tie
    ([(1, 4), (3, 4), (1, 4)], [(1, 4)]),                   # 4 players, 2 to 1
    ([(1, 4), (3, 4), (5, 4)], [(1, 4), (3, 4), (5, 4)]),   # 4 players, all different
    ([[3, 4], [3, 4], None], [(3, 4)]),                     # list coords; an unvoted entry is ignored
    ([None, None], []),
    ([], []),
    ([1, 0, 1], [1]),                                       # event options
])
def test_majority(votes, expected):
    assert majority(votes) == expected


# ---- vote last, match the majority

def test_never_leads_a_vote():
    for others in ((None,), ((3, 4), None), ((3, 4), (3, 4), None)):
        r = ruling(map_state(others=others))
        assert r.rule == "wait_turn" and r.hold and r.actions == (Wait(rules.HOLD_SECONDS),)
        assert r.why.startswith("teammates_voting")


@pytest.mark.parametrize("others, target, k", [
    (((5, 4),), "elite", "3rd"),                       # 2 players
    (((1, 4), (1, 4)), "monster", "1st"),              # 3 players, agreed
    (((5, 4), (1, 4), (5, 4)), "elite", "3rd"),        # 4 players, majority
])
def test_vote_map_follows_the_majority(others, target, k):
    r = ruling(map_state(others=others))
    assert r.rule == "vote_map" and not r.actions and "majority" in r.why
    assert f"the {target} node, the {k} of the 3 nodes" in r.directive


def test_map_tie_uses_the_preference_table():
    r = ruling(map_state(others=((5, 4), (3, 4))))  # elite vs rest site
    assert "rest site" in r.directive and "MAP_PREFERENCE" in r.why
    r = ruling(map_state(others=((5, 4), (1, 4))))  # elite vs monster
    assert "monster" in r.directive
    # All different with 4 players: monster first at full HP, rest first at low HP.
    assert "monster" in ruling(map_state(others=((1, 4), (3, 4), (5, 4)))).directive
    assert "rest site" in ruling(map_state(others=((1, 4), (3, 4), (5, 4)), hp=20)).directive


def test_map_tiebreak_table():
    assert map_tiebreak(NODES, low_hp=False)["type"] == "Monster"
    assert map_tiebreak(NODES, low_hp=True)["type"] == "RestSite"
    two_monsters = [{"col": 4, "type": "Monster"}, {"col": 2, "type": "Monster"}]
    assert map_tiebreak(two_monsters, low_hp=False)["col"] == 2


def test_after_voting_it_waits():
    r = ruling(map_state(local=(5, 4), others=(None,)))
    assert r.rule == "wait_turn" and r.why.startswith("map_vote")


def test_vote_event():
    assert ruling(event_state(others=(None,))).rule == "wait_turn"
    r = ruling(event_state(others=(1,)))
    assert r.rule == "vote_event" and '"Leave"' in r.directive
    r = ruling(event_state(others=(0, 1)))  # tie: the model picks among the tied options
    assert "one of these" in r.directive and '"Take the gold"' in r.directive and '"Leave"' in r.directive
    assert ruling(event_state(others=(1, 1, 0))).directive.count('"') == 2


def test_bid_treasure_avoids_teammates_relics():
    assert ruling(treasure_state(others=(None,))).rule == "wait_turn"
    r = ruling(treasure_state(others=(0,)))
    assert r.rule == "bid_treasure" and "Lantern" in r.directive and "Anchor" not in r.directive
    assert ruling(treasure_state(others=(0, 1))) is None  # nothing free: the model decides


# ---- turns

def test_waits_when_it_is_not_the_agents_move():
    for raw in (combat(local_ready=True), combat(turn="enemy", play_phase=False), lobby(local_ready=True),
                combat(local_alive=False)):
        r = ruling(raw)
        assert r.rule == "wait_turn" and r.hold


def test_end_turn_when_nothing_is_playable():
    r = ruling(combat(hand=[card(0, can_play=False), card(1, "Bash", can_play=False)]))
    assert r.rule == "end_turn" and r.actions == (Key("e"),)
    assert ruling(combat(hand=[])).rule == "end_turn"
    assert ruling(combat(hand=[card(0, can_play=False), card(1, can_play=True)])) is None  # the model plays
    unknown = combat()
    del unknown["player"]["hand"]
    assert ruling(unknown) is None


# ---- rewards, proceed, lobby

def test_take_rewards():
    r = ruling(rewards_state())
    assert r.rule == "take_rewards" and "17 gold" in r.directive and "Fire Potion" in r.directive
    assert ruling(rewards_state(items=(("card", None),))) is None  # a card pick is the model's


def test_proceed():
    r = ruling(rewards_state(items=()))
    assert r.rule == "proceed" and "Proceed" in r.directive
    assert ruling(rewards_state(items=(), can_proceed=False)) is None
    raw = event_state(shared=False)
    for o in raw["event"]["options"]:
        o["is_proceed"] = True
    assert ruling(raw).rule == "proceed"
    assert ruling(event_state(shared=False)) is None  # a real event choice


def test_lobby_ready():
    r = ruling(lobby())
    assert r.rule == "lobby_ready" and "confirm" in r.directive and "Never press Back" in r.directive


# ---- routing

def test_model_decides_real_choices():
    assert ruling(combat()) is None  # cards to play
    assert decide(None) is None
    assert decide({}) is None


def test_rules_table_is_ordered_and_named():
    names = [name for name, _ in rules.RULES]
    assert names[0] == "wait_turn" and len(names) == len(set(names))
    for name, fn in rules.RULES:
        assert fn.__name__ == name
