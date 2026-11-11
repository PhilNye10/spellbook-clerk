from __future__ import annotations
from datetime import datetime, timezone
import aiohttp

BASE = "https://sesh.fyi/api/v1"

class SeshError(RuntimeError):
    pass

class SeshClient:
    def __init__(self, token: str):
        self.token = token
        self.session: aiohttp.ClientSession | None = None

    async def start(self):
        if self.session is None:
            self.session = aiohttp.ClientSession(headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"})

    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None

    async def _get(self, path: str, params: dict | None = None):
        await self.start()
        assert self.session
        async with self.session.get(BASE + path, params=params) as r:
            text = await r.text()
            if r.status >= 400:
                raise SeshError(f"Sesh API {r.status}: {text[:500]}")
            try:
                return await r.json()
            except Exception as e:
                raise SeshError(f"Sesh returned invalid JSON: {text[:500]}") from e

    @staticmethod
    def _items(payload):
        if isinstance(payload, list): return payload
        if isinstance(payload, dict):
            for key in ("data", "events", "rsvps", "items", "results"):
                if isinstance(payload.get(key), list): return payload[key]
        return []

    async def list_upcoming_events(self, limit: int = 25):
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        payload = await self._get("/events", {"start_after": now})
        return self._items(payload)[:limit]

    async def get_event(self, event_id: str):
        return await self._get(f"/events/{event_id}")

    async def get_rsvps(self, event_id: str, option: str | None = None):
        all_rows = []
        offset = 0
        while True:
            params = {"limit": 100, "offset": offset}
            if option: params["option"] = option
            payload = await self._get(f"/events/{event_id}/rsvps", params)
            batch = self._items(payload)
            all_rows.extend(batch)
            if len(batch) < 100: break
            offset += 100
        return all_rows

    async def sync_event(self, event_id: str, attending_option: str = "✅", include_maybe: bool = False):
        event = await self.get_event(event_id)
        rows = await self.get_rsvps(event_id)
        out = []
        for row in rows:
            option = row.get("option") or row.get("emoji") or row.get("rsvp_option") or ""
            if isinstance(option, dict): option = option.get("emoji") or option.get("name") or ""
            name = row.get("display_name") or row.get("username") or row.get("name") or row.get("user", {}).get("username") or row.get("user", {}).get("global_name") or str(row.get("user_id", "Unknown"))
            uid = str(row.get("user_id") or row.get("discord_user_id") or row.get("user", {}).get("id") or row.get("id", ""))
            if not uid: continue
            if option == attending_option or (include_maybe and str(option).lower() in {"?", "maybe"}):
                out.append((uid, name, str(option)))
        return event, out
