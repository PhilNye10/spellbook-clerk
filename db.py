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
            """)
            await db.commit()

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

    async def mark_delivery_once(self, delivery_id: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            try:
                await db.execute("INSERT INTO webhook_deliveries(delivery_id) VALUES(?)", (delivery_id,))
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                return False
