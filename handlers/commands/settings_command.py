import asyncio
import difflib
import os
import traceback
from decimal import Decimal, ROUND_HALF_UP

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, Message, ReactionTypeEmoji
from aiogram.utils.keyboard import InlineKeyboardBuilder
from loguru import logger

import db
from main import ADMIN_IDS, bot
from utils import get_message_text, log_command
from utils.definitions import chat_configs
from utils.frange import FloatRange
from config import config

pending_sets = {
    # user_id: [target_chat_id, target_param, has_notified, notif_message_id]
}


def obfuscate_string(s: str) -> str:
    if s == "None" or s is None:
        return str(s)

    prefix = ""
    if s.startswith("https://"):
        prefix = "https://"
        s = s[len("https://"):]  # Remove the prefix for obfuscation

    if len(s) <= 10:
        obfuscated = '*' * len(s)
    else:
        obfuscated = s[:4] + '*' * (len(s) - 8) + s[-4:]

    return prefix + obfuscated


async def _get_current_value(chat_id: int, param: str, available_parameters: dict):
    value = await db.get_chat_parameter(chat_id, param)
    if isinstance(value, Decimal):
        value = value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    if available_parameters[param]["private"] and isinstance(value, str):
        value = obfuscate_string(value)

    return value


async def _get_available_parameters(chat_id: int):
    chat_endpoint = await db.get_chat_parameter(chat_id, "endpoint")
    try:
        available_parameters = chat_configs["all_endpoints"] | chat_configs[chat_endpoint]
    except KeyError:
        available_parameters = chat_configs["all_endpoints"]
    return available_parameters


async def _parse_requested_value(param_type, requested_value):
    requested_value_parsed = None
    if param_type == "integer":
        try:
            requested_value_parsed = int(requested_value)
        except ValueError:
            pass
    elif param_type == "decimal":
        try:
            requested_value_parsed = float(requested_value)
        except ValueError:
            pass
    elif param_type == "boolean":
        if requested_value.lower() in ["true", "1"]:
            requested_value_parsed = True
        elif requested_value.lower() in ["false", "0"]:
            requested_value_parsed = False
    else:
        requested_value_parsed = requested_value
    return requested_value_parsed


async def settings_command(message: Message) -> None:
    await log_command(message)
    init_text = await get_message_text(message)

    command = init_text.split(" ")
    chat_id = message.chat.id
    show_advanced = await db.get_chat_parameter(chat_id, "show_advanced_settings")
    available_parameters = await _get_available_parameters(chat_id)

    if len(command) == 1:
        text = config.messages['info']['command']['settings']
        have_separated = False

        for parameter in available_parameters:
            if available_parameters[parameter]["advanced"] and not show_advanced:
                continue

            if parameter not in chat_configs["all_endpoints"] and not have_separated:
                text += "\n"
                have_separated = True

            current_value = await _get_current_value(chat_id, parameter, available_parameters)
            text += config.messages['info']['command']['settings_parameter'].format(parameter=parameter, value=current_value)

        text += config.messages['info']['command']['settings_footer']
        await message.reply(text)
    else:
        requested_parameter = command[1].lower()

        if requested_parameter not in available_parameters:
            await message.reply(config.messages['error']['help_command']['unknown'])
            return

        current_value = await _get_current_value(chat_id, requested_parameter, available_parameters)
        default_value = available_parameters[requested_parameter]['default_value']
        if isinstance(default_value, str):
            default_value = default_value.replace("'", "")

        text = config.messages['info']['command']['settings_detail'].format(
            parameter=requested_parameter,
            description=available_parameters[requested_parameter]['description'],
            current_value=current_value,
            default_value=default_value if default_value != "None" else "-"
        )

        _value_range = available_parameters[requested_parameter]['accepted_values']
        if isinstance(_value_range, range):
            accepted_values = f"{_value_range.start}-{_value_range.stop}"
        elif isinstance(_value_range, FloatRange):
            accepted_values = f"{_value_range[0]}—{_value_range[-1]}, шаг {round(_value_range[1] - _value_range[0], 2)}"
        elif callable(_value_range):
            accepted_values = await _value_range(message)
            if not accepted_values:
                _value_range = None
            elif isinstance(accepted_values, list):
                accepted_values = ", ".join(accepted_values)
        else:
            try:
                accepted_values = ", ".join(_value_range)
            except TypeError:
                accepted_values = "True, False"

        if _value_range:
            text += config.messages['info']['command']['settings_accepted_values'].format(accepted_values=accepted_values)

        if available_parameters[requested_parameter]["protected"]:
            text += config.messages['info']['command']['settings_protected']

        if len(command) > 2:
            text += config.messages['info']['command']['settings_extra_flags'].format(
                set_command=init_text.replace('/settings', '/set', 1)
            )

        await message.reply(text, disable_web_page_preview=True)
        

async def set_command(message: Message) -> None:
    global pending_sets

    await log_command(message)

    is_admin = message.from_user.id in ADMIN_IDS
    command_parts = message.text.strip().split(" ")

    command_args = command_parts[1:]

    if not command_args:
        await message.reply("❌ <b>Недостаточно аргументов.</b>")
        return

    target_chat_id = message.chat.id

    if is_admin:
        # Admin can use /set [chat_id] [param-name] [value] or /set [param-name] [value]
        if len(command_args) == 3:
            # /set [chat_id] [param-name] [value]
            try:
                target_chat_id = int(command_args[0])
                requested_parameter = command_args[1].lower()
                requested_value = command_args[2]
            except ValueError:
                await message.reply("❌ <b>Неверный идентификатор чата.</b>")
                return
        elif len(command_args) == 2:
            # /set [param-name] [value]
            requested_parameter = command_args[0].lower()
            requested_value = command_args[1]
        elif len(command_args) == 1:
            # /set [param-name]
            requested_parameter = command_args[0]
            requested_value = None
        else:
            await message.reply("❌ <b>Недостаточно аргументов.</b>")
            return
    else:
        # Non-admin users
        requested_parameter = command_args[0].lower()
        requested_value = command_args[1]

    # Permission checks
    if not is_admin:
        if target_chat_id != message.chat.id:
            await message.reply("❌ <b>Вы не можете изменять настройки другого чата.</b>")
            return

        if message.chat.id != message.from_user.id:
            # Check if the user is an admin in the chat
            try:
                member = await bot.get_chat_member(message.chat.id, message.from_user.id)
                if member.status not in ["administrator", "creator"]:
                    await message.reply("❌ <b>Параметры могут менять только администраторы.</b>")
                    return
            except TelegramForbiddenError:
                await message.reply("❌ <b>Не удалось получить информацию о вас в этом чате.</b>")
                return
    else:
        pass  # Admins can modify any chat's parameters

    # Get the available parameters for the target chat
    available_parameters = await _get_available_parameters(target_chat_id)

    if requested_parameter not in available_parameters:
        # Try to find parameters that start with the requested_parameter
        matching_parameters = [param for param in available_parameters if param.startswith(requested_parameter)]

        if len(matching_parameters) == 1:
            # Exactly one parameter matches the prefix, use it
            requested_parameter = matching_parameters[0]
        elif len(matching_parameters) > 1:
            # Multiple parameters match the prefix, inform the user
            await message.reply(
                f"❌ <b>Неизвестный параметр.</b> Найдено несколько параметров, начинающихся с "
                f"<code>{requested_parameter}</code>:\n" +
                "\n".join(f"• <code>{param}</code>" for param in matching_parameters)
            )
            return
        else:
            await message.reply("❌ <b>Неизвестный параметр.</b>")
            return

    # Check if the parameter is protected
    if available_parameters[requested_parameter]["protected"]:
        if message.from_user.id not in ADMIN_IDS:
            await message.reply("❌ <b>У вас нет доступа к этому параметру. Свяжитесь с администратором бота, "
                                "если хотите его изменить.</b>")
            return

    # Get target parameter's accepted value type
    accepted_values = available_parameters[requested_parameter]['accepted_values']
    param_type = available_parameters[requested_parameter]["type"]

    if not accepted_values and isinstance(requested_value, str) and requested_value.lower() in ["none", "null", "0"]:
        await db.set_chat_parameter(target_chat_id, requested_parameter, None)
        await message.reply("✅ <b>Параметр сброшен.</b>")
        return

    # Check if it's private
    if available_parameters[requested_parameter]["private"] and message.from_user.id != message.chat.id:
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(
            text="Открыть диалог",
            url=f"https://t.me/{os.getenv('BOT_USERNAME')}")
        )
        notif_message = await message.reply("👋 <b>Давайте перейдём в личные сообщения, чтобы установить этот "
                                            "параметр, не раскрывая его другим.</b>",
                                            reply_markup=builder.as_markup())

        pending_sets[message.from_user.id] = [
            target_chat_id,
            requested_parameter,
            False,
            notif_message.message_id
        ]

        await asyncio.sleep(1)
        try:
            await handle_private_setting(message)
        except TelegramForbiddenError:
            logger.warning("Tried to send a private setting request but failed, waiting for a /start...")
        return

    else:
        if (not is_admin and len(command_args) < 2) or not requested_value:
            await message.reply("❌ <b>Недостаточно аргументов.</b>")
            return

    # Attempt to parse the requested value according to its type
    requested_value_parsed = await _parse_requested_value(param_type, requested_value)

    if requested_value_parsed is None:
        await message.reply(f"❌ <b>Недопустимое значение для параметра '{requested_parameter}'.</b>")
        return

    # Validate target value
    if accepted_values:
        if isinstance(accepted_values, range):
            accepted_values = range(accepted_values.start, accepted_values.stop + 1)
        elif callable(accepted_values):
            accepted_values = await accepted_values(message)
            if not accepted_values:
                _value_range = None
        if requested_value_parsed not in accepted_values:
            if param_type == "text":
                # Try to find accepted values that start with the requested_value
                matching_values = [value for value in accepted_values if value.startswith(str(requested_value_parsed))]

                if len(matching_values) == 1:
                    # Exactly one value matches the prefix, use it
                    requested_value_parsed = matching_values[0]
                elif len(matching_values) > 1:
                    # Multiple values match the prefix, inform the user
                    reply_text = (
                            "❌ <b>Недопустимое значение для параметра.</b> "
                            f"Найдено несколько значений, начинающихся с <code>{requested_value_parsed}</code>:\n" +
                            "\n".join(f"• <code>{value}</code>" for value in matching_values)
                    )
                    await message.reply(reply_text)
                    return
                else:
                    # No matches, suggest the closest match
                    if message.from_user.id not in ADMIN_IDS:
                        reply_text = "❌ <b>Недопустимое значение для параметра.</b> "
                        guess = difflib.get_close_matches(
                            str(requested_value_parsed),
                            accepted_values,
                            1,
                            0.4
                        )
                        if guess:
                            reply_text += f"Может быть, вы имели в виду <code>{guess[0]}</code>?"
                        await message.reply(reply_text)
                        return
                    else:
                        await message.reply(
                            "⚠️ <b>Значение параметра вне списка разрешённых, но так как вы - администратор "
                            "бота, оно всё равно будет установлено.</b>"
                        )
            else:
                # Non-text type or no accepted values
                if message.from_user.id not in ADMIN_IDS:
                    await message.reply("❌ <b>Недопустимое значение для параметра.</b>")
                    return
                else:
                    await message.reply(
                        "⚠️ <b>Значение параметра вне списка разрешённых, но так как вы - администратор "
                        "бота, оно всё равно будет установлено.</b>"
                    )

    if not accepted_values and requested_value.lower() == "null":
        requested_value_parsed = None

    # Set the parameter
    try:
        await db.set_chat_parameter(target_chat_id, requested_parameter, requested_value_parsed)
        await message.react([ReactionTypeEmoji(emoji="👌")])
    except Exception as e:
        logger.error(e)
        traceback.print_exc()
        await message.reply("❌ <b>Непредвиденная ошибка при установке параметра.</b>")
        return


async def handle_private_setting(message: Message):
    global pending_sets

    if message.from_user.id not in pending_sets:
        logger.warning(f"Was just called but the ID ({message.from_user.id}) is not in the pending list.")
        await message.reply("❌ <b>Произошёл непредвиденный сбой.</b>")
        return

    pending_set = pending_sets[message.from_user.id]

    if not pending_set[2]:
        await bot.send_message(message.from_user.id,
                               f"<b>Пожалуйста, отправьте сюда новое значение параметра <code>{pending_set[1]}</code"
                               f">. Оно будет установлено в чате с идентификатором {pending_set[0]}</b>\n<i>(его "
                               f"можно проверить командой /status в целевом чате)</i>")
        pending_set[2] = True
        return

    requested_value = message.text

    if requested_value.lower() in ["null", "none"]:
        requested_value = None

    # Get the available parameters for the target chat
    available_parameters = await _get_available_parameters(pending_set[0])

    if pending_set[1] not in available_parameters:
        await message.reply("❌ <b>Неизвестный параметр.</b>")
        pending_sets.pop(message.from_user.id)
        return

    # Similar validation as in set_command
    accepted_values = available_parameters[pending_set[1]]['accepted_values']
    param_type = available_parameters[pending_set[1]]["type"]

    # Attempt to parse the requested value according to its type
    requested_value_parsed = await _parse_requested_value(param_type, requested_value)

    if requested_value_parsed is None:
        await message.reply(f"❌ <b>Недопустимое значение для параметра '{pending_set[1]}'.</b>")
        pending_sets.pop(message.from_user.id)
        return

    # Validate target value
    if accepted_values:
        if isinstance(accepted_values, range):
            accepted_values = range(accepted_values.start, accepted_values.stop + 1)
        if requested_value_parsed not in accepted_values:
            await message.reply("❌ <b>Недопустимое значение для параметра.</b>")
            pending_sets.pop(message.from_user.id)
            return

    # Set the parameter
    try:
        await db.set_chat_parameter(pending_set[0], pending_set[1], requested_value_parsed)
        await message.reply("✅ <b>Значение установлено!</b>")

        try:
            await bot.edit_message_text("✅ <b>Значение установлено!</b>", chat_id=pending_set[0],
                                        message_id=pending_set[3])
        except TelegramBadRequest:
            logger.warning(f"Failed to edit message {pending_set[3]} at {pending_set[0]}")

    except Exception as e:
        logger.error(e)
        traceback.print_exc()
        await message.reply("❌ <b>Непредвиденная ошибка при установке параметра.</b>")
        pending_sets.pop(message.from_user.id)
        return

    pending_sets.pop(message.from_user.id)
