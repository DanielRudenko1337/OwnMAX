import asyncio
from ownmax.integrations.telegram.bot import TelegramBot
from ownmax.core.controllerbase import ControllerBase
from ownmax.infrastructure.config import ServerConfig


class TelegramBotController(ControllerBase):

    def __init__(self):
        self.config = ServerConfig()
        self.bot = None

    def launch(self, api):

        async def _start_all():
            await asyncio.gather(self.bot.start())

        self.bot = TelegramBot(
            token=self.config.telegram_bot_token,
            enabled=self.config.telegram_bot_enabled,
            db_pool=api["db"],
            whitelist_ids=self.config.telegram_whitelist_ids,
            whitelist_enabled=self.config.telegram_whitelist_enabled,
        )
        return _start_all()

    async def send_code(self, chat_id, phone, code):
        await self.bot.send_auth_code(chat_id, phone, code)
