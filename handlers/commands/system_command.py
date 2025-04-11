from aiogram.types import Message, ReactionTypeEmoji

import db
from handlers.commands.shared import is_allowed_to_alter_memory
from utils import get_message_text, log_command
from config import config

async def system_command(message: Message) -> None:
    await log_command(message)
    if not await is_allowed_to_alter_memory(message):
        await message.reply(config.messages['error']['access'])
        return

    text = await get_message_text(message)
    try:
        text = text.split(" ", maxsplit=1)[1]
    except IndexError:
        await message.reply(config.messages['error']['help_command']['system'])
        return

    await db.save_system_message(message.chat.id, text)
    await message.react([ReactionTypeEmoji(emoji="👌")])
