from __future__ import annotations
import hashlib, hmac, json
from aiohttp import web

class WebhookServer:
    def __init__(self, bot, host: str, port: int, secret: str | None):
        self.bot, self.host, self.port, self.secret = bot, host, port, secret
        self.runner = None

    def _valid(self, raw: bytes, header: str | None):
        if not self.secret:
            return False
        if not header or not header.startswith("sha256="):
            return False
        expected = hmac.new(self.secret.encode(), raw, hashlib.sha256).hexdigest()
        return hmac.compare_digest(header[7:], expected)

    async def handle(self, request: web.Request):
        raw = await request.read()
        if not self._valid(raw, request.headers.get("X-Sesh-Signature")):
            return web.Response(status=401, text="Invalid signature")
        try:
            payload = json.loads(raw)
            if not await self.bot.db.mark_delivery_once(str(payload.get("id", ""))):
                return web.Response(text="Already processed")
            await self.bot.handle_sesh_webhook(payload)
            return web.Response(text="OK")
        except Exception as exc:
            self.bot.logger.exception("Webhook processing failed: %s", exc)
            return web.Response(status=500, text="Webhook processing failed")

    async def start(self):
        app = web.Application()
        app.router.add_post("/webhooks/sesh", self.handle)
        app.router.add_get("/health", lambda _: web.json_response({"ok": True}))
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, self.host, self.port)
        await site.start()

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()
