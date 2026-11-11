from __future__ import annotations
import re
import discord

ID_RE = re.compile(r"<@!?(\d{15,25})>")
# Sesh's embed uses these headings, including an optional emoji immediately before them.
SECTION_RE = re.compile(r"(?:[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]*\s*)?\b(Attendees|Maybe|No)\s*\(\s*(\d+)\s*\)", re.IGNORECASE)


def _message_text(message: discord.Message) -> str:
    """Flatten the visible text of a Discord message/embed in display order."""
    chunks: list[str] = [message.content or ""]
    for embed in message.embeds:
        if embed.title:
            chunks.append(embed.title)
        if embed.description:
            chunks.append(embed.description)
        for field in embed.fields:
            chunks.append(field.name or "")
            chunks.append(field.value or "")
        if embed.footer and embed.footer.text:
            chunks.append(embed.footer.text)
    return "\n".join(chunks)


def parse_sesh_rsvps(message: discord.Message) -> dict[str, list[str]]:
    """Parse Sesh's Attendees/Maybe/No blocks without leaking mentions across sections.

    The parser deliberately treats each section as a bounded block: mentions after
    ``Maybe (N)`` cannot be counted as Going, and mentions after ``No (N)`` cannot
    be counted as Going. This also works when Sesh splits the embed across fields.
    """
    result: dict[str, list[str]] = {"going": [], "maybe": [], "no": []}
    section_map = {"attendees": "going", "maybe": "maybe", "no": "no"}
    text = _message_text(message)
    matches = list(SECTION_RE.finditer(text))

    for index, match in enumerate(matches):
        key = section_map[match.group(1).lower()]
        block_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[match.end():block_end]
        result[key].extend(ID_RE.findall(block))

    # De-duplicate while preserving Sesh's displayed order. If malformed content
    # puts a user in multiple sections, the first section encountered wins.
    seen: set[str] = set()
    for key in ("going", "maybe", "no"):
        clean: list[str] = []
        for uid in result[key]:
            if uid not in seen:
                clean.append(uid)
                seen.add(uid)
        result[key] = clean
    return result


async def parse_sesh_message(message: discord.Message, attending_markers: tuple[str, ...] = ("✅", "Going", "Attending")) -> list[str]:
    """Backward-compatible helper returning only the Going/Attendees IDs."""
    return parse_sesh_rsvps(message)["going"]


def is_sesh_event_message(message: discord.Message) -> bool:
    """Return True when a message looks like a Sesh RSVP event message."""
    text = _message_text(message)
    headings = {m.group(1).lower() for m in SECTION_RE.finditer(text)}
    return "attendees" in headings and "maybe" in headings and "no" in headings


def sesh_event_title(message: discord.Message) -> str:
    """Return the most useful human-readable title for a Sesh event message."""
    for embed in message.embeds:
        if embed.title:
            return embed.title.strip()
    return "Sesh Event"
