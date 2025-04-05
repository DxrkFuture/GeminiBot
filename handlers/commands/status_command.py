import datetime
import random

from aiogram.types import Message

import api.google
import api.openai
import db
from main import start_time
from utils import log_command
from config import config


def format_timedelta(delta: datetime.timedelta) -> str:
    total_seconds = int(delta.total_seconds())
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    parts = []
    
    if days > 0:
        parts.append(config.messages['status']['format_timedelta_days'].format(days=days))
    if hours > 0:
        parts.append(config.messages['status']['format_timedelta_hours'].format(hours=hours))
    if minutes > 0:
        parts.append(config.messages['status']['format_timedelta_minutes'].format(minutes=minutes))
    if seconds > 0 or not parts:
        parts.append(config.messages['status']['format_timedelta_seconds'].format(seconds=seconds))
    
    return ', '.join(parts)


async def status_command(message: Message):
    if await db.is_blacklisted(message.from_user.id):
        await message.reply(config.messages['error']['black_list'])
        return
    if await db.is_blacklisted(message.chat.id):
        await message.reply(config.messages['error']['black_list_chat'])
        return

    await log_command(message)

    messages = await db.get_messages(message.chat.id)

    messages_limit = await db.get_chat_parameter(message.chat.id, "message_limit")
    endpoint = await db.get_chat_parameter(message.chat.id, "endpoint")
    model = await db.get_chat_parameter(message.chat.id, endpoint[0] + "_model")
    rate_limit = await db.get_chat_parameter(message.chat.id, "max_requests_per_hour")

    request_count = await db.get_request_count(message.chat.id, datetime.timedelta(hours=1))
    uptime = datetime.datetime.now() - start_time

    status_msg = config.messages['status']['messages']
    token_count_text = status_msg['tokens_loading'] if endpoint == "google" else f"{await api.openai.count_tokens(message.chat.id)}{status_msg['tokens_suffix']}" # TODO правильное получение названия модели и токенов при использовании OpenWebUI API
    quota_text = status_msg['unlimited'] if rate_limit == 0 else f"{request_count}/{rate_limit}"
    
    if request_count >= rate_limit * 0.8 > 0:
        quota_text += f" {status_msg['warning_emoji']}"

    text_to_send = f"""{status_msg['title']}

{status_msg['memory'].format(
    messages_count=len(messages),
    messages_limit=messages_limit,
    token_count=token_count_text
)}
{status_msg['model'].format(model_name=model)}
{status_msg['rate_limit'].format(quota=quota_text)}

{status_msg['chat_id'].format(chat_id=message.chat.id)}
{status_msg['uptime'].format(uptime=format_timedelta(uptime))}
"""
    
    if random.randint(1, 6) == 3 or request_count >= rate_limit > 0:
        text_to_send += f"\n{status_msg['feedback']}"
    reply = await message.reply(text_to_send)

    if endpoint == "google":
        token_count_text = await api.google.count_tokens_for_chat(message)
        text_to_send = text_to_send.replace(status_msg['tokens_loading'], f"{token_count_text} {status_msg['tokens_suffix']}")
        await reply.edit_text(text_to_send)
