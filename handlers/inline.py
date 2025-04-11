from aiogram import Router, F
from aiogram.types import (
    InlineQuery, 
    InlineQueryResultArticle, 
    InputTextMessageContent,
    InlineKeyboardMarkup, 
    InlineKeyboardButton,
    CallbackQuery,
    Message,
    Chat,
    User
)
from aiogram.enums import ParseMode
import asyncio
import logging
import hashlib
from datetime import datetime
import api
import db

router = Router()

# Константы
MAX_QUERY_LENGTH = 256
WARNING_THRESHOLD = 0.8
query_cache = {}

async def handle_inline_generation(query_text: str, user_id: int) -> str:
    endpoint = await db.get_chat_parameter(user_id, "endpoint") or "openai"
    
    if endpoint == "openai":
        return await api.openai.generate_inline_response(query_text, user_id)
    elif endpoint == "google":
        return await api.google.generate_inline_response(query_text, user_id)
    elif endpoint == "openwebui":
        return await api.openwebui.generate_inline_response(query_text, user_id)

def escape_markdown(text: str) -> str:
    for char in ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']:
        text = text.replace(char, f'\\{char}')
    return text

@router.inline_query()
async def handle_inline_query(inline_query: InlineQuery):
    query_text = inline_query.query or "Нет запроса"
    query_length = len(query_text)
    
    if query_length > MAX_QUERY_LENGTH:
        result = InlineQueryResultArticle(
            id="error_too_long",
            title="⚠️ Запрос слишком длинный!",
            description=f"Максимальная длина: {MAX_QUERY_LENGTH}. У вас: {query_length}",
            input_message_content=InputTextMessageContent(
                message_text=f"⚠️ Максимальная длина: {MAX_QUERY_LENGTH}."
            )
        )
        await inline_query.answer(results=[result], cache_time=1)
        return
    
    query_id = hashlib.md5(query_text.encode()).hexdigest()
    query_cache[query_id] = query_text
    safe_query_text = escape_markdown(query_text)
    
    message_text = f"💬: {safe_query_text}\n\n🤖: **Нажмите кнопку для ответа**"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Сгенерировать ответ", callback_data=f"gen:{query_id}")]
    ])
    
    warning_length = int(MAX_QUERY_LENGTH * WARNING_THRESHOLD)
    if query_length > warning_length:
        description = f"⚠️ {query_length}/{MAX_QUERY_LENGTH} ({int(query_length/MAX_QUERY_LENGTH*100)}%)"
    else:
        description = f"{query_length}/{MAX_QUERY_LENGTH} - {query_text[:50]}{'...' if len(query_text) > 50 else ''}"
    
    result = InlineQueryResultArticle(
        id=query_id,
        title="Ответ ИИ",
        description=description,
        input_message_content=InputTextMessageContent(
            message_text=message_text,
            parse_mode=ParseMode.MARKDOWN_V2
        ),
        reply_markup=keyboard
    )
    
    await inline_query.answer(results=[result], cache_time=1)

@router.callback_query(F.data.startswith("gen:"))
async def generate_ai_response(callback_query: CallbackQuery):
    bot = callback_query.bot  # Используем бота из контекста
    _, query_id = callback_query.data.split(":", 1)
    await callback_query.answer("Генерирую ответ...")
    
    query_text = query_cache.get(query_id)
    if not query_text:
        await bot.edit_message_text(
            text="❌ Ошибка: запрос устарел",
            inline_message_id=callback_query.inline_message_id
        )
        return
    
    ai_response = await handle_inline_generation(
        query_text=query_text,
        user_id=callback_query.from_user.id  # Используем ID пользователя вместо чата
    )
    safe_query_text = escape_markdown(query_text)
    safe_ai_response = escape_markdown(ai_response)
    
    new_message_text = f"💬: {safe_query_text}\n\n🤖: {safe_ai_response}"
    
    try:
        await bot.edit_message_text(
            text=new_message_text,
            inline_message_id=callback_query.inline_message_id,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=None
        )
    except Exception as e:
        logging.error(f"Ошибка: {e}")
        await bot.edit_message_text(
            text=f"💬: {query_text}\n\n🤖: {ai_response}",
            inline_message_id=callback_query.inline_message_id,
            reply_markup=None
        )
    
    if query_id in query_cache:
        del query_cache[query_id]