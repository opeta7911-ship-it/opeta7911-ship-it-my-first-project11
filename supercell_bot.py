#!/usr/bin/env python3
"""Standalone Supercell OTP Bot"""
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
import zipfile
from contextlib import suppress

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton as IKB,
    InlineKeyboardMarkup as IKM,
    Message,
)

try:
    from fake_useragent import UserAgent
    _UA_OK = True
except Exception:
    _UA_OK = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════
#  НАСТРОЙКИ — заполни перед запуском
# ══════════════════════════════════════════════════════════════════════
BOT_TOKEN    = "ВСТАВЬ_ТОКЕН_БОТА"
ADMIN_ID     = 0          # твой Telegram ID (узнать у @userinfobot)
FUNPAY_KEY   = ""         # golden_key от FunPay (необязательно для старта)
# ══════════════════════════════════════════════════════════════════════

CACHE_FOLDER    = "storage/cache/supercell_auto_otp/"
BIN_STATE_FILE  = os.path.join(CACHE_FOLDER, "last_bin.json")
SETTINGS_FILE   = "otp_settings.json"
VALID_PKL       = ["laser.pkl", "scroll.pkl", "magic.pkl"]

EXPECTED_CODES: dict = {}   # chat_id -> state

DEFAULT_SETTINGS = {
    "auto_request":  True,
    "validate_code": True,
    "group_enabled": True,
    "funpay_key":    FUNPAY_KEY,
    "messages": {
        "new_order":      "Напишите пожалуйста вашу почту Supercell ID",
        "code_requested": "Запросил код на вашу почту для $game_name, скиньте его пожалуйста сюда в чат, как придёт",
        "code_valid":     "Спасибо, код $code верный! Ожидайте выполнения, отпишусь",
        "code_invalid":   "Код указан неверно :(",
    }
}

GAME_CATEGORIES = {
    "laser":  [1127, 967, 1091, 3126, 3151],
    "scroll": [973,  1130, 150, 3180],
    "magic":  [972,  1129, 1088, 3181],
}

recaptcha_url = "https://www.recaptcha.net/recaptcha/api3/mrr"
sc_api_url    = "https://id.supercell.com/api/account/v2/pinAuthentication.start"
validate_url  = "https://id.supercell.com/api/account/v2/pinAuthentication.complete"

PHONE_MODELS = ["iPhone14,5","iPhone14,7","iPhone15,2","iPhone15,4",
                "iPhone16,1","iPhone16,2","iPhone17,1","iPhone17,3"]
IOS_VERSIONS = ["17.4","17.5","17.6","18.0","18.1","18.2"]


# ── Settings ──────────────────────────────────────────────────────────────────

def load_settings() -> dict:
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in DEFAULT_SETTINGS.items():
            if k not in data:
                data[k] = v
        if "messages" in data:
            for mk, mv in DEFAULT_SETTINGS["messages"].items():
                if mk not in data["messages"]:
                    data["messages"][mk] = mv
        return data
    return DEFAULT_SETTINGS.copy()


def save_settings(s: dict):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=4, ensure_ascii=False)


# ── Supercell crypto ──────────────────────────────────────────────────────────

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


keychain = {
    "laser": {
        "key": _shuffle(bytes.fromhex("4d5875b5afc4aee2cffa68dfe5788d730e602e1cb6061ff3c3cb5ba37bd4bf58"), 42),
        "scid_version": "1.12.16", "version": "65.165",
        "recaptchasitekey": "6Lf3ThsqAAAAABuxaWIkogybKxfxoKxtR-aq5g7l",
        "name": "Brawl Stars", "packet": "laser",
    },
    "scroll": {
        "key": _shuffle(bytes.fromhex("884e0665320eca797ac8bfed384b485b84039b441cbd0995483a796569eff170"), 42),
        "scid_version": "1.12.11", "version": "13.300.33",
        "recaptchasitekey": "6LcwMCIqAAAAAEbYq9yxb6JwEz-yBTwTfYrjAOSl",
        "name": "Clash Royale", "packet": "clashroyale",
    },
    "magic": {
        "key": _shuffle(bytes.fromhex("ad161215d2216483441a3fc5ba0f18b108441584ba888e0f66d43a38f870c1b9"), 42),
        "scid_version": "1.12.8", "version": "18.0.10",
        "recaptchasitekey": "6Lf9SSIqAAAAAHfB6t8O9gGu6-Y_oHNkFtlMO2eT",
        "name": "Clash of Clans", "packet": "clashofclans",
    },
}


def ensure_cache():
    os.makedirs(CACHE_FOLDER, exist_ok=True)
    if not os.path.exists(BIN_STATE_FILE):
        with open(BIN_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f)


def get_bin_info() -> dict:
    """Возвращает {game: count} для каждой игры."""
    ensure_cache()
    info = {}
    for game in keychain:
        pkl = os.path.join(CACHE_FOLDER, f"{game}.pkl")
        if os.path.exists(pkl):
            try:
                with open(pkl, "rb") as f:
                    data = pickle.load(f)
                info[game] = len(data) if data else 0
            except Exception:
                info[game] = 0
        else:
            info[game] = None   # нет файла
    return info


def get_next_bin(game: str):
    ensure_cache()
    pkl = os.path.join(CACHE_FOLDER, f"{game}.pkl")
    if not os.path.exists(pkl):
        return None
    try:
        with open(BIN_STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        state = {}
    try:
        with open(pkl, "rb") as f:
            bin_data = pickle.load(f)
        if not bin_data:
            return None
        idx = (state.get(game, {}).get("last_index", -1) + 1) % len(bin_data)
        state[game] = {"last_index": idx}
        with open(BIN_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=4)
        return bin_data[idx]
    except (pickle.UnpicklingError, EOFError) as e:
        logger.error("Бин %s повреждён (%s), удаляем", pkl, e)
        with suppress(OSError):
            os.remove(pkl)
        return None


def _generate_sig(data, method, ua, did, game):
    key = keychain[game]["key"]
    t = int(time.time())
    raw = f"{t}POST/{method}{urllib.parse.urlencode(data)}user-agent={ua}x-supercell-device-id={did}"
    sig = (base64.b64encode(hmac.digest(key, raw.encode(), "sha256"))
           .decode().replace("+", "-").replace("/", "_").replace("=", ""))
    return f"RFPv1 Timestamp={t},SignedHeaders=user-agent;x-supercell-device-id,Signature={sig}"


def _get_recaptcha(game: str) -> str | None:
    data = get_next_bin(game)
    if not data:
        return None
    ua = UserAgent().random if _UA_OK else "Mozilla/5.0"
    headers = {
        "User-Agent": ua, "Connection": "Keep-Alive",
        "Accept-Encoding": "gzip", "Host": "www.recaptcha.net",
        "Content-Type": "application/x-protobuffer",
    }
    try:
        r = httpx.post(recaptcha_url, headers=headers, data=data, timeout=20)
        content = str(r.content)
        start = content.find("0cAFcW")
        return content[start:].split("\\x")[0] if start != -1 else None
    except Exception:
        logger.error("reCAPTCHA ошибка", exc_info=True)
    return None


def _build_ua(game: str) -> tuple[str, str, str]:
    model = random.choice(PHONE_MODELS)
    ver   = random.choice(IOS_VERSIONS)
    gc    = keychain[game]
    ua = (f"scid/{gc['scid_version']} (iOS {ver}; {game}-prod; {model}) "
          f"com.supercell.{gc['packet']}/{gc['version']}")
    return ua, model, ver


def do_send_request(email: str, game: str) -> tuple:
    did = secrets.token_hex(8)
    cap = _get_recaptcha(game)
    if not cap:
        return None, did, None
    ua, _, _ = _build_ua(game)
    ua_info: dict = {"ua": ua}
    data = {
        "scope": "account/connect", "identifier": email,
        "identifierType": "EMAIL", "application": f"{game}-prod",
        "recaptchaToken": cap, "recaptchaSiteKey": keychain[game]["recaptchasitekey"],
        "intent": "LOGIN",
    }
    encoded = urllib.parse.urlencode(data)
    headers = {
        "accept": "*/*", "accept-encoding": "gzip, deflate",
        "accept-language": "ru", "content-length": str(len(encoded)),
        "content-type": "application/x-www-form-urlencoded; charset=utf-8",
        "host": "id.supercell.com", "user-agent": ua,
        "x-supercell-device-id": did,
        "x-supercell-request-forgery-protection": _generate_sig(
            data, "api/account/v2/pinAuthentication.start", ua, did, game),
    }
    try:
        r = httpx.post(sc_api_url, headers=headers, data=encoded, timeout=20)
        with suppress(Exception):
            st = (r.json().get("data") or {}).get("state")
            if st:
                ua_info["state"] = st
        return r, did, ua_info
    except Exception:
        logger.error("Ошибка запроса кода для %s", email, exc_info=True)
        return None, did, ua_info


def do_validate_code(pin: str, game: str, did: str, ua_info: dict | None = None) -> bool | None:
    ua = (ua_info or {}).get("ua") or _build_ua(game)[0]
    state_token = (ua_info or {}).get("state", "")
    if not state_token:
        return None
    pin = pin.replace(" ", "")
    data = {"pin": pin, "state": state_token}
    encoded = urllib.parse.urlencode(data)
    headers = {
        "accept": "*/*", "accept-encoding": "gzip, deflate",
        "accept-language": "ru", "content-length": str(len(encoded)),
        "content-type": "application/x-www-form-urlencoded; charset=utf-8",
        "host": "id.supercell.com", "user-agent": ua,
        "x-supercell-device-id": did,
        "x-supercell-request-forgery-protection": _generate_sig(
            data, "api/account/v2/pinAuthentication.complete", ua, did, game),
    }
    try:
        r = httpx.post(validate_url, headers=headers, data=encoded, timeout=20)
        return r.json().get("ok", False)
    except Exception:
        logger.error("Ошибка валидации", exc_info=True)
        return None


def extract_email(text: str) -> str | None:
    m = re.search(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,7}\b", text)
    return m.group(0) if m else None


def is_code(text: str) -> bool:
    return bool(re.fullmatch(r"\d{3}\s*\d{3}", text.strip()))


def fmt(template: str, **kw) -> str:
    for k, v in kw.items():
        template = template.replace(f"${k}", str(v) if v is not None else "")
    return template


# ── FunPay monitor ────────────────────────────────────────────────────────────

class FunPayMonitor:
    def __init__(self, settings: dict, bot: Bot):
        self.settings = settings
        self.bot = bot
        self._account = None
        self._username = None
        self._running = False

    def _init(self):
        key = self.settings.get("funpay_key", "")
        if not key:
            return False
        try:
            from FunPayAPI.account import Account
            self._account = Account(golden_key=key).get()
            self._username = self._account.username
            return True
        except Exception as e:
            logger.warning("FunPay init failed: %s", e)
            return False

    def get_username(self) -> str | None:
        if self._account:
            return self._username
        key = self.settings.get("funpay_key", "")
        if not key:
            return None
        try:
            from FunPayAPI.account import Account
            acc = Account(golden_key=key).get()
            self._account = acc
            self._username = acc.username
            return acc.username
        except Exception:
            return None

    async def run(self):
        self._running = True
        if not self._init():
            logger.info("FunPay мониторинг отключён — нет golden_key")
            return
        logger.info("FunPay мониторинг запущен (%s)", self._username)

        try:
            from FunPayAPI.updater.runner import Runner
            from FunPayAPI.updater.events import NewMessageEvent, NewOrderEvent

            runner = Runner(self._account)

            @runner.event
            async def on_new_message(e: NewMessageEvent):
                await self._handle_message(e.message)

            @runner.event
            async def on_new_order(e: NewOrderEvent):
                await self._handle_order(e.order)

            await runner.run()
        except Exception as e:
            logger.error("FunPay runner error: %s", e, exc_info=True)

    async def _handle_order(self, order):
        if not self.settings.get("auto_request"):
            return
        if not order.subcategory:
            return
        sid = order.subcategory.id
        game = next((g for g, cats in GAME_CATEGORIES.items() if sid in cats), None)
        if not game:
            return
        game_name = keychain[game]["name"]
        with suppress(Exception):
            self._account.send_message(
                order.chat_id,
                fmt(self.settings["messages"]["new_order"], game_name=game_name),
                order.buyer_username,
            )

    async def _handle_message(self, message):
        chat_id = message.chat_id
        username = message.author
        text = message.text or ""

        if username == self._username:
            return

        # Протухшие ожидания
        if chat_id in EXPECTED_CODES:
            if time.time() - EXPECTED_CODES[chat_id].get("ts", 0) > 600:
                del EXPECTED_CODES[chat_id]

        email = extract_email(text)
        if email and self.settings.get("auto_request"):
            # Определяем игру по заказам
            game = await asyncio.to_thread(self._get_game, chat_id, username)
            if not game:
                return
            game_name = keychain[game]["name"]
            resp, did, ua_info = await asyncio.to_thread(do_send_request, email, game)
            if resp and resp.status_code == 200:
                EXPECTED_CODES[chat_id] = {"email": email, "game": game,
                                           "did": did, "ua_info": ua_info, "ts": time.time()}
                with suppress(Exception):
                    self._account.send_message(
                        chat_id,
                        fmt(self.settings["messages"]["code_requested"],
                            game_name=game_name, email=email),
                        username,
                    )
            else:
                logger.warning("Код не запрошен для %s (%s)", email, game)

        elif chat_id in EXPECTED_CODES and is_code(text):
            st = EXPECTED_CODES[chat_id]
            game, did, ua_info, email = st["game"], st["did"], st.get("ua_info"), st["email"]
            game_name = keychain[game]["name"]
            if self.settings.get("validate_code"):
                ok = await asyncio.to_thread(do_validate_code, text, game, did, ua_info)
                if ok is True:
                    msg_key = "code_valid"
                    del EXPECTED_CODES[chat_id]
                elif ok is False:
                    msg_key = "code_invalid"
                else:
                    msg_key = None
                if msg_key:
                    with suppress(Exception):
                        self._account.send_message(
                            chat_id,
                            fmt(self.settings["messages"][msg_key],
                                game_name=game_name, email=email, code=text),
                            username,
                        )

    def _get_game(self, chat_id: int, username: str) -> str | None:
        try:
            from FunPayAPI.common.enums import OrderStatuses
            sells, start = [], None
            while True:
                start, batch = self._account.get_sells(buyer=username, start_from=start)
                sells.extend(batch)
                if start is None:
                    break
                time.sleep(0.3)
            for o in reversed(sells):
                if o.status == OrderStatuses.PAID and o.subcategory:
                    game = next((g for g, cats in GAME_CATEGORIES.items()
                                 if o.subcategory.id in cats), None)
                    if game:
                        return game
        except Exception:
            pass
        return None


# ── Keyboards ──────────────────────────────────────────────────────────────────

def v(flag: bool) -> str:
    return "✅" if flag else "❌"


def main_menu_kb(s: dict) -> IKM:
    return IKM(inline_keyboard=[
        [IKB(f"{v(s['auto_request'])} Авто-запрос",  callback_data="toggle:auto_request"),
         IKB(f"{v(s['validate_code'])} Авто-проверка", callback_data="toggle:validate_code")],
        [IKB("🤖 Запросить код",  callback_data="manual:request"),
         IKB("🔍 Проверить код",  callback_data="manual:validate")],
        [IKB("📁 Загрузить бины", callback_data="action:upload_bins"),
         IKB("💬 Тексты",         callback_data="action:texts")],
        [IKB("🔑 FunPay",         callback_data="action:funpay"),
         IKB("🔄 Обновить",       callback_data="action:refresh")],
    ])


def texts_kb() -> IKM:
    return IKM(inline_keyboard=[
        [IKB("📦 Новый заказ",   callback_data="editmsg:new_order")],
        [IKB("📨 Код запрошен",  callback_data="editmsg:code_requested")],
        [IKB("✅ Код верный",    callback_data="editmsg:code_valid")],
        [IKB("❌ Код неверный",  callback_data="editmsg:code_invalid")],
        [IKB("◀️ Назад",         callback_data="action:back")],
    ])


def back_kb() -> IKM:
    return IKM(inline_keyboard=[[IKB("◀️ Назад", callback_data="action:back")]])


# ── Main menu text ──────────────────────────────────────────────────────────────

def build_main_text(s: dict, fp_monitor: FunPayMonitor) -> str:
    # FunPay статус
    fp_username = fp_monitor.get_username()
    fp_line = f"🔑 FunPay: {v(bool(fp_username))} {fp_username or 'не настроен'}"

    # Бины
    bins = get_bin_info()
    loaded = sum(1 for v2 in bins.values() if v2 is not None and v2 > 0)
    total  = len(bins)
    bins_line = f"📦 Бины: {loaded}/{total}"
    game_lines = ""
    game_names = {"laser": "Brawl Stars", "scroll": "Clash Royale", "magic": "Clash of Clans"}
    for game, cnt in bins.items():
        if cnt is None:
            game_lines += f"\n  • {game_names[game]}: ❌ нет файла"
        else:
            game_lines += f"\n  • {game_names[game]}: {cnt}"

    return (
        "🏠 <b>Supercell OTP Bot</b>\n\n"
        f"{fp_line}\n"
        f"{bins_line}{game_lines}\n\n"
        f"⚡ Авто-запрос: {v(s['auto_request'])}\n"
        f"🔍 Авто-проверка: {v(s['validate_code'])}\n"
        f"👥 Группа: {v(s.get('group_enabled', True))}"
    )


# ── FSM ────────────────────────────────────────────────────────────────────────

class States(StatesGroup):
    manual_request_game  = State()
    manual_request_email = State()
    manual_validate_code = State()
    edit_funpay_key      = State()
    edit_msg             = State()
    upload_bins          = State()


# ── Router ─────────────────────────────────────────────────────────────────────

router = Router()
_fp_monitor: FunPayMonitor | None = None


def _get_monitor() -> FunPayMonitor:
    return _fp_monitor  # type: ignore


def _admin_only(func):
    async def wrapper(obj, **kw):
        uid = obj.from_user.id if hasattr(obj, "from_user") else 0
        if ADMIN_ID and uid != ADMIN_ID:
            return
        return await func(obj, **kw)
    wrapper.__name__ = func.__name__
    return wrapper


async def show_main(target, s: dict, edit: bool = False):
    mon = _get_monitor()
    text = build_main_text(s, mon)
    kb   = main_menu_kb(s)
    if edit and isinstance(target, (Message, CallbackQuery)):
        msg = target.message if isinstance(target, CallbackQuery) else target
        with suppress(Exception):
            await msg.edit_text(text, reply_markup=kb)
    else:
        msg = target.message if isinstance(target, CallbackQuery) else target
        await msg.answer(text, reply_markup=kb)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    if ADMIN_ID and message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    s = load_settings()
    await show_main(message, s)


# ── Toggles ──────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("toggle:"))
async def cb_toggle(call: CallbackQuery):
    if ADMIN_ID and call.from_user.id != ADMIN_ID:
        return
    key = call.data.split(":")[1]
    s = load_settings()
    s[key] = not s[key]
    save_settings(s)
    await call.answer(f"{'Включено' if s[key] else 'Выключено'}")
    await show_main(call, s, edit=True)


# ── Refresh ───────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "action:refresh")
async def cb_refresh(call: CallbackQuery):
    if ADMIN_ID and call.from_user.id != ADMIN_ID:
        return
    s = load_settings()
    await call.answer("Обновлено")
    await show_main(call, s, edit=True)


@router.callback_query(F.data == "action:back")
async def cb_back(call: CallbackQuery, state: FSMContext):
    if ADMIN_ID and call.from_user.id != ADMIN_ID:
        return
    await state.clear()
    s = load_settings()
    await show_main(call, s, edit=True)


# ── Manual request ────────────────────────────────────────────────────────────

@router.callback_query(F.data == "manual:request")
async def cb_manual_request(call: CallbackQuery, state: FSMContext):
    if ADMIN_ID and call.from_user.id != ADMIN_ID:
        return
    await state.set_state(States.manual_request_game)
    kb = IKM(inline_keyboard=[
        [IKB("🔥 Brawl Stars",   callback_data="pick_game:laser"),
         IKB("👑 Clash Royale",   callback_data="pick_game:scroll")],
        [IKB("⚔️ Clash of Clans", callback_data="pick_game:magic")],
        [IKB("◀️ Назад",          callback_data="action:back")],
    ])
    await call.message.edit_text("Выбери игру:", reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("pick_game:"), States.manual_request_game)
async def cb_pick_game(call: CallbackQuery, state: FSMContext):
    game = call.data.split(":")[1]
    await state.update_data(game=game)
    await state.set_state(States.manual_request_email)
    await call.message.edit_text(
        f"Игра: <b>{keychain[game]['name']}</b>\n\nВведи email:", reply_markup=back_kb()
    )
    await call.answer()


@router.message(States.manual_request_email)
async def msg_manual_email(message: Message, state: FSMContext):
    email = extract_email(message.text or "")
    if not email:
        await message.answer("❌ Email не найден, попробуй ещё раз:")
        return
    data = await state.get_data()
    game = data["game"]
    wait = await message.answer("⏳ Запрашиваю код...")
    resp, did, ua_info = await asyncio.to_thread(do_send_request, email, game)
    if resp and resp.status_code == 200:
        await state.update_data(email=email, did=did, ua_info=ua_info)
        await state.set_state(States.manual_validate_code)
        await wait.edit_text(
            f"✅ Код отправлен на <code>{email}</code>\n\nТеперь введи код из письма:",
            reply_markup=back_kb(),
        )
    else:
        await wait.edit_text("❌ Не удалось запросить код. Нет бинов или ошибка reCAPTCHA.")
        await state.clear()


# ── Manual validate ───────────────────────────────────────────────────────────

@router.callback_query(F.data == "manual:validate")
async def cb_manual_validate(call: CallbackQuery, state: FSMContext):
    if ADMIN_ID and call.from_user.id != ADMIN_ID:
        return
    # Если уже есть ожидание из manual request — переходим к вводу кода
    data = await state.get_data()
    if data.get("did"):
        await state.set_state(States.manual_validate_code)
        await call.message.edit_text("Введи код из письма:", reply_markup=back_kb())
    else:
        await call.answer("Сначала запроси код через «🤖 Запросить код»", show_alert=True)


@router.message(States.manual_validate_code)
async def msg_manual_code(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not is_code(text):
        await message.answer("❌ Код должен быть 6 цифр (например: 123 456):")
        return
    data = await state.get_data()
    game    = data["game"]
    did     = data["did"]
    ua_info = data.get("ua_info")
    ok = await asyncio.to_thread(do_validate_code, text, game, did, ua_info)
    if ok is True:
        await message.answer(f"✅ Код <code>{text}</code> верный!")
    elif ok is False:
        await message.answer("❌ Код неверный или истёк.")
    else:
        await message.answer("⚠️ Не удалось проверить (нет state-токена).")
    await state.clear()
    s = load_settings()
    await show_main(message, s)


# ── Upload bins ───────────────────────────────────────────────────────────────

@router.callback_query(F.data == "action:upload_bins")
async def cb_upload_bins(call: CallbackQuery, state: FSMContext):
    if ADMIN_ID and call.from_user.id != ADMIN_ID:
        return
    await state.set_state(States.upload_bins)
    await call.message.edit_text(
        "📁 Отправь ZIP-архив с файлами:\n"
        "<code>laser.pkl</code> — Brawl Stars\n"
        "<code>scroll.pkl</code> — Clash Royale\n"
        "<code>magic.pkl</code> — Clash of Clans",
        reply_markup=back_kb(),
    )
    await call.answer()


@router.message(States.upload_bins, F.document)
async def msg_upload_zip(message: Message, state: FSMContext, bot: Bot):
    doc = message.document
    if not doc.file_name.endswith(".zip"):
        await message.answer("❌ Нужен .zip файл")
        return
    ensure_cache()
    file = await bot.get_file(doc.file_id)
    tmp = os.path.join(CACHE_FOLDER, "tmp.zip")
    await bot.download_file(file.file_path, tmp)
    found = []
    try:
        with zipfile.ZipFile(tmp, "r") as z:
            for name in z.namelist():
                for valid in VALID_PKL:
                    if name.endswith(valid):
                        z.extract(name, CACHE_FOLDER)
                        extracted = os.path.join(CACHE_FOLDER, name)
                        target = os.path.join(CACHE_FOLDER, valid)
                        if extracted != target:
                            os.replace(extracted, target)
                            with suppress(OSError):
                                os.removedirs(os.path.dirname(extracted))
                        found.append(valid)
        os.remove(tmp)
    except Exception as e:
        await message.answer(f"❌ Ошибка при распаковке: {e}")
        with suppress(OSError):
            os.remove(tmp)
        return
    if not found:
        await message.answer("❌ В архиве нет нужных .pkl файлов")
        return
    await message.answer(f"✅ Загружено: {', '.join(found)}")
    await state.clear()
    s = load_settings()
    await show_main(message, s)


# ── Texts editor ──────────────────────────────────────────────────────────────

MSG_NAMES = {
    "new_order":      "📦 Новый заказ",
    "code_requested": "📨 Код запрошен",
    "code_valid":     "✅ Код верный",
    "code_invalid":   "❌ Код неверный",
}
MSG_VARS = {
    "new_order":      "$game_name, $id, $buyer",
    "code_requested": "$game_name, $email",
    "code_valid":     "$game_name, $email, $code",
    "code_invalid":   "$game_name, $email",
}


@router.callback_query(F.data == "action:texts")
async def cb_texts(call: CallbackQuery):
    if ADMIN_ID and call.from_user.id != ADMIN_ID:
        return
    await call.message.edit_text("💬 Выбери текст для редактирования:", reply_markup=texts_kb())
    await call.answer()


@router.callback_query(F.data.startswith("editmsg:"))
async def cb_editmsg(call: CallbackQuery, state: FSMContext):
    key = call.data.split(":")[1]
    s   = load_settings()
    cur = s["messages"].get(key, "")
    await state.set_state(States.edit_msg)
    await state.update_data(msg_key=key)
    await call.message.edit_text(
        f"<b>{MSG_NAMES.get(key, key)}</b>\n\n"
        f"Текущий:\n<code>{cur or 'не задан'}</code>\n\n"
        f"Переменные: <code>{MSG_VARS.get(key, '')}</code>\n\n"
        "Отправь новый текст (или <code>-</code> чтобы очистить):",
        reply_markup=back_kb(),
    )
    await call.answer()


@router.message(States.edit_msg)
async def msg_edit_msg(message: Message, state: FSMContext):
    data = await state.get_data()
    key  = data["msg_key"]
    s    = load_settings()
    s["messages"][key] = "" if message.text == "-" else (message.text or "")
    save_settings(s)
    await message.answer("✅ Сохранено")
    await state.clear()
    await show_main(message, s)


# ── FunPay settings ───────────────────────────────────────────────────────────

@router.callback_query(F.data == "action:funpay")
async def cb_funpay(call: CallbackQuery, state: FSMContext):
    if ADMIN_ID and call.from_user.id != ADMIN_ID:
        return
    s  = load_settings()
    cur = s.get("funpay_key", "")
    masked = f"{cur[:6]}...{cur[-4:]}" if len(cur) > 10 else (cur or "не задан")
    await state.set_state(States.edit_funpay_key)
    await call.message.edit_text(
        f"🔑 <b>FunPay golden_key</b>\n\nТекущий: <code>{masked}</code>\n\n"
        "Отправь новый golden_key:",
        reply_markup=back_kb(),
    )
    await call.answer()


@router.message(States.edit_funpay_key)
async def msg_funpay_key(message: Message, state: FSMContext):
    key = (message.text or "").strip()
    s   = load_settings()
    s["funpay_key"] = key
    save_settings(s)
    mon = _get_monitor()
    mon.settings = s
    mon._account  = None
    await message.answer("✅ golden_key сохранён. Перезапусти бота чтобы применить.")
    await state.clear()
    await show_main(message, s)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    global _fp_monitor

    ensure_cache()
    s   = load_settings()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp  = Dispatcher()
    dp.include_router(router)

    _fp_monitor = FunPayMonitor(s, bot)

    await asyncio.gather(
        dp.start_polling(bot, allowed_updates=["message", "callback_query"]),
        _fp_monitor.run(),
    )


if __name__ == "__main__":
    asyncio.run(main())
