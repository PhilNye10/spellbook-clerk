from __future__ import annotations
import logging, os
from dotenv import load_dotenv
from .bot import TableBot, TablesGroup
from .config import load_config
from .db import Database
from .sesh import SeshClient
from .web import WebhookServer

async def run():
    load_dotenv()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(os.getenv("CONFIG_PATH", "config.yaml"))
    db = Database(os.getenv("DATABASE_PATH", "./data/mtg_tables.db"))
    sesh_token = os.getenv("SESH_API_TOKEN", "").strip()
    bot = TableBot(cfg, db, SeshClient(sesh_token) if sesh_token else None)
    bot.tree.add_command(TablesGroup(bot))
    webhook = WebhookServer(bot, os.getenv("WEBHOOK_HOST", "0.0.0.0"), int(os.getenv("WEBHOOK_PORT", "8080")), os.getenv("SESH_WEBHOOK_SECRET"))
    await webhook.start()
    try:
        await bot.start(os.environ["DISCORD_TOKEN"])
    finally:
        await webhook.stop()
        if bot.sesh:
            await bot.sesh.close()

if __name__ == "__main__":
    import asyncio
    asyncio.run(run())
