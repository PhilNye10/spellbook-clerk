from mtg_table_bot.bracket import build_single_elimination, Bracket


def test_eight_player_bracket():
    b = build_single_elimination([str(i) for i in range(8)], seed=123)
    assert len(b.players) == 8
    assert len([m for m in b.matches if m.round_number == 1]) == 4
    assert len([m for m in b.matches if m.round_number == 2]) == 2
    assert len([m for m in b.matches if m.round_number == 3]) == 1
    assert all(not m.bye for m in b.matches if m.round_number == 1)


def test_six_player_bracket_has_two_byes_and_real_players():
    b = build_single_elimination([str(i) for i in range(6)], seed=123)
    first_round = [m for m in b.matches if m.round_number == 1]
    assert len(first_round) == 4
    assert sum(m.bye for m in first_round) == 2
    assert all((m.player1 or m.player2) for m in first_round if m.bye)


def test_winner_advances_and_champion_is_declared():
    b = build_single_elimination([str(i) for i in range(4)], seed=123)
    first = b.active_matches()[0]
    first_winner = first.player1
    b.record_winner(first.match_id, first_winner)
    assert first.winner == first_winner
    nxt = b.get_match(first.next_match_id)
    assert first_winner in {nxt.player1, nxt.player2}

    for m in list(b.active_matches()):
        if m.winner is None:
            b.record_winner(m.match_id, m.player1)
    for m in list(b.active_matches()):
        if m.winner is None and m.player1 and m.player2:
            b.record_winner(m.match_id, m.player1)
    assert b.champion is not None


def test_recording_completed_match_fails():
    b = build_single_elimination([str(i) for i in range(2)], seed=1)
    m = b.active_matches()[0]
    b.record_winner(m.match_id, m.player1)
    assert b.champion == m.player1
    try:
        b.record_winner(m.match_id, m.player2)
    except ValueError as exc:
        assert "already" in str(exc)
    else:
        raise AssertionError("Expected completed-match error")
