from __future__ import annotations

import json
from pathlib import Path
import aiosqlite


class Database:
    def __init__(self, path: str):
        self.path = path

    async def init(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                guild_id TEXT NOT NULL,
                name TEXT NOT NULL,
                channel_id TEXT,
                start_time TEXT,
                data_json TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS attendees (
                event_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                option TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(event_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                mode TEXT NOT NULL,
                seed INTEGER NOT NULL,
                players_json TEXT NOT NULL,
                tables_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS brackets (
                event_id TEXT PRIMARY KEY,
                guild_id TEXT NOT NULL,
                channel_id TEXT,
                message_id TEXT,
                bracket_json TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS webhook_deliveries (
                delivery_id TEXT PRIMARY KEY,
                received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS commander_games (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                assignment_id INTEGER NOT NULL,
                pod_number INTEGER NOT NULL,
                winner_user_id TEXT NOT NULL,
                recorded_by TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS commander_game_players (
                game_id INTEGER NOT NULL,
                guild_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                commander_name TEXT NOT NULL,
                is_winner INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(game_id, user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_commander_players_guild_user
                ON commander_game_players(guild_id, user_id);
            CREATE INDEX IF NOT EXISTS idx_commander_games_guild_created
                ON commander_games(guild_id, created_at);

            CREATE TABLE IF NOT EXISTS draft_tournaments (
                event_id TEXT PRIMARY KEY,
                guild_id TEXT NOT NULL,
                event_name TEXT NOT NULL,
                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TEXT,
                champion_id TEXT,
                runner_up_id TEXT,
                players_json TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS draft_tournament_players (
                event_id TEXT NOT NULL,
                guild_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                placing INTEGER,
                PRIMARY KEY(event_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS draft_matches (
                event_id TEXT NOT NULL,
                match_id INTEGER NOT NULL,
                round_number INTEGER NOT NULL,
                match_number INTEGER NOT NULL,
                player1_id TEXT,
                player2_id TEXT,
                winner_id TEXT,
                is_bye INTEGER NOT NULL DEFAULT 0,
                recorded_at TEXT,
                PRIMARY KEY(event_id, match_id)
            );
            CREATE INDEX IF NOT EXISTS idx_draft_players_guild_user
                ON draft_tournament_players(guild_id, user_id);
            CREATE INDEX IF NOT EXISTS idx_draft_matches_event
                ON draft_matches(event_id, winner_id);
            """)
            await db.commit()
            # Backfill records from brackets that were created before statistics existed.
            cur = await db.execute("""
                SELECT b.event_id, b.guild_id, b.bracket_json, b.updated_at,
                       COALESCE(e.name, 'MTG Draft'), e.start_time
                FROM brackets b LEFT JOIN events e ON e.event_id=b.event_id
            """)
            old_brackets = await cur.fetchall()

        for event_id, guild_id, bracket_json, updated_at, event_name, start_time in old_brackets:
            try:
                bracket_data = json.loads(bracket_json)
                await self.sync_draft_tournament(
                    event_id, guild_id, event_name, bracket_data,
                    started_at=start_time or updated_at,
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                # A malformed legacy bracket should not prevent the bot from starting.
                continue

    async def upsert_event(self, event: dict, guild_id: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""INSERT INTO events(event_id,guild_id,name,channel_id,start_time,data_json)
            VALUES(?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET
            guild_id=excluded.guild_id,name=excluded.name,channel_id=excluded.channel_id,
            start_time=excluded.start_time,data_json=excluded.data_json,updated_at=CURRENT_TIMESTAMP""",
            (str(event["id"]), guild_id, event.get("name","Unnamed Event"), str(event.get("channel_id")) if event.get("channel_id") else None,
             event.get("start_time"), json.dumps(event)))
            await db.commit()

    async def replace_attendees(self, event_id: str, rows: list[tuple[str,str,str]]):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM attendees WHERE event_id=?", (event_id,))
            await db.executemany("INSERT INTO attendees(event_id,user_id,display_name,option) VALUES(?,?,?,?)",
                                 [(event_id, uid, name, opt) for uid,name,opt in rows])
            await db.commit()

    async def upsert_attendee(self, event_id: str, user_id: str, display_name: str, option: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""INSERT INTO attendees(event_id,user_id,display_name,option) VALUES(?,?,?,?)
            ON CONFLICT(event_id,user_id) DO UPDATE SET display_name=excluded.display_name,
            option=excluded.option,updated_at=CURRENT_TIMESTAMP""", (event_id,user_id,display_name,option))
            await db.commit()

    async def remove_attendee(self, event_id: str, user_id: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM attendees WHERE event_id=? AND user_id=?", (event_id,user_id))
            await db.commit()

    async def get_all_rsvps(self, event_id: str):
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT user_id,display_name,option FROM attendees WHERE event_id=? ORDER BY option,display_name", (event_id,))
            return await cur.fetchall()

    async def get_attendees(self, event_id: str, attending_option: str, include_maybe: bool=False):
        allowed = [attending_option]
        if include_maybe:
            allowed.append("?")
        q = "SELECT user_id,display_name,option FROM attendees WHERE event_id=? AND option IN (%s) ORDER BY display_name" % ",".join("?" for _ in allowed)
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(q, [event_id, *allowed])
            return await cur.fetchall()

    async def get_event_name(self, event_id: str) -> str:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT name FROM events WHERE event_id=?", (event_id,))
            row = await cur.fetchone()
            return row[0] if row else event_id

    async def list_events(self, guild_id: str, limit: int = 25):
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT event_id,name,start_time,data_json FROM events WHERE guild_id=? ORDER BY start_time LIMIT ?", (guild_id,limit))
            return await cur.fetchall()

    async def save_assignment(self, event_id: str, mode: str, seed: int, players: list[str], tables: list[list[str]]):
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("INSERT INTO assignments(event_id,mode,seed,players_json,tables_json) VALUES(?,?,?,?,?)",
                                   (event_id,mode,seed,json.dumps(players),json.dumps(tables)))
            await db.commit()
            return cur.lastrowid

    async def list_recent_assignments(self, guild_id: str, mode: str, limit: int = 25):
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("""
                SELECT a.id, a.event_id, COALESCE(e.name, 'MTG Event'), a.tables_json, a.created_at
                FROM assignments a
                LEFT JOIN events e ON e.event_id=a.event_id
                JOIN (SELECT event_id, MAX(id) AS latest_id FROM assignments WHERE mode=? GROUP BY event_id) latest
                  ON latest.latest_id=a.id
                WHERE (?='' OR e.guild_id=? OR e.guild_id IS NULL)
                ORDER BY a.id DESC LIMIT ?
            """, (mode, guild_id, guild_id, limit))
            return await cur.fetchall()

    async def get_assignment(self, assignment_id: int):
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT event_id,mode,tables_json FROM assignments WHERE id=?", (assignment_id,))
            return await cur.fetchone()

    async def save_commander_game(
        self, guild_id: str, event_id: str, assignment_id: int, pod_number: int,
        winner_user_id: str, recorded_by: str,
        players: list[tuple[str, str, str]],
    ) -> int:
        if not players or winner_user_id not in {p[0] for p in players}:
            raise ValueError("The winner must be one of the players in the selected pod.")
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("""INSERT INTO commander_games
                (guild_id,event_id,assignment_id,pod_number,winner_user_id,recorded_by)
                VALUES(?,?,?,?,?,?)""",
                (guild_id, event_id, assignment_id, pod_number, winner_user_id, recorded_by))
            game_id = cur.lastrowid
            await db.executemany("""INSERT INTO commander_game_players
                (game_id,guild_id,user_id,display_name,commander_name,is_winner)
                VALUES(?,?,?,?,?,?)""",
                [(game_id, guild_id, uid, name, commander, int(uid == winner_user_id))
                 for uid, name, commander in players])
            await db.commit()
            return game_id

    async def delete_commander_game(self, guild_id: str, game_id: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("DELETE FROM commander_games WHERE guild_id=? AND id=?", (guild_id, game_id))
            await db.execute("DELETE FROM commander_game_players WHERE game_id=?", (game_id,))
            await db.commit()
            return cur.rowcount > 0

    async def commander_leaderboard(self, guild_id: str):
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("""
                SELECT user_id, MAX(display_name), COUNT(*) AS games,
                       SUM(is_winner) AS wins
                FROM commander_game_players WHERE guild_id=?
                GROUP BY user_id ORDER BY wins DESC, (1.0*wins/COUNT(*)) DESC, games DESC
            """, (guild_id,))
            return await cur.fetchall()

    async def commander_player_stats(self, guild_id: str, user_id: str):
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("""
                SELECT COUNT(*), COALESCE(SUM(is_winner),0), MAX(display_name)
                FROM commander_game_players WHERE guild_id=? AND user_id=?
            """, (guild_id, user_id))
            return await cur.fetchone()

    async def commander_commander_leaderboard(self, guild_id: str):
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("""
                SELECT MIN(TRIM(commander_name)) AS commander, COUNT(*) AS games,
                       SUM(is_winner) AS wins
                FROM commander_game_players
                WHERE guild_id=? AND TRIM(commander_name)<>''
                GROUP BY LOWER(TRIM(commander_name))
                ORDER BY wins DESC, (1.0*wins/COUNT(*)) DESC, games DESC
            """, (guild_id,))
            return await cur.fetchall()

    async def commander_player_commanders(self, guild_id: str, user_id: str):
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("""
                SELECT MIN(TRIM(commander_name)), COUNT(*), SUM(is_winner)
                FROM commander_game_players
                WHERE guild_id=? AND user_id=? AND TRIM(commander_name)<>''
                GROUP BY LOWER(TRIM(commander_name))
                ORDER BY SUM(is_winner) DESC, COUNT(*) DESC
            """, (guild_id, user_id))
            return await cur.fetchall()

    async def commander_history(self, guild_id: str, limit: int = 10, user_id: str | None = None):
        async with aiosqlite.connect(self.path) as db:
            if user_id:
                cur = await db.execute("""
                    SELECT DISTINCT g.id,g.event_id,g.pod_number,g.winner_user_id,g.created_at
                    FROM commander_games g JOIN commander_game_players p ON p.game_id=g.id
                    WHERE g.guild_id=? AND p.user_id=? ORDER BY g.id DESC LIMIT ?
                """, (guild_id, user_id, limit))
            else:
                cur = await db.execute("""
                    SELECT id,event_id,pod_number,winner_user_id,created_at
                    FROM commander_games WHERE guild_id=? ORDER BY id DESC LIMIT ?
                """, (guild_id, limit))
            games = await cur.fetchall()
            result = []
            for game_id, event_id, pod_number, winner, created_at in games:
                cur2 = await db.execute("""
                    SELECT user_id,display_name,commander_name,is_winner
                    FROM commander_game_players WHERE game_id=? ORDER BY display_name
                """, (game_id,))
                players = await cur2.fetchall()
                result.append({"id": game_id, "event_id": event_id, "pod_number": pod_number,
                               "winner_id": winner, "created_at": created_at, "players": players})
            return result

    async def save_bracket(self, event_id: str, guild_id: str, channel_id: int | None, message_id: int | None, bracket: dict):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""INSERT INTO brackets(event_id,guild_id,channel_id,message_id,bracket_json)
            VALUES(?,?,?,?,?)
            ON CONFLICT(event_id) DO UPDATE SET
              guild_id=excluded.guild_id, channel_id=excluded.channel_id,
              message_id=excluded.message_id, bracket_json=excluded.bracket_json,
              updated_at=CURRENT_TIMESTAMP""",
            (event_id, guild_id, str(channel_id) if channel_id else None, str(message_id) if message_id else None, json.dumps(bracket)))
            await db.commit()

    async def sync_draft_tournament(self, event_id: str, guild_id: str, event_name: str,
                                    bracket: dict, started_at: str | None = None):
        players = [str(x) for x in bracket.get("players", [])]
        matches = bracket.get("matches", [])
        champion = str(bracket["champion"]) if bracket.get("champion") else None
        final_match = max(matches, key=lambda m: int(m.get("round_number", 0)), default=None)
        runner_up = None
        if champion and final_match:
            p1, p2 = final_match.get("player1"), final_match.get("player2")
            if p1 and p2:
                runner_up = str(p2 if str(p1) == champion else p1)
        async with aiosqlite.connect(self.path) as db:
            # Prefer the event's scheduled start time for existing brackets; otherwise preserve
            # the first-seen timestamp so year-based leaderboards remain stable.
            if started_at is None:
                cur = await db.execute("SELECT start_time FROM events WHERE event_id=?", (event_id,))
                row = await cur.fetchone()
                started_at = row[0] if row and row[0] else None
            await db.execute("""
                INSERT INTO draft_tournaments
                    (event_id,guild_id,event_name,started_at,completed_at,champion_id,runner_up_id,players_json)
                VALUES(?,?,?,COALESCE(?,CURRENT_TIMESTAMP),CASE WHEN ? IS NULL THEN NULL ELSE CURRENT_TIMESTAMP END,?,?,?)
                ON CONFLICT(event_id) DO UPDATE SET
                    guild_id=excluded.guild_id,event_name=excluded.event_name,
                    completed_at=COALESCE(draft_tournaments.completed_at,excluded.completed_at),
                    champion_id=excluded.champion_id,runner_up_id=excluded.runner_up_id,
                    players_json=excluded.players_json
            """, (event_id, guild_id, event_name, started_at, champion, champion, runner_up, json.dumps(players)))
            await db.execute("DELETE FROM draft_tournament_players WHERE event_id=?", (event_id,))
            await db.executemany("""INSERT INTO draft_tournament_players(event_id,guild_id,user_id,placing)
                VALUES(?,?,?,?)""",
                [(event_id, guild_id, uid, 1 if uid == champion else (2 if uid == runner_up else None)) for uid in players])
            await db.execute("DELETE FROM draft_matches WHERE event_id=?", (event_id,))
            await db.executemany("""INSERT INTO draft_matches
                (event_id,match_id,round_number,match_number,player1_id,player2_id,winner_id,is_bye)
                VALUES(?,?,?,?,?,?,?,?)""",
                [(event_id, int(m["match_id"]), int(m.get("round_number", 0)), int(m.get("match_number", 0)),
                  str(m["player1"]) if m.get("player1") else None,
                  str(m["player2"]) if m.get("player2") else None,
                  str(m["winner"]) if m.get("winner") else None,
                  int(bool(m.get("bye")))) for m in matches])
            await db.commit()

    async def get_bracket(self, event_id: str):
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT guild_id,channel_id,message_id,bracket_json FROM brackets WHERE event_id=?", (event_id,))
            row = await cur.fetchone()
            if not row:
                return None
            return {"guild_id": row[0], "channel_id": row[1], "message_id": row[2], "bracket": json.loads(row[3])}

    async def list_active_brackets(self):
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT event_id,guild_id,channel_id,message_id,bracket_json FROM brackets WHERE message_id IS NOT NULL")
            return await cur.fetchall()

    async def get_last_assignment(self, event_id: str, mode: str):
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT id,seed,players_json,tables_json FROM assignments WHERE event_id=? AND mode=? ORDER BY id DESC LIMIT 1", (event_id,mode))
            return await cur.fetchone()

    async def draft_leaderboard(self, guild_id: str, year: int | None = None):
        year_filter = str(year) if year else None
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("""
                WITH match_players AS (
                    SELECT event_id, player1_id AS user_id,
                           CASE WHEN winner_id=player1_id THEN 1 ELSE 0 END AS won
                    FROM draft_matches WHERE winner_id IS NOT NULL AND is_bye=0 AND player1_id IS NOT NULL
                    UNION ALL
                    SELECT event_id, player2_id AS user_id,
                           CASE WHEN winner_id=player2_id THEN 1 ELSE 0 END AS won
                    FROM draft_matches WHERE winner_id IS NOT NULL AND is_bye=0 AND player2_id IS NOT NULL
                ), match_totals AS (
                    SELECT event_id,user_id,COUNT(*) AS played,SUM(won) AS wins
                    FROM match_players GROUP BY event_id,user_id
                )
                SELECT p.user_id,COUNT(*) AS appearances,
                       SUM(CASE WHEN p.placing=1 THEN 1 ELSE 0 END) AS tournament_wins,
                       SUM(CASE WHEN p.placing=2 THEN 1 ELSE 0 END) AS runner_ups,
                       COALESCE(SUM(m.played),0) AS matches_played,
                       COALESCE(SUM(m.wins),0) AS match_wins
                FROM draft_tournament_players p
                JOIN draft_tournaments t ON t.event_id=p.event_id
                LEFT JOIN match_totals m ON m.event_id=p.event_id AND m.user_id=p.user_id
                WHERE t.guild_id=? AND (? IS NULL OR substr(t.started_at,1,4)=?)
                GROUP BY p.user_id
                ORDER BY tournament_wins DESC, match_wins DESC,
                         (1.0*match_wins/NULLIF(matches_played,0)) DESC, appearances DESC
            """, (guild_id, year_filter, year_filter))
            rows = await cur.fetchall()
            return [(r[0], r[1], r[2], r[3], r[4], r[5], r[4]-r[5]) for r in rows]

    async def draft_player_stats(self, guild_id: str, user_id: str, year: int | None = None):
        rows = await self.draft_leaderboard(guild_id, year)
        return next((r for r in rows if r[0] == user_id), None)

    async def draft_history(self, guild_id: str, limit: int = 10, year: int | None = None):
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("""
                SELECT event_id,event_name,started_at,completed_at,champion_id,runner_up_id,players_json
                FROM draft_tournaments
                WHERE guild_id=? AND (? IS NULL OR substr(started_at,1,4)=?)
                ORDER BY started_at DESC LIMIT ?
            """, (guild_id, str(year) if year else None, str(year) if year else None, limit))
            return await cur.fetchall()

    async def mark_delivery_once(self, delivery_id: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            try:
                await db.execute("INSERT INTO webhook_deliveries(delivery_id) VALUES(?)", (delivery_id,))
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                return False
