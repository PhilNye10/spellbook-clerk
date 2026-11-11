# The Spellbook Clerk — Ubuntu Server Install

This version is **Sesh-Pro optional**. The normal installation uses Sesh event messages as the attendee source, so there is no Sesh API token or webhook required.

## 1. Prerequisites

Ubuntu Server 22.04 or 24.04, a Discord bot token, and Docker Engine + Compose plugin.

Install Docker from Docker's official Ubuntu repository if needed.

## 2. Install the bot

Upload the ZIP to `/tmp/mtg-table-bot.zip`, then run:

```bash
sudo mkdir -p /opt/mtg-table-bot
sudo unzip -o /tmp/mtg-table-bot.zip -d /opt/mtg-table-bot/_unpack
sudo cp -a /opt/mtg-table-bot/_unpack/mtg-table-bot/. /opt/mtg-table-bot/
sudo rm -rf /opt/mtg-table-bot/_unpack
sudo mkdir -p /opt/mtg-table-bot/data
sudo chown -R "$USER":"$USER" /opt/mtg-table-bot
cd /opt/mtg-table-bot
cp .env.example .env
cp config.example.yaml config.yaml
chmod 600 .env
```

## 3. Configure Discord

Edit `.env`:

```bash
nano /opt/mtg-table-bot/.env
```

Set:

```dotenv
DISCORD_TOKEN=YOUR_DISCORD_BOT_TOKEN
DISCORD_GUILD_ID=YOUR_DISCORD_SERVER_ID
```

Leave `SESH_API_TOKEN` and `SESH_WEBHOOK_SECRET` blank unless you have Sesh Pro.

The bot needs the `bot` and `applications.commands` OAuth scopes. Enable the **Sesh message fallback** gateway intent in the Discord Developer Portal.

## 4. Start

```bash
cd /opt/mtg-table-bot
docker compose up -d --build
docker logs -f mtg-table-bot
```

## 5. Configure feed destinations (both are Forum Channels)

In Discord, enable **Developer Mode** in User Settings → Advanced. Copy the channel ID for each forum channel by right-clicking it on desktop or long-pressing it on mobile.

Edit `/opt/mtg-table-bot/config.yaml`:

```bash
nano /opt/mtg-table-bot/config.yaml
```

Set the feed section to your actual forum channel IDs:

```yaml
feeds:
  news_channel_id: YOUR_MTG_NEWS_FORUM_ID
  deals_channel_id: YOUR_MTG_DEALS_FORUM_ID
  poll_minutes: 10
  auto_delete_days: 3
  cleanup_poll_minutes: 60
```

Keep the rest of the existing configuration intact, including any Sesh schedule channel ID. Both configured feed destinations are Forum Channels: each new Magic announcement becomes a separate post in `#mtg-news`, and each new r/sealedmtgdeals item becomes a separate post in `#mtg-deals`.

The bot needs **View Channel**, **Send Messages**, and permission to create forum posts/threads in both forums. It also needs **Manage Threads** in `#mtg-deals` so it can delete posts after 3 days without activity; any reply resets that timer. Cleanup checks hourly and only targets the configured deals forum, not the news forum. If your forum requires tags, either make tags optional or modify the bot to apply the required tag(s).

## 6. Start and verify

```bash
cd /opt/mtg-table-bot
docker compose up -d --build
docker compose ps
docker logs -f mtg-table-bot
```

In Discord, run `/tables create` to test the table/RSVP workflow. Feed checks run automatically every 10 minutes. On a first run with no saved feed history, the bot records the current items as a baseline and avoids flooding the forums with older items; new items discovered on later checks are posted automatically.

The web hook listener, if used, remains on container port 8080. Docker publishes it on **host port 8484**, so access it at `http://YOUR_SERVER_IP:8484`. The port mapping is `8484:8080` (host:container); it does not require changing `WEBHOOK_PORT` in `.env`.

## 8. Updates

```bash
cd /opt/mtg-table-bot
docker compose down
# Replace the application files with the new release while preserving .env and data/
docker compose up -d --build
```

## 9. Useful commands

```bash
cd /opt/mtg-table-bot
docker compose ps
docker logs -f mtg-table-bot
docker compose restart
docker compose down
```

## Version 13 features

- Run `/spellbook` for the private command hub with clickable slash-command references.
- `/commander record` lets an organizer select a saved Commander event and pod, enter each player's commander, and select the winner.
- `/commander leaderboard`, `/commander player`, `/commander commanders`, and `/commander history` show lifetime Commander statistics.
- `/commander delete game_id` removes an incorrectly recorded Commander game.
- `/draft leaderboard`, `/draft player`, and `/draft history` show tournament and match statistics. Bracket winners are recorded automatically; byes are excluded from match win/loss totals.
- Existing brackets are imported into the new Draft history tables during database initialization when their data is still present.

The update adds database tables without deleting existing tables or feed history. Keep the existing `.env`, `config.yaml`, and entire `data/` directory when upgrading.

### Updating to v13

Upload the v13 ZIP to `/tmp` on the Ubuntu host, then run:

```bash
cd /opt/mtg-table-bot
BACKUP="$HOME/spellbook-clerk-backup-$(date +%F-%H%M%S).tar.gz"
sudo tar -czf "$BACKUP" .env config.yaml data docker-compose.yml
sudo chown "$USER":"$(id -gn)" "$BACKUP"

unzip -t /tmp/spellbook-clerk-mtg-discord-bot-v13-command-hub-stats.zip
mkdir -p /tmp/spellbook-v13
unzip -o /tmp/spellbook-clerk-mtg-discord-bot-v13-command-hub-stats.zip -d /tmp/spellbook-v13

docker compose down
cp -a /tmp/spellbook-v13/mtg-table-bot/mtg_table_bot .
cp -a /tmp/spellbook-v13/mtg-table-bot/Dockerfile .
cp -a /tmp/spellbook-v13/mtg-table-bot/requirements.txt .
cp -a /tmp/spellbook-v13/mtg-table-bot/docker-compose.yml .
docker compose up -d --build
docker compose logs -f
```

Do not replace `.env`, `config.yaml`, or `data/` with the example files from the archive. Do not use `docker compose down -v`. Once the bot is online, run `/spellbook` and verify the Commander and Draft sections appear. Existing draft brackets will be backfilled when the bot initializes its database.


## Version 13.1 feed hotfix

This hotfix fixes feed publishing by passing the correct opt-in role name into the publisher. It also records feed IDs as seen only after Discord successfully creates the post, so failed sends are retried on the next poll. Unexpected per-feed errors are logged without terminating the background feed loop.

To update from v13, preserve `.env`, `config.yaml`, and `data/` exactly as before. Replace the application files with this archive and rebuild the container. The existing `data/feed_state.json` is retained.


### Bot presence/status

The bot now displays `Currently calculating lethal | /spellbook • /tables` as its Playing status.
