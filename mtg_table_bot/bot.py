from __future__ import annotations
import json
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
import discord
from discord import app_commands
from discord.ext import commands

from .assignments import make_assignment
from .bracket import Bracket, build_single_elimination
from .config import AppConfig
from .db import Database
from .fallback import is_sesh_event_message, parse_sesh_message, parse_sesh_rsvps, sesh_event_title
from .sesh import SeshClient, SeshError
from .feeds import FeedPublisher

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
        self.feed_publisher = FeedPublisher(self, cfg)
        self.feed_task: asyncio.Task | None = None
        self.deals_cleanup_task: asyncio.Task | None = None
        self.command_ids: dict[str, int] = {}

    async def setup_hook(self):
        await self.db.init()
        guild_id = os.getenv("DISCORD_GUILD_ID", "").strip()
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            self.logger.info("Slash commands synced to guild %s", guild_id)
            try:
                remote_commands = await self.tree.fetch_commands(guild=guild)
                self.command_ids = {command.name: command.id for command in remote_commands}
            except discord.HTTPException:
                self.logger.exception("Could not fetch synced command IDs for the Spellbook hub")
        else:
            await self.tree.sync()
            self.logger.info("Global slash commands synced")
            try:
                remote_commands = await self.tree.fetch_commands()
                self.command_ids = {command.name: command.id for command in remote_commands}
            except discord.HTTPException:
                self.logger.exception("Could not fetch synced command IDs for the Spellbook hub")

        # Re-register persistent notification opt-in controls.
        self.add_view(NotificationRoleView(self))

        # Re-register persistent RSVP roster controls.
        for event_id, _name, _start_time, event_json in await self.db.list_events(str(guild_id)) if guild_id else []:
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
        # Keep the bot's status visible after startup or a Discord reconnect.
        await self.change_presence(
            activity=discord.Game(name="Currently calculating lethal | /spellbook"),
            status=discord.Status.online,
        )
        if (self.cfg.news_channel_id or self.cfg.deals_channel_id) and (self.feed_task is None or self.feed_task.done()):
            self.feed_task = asyncio.create_task(self.feed_publisher.run(), name="mtg-feed-publisher")
        if self.cfg.deals_channel_id and (self.deals_cleanup_task is None or self.deals_cleanup_task.done()):
            self.deals_cleanup_task = asyncio.create_task(self.cleanup_old_deal_posts(), name="mtg-deals-cleanup")

    async def cleanup_old_deal_posts(self):
        """Delete deal forum posts after the configured period without activity."""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                channel = self.get_channel(self.cfg.deals_channel_id) if self.cfg.deals_channel_id else None
                if channel is None and self.cfg.deals_channel_id:
                    channel = await self.fetch_channel(self.cfg.deals_channel_id)
                if isinstance(channel, discord.ForumChannel):
                    cutoff = datetime.now(timezone.utc) - timedelta(days=self.cfg.deals_auto_delete_days)
                    threads = {thread.id: thread for thread in channel.threads}
                    # Include archived forum posts too; replies update last_message_id and reset the timer.
                    async for thread in channel.archived_threads(limit=None):
                        threads[thread.id] = thread
                    for thread in threads.values():
                        last_activity = discord.utils.snowflake_time(thread.last_message_id) if thread.last_message_id else thread.created_at
                        if last_activity and last_activity < cutoff:
                            try:
                                await thread.delete(reason=f"MTG deal post inactive for {self.cfg.deals_auto_delete_days} days")
                                self.logger.info("Deleted inactive MTG deal post %s (%s)", thread.id, thread.name)
                            except discord.NotFound:
                                pass
                            except (discord.Forbidden, discord.HTTPException):
                                self.logger.exception("Could not delete inactive MTG deal post %s", thread.id)
                elif channel is not None:
                    self.logger.warning("Deal auto-cleanup is enabled, but configured deals channel %s is not a Forum Channel; skipping cleanup", self.cfg.deals_channel_id)
            except asyncio.CancelledError:
                raise
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                self.logger.exception("MTG deals cleanup check failed")
            except Exception:
                self.logger.exception("Unexpected error during MTG deals cleanup")
            await asyncio.sleep(self.cfg.deals_cleanup_poll_minutes * 60)

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

    async def find_sesh_events(self, channel: discord.TextChannel, limit: int | None = None) -> list[discord.Message]:
        """Scan a schedule channel and return recent Sesh RSVP event messages."""
        scan_limit = limit or self.cfg.schedule_scan_limit
        found: list[discord.Message] = []
        async for message in channel.history(limit=scan_limit):
            try:
                if is_sesh_event_message(message):
                    found.append(message)
            except Exception:
                self.logger.exception("Failed parsing possible Sesh message %s", message.id)
        return found[:25]

    async def import_sesh_message(self, guild: discord.Guild, channel: discord.TextChannel, message: discord.Message):
        rsvps = parse_sesh_rsvps(message)
        if not any(rsvps.values()):
            raise RuntimeError("I couldn't find the Sesh Attendees/Maybe/No sections or any Discord mentions in the event message.")

        event_id = f"sesh-{message.id}"
        title = sesh_event_title(message)
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
        embed.set_footer(text=f"The Spellbook Clerk • Source: Sesh message • Event ID: {event.get('id')}")
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
                await self.db.sync_draft_tournament(event_id, str(interaction.guild_id), event.get("name", "MTG Draft"), bracket.to_dict())
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
            embed.set_footer(text=f"The Spellbook Clerk • Bracket {bracket.seed}")
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
        embed.set_footer(text="The Spellbook Clerk • Select a live match below to record the winner.")
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
            event_name = await self.db.get_event_name(event_id)
            await self.db.sync_draft_tournament(event_id, data["guild_id"], event_name, bracket.to_dict())
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


class SeshEventSelect(discord.ui.Select):
    def __init__(self, bot: TableBot, channel: discord.TextChannel, messages: list[discord.Message]):
        self.bot, self.channel, self.messages = bot, channel, {str(m.id): m for m in messages}
        options = []
        for message in messages:
            rsvps = parse_sesh_rsvps(message)
            title = sesh_event_title(message)
            counts = f"{len(rsvps['going'])} Going • {len(rsvps['maybe'])} Maybe • {len(rsvps['no'])} No"
            options.append(discord.SelectOption(
                label=title[:100],
                description=f"{counts} • {message.created_at.strftime('%b %d')}"[:100],
                value=str(message.id),
            ))
        super().__init__(placeholder="Choose a Sesh event…", min_values=1, max_values=1, options=options, custom_id=f"sesh:create:{channel.id}:{os.urandom(4).hex()}")

    async def callback(self, interaction: discord.Interaction):
        if not self.bot._is_admin(interaction):
            return await interaction.response.send_message("❌ Organizer permissions required.", ephemeral=True)
        message = self.messages.get(self.values[0])
        if message is None:
            return await interaction.response.send_message("❌ That Sesh event is no longer available.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            event, rsvps, rows = await self.bot.import_sesh_message(interaction.guild, self.channel, message)
            roster = await interaction.channel.send(embed=self.bot.roster_embed(event, rows), view=RosterView(self.bot, event["id"]))
            event["roster_message_id"] = str(roster.id)
            await self.bot.db.upsert_event(event, str(interaction.guild_id))
            await interaction.followup.send(
                f"✅ Imported **{event.get('name', 'Sesh Event')}** automatically — **{len(rsvps['going'])} Going**, **{len(rsvps['maybe'])} Maybe**, **{len(rsvps['no'])} Not Going**.\n\nThe roster card is posted above. Use **Draft Bracket** or **Commander Pods** to generate the table.",
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.followup.send(f"❌ Import failed: {exc}", ephemeral=True)


class SeshEventCreateView(discord.ui.View):
    def __init__(self, bot: TableBot, channel: discord.TextChannel, messages: list[discord.Message]):
        super().__init__(timeout=300)
        self.add_item(SeshEventSelect(bot, channel, messages))


class TablesGroup(app_commands.Group):
    def __init__(self, bot: TableBot):
        super().__init__(name="tables", description="Generate MTG draft brackets and Commander pods")
        self.bot = bot

    @app_commands.command(name="create", description="Find a Sesh event in the schedule channel and build its roster")
    @app_commands.describe(channel="Optional schedule channel to scan; otherwise the configured schedule channel is used")
    async def create(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        if not self.bot._is_admin(interaction):
            return await interaction.response.send_message("❌ Organizer permissions required.", ephemeral=True)
        channel = channel or (self.bot.get_channel(self.bot.cfg.schedule_channel_id) if self.bot.cfg.schedule_channel_id else None)
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message(
                "❌ I don't have a schedule channel configured. Use `/tables create channel:#your-schedule-channel` or set `schedule.channel_id` in `config.yaml`.",
                ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True)
        try:
            messages = await self.bot.find_sesh_events(channel)
            if not messages:
                return await interaction.followup.send(
                    f"❌ I couldn't find any Sesh event messages in {channel.mention}. Make sure the bot can **View Channel** and **Read Message History**.",
                    ephemeral=True,
                )
            view = SeshEventCreateView(self.bot, channel, messages)
            await interaction.followup.send(
                f"📅 Found **{len(messages)}** Sesh event(s) in {channel.mention}. Choose the event you want to use:",
                view=view,
                ephemeral=True,
            )
        except discord.Forbidden:
            await interaction.followup.send(
                f"❌ I can't read {channel.mention}. Give the bot **View Channel** and **Read Message History** permissions.",
                ephemeral=True,
            )
        except Exception as exc:
            self.bot.logger.exception("Automatic Sesh scan failed")
            await interaction.followup.send(f"❌ Schedule scan failed: {exc}", ephemeral=True)


class NotificationRoleView(discord.ui.View):
    """Persistent self-service toggles for MTG feed notification roles."""
    def __init__(self, bot: TableBot):
        super().__init__(timeout=None)
        self.bot = bot

    async def _toggle(self, interaction: discord.Interaction, role_name: str):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Use these buttons inside the server.", ephemeral=True)
        role = discord.utils.get(interaction.guild.roles, name=role_name)
        if role is None:
            return await interaction.response.send_message(
                f"The **{role_name}** role hasn't been created yet. Ask an organizer to run `/notifications setup`.", ephemeral=True
            )
        me = interaction.guild.me
        if me is None or not me.guild_permissions.manage_roles or role >= me.top_role:
            return await interaction.response.send_message(
                "I can't manage that role. Give me **Manage Roles** and move my bot role above the notification roles.", ephemeral=True
            )
        try:
            if role in interaction.user.roles:
                await interaction.user.remove_roles(role, reason="Member opted out of MTG feed notifications")
                message = f"🔕 Removed **{role.name}**. You won't be pinged for that feed."
            else:
                await interaction.user.add_roles(role, reason="Member opted into MTG feed notifications")
                message = f"🔔 Added **{role.name}**. You'll be pinged for new posts in that feed."
            await interaction.response.send_message(message, ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                "I don't have permission to update that role. Check **Manage Roles** and my role's position.", ephemeral=True
            )

    @discord.ui.button(label="MTG News", style=discord.ButtonStyle.primary, emoji="📰", custom_id="spellbook:notify:news")
    async def news(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._toggle(interaction, "MTG News")

    @discord.ui.button(label="MTG Deals", style=discord.ButtonStyle.success, emoji="💰", custom_id="spellbook:notify:deals")
    async def deals(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._toggle(interaction, "MTG Deals")


class NotificationsGroup(app_commands.Group):
    def __init__(self, bot: TableBot):
        super().__init__(name="notifications", description="Manage opt-in MTG feed notifications")
        self.bot = bot

    @app_commands.command(name="setup", description="Create the opt-in roles and post the notification sign-up panel")
    async def setup(self, interaction: discord.Interaction):
        if not self.bot._is_admin(interaction):
            return await interaction.response.send_message("❌ Organizer permissions required.", ephemeral=True)
        if not interaction.guild or not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
            return await interaction.response.send_message("Run this command in a server text channel.", ephemeral=True)
        me = interaction.guild.me
        if me is None or not me.guild_permissions.manage_roles:
            return await interaction.response.send_message("❌ I need the **Manage Roles** permission to create and assign opt-in roles.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        roles = {}
        try:
            for role_name, color in (("MTG News", discord.Color.blue()), ("MTG Deals", discord.Color.gold())):
                role = discord.utils.get(interaction.guild.roles, name=role_name)
                if role is None:
                    role = await interaction.guild.create_role(name=role_name, color=color, mentionable=True, reason="MTG feed notification opt-in")
                if role >= me.top_role:
                    return await interaction.followup.send(
                        f"❌ I can't manage **{role_name}** because it is at or above my highest role. Move my bot role above it, then run `/notifications setup` again.", ephemeral=True
                    )
                roles[role_name] = role
            embed = discord.Embed(
                title="🔔 Choose Your MTG Notifications",
                description=(
                    "Want to keep up with the latest Magic news or deals without getting every notification? "
                    "Use the buttons below to opt in or out of each feed. You can choose either, both, or neither.\n\n"
                    "📰 **MTG News** — official Magic announcements\n"
                    "💰 **MTG Deals** — deals from r/sealedmtgdeals\n\n"
                    "Press a button again any time to remove that role."
                ),
                color=discord.Color.blurple(),
            )
            await interaction.channel.send(embed=embed, view=NotificationRoleView(self.bot))
            await interaction.followup.send(
                "✅ Notification roles are ready and the sign-up panel has been posted here. "
                "Make sure my bot role is above **MTG News** and **MTG Deals**.", ephemeral=True
            )
        except discord.Forbidden:
            await interaction.followup.send("❌ I need **Manage Roles** and permission to send messages/embed links in this channel.", ephemeral=True)


def _slash_mention(bot: TableBot, qualified_name: str) -> str:
    """Build a clickable Discord slash-command mention when its remote ID is known."""
    root = qualified_name.split(" ", 1)[0]
    command_id = bot.command_ids.get(root)
    return f"</{qualified_name}:{command_id}>" if command_id else f"`/{qualified_name}`"


def _stats_embed(title: str, description: str | None = None) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=discord.Color.blurple())


class SpellbookHubView(discord.ui.View):
    """Ephemeral navigation hub that links to the bot's feature commands."""
    def __init__(self, bot: TableBot):
        super().__init__(timeout=300)
        self.bot = bot

    def page(self, section: str = "home") -> discord.Embed:
        if section == "commander":
            embed = _stats_embed("⚔️ Commander", "Record games and explore player and commander performance.")
            embed.add_field(name="Record a game", value=f"{_slash_mention(self.bot, 'commander record')}\nChoose an event, pod, commanders, and winner.", inline=False)
            embed.add_field(name="Player statistics", value=f"{_slash_mention(self.bot, 'commander leaderboard')} · {_slash_mention(self.bot, 'commander player')}", inline=False)
            embed.add_field(name="Commander rankings", value=f"{_slash_mention(self.bot, 'commander commanders')}\nCompare commander win rates and usage.", inline=False)
            embed.add_field(name="Game history & corrections", value=f"{_slash_mention(self.bot, 'commander history')} · {_slash_mention(self.bot, 'commander delete')}", inline=False)
            return embed
        if section == "draft":
            embed = _stats_embed("🏆 Draft", "Tournament standings, match records, and past champions.")
            embed.add_field(name="Leaderboards", value=f"{_slash_mention(self.bot, 'draft leaderboard')}\nUse the optional year filter for a calendar-year view.", inline=False)
            embed.add_field(name="Player profile", value=f"{_slash_mention(self.bot, 'draft player')}", inline=False)
            embed.add_field(name="Tournament history", value=f"{_slash_mention(self.bot, 'draft history')}", inline=False)
            return embed
        if section == "tables":
            embed = _stats_embed("🧑‍🤝‍🧑 Tables", "Organize game night and generate pods or draft brackets.")
            embed.add_field(name="Create tables", value=f"{_slash_mention(self.bot, 'tables create')}\nFind a Sesh event, import RSVPs, and post the roster card.", inline=False)
            return embed
        if section == "feeds":
            embed = _stats_embed("🔔 News & Deals", "Choose which MTG updates members receive.")
            embed.add_field(name="Notification panel", value=f"{_slash_mention(self.bot, 'notifications setup')}\nOrganizer-only: post the opt-in News and Deals role panel.", inline=False)
            return embed

        embed = _stats_embed(
            "📖 The Spellbook Clerk",
            "Welcome, Planeswalker. Choose a section below to find commands and tools.\n\n"
            "Use the buttons to browse. The command references are clickable and open Discord's slash-command interface.",
        )
        embed.add_field(name="⚔️ Commander", value="Game recording, player stats, and commander rankings", inline=True)
        embed.add_field(name="🏆 Draft", value="Tournament history and leaderboards", inline=True)
        embed.add_field(name="🧑‍🤝‍🧑 Tables", value="RSVP rosters, Commander pods, and draft brackets", inline=True)
        embed.add_field(name="🔔 News & Deals", value="Opt-in feed notification settings", inline=True)
        embed.set_footer(text="The Spellbook Clerk • Choose a section below")
        return embed

    async def _show(self, interaction: discord.Interaction, section: str):
        await interaction.response.edit_message(embed=self.page(section), view=self)

    @discord.ui.button(label="Home", emoji="📖", style=discord.ButtonStyle.secondary)
    async def home(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show(interaction, "home")

    @discord.ui.button(label="Commander", emoji="⚔️", style=discord.ButtonStyle.primary)
    async def commander(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show(interaction, "commander")

    @discord.ui.button(label="Draft", emoji="🏆", style=discord.ButtonStyle.primary)
    async def draft(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show(interaction, "draft")

    @discord.ui.button(label="Tables", emoji="🧑‍🤝‍🧑", style=discord.ButtonStyle.secondary)
    async def tables(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show(interaction, "tables")

    @discord.ui.button(label="Feeds", emoji="🔔", style=discord.ButtonStyle.secondary)
    async def feeds(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show(interaction, "feeds")


async def spellbook_home(interaction: discord.Interaction):
    """Root /spellbook command: open the private navigation hub."""
    bot = interaction.client
    if not isinstance(bot, TableBot):
        return await interaction.response.send_message("The Spellbook Clerk is unavailable.", ephemeral=True)
    view = SpellbookHubView(bot)
    await interaction.response.send_message(embed=view.page(), view=view, ephemeral=True)


class CommanderGroup(app_commands.Group):
    def __init__(self, bot: TableBot):
        super().__init__(name="commander", description="Record Commander games and view statistics")
        self.bot = bot

    @app_commands.command(name="record", description="Record a completed game from a saved Commander pod")
    async def record(self, interaction: discord.Interaction):
        if not self.bot._is_admin(interaction):
            return await interaction.response.send_message("❌ Organizer permissions required to record game results.", ephemeral=True)
        if not interaction.guild_id:
            return await interaction.response.send_message("Run this command inside your MTG server.", ephemeral=True)
        assignments = await self.bot.db.list_recent_assignments(str(interaction.guild_id), "commander", 25)
        if not assignments:
            return await interaction.response.send_message(
                "I couldn't find any saved Commander pods. Run `/tables create` and choose **Commander Pods** first.", ephemeral=True
            )
        view = CommanderAssignmentView(self.bot, assignments)
        await interaction.response.send_message(
            "Choose the game-night event whose Commander pod just finished:", view=view, ephemeral=True
        )

    @app_commands.command(name="leaderboard", description="View the Commander player leaderboard")
    async def leaderboard(self, interaction: discord.Interaction):
        if not interaction.guild_id:
            return await interaction.response.send_message("Run this command inside your MTG server.", ephemeral=True)
        rows = await self.bot.db.commander_leaderboard(str(interaction.guild_id))
        embed = _stats_embed("⚔️ Commander Player Leaderboard", "Ranked by wins, then win rate. All recorded games are included.")
        if not rows:
            embed.description = "No Commander games have been recorded yet. Use `/commander record` after a game."
        else:
            lines = []
            for index, (uid, display_name, games, wins) in enumerate(rows[:15], 1):
                pct = (wins / games * 100) if games else 0
                lines.append(f"**{index}.** <@{uid}> — **{wins}–{games-wins}** ({pct:.1f}%) · {games} games")
            embed.description = "\n".join(lines)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="player", description="View a player's Commander record and commander stats")
    @app_commands.describe(member="Player to look up; defaults to you")
    async def player(self, interaction: discord.Interaction, member: discord.Member | None = None):
        if not interaction.guild_id:
            return await interaction.response.send_message("Run this command inside your MTG server.", ephemeral=True)
        target = member or interaction.user
        total, wins, saved_name = await self.bot.db.commander_player_stats(str(interaction.guild_id), str(target.id))
        if not total:
            return await interaction.response.send_message(f"No Commander games are recorded for {target.mention} yet.")
        pct = wins / total * 100
        history = await self.bot.db.commander_history(str(interaction.guild_id), 50, str(target.id))
        streak = 0
        for game in history:
            result = next((p[3] for p in game["players"] if p[0] == str(target.id)), 0)
            if not result:
                break
            streak += 1
        commanders = await self.bot.db.commander_player_commanders(str(interaction.guild_id), str(target.id))
        embed = _stats_embed(f"⚔️ Commander Profile — {target.display_name}")
        embed.add_field(name="Games", value=str(total), inline=True)
        embed.add_field(name="Wins", value=str(wins), inline=True)
        embed.add_field(name="Win rate", value=f"{pct:.1f}%", inline=True)
        embed.add_field(name="Current win streak", value=str(streak), inline=True)
        if commanders:
            best = sorted(commanders, key=lambda r: (r[1], r[2]), reverse=True)[:5]
            embed.add_field(name="Commanders played", value="\n".join(f"**{name}** — {w}–{n-w} ({w/n*100:.0f}%)" for name,n,w in best), inline=False)
        recent = []
        for game in history[:5]:
            result = next((p[3] for p in game["players"] if p[0] == str(target.id)), 0)
            recent.append(f"{'🏆 Win' if result else 'Loss'} · Pod {game['pod_number']} · {game['created_at'][:10]}")
        if recent:
            embed.add_field(name="Recent games", value="\n".join(recent), inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="commanders", description="Rank commanders by recorded wins and win rate")
    async def commanders(self, interaction: discord.Interaction):
        if not interaction.guild_id:
            return await interaction.response.send_message("Run this command inside your MTG server.", ephemeral=True)
        rows = await self.bot.db.commander_commander_leaderboard(str(interaction.guild_id))
        embed = _stats_embed("👑 Commander Rankings", "Grouped by commander name across all players. Small sample sizes are shown for context.")
        if not rows:
            embed.description = "No commanders have been recorded yet."
        else:
            lines = []
            for index, (name, games, wins) in enumerate(rows[:15], 1):
                lines.append(f"**{index}. {name}** — {wins}–{games-wins} ({wins/games*100:.1f}%) · {games} games")
            embed.description = "\n".join(lines)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="delete", description="Delete an incorrectly recorded Commander game")
    @app_commands.describe(game_id="Game number shown in the Commander history")
    async def delete(self, interaction: discord.Interaction, game_id: int):
        if not self.bot._is_admin(interaction):
            return await interaction.response.send_message("Organizer permissions required.", ephemeral=True)
        if not interaction.guild_id:
            return await interaction.response.send_message("Run this command inside your MTG server.", ephemeral=True)
        deleted = await self.bot.db.delete_commander_game(str(interaction.guild_id), game_id)
        await interaction.response.send_message(
            f"✅ Deleted Commander game **#{game_id}** and removed it from the statistics." if deleted
            else f"I couldn't find Commander game **#{game_id}** in this server.",
            ephemeral=True,
        )

    @app_commands.command(name="history", description="Show recently recorded Commander games")
    @app_commands.describe(member="Optionally show games involving one player", limit="Number of games to display")
    @app_commands.guild_only()
    @app_commands.checks.cooldown(2, 10.0)
    async def history(self, interaction: discord.Interaction, member: discord.Member | None = None, limit: app_commands.Range[int, 1, 15] = 10):
        rows = await self.bot.db.commander_history(str(interaction.guild_id), int(limit), str(member.id) if member else None)
        embed = _stats_embed("📜 Commander Game History")
        if not rows:
            embed.description = "No Commander games have been recorded yet."
        else:
            lines = []
            for game in rows:
                winner = f"<@{game['winner_id']}>"
                others = [f"<@{p[0]}> ({p[2]})" for p in game["players"]]
                event_name = await self.bot.db.get_event_name(game["event_id"])
                lines.append(f"**Game #{game['id']} · {event_name} · Pod {game['pod_number']}** — 🏆 {winner}\n{', '.join(others)} · {game['created_at'][:16]}")
            embed.description = "\n\n".join(lines)[:4000]
        await interaction.response.send_message(embed=embed)


class DraftGroup(app_commands.Group):
    def __init__(self, bot: TableBot):
        super().__init__(name="draft", description="Draft tournament leaderboards and history")
        self.bot = bot

    @app_commands.command(name="leaderboard", description="View all-time or calendar-year draft standings")
    @app_commands.describe(year="Optional calendar year; omit for lifetime standings")
    async def leaderboard(self, interaction: discord.Interaction, year: int | None = None):
        if not interaction.guild_id:
            return await interaction.response.send_message("Run this command inside your MTG server.", ephemeral=True)
        rows = await self.bot.db.draft_leaderboard(str(interaction.guild_id), year)
        title = f"🏆 Draft Leaderboard — {year}" if year else "🏆 All-Time Draft Leaderboard"
        embed = _stats_embed(title, "Ranked by tournament wins, then match wins. Byes do not count as matches or victories.")
        if not rows:
            embed.description = "No draft tournament history is available yet. Completed brackets will populate this leaderboard automatically."
        else:
            lines = []
            for index, row in enumerate(rows[:15], 1):
                uid, appearances, tournament_wins, runner_ups, played, wins, losses = row
                pct = wins / played * 100 if played else 0
                lines.append(f"**{index}.** <@{uid}> — 🏆 {tournament_wins} · 🥈 {runner_ups} · **{wins}–{losses}** ({pct:.1f}%) · {appearances} drafts")
            embed.description = "\n".join(lines)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="player", description="View an individual player's draft career")
    @app_commands.describe(member="Player to look up; defaults to you", year="Optional calendar year")
    async def player(self, interaction: discord.Interaction, member: discord.Member | None = None, year: int | None = None):
        if not interaction.guild_id:
            return await interaction.response.send_message("Run this command inside your MTG server.", ephemeral=True)
        target = member or interaction.user
        row = await self.bot.db.draft_player_stats(str(interaction.guild_id), str(target.id), year)
        title = f"🏆 Draft Profile — {target.display_name}"
        if year:
            title += f" ({year})"
        if not row:
            return await interaction.response.send_message(f"No draft records are available for {target.mention} in that period.")
        _, appearances, tournament_wins, runner_ups, played, wins, losses = row
        pct = wins / played * 100 if played else 0
        embed = _stats_embed(title)
        embed.add_field(name="Drafts", value=str(appearances), inline=True)
        embed.add_field(name="Tournament wins", value=str(tournament_wins), inline=True)
        embed.add_field(name="Runner-up finishes", value=str(runner_ups), inline=True)
        embed.add_field(name="Match record", value=f"{wins}–{losses}", inline=True)
        embed.add_field(name="Match win rate", value=f"{pct:.1f}%", inline=True)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="history", description="Show recent draft tournaments and champions")
    @app_commands.describe(limit="Number of tournaments to display", year="Optional calendar year")
    async def history(self, interaction: discord.Interaction, limit: app_commands.Range[int, 1, 15] = 10, year: int | None = None):
        if not interaction.guild_id:
            return await interaction.response.send_message("Run this command inside your MTG server.", ephemeral=True)
        rows = await self.bot.db.draft_history(str(interaction.guild_id), int(limit), year)
        embed = _stats_embed("📚 Draft Tournament History")
        if not rows:
            embed.description = "No draft tournaments have been recorded yet."
        else:
            lines = []
            for event_id, name, started_at, completed_at, champion, runner_up, players_json in rows:
                if champion:
                    result = f"🏆 <@{champion}>" + (f" · 🥈 <@{runner_up}>" if runner_up else "")
                else:
                    result = "⏳ In progress"
                try:
                    count = len(json.loads(players_json))
                except (TypeError, ValueError):
                    count = 0
                date = (started_at or "")[:10]
                lines.append(f"**{name}** · {date} · {count} players\n{result}")
            embed.description = "\n\n".join(lines)[:4000]
        await interaction.response.send_message(embed=embed)


class CommanderAssignmentView(discord.ui.View):
    def __init__(self, bot: TableBot, assignments):
        super().__init__(timeout=300)
        self.add_item(CommanderAssignmentSelect(bot, assignments))


class CommanderAssignmentSelect(discord.ui.Select):
    def __init__(self, bot: TableBot, assignments):
        self.bot = bot
        self.assignments = {str(row[0]): row for row in assignments}
        options = []
        for assignment_id, event_id, event_name, tables_json, created_at in assignments[:25]:
            try:
                pod_count = len(json.loads(tables_json))
            except (TypeError, ValueError):
                pod_count = 0
            options.append(discord.SelectOption(
                label=str(event_name)[:100], value=str(assignment_id),
                description=f"{pod_count} pods · {str(created_at)[:16]}"[:100],
            ))
        super().__init__(placeholder="Choose a Commander event…", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if not self.bot._is_admin(interaction):
            return await interaction.response.send_message("Organizer permissions required.", ephemeral=True)
        row = self.assignments.get(self.values[0])
        if not row:
            return await interaction.response.send_message("That Commander assignment is no longer available.", ephemeral=True)
        assignment_id, event_id, event_name, tables_json, created_at = row
        try:
            pods = json.loads(tables_json)
        except (TypeError, ValueError):
            pods = []
        if not pods or not any(pods):
            return await interaction.response.send_message("No pods were saved for that assignment.", ephemeral=True)
        view = CommanderPodView(self.bot, int(assignment_id), str(event_id), str(event_name), pods)
        await interaction.response.send_message(f"**{event_name}** — choose the pod that finished a game:", view=view, ephemeral=True)


class CommanderPodView(discord.ui.View):
    def __init__(self, bot: TableBot, assignment_id: int, event_id: str, event_name: str, pods: list[list[str]]):
        super().__init__(timeout=300)
        self.add_item(CommanderPodSelect(bot, assignment_id, event_id, event_name, pods))


class CommanderPodSelect(discord.ui.Select):
    def __init__(self, bot: TableBot, assignment_id: int, event_id: str, event_name: str, pods: list[list[str]]):
        self.bot, self.assignment_id, self.event_id, self.event_name = bot, assignment_id, event_id, event_name
        self.pods = pods
        options = [discord.SelectOption(label=f"Pod {i} — {len(players)} players", value=str(i-1)) for i, players in enumerate(pods, 1) if players]
        super().__init__(placeholder="Choose a pod…", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if not self.bot._is_admin(interaction):
            return await interaction.response.send_message("Organizer permissions required.", ephemeral=True)
        pod_index = int(self.values[0])
        if pod_index >= len(self.pods):
            return await interaction.response.send_message("That pod is no longer available.", ephemeral=True)
        member_rows = []
        for uid in self.pods[pod_index]:
            try:
                display_name = await self.bot._member_display_name(interaction.guild, str(uid))
            except Exception:
                display_name = f"Player {uid}"
            member_rows.append((str(uid), display_name))
        if len(member_rows) > 5:
            return await interaction.response.send_message("This pod has more than five players, which exceeds Discord's modal field limit. Please split or adjust the pod before recording.", ephemeral=True)
        modal = CommanderGameModal(self.bot, self.assignment_id, self.event_id, self.event_name, pod_index + 1, member_rows)
        await interaction.response.send_modal(modal)


class CommanderGameModal(discord.ui.Modal):
    def __init__(self, bot: TableBot, assignment_id: int, event_id: str, event_name: str,
                 pod_number: int, members: list[tuple[str, str]]):
        super().__init__(title=f"Record Pod {pod_number} Commander Game")
        self.bot, self.assignment_id, self.event_id = bot, assignment_id, event_id
        self.event_name, self.pod_number = event_name, pod_number
        self.members = members
        self.commander_inputs = []
        for uid, display_name in members:
            field = discord.ui.TextInput(
                label=display_name[:45], placeholder="Enter commander name(s)",
                required=True, max_length=100,
            )
            self.commander_inputs.append((uid, display_name, field))
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction):
        if not self.bot._is_admin(interaction):
            return await interaction.response.send_message("Organizer permissions required.", ephemeral=True)
        players = []
        for uid, display_name, field in self.commander_inputs:
            commander = str(field.value).strip()
            if not commander:
                return await interaction.response.send_message(f"Please enter a commander for {display_name}.", ephemeral=True)
            players.append((uid, display_name, commander))
        view = CommanderWinnerView(
            self.bot, str(interaction.guild_id), self.event_id, self.assignment_id,
            self.pod_number, str(interaction.user.id), players,
        )
        summary = "\n".join(f"• **{name}** — {commander}" for _, name, commander in players)
        await interaction.response.send_message(
            f"**{self.event_name} · Pod {self.pod_number}**\n{summary}\n\nWho won this game?",
            view=view, ephemeral=True,
        )


class CommanderWinnerView(discord.ui.View):
    def __init__(self, bot: TableBot, guild_id: str, event_id: str, assignment_id: int,
                 pod_number: int, recorded_by: str, players: list[tuple[str, str, str]]):
        super().__init__(timeout=300)
        self.add_item(CommanderWinnerSelect(bot, guild_id, event_id, assignment_id, pod_number, recorded_by, players))


class CommanderWinnerSelect(discord.ui.Select):
    def __init__(self, bot: TableBot, guild_id: str, event_id: str, assignment_id: int,
                 pod_number: int, recorded_by: str, players: list[tuple[str, str, str]]):
        self.bot, self.guild_id, self.event_id = bot, guild_id, event_id
        self.assignment_id, self.pod_number, self.recorded_by = assignment_id, pod_number, recorded_by
        self.players = players
        options = [discord.SelectOption(label=name[:100], value=uid, description=commander[:100]) for uid, name, commander in players]
        super().__init__(placeholder="Select the winner…", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if not self.bot._is_admin(interaction):
            return await interaction.response.send_message("Organizer permissions required.", ephemeral=True)
        winner_id = self.values[0]
        try:
            game_id = await self.bot.db.save_commander_game(
                self.guild_id, self.event_id, self.assignment_id, self.pod_number,
                winner_id, self.recorded_by, self.players,
            )
        except Exception as exc:
            self.bot.logger.exception("Could not save Commander game result")
            return await interaction.response.send_message(f"Couldn't save that game: {exc}", ephemeral=True)
        await interaction.response.edit_message(
            content=f"✅ Commander game **#{game_id}** recorded. Winner: <@{winner_id}>.\nUse `/commander record` again for the next game.",
            view=None,
        )
