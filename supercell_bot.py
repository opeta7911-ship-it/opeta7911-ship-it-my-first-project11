#!/usr/bin/env python3
"""Standalone Supercell Auto OTP Telegram bot."""
from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import os
import pickle
import random
import re
import secrets
import time
import urllib.parse
from contextlib import suppress

import httpx
from fake_useragent import UserAgent
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_ТОКЕН_СЮДА")
CACHE_FOLDER = "storage/cache/supercell_auto_otp/"
BIN_STATE_FILE = os.path.join(CACHE_FOLDER, "last_bin.json")

recaptcha_url = "https://www.recaptcha.net/recaptcha/api3/mrr"
sc_api_url = "https://id.supercell.com/api/account/v2/pinAuthentication.start"
validate_url = "https://id.supercell.com/api/account/v2/pinAuthentication.complete"

PHONE_MODELS = [
    "iPhone14,5", "iPhone14,7", "iPhone15,2", "iPhone15,4",
    "iPhone16,1", "iPhone16,2", "iPhone17,1", "iPhone17,3",
]
IOS_VERSIONS = ["17.4", "17.5", "17.6", "18.0", "18.1", "18.2"]


def shuffle(base, seed):
    size, numbers, x = len(base), list(range(len(base))), seed
    for i in range(size):
        j = (size - 1) - i
        x = (0x19660D * x + 0x3C6EF35F) & 0xFFFFFFFF
        k, v = x % (j + 1), numbers[j]
        numbers[j], numbers[k] = numbers[k], v
    offsets = [0] * size
    for i in range(size):
        offsets[numbers[i]] = i
    return bytes([base[offsets[i]] for i in range(size)])


keychain = {
    "laser": {
        "key": shuffle(bytes.fromhex("4d5875b5afc4aee2cffa68dfe5788d730e602e1cb6061ff3c3cb5ba37bd4bf58"), 42),
        "scid_version": "1.12.16",
        "version": "65.165",
        "recaptchasitekey": "6Lf3ThsqAAAAABuxaWIkogybKxfxoKxtR-aq5g7l",
        "name": "Brawl Stars",
        "packet": "laser",
    },
    "scroll": {
        "key": shuffle(bytes.fromhex("884e0665320eca797ac8bfed384b485b84039b441cbd0995483a796569eff170"), 42),
        "scid_version": "1.12.11",
        "version": "13.300.33",
        "recaptchasitekey": "6LcwMCIqAAAAAEbYq9yxb6JwEz-yBTwTfYrjAOSl",
        "name": "Clash Royale",
        "packet": "clashroyale",
    },
    "magic": {
        "key": shuffle(bytes.fromhex("ad161215d2216483441a3fc5ba0f18b108441584ba888e0f66d43a38f870c1b9"), 42),
        "scid_version": "1.12.8",
        "version": "18.0.10",
        "recaptchasitekey": "6Lf9SSIqAAAAAHfB6t8O9gGu6-Y_oHNkFtlMO2eT",
        "name": "Clash of Clans",
        "packet": "clashofclans",
    },
}


def ensure_cache_folder():
    os.makedirs(CACHE_FOLDER, exist_ok=True)
    if not os.path.exists(BIN_STATE_FILE):
        with open(BIN_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f)


def extract_email(text: str) -> str | None:
    match = re.search(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,7}\b", text)
    return match.group(0) if match else None


def is_valid_code_format(text: str) -> bool:
    return bool(re.fullmatch(r"\d{3}\s*\d{3}", text.strip()))


def get_next_bin(game: str):
    ensure_cache_folder()
    pkl_file = os.path.join(CACHE_FOLDER, f"{game}.pkl")
    if not os.path.exists(pkl_file):
        logger.warning("Файл %s не найден", pkl_file)
        return None
    try:
        with open(BIN_STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        state = {}
    try:
        with open(pkl_file, "rb") as f:
            bin_data = pickle.load(f)
        if not bin_data:
            return None
        last = state.get(game, {}).get("last_index", -1)
        idx = (last + 1) % len(bin_data)
        state[game] = {"last_index": idx}
        with open(BIN_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=4)
        return bin_data[idx]
    except (pickle.UnpicklingError, EOFError) as e:
        logger.error("Файл %s повреждён (%s), удаляем...", pkl_file, e)
        with suppress(OSError):
            os.remove(pkl_file)
        return None
    except Exception as e:
        logger.error("Ошибка при чтении bin для %s: %s", game, e)
        return None


def generate_sig(data, method, useragent, did, game):
    key = keychain[game]["key"]
    t = int(time.time())
    raw = f"{t}POST/{method}{urllib.parse.urlencode(data)}user-agent={useragent}x-supercell-device-id={did}"
    sig = (
        base64.b64encode(hmac.digest(key, raw.encode(), "sha256"))
        .decode()
        .replace("+", "-")
        .replace("/", "_")
        .replace("=", "")
    )
    return f"RFPv1 Timestamp={t},SignedHeaders=user-agent;x-supercell-device-id,Signature={sig}"


def get_recaptcha(game: str) -> str | None:
    data = get_next_bin(game)
    if not data:
        return None
    headers = {
        "User-Agent": UserAgent().random,
        "Connection": "Keep-Alive",
        "Accept-Encoding": "gzip",
        "Host": "www.recaptcha.net",
        "Content-Type": "application/x-protobuffer",
    }
    try:
        r = httpx.post(recaptcha_url, headers=headers, data=data, timeout=20)
        content = str(r.content)
        start = content.find("0cAFcW")
        return content[start:].split("\\x")[0] if start != -1 else None
    except Exception:
        logger.error("Ошибка reCAPTCHA", exc_info=True)
    return None


def _build_ua(game: str):
    model = random.choice(PHONE_MODELS)
    os_version = random.choice(IOS_VERSIONS)
    gc = keychain[game]
    ua = (
        f"scid/{gc['scid_version']} (iOS {os_version}; {game}-prod; {model}) "
        f"com.supercell.{gc['packet']}/{gc['version']}"
    )
    return ua, model, os_version


def send_request(email: str, game: str) -> tuple:
    did = secrets.token_hex(8)
    recaptcha = get_recaptcha(game)
    if not recaptcha:
        return None, did, None

    ua, _, _ = _build_ua(game)
    ua_info: dict = {"ua": ua}

    data = {
        "scope": "account/connect",
        "identifier": email,
        "identifierType": "EMAIL",
        "application": f"{game}-prod",
        "recaptchaToken": recaptcha,
        "recaptchaSiteKey": keychain[game]["recaptchasitekey"],
        "intent": "LOGIN",
    }
    encoded = urllib.parse.urlencode(data)
    headers = {
        "accept": "*/*",
        "accept-encoding": "gzip, deflate",
        "accept-language": "ru",
        "content-length": str(len(encoded)),
        "content-type": "application/x-www-form-urlencoded; charset=utf-8",
        "host": "id.supercell.com",
        "user-agent": ua,
        "x-supercell-device-id": did,
        "x-supercell-request-forgery-protection": generate_sig(
            data, "api/account/v2/pinAuthentication.start", ua, did, game
        ),
    }
    try:
        r = httpx.post(sc_api_url, headers=headers, data=encoded, timeout=20)
        with suppress(Exception):
            state_token = (r.json().get("data") or {}).get("state")
            if state_token:
                ua_info["state"] = state_token
        return r, did, ua_info
    except Exception:
        logger.error("Ошибка при отправке кода для %s", email, exc_info=True)
        return None, did, ua_info


def validate_code(pin: str, game: str, did: str, ua_info: dict | None = None) -> bool | None:
    ua = (ua_info or {}).get("ua") or _build_ua(game)[0]
    state_token = (ua_info or {}).get("state", "")
    if not state_token:
        logger.error("state-токен отсутствует")
        return None

    pin = pin.replace(" ", "")
    data = {"pin": pin, "state": state_token}
    encoded = urllib.parse.urlencode(data)
    headers = {
        "accept": "*/*",
        "accept-encoding": "gzip, deflate",
        "accept-language": "ru",
        "content-length": str(len(encoded)),
        "content-type": "application/x-www-form-urlencoded; charset=utf-8",
        "host": "id.supercell.com",
        "user-agent": ua,
        "x-supercell-device-id": did,
        "x-supercell-request-forgery-protection": generate_sig(
            data, "api/account/v2/pinAuthentication.complete", ua, did, game
        ),
    }
    try:
        r = httpx.post(validate_url, headers=headers, data=encoded, timeout=20)
        return r.json().get("ok", False)
    except Exception:
        logger.error("Ошибка валидации кода", exc_info=True)
        return None


# ── FSM ────────────────────────────────────────────────────────────────────────

class OTPState(StatesGroup):
    choosing_game = State()
    waiting_email = State()
    waiting_code = State()


def game_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔥 Brawl Stars", callback_data="game:laser"),
            InlineKeyboardButton(text="👑 Clash Royale", callback_data="game:scroll"),
        ],
        [
            InlineKeyboardButton(text="⚔️ Clash of Clans", callback_data="game:magic"),
        ],
    ])


router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(OTPState.choosing_game)
    await message.answer("Выбери игру:", reply_markup=game_keyboard())


@router.callback_query(F.data.startswith("game:"), OTPState.choosing_game)
async def cb_game(call: CallbackQuery, state: FSMContext) -> None:
    game = call.data.split(":")[1]
    if game not in keychain:
        await call.answer("Неизвестная игра", show_alert=True)
        return
    game_name = keychain[game]["name"]
    await state.update_data(game=game)
    await state.set_state(OTPState.waiting_email)
    await call.message.edit_text(
        f"Игра: <b>{game_name}</b>\n\nОтправь свою почту Supercell ID:"
    )
    await call.answer()


@router.message(OTPState.waiting_email)
async def msg_email(message: Message, state: FSMContext) -> None:
    email = extract_email(message.text or "")
    if not email:
        await message.answer("❌ Почта не найдена. Отправь ещё раз:")
        return

    data = await state.get_data()
    game = data["game"]
    game_name = keychain[game]["name"]

    wait_msg = await message.answer("⏳ Запрашиваю код...")

    resp, device_id, ua_info = await asyncio.to_thread(send_request, email, game)

    if resp is not None and resp.status_code == 200:
        await state.update_data(email=email, device_id=device_id, ua_info=ua_info)
        await state.set_state(OTPState.waiting_code)
        await wait_msg.edit_text(
            f"✅ Код отправлен на <code>{email}</code> для <b>{game_name}</b>\n\n"
            f"Жди письмо и отправь код сюда:"
        )
    else:
        await wait_msg.edit_text(
            "❌ Не удалось запросить код.\n\n"
            "Возможные причины:\n"
            "• Неверная почта\n"
            "• Нет .pkl файлов (нужны laser.pkl / scroll.pkl / magic.pkl в папке storage/cache/supercell_auto_otp/)\n\n"
            "Попробуй снова /start"
        )
        await state.clear()


@router.message(OTPState.waiting_code)
async def msg_code(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not is_valid_code_format(text):
        await message.answer("❌ Код должен быть 6 цифр (например: 123456 или 123 456). Попробуй ещё раз:")
        return

    data = await state.get_data()
    game = data["game"]
    device_id = data["device_id"]
    ua_info = data.get("ua_info")

    try:
        success = await asyncio.to_thread(validate_code, text, game, device_id, ua_info=ua_info)
    except Exception:
        await message.answer("❌ Ошибка при проверке кода. Начни заново: /start")
        await state.clear()
        return

    if success is True:
        await message.answer(f"✅ Код <code>{text}</code> верный! Можно входить.")
    elif success is False:
        await message.answer("❌ Код неверный или истёк. Начни заново: /start")
    else:
        await message.answer("⚠️ Не удалось проверить код. Начни заново: /start")
    await state.clear()


# ── Запуск ─────────────────────────────────────────────────────────────────────

async def main() -> None:
    ensure_cache_folder()
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)
    logger.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
