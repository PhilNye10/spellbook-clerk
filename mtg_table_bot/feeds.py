from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import urljoin
import aiohttp
import discord
from .config import AppConfig

log = logging.getLogger("mtg-table-bot.feeds")
WIZARDS_URL = "https://magic.wizards.com/en/news/announcements/"
REDDIT_RSS = "https://www.reddit.com/r/sealedmtgdeals/.rss"
USER_AGENT = "MTGTableBot/1.0 (Discord community feed notifier)"

class MetaParser(HTMLParser):
    """Collect Open Graph metadata used for rich link previews."""
    def __init__(self):
        super().__init__()
        self.meta = {}
    def handle_starttag(self, tag, attrs):
        if tag.lower() != "meta":
            return
        values = {k.lower(): v for k, v in attrs if k and v}
        key = (values.get("property") or values.get("name") or "").lower()
        value = values.get("content")
        if key and value:
            self.meta.setdefault(key, value.strip())

class ImageParser(HTMLParser):
    """Find a useful article image when a publisher omits Open Graph metadata."""
    def __init__(self):
        super().__init__()
        self.images = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "img":
            return
        values = {k.lower(): v for k, v in attrs if k and v}
        candidate = values.get("src") or values.get("data-src") or values.get("data-original")
        if candidate:
            self.images.append(candidate.strip())


class AnchorParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.href = None
        self.text_parts = []
        self.anchors = []
    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            self.href = dict(attrs).get("href")
            self.text_parts = []
    def handle_data(self, data):
        if self.href is not None:
            self.text_parts.append(data)
    def handle_endtag(self, tag):
        if tag.lower() == "a" and self.href is not None:
            title = " ".join(" ".join(self.text_parts).split())
            self.anchors.append((self.href, title))
            self.href = None
            self.text_parts = []

class FeedPublisher:
    def __init__(self, bot, cfg: AppConfig):
        self.bot, self.cfg = bot, cfg
        self.state_path = os.getenv("FEED_STATE_PATH", "/data/feed_state.json")
        self.state = self._load_state()
        self.session: aiohttp.ClientSession | None = None

    def _load_state(self):
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_state(self):
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.state_path)

    async def _get(self, url: str) -> str:
        assert self.session
        async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=25)) as response:
            response.raise_for_status()
            return await response.text()

    async def _wizard_items(self):
        html = await self._get(WIZARDS_URL)
        parser = AnchorParser()
        parser.feed(html)
        items = []
        seen = set()
        for href, title in parser.anchors:
            if not href or not title:
                continue
            url = urljoin(WIZARDS_URL, href).split("#", 1)[0]
            path = url.split("?", 1)[0]
            if "magic.wizards.com/en/news/announcements/" not in path:
                continue
            if path.rstrip("/") == WIZARDS_URL.rstrip("/") or path in seen:
                continue
            if not re.match(r"^https://magic\.wizards\.com/en/news/announcements/.+", path):
                continue
            seen.add(path)
            items.append({"id": path, "title": title, "url": path, "source": "Wizards of the Coast — Announcements"})
        # Archive pages can repeat article links; preserve site order and bound work.
        return items[:40]

    async def _enrich_item(self, item):
        """Fetch page metadata for a thumbnail and short summary when available."""
        try:
            html = await self._get(item["url"])
            page = html[:1_500_000]
            parser = MetaParser()
            parser.feed(page)
            image = (parser.meta.get("og:image") or parser.meta.get("og:image:secure_url") or
                     parser.meta.get("twitter:image") or parser.meta.get("twitter:image:src") or "").strip()
            if not image:
                image_parser = ImageParser()
                image_parser.feed(page)
                image = next((candidate for candidate in image_parser.images
                              if candidate.startswith(("https://", "http://"))), "")
            item["image"] = urljoin(item["url"], image) if image else ""
            item["summary"] = (parser.meta.get("og:description") or parser.meta.get("description") or "").strip()
            # Prefer the source's published page title only if the feed title is absent.
            item["title"] = item.get("title") or parser.meta.get("og:title") or "New MTG feed item"
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            log.debug("Could not retrieve preview metadata for %s", item.get("url"), exc_info=True)
            item.setdefault("image", "")
            item.setdefault("summary", "")
        return item

    async def _reddit_items(self):
        xml_text = await self._get(REDDIT_RSS)
        root = ET.fromstring(xml_text)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        items = []
        for entry in root.findall("atom:entry", ns):
            title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
            link_el = entry.find("atom:link", ns)
            url = link_el.attrib.get("href", "") if link_el is not None else ""
            entry_id = (entry.findtext("atom:id", default=url, namespaces=ns) or url).strip()
            if title and url:
                summary = (entry.findtext("atom:content", default="", namespaces=ns) or
                           entry.findtext("atom:summary", default="", namespaces=ns)).strip()
                # Reddit may expose a thumbnail as media metadata or inside its HTML content.
                image = ""
                for el in entry.iter():
                    if el.tag.rsplit("}", 1)[-1] in ("thumbnail", "content"):
                        candidate = el.attrib.get("url") or el.attrib.get("href")
                        if candidate and candidate.startswith("https://") and any(x in candidate.lower() for x in ("preview.redd.it", "i.redd.it", "external-preview.redd.it")):
                            image = candidate
                            break
                if not image and summary:
                    image_parser = ImageParser()
                    image_parser.feed(summary)
                    image = next((candidate for candidate in image_parser.images
                                  if candidate.startswith("https://") and any(host in candidate.lower()
                                  for host in ("preview.redd.it", "i.redd.it", "external-preview.redd.it"))), "")
                items.append({"id": entry_id, "title": title, "url": url, "source": "r/sealedmtgdeals", "summary": summary, "image": image})
        return items[:40]

    async def _channel(self, channel_id: int | None):
        if not channel_id:
            return None
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                log.exception("Could not access configured feed channel %s", channel_id)
                return None
        # Both text channels and forum channels are valid destinations. ForumChannel
        # exposes create_thread(), which creates a separate discussion post.
        if not hasattr(channel, "send") and not isinstance(channel, discord.ForumChannel):
            log.error("Configured feed destination %s cannot receive posts", channel_id)
            return None
        return channel

    async def _send_item(self, channel, item, embed):
        # Feed posts are intentionally silent: channel visibility is controlled
        # by Discord role permissions, so there is no need to ping either role.
        # Explicitly disable mentions so feed content can never trigger a ping.
        allowed = discord.AllowedMentions.none()
        if isinstance(channel, discord.ForumChannel):
            title = " ".join(item["title"].split())[:100] or "New MTG feed item"
            await channel.create_thread(
                name=title,
                embeds=[embed],
                allowed_mentions=allowed,
            )
        else:
            await channel.send(embed=embed, allowed_mentions=allowed)

    async def _publish_source(self, source_key, items, channel, color):
        ids = [item["id"] for item in items]
        old = set(self.state.get(source_key, []))
        if source_key not in self.state:
            # First launch establishes a baseline instead of flooding the channel with old posts.
            self.state[source_key] = ids
            self._save_state()
            log.info("Initialized %s feed baseline with %d existing items", source_key, len(ids))
            return
        new_items = [item for item in items if item["id"] not in old]
        # Send oldest-first so a batch arrives in chronological order. Only mark
        # items as seen after Discord confirms that their post was created.
        successfully_published = set()
        for item in reversed(new_items):
            # Metadata is fetched only for newly discovered links, avoiding repeated page requests.
            if source_key == "wizards_announcements" or not item.get("image") or not item.get("summary"):
                item = await self._enrich_item(item)
            embed = discord.Embed(title=item["title"][:256], url=item["url"], color=color)
            summary = item.get("summary", "")
            # Feed descriptions may contain markup or be long; keep embeds readable.
            summary = re.sub(r"<[^>]+>", " ", summary)
            summary = " ".join(summary.split())
            if len(summary) > 700:
                summary = summary[:697].rstrip() + "..."
            source_line = f"**Source:** {item['source']}"
            embed.description = (summary + "\n\n" if summary else "") + source_line
            embed.set_author(name="The Spellbook Clerk", icon_url="https://magic.wizards.com/favicon.ico")
            embed.set_footer(text=f"Tap Lands to Attack • Source: {item['source']}")
            image_url = item.get("image", "")
            if image_url.startswith("https://"):
                # Keep previews as compact right-side thumbnails so the title and
                # summary remain easy to scan in a busy feed channel.
                embed.set_thumbnail(url=image_url)
            try:
                await self._send_item(channel, item, embed)
                successfully_published.add(item["id"])
                log.info("Published %s feed item: %s", source_key, item["url"])
                await asyncio.sleep(0.35)
            except (discord.Forbidden, discord.HTTPException):
                log.exception("Failed publishing feed item %s; it will be retried on the next poll", item["url"])
                break
        retained_ids = [item_id for item_id in ids if item_id in old or item_id in successfully_published]
        self.state[source_key] = list(dict.fromkeys(retained_ids + list(old)))[:300]
        self._save_state()

    async def poll_once(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/atom+xml,application/rss+xml,application/xml"})
        destinations = (
            ("wizards_announcements", self._wizard_items, self.cfg.news_channel_id, discord.Color.blue()),
            ("sealedmtgdeals", self._reddit_items, self.cfg.deals_channel_id, discord.Color.gold()),
        )
        for key, fetcher, channel_id, color in destinations:
            if not channel_id:
                continue
            channel = await self._channel(channel_id)
            if channel is None:
                continue
            try:
                items = await fetcher()
                await self._publish_source(key, items, channel, color)
            except (aiohttp.ClientError, asyncio.TimeoutError, ET.ParseError, ValueError):
                log.exception("Feed poll failed for %s", key)
            except Exception:
                # Keep one malformed item or unexpected publishing error from
                # permanently killing the background feed task.
                log.exception("Unexpected error while processing %s feed; the next poll will retry", key)

    async def run(self):
        log.info("Feed publisher started; news channel ID=%s, deals channel ID=%s", self.cfg.news_channel_id, self.cfg.deals_channel_id)
        try:
            while not self.bot.is_closed():
                await self.poll_once()
                await asyncio.sleep(self.cfg.feed_poll_minutes * 60)
        finally:
            if self.session and not self.session.closed:
                await self.session.close()
