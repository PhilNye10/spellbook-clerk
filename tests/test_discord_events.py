from mtg_table_bot.discord_events import DiscordEventClient


def test_event_record_maps_discord_event():
    raw = {
        "id": "123",
        "name": "Wednesday Draft",
        "channel_id": "456",
        "scheduled_start_time": "2026-08-19T23:00:00.000Z",
        "description": "MTG",
        "user_count": 8,
        "status": 1,
    }
    record = DiscordEventClient.to_event_record(raw)
    assert record["id"] == "123"
    assert record["name"] == "Wednesday Draft"
    assert record["source"] == "discord"
    assert record["user_count"] == 8


def test_display_name_prefers_nick_then_global_name():
    from mtg_table_bot.discord_events import _display_name
    assert _display_name({"id": "1", "global_name": "Phil", "username": "phil"}) == "Phil"
    assert _display_name({"id": "1", "member": {"nick": "Captain Phil"}, "global_name": "Phil"}) == "Captain Phil"
