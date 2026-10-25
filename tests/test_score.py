import json

from fakestates import ended, fight, game_over, menu, room

from sts_cua.score import (
    BOSS_POINTS, RunOverDetector, at_main_menu, combats, load_states, run_over, score_run,
)

START = [menu(), menu("character_select", ["ironclad", "confirm"]), menu("tutorial_prompt", ["no", "yes"])]


def act_boss(act, floor, hp=60, boss="Boss"):
    """A map step, the boss fight over two steps, then rewards."""
    return [
        room("map", floor - 1, act, hp, bosses=[boss]),
        fight("boss", floor, act, hp, boss),
        fight("boss", floor, act, hp - 10, boss),
        room("rewards", floor, act, hp - 10),
    ]


def test_win():
    states = START + [
        room("map", 0), fight("monster", 1, 1, 80, "Nibbit"), ended("monster", 1, 1, 74), room("rewards", 1, 1, 74),
        *act_boss(1, 17, boss="Vantom"), *act_boss(2, 34), *act_boss(3, 51)[:3], game_over(51, 3, 45),
    ]
    s = score_run(states)
    assert s.outcome == "won" and s.score == 100 and s.bosses_killed == 3 and s.max_floor == 51
    assert s.character == "The Ironclad" and s.started
    first = s.combats[0]
    assert (first.room, first.enemies, first.hp_start, first.hp_end, first.hp_lost, first.result, first.steps) == (
        "monster", ["Nibbit"], 80, 74, 6, "won", 2)
    # The final boss goes straight to the game-over screen with HP left: a win.
    assert s.combats[-1].room == "boss" and s.combats[-1].result == "won" and s.combats[-1].hp_end == 45
    assert run_over(states) == "run_over:won"


def test_double_boss_needs_both_kills():
    final = [room("map", 50, 3, 60, bosses=["A", "B"]), fight("boss", 51, 3, 60, "A"), room("map", 51, 3, 50)]
    states = START + [room("map", 0), *final, fight("boss", 52, 3, 50, "B"), game_over(52, 3, 40)]
    assert score_run(states).outcome == "won"
    assert score_run(START + [room("map", 0), *final]).outcome == "unknown"


def test_death_in_act_1_after_boss():
    states = START + [
        room("map", 0), fight("monster", 1, 1, 80, "Nibbit"), room("rewards", 1, 1, 70),
        *act_boss(1, 17, hp=50, boss="Vantom"), room("map", 17, 2, 40),
        fight("elite", 18, 2, 40, "Knight", "Squire"), fight("elite", 18, 2, 0, "Knight"), game_over(18, 2, 0),
    ]
    s = score_run(states)
    assert s.outcome == "died" and s.bosses_killed == 1 and s.max_floor == 18
    assert s.score == round(18 + BOSS_POINTS, 2) == 35.33
    boss, last = s.combats[1], s.combats[2]
    assert boss.enemies == ["Vantom"] and boss.result == "won" and boss.hp_end == 40
    assert last.enemies == ["Knight", "Squire"] and last.result == "lost" and last.hp_lost == 40
    assert (last.first_step, last.last_step) == (len(states) - 2, len(states) - 1)


def test_abandoned_by_give_up_and_by_leaving():
    give_up = START + [room("map", 0), fight("monster", 1, 1, 80, "Nibbit"), menu("popup", ["confirm", "cancel"]),
                       game_over(1, 1, 75)]
    s = score_run(give_up)
    assert s.outcome == "abandoned" and s.score == 1 and s.combats[0].result == "abandoned"
    assert run_over(give_up[:-1]) is None  # a popup mid-run doesn't end it
    saved = START + [room("map", 0), room("event", 1), menu("main", ["continue", "abandon_run", "settings"])]
    assert score_run(saved).outcome == "abandoned"
    assert run_over(saved) == "run_over:abandoned"


def test_unknown_when_log_ends_mid_run_or_never_starts():
    s = score_run(START + [room("map", 0), fight("monster", 1, 1, 80, "Nibbit"), None])
    assert s.outcome == "unknown" and s.combats[0].result == "unfinished" and s.score == 1
    never = score_run(START + [None])
    assert never.outcome == "unknown" and not never.started and never.max_floor is None and never.score == 0


def test_none_states_inside_a_fight_dont_split_it():
    fights = combats([fight("monster", 1, 1, 80, "A"), None, fight("hand_select", 1, 1, 70), room("rewards", 1, 1, 70)])
    assert len(fights) == 1 and fights[0].steps == 2 and fights[0].result == "won"


def test_detector_waits_for_the_run_to_start_and_end():
    detector = RunOverDetector()
    assert [detector(s) for s in START] == [None, None, None]
    assert detector(room("map", 0)) is None
    assert detector(game_over(0, 1, 0)) == "run_over:died"


def test_at_main_menu():
    assert at_main_menu(menu())
    assert not at_main_menu(menu(options=["continue", "abandon_run", "singleplayer"]))
    assert not at_main_menu(menu("character_select")) and not at_main_menu(None) and not at_main_menu(room("map", 0))


def test_load_states(tmp_path):
    lines = [{"step": 2, "state": room("map", 0)}, {"step": 1, "state": menu()}, {"step": 3}]
    (tmp_path / "steps.jsonl").write_text("".join(json.dumps(l) + "\n" for l in lines))
    assert load_states(tmp_path) == [menu(), room("map", 0), None]
    assert load_states(tmp_path / "missing") == []
