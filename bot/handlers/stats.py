from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from sqlalchemy import case, func, select

from bot.keyboards.menus import back_button, stats_menu
from database.db import Database
from database.models import BumpHistory, Filter, Lot

router = Router()

PERIODS = {
    "today": ("Сегодня", lambda now: now.replace(hour=0, minute=0, second=0, microsecond=0)),
    "yesterday": (
        "Вчера",
        lambda now: (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0),
    ),
    "3d": ("3 дня", lambda now: now - timedelta(days=3)),
    "5d": ("5 дней", lambda now: now - timedelta(days=5)),
    "7d": ("7 дней", lambda now: now - timedelta(days=7)),
    "14d": ("2 недели", lambda now: now - timedelta(days=14)),
    "all": ("Всё время", lambda now: datetime(1970, 1, 1)),
}


async def show_stats_menu(message: Message) -> None:
    await message.answer(
        "<b>Статистика</b>\n\nВыбери период:", reply_markup=stats_menu()
    )


@router.callback_query(F.data == "stats")
async def cb_stats(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.edit_text(
        "<b>Статистика</b>\n\nВыбери период:", reply_markup=stats_menu()
    )


@router.callback_query(F.data.startswith("stats:"))
async def cb_stats_period(call: CallbackQuery, db: Database) -> None:
    await call.answer()
    code = call.data.split(":")[1]
    label, frm = PERIODS[code]
    now = datetime.utcnow()
    start = frm(now)
    end = now if code != "yesterday" else now.replace(hour=0, minute=0, second=0, microsecond=0)

    async with db.session_factory() as session:
        # Overall totals
        totals_q = select(
            func.count(BumpHistory.id),
            func.sum(BumpHistory.cost_kopecks),
            func.sum(case((BumpHistory.success == True, 1), else_=0)),
            func.sum(case((BumpHistory.success == False, 1), else_=0)),
        ).where(BumpHistory.occurred_at >= start, BumpHistory.occurred_at <= end)
        total, spent, success, failed = (await session.execute(totals_q)).one()

        # Per-filter breakdown: filter name, bumps, cost
        per_filter_q = (
            select(
                Filter.name,
                func.count(BumpHistory.id),
                func.sum(BumpHistory.cost_kopecks),
                func.sum(case((BumpHistory.success == True, 1), else_=0)),
            )
            .join(Lot, Lot.id == BumpHistory.lot_id)
            .join(Filter, Filter.id == Lot.filter_id)
            .where(BumpHistory.occurred_at >= start, BumpHistory.occurred_at <= end)
            .group_by(Filter.id)
            .order_by(func.count(BumpHistory.id).desc())
        )
        filter_rows = (await session.execute(per_filter_q)).all()

    total = total or 0
    spent = spent or 0
    success = success or 0
    failed = failed or 0

    lines = [
        f"<b>Статистика — {label}</b>",
        "",
        f"Поднятий всего: <b>{total}</b>",
        f"✅ Успешных: <b>{success}</b>",
        f"❌ Ошибок: <b>{failed}</b>",
        f"💸 Потрачено: <b>{spent / 100:.0f}₽</b>",
    ]

    if filter_rows:
        lines.append("")
        lines.append("<b>По фильтрам:</b>")
        for fname, cnt, fcost, fok in filter_rows:
            fcost = fcost or 0
            lines.append(
                f"  • {fname[:35]}: {fok}/{cnt} поднятий, {fcost/100:.0f}₽"
            )

    await call.message.edit_text(
        "\n".join(lines), reply_markup=stats_menu()
    )


@router.callback_query(F.data == "history")
async def cb_history(call: CallbackQuery, db: Database) -> None:
    await call.answer()
    async with db.session_factory() as session:
        result = await session.execute(
            select(BumpHistory)
            .order_by(BumpHistory.occurred_at.desc())
            .limit(30)
        )
        items = result.scalars().all()
        if items:
            lots_q = await session.execute(
                select(Lot).where(Lot.id.in_([h.lot_id for h in items]))
            )
            lots_map = {l.id: l for l in lots_q.scalars()}
        else:
            lots_map = {}

    if not items:
        await call.message.edit_text("История пуста.", reply_markup=back_button())
        return

    lines = ["<b>Последние 30 поднятий</b>", ""]
    for h in items:
        lot = lots_map.get(h.lot_id)
        time = h.occurred_at.strftime("%d.%m %H:%M")
        if lot is None:
            lines.append(f"[{time}] лот удалён")
            continue
        name = lot.name[:40]
        if h.success:
            lines.append(
                f'🚀 [{time}] <a href="{lot.url}">{name}</a> '
                f"({h.cost_kopecks/100:.0f}₽/{lot.price_kopecks/100:.0f}₽)"
            )
        else:
            lines.append(
                f'❌ [{time}] <a href="{lot.url}">{name}</a>\n'
                f"        {h.error or 'ошибка'}"
            )

    text = "\n".join(lines)
    # Telegram message limit 4096
    if len(text) > 4000:
        text = text[:4000] + "\n…"
    await call.message.edit_text(
        text, reply_markup=back_button(), disable_web_page_preview=True
    )
