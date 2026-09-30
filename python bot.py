# Обновленная версия: интервал 0.01с, убраны строки Интервал и пояснение в статусе
import asyncio
import logging
import time
from typing import Dict, Any, Set, List
import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery, 
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage


MONITORING_INTERVAL = 0.01


PAYMENT_METHODS = {
    "ALL": "Все методы оплаты",
    "Tinkoff": "Т-Банк (Тинькофф)",
    "Sber": "Сбербанк",
    "SBP": "СБП (Система быстрых платежей)",
    "Raiffeisen": "Райффайзенбанк",
    "Alfa": "Альфа-Банк",
    "VTB": "ВТБ",
    "YooMoney": "ЮMoney",
}


EXCHANGES = {
    "binance": "Binance",
    "bybit": "Bybit",
    "htx": "HTX",
}


user_settings: Dict[int, Dict[str, Any]] = {}


def get_user_config(user_id: int) -> Dict[str, Any]:
    if user_id not in user_settings:
        user_settings[user_id] = {
            "min_price": None,
            "max_price": None,
            "min_limit": None,
            "max_limit": None,
            "exchanges": ["binance", "bybit", "htx"],
            "payments": ["ALL"],
            "is_paused": False,
            "seen_orders": set(),
        }
    return user_settings[user_id]


class FilterStates(StatesGroup):
    waiting_for_price = State()
    waiting_for_limits = State()
    confirm_price = State()
    confirm_limits = State()


def build_reply_keyboard(is_paused: bool = False) -> ReplyKeyboardMarkup:
    pause_text = "▶️ Возобновить" if is_paused else "⏸ Пауза"
    keyboard = [
        [KeyboardButton(text="📋 Главное меню"), KeyboardButton(text="💰 Фильтр цены")],
        [KeyboardButton(text="📊 Фильтр лимитов"), KeyboardButton(text="💳 Методы оплаты")],
        [KeyboardButton(text="🏛 Выбор бирж"), KeyboardButton(text=pause_text)]
    ]
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите действие в меню 👇"
    )


def build_main_menu_text(cfg: Dict[str, Any]) -> str:
    min_p = f"{cfg['min_price']:.2f} ₽" if cfg['min_price'] is not None else "0.00 ₽"
    max_p = f"{cfg['max_price']:.2f} ₽" if cfg['max_price'] is not None else "Без ограничений"
    price_str = f"{min_p} — {max_p}"


    min_l = f"{cfg['min_limit']:,.0f} ₽".replace(",", " ") if cfg['min_limit'] is not None else "0 ₽"
    max_l = f"{cfg['max_limit']:,.0f} ₽".replace(",", " ") if cfg['max_limit'] is not None else "Без ограничений"
    limits_str = f"{min_l} — {max_l}" if (cfg['min_limit'] is not None or cfg['max_limit'] is not None) else "Любая сумма"


    ex_names = [EXCHANGES[e] for e in cfg['exchanges'] if e in EXCHANGES]
    ex_str = ", ".join(ex_names) if ex_names else "❌ Не выбрано"


    if "ALL" in cfg['payments']:
        pay_str = "Все способы оплаты"
    else:
        pay_names = [PAYMENT_METHODS[p] for p in cfg['payments'] if p in PAYMENT_METHODS]
        pay_str = ", ".join(pay_names) if pay_names else "❌ Не выбрано"


    status_str = "⏸ НА ПАУЗЕ" if cfg['is_paused'] else "🟢 РАБОТАЕТ"


    text = (
        "📊 <b>ТАБЛИЦА НАСТРОЕК МОНИТОРИНГА P2P (USDT / RUB)</b>\n"
        "───────────────────────────────\n"
        f"💰 <b>Диапазон цены:</b> <code>{price_str}</code>\n"
        f"📊 <b>Лимиты на сумму:</b> <code>{limits_str}</code>\n"
        f"💳 <b>Методы оплаты:</b> <code>{pay_str}</code>\n"
        f"🏛 <b>Выбранные биржи:</b> <code>{ex_str}</code>\n"
        f"📡 <b>Статус:</b> <b>{status_str}</b>\n"
        "───────────────────────────────\n"
        "<i>Используйте кнопки под полем ввода или ниже для управления:</i>"
    )
    return text


def build_main_keyboard(cfg: Dict[str, Any]) -> InlineKeyboardMarkup:
    pause_btn_text = "▶️ Возобновить" if cfg['is_paused'] else "⏸ Поставить на паузу"
    keyboard = [
        [InlineKeyboardButton(text="✏️ Редактировать диапазон цены", callback_data="edit_price")],
        [InlineKeyboardButton(text="📊 Редактировать лимиты суммы", callback_data="edit_limits")],
        [InlineKeyboardButton(text="💳 Настроить методы оплаты", callback_data="edit_payments")],
        [InlineKeyboardButton(text="🏛 Выбрать биржи", callback_data="edit_exchanges")],
        [InlineKeyboardButton(text=pause_btn_text, callback_data="toggle_pause")],
        [InlineKeyboardButton(text="🔄 Обновить меню", callback_data="refresh_menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def build_confirm_keyboard(edit_callback: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✏️ Редактировать", callback_data=edit_callback),
            InlineKeyboardButton(text="✅ Готово", callback_data="back_to_main")
        ]
    ])


def build_payments_keyboard(cfg: Dict[str, Any]) -> InlineKeyboardMarkup:
    buttons = []
    all_selected = "ALL" in cfg['payments']
    buttons.append([InlineKeyboardButton(
        text=f"{'✅' if all_selected else '⚪️'} {PAYMENT_METHODS['ALL']}",
        callback_data="toggle_pay:ALL"
    )])


    for code, name in PAYMENT_METHODS.items():
        if code == "ALL":
            continue
        is_sel = (code in cfg['payments']) and not all_selected
        buttons.append([InlineKeyboardButton(
            text=f"{'✅' if is_sel else '⚪️'} {name}",
            callback_data=f"toggle_pay:{code}"
        )])


    buttons.append([
        InlineKeyboardButton(text="✏️ Редактировать еще", callback_data="edit_payments"),
        InlineKeyboardButton(text="✅ Готово", callback_data="back_to_main")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_exchanges_keyboard(cfg: Dict[str, Any]) -> InlineKeyboardMarkup:
    buttons = []
    for ex_code, ex_name in EXCHANGES.items():
        is_on = ex_code in cfg['exchanges']
        buttons.append([InlineKeyboardButton(
            text=f"{'✅' if is_on else '❌'} {ex_name}",
            callback_data=f"toggle_ex:{ex_code}"
        )])
    buttons.append([
        InlineKeyboardButton(text="✏️ Редактировать еще", callback_data="edit_exchanges"),
        InlineKeyboardButton(text="✅ Готово", callback_data="back_to_main")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def fetch_binance(session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
    url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    payload = {
        "asset": "USDT",
        "fiat": "RUB",
        "merchantCheck": False,
        "page": 1,
        "rows": 10,
        "payTypes": [],
        "publisherType": None,
        "tradeType": "BUY"
    }
    results = []
    try:
        async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=2)) as resp:
            if resp.status == 200:
                data = await resp.json()
                for item in data.get("data", []):
                    adv = item.get("adv", {})
                    advertiser = item.get("advertiser", {})
                    adv_no = adv.get("advNo")
                    if not adv_no:
                        continue
                    price = float(adv.get("price", 0))
                    min_limit = float(adv.get("minSingleTransAmount", 0))
                    max_limit = float(adv.get("maxSingleTransAmount", 0))
                    methods = [m.get("tradeMethodName", "") for m in adv.get("tradeMethods", []) if m.get("tradeMethodName")]
                    user_no = advertiser.get("userNo")
                    order_url = f"https://p2p.binance.com/ru/advertiserDetail?userNo={user_no}" if user_no else f"https://p2p.binance.com/ru/trade/{adv_no}"
                    results.append({
                        "exchange": "Binance",
                        "order_id": f"binance_{adv_no}",
                        "order_url": order_url,
                        "price": price,
                        "min_limit": min_limit,
                        "max_limit": max_limit,
                        "methods": methods or ["Не указан"]
                    })
    except Exception as e:
        logging.debug(f"Binance error: {e}")
    return results


async def fetch_bybit(session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
    url = "https://api2.bybit.com/fiat/otc/item/online"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://www.bybit.com",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    payload = {
        "userId": "",
        "tokenId": "USDT",
        "currencyId": "RUB",
        "payment": [],
        "side": "1",
        "size": "10",
        "page": "1",
        "amount": "",
        "authMaker": False,
        "canTrade": False
    }
    results = []
    try:
        async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=2)) as resp:
            if resp.status == 200:
                data = await resp.json()
                items = data.get("result", {}).get("items", [])
                for item in items:
                    order_id = item.get("id")
                    user_id = item.get("userId")
                    if not order_id:
                        continue
                    price = float(item.get("price", 0))
                    min_limit = float(item.get("minAmount", 0))
                    max_limit = float(item.get("maxAmount", 0))
                    raw_payments = item.get("payments", [])
                    methods = [str(p) for p in raw_payments]
                    order_url = f"https://www.bybit.com/fiat/trade/otc/profile/{user_id}" if user_id else f"https://www.bybit.com/fiat/trade/otc/?actionType=1&token=USDT&fiat=RUB"
                    results.append({
                        "exchange": "Bybit",
                        "order_id": f"bybit_{order_id}",
                        "order_url": order_url,
                        "price": price,
                        "min_limit": min_limit,
                        "max_limit": max_limit,
                        "methods": methods or ["Банковский перевод"]
                    })
    except Exception as e:
        logging.debug(f"Bybit error: {e}")
    return results


async def fetch_htx(session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
    url = "https://otc-api.htx.com/v1/data/trade-market"
    params = {
        "coinId": "2",
        "currency": "11",
        "tradeType": "sell",
        "currPage": "1",
        "payMethod": "0",
        "acceptOrder": "-1",
        "blockType": "general",
        "online": "1"
    }
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    results = []
    try:
        async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=2)) as resp:
            if resp.status == 200:
                data = await resp.json()
                items = data.get("data", [])
                for item in items:
                    order_id = item.get("id")
                    uid = item.get("uid")
                    if not order_id:
                        continue
                    price = float(item.get("price", 0))
                    min_limit = float(item.get("minTradeLimit", 0))
                    max_limit = float(item.get("maxTradeLimit", 0))
                    pay_methods = [pm.get("name", "") for pm in item.get("payMethods", []) if pm.get("name")]
                    order_url = f"https://www.htx.com/ru-ru/fiat-crypto/trader/{uid}" if uid else "https://www.htx.com/ru-ru/fiat-crypto/trade/buy-usdt-rub"
                    results.append({
                        "exchange": "HTX",
                        "order_id": f"htx_{order_id}",
                        "order_url": order_url,
                        "price": price,
                        "min_limit": min_limit,
                        "max_limit": max_limit,
                        "methods": pay_methods or ["Все способы"]
                    })
    except Exception as e:
        logging.debug(f"HTX error: {e}")
    return results


def matches_filters(order: Dict[str, Any], cfg: Dict[str, Any]) -> bool:
    price = order["price"]
    if cfg["min_price"] is not None and price < cfg["min_price"]:
        return False
    if cfg["max_price"] is not None and price > cfg["max_price"]:
        return False


    order_min = order["min_limit"]
    order_max = order["max_limit"]
    if cfg["min_limit"] is not None and order_max < cfg["min_limit"]:
        return False
    if cfg["max_limit"] is not None and order_min > cfg["max_limit"]:
        return False


    if "ALL" not in cfg["payments"]:
        methods_text = " ".join(order["methods"]).lower()
        matched = False
        for p in cfg["payments"]:
            kw = PAYMENT_METHODS.get(p, "").lower()
            if p.lower() in methods_text or kw in methods_text:
                matched = True
                break
        if not matched:
            return False


    return True


async def monitoring_worker(bot: Bot):
    logging.info(f"Фоновый воркер мониторинга запущен ({MONITORING_INTERVAL}с)")
    async with aiohttp.ClientSession() as session:
        while True:
            start_time = time.time()
            try:
                active_users = [
                    (uid, cfg) for uid, cfg in user_settings.items()
                    if not cfg["is_paused"] and cfg["exchanges"]
                ]


                if active_users:
                    needed_exchanges = set()
                    for _, cfg in active_users:
                        needed_exchanges.update(cfg["exchanges"])


                    tasks = []
                    if "binance" in needed_exchanges:
                        tasks.append(fetch_binance(session))
                    if "bybit" in needed_exchanges:
                        tasks.append(fetch_bybit(session))
                    if "htx" in needed_exchanges:
                        tasks.append(fetch_htx(session))


                    exchange_results = await asyncio.gather(*tasks, return_exceptions=True)
                    all_orders: List[Dict[str, Any]] = []
                    for res in exchange_results:
                        if isinstance(res, list):
                            all_orders.extend(res)


                    for uid, cfg in active_users:
                        for order in all_orders:
                            ex_key = order["exchange"].lower()
                            if ex_key not in cfg["exchanges"]:
                                continue


                            unique_key = f"{order['order_id']}_{order['price']}_{order['min_limit']}_{order['max_limit']}"
                            if unique_key in cfg["seen_orders"]:
                                continue


                            if matches_filters(order, cfg):
                                cfg["seen_orders"].add(unique_key)
                                if len(cfg["seen_orders"]) > 2000:
                                    cfg["seen_orders"].clear()


                                methods_str = ", ".join(order["methods"])
                                msg_text = (
                                    f"🏛 Биржа: {order['exchange']}\n"
                                    f"🔗 Ссылка на ордер: {order['order_url']}\n"
                                    f"💵 Цена в рублях за 1 usdt: {order['price']:.2f} ₽\n"
                                    f"💳 Методы оплаты: {methods_str}\n"
                                    f"📊 Лимит: {order['min_limit']:,.0f} – {order['max_limit']:,.0f} ₽".replace(",", " ")
                                )
                                try:
                                    await bot.send_message(
                                        chat_id=uid,
                                        text=msg_text,
                                        disable_web_page_preview=False
                                    )
                                except Exception as send_err:
                                    logging.debug(f"Ошибка отправки сообщения: {send_err}")


            except Exception as e:
                logging.error(f"Ошибка в цикле мониторинга: {e}")


            elapsed = time.time() - start_time
            sleep_duration = max(0.001, MONITORING_INTERVAL - elapsed)
            await asyncio.sleep(sleep_duration)


dp = Dispatcher(storage=MemoryStorage())


@dp.message(StateFilter("*"), CommandStart())
@dp.message(StateFilter("*"), F.text == "📋 Главное меню")
async def handle_start_or_menu(message: Message, state: FSMContext):
    await state.clear()
    cfg = get_user_config(message.from_user.id)
    text = build_main_menu_text(cfg)
    keyboard = build_main_keyboard(cfg)
    reply_kb = build_reply_keyboard(cfg["is_paused"])
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
    await message.answer("📌 Панель быстрого доступа закреплена внизу экрана.", reply_markup=reply_kb)


@dp.callback_query(StateFilter("*"), F.data == "refresh_menu")
@dp.callback_query(StateFilter("*"), F.data == "back_to_main")
async def handle_back_to_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    cfg = get_user_config(callback.from_user.id)
    text = build_main_menu_text(cfg)
    keyboard = build_main_keyboard(cfg)
    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@dp.message(StateFilter("*"), F.text == "💰 Фильтр цены")
@dp.callback_query(StateFilter("*"), F.data == "edit_price")
async def handle_edit_price(event: Message | CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(FilterStates.waiting_for_price)
    prompt_text = (
        "💰 <b>Редактирование диапазона цены (в рублях за 1 USDT)</b>\n\n"
        "Отправьте диапазон <b>ОТ и ДО</b> через пробел.\n\n"
        "Примеры:\n"
        "• <code>90 95.5</code> — искать от 90.00 до 95.50 ₽\n"
        "• <code>92 0</code> — от 92.00 ₽ без верхнего предела\n"
        "• <code>0 0</code> — сбросить фильтр цены"
    )
    if isinstance(event, CallbackQuery):
        await event.message.answer(prompt_text, parse_mode="HTML")
        await event.answer()
    else:
        await event.answer(prompt_text, parse_mode="HTML")


@dp.message(StateFilter("*"), F.text == "📊 Фильтр лимитов")
@dp.callback_query(StateFilter("*"), F.data == "edit_limits")
async def handle_edit_limits(event: Message | CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(FilterStates.waiting_for_limits)
    prompt_text = (
        "📊 <b>Настройка лимитов сделки (в рублях)</b>\n\n"
        "Отправьте желаемый диапазон <b>ОТ и ДО</b> через пробел, либо одно число (сумму сделки).\n\n"
        "Примеры:\n"
        "• <code>10000 50000</code> — ордеры от 10 000 до 50 000 ₽\n"
        "• <code>25000</code> — ордеры, куда укладывается сумма 25 000 ₽\n"
        "• <code>0 0</code> — сбросить лимит (любая сумма)"
    )
    if isinstance(event, CallbackQuery):
        await event.message.answer(prompt_text, parse_mode="HTML")
        await event.answer()
    else:
        await event.answer(prompt_text, parse_mode="HTML")


@dp.message(StateFilter("*"), F.text == "💳 Методы оплаты")
@dp.callback_query(StateFilter("*"), F.data == "edit_payments")
async def handle_edit_payments(event: Message | CallbackQuery, state: FSMContext):
    await state.clear()
    cfg = get_user_config(event.from_user.id)
    text = "💳 <b>Выбор методов оплаты:</b>"
    keyboard = build_payments_keyboard(cfg)
    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        except Exception:
            await event.message.answer(text, reply_markup=keyboard, parse_mode="HTML")
        await event.answer()
    else:
        await event.answer(text, reply_markup=keyboard, parse_mode="HTML")


@dp.message(StateFilter("*"), F.text == "🏛 Выбор бирж")
@dp.callback_query(StateFilter("*"), F.data == "edit_exchanges")
async def handle_edit_exchanges(event: Message | CallbackQuery, state: FSMContext):
    await state.clear()
    cfg = get_user_config(event.from_user.id)
    text = "🏛 <b>Выбор бирж для мониторинга:</b>"
    keyboard = build_exchanges_keyboard(cfg)
    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        except Exception:
            await event.message.answer(text, reply_markup=keyboard, parse_mode="HTML")
        await event.answer()
    else:
        await event.answer(text, reply_markup=keyboard, parse_mode="HTML")


@dp.message(StateFilter("*"), F.text.in_({"⏸ Пауза", "▶️ Возобновить"}))
@dp.callback_query(StateFilter("*"), F.data == "toggle_pause")
async def handle_toggle_pause(event: Message | CallbackQuery, state: FSMContext):
    await state.clear()
    cfg = get_user_config(event.from_user.id)
    cfg["is_paused"] = not cfg["is_paused"]
    status_msg = "⏸ Мониторинг приостановлен." if cfg["is_paused"] else "🟢 Мониторинг возобновлен."


    text = build_main_menu_text(cfg)
    keyboard = build_main_keyboard(cfg)
    reply_kb = build_reply_keyboard(cfg["is_paused"])


    if isinstance(event, CallbackQuery):
        await event.answer(status_msg, show_alert=True)
        try:
            await event.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        except Exception:
            pass
        await event.message.answer(f"{status_msg}\nКнопки панели обновлены.", reply_markup=reply_kb)
    else:
        await event.answer(status_msg, reply_markup=reply_kb)
        await event.answer(text, reply_markup=keyboard, parse_mode="HTML")


@dp.message(FilterStates.waiting_for_price)
async def process_price_input(message: Message, state: FSMContext):
    parts = message.text.strip().replace(",", ".").split()
    if len(parts) != 2:
        await message.answer("❌ Введите два числа через пробел (ОТ и ДО). Например: <code>91 95</code>", parse_mode="HTML")
        return


    try:
        val_from = float(parts[0])
        val_to = float(parts[1])
    except ValueError:
        await message.answer("❌ Введите корректные числа. Например: <code>91.5 95</code>", parse_mode="HTML")
        return


    cfg = get_user_config(message.from_user.id)
    cfg["min_price"] = val_from if val_from > 0 else None
    cfg["max_price"] = val_to if val_to > 0 else None


    min_disp = f"{cfg['min_price']:.2f} ₽" if cfg['min_price'] is not None else "0.00 ₽"
    max_disp = f"{cfg['max_price']:.2f} ₽" if cfg['max_price'] is not None else "Без ограничений"


    confirm_text = (
        "✅ <b>Диапазон цены сохранен!</b>\n\n"
        f"Установлен диапазон: <b>{min_disp} — {max_disp}</b>\n\n"
        "Выберите действие:"
    )
    await state.set_state(FilterStates.confirm_price)
    await message.answer(confirm_text, reply_markup=build_confirm_keyboard("edit_price"), parse_mode="HTML")


@dp.message(FilterStates.waiting_for_limits)
async def process_limits_input(message: Message, state: FSMContext):
    raw = message.text.strip().replace(",", ".")
    parts = raw.split()
    cfg = get_user_config(message.from_user.id)


    try:
        if len(parts) == 1:
            val = float(parts[0])
            cfg["min_limit"] = val if val > 0 else None
            cfg["max_limit"] = val if val > 0 else None
        elif len(parts) == 2:
            v_from = float(parts[0])
            v_to = float(parts[1])
            cfg["min_limit"] = v_from if v_from > 0 else None
            cfg["max_limit"] = v_to if v_to > 0 else None
        else:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите одно число (например <code>20000</code>) или два (<code>10000 50000</code>):", parse_mode="HTML")
        return


    min_disp = f"{cfg['min_limit']:,.0f} ₽".replace(",", " ") if cfg['min_limit'] is not None else "0 ₽"
    max_disp = f"{cfg['max_limit']:,.0f} ₽".replace(",", " ") if cfg['max_limit'] is not None else "Без ограничений"
    lim_str = f"{min_disp} — {max_disp}" if (cfg['min_limit'] is not None or cfg['max_limit'] is not None) else "Любая сумма"


    confirm_text = (
        "✅ <b>Фильтр лимитов сохранен!</b>\n\n"
        f"Установлен диапазон: <b>{lim_str}</b>\n\n"
        "Выберите действие:"
    )
    await state.set_state(FilterStates.confirm_limits)
    await message.answer(confirm_text, reply_markup=build_confirm_keyboard("edit_limits"), parse_mode="HTML")


@dp.callback_query(StateFilter("*"), F.data.startswith("toggle_pay:"))
async def handle_toggle_pay(callback: CallbackQuery):
    method = callback.data.split(":")[1]
    cfg = get_user_config(callback.from_user.id)


    if method == "ALL":
        cfg["payments"] = ["ALL"]
    else:
        if "ALL" in cfg["payments"]:
            cfg["payments"].remove("ALL")


        if method in cfg["payments"]:
            cfg["payments"].remove(method)
            if not cfg["payments"]:
                cfg["payments"] = ["ALL"]
        else:
            cfg["payments"].append(method)


    await callback.message.edit_reply_markup(reply_markup=build_payments_keyboard(cfg))
    await callback.answer("Методы обновлены")


@dp.callback_query(StateFilter("*"), F.data.startswith("toggle_ex:"))
async def handle_toggle_ex(callback: CallbackQuery):
    ex_code = callback.data.split(":")[1]
    cfg = get_user_config(callback.from_user.id)


    if ex_code in cfg["exchanges"]:
        cfg["exchanges"].remove(ex_code)
    else:
        cfg["exchanges"].append(ex_code)


    await callback.message.edit_reply_markup(reply_markup=build_exchanges_keyboard(cfg))
    await callback.answer("Биржи обновлены")


@dp.message(StateFilter("*"))
async def fallback_any_message(message: Message, state: FSMContext):
    await state.clear()
    cfg = get_user_config(message.from_user.id)
    text = build_main_menu_text(cfg)
    keyboard = build_main_keyboard(cfg)
    reply_kb = build_reply_keyboard(cfg["is_paused"])
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


async def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(token=BOT_TOKEN)
    asyncio.create_task(monitoring_worker(bot))
    await dp.start_polling(bot, drop_pending_updates=True)


if __name__ == "__main__":
    asyncio.run(main())