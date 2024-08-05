from aiogram.types import Message

import api.google
import api.openai
import db


async def generate_response(message: Message) -> str:
    endpoint = await db.get_chat_parameter(message.chat.id, "endpoint")
    if endpoint == "google":
        return await api.google.generate_response(message)
    elif endpoint == "openai":
        out = await api.openai.generate_response(message)
        if out.startswith("❌") and db.get_chat_parameter(message.chat.id, "o_auto_fallback"):
            await message.reply(f"⚠️ <b>Эндпоинт OpenAI дал сбой, запрос был направлен в Gemini API.</b>")
            out = await api.google.generate_response(message)
        return out
    else:
        raise ValueError("what.")
