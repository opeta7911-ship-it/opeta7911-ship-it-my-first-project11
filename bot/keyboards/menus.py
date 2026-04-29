from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database.models import Cycle, Filter, Lot


def main_menu(running: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    label = "⏸ Остановить автоподнятие" if running else "▶️ Запустить автоподнятие"
    kb.button(text=label, callback_data="toggle_engine")
    kb.button(text="🎛 Фильтры", callback_data="filters")
    kb.button(text="🔄 Циклы", callback_data="cycles")
    kb.button(text="📊 Статистика", callback_data="stats")
    kb.button(text="📜 История", callback_data="history")
    kb.button(text="⚙️ Настройки", callback_data="settings")
    kb.adjust(1, 2, 2, 1)
    return kb.as_markup()


def filters_menu(filters: list[Filter]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for f in filters:
        flag = "✅" if f.enabled else "⛔"
        kb.button(
            text=f"{flag} {f.name} ({len(f.lots)} лот.)",
            callback_data=f"filter:{f.id}",
        )
    kb.button(text="➕ Создать фильтр", callback_data="filter_create")
    kb.button(text="◀️ Назад", callback_data="back_main")
    kb.adjust(1)
    return kb.as_markup()


def filter_card(flt: Filter) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text=("⛔ Выключить" if flt.enabled else "✅ Включить"),
        callback_data=f"filter_toggle:{flt.id}",
    )
    kb.button(text="➕ Добавить лот", callback_data=f"filter_addlot:{flt.id}")
    kb.button(text="📋 Лоты", callback_data=f"filter_lots:{flt.id}")
    kb.button(text="💰 Лимит расходов", callback_data=f"filter_limit:{flt.id}")
    kb.button(text="⏱ Интервал", callback_data=f"filter_interval:{flt.id}")
    kb.button(text="🕒 Время старта", callback_data=f"filter_start:{flt.id}")
    kb.button(text="🗑 Удалить", callback_data=f"filter_delete:{flt.id}")
    kb.button(text="◀️ Назад", callback_data="filters")
    kb.adjust(1, 2, 2, 1, 1, 1)
    return kb.as_markup()


def lots_menu(flt_id: int, lots: list[Lot]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for l in lots:
        flag = "⏸" if l.paused else "▶️"
        kb.button(text=f"{flag} {l.name[:40]}", callback_data=f"lot:{l.id}")
    kb.button(text="◀️ Назад", callback_data=f"filter:{flt_id}")
    kb.adjust(1)
    return kb.as_markup()


def lot_card(lot: Lot) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text=("▶️ Снять с паузы" if lot.paused else "⏸ Пауза"),
        callback_data=f"lot_pause:{lot.id}",
    )
    kb.button(text="🗑 Удалить", callback_data=f"lot_delete:{lot.id}")
    kb.button(text="◀️ Назад", callback_data=f"filter_lots:{lot.filter_id}")
    kb.adjust(2, 1)
    return kb.as_markup()


def cycles_menu(cycles: list[Cycle]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for c in cycles:
        flag = "✅" if c.enabled else "⛔"
        kb.button(
            text=f"{flag} {c.name} ({c.start_time}, {c.duration_minutes}мин)",
            callback_data=f"cycle:{c.id}",
        )
    kb.button(text="➕ Создать цикл", callback_data="cycle_create")
    kb.button(text="◀️ Назад", callback_data="back_main")
    kb.adjust(1)
    return kb.as_markup()


def cycle_card(cycle: Cycle, available_filters: list[Filter]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text=("⛔ Выключить" if cycle.enabled else "✅ Включить"),
        callback_data=f"cycle_toggle:{cycle.id}",
    )
    kb.button(text="🕒 Время старта", callback_data=f"cycle_start:{cycle.id}")
    kb.button(text="⏱ Длительность", callback_data=f"cycle_duration:{cycle.id}")
    kb.button(text="🗑 Удалить", callback_data=f"cycle_delete:{cycle.id}")
    in_cycle_ids = {f.id for f in cycle.filters}
    for f in available_filters:
        flag = "☑" if f.id in in_cycle_ids else "☐"
        kb.button(
            text=f"{flag} {f.name}",
            callback_data=f"cycle_togglefilter:{cycle.id}:{f.id}",
        )
    kb.button(text="◀️ Назад", callback_data="cycles")
    kb.adjust(1, 2, 1, 1)
    return kb.as_markup()


def stats_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for label, code in [
        ("Сегодня", "today"),
        ("Вчера", "yesterday"),
        ("3 дня", "3d"),
        ("5 дней", "5d"),
        ("7 дней", "7d"),
        ("2 недели", "14d"),
        ("Всё время", "all"),
    ]:
        kb.button(text=label, callback_data=f"stats:{code}")
    kb.button(text="◀️ Назад", callback_data="back_main")
    kb.adjust(2, 2, 2, 1, 1)
    return kb.as_markup()


def back_button(target: str = "back_main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data=target)]]
    )
