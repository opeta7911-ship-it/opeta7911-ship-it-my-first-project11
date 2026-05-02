from aiogram.fsm.state import State, StatesGroup


class FilterCreate(StatesGroup):
    waiting_for_name = State()


class FilterEditLimit(StatesGroup):
    waiting_for_amount = State()


class FilterEditIntervalCustom(StatesGroup):
    waiting_for_minutes = State()


class CycleCreate(StatesGroup):
    waiting_for_name = State()
    waiting_for_start = State()
    waiting_for_duration = State()


class CycleEditStart(StatesGroup):
    waiting_for_start = State()


class CycleEditDuration(StatesGroup):
    waiting_for_minutes = State()


class FilterEditKeyword(StatesGroup):
    waiting_for_keyword = State()
