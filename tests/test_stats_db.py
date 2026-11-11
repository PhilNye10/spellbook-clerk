import asyncio
import pytest

aiosqlite = pytest.importorskip("aiosqlite")
from mtg_table_bot.db import Database


def test_commander_and_draft_statistics_round_trip(tmp_path):
    async def scenario():
        db = Database(str(tmp_path / "stats.db"))
        await db.init()
        await db.upsert_event(
            {"id": "draft-event", "name": "Friday Draft", "start_time": "2026-11-10T18:00:00"},
            "guild-1",
        )
        assignment_id = await db.save_assignment(
            "draft-event", "commander", 123, ["u1", "u2", "u3", "u4"],
            [["u1", "u2", "u3", "u4"]],
        )
        await db.save_commander_game(
            "guild-1", "draft-event", assignment_id, 1, "u1", "admin",
            [
                ("u1", "Alice", "Krenko, Mob Boss"),
                ("u2", "Bob", "Sauron, the Dark Lord"),
                ("u3", "Carol", "Edgar Markov"),
                ("u4", "Dan", "Sauron, the Dark Lord"),
            ],
        )
        commander_stats = await db.commander_player_stats("guild-1", "u1")
        assert commander_stats[:2] == (1, 1)
        commander_rankings = await db.commander_commander_leaderboard("guild-1")
        sauron = next(row for row in commander_rankings if row[0].startswith("Sauron"))
        assert sauron[1:] == (2, 0)

        bracket = {
            "players": ["u1", "u2", "u3", "u4"],
            "champion": "u4",
            "matches": [
                {"match_id": 1, "round_number": 1, "match_number": 1, "player1": "u1", "player2": "u2", "winner": "u1", "bye": False},
                {"match_id": 2, "round_number": 1, "match_number": 2, "player1": "u3", "player2": "u4", "winner": "u4", "bye": False},
                {"match_id": 3, "round_number": 2, "match_number": 1, "player1": "u1", "player2": "u4", "winner": "u4", "bye": False},
            ],
        }
        await db.sync_draft_tournament("draft-event", "guild-1", "Friday Draft", bracket)
        leaderboard = await db.draft_leaderboard("guild-1")
        champion = next(row for row in leaderboard if row[0] == "u4")
        assert champion[2] == 1  # tournament wins
        assert champion[4:] == (2, 2, 0)  # matches played, wins, losses
        history = await db.draft_history("guild-1")
        assert history[0][4:6] == ("u4", "u1")

    asyncio.run(scenario())
