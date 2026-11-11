from __future__ import annotations
import re
import discord

ID_RE = re.compile(r"<@!?(\d{15,25})>")
SECTION_RE = re.compile(r"\b(Attendees|Maybe|No)\s*\(\d+\)", re.IGNORECASE)


def _message_chunks(message: discord.Message) -> list[str]:
    chunks: list[str] = [message.content or ""]
    for embed in message.embeds:
        if embed.title:
            chunks.append(embed.title)
        if embed.description:
            chunks.append(embed.description)
        for field in embed.fields:
            chunks.extend([field.name or "", field.value or ""])
        if embed.footer and embed.footer.text:
            chunks.append(embed.footer.text)
    return chunks


def parse_sesh_rsvps(message: discord.Message) -> dict[str, list[str]]:
    """Parse Sesh's visible RSVP sections from a Discord message.

    Sesh renders three sections in its event embed: Attendees, Maybe, and No.
    Each section contains Discord mention tokens, which gives us stable user IDs
    without needing the Sesh Pro API.
    """
    result: dict[str, list[str]] = {"going": [], "maybe": [], "no": []}
    current: str | None = None
    section_map = {"attendees": "going", "maybe": "maybe", "no": "no"}

    for chunk in _message_chunks(message):
        for line in chunk.splitlines():
            heading = SECTION_RE.search(line)
            if heading:
                current = section_map[heading.group(1).lower()]
                tail = line[heading.end():]
                result[current].extend(ID_RE.findall(tail))
                continue
            if current:
                result[current].extend(ID_RE.findall(line))

    # De-duplicate while preserving Sesh's displayed order. If a malformed
    # message puts a user in multiple sections, prefer the first section seen.
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
