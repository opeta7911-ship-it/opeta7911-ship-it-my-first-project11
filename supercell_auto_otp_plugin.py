from __future__ import annotations

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
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from pip._internal.cli.main import main
from telebot.types import (
    CallbackQuery,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Message,
)
from telebot.types import InlineKeyboardButton as B
from telebot.types import InlineKeyboardMarkup as K

from FunPayAPI.common.enums import OrderStatuses
from FunPayAPI.updater.events import NewMessageEvent, NewOrderEvent
from tg_bot import CBT
from tg_bot.static_keyboards import CLEAR_STATE_BTN
from tg_bot.utils import bool_to_text

try:
    import httpx
    from fake_useragent import UserAgent
except ImportError:
    main(["install", "-U", "httpx"])
    main(["install", "-U", "fake_useragent"])
    import httpx
    from fake_useragent import UserAgent

if TYPE_CHECKING:
    from cardinal import Cardinal

NAME = "Supercell Auto OTP"
VERSION = "0.0.2"
DESCRIPTION = (
    "Добавляет функционал автоматического запроса и проверки кодов Supercell"
)
CREDITS = "@swizzyer"
UUID = "adb1a859-aa42-420a-aaf5-dedff8bbfe94"
SETTINGS_PAGE = True

PLUGIN_FOLDER = f"storage/plugins/{UUID}/"
CACHE_FOLDER = "storage/cache/supercell_auto_otp/"
SETTINGS_FILE = os.path.join(PLUGIN_FOLDER, "settings.json")
BIN_STATE_FILE = os.path.join(CACHE_FOLDER, "last_bin.json")

CBT_SWITCH_AUTO = "sotp_switcha"
CBT_SWITCH_VALIDATE = "sotp_switchval"
CBT_UPLOAD_ZIP = "sotp_zip"
CBT_MESSAGES_MENU = "sotp_msgsmenu"
CBT_EDIT_MESSAGE = "sotp_editmsg"
CBT_SET_GROUP = "sotp_setgroup"

logger = logging.getLogger("FPC.supercell_auto_otp")

GAME_CATEGORIES = {
    "laser": [1127, 967, 1091, 3126, 3151],
    "scroll": [973, 1130, 150, 3180],
    "magic": [972, 1129, 1088, 3181],
}

DEFAULT_SETTINGS = {
    "auto_request": True,
    "validate_code": True,
    "group_chat_id": "",
    "messages": {
        "new_order": "Напишите пожалуйста вашу почту Supercell ID",
        "code_requested": "Запросил код на вашу почту для $game_name, скиньте его пожалуйста сюда в чат, как придёт",
        "code_valid": "Спасибо, код $code верный! Ожидайте выполнения, отпишусь",
        "code_invalid": "Код указан неверно :(",
    },
}

INLINE_FILTERS = {
    "bs": "laser",
    "cr": "scroll",
    "coc": "magic",
}

recaptcha_url = "https://www.recaptcha.net/recaptcha/api3/mrr"
sc_api_url = "https://id.supercell.com/api/account/v2/pinAuthentication.start"
validate_url = (
    "https://id.supercell.com/api/account/v2/pinAuthentication.complete"
)

PHONE_MODELS = [
    "iPhone14,5",
    "iPhone14,7",
    "iPhone15,2",
    "iPhone15,4",
    "iPhone16,1",
    "iPhone16,2",
    "iPhone17,1",
    "iPhone17,3",
]
IOS_VERSIONS = ["17.4", "17.5", "17.6", "18.0", "18.1", "18.2"]

VALID_PKL_FILES = [
    "laser.pkl",
    "scroll.pkl",
    "magic.pkl",
]
EXPECTED_CODES = {}

VARS_DICT = {
    "$id": "ID заказа",
    "$game_name": "Название игры",
    "$buyer": "Ник покупателя",
    "$created_at": "Дата создания заказа",
    "$amount_lot": "Сумма заказа",
    "$email": "Почта Supercell ID",
    "$code": "Код для входа",
}

AVAILABLE_VARS = {
    "new_order": ["id", "game_name", "buyer", "created_at", "amount_lot"],
    "code_requested": ["game_name", "email"],
    "code_valid": ["game_name", "email", "code"],
    "code_invalid": ["game_name", "email"],
}

LP = "[Supercell Auto OTP PLUGIN]"


def get_text_translate(
    txt: Literal["new_order", "code_requested", "code_valid", "code_invalid"],
) -> str:
    return {
        "new_order": "Новый заказ",
        "code_requested": "Код запрошен",
        "code_valid": "Код верный",
        "code_invalid": "Код неверный",
    }.get(txt, txt)


def back_to_settings_markup() -> K:
    return K().add(B("◀️ Назад", callback_data=f"{CBT.PLUGIN_SETTINGS}:{UUID}"))


def extract_email(text: str) -> str | None:
    match = re.search(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,7}\b", text
    )
    return match.group(0) if match else None


def is_valid_code_format(text: str) -> bool:
    return bool(re.fullmatch(r"\d{3}\s*\d{3}", text.strip()))


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
        "key": shuffle(
            bytes.fromhex(
                "4d5875b5afc4aee2cffa68dfe5788d730e602e1cb6061ff3c3cb5ba37bd4bf58"
            ),
            42,
        ),
        "scid_version": "1.12.16",
        "version": "65.165",
        "recaptchasitekey": "6Lf3ThsqAAAAABuxaWIkogybKxfxoKxtR-aq5g7l",
        "name": "Brawl Stars",
        "packet": "laser",
    },
    "scroll": {
        "key": shuffle(
            bytes.fromhex(
                "884e0665320eca797ac8bfed384b485b84039b441cbd0995483a796569eff170"
            ),
            42,
        ),
        "scid_version": "1.12.11",
        "version": "13.300.33",
        "recaptchasitekey": "6LcwMCIqAAAAAEbYq9yxb6JwEz-yBTwTfYrjAOSl",
        "name": "Clash Royale",
        "packet": "clashroyale",
    },
    "magic": {
        "key": shuffle(
            bytes.fromhex(
                "ad161215d2216483441a3fc5ba0f18b108441584ba888e0f66d43a38f870c1b9"
            ),
            42,
        ),
        "scid_version": "1.12.8",
        "version": "18.0.10",
        "recaptchasitekey": "6Lf9SSIqAAAAAHfB6t8O9gGu6-Y_oHNkFtlMO2eT",
        "name": "Clash of Clans",
        "packet": "clashofclans",
    },
}


def ensure_cache_folder():
    if not os.path.exists(CACHE_FOLDER):
        os.makedirs(CACHE_FOLDER)
    if not os.path.exists(BIN_STATE_FILE):
        with open(BIN_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f, indent=4, ensure_ascii=False)


def get_available_games():
    ensure_cache_folder()
    available_games = []
    for game, config in keychain.items():
        pkl_file = os.path.join(CACHE_FOLDER, f"{game}.pkl")
        if os.path.exists(pkl_file):
            available_games.append(config["name"])
    return available_games


def check_zip_file(tg, msg: Message) -> bool:
    if not msg.document:
        tg.bot.send_message(
            msg.chat.id,
            "❌ <i>Файл не обнаружен</i>",
            reply_markup=back_to_settings_markup(),
        )
        return False
    if not msg.document.file_name.endswith(".zip"):
        tg.bot.send_message(
            msg.chat.id,
            "❌ <i>Файл должен быть в формате <code>.zip</code></i>",
            reply_markup=back_to_settings_markup(),
        )
        return False
    if msg.document.file_size >= 20971520:
        tg.bot.send_message(
            msg.chat.id,
            "❌ <i>Размер файла не должен превышать <code>20МБ</code></i>",
            reply_markup=back_to_settings_markup(),
        )
        return False
    return True


def extract_zip_file(tg, msg: Message) -> bool:
    ensure_cache_folder()
    temp_zip_path = os.path.join(CACHE_FOLDER, "tmp.zip")
    try:
        file_info = tg.bot.get_file(msg.document.file_id)
        file = tg.bot.download_file(file_info.file_path)
        with open(temp_zip_path, "wb") as f:
            f.write(file)
    except Exception:
        tg.bot.send_message(
            msg.chat.id,
            "❌ <i>Произошла ошибка при загрузке ZIP-архива</i>",
            reply_markup=back_to_settings_markup(),
        )
        logger.error(f"{LP} ошибка при загрузке зип архива", exc_info=True)
        return False

    valid_files_found = False
    try:
        with zipfile.ZipFile(temp_zip_path, "r") as zip_ref:
            for filename in zip_ref.namelist():
                if "__MACOSX" in filename or os.path.basename(
                    filename
                ).startswith("."):
                    continue

                for valid_name in VALID_PKL_FILES:
                    if filename.endswith(valid_name):
                        zip_ref.extract(filename, CACHE_FOLDER)
                        extracted_path = os.path.join(CACHE_FOLDER, filename)
                        target_path = os.path.join(CACHE_FOLDER, valid_name)
                        if extracted_path != target_path:
                            os.replace(extracted_path, target_path)
                            try:
                                os.removedirs(os.path.dirname(extracted_path))
                            except OSError:
                                pass
                        valid_files_found = True
        os.remove(temp_zip_path)
        if not valid_files_found:
            nd = "\n• ".join(VALID_PKL_FILES)
            tg.bot.send_message(
                msg.chat.id,
                f"❌ В ZIP-архиве отсутствуют необходимые файлы:\n• {nd}",
                reply_markup=back_to_settings_markup(),
            )
            return False
        return True
    except Exception:
        tg.bot.send_message(
            msg.chat.id,
            "❌ <i>Произошла ошибка при распаковке ZIP-архива</i>",
            reply_markup=back_to_settings_markup(),
        )
        logger.debug(f"{LP} Ошибка при распаковке зип архива", exc_info=True)
        os.remove(temp_zip_path) if os.path.exists(temp_zip_path) else None
        return False


def get_next_bin(game):
    pkl_file = os.path.join(CACHE_FOLDER, f"{game}.pkl")

    try:
        ensure_cache_folder()
        try:
            with open(BIN_STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            state = {}
        last = state.get(game, {}).get("last_index", -1)
        if not os.path.exists(pkl_file):
            logger.warning(f"{LP} Файл {pkl_file} не найден")
            return None
        with open(pkl_file, "rb") as f:
            bin_data = pickle.load(f)
        if not bin_data:
            logger.warning(f"{LP} Нет доступных bin данных для игры {game}")
            return None
        idx = (last + 1) % len(bin_data)
        state[game] = {"last_index": idx}
        with open(BIN_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=4, ensure_ascii=False)
        return bin_data[idx]

    except (pickle.UnpicklingError, EOFError, IndexError) as e:
        logger.error(f"{LP} Файл {pkl_file} поврежден ({e}), удаляем...")
        with suppress(OSError):
            os.remove(pkl_file)
        return None
    except Exception as e:
        logger.error(f"{LP} Ошибка при получении bin данных для {game}: {e}")
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


def get_recaptcha(game):
    data = get_next_bin(game)
    if not data:
        logger.warning(f"{LP} Не удалось получить bin данные для {game}")
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
        logger.error(
            f"{LP} [reCAPTCHA] Произошел лютейший бабах", exc_info=True
        )
    return None


def _build_ua(game, model=None, os_version=None):
    model = model or random.choice(PHONE_MODELS)
    os_version = os_version or random.choice(IOS_VERSIONS)
    gc = keychain[game]
    ua = (
        f"scid/{gc['scid_version']} (iOS {os_version}; {game}-prod; {model}) "
        f"com.supercell.{gc['packet']}/{gc['version']}"
    )
    return ua, model, os_version


def send_request(email, game):
    did = secrets.token_hex(8)
    recaptcha = get_recaptcha(game)
    if not recaptcha:
        logger.warning(f"{LP} Не удалось получить reCAPTCHA токен для {game}")
        return None, did, None

    ua, model, os_version = _build_ua(game)
    ua_info = {"ua": ua, "model": model, "os_version": os_version}

    data = {
        "scope": "account/connect",
        "identifier": email,
        "identifierType": "EMAIL",
        "application": f"{game}-prod",
        "recaptchaToken": recaptcha,
        "recaptchaSiteKey": keychain[game]["recaptchasitekey"],
        "intent": "LOGIN",
    }
    encoded_data = urllib.parse.urlencode(data)
    headers = {
        "accept": "*/*",
        "accept-encoding": "gzip, deflate",
        "accept-language": "ru",
        "content-length": str(len(encoded_data)),
        "content-type": "application/x-www-form-urlencoded; charset=utf-8",
        "host": "id.supercell.com",
        "user-agent": ua,
        "x-supercell-device-id": did,
        "x-supercell-request-forgery-protection": generate_sig(
            data, "api/account/v2/pinAuthentication.start", ua, did, game
        ),
    }
    try:
        r = httpx.post(
            sc_api_url, headers=headers, data=encoded_data, timeout=20
        )
        try:
            resp_json = r.json()
            state_token = (resp_json.get("data") or {}).get("state")
            if state_token:
                ua_info["state"] = state_token
        except Exception:
            pass
        return r, did, ua_info
    except Exception:
        logger.error(
            f"{LP} Ошибка при отправке кода для {email}", exc_info=True
        )
        return None, did, ua_info


def validate_code(pin, game, did, ua_info=None):
    if ua_info and ua_info.get("ua"):
        ua = ua_info["ua"]
    else:
        ua, _, _ = _build_ua(game)

    state_token = (ua_info or {}).get("state", "")

    if not state_token:
        logger.error(f"{LP} state-токен отсутствует, будет бабах")
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
        logger.error(f"{LP} Ошибка в валидации кода", exc_info=True)
        raise


def get_game_from_order(
    cardinal: Cardinal, chat_id: int, username: str
) -> str | None:
    try:
        sells = []
        start_from = None
        while True:
            start_from, sells_temp = cardinal.account.get_sells(
                buyer=username, start_from=start_from
            )
            sells.extend(sells_temp)
            if start_from is None:
                break
            time.sleep(0.5)

        for order in reversed(sells):
            if order.status == OrderStatuses.PAID and order.subcategory:
                subcategory_id = order.subcategory.id
                for game, categories in GAME_CATEGORIES.items():
                    if subcategory_id in categories:
                        return game
        logger.debug(f"{LP} Игра не найдена для {username} (ID: {chat_id})")
        return None
    except Exception:
        logger.error(
            f"{LP} Ошибка при получении заказа для {username} (ID: {chat_id})",
            exc_info=True,
        )
        return None


def format_message(template: str, **kwargs) -> str:
    if not template:
        return ""

    created_at = kwargs.get("created_at", "Нет")
    if created_at != "Нет" and "T" in str(created_at):
        with suppress(Exception):
            dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
            created_at = dt.strftime("%d.%m.%Y %H:%M")

    replacements = {
        "$id": str(kwargs.get("id", kwargs.get("order_id", "Нет"))),
        "$order_id": str(kwargs.get("id", kwargs.get("order_id", "Нет"))),
        "$game_name": kwargs.get("game_name", "Нет"),
        "$buyer": kwargs.get("buyer", "Нет"),
        "$created_at": str(created_at),
        "$amount_lot": str(kwargs.get("amount_lot", "Нет")),
        "$email": kwargs.get("email", "Нет"),
        "$code": kwargs.get("code", "Нет"),
    }
    result = template
    for key, value in replacements.items():
        if value is None:
            value = "Нет"
        elif not isinstance(value, str):
            value = str(value)
        result = result.replace(key, value)
    return result


class SupercellOTP:
    def __init__(self, cardinal: Cardinal):
        self.cardinal = cardinal
        self.tg = cardinal.telegram
        self.bot = cardinal.telegram.bot if cardinal.telegram else None
        self.settings = DEFAULT_SETTINGS.copy()
        self.settings["messages"] = DEFAULT_SETTINGS["messages"].copy()
        self._tg_states = {}
        self.load_settings()
        ensure_cache_folder()

    def load_settings(self):
        if not os.path.exists(PLUGIN_FOLDER):
            os.makedirs(PLUGIN_FOLDER)
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                loaded_settings = json.loads(f.read())
                for key in ["auto_request", "validate_code", "group_chat_id"]:
                    if key in loaded_settings:
                        self.settings[key] = loaded_settings[key]
                if "messages" in loaded_settings and isinstance(
                    loaded_settings["messages"], dict
                ):
                    for msg_key in DEFAULT_SETTINGS["messages"]:
                        if msg_key in loaded_settings["messages"]:
                            self.settings["messages"][msg_key] = (
                                loaded_settings["messages"][msg_key]
                            )
        else:
            self.save_settings()

    def save_settings(self):
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(self.settings, f, indent=4, ensure_ascii=False)

    def send_to_group(self, text: str):
        """Отправляет уведомление в настроенную Telegram группу."""
        group_id = self.settings.get("group_chat_id", "")
        if not group_id:
            return
        try:
            self.bot.send_message(
                group_id,
                text,
                parse_mode="HTML",
            )
        except Exception:
            logger.error(f"{LP} Ошибка при отправке в группу {group_id}", exc_info=True)

    def handle_message(self, e: NewMessageEvent):
        message = e.message
        chat_id = message.chat_id
        username = message.author
        text = message.text or ""

        if username == self.cardinal.account.username:
            return

        if (
            chat_id in EXPECTED_CODES
            and "timestamp" in EXPECTED_CODES[chat_id]
        ):
            if time.time() - EXPECTED_CODES[chat_id]["timestamp"] > 600:
                del EXPECTED_CODES[chat_id]

        email = extract_email(text)
        if email and self.settings["auto_request"]:
            game = get_game_from_order(self.cardinal, chat_id, username)
            if not game:
                return

            try:
                sells = self.cardinal.account.get_sells(buyer=username)[1]
                latest_order_shortcut = next(
                    (
                        order
                        for order in reversed(sells)
                        if order.status == OrderStatuses.PAID
                    ),
                    None,
                )
                if latest_order_shortcut:
                    latest_order = self.cardinal.account.get_order(
                        latest_order_shortcut.id
                    )
                    if latest_order and latest_order.short_description:
                        order_title = latest_order.short_description.lower()
                        if (
                            "supercell store" in order_title
                            or "id rewards" in order_title
                            or "матчерино" in order_title
                        ):
                            return
            except Exception:
                logger.error(
                    f"{LP} Ошибка при проверке названия заказа для {username}",
                    exc_info=True,
                )
                return

            resp, device_id, ua_info = send_request(email, game)
            game_name = keychain.get(game, {}).get("name", game)
            if resp and resp.status_code == 200:
                EXPECTED_CODES[chat_id] = {
                    "email": email,
                    "game": game,
                    "device_id": device_id,
                    "timestamp": time.time(),
                    "ua_info": ua_info,
                    "username": username,
                }
                try:
                    self.cardinal.account.send_message(
                        chat_id,
                        format_message(
                            self.settings["messages"]["code_requested"],
                            game_name=game_name,
                            email=email,
                        ),
                        username,
                    )
                except Exception:
                    logger.error(
                        f"{LP} Ошибка при отправке сообщения",
                        exc_info=True,
                    )

                self.send_to_group(
                    f"📧 <b>Код запрошен</b>\n"
                    f"👤 Покупатель: <code>{username}</code>\n"
                    f"📮 Почта: <code>{email}</code>\n"
                    f"🎮 Игра: <code>{game_name}</code>"
                )
            else:
                logger.error(
                    f"{LP} Ошибка запроса кода для {email} (игра: {game})"
                )
                self.send_to_group(
                    f"⚠️ <b>Ошибка запроса кода</b>\n"
                    f"👤 Покупатель: <code>{username}</code>\n"
                    f"📮 Почта: <code>{email}</code>\n"
                    f"🎮 Игра: <code>{game_name}</code>"
                )

        elif (
            chat_id in EXPECTED_CODES
            and is_valid_code_format(text)
            and message.author_id != self.cardinal.account.id
        ):
            state = EXPECTED_CODES[chat_id]
            email = state["email"]
            game = state["game"]
            device_id = state["device_id"]
            ua_info = state.get("ua_info")
            game_name = keychain.get(game, {}).get("name", game)

            if self.settings["validate_code"]:
                success = validate_code(text, game, device_id, ua_info=ua_info)
                if success is True:
                    try:
                        self.cardinal.account.send_message(
                            chat_id,
                            format_message(
                                self.settings["messages"]["code_valid"],
                                game_name=game_name,
                                email=email,
                                code=text,
                            ),
                            username,
                        )
                        del EXPECTED_CODES[chat_id]
                    except Exception:
                        logger.error(
                            f"{LP} Ошибка при отправке сообщения на фанпей",
                            exc_info=True,
                        )

                    self.send_to_group(
                        f"✅ <b>Код верный!</b>\n"
                        f"👤 Покупатель: <code>{username}</code>\n"
                        f"📮 Почта: <code>{email}</code>\n"
                        f"🎮 Игра: <code>{game_name}</code>\n"
                        f"🔑 Код: <code>{text.replace(' ', '')}</code>"
                    )
                elif success is False:
                    try:
                        self.cardinal.account.send_message(
                            chat_id,
                            format_message(
                                self.settings["messages"]["code_invalid"],
                                game_name=game_name,
                                email=email,
                            ),
                            username,
                        )
                    except Exception:
                        logger.error(
                            f"{LP} Ошибка при отправке сообщения о неверном коде",
                            exc_info=True,
                        )

                    self.send_to_group(
                        f"❌ <b>Неверный код</b>\n"
                        f"👤 Покупатель: <code>{username}</code>\n"
                        f"📮 Почта: <code>{email}</code>\n"
                        f"🎮 Игра: <code>{game_name}</code>\n"
                        f"🔑 Введённый код: <code>{text.replace(' ', '')}</code>"
                    )
                else:
                    try:
                        self.cardinal.account.send_message(
                            chat_id,
                            "Не удалось проверить код. Запросите новый код и отправьте его снова.",
                            username,
                        )
                    except Exception:
                        logger.error(
                            f"{LP} Ошибка при отправке сообщения о недоступной проверке кода",
                            exc_info=True,
                        )
                    EXPECTED_CODES.pop(chat_id, None)

                    self.send_to_group(
                        f"⚠️ <b>Не удалось проверить код</b>\n"
                        f"👤 Покупатель: <code>{username}</code>\n"
                        f"📮 Почта: <code>{email}</code>\n"
                        f"🎮 Игра: <code>{game_name}</code>\n"
                        f"🔑 Код: <code>{text.replace(' ', '')}</code>"
                    )
            else:
                logger.info(
                    f"{LP} Проверка кода {text} для {email} (игра: {game}, CID: {chat_id}) отключена, скип"
                )
                self.send_to_group(
                    f"🔑 <b>Получен код (без проверки)</b>\n"
                    f"👤 Покупатель: <code>{username}</code>\n"
                    f"📮 Почта: <code>{email}</code>\n"
                    f"🎮 Игра: <code>{game_name}</code>\n"
                    f"🔑 Код: <code>{text.replace(' ', '')}</code>"
                )

    def handle_new_order(self, e: NewOrderEvent):
        order = e.order
        if order.buyer_username == self.cardinal.account.username:
            return
        if not self.settings["auto_request"]:
            return

        if not order.subcategory:
            return

        subcategory_id = order.subcategory.id
        game = None
        for game_key, categories in GAME_CATEGORIES.items():
            if subcategory_id in categories:
                game = game_key
                break

        if not game:
            return

        game_name = keychain.get(game, {}).get("name", game)

        try:
            self.cardinal.account.send_message(
                order.chat_id,
                format_message(
                    self.settings["messages"]["new_order"],
                    game_name=game_name,
                    id=order.id,
                    buyer=order.buyer_username,
                    created_at=order.date.isoformat(),
                    amount_lot=order.price,
                ),
                order.buyer_username,
            )
        except Exception:
            logger.error(
                f"{LP} Ошибка при отправке сообщения о новом заказе",
                exc_info=True,
            )

    def open_settings(self, call: CallbackQuery):
        available_games = get_available_games()
        status_text = "✅ Да" if available_games else "❌ Неа"

        if available_games:
            games_list = "\n".join([
                f"<i>{i} -</i> <code>{game}</code>"
                for i, game in enumerate(available_games, start=1)
            ])
            content = f"🎮 Доступные игры для запроса кода:\n\n{games_list}"
        else:
            content = "\n‼️ <b>Отсутствуют необходимые файлы для работы плагина, загрузите их с помощью кнопки «📁 Загрузить файлы»</b>"

        group_id = self.settings.get("group_chat_id", "")
        group_status = f"<code>{group_id}</code>" if group_id else "❌ Не настроена"

        settings_text = (
            "⚙️ <b>Настройки «Supercell Авто-Код»</b>\n\n"
            f"ℹ️ Готов к работе: <b>{status_text}</b>\n"
            f"📢 Группа уведомлений: {group_status}\n"
            + ("\n" if available_games else "")
            + content
        )

        is_on_auto_request = bool_to_text(self.settings["auto_request"])
        is_on_validate = bool_to_text(self.settings["validate_code"])

        keyboard = K()
        auto_request_txt = f"{is_on_auto_request} Авто-запрос кодов"
        auto_check_txt = f"{is_on_validate} Авто-проверка кодов"
        keyboard.row(
            B(auto_request_txt, callback_data=f"{CBT_SWITCH_AUTO}:0"),
            B(auto_check_txt, callback_data=f"{CBT_SWITCH_VALIDATE}:0"),
        )
        keyboard.add(B("📢 Настроить группу", callback_data=f"{CBT_SET_GROUP}:0"))
        keyboard.add(B("💬 Тексты", callback_data=f"{CBT_MESSAGES_MENU}:0"))
        keyboard.add(
            B("📁 Загрузить файлы", callback_data=f"{CBT_UPLOAD_ZIP}:0")
        )
        keyboard.add(B("◀️ Назад", callback_data=f"{CBT.EDIT_PLUGIN}:{UUID}:0"))

        self.bot.edit_message_text(
            settings_text,
            call.message.chat.id,
            call.message.id,
            reply_markup=keyboard,
        )
        self.bot.answer_callback_query(call.id)

    def switch_auto(self, call: CallbackQuery):
        self.settings["auto_request"] = not self.settings["auto_request"]
        self.save_settings()
        self.load_settings()
        self.open_settings(call)

    def switch_validate(self, call: CallbackQuery):
        self.settings["validate_code"] = not self.settings["validate_code"]
        self.save_settings()
        self.load_settings()
        self.open_settings(call)

    def set_group_chat(self, call: CallbackQuery):
        """Запрашивает ID группы у пользователя."""
        current = self.settings.get("group_chat_id", "")
        current_txt = f"Текущий ID: <code>{current}</code>" if current else "Группа пока не настроена."

        text = (
            "📢 <b>Настройка группы уведомлений</b>\n\n"
            f"{current_txt}\n\n"
            "Отправь ID группы (число, например <code>-1001234567890</code>).\n"
            "Чтобы узнать ID — добавь бота <b>@userinfobot</b> в группу.\n\n"
            "Отправь <code>0</code> чтобы отключить уведомления."
        )
        result = self.bot.send_message(
            call.message.chat.id, text, reply_markup=CLEAR_STATE_BTN()
        )
        self.tg.set_state(
            call.message.chat.id, result.id, call.from_user.id, CBT_SET_GROUP
        )
        self.bot.answer_callback_query(call.id)

    def handle_group_input(self, msg: Message):
        """Обрабатывает ввод ID группы."""
        self.tg.clear_state(msg.chat.id, msg.from_user.id, True)
        raw = msg.text.strip() if msg.text else ""

        if raw == "0":
            self.settings["group_chat_id"] = ""
            self.save_settings()
            self.bot.send_message(
                msg.chat.id,
                "✅ Уведомления в группу <b>отключены</b>.",
                reply_markup=back_to_settings_markup(),
            )
            return

        if not re.fullmatch(r"-?\d+", raw):
            self.bot.send_message(
                msg.chat.id,
                "❌ Неверный формат. Введи числовой ID группы.",
                reply_markup=back_to_settings_markup(),
            )
            return

        self.settings["group_chat_id"] = raw
        self.save_settings()

        try:
            self.bot.send_message(
                raw,
                "✅ <b>Supercell Auto OTP</b> — уведомления подключены!",
                parse_mode="HTML",
            )
            self.bot.send_message(
                msg.chat.id,
                f"✅ Группа <code>{raw}</code> успешно настроена!",
                reply_markup=back_to_settings_markup(),
            )
        except Exception:
            self.bot.send_message(
                msg.chat.id,
                f"⚠️ ID сохранён (<code>{raw}</code>), но не удалось отправить тестовое сообщение.\n"
                "Убедись, что бот добавлен в группу и имеет право писать.",
                reply_markup=back_to_settings_markup(),
            )

    def upload_zip(self, call: CallbackQuery):
        files_txt = "\n".join(f"<code>{f}</code>" for f in VALID_PKL_FILES)

        text = (
            "📁 Отправь мне ZIP-архив, он должен содержать хотя-бы один из этих файлов:\n\n"
            f"{files_txt}"
        )
        result = self.bot.send_message(
            call.message.chat.id, text, reply_markup=CLEAR_STATE_BTN()
        )
        self.tg.set_state(
            call.message.chat.id, result.id, call.from_user.id, CBT_UPLOAD_ZIP
        )
        self.bot.answer_callback_query(call.id)

    def handle_zip_upload(self, msg: Message):
        self.tg.clear_state(msg.chat.id, msg.from_user.id, True)
        if not check_zip_file(self.tg, msg):
            return
        if not extract_zip_file(self.tg, msg):
            return

        self.bot.send_message(
            msg.chat.id,
            "✅ <b>ZIP-архив успешно загружен</b>",
            reply_markup=K().add(
                B("⚙️ Настройки", callback_data=f"{CBT.PLUGIN_SETTINGS}:{UUID}")
            ),
        )

    def messages_menu(self, call: CallbackQuery):
        keyboard = K()
        for key in AVAILABLE_VARS:
            keyboard.add(
                B(
                    get_text_translate(key),  # type: ignore
                    callback_data=f"{CBT_EDIT_MESSAGE}:{key}",
                )
            )
        keyboard.add(
            B("◀️ Назад", callback_data=f"{CBT.PLUGIN_SETTINGS}:{UUID}")
        )

        self.bot.edit_message_text(
            "<b>💬 Настройка сообщений</b>",
            call.message.chat.id,
            call.message.id,
            reply_markup=keyboard,
        )
        self.bot.answer_callback_query(call.id)

    def edit_message(self, call: CallbackQuery):
        message_key = call.data.split(":")[-1]
        message = self.settings["messages"].get(message_key, "")

        available_vars_keys = AVAILABLE_VARS.get(message_key, [])
        vars_str = "\n".join(
            f"<code>{k}</code> - {v}"
            for k, v in VARS_DICT.items()
            if k.strip("$") in available_vars_keys
        )

        def handler(m: Message):
            if m.text == "-":
                m.text = ""
            self.settings["messages"][message_key] = m.text
            self.save_settings()
            self.bot.send_message(
                m.chat.id,
                "✅",
                reply_markup=K().add(
                    B("◀️ Назад", callback_data=f"{CBT_MESSAGES_MENU}:0")
                ),
            )

        text = (
            f"<b>✏️ Редактирование:</b> <code>{get_text_translate(message_key)}</code>\n\n"  # type: ignore
            f"<b>Текущий текст:</b>\n<code>{message or 'Не установлен'}</code>\n\n"
        )
        if vars_str:
            text += f"<b>Доступные переменные:</b>\n{vars_str}\n\n"
        text += "❓ <i>Отправь <code>-</code> чтобы удалить</i>"

        state_key = f"{CBT_EDIT_MESSAGE}_{message_key}"
        if state_key not in self._tg_states:
            self.tg.msg_handler(
                lambda m: (
                    handler(m),
                    self.tg.clear_state(m.chat.id, m.from_user.id, True),
                ),
                func=lambda m: self.tg.check_state(
                    call.message.chat.id, call.from_user.id, state_key
                ),
            )
            self._tg_states[state_key] = True

        result = self.bot.send_message(
            call.message.chat.id,
            text,
            reply_markup=CLEAR_STATE_BTN(),
        )
        self.tg.set_state(
            call.message.chat.id,
            result.message_id,
            call.from_user.id,
            state_key,
        )
        self.bot.answer_callback_query(call.id)

    def handle_inline_query(self, inline_query: InlineQuery):
        try:
            query = inline_query.query.strip()
            parts = query.split()

            if (
                len(parts) != 3
                or parts[0] != "sc"
                or parts[1] not in INLINE_FILTERS
            ):
                results = []
            else:
                filter_key = parts[1]
                email = parts[2]
                game = INLINE_FILTERS[filter_key]

                if not extract_email(email):
                    results = [
                        InlineQueryResultArticle(
                            id="invalid_email",
                            title="Ошибка",
                            input_message_content=InputTextMessageContent(
                                "❌ Указана некорректная почта"
                            ),
                        )
                    ]
                else:
                    resp, _, _ = send_request(email, game)
                    game_name = keychain.get(game, {}).get("name", game)

                    if resp and resp.status_code == 200:
                        results = [
                            InlineQueryResultArticle(
                                id="success",
                                title=f"Код запрошен для {game_name}",
                                input_message_content=InputTextMessageContent(
                                    f"✅ <b>Код для <code>{email}</code> в <code>{game_name}</code> запрошен</b>"
                                ),
                            )
                        ]
                    else:
                        results = [
                            InlineQueryResultArticle(
                                id="error",
                                title="Ошибка",
                                input_message_content=InputTextMessageContent(
                                    f"❌ <b>Ошибка при запросе кода для <code>{email}</code> в <code>{game_name}</code></b>"
                                ),
                            )
                        ]

            self.bot.answer_inline_query(
                inline_query.id, results, cache_time=1
            )
        except Exception:
            logger.error(
                f"{LP} Ошибка при обработке инлайн запроса", exc_info=True
            )
            self.bot.answer_inline_query(
                inline_query.id,
                [
                    InlineQueryResultArticle(
                        id="error",
                        title="❌ Ошибка",
                        input_message_content=InputTextMessageContent(
                            "❌ <b>Произошла ошибка при обработке запроса :(</b>"
                        ),
                    )
                ],
                cache_time=1,
            )


# Синглтон для разделения состояния между вызовами хэндлеров
_instance: SupercellOTP | None = None


def _get_instance(cardinal: Cardinal) -> SupercellOTP:
    global _instance
    if _instance is None:
        _instance = SupercellOTP(cardinal)
    return _instance


def init(c: Cardinal):
    global _instance
    _instance = SupercellOTP(c)
    requester = _instance

    c.telegram.cbq_handler(
        requester.open_settings,
        lambda cb: f"{CBT.PLUGIN_SETTINGS}:{UUID}" in cb.data,
    )
    c.telegram.cbq_handler(
        requester.switch_auto,
        lambda cb: cb.data.startswith(f"{CBT_SWITCH_AUTO}:"),
    )
    c.telegram.cbq_handler(
        requester.switch_validate,
        lambda cb: cb.data.startswith(f"{CBT_SWITCH_VALIDATE}:"),
    )
    c.telegram.cbq_handler(
        requester.set_group_chat,
        lambda cb: cb.data.startswith(f"{CBT_SET_GROUP}:"),
    )
    c.telegram.cbq_handler(
        requester.upload_zip,
        lambda cb: cb.data.startswith(f"{CBT_UPLOAD_ZIP}:"),
    )
    c.telegram.cbq_handler(
        requester.messages_menu,
        lambda cb: cb.data.startswith(f"{CBT_MESSAGES_MENU}:"),
    )
    c.telegram.cbq_handler(
        requester.edit_message,
        lambda cb: cb.data.startswith(f"{CBT_EDIT_MESSAGE}:"),
    )
    c.telegram.file_handler(CBT_UPLOAD_ZIP, requester.handle_zip_upload)
    c.telegram.msg_handler(
        requester.handle_group_input,
        func=lambda m: c.telegram.check_state(
            m.chat.id, m.from_user.id, CBT_SET_GROUP
        ),
    )

    @requester.bot.inline_handler(
        lambda query: query.query.strip().startswith("sc")
        and len(query.query.strip().split()) == 3
        and query.query.strip().split()[1] in INLINE_FILTERS
    )
    def inline_handler(query):
        requester.handle_inline_query(query)


def handle_new_message(cardinal: Cardinal, e: NewMessageEvent):
    _get_instance(cardinal).handle_message(e)


def handle_new_order(cardinal: Cardinal, e: NewOrderEvent):
    _get_instance(cardinal).handle_new_order(e)


BIND_TO_PRE_INIT = [init]
BIND_TO_NEW_MESSAGE = [handle_new_message]
BIND_TO_NEW_ORDER = [handle_new_order]
BIND_TO_DELETE = None
