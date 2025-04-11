import asyncio
import os
import random
import time
from typing import List, Optional
import traceback

import aiohttp
from aiogram.types import Message
from aiohttp_socks import ProxyConnector
from async_lru import alru_cache
from asyncpg import Record
from loguru import logger

import db
from api.google import format_message_for_prompt
from api.google.media import get_photo
from api.prompt import get_system_prompt
from utils import simulate_typing

# Environment variables
OPENAI_API_KEY = os.getenv("OAI_API_KEY")
PROXY_URL = os.getenv("PROXY_URL")
OAI_API_URL = os.getenv("OAI_API_URL")
OAI_ENABLED = os.getenv("OAI_ENABLED")


async def generate_inline_response(query_text: str, user_id: int) -> str:
    """Упрощенная версия для inline-запросов"""
    request_id = random.randint(100000, 999999)
    logger.info(f"INLINE R: {request_id} | U: {user_id}")

    try:
        # Получаем персональные настройки пользователя
        model = await db.get_chat_parameter(user_id, "owui_model") or "gpt-3.5-turbo"
        
        # Преобразуем Decimal в float
        temperature = float(await db.get_chat_parameter(user_id, "owui_temperature") or 0.7)
        
        url = await db.get_chat_parameter(user_id, "owui_url") or OAI_API_URL
        key = await db.get_chat_parameter(user_id, "owui_key") or OPENAI_API_KEY
        
        # Формируем минимальный промпт
        messages = [{"role": "user", "content": query_text}]
        
        # Добавляем системный промпт если нужно
        if await db.get_chat_parameter(user_id, "add_system_prompt"):
            system_prompt_content = await get_system_prompt()
            system_content = system_prompt_content.format(
                chat_type="inline query",
                chat_title=f"with user {user_id}"
            )
            messages.insert(0, {"role": "system", "content": system_content})

        # Отправка запроса
        async with aiohttp.ClientSession() as session:
            response = await session.post(
                f"{url.rstrip('/')}/api/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": model,
                    "messages": messages,
                    "temperature": temperature
                },
                timeout=15
            )
            data = await response.json()
            return data['choices'][0]['message']['content']
            
    except Exception as e:
        logger.error(f"OpenWebUI inline error: {str(e)}")
        return "❌ Ошибка генерации через OpenWebUI"


async def _send_request(
        messages_list: List[dict],
        url: str,
        key: str,
        model: str,
        request_id: int,
        temperature: float,
        top_p: float,
        frequency_penalty: float,
        presence_penalty: float,
        max_output_tokens: int,
        timeout: int,
        tool_ids: Optional[List[str]] = None,
) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
    }
    data = {
        "model": model,
        "messages": messages_list,
        "temperature": temperature,
        "top_p": top_p,
        "frequency_penalty": frequency_penalty,
        "presence_penalty": presence_penalty,
        "max_tokens": max_output_tokens,
    }

    if "o1" in model and "trycloudflare" not in url:
        data["max_completion_tokens"] = max_output_tokens
        del data["max_tokens"]

    if tool_ids:
        data["tool_ids"] = tool_ids

    logger.info(f"{request_id} | Sending request to {url}")

    connector = ProxyConnector.from_url(PROXY_URL) if PROXY_URL else None

    gen_start_time = time.perf_counter()

    async with aiohttp.ClientSession(connector=connector) as session:
        try:
            async with session.post(
                    f"{url}/api/chat/completions",
                    headers=headers,
                    json=data,
                    timeout=timeout,
            ) as response:
                response_decoded = await response.json()

                gen_end_time = time.perf_counter()
                gen_timedelta = gen_end_time - gen_start_time

                logger.info(f"{request_id} | Сomplete | {response.status} | {round(gen_timedelta, 2)}s")
                return response_decoded
        except Exception as e:
            logger.error("Failed to parse response to JSON.")
            logger.exception(e)
            raise


async def get_prompt(
        trigger_message: Message,
        messages_list: List[Record],
        system_prompt: bool,
        system_messages: bool,
) -> List[dict]:
    is_direct_message = trigger_message.from_user.id == trigger_message.chat.id
    chat_type = "direct message (DM)" if is_direct_message else "group"
    chat_title = (
        f" with {trigger_message.from_user.first_name}"
        if is_direct_message
        else f" called {trigger_message.chat.title}"
    )

    system_prompt_template = await get_system_prompt()
    add_reply_to = await db.get_chat_parameter(trigger_message.chat.id, "add_reply_to")

    final_prompt = []
    if system_prompt and system_messages:
        final_prompt.append(
            {
                "role": "system",
                "content": system_prompt_template.format(
                    chat_type=chat_type,
                    chat_title=chat_title,
                    examplefile='' # TODO
                ),
            }
        )

    last_role = None
    for message in messages_list:
        sender_id = message["sender_id"]

        if sender_id == 727:
            role = "system"
        elif sender_id == 0:
            role = "assistant"
        else:
            role = "user"

        formatted_message = await format_message_for_prompt(message, add_reply_to)

        if role == "system" and system_messages:
            formatted_message = formatted_message.replace("SYSTEM: ", "", 1)
        elif role == "assistant":
            formatted_message = await format_message_for_prompt(message, False)
            formatted_message = formatted_message.replace("You: ", "", 1)

        if last_role == role and final_prompt:
            final_prompt[-1]["content"] += f"\n{formatted_message}"
        else:
            final_prompt.append({"role": role, "content": formatted_message})
            last_role = role

    clarify_target_message = await db.get_chat_parameter(
        trigger_message.chat.id, "owui_clarify_target_message"
    )
    if system_prompt and system_messages and clarify_target_message:
        final_prompt.append(
            {
                "role": "system",
                "content": (
                    "That's it with the chat history. The next User message will be your TARGET message. "
                    "This is the message that triggered this request in the first place. Read it, "
                    "make sure to not confuse what it's asking or talking about while NOT CONFUSING ONGOING TOPICS "
                    "based on the current chat history, and respond to it."
                ),
            }
        )
        target_msg = await db.get_specific_message(
            trigger_message.chat.id, trigger_message.message_id
        )
        final_prompt.append(
            {
                "role": "user",
                "content": await format_message_for_prompt(target_msg, add_reply_to),
            }
        )

    vision_enabled = await db.get_chat_parameter(trigger_message.chat.id, "owui_vision")
    image = await get_photo(trigger_message, messages_list) if vision_enabled else None

    if image:
        last_message = final_prompt[-1]
        final_prompt[-1] = {
            "role": last_message["role"],
            "content": [
                {"type": "text", "text": last_message["content"]},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image}"}},
            ],
        }
        
    return final_prompt


async def generate_response(message: Message) -> str:
    request_id = random.randint(100000, 999999)
    user_id = message.from_user.id
    user_name = message.from_user.first_name
    chat_id = message.chat.id
    chat_title = message.chat.title
    message_id = message.message_id

    logger.info(
        f"R: {request_id} | U: {user_id} ({user_name}) | C: {chat_id} ({chat_title}) | M: {message_id}"
    )

    if not OAI_ENABLED or OAI_ENABLED.lower() != "true":
        logger.warning(
            f"{request_id} | OpenWebUI endpoint is disabled. Raising NotImplementedError."
        )
        raise NotImplementedError("OpenWebUI endpoint is disabled globally.")

    show_errors = await db.get_chat_parameter(chat_id, "show_error_messages")
    append_system_prompt = await db.get_chat_parameter(chat_id, "add_system_prompt")
    add_system_messages = await db.get_chat_parameter(chat_id, "add_system_messages")

    messages = await db.get_messages(chat_id)
    model = await db.get_chat_parameter(chat_id, "owui_model")
    log_prompt = await db.get_chat_parameter(chat_id, "owui_log_prompt")
    prompt = await get_prompt(message, messages, append_system_prompt, add_system_messages)
    owui_tools_ids_raw = await db.get_chat_parameter(chat_id, "owui_tools_ids")
    owui_tools_ids = (
        [tool.strip() for tool in owui_tools_ids_raw.split(",") if tool.strip()]
        if owui_tools_ids_raw else None
    )

    if log_prompt:
        logger.debug(prompt)

    logger.debug(f"{request_id} | Using model {model}")

    timeout = int(await db.get_chat_parameter(chat_id, "owui_timeout"))
    url = await db.get_chat_parameter(chat_id, "owui_url") or OAI_API_URL
    url = url.rstrip("/")

    key = await db.get_chat_parameter(chat_id, "owui_key") or OPENAI_API_KEY

    async with simulate_typing(message.chat.id):
        try:
            response = await _send_request(
                messages_list=prompt,
                url=url,
                key=key,
                model=model,
                request_id=request_id,
                temperature=float(await db.get_chat_parameter(chat_id, "owui_temperature")),
                top_p=float(await db.get_chat_parameter(chat_id, "owui_top_p")),
                frequency_penalty=float(
                    await db.get_chat_parameter(chat_id, "owui_frequency_penalty")
                ),
                presence_penalty=float(
                    await db.get_chat_parameter(chat_id, "owui_presence_penalty")
                ),
                max_output_tokens=int(
                    await db.get_chat_parameter(chat_id, "max_output_tokens")
                ),
                timeout=timeout,
                **({"tool_ids": owui_tools_ids} if owui_tools_ids else {})
            )
        except asyncio.TimeoutError:
            output = (
                f"❌ *Превышено время ожидания ответа от эндпоинта OpenWebUI.*\n"
                f"Нынешний таймаут: `{timeout}`"
            )
            return output
        except Exception as e:
            logger.exception(e)
            if show_errors:
                output = f"❌ *Произошёл неизвестный сбой:*\n`{str(e)}`"
            else:
                output = "❌ *Произошёл неизвестный сбой.*\n\nПожалуйста, попробуйте позже."
            return output

    try:
        if "error" in response:
            error_message = response["error"]["message"]
            if show_errors:
                output = f"❌ *Ошибка OpenWebUI:*\n {error_message}"
            else:
                output = "❌ *Произошёл неизвестный сбой.*\n\nПожалуйста, попробуйте позже."
            return output

        output = response["choices"][0]["message"]["content"]
        return output

    except Exception as e:
        logger.exception(e)
        if show_errors:
            output = f"❌ *Не удалось обработать ответ OpenWebUI:*\n`{str(e)}`"
        else:
            output = "❌ *Произошёл неизвестный сбой.*\n\nПожалуйста, попробуйте позже."
        return output


@alru_cache(ttl=300)
async def _get_available_models(url: str, key: str, get_all_models=False) -> List[str]:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
    }

    connector = ProxyConnector.from_url(PROXY_URL) if PROXY_URL else None

    async with aiohttp.ClientSession(connector=connector) as session:
        try:
            async with session.get(
                    f"{url}/api/models",
                    headers=headers,
            ) as response:
                response_decoded = await response.json()
                if get_all_models:
                    return [model["id"] for model in response_decoded["data"]]
                else:
                    return [
                        model["id"]
                        for model in response_decoded["data"]
                        if model["owned_by"] != "anthropic"
                    ]
        except Exception as e:
            logger.error("Failed to get available models.")
            logger.exception(e)
            return []


async def get_available_models(message: Message) -> List[str]:
    chat_id = message.chat.id
    url = await db.get_chat_parameter(chat_id, "owui_url") or OAI_API_URL
    url = url.rstrip("/")
    key = await db.get_chat_parameter(chat_id, "owui_key") or OPENAI_API_KEY

    return await _get_available_models(url, key)