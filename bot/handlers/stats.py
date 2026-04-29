from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from bot.keyboards.menus import back_button, stats_menu
from database.db import Database
from database.models import BumpHistory, Lot

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
    await call.message.edit_text(
        "<b>Статистика</b>\n\nВыбери период:", reply_markup=stats_menu()
    )
    await call.answer()


@router.callback_query(F.data.startswith("stats:"))
async def cb_stats_period(call: CallbackQuery, db: Database) -> None:
    code = call.data.split(":")[1]
    label, frm = PERIODS[code]
    now = datetime.utcnow()
    start = frm(now)
    end = now if code != "yesterday" else now.replace(hour=0, minute=0, second=0, microsecond=0)

    async with db.session_factory() as session:
        q = select(
            func.count(BumpHistory.id),
            func.sum(BumpHistory.cost_kopecks),
            func.sum(func.case((BumpHistory.success.is_(True), 1), else_=0)),
        ).where(BumpHistory.occurred_at >= start, BumpHistory.occurred_at <= end)
        total, spent, success = (await session.execute(q)).one()

    text = (
        f"<b>Статистика — {label}</b>\n\n"
        f"Всего попыток: {total or 0}\n"
        f"Успешных поднятий: {success or 0}\n"
        f"Потрачено: {(spent or 0) / 100:.0f}₽"
    )
    await call.message.edit_text(text, reply_markup=stats_menu())
    await call.answer()


@router.callback_query(F.data == "history")
async def cb_history(call: CallbackQuery, db: Database) -> None:
    async with db.session_factory() as session:
        result = await session.execute(
            select(BumpHistory)
            .order_by(BumpHistory.occurred_at.desc())
            .limit(20)
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
        await call.message.edit_text(
            "История пуста.", reply_markup=back_button()
        )
        await call.answer()
        return

    lines = ["<b>Последние 20 поднятий</b>", ""]
    for h in items:
        lot = lots_map.get(h.lot_id)
        time = h.occurred_at.strftime("%d.%m %H:%M")
        if lot is None:
            lines.append(f"[{time}] (лот удалён)")
            continue
        if h.success:
            lines.append(
                f"🚀 [{time}] <a href=\"{lot.url}\">{lot.name[:50]}</a> "
                f"({h.cost_kopecks/100:.0f}₽ / {lot.price_kopecks/100:.0f}₽)"
            )
        else:
            lines.append(
                f"❌ [{time}] <a href=\"{lot.url}\">{lot.name[:50]}</a> — {h.error or 'ошибка'}"
            )
    await call.message.edit_text(
        "\n".join(lines), reply_markup=back_button(), disable_web_page_preview=True
    )
    await call.answer()
