#!/usr/bin/env python3
"""Standalone FunPay Brawl Stars OTP Bot"""
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
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import CallbackQuery, InlineKeyboardButton as IKB, InlineKeyboardMarkup as IKM

try:
    from fake_useragent import UserAgent
    _UA_OK = True
except Exception:
    _UA_OK = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════
BOT_TOKEN  = "8733509952:AAFKqAu9ARRz-hxgEU6VgnQOO5erPpY8T9s"
GOLDEN_KEY = "tzjjscw4knm25btp908cu05g068t8rzv"
ADMIN_ID   = 1330689833
GROUP_ID   = -1003823157522
# ══════════════════════════════════════════════════════════════════════

BRAWL_CATS = [1127, 967, 1091, 3126, 3151]

MSG_GREETING  = "Привет! Я в сети, могу выполнить заказ"
MSG_ASK_EMAIL = (
    "Отправьте вашу почту\n\n"
    "❗Проверьте правильно ли вы написали почту\n"
    "❗Проверьте что почта привязана к вашему нужному аккаунту для доната"
)
MSG_CODE_SENT  = "Запросил код, отправь его без пробела пожалуйста"
MSG_DONE_1     = "купил, с ака вышел"
MSG_DONE_2     = "жду подтверждение оплаты и хороший отзыв"
MSG_WRONG_CODE = "Код неверный, попробуй ещё раз"

CACHE_FOLDER   = "storage/cache/bs_otp/"
BIN_STATE_FILE = os.path.join(CACHE_FOLDER, "last_bin.json")

# Состояния чатов
GREETED:       set  = set()   # чаты где уже поздоровались
WAITING_EMAIL: set  = set()   # чаты где ждём почту
WAITING_CODE:  dict = {}      # chat_id -> {email, did, ua_info, username}

# ── OTP ────────────────────────────────────────────────────────────────────────

recaptcha_url = "https://www.recaptcha.net/recaptcha/api3/mrr"
sc_api_url    = "https://id.supercell.com/api/account/v2/pinAuthentication.start"

PHONE_MODELS = ["iPhone14,5","iPhone14,7","iPhone15,2","iPhone15,4",
                "iPhone16,1","iPhone16,2","iPhone17,1","iPhone17,3"]
IOS_VERSIONS = ["17.4","17.5","17.6","18.0","18.1","18.2"]

BRAWL_SCID   = "1.12.16"
BRAWL_VER    = "65.165"
BRAWL_SITE_KEY = "6Lf3ThsqAAAAABuxaWIkogybKxfxoKxtR-aq5g7l"


def _shuffle(base, seed):
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


BRAWL_KEY = _shuffle(bytes.fromhex(
    "4d5875b5afc4aee2cffa68dfe5788d730e602e1cb6061ff3c3cb5ba37bd4bf58"), 42)


def ensure_cache():
    os.makedirs(CACHE_FOLDER, exist_ok=True)
    if not os.path.exists(BIN_STATE_FILE):
        with open(BIN_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f)


def get_next_bin():
    ensure_cache()
    pkl = os.path.join(CACHE_FOLDER, "laser.pkl")
    if not os.path.exists(pkl):
        logger.warning("laser.pkl не найден — загрузи бины через бота Cardinal")
        return None
    try:
        with open(BIN_STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        with open(pkl, "rb") as f:
            bin_data = pickle.load(f)
        if not bin_data:
            return None
        idx = (state.get("laser", {}).get("last_index", -1) + 1) % len(bin_data)
        state["laser"] = {"last_index": idx}
        with open(BIN_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=4)
        return bin_data[idx]
    except Exception as e:
        logger.error("Ошибка чтения бина: %s", e)
        return None


def _gen_sig(data, method, ua, did):
    t = int(time.time())
    raw = (f"{t}POST/{method}{urllib.parse.urlencode(data)}"
           f"user-agent={ua}x-supercell-device-id={did}")
    sig = (base64.b64encode(hmac.digest(BRAWL_KEY, raw.encode(), "sha256"))
           .decode().replace("+", "-").replace("/", "_").replace("=", ""))
    return f"RFPv1 Timestamp={t},SignedHeaders=user-agent;x-supercell-device-id,Signature={sig}"


def _build_ua() -> str:
    model = random.choice(PHONE_MODELS)
    ver   = random.choice(IOS_VERSIONS)
    return (f"scid/{BRAWL_SCID} (iOS {ver}; laser-prod; {model}) "
            f"com.supercell.laser/{BRAWL_VER}")


def request_otp(email: str) -> tuple:
    did = secrets.token_hex(8)

    bin_data = get_next_bin()
    if not bin_data:
        return None, did, None

    ua_str = UserAgent().random if _UA_OK else "Mozilla/5.0"
    try:
        r = httpx.post(recaptcha_url, headers={
            "User-Agent": ua_str, "Connection": "Keep-Alive",
            "Accept-Encoding": "gzip", "Host": "www.recaptcha.net",
            "Content-Type": "application/x-protobuffer",
        }, data=bin_data, timeout=20)
        content = str(r.content)
        start   = content.find("0cAFcW")
        cap     = content[start:].split("\\x")[0] if start != -1 else None
    except Exception:
        return None, did, None

    if not cap:
        return None, did, None

    ua = _build_ua()
    data = {
        "scope": "account/connect", "identifier": email,
        "identifierType": "EMAIL", "application": "laser-prod",
        "recaptchaToken": cap, "recaptchaSiteKey": BRAWL_SITE_KEY,
        "intent": "LOGIN",
    }
    encoded = urllib.parse.urlencode(data)
    headers = {
        "accept": "*/*", "accept-encoding": "gzip, deflate",
        "accept-language": "ru", "content-length": str(len(encoded)),
        "content-type": "application/x-www-form-urlencoded; charset=utf-8",
        "host": "id.supercell.com", "user-agent": ua,
        "x-supercell-device-id": did,
        "x-supercell-request-forgery-protection": _gen_sig(
            data, "api/account/v2/pinAuthentication.start", ua, did),
    }
    try:
        resp    = httpx.post(sc_api_url, headers=headers, data=encoded, timeout=20)
        ua_info = {"ua": ua}
        with suppress(Exception):
            st = (resp.json().get("data") or {}).get("state")
            if st:
                ua_info["state"] = st
        return resp, did, ua_info
    except Exception:
        return None, did, None


def extract_email(text: str) -> str | None:
    m = re.search(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,7}\b", text)
    return m.group(0) if m else None


# ── Telegram handlers ──────────────────────────────────────────────────────────

router   = Router()
_account = None   # FunPayAPI Account object


def fp_account():
    return _account


async def fp_send(chat_id: int, text: str, username: str):
    await asyncio.to_thread(fp_account().send_message, chat_id, text, username)


async def notify_group(bot: Bot, chat_id: int, username: str,
                       email: str, code: str):
    kb = IKM(inline_keyboard=[[
        IKB(text="✅ Выполнено",          callback_data=f"done:{chat_id}:{username}"),
        IKB(text="❌ Сообщить о проблеме", callback_data=f"problem:{chat_id}:{username}"),
    ]])
    text = (
        f"🆕 <b>Новый заказ — Brawl Stars</b>\n\n"
        f"👤 Покупатель: <b>{username}</b>\n"
        f"📧 Почта покупателя: <code>{email}</code>\n"
        f"🔑 Код покупателя: <code>{code}</code>"
    )
    await bot.send_message(GROUP_ID, text, reply_markup=kb)


@router.callback_query(F.data.startswith("done:"))
async def cb_done(call: CallbackQuery):
    _, chat_id_s, username = call.data.split(":", 2)
    chat_id = int(chat_id_s)
    await fp_send(chat_id, MSG_DONE_1, username)
    await asyncio.sleep(0.5)
    await fp_send(chat_id, MSG_DONE_2, username)
    with suppress(Exception):
        await call.message.edit_reply_markup(reply_markup=None)
    await call.answer("✅ Готово!")


@router.callback_query(F.data.startswith("problem:"))
async def cb_problem(call: CallbackQuery):
    _, chat_id_s, username = call.data.split(":", 2)
    chat_id = int(chat_id_s)
    await fp_send(chat_id, MSG_WRONG_CODE, username)
    with suppress(Exception):
        await call.message.edit_reply_markup(reply_markup=None)
    await call.answer("Покупатель уведомлён")


# ── FunPay monitor ─────────────────────────────────────────────────────────────

async def funpay_monitor(bot: Bot):
    global _account
    logger.info("Подключаюсь к FunPay...")
    try:
        _account = await asyncio.to_thread(
            lambda: __import__("FunPayAPI.account", fromlist=["Account"])
            .Account(golden_key=GOLDEN_KEY).get()
        )
        logger.info("FunPay: авторизован как %s", _account.username)
    except Exception as e:
        logger.error("FunPay: ошибка авторизации: %s", e)
        return

    try:
        from FunPayAPI.updater.runner import Runner
        from FunPayAPI.updater.events import NewMessageEvent, NewOrderEvent

        runner = Runner(_account)

        async def on_new_order(e: NewOrderEvent):
            order = e.order
            if not order.subcategory:
                return
            if order.subcategory.id not in BRAWL_CATS:
                return
            chat_id  = order.chat_id
            username = order.buyer_username
            WAITING_EMAIL.add(chat_id)
            await fp_send(chat_id, MSG_ASK_EMAIL, username)
            logger.info("Заказ BS от %s — ждём почту", username)

        async def on_new_message(e: NewMessageEvent):
            msg      = e.message
            chat_id  = msg.chat_id
            username = msg.author
            text     = (msg.text or "").strip()

            if username == _account.username:
                return

            # Ждём код
            if chat_id in WAITING_CODE:
                code = text.replace(" ", "")
                if re.fullmatch(r"\d{6}", code):
                    state = WAITING_CODE.pop(chat_id)
                    await notify_group(bot, chat_id, username, state["email"], code)
                    logger.info("Код от %s отправлен в группу", username)
                return

            # Ждём почту
            if chat_id in WAITING_EMAIL:
                email = extract_email(text)
                if email:
                    WAITING_EMAIL.discard(chat_id)
                    logger.info("Получена почта %s от %s — запрашиваю код", email, username)
                    resp, did, ua_info = await asyncio.to_thread(request_otp, email)
                    if resp and resp.status_code == 200:
                        WAITING_CODE[chat_id] = {
                            "email": email, "did": did,
                            "ua_info": ua_info, "username": username,
                        }
                        await fp_send(chat_id, MSG_CODE_SENT, username)
                    else:
                        await fp_send(chat_id,
                                      "❌ Не удалось запросить код, попробуй позже",
                                      username)
                return

            # Первое сообщение — приветствие
            if chat_id not in GREETED:
                GREETED.add(chat_id)
                await fp_send(chat_id, MSG_GREETING, username)

        runner.add_handler(NewOrderEvent, on_new_order)
        runner.add_handler(NewMessageEvent, on_new_message)
        await runner.run()

    except Exception as e:
        logger.error("FunPay runner error: %s", e, exc_info=True)


# ── Main ───────────────────────────────────────────────────────────────────────

async def main():
    ensure_cache()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp  = Dispatcher()
    dp.include_router(router)

    logger.info("Бот запускается...")
    await asyncio.gather(
        dp.start_polling(bot, allowed_updates=["message", "callback_query"]),
        funpay_monitor(bot),
    )


if __name__ == "__main__":
    asyncio.run(main())
