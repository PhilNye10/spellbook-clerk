from __future__ import annotations
import json
import logging
import os
import discord
from discord import app_commands
from discord.ext import commands

from .assignments import make_assignment
from .bracket import Bracket, build_single_elimination
from .config import AppConfig
from .db import Database
from .fallback import parse_sesh_message, parse_sesh_rsvps
from .sesh import SeshClient, SeshError

log = logging.getLogger("mtg-table-bot")

class TableBot(commands.Bot):
    def __init__(self, cfg: AppConfig, db: Database, sesh: SeshClient | None):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.messages = True
        super().__init__(command_prefix="!", intents=intents)
        self.cfg, self.db, self.sesh = cfg, db, sesh
        self.logger = log

    async def setup_hook(self):
        await self.db.init()
        guild_id = os.getenv("DISCORD_GUILD_ID", "").strip()
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            self.logger.info("Slash commands synced to guild %s", guild_id)
        else:
            await self.tree.sync()
            self.logger.info("Global slash commands synced")

        # Re-register persistent RSVP roster controls.
        for event_id, _guild_id, _channel_id, _start_time, event_json in await self.db.list_events(str(guild_id)) if guild_id else []:
            try:
                event = json.loads(event_json)
                roster_message_id = event.get("roster_message_id")
                if roster_message_id:
                    self.add_view(RosterView(self, event_id), message_id=int(roster_message_id))
            except Exception:
                self.logger.exception("Failed to restore RSVP roster view for event %s", event_id)

        # Re-register controls for brackets that were saved before a restart.
        for event_id, guild_id, channel_id, message_id, bracket_json in await self.db.list_active_brackets():
            try:
                bracket = Bracket.from_dict(json.loads(bracket_json))
                if bracket.champion or not message_id:
                    continue
                self.add_view(BracketView(self, event_id), message_id=int(message_id))
            except Exception:
                self.logger.exception("Failed to restore bracket view for event %s", event_id)

    async def on_ready(self):
        self.logger.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "?")

    async def handle_sesh_webhook(self, payload: dict):
        typ = payload.get("type", "")
        data = payload.get("data") or {}
        guild_id = str(payload.get("guild_id") or data.get("guild_id") or "")
        event_id = str(data.get("id") or "")
        if not event_id:
            return
        await self.db.upsert_event(data, guild_id)
        if typ in {"event.rsvp.added", "event.rsvp.removed"}:
            if self.sesh:
                try:
                    _, attendees = await self.sesh.sync_event(event_id, self.cfg.attending_option, self.cfg.include_maybe)
                    await self.db.replace_attendees(event_id, attendees)
                    return
                except Exception as exc:
                    self.logger.warning("Webhook RSVP refresh failed: %s", exc)
            user = data.get("user") or {}
            uid = str(data.get("user_id") or data.get("discord_user_id") or user.get("id") or "")
            if typ == "event.rsvp.removed":
                if uid:
                    await self.db.remove_attendee(event_id, uid)
            elif uid:
                option = str(data.get("option") or data.get("emoji") or data.get("rsvp_option") or "")
                name = data.get("display_name") or data.get("username") or user.get("global_name") or user.get("username") or uid
                await self.db.upsert_attendee(event_id, uid, name, option)

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        if not self.cfg.admin_only_commands:
            return True
        perms = getattr(interaction.user, "guild_permissions", None)
        return bool(perms and (perms.manage_guild or perms.administrator))

    async def _member_display_name(self, guild: discord.Guild, user_id: str) -> str:
        member = guild.get_member(int(user_id))
        if member is not None:
            return member.display_name
        try:
            member = await guild.fetch_member(int(user_id))
            return member.display_name
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
            try:
                user = await self.fetch_user(int(user_id))
                return user.global_name or user.name
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
                return f"User {user_id}"

    async def import_sesh_message(self, guild: discord.Guild, channel: discord.TextChannel, message: discord.Message):
        rsvps = parse_sesh_rsvps(message)
        if not any(rsvps.values()):
            raise RuntimeError("I couldn't find the Sesh Attendees/Maybe/No sections or any Discord mentions in the event message.")

        event_id = f"sesh-{message.id}"
        title = next((e.title for e in message.embeds if e.title), "Sesh Event")
        event = {
            "id": event_id,
            "name": title,
            "channel_id": str(channel.id),
            "source_message_id": str(message.id),
            "start_time": None,
            "source": "sesh-message",
        }
        await self.db.upsert_event(event, str(guild.id))

        rows = []
        for option, key in (("going", "\u2705"), ("maybe", "?"), ("no", "❌")):
            for uid in rsvps[option]:
                rows.append((uid, await self._member_display_name(guild, uid), key))
        await self.db.replace_attendees(event_id, rows)
        return event, rsvps, rows

    def roster_embed(self, event: dict, rows: list[tuple[str, str, str]]) -> discord.Embed:
        going = [(uid, name) for uid, name, opt in rows if opt == "\u2705"]
        maybe = [(uid, name) for uid, name, opt in rows if opt == "?"]
        no = [(uid, name) for uid, name, opt in rows if opt == "❌"]
        embed = discord.Embed(
            title=f"🃏 {event.get('name', 'MTG Event')} — RSVP Roster",
            description=f"**{len(going)} Going** • **{len(maybe)} Maybe** • **{len(no)} Not Going**\n\nOnly **Going** players are included when generating a bracket or pods.",
        )
        def names(items):
            if not items:
                return "—"
            text = "\n".join(f"• <@{uid}>" for uid, _ in items)
            return text[:1024]
        embed.add_field(name=f"✅ Going ({len(going)})", value=names(going), inline=False)
        embed.add_field(name=f"🧑 Maybe ({len(maybe)})", value=names(maybe), inline=False)
        embed.add_field(name=f"❌ No ({len(no)})", value=names(no), inline=False)
        embed.set_footer(text=f"Source: Sesh message • Event ID: {event.get('id')}")
        return embed

    async def refresh_sesh_roster(self, interaction: discord.Interaction, event_id: str):
        data_rows = await self.db.list_events(str(interaction.guild_id))
        match = next((r for r in data_rows if r[0] == event_id), None)
        if not match:
            raise RuntimeError("Roster event not found. Run `/tables fallback` again.")
        event = json.loads(match[3])
        channel_id = int(event["channel_id"])
        message_id = int(event["source_message_id"])
        channel = interaction.guild.get_channel(channel_id) or await interaction.guild.fetch_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError("The saved Sesh channel is no longer accessible.")
        source_message = await channel.fetch_message(message_id)
        event, rsvps, rows = await self.import_sesh_message(interaction.guild, channel, source_message)
        return event, rows

    async def sync_event(self, guild_id: int, event_id: str):
        if not self.sesh:
            raise SeshError("Sesh API is not configured")
        event, attendees = await self.sesh.sync_event(event_id, self.cfg.attending_option, self.cfg.include_maybe)
        await self.db.upsert_event(event, str(guild_id))
        await self.db.replace_attendees(event_id, attendees)
        return event, attendees

    async def get_event_and_attendees(self, interaction: discord.Interaction, event_id: str):
        """Resolve roster from the local/manual Sesh-message cache first.

        Sesh Pro API remains an optional secondary source when no local roster
        has been imported for the requested event.
        """
        guild_id = int(interaction.guild_id)
        rows = await self.db.get_attendees(event_id, self.cfg.attending_option, self.cfg.include_maybe)
        attendees = [(uid, name, opt) for uid, name, opt in rows]

        events = await self.db.list_events(str(guild_id))
        match = next((r for r in events if r[0] == event_id), None)
        event = json.loads(match[3]) if match else None
        if event is not None and attendees:
            return event, attendees

        # Optional secondary source: Sesh Pro API. This is never required for
        # the normal/free workflow.
        if self.sesh and not event_id.startswith(("discord-", "sesh-")):
            try:
                event, attendees = await self.sync_event(guild_id, event_id)
                if attendees:
                    return event, attendees
            except Exception as exc:
                self.logger.info("Optional Sesh API lookup failed for %s: %s", event_id, exc)

        if event is None:
            event = {"id": event_id, "name": "MTG Event", "source": "manual"}
        return event, attendees

    async def generate(self, interaction: discord.Interaction, mode: str, event_id: str, reroll: bool=False):
        await interaction.response.defer(ephemeral=False)
        if not self._is_admin(interaction):
            return await interaction.followup.send("❌ You need Manage Server or Administrator to manage assignments.", ephemeral=True)
        try:
            event, attendees = await self.get_event_and_attendees(interaction, event_id)
            if not attendees:
                raise RuntimeError("No attending players were found. Check the Sesh RSVP option or use the fallback command.")
            players = [uid for uid, _, _ in attendees]

            if mode == "draft":
                bracket = build_single_elimination(players)
                msg = await interaction.followup.send(
                    embed=self.bracket_embed(event, bracket),
                    view=BracketView(self, event_id),
                    wait=True,
                )
                await self.db.save_bracket(event_id, str(interaction.guild_id), interaction.channel_id, msg.id, bracket.to_dict())
                return

            cfg = self.cfg.commander
            assignment = make_assignment(players, mode, cfg.preferred_size, cfg.minimum_size, cfg.maximum_size, seed=None)
            await self.db.save_assignment(event_id, mode, assignment.seed, list(assignment.players), [list(t.players) for t in assignment.tables])
            embed = self.assignment_embed(event, mode, assignment.tables, len(attendees), assignment.seed)
            await interaction.followup.send(embed=embed, view=AssignmentView(self, event_id, mode))
        except SeshError as exc:
            await interaction.followup.send(f"⚠️ Sesh API unavailable: `{exc}`\nUse `/tables fallback` to import the Sesh event message roster.", ephemeral=True)
        except Exception as exc:
            self.logger.exception("Generate failed")
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)

    def bracket_embed(self, event: dict, bracket: Bracket) -> discord.Embed:
        if bracket.champion:
            embed = discord.Embed(
                title="🏆 MTG Draft — Champion",
                description=f"**{event.get('name','MTG Event')}**\n\n🏆 <@{bracket.champion}> wins the draft!",
            )
            embed.set_footer(text=f"Bracket {bracket.seed}")
            return embed

        embed = discord.Embed(
            title="🃏 MTG Draft — 1v1 Single-Elimination Bracket",
            description=f"**{event.get('name','MTG Event')}**\n{len(bracket.players)} players • bracket `{bracket.seed}`",
        )
        round_names = {1: "Round 1", 2: "Semifinals", 3: "Final"}
        max_round = max((m.round_number for m in bracket.matches), default=0)
        for round_number in range(1, max_round + 1):
            matches = [m for m in bracket.matches if m.round_number == round_number]
            lines = []
            for m in matches:
                if m.winner:
                    status = f"✅ <@{m.winner}> advances"
                    if m.player1 and m.player2:
                        lines.append(f"**Match {m.match_number}**\n• <@{m.player1}> vs. <@{m.player2}>\n{status}")
                    elif m.bye:
                        lines.append(f"**Match {m.match_number} — BYE**\n• <@{m.winner}> advances")
                    continue
                if m.bye:
                    player = m.player1 or m.player2
                    lines.append(f"**Match {m.match_number} — BYE**\n• <@{player}> advances")
                else:
                    p1 = f"<@{m.player1}>" if m.player1 else "TBD"
                    p2 = f"<@{m.player2}>" if m.player2 else "TBD"
                    lines.append(f"**Match {m.match_number}**\n• {p1} vs. {p2}")
            if lines:
                embed.add_field(name=round_names.get(round_number, f"Round {round_number}"), value="\n\n".join(lines)[:1024], inline=False)
        embed.set_footer(text="Select a live match below to record the winner.")
        return embed

    def assignment_embed(self, event: dict, mode: str, tables, count: int, seed: int) -> discord.Embed:
        title = "⚔️ Commander Pods" if mode == "commander" else "🃏 MTG Draft"
        embed = discord.Embed(title=title, description=f"**{event.get('name','MTG Event')}**\n{count} attendees • assignment `{seed}`")
        for t in tables:
            mentions = "\n".join(f"• <@{uid}>" for uid in t.players)
            label = "Pod" if mode == "commander" else "Table"
            embed.add_field(name=f"{label} {t.number} — {len(t.players)} players", value=mentions, inline=True)
        return embed

    async def record_bracket_winner(self, interaction: discord.Interaction, event_id: str, match_id: int, winner: str):
        if not self._is_admin(interaction):
            return await interaction.response.send_message("❌ Organizer permissions required.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        data = await self.db.get_bracket(event_id)
        if not data:
            return await interaction.followup.send("❌ That bracket no longer exists.", ephemeral=True)
        bracket = Bracket.from_dict(data["bracket"])
        try:
            bracket.record_winner(match_id, winner)
            await self.db.save_bracket(event_id, data["guild_id"], int(data["channel_id"]) if data["channel_id"] else None, int(data["message_id"]) if data["message_id"] else None, bracket.to_dict())
        except ValueError as exc:
            return await interaction.followup.send(f"❌ {exc}", ephemeral=True)

        channel_id = int(data["channel_id"])
        message_id = int(data["message_id"])
        channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        message = await channel.fetch_message(message_id)
        event = next((json.loads(r[3]) for r in await self.db.list_events(data["guild_id"]) if r[0] == event_id), {"name": "MTG Draft"})
        await message.edit(embed=self.bracket_embed(event, bracket), view=None if bracket.champion else BracketView(self, event_id))

        if bracket.champion:
            await interaction.followup.send(f"🏆 <@{bracket.champion}> is the draft champion!", ephemeral=False)
        else:
            await interaction.followup.send(f"✅ <@{winner}> advances.", ephemeral=True)

class AssignmentView(discord.ui.View):
    def __init__(self, bot: TableBot, event_id: str, mode: str):
        super().__init__(timeout=3600)
        self.bot, self.event_id, self.mode = bot, event_id, mode

    @discord.ui.button(label="Reroll", emoji="🎲", style=discord.ButtonStyle.primary)
    async def reroll(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.bot._is_admin(interaction):
            return await interaction.response.send_message("❌ Organizer permissions required.", ephemeral=True)
        await self.bot.generate(interaction, self.mode, self.event_id, reroll=True)

    @discord.ui.button(label="Sync Sesh API", emoji="🔄", style=discord.ButtonStyle.secondary)
    async def sync(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.bot._is_admin(interaction):
            return await interaction.response.send_message("❌ Organizer permissions required.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            event, attendees = await self.bot.sync_event(interaction.guild_id, self.event_id)
            await interaction.followup.send(f"✅ Sesh synced: **{len(attendees)}** attending.", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"❌ Sync failed: {exc}", ephemeral=True)

class BracketView(discord.ui.View):
    def __init__(self, bot: TableBot, event_id: str):
        super().__init__(timeout=None)
        self.bot, self.event_id = bot, event_id
        self.add_item(BracketMatchSelect(bot, event_id))
        self.add_item(BracketRerollButton(bot, event_id))

class BracketMatchSelect(discord.ui.Select):
    def __init__(self, bot: TableBot, event_id: str):
        self.bot, self.event_id = bot, event_id
        data = None
        # Options are populated lazily at interaction time if the view is restored after restart.
        options = [discord.SelectOption(label="Choose a live match…", value="none", description="Select this to see available matches")]
        super().__init__(placeholder="Record a match result…", min_values=1, max_values=1, options=options, custom_id=f"bracket:select:{event_id}")

    async def callback(self, interaction: discord.Interaction):
        if not self.bot._is_admin(interaction):
            return await interaction.response.send_message("❌ Organizer permissions required.", ephemeral=True)
        data = await self.bot.db.get_bracket(self.event_id)
        if not data:
            return await interaction.response.send_message("❌ Bracket not found.", ephemeral=True)
        bracket = Bracket.from_dict(data["bracket"])
        active = bracket.active_matches()
        if not active:
            return await interaction.response.send_message("🏁 There are no live matches. The bracket may be complete.", ephemeral=True)
        options = [discord.SelectOption(label=f"Match {m.match_number}: {m.player1} vs {m.player2}"[:100], value=str(m.match_id)) for m in active[:25]]
        # Send a fresh ephemeral selector with actual matches.
        view = discord.ui.View(timeout=300)
        chooser = LiveMatchSelect(self.bot, self.event_id, options)
        view.add_item(chooser)
        await interaction.response.send_message("Select the match that just finished:", view=view, ephemeral=True)

class LiveMatchSelect(discord.ui.Select):
    def __init__(self, bot: TableBot, event_id: str, options: list[discord.SelectOption]):
        self.bot, self.event_id = bot, event_id
        super().__init__(placeholder="Choose match…", min_values=1, max_values=1, options=options, custom_id=f"bracket:livematch:{event_id}:{os.urandom(4).hex()}")

    async def callback(self, interaction: discord.Interaction):
        if not self.bot._is_admin(interaction):
            return await interaction.response.send_message("❌ Organizer permissions required.", ephemeral=True)
        match_id = int(self.values[0])
        data = await self.bot.db.get_bracket(self.event_id)
        if not data:
            return await interaction.response.send_message("❌ Bracket not found.", ephemeral=True)
        bracket = Bracket.from_dict(data["bracket"])
        match = bracket.get_match(match_id)
        if not match or match.winner or not match.player1 or not match.player2:
            return await interaction.response.send_message("❌ That match is no longer available.", ephemeral=True)
        guild = interaction.guild

        async def display_name(user_id: str) -> str:
            if guild is not None:
                member = guild.get_member(int(user_id))
                if member is not None:
                    return member.display_name
                try:
                    member = await guild.fetch_member(int(user_id))
                    return member.display_name
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
            try:
                user = await self.bot.fetch_user(int(user_id))
                return user.display_name
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return f"Player {user_id}"

        view = WinnerView(
            self.bot,
            self.event_id,
            match.match_id,
            match.player1,
            match.player2,
            await display_name(match.player1),
            await display_name(match.player2),
        )
        await interaction.response.send_message(
            f"**Match {match.match_number}**\nWhich player won?",
            view=view,
            ephemeral=True,
        )

class WinnerView(discord.ui.View):
    def __init__(self, bot: TableBot, event_id: str, match_id: int, player1: str, player2: str, name1: str, name2: str):
        super().__init__(timeout=300)
        self.bot, self.event_id, self.match_id = bot, event_id, match_id
        self.add_item(WinnerButton(bot, event_id, match_id, player1, name1, "left"))
        self.add_item(WinnerButton(bot, event_id, match_id, player2, name2, "right"))

class WinnerButton(discord.ui.Button):
    def __init__(self, bot: TableBot, event_id: str, match_id: int, player_id: str, display_name: str, side: str):
        self.bot, self.event_id, self.match_id, self.player_id = bot, event_id, match_id, player_id
        label = display_name.strip() or f"User {player_id}"
        super().__init__(label=label[:80], style=discord.ButtonStyle.success, custom_id=f"bracket:winner:{event_id}:{match_id}:{side}")

    async def callback(self, interaction: discord.Interaction):
        await self.bot.record_bracket_winner(interaction, self.event_id, self.match_id, self.player_id)

class BracketRerollButton(discord.ui.Button):
    def __init__(self, bot: TableBot, event_id: str):
        self.bot, self.event_id = bot, event_id
        super().__init__(label="Reroll Bracket", emoji="🎲", style=discord.ButtonStyle.secondary, custom_id=f"bracket:reroll:{event_id}")

    async def callback(self, interaction: discord.Interaction):
        if not self.bot._is_admin(interaction):
            return await interaction.response.send_message("❌ Organizer permissions required.", ephemeral=True)
        await self.bot.generate(interaction, "draft", self.event_id, reroll=True)

class RosterView(discord.ui.View):
    def __init__(self, bot: TableBot, event_id: str):
        super().__init__(timeout=None)
        self.bot, self.event_id = bot, event_id
        self.add_item(RosterRefreshButton(bot, event_id))
        self.add_item(RosterGenerateButton(bot, event_id, "draft", "🃏 Draft Bracket"))
        self.add_item(RosterGenerateButton(bot, event_id, "commander", "⚔️ Commander Pods"))


class RosterRefreshButton(discord.ui.Button):
    def __init__(self, bot: TableBot, event_id: str):
        self.bot, self.event_id = bot, event_id
        super().__init__(label="Refresh RSVPs", emoji="🔄", style=discord.ButtonStyle.primary, custom_id=f"roster:refresh:{event_id}")

    async def callback(self, interaction: discord.Interaction):
        if not self.bot._is_admin(interaction):
            return await interaction.response.send_message("❌ Organizer permissions required.", ephemeral=True)
        await interaction.response.defer()
        try:
            event, rows = await self.bot.refresh_sesh_roster(interaction, self.event_id)
            await interaction.message.edit(embed=self.bot.roster_embed(event, rows), view=RosterView(self.bot, self.event_id))
            going = sum(1 for _, _, opt in rows if opt == "\u2705")
            maybe = sum(1 for _, _, opt in rows if opt == "?")
            no = sum(1 for _, _, opt in rows if opt == "❌")
            await interaction.followup.send(f"✅ RSVP roster refreshed — **{going} Going**, **{maybe} Maybe**, **{no} Not Going**.", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"❌ Refresh failed: {exc}", ephemeral=True)


class RosterGenerateButton(discord.ui.Button):
    def __init__(self, bot: TableBot, event_id: str, mode: str, label: str):
        self.bot, self.event_id, self.mode = bot, event_id, mode
        super().__init__(label=label, style=discord.ButtonStyle.success if mode == "draft" else discord.ButtonStyle.secondary, custom_id=f"roster:generate:{mode}:{event_id}")

    async def callback(self, interaction: discord.Interaction):
        if not self.bot._is_admin(interaction):
            return await interaction.response.send_message("❌ Organizer permissions required.", ephemeral=True)
        await self.bot.generate(interaction, self.mode, self.event_id)


class TablesGroup(app_commands.Group):
    def __init__(self, bot: TableBot):
        super().__init__(name="tables", description="Generate MTG draft brackets and Commander pods")
        self.bot = bot

    @app_commands.command(name="events", description="Show imported MTG event rosters")
    async def events(self, interaction: discord.Interaction):
        if not self.bot._is_admin(interaction):
            return await interaction.response.send_message("❌ Organizer permissions required.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        rows = await self.bot.db.list_events(str(interaction.guild_id))
        if not rows:
            return await interaction.followup.send(
                "No imported event rosters found. Use `/tables fallback` on a Sesh event message first.",
                ephemeral=True,
            )
        lines = []
        for event_id, guild_id, channel_id, event_json in rows[:20]:
            try:
                event = json.loads(event_json)
            except Exception:
                event = {"name": "MTG Event", "source": "manual"}
            attendees = await self.bot.db.get_attendees(event_id, self.bot.cfg.attending_option, self.bot.cfg.include_maybe)
            source = event.get("source", "fallback")
            lines.append(f"`{event_id}` — **{event.get('name','Unnamed')}** — 👥 {len(attendees)} — 🟡 {source}")
        note = "🟡 Primary source: imported Sesh event messages. Use `/tables fallback` to refresh a roster."
        if self.bot.sesh:
            note += " Sesh Pro API is configured as an optional backup."
        await interaction.followup.send(note + "\n\n" + "\n".join(lines), ephemeral=True)

    @app_commands.command(name="generate", description="Generate a Draft 1v1 bracket or Commander pods")
    @app_commands.describe(mode="draft or commander", event_id="Imported Sesh event ID")
    @app_commands.choices(mode=[app_commands.Choice(name="Draft — 1v1 Bracket", value="draft"), app_commands.Choice(name="Commander", value="commander")])
    async def generate(self, interaction: discord.Interaction, mode: app_commands.Choice[str], event_id: str):
        await self.bot.generate(interaction, mode.value, event_id)

    @app_commands.command(name="sync", description="Refresh an event from Sesh Pro API (optional)")
    @app_commands.describe(event_id="Sesh event ID")
    async def sync(self, interaction: discord.Interaction, event_id: str):
        if not self.bot._is_admin(interaction):
            return await interaction.response.send_message("❌ Organizer permissions required.", ephemeral=True)
        if not self.bot.sesh:
            return await interaction.response.send_message(
                "ℹ️ Sesh Pro API is not configured. Use `/tables fallback` to import the attendees from the Sesh event message.",
                ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True)
        try:
            event, attendees = await self.bot.sync_event(interaction.guild_id, event_id)
            await interaction.followup.send(f"✅ Sesh API synced: **{event.get('name','event')}** — **{len(attendees)}** attending.", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"❌ Sesh API sync failed: {exc}", ephemeral=True)

    @app_commands.command(name="fallback", description="Import Going/Maybe/No RSVPs from a Sesh event message")
    @app_commands.describe(channel="Channel containing the Sesh event message", message_id="Message ID of the Sesh event")
    async def fallback(self, interaction: discord.Interaction, channel: discord.TextChannel, message_id: str):
        if not self.bot._is_admin(interaction):
            return await interaction.response.send_message("❌ Organizer permissions required.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            msg = await channel.fetch_message(int(message_id))
            event, rsvps, rows = await self.bot.import_sesh_message(interaction.guild, channel, msg)
            going = len(rsvps["going"])
            maybe = len(rsvps["maybe"])
            no = len(rsvps["no"])
            roster = await interaction.channel.send(embed=self.bot.roster_embed(event, rows), view=RosterView(self.bot, event["id"]))
            event["roster_message_id"] = str(roster.id)
            await self.bot.db.upsert_event(event, str(interaction.guild_id))
            await interaction.followup.send(
                f"✅ Sesh RSVP roster imported. **{going} Going**, **{maybe} Maybe**, **{no} Not Going**.\n\nRoster card posted above with **Refresh RSVPs** and generation controls.",
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.followup.send(f"❌ Fallback failed: {exc}", ephemeral=True)
