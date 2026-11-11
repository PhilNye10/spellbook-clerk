from mtg_table_bot.assignments import choose_sizes, make_assignment

def test_common_sizes():
    assert choose_sizes(8, 4, 3, 5) == [4, 4]
    assert choose_sizes(11, 4, 3, 5) == [4, 4, 3]
    assert choose_sizes(12, 4, 3, 5) == [4, 4, 4]

def test_all_players_preserved():
    players = [str(i) for i in range(13)]
    a = make_assignment(players, "commander", 4, 3, 5, seed=123)
    assert sorted(sum((list(t.players) for t in a.tables), [])) == sorted(players)
