from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import aiohttp

DISCORD_API = "https://discord.com/api/v10"


class DiscordEventError(RuntimeError):
    pass


def _display_name(user_obj: dict[str, Any]) -> str:
    member = user_obj.get("member") or {}
    return (
        member.get("nick")
        or user_obj.get("global_name")
        or user_obj.get("username")
        or str(user_obj.get("id", "Unknown"))
    )


class DiscordEventClient:
    """Read Discord Scheduled Events and their subscribers using the bot token."""

    def __init__(self, token: str):
        self.token = token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bot {self.token}", "User-Agent": "MTG-Table-Bot/1.0"}

    async def _request(self, method: str, path: str, **kwargs):
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout, headers=self._headers()) as session:
            async with session.request(method, f"{DISCORD_API}{path}", **kwargs) as response:
                text = await response.text()
                if response.status >= 400:
                    raise DiscordEventError(f"Discord API {response.status}: {text[:300]}")
                return await response.json()

    async def list_events(self, guild_id: int) -> list[dict[str, Any]]:
        data = await self._request("GET", f"/guilds/{guild_id}/scheduled-events", params={"with_user_count": "true"})
        now = datetime.now(timezone.utc)
        out = []
        for event in data:
            try:
                start = datetime.fromisoformat(event["scheduled_start_time"].replace("Z", "+00:00"))
            except Exception:
                start = None
            # Keep scheduled/active events and a small grace window for events in progress.
            if event.get("status") in (1, 2) or (start and start >= now):
                out.append(event)
        out.sort(key=lambda e: e.get("scheduled_start_time") or "")
        return out

    async def get_event(self, guild_id: int, event_id: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/guilds/{guild_id}/scheduled-events/{event_id}",
            params={"with_user_count": "true"},
        )

    async def get_attendees(self, guild_id: int, event_id: str) -> list[tuple[str, str, str]]:
        users: list[tuple[str, str, str]] = []
        after: str | None = None
        while True:
            params: dict[str, str] = {"limit": "100", "with_member": "true"}
            if after:
                params["after"] = after
            page = await self._request(
                "GET",
                f"/guilds/{guild_id}/scheduled-events/{event_id}/users",
                params=params,
            )
            if not page:
                break
            for item in page:
                user = item.get("user") or {}
                uid = str(user.get("id", ""))
                if uid:
                    users.append((uid, _display_name(user), "✅"))
            if len(page) < 100:
                break
            after = str((page[-1].get("user") or {}).get("id") or "")
            if not after:
                break
        return users

    @staticmethod
    def to_event_record(event: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(event["id"]),
            "name": event.get("name", "Discord Event"),
            "channel_id": str(event.get("channel_id")) if event.get("channel_id") else None,
            "start_time": event.get("scheduled_start_time"),
            "source": "discord",
            "description": event.get("description"),
            "user_count": event.get("user_count"),
            "status": event.get("status"),
        }
