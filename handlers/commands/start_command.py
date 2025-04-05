import asyncio

from aiogram.enums import ChatAction
from aiogram.types import Message, ReactionTypeEmoji

import handlers.commands.settings_command as settings
from main import bot
from utils import log_command
from config import config

async def start_command(message: Message):
    await log_command(message)
    if message.from_user.id != message.chat.id:
        await message.react([ReactionTypeEmoji(emoji="👎")])
        return
    await message.answer("👋")
    if message.from_user.id not in settings.pending_sets.keys():
        await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        await asyncio.sleep(2)
        text = config.messages['info']['welcome']
        await message.answer(text, disable_web_page_preview=True)
    else:
        await settings.handle_private_setting(message)
