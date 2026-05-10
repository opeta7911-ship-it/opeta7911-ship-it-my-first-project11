from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database.models import Cycle, Filter, Lot
from playerok.client import MyLot

INTERVALS = [
    (1,    "Каждую минуту"),
    (5,    "Каждые 5 мин"),
    (10,   "Каждые 10 мин"),
    (30,   "Каждые 30 мин"),
    (60,   "Каждый час"),
    (0,    "Своё значение"),
]


def main_menu(running: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    label = "⏸ Автоподнятие: ВКЛ" if running else "▶️ Автоподнятие: ВЫКЛ"
    kb.button(text=label, callback_data="toggle_engine")
    kb.button(text="🔁 Авто восстановление", callback_data="restore")
    kb.button(text="🎛 Фильтры", callback_data="filters")
    kb.button(text="📊 Статистика", callback_data="stats")
    kb.button(text="📜 История", callback_data="history")
    kb.button(text="⚙️ Настройки", callback_data="settings")
    kb.button(text="🔄 Циклы", callback_data="cycles")
    kb.button(text="🗑 Сброс", callback_data="reset_request")
    kb.adjust(1, 1, 2, 2, 1, 1)
    return kb.as_markup()


def filters_menu(filters: list[Filter]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for f in filters:
        flag = "🟢" if f.enabled else "🔴"
        interval = f"{f.interval_minutes}мин" if f.interval_minutes else "не задан"
        kb.button(
            text=f"{f.name} · {interval}",
            callback_data=f"filter:{f.id}",
        )
        kb.button(
            text=flag,
            callback_data=f"filter_quick_toggle:{f.id}",
        )
    any_enabled = any(f.enabled for f in filters)
    if any_enabled:
        kb.button(text="⛔ Выключить все", callback_data="filters_toggle_all:0")
    else:
        kb.button(text="✅ Включить все", callback_data="filters_toggle_all:1")
    kb.button(text="➕ Создать фильтр", callback_data="filter_create")
    kb.button(text="◀️ Назад", callback_data="back_main")
    kb.adjust(*([2] * len(filters)), 1, 1, 1)
    return kb.as_markup()


def filter_card(flt: Filter) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    toggle = "🔴 Выключить" if flt.enabled else "🟢 Включить"
    kb.button(text=toggle, callback_data=f"filter_toggle:{flt.id}")
    kw_label = f"🔑 Ключ: {flt.keyword[:22]}" if flt.keyword else "🔑 Ключевое слово"
    kb.button(text=kw_label, callback_data=f"filter_keyword:{flt.id}")
    kb.button(text="📋 Логи поднятий", callback_data=f"filter_logs:{flt.id}:0")
    kb.button(text="⏱ Когда поднимать", callback_data=f"filter_interval_menu:{flt.id}")
    kb.button(text=f"🛒 Лотов за раз: {flt.lots_per_trigger}", callback_data=f"filter_lpt_menu:{flt.id}")
    if flt.spend_limit_kopecks is not None:
        lim = flt.spend_limit_kopecks // 100
        spent = flt.spent_kopecks // 100
        kb.button(text=f"💰 Лимит/сутки: {spent}₽/{lim}₽", callback_data=f"filter_limit:{flt.id}")
    else:
        kb.button(text="💰 Лимит/сутки: нет", callback_data=f"filter_limit:{flt.id}")
    top_label = f"🎯 Топ-{flt.top_position}" if flt.top_position else "🎯 Топ: выкл"
    kb.button(text=top_label, callback_data=f"filter_top:{flt.id}")
    kb.button(text="🔄 В цикл", callback_data=f"filter_cycle_assign:{flt.id}")
    kb.button(text="🗑 Удалить", callback_data=f"filter_delete:{flt.id}")
    kb.button(text="◀️ Назад", callback_data="filters")
    kb.adjust(1, 1, 1, 2, 1, 1, 1, 1, 1)
    return kb.as_markup()


def lot_selection_menu(
    flt_id: int,
    lots: list[MyLot],
    selected_ids: set[str],
    page: int,
    total: int,
    page_size: int = 8,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for lot in lots:
        check = "☑" if lot.playerok_id in selected_ids else "☐"
        kb.button(
            text=f"{check} {lot.name[:35]} · {lot.price_kopecks//100}₽",
            callback_data=f"lot_toggle:{flt_id}:{lot.playerok_id}:{page}",
        )
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"filter_lots_fetch:{flt_id}:{page-1}"))
    nav.append(InlineKeyboardButton(
        text=f"{page+1}/{(total + page_size - 1) // page_size}",
        callback_data="noop",
    ))
    if (page + 1) * page_size < total:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"filter_lots_fetch:{flt_id}:{page+1}"))
    kb.adjust(1)
    kb.row(*nav)
    kb.row(InlineKeyboardButton(text="✅ Готово", callback_data=f"filter:{flt_id}"))
    return kb.as_markup()


def interval_menu(flt_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for minutes, label in INTERVALS:
        cb = f"filter_interval_set:{flt_id}:{minutes}" if minutes > 0 else f"filter_interval_custom:{flt_id}"
        kb.button(text=label, callback_data=cb)
    kb.button(text="◀️ Назад", callback_data=f"filter:{flt_id}")
    kb.adjust(2, 2, 2, 1)
    return kb.as_markup()


def lots_per_trigger_menu(flt_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for n in [1, 2, 3, 5, 10]:
        kb.button(text=str(n), callback_data=f"filter_lpt_set:{flt_id}:{n}")
    kb.button(text="◀️ Назад", callback_data=f"filter:{flt_id}")
    kb.adjust(5, 1)
    return kb.as_markup()


def top_position_menu(flt_id: int, current: int | None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for n in [3, 5, 10, 20]:
        mark = " ✅" if current == n else ""
        kb.button(text=f"Топ-{n}{mark}", callback_data=f"filter_top_set:{flt_id}:{n}")
    kb.button(text="❌ Выключить", callback_data=f"filter_top_set:{flt_id}:0")
    kb.button(text="◀️ Назад", callback_data=f"filter:{flt_id}")
    kb.adjust(4, 1, 1)
    return kb.as_markup()


def filter_cycle_assign_menu(flt_id: int, cycles: list[Cycle], current_cycle_id: int | None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for c in cycles:
        check = "☑" if c.id == current_cycle_id else "☐"
        kb.button(text=f"{check} {c.name}", callback_data=f"filter_cycle_set:{flt_id}:{c.id}")
    if current_cycle_id:
        kb.button(text="✖ Убрать из цикла", callback_data=f"filter_cycle_set:{flt_id}:0")
    kb.button(text="◀️ Назад", callback_data=f"filter:{flt_id}")
    kb.adjust(1)
    return kb.as_markup()


def cycles_menu(cycles: list[Cycle]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for c in cycles:
        flag = "🟢" if c.enabled else "🔴"
        kb.button(
            text=f"{flag} {c.name} ({c.start_time}, {c.duration_minutes}мин)",
            callback_data=f"cycle:{c.id}",
        )
    kb.button(text="➕ Создать цикл", callback_data="cycle_create")
    kb.button(text="◀️ Назад", callback_data="back_main")
    kb.adjust(1)
    return kb.as_markup()


def cycle_card(cycle: Cycle, all_filters: list[Filter]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text=("🔴 Выключить" if cycle.enabled else "🟢 Включить"),
        callback_data=f"cycle_toggle:{cycle.id}",
    )
    kb.button(text="🕒 Время старта", callback_data=f"cycle_start:{cycle.id}")
    kb.button(text="⏱ Длительность", callback_data=f"cycle_duration:{cycle.id}")
    kb.button(text="🗑 Удалить", callback_data=f"cycle_delete:{cycle.id}")
    in_cycle_ids = {f.id for f in cycle.filters}
    for f in all_filters:
        flag = "☑" if f.id in in_cycle_ids else "☐"
        kb.button(text=f"{flag} {f.name}", callback_data=f"cycle_togglefilter:{cycle.id}:{f.id}")
    kb.button(text="◀️ Назад", callback_data="cycles")
    kb.adjust(1, 2, 1, 1)
    return kb.as_markup()


def stats_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for label, code in [
        ("Сегодня", "today"), ("Вчера", "yesterday"),
        ("3 дня", "3d"), ("5 дней", "5d"),
        ("7 дней", "7d"), ("2 недели", "14d"),
        ("Всё время", "all"),
    ]:
        kb.button(text=label, callback_data=f"stats:{code}")
    kb.button(text="◀️ Назад", callback_data="back_main")
    kb.adjust(2, 2, 2, 1, 1)
    return kb.as_markup()


def reset_confirm_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, сбросить всё", callback_data="reset_confirm")
    kb.button(text="❌ Отмена", callback_data="back_main")
    kb.adjust(1)
    return kb.as_markup()


def back_button(target: str = "back_main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data=target)]]
    )
