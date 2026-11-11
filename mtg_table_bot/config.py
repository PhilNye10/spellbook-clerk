from __future__ import annotations
from dataclasses import dataclass
import os
import yaml

@dataclass(frozen=True)
class ModeConfig:
    preferred_size: int
    minimum_size: int
    maximum_size: int

@dataclass(frozen=True)
class AppConfig:
    draft: ModeConfig
    commander: ModeConfig
    attending_option: str = "✅"
    include_maybe: bool = False
    admin_only_commands: bool = True
    schedule_channel_id: int | None = None
    schedule_scan_limit: int = 100
    feed_channel_id: int | None = None  # Legacy shared channel setting
    news_channel_id: int | None = None
    deals_channel_id: int | None = None
    feed_poll_minutes: int = 10
    deals_auto_delete_days: int = 3
    deals_cleanup_poll_minutes: int = 60


def load_config(path: str = "config.yaml") -> AppConfig:
    raw = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    def mode(name: str, defaults: tuple[int, int, int]) -> ModeConfig:
        x = raw.get(name, {})
        return ModeConfig(
            int(x.get("preferred_size", defaults[0])),
            int(x.get("minimum_size", defaults[1])),
            int(x.get("maximum_size", defaults[2])),
        )
    rsvp = raw.get("rsvp", {})
    perms = raw.get("permissions", {})
    schedule = raw.get("schedule", {})
    feeds = raw.get("feeds", {})
    channel_id = schedule.get("channel_id")
    try:
        channel_id = int(channel_id) if channel_id else None
    except (TypeError, ValueError):
        channel_id = None
    def parse_channel_id(value):
        try:
            return int(value) if value else None
        except (TypeError, ValueError):
            return None
    # New per-feed destinations take precedence; channel_id remains supported for older configs.
    feed_channel_id = parse_channel_id(feeds.get("channel_id"))
    news_channel_id = parse_channel_id(feeds.get("news_channel_id")) or feed_channel_id
    deals_channel_id = parse_channel_id(feeds.get("deals_channel_id")) or feed_channel_id
    return AppConfig(
        draft=mode("draft", (4, 3, 5)),
        commander=mode("commander", (4, 3, 5)),
        attending_option=str(rsvp.get("attending_option", "✅")),
        include_maybe=bool(rsvp.get("include_maybe", False)),
        admin_only_commands=bool(perms.get("admin_only_commands", True)),
        schedule_channel_id=channel_id,
        schedule_scan_limit=max(1, min(int(schedule.get("scan_limit", 100)), 100)),
        feed_channel_id=feed_channel_id,
        news_channel_id=news_channel_id,
        deals_channel_id=deals_channel_id,
        feed_poll_minutes=max(2, min(int(feeds.get("poll_minutes", 10)), 1440)),
        deals_auto_delete_days=max(1, min(int(feeds.get("auto_delete_days", 3)), 365)),
        deals_cleanup_poll_minutes=max(5, min(int(feeds.get("cleanup_poll_minutes", 60)), 1440)),
    )
