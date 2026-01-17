import asyncio
import json
import csv
import io
import time
import requests

from PIL import Image, ImageDraw, ImageFont

from aiogram import Bot, Dispatcher
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command

from config import BOT_TOKEN

# --- СПИСОК РАЗРЕШЕННЫХ ID ---
# Добавьте сюда свой ID и ID сотрудников через запятую
ALLOWED_USERS = [
    516996400,  # Пиван
    5122416809,  # Репа
    334020724,  # Кор
    516996400,  # Макс
]

# Функция-фильтр для проверки доступа
def access_filter(message: Message) -> bool:
    return message.from_user.id in ALLOWED_USERS

# --- Apps Script: журнал ---
APPS_SCRIPT_URL = "https://script.google.com/macros/s/AKfycbwSXVi6APyb3Zz1eLchxTxERPXwNe3f6ecKRFV3CH7dhfqGB9djedwe_aB7-g4j4YY/exec"

# --- ОДНА группа: фото + итог ---
TARGET_GROUP_ID = -1003451445410

# --- Прайс (Google Sheet): Лист1, колонки: Продукт общий | Продукт | Цена ---
PRICES_SHEET_ID = "1SdI0i-vAkkEpdguFzdSp9_-JZAAhIZzBO7IZ55YUX_A"
PRICES_TAB_NAME = "Лист1"
PRICES_CSV_URL = f"https://docs.google.com/spreadsheets/d/{PRICES_SHEET_ID}/gviz/tq?tqx=out:csv&sheet={PRICES_TAB_NAME}"

# Кэш прайса (чтобы не дергать таблицу каждую кнопку)
PRICE_CACHE = {"ts": 0, "map": {}, "catalog": []}
PRICE_TTL_SECONDS = 300  # 5 минут

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# USER_STATE[user_id] = {"mode": "...", "fam_id": int|None, "prod_id": int|None}
USER_STATE = {}

USER_UI_MESSAGE_ID = {}

DRAFT_RECEPTIONS = {}

# USER_DATA[user_id] = {
#   "shop": str,
#   "photos": [file_id,...],
#   "catalog": {
#       "families": [{"fam_id": 1, "family": "..."}, ...],
#       "products": [{"prod_id": 1, "family": "...", "name": "..."}, ...],
#       "fam_to_prod_ids": {1: [1,2,3], ...},
#   },
#   prod_id: qty, prod_id: qty ...
# }
USER_DATA = {}

def reset_reception(user_id: int):
    USER_DATA.pop(user_id, None)
    USER_STATE.pop(user_id, None)
    DRAFT_RECEPTIONS.pop(user_id, None)

    # ❗ сбрасываем UI-сообщение текущей приёмки
    USER_UI_MESSAGE_ID.pop(user_id, None)


def fmt(n):
    try:
        return f"{int(round(float(n))):,}".replace(",", " ")
    except Exception:
        return "0"


def fetch_price_and_catalog():
    """
    Загружает прайс из Google Sheet (CSV).
    Возвращает:
      - price_map: dict[(family,name)] = price(float)
      - catalog: список уникальных товаров [{"family":..., "name":...}, ...]
    """
    now = time.time()
    if PRICE_CACHE["map"] and (now - PRICE_CACHE["ts"]) < PRICE_TTL_SECONDS:
        return PRICE_CACHE["map"], PRICE_CACHE["catalog"]

    r = requests.get(PRICES_CSV_URL, timeout=20)
    r.raise_for_status()

    reader = csv.DictReader(io.StringIO(r.text))

    price_map = {}
    seen = set()
    catalog = []

    for row in reader:
        fam = (row.get("Продукт общий") or "").strip()
        name = (row.get("Продукт") or "").strip()
        price_raw = (row.get("Цена") or "").strip()

        if not fam or not name:
            continue

        try:
            price = float(price_raw.replace(",", "."))
        except Exception:
            price = 0.0

        key = (fam, name)
        price_map[key] = price

        if key not in seen:
            seen.add(key)
            catalog.append({"family": fam, "name": name})


    PRICE_CACHE["ts"] = now
    PRICE_CACHE["map"] = price_map
    PRICE_CACHE["catalog"] = catalog
    return price_map, catalog


def ensure_user_catalog(user_id: int):
    """
    Фиксируем каталог на момент начала приёмки, чтобы ID не прыгали.
    """
    if user_id in USER_DATA and USER_DATA[user_id].get("catalog"):
        return

    _, flat = fetch_price_and_catalog()

    # семьи
    family_names = []
    seen_fam = set()

    for x in flat:
        fam = x["family"]
        if fam not in seen_fam:
            seen_fam.add(fam)
            family_names.append(fam)

    families = [{"fam_id": i + 1, "family": fam} for i, fam in enumerate(family_names)]
    fam_name_to_id = {x["family"]: x["fam_id"] for x in families}

    # продукты
    products = []
    fam_to_prod_ids = {x["fam_id"]: [] for x in families}

    prod_id = 1
    for x in flat:
        fam = x["family"]
        name = x["name"]
        fid = fam_name_to_id[fam]
        products.append({"prod_id": prod_id, "family": fam, "name": name, "fam_id": fid})
        fam_to_prod_ids[fid].append(prod_id)
        prod_id += 1

    USER_DATA.setdefault(user_id, {})
    USER_DATA[user_id]["catalog"] = {
        "families": families,
        "products": products,
        "fam_to_prod_ids": fam_to_prod_ids,
    }


def get_product_by_id(user_id: int, prod_id: int):
    catalog = USER_DATA[user_id]["catalog"]["products"]
    for p in catalog:
        if p["prod_id"] == prod_id:
            return p
    return None


def sum_family_qty(user_id: int, fam_id: int) -> int:
    cat = USER_DATA[user_id]["catalog"]
    total = 0
    for pid in cat["fam_to_prod_ids"].get(fam_id, []):
        total += float(USER_DATA.get(user_id, {}).get(pid, 0) or 0)
    return total


def families_keyboard(user_id: int) -> InlineKeyboardMarkup:
    ensure_user_catalog(user_id)
    fams = USER_DATA[user_id]["catalog"]["families"]

    buttons = []
    for idx, f in enumerate(fams, start=1):
        total = sum_family_qty(user_id, f["fam_id"])
        text = f"{idx}. {f['family']} — {total}"
        buttons.append(
            [InlineKeyboardButton(text=text, callback_data=f"fam_{f['fam_id']}")]
        )

    buttons.append(
        [InlineKeyboardButton(text="✅ Завершить приёмку", callback_data="finish")]
    )
    buttons.append(
        [InlineKeyboardButton(text="❌ Сбросить приёмку", callback_data="reset_confirm")]
    )

    return InlineKeyboardMarkup(inline_keyboard=buttons)



def products_keyboard(user_id: int, fam_id: int) -> InlineKeyboardMarkup:
    ensure_user_catalog(user_id)
    cat = USER_DATA[user_id]["catalog"]
    prod_ids = cat["fam_to_prod_ids"].get(fam_id, [])

    # для быстрого доступа по id -> продукт
    prod_map = {p["prod_id"]: p for p in cat["products"]}

    buttons = []
    for idx, pid in enumerate(prod_ids, start=1):
        p = prod_map[pid]
        qty = float(USER_DATA.get(user_id, {}).get(pid, 0) or 0)
        qty_txt = int(qty) if qty.is_integer() else str(qty).replace(".", ",")
        text = f"{idx}. {p['name']} — {qty}"
        buttons.append([InlineKeyboardButton(text=text, callback_data=f"prod_{pid}")])

    # навигация
    buttons.append([InlineKeyboardButton(text="⬅️ Назад к категориям", callback_data="back_fams")])
    buttons.append([InlineKeyboardButton(text="✅ Завершить приёмку", callback_data="finish")])
    buttons.append(
        [InlineKeyboardButton(text="❌ Сбросить приёмку", callback_data="reset_confirm")]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def render_report_image(shop: str, rows: list, extra: float = 0.0) -> bytes:
    """
    rows: [{"name": str, "qty": float, "price": float, "sum": float}, ...]
    Возвращает PNG bytes.
    """
    font = ImageFont.load_default()

    header_lines = ["Итог приёмки", f"Магазин: {shop}", ""]
    col_titles = ["Продукт", "Шт", "Цена", "Сумма"]

    total = 0.0
    table_rows = []

    for r in rows:
        total += r["sum"]

        price_txt = str(int(r["price"])) if float(r["price"]).is_integer() else fmt(r["price"])
        sum_txt = str(int(r["sum"])) if float(r["sum"]).is_integer() else fmt(r["sum"])
        qty_txt = str(int(r["qty"])) if float(r["qty"]).is_integer() else str(r["qty"]).replace(".", ",")

        table_rows.append([
            r["name"],
            qty_txt,
            price_txt,
            sum_txt
        ])

    footer_lines = [""]

    if extra > 0:
        footer_lines.append(f"Доп. сумма: {fmt(extra)}")
        total += extra

    footer_lines.append(f"Итого: {fmt(total)}")

    padding = 12
    line_h = 16
    col_w = [360, 55, 80, 90]
    width = padding * 2 + sum(col_w)
    height = padding * 2 + (
        len(header_lines)
        + 2
        + len(table_rows)
        + len(footer_lines)
    ) * line_h + 12

    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)

    y = padding
    for line in header_lines:
        draw.text((padding, y), line, fill="black", font=font)
        y += line_h

    x = padding
    for i, t in enumerate(col_titles):
        draw.text((x, y), t, fill="black", font=font)
        x += col_w[i]

    y += line_h
    draw.line((padding, y, width - padding, y), fill="black", width=1)
    y += 4

    for row in table_rows:
        x = padding
        for i, t in enumerate(row):
            draw.text((x, y), t, fill="black", font=font)
            x += col_w[i]
        y += line_h

    y += 6
    for line in footer_lines:
        draw.text((padding, y), line, fill="black", font=font)
        y += line_h

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

def build_group_report_text(data: dict, status: str) -> str:
    """
    status: 'draft' | 'edit' | 'final'
    """
    shop = data.get("shop", "")
    items = data.get("items", [])
    extra = float(data.get("extra", 0.0))

    # подтягиваем цены
    price_map, _ = fetch_price_and_catalog()

    lines = []

    # ---- шапка ----
    if status == "draft":
        lines.append("📝 Черновик приёмки")
    elif status == "edit":
        lines.append("✏️ Черновик приёмки (редактируется)")
    else:
        lines.append("✅ Приёмка сохранена")

    lines.append(f"Магазин: {shop}")
    lines.append("")

    # ---- позиции ----
    total = 0.0
    for it in items:
        price = float(price_map.get((it["family"], it["name"]), 0.0))
        qty = float(it["qty"])
        summ = qty * price
        total += summ

        lines.append(
            f"• {it['name']} — {qty} × {fmt(price)} = {fmt(summ)}"
        )

    # ---- доп. сумма ----
    if extra > 0:
        lines.append("")
        lines.append(f"Доп. сумма: {fmt(extra)}")
        total += extra

    # ---- итог ----
    lines.append("")
    lines.append(f"ИТОГО: {fmt(total)}")

    # ---- подсказка ----
    if status in ("draft", "edit"):
        lines.append("")
        lines.append("⏳ Можно редактировать 10 минут")

    return "\n".join(lines)


@dp.message(Command("myid"), lambda m: m.chat.type == "private")
async def show_user_id(message: Message):
    await message.answer(
        f"user_id = {message.from_user.id}\n"
        f"chat_id = {message.chat.id}"
    )

@dp.message(Command("id"), lambda m: m.chat.type == "private")
async def show_chat_id(message: Message):
    await message.answer(f"chat_id = {message.chat.id}")


@dp.message(Command("start"), lambda m: m.chat.type == "private")
async def start(message: Message):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="➕ Новая приёмка", callback_data="new_reception")]]
    )
    await message.answer("Привет! Запусти приёмку товара:", reply_markup=keyboard)

@dp.message(Command("reset"), lambda m: m.chat.type == "private")
async def hard_reset_command(message: Message):
    user_id = message.from_user.id

    # ❗ сбрасываем UI-сообщение текущей приёмки
    USER_UI_MESSAGE_ID.pop(user_id, None)

    # жёсткий сброс приёмки
    reset_reception(user_id)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Начать новую приёмку", callback_data="new_reception")]
        ]
    )

    await message.answer(
        "❌ Приёмка полностью сброшена.\nМожно начинать заново.",
        reply_markup=keyboard
    )



@dp.callback_query(lambda c: c.data == "new_reception")
async def new_reception(callback):
    user_id = callback.from_user.id

    # 1) полный сброс ВСЕГО, включая UI-сообщение
    reset_reception(user_id)

    # 2) ставим режим ожидания магазина
    USER_STATE[user_id] = {"mode": "wait_shop"}

    # 3) просим ввести магазин
    await callback.message.answer("Введите магазин (любой текст):")
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("fam_"))
async def choose_family(callback):
    user_id = callback.from_user.id
    state = USER_STATE.get(user_id, {})

    if state.get("mode") == "wait_shop":
        await callback.message.answer("Сначала введите магазин текстом.")
        await callback.answer()
        return

    fam_id = int(callback.data.split("_")[1])
    USER_STATE[user_id] = {"mode": "choose_product", "fam_id": fam_id, "prod_id": None}

    text = (
        "🧾 Приёмка товара\n\n"
        "➡️ Выберите продукт и укажите количество:"
    )

    ui_msg_id = USER_UI_MESSAGE_ID.get(user_id)

    if ui_msg_id:
        await bot.edit_message_text(
            chat_id=callback.message.chat.id,
            message_id=ui_msg_id,
            text=text,
            reply_markup=products_keyboard(user_id, fam_id)
        )
    else:
        msg = await callback.message.answer(
            text,
            reply_markup=products_keyboard(user_id, fam_id)
        )
        USER_UI_MESSAGE_ID[user_id] = msg.message_id

    await callback.answer()


@dp.callback_query(lambda c: c.data == "back_fams")
async def back_to_families(callback):
    user_id = callback.from_user.id
    USER_STATE[user_id] = {"mode": "choose_family", "fam_id": None, "prod_id": None}

    text = "🧾 Приёмка товара\n\n➡️ Выберите категорию (Продукт общий):"

    ui_msg_id = USER_UI_MESSAGE_ID.get(user_id)

    if ui_msg_id:
        await bot.edit_message_text(
            chat_id=callback.message.chat.id,
            message_id=ui_msg_id,
            text=text,
            reply_markup=families_keyboard(user_id)
        )
    else:
        msg = await callback.message.answer(
            text,
            reply_markup=families_keyboard(user_id)
        )
        USER_UI_MESSAGE_ID[user_id] = msg.message_id

    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("prod_"))
async def choose_product(callback):
    user_id = callback.from_user.id
    state = USER_STATE.get(user_id, {})

    if state.get("mode") == "wait_shop":
        await callback.message.answer("Сначала введите магазин текстом.")
        await callback.answer()
        return

    prod_id = int(callback.data.split("_")[1])
    ensure_user_catalog(user_id)
    p = get_product_by_id(user_id, prod_id)
    if not p:
        await callback.message.answer("Продукт не найден. Начните заново: /start")
        await callback.answer()
        return

    # ✅ ВОТ ЭТА СТРОКА БЫЛА НУЖНА
    product_name = p["name"]

    # запоминаем текущую категорию, чтобы после ввода количества вернуться в неё
    USER_STATE[user_id] = {
        "mode": "wait_qty",
        "fam_id": p["fam_id"],
        "prod_id": prod_id
    }

    text = (
        "🧾 Приёмка товара\n\n"
        f"📄 Товар: {product_name}\n"
        "✍️ Введите количество (можно 1.5)"
    )

    ui_msg_id = USER_UI_MESSAGE_ID.get(user_id)

    if ui_msg_id:
        await bot.edit_message_text(
            chat_id=callback.message.chat.id,
            message_id=ui_msg_id,
            text=text
        )
    else:
        msg = await callback.message.answer(text)
        USER_UI_MESSAGE_ID[user_id] = msg.message_id

    await callback.answer()


@dp.callback_query(lambda c: c.data == "finish")
async def finish_reception(callback):
    user_id = callback.from_user.id
    state = USER_STATE.get(user_id, {})

    # если магазин ещё не введён
    if state.get("mode") == "wait_shop":
        await callback.answer(
            "Сначала введите магазин, потом завершайте приёмку.",
            show_alert=True
        )
        return

    # новый шаг: ждём доп. сумму
    USER_STATE[user_id] = {
        "mode": "wait_extra",
        "fam_id": state.get("fam_id"),
        "prod_id": None,
    }

    text = (
        "🧾 Приёмка товара\n\n"
        "💰 Введите дополнительную сумму.\n"
        "Если доплаты нет — введите 0"
    )

    ui_msg_id = USER_UI_MESSAGE_ID.get(user_id)

    if ui_msg_id:
        await bot.edit_message_text(
            chat_id=callback.message.chat.id,
            message_id=ui_msg_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="❌ Сбросить приёмку", callback_data="reset_confirm")]
                ]
            )
        )
    else:
        # на всякий случай (почти не должен срабатывать)
        msg = await callback.message.answer(
            text,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="❌ Сбросить приёмку", callback_data="reset_confirm")]
                ]
            )
        )
        USER_UI_MESSA




@dp.callback_query(lambda c: c.data == "reset_confirm")
async def reset_confirm(callback):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, сбросить", callback_data="reset_yes"),
                InlineKeyboardButton(text="❌ Нет, продолжить", callback_data="reset_no"),
            ]
        ]
    )

    await callback.message.answer(
        "⚠️ Точно сбросить приёмку?\nВсе введённые данные будут удалены.",
        reply_markup=keyboard
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "reset_yes")
async def reset_yes(callback):
    user_id = callback.from_user.id

    # ❗ сбрасываем UI-сообщение текущей приёмки
    USER_UI_MESSAGE_ID.pop(user_id, None)

    USER_STATE.pop(user_id, None)
    USER_DATA.pop(user_id, None)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Начать новую приёмку", callback_data="new_reception")]
        ]
    )

    await callback.message.answer(
        "♻️ Приёмка сброшена.\nМожно начать заново.",
        reply_markup=keyboard
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data == "reset_no")
async def reset_no(callback):
    user_id = callback.from_user.id

    # возвращаем пользователя туда, где он был
    text = "🧾 Приёмка товара\n\n➡️ Выберите категорию (Продукт общий):"

    ui_msg_id = USER_UI_MESSAGE_ID.get(user_id)

    if ui_msg_id:
        await bot.edit_message_text(
            chat_id=callback.message.chat.id,
            message_id=ui_msg_id,
            text=text,
            reply_markup=families_keyboard(user_id)
        )
    await callback.answer()


@dp.message(lambda m: m.photo and m.chat.type == "private")
async def get_photos(message: Message):
    user_id = message.from_user.id
    if USER_STATE.get(user_id, {}).get("mode") != "wait_photos":
        return

    photo_id = message.photo[-1].file_id
    USER_DATA.setdefault(user_id, {})
    USER_DATA[user_id].setdefault("photos", [])
    USER_DATA[user_id]["photos"].append(photo_id)

    shop = USER_DATA.get(user_id, {}).get("shop", "")
    caption = f"Фото приёмки\nМагазин: {shop}\nОт: {message.from_user.full_name}"

    try:
        await bot.send_photo(chat_id=TARGET_GROUP_ID, photo=photo_id, caption=caption)
    except Exception:
        pass

    await message.answer("Фото добавлено")


@dp.callback_query(lambda c: c.data == "photos_done")
async def photos_done(callback):
    user_id = callback.from_user.id
    data = USER_DATA.get(user_id, {})
    shop = data.get("shop", "")
    extra = float(data.get("extra", 0.0))

    ensure_user_catalog(user_id)
    cat = data.get("catalog", {})
    products = cat.get("products", [])

    # items (без цены) -> Apps Script подтянет цену и запишет
    items = []
    for p in products:
        pid = p["prod_id"]
        qty = float(data.get(pid, 0) or 0)
        if qty > 0:
            items.append({
                "prod_id": p["prod_id"],   # 🔑 КЛЮЧ
                "family": p["family"],
                "name": p["name"],
                "qty": qty
            })


    # --- сохраняем черновик (ничего пока не пишем в таблицу) ---
    draft = DRAFT_RECEPTIONS.get(user_id)

    if not draft:
        # первый раз — создаём черновик
        draft = {
            "data": {},
            "created_at": time.time(),
            "finalized": False,
            "group_msg_id": None,
        }
        DRAFT_RECEPTIONS[user_id] = draft

    # обновляем данные черновика
    draft["data"] = {
        "shop": shop,
        "extra": extra,
        "items": items,
        "photos": data.get("photos", []),
        "author": callback.from_user.full_name,
    }


    # --- сообщение в группу о черновике ---
    group_text = build_group_report_text(
        DRAFT_RECEPTIONS[user_id]["data"],
        status="draft"
    )

    draft = DRAFT_RECEPTIONS[user_id]
    group_msg_id = draft.get("group_msg_id")

    if group_msg_id:
        # уже есть сообщение → редактируем
        try:
            await bot.edit_message_text(
                chat_id=TARGET_GROUP_ID,
                message_id=group_msg_id,
                text=group_text
            )
        except Exception:
            pass
    else:
        # первого раза ещё не было → создаём
        msg = await bot.send_message(
            chat_id=TARGET_GROUP_ID,
            text=group_text
        )
        draft["group_msg_id"] = msg.message_id

    # --- кнопки пользователю ---
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Исправить приёмку", callback_data="edit_draft")],
            [InlineKeyboardButton(text="✅ Подтвердить и сохранить", callback_data="finalize_draft")],
        ]
    )

    await callback.message.answer(
        "🧾 Приёмка сформирована.\n\n"
        "✏️ Можно исправить последнюю приёмку\n"
        "⏳ Автосохранение через 10 минут.",
        reply_markup=keyboard
    )

    await callback.answer()
    return


@dp.callback_query(lambda c: c.data == "edit_draft")
async def edit_draft(callback):
    user_id = callback.from_user.id

   # ❗ сбрасываем UI-сообщение текущей приёмки
    USER_UI_MESSAGE_ID.pop(user_id, None)

    draft = DRAFT_RECEPTIONS.get(user_id)
    if not draft:
        await callback.answer("Черновик не найден", show_alert=True)
        return

    d = draft["data"]

    # === ВОССТАНАВЛИВАЕМ ДАННЫЕ ПРИЁМКИ ===
    # фиксируем каталог (ВАЖНО)
    ensure_user_catalog(user_id)

    # полностью пересобираем USER_DATA
    USER_DATA[user_id] = {
        "shop": d.get("shop"),
        "extra": d.get("extra", 0.0),
    }

    # восстанавливаем количества
    for item in d.get("items", []):
        prod_id = item.get("prod_id")
        qty = item.get("qty", 0)
        if isinstance(prod_id, int):
            USER_DATA[user_id][prod_id] = qty

    # === ВОССТАНАВЛИВАЕМ СОСТОЯНИЕ ===
    USER_STATE[user_id] = {
        "mode": "choose_family",
        "fam_id": None,
        "prod_id": None
    }

    # === ОБНОВЛЯЕМ ГРУППОВОЕ СООБЩЕНИЕ ===
    group_msg_id = draft.get("group_msg_id")
    if group_msg_id:
        try:
            await bot.edit_message_text(
                chat_id=TARGET_GROUP_ID,
                message_id=group_msg_id,
                text=build_group_report_text(draft["data"], status="edit")
            )
        except Exception:
            pass

    # === ОБНОВЛЯЕМ ОСНОВНОЕ UI-СООБЩЕНИЕ ===
    ui_msg_id = USER_UI_MESSAGE_ID.get(user_id)

    text = (
        "✏️ Исправление приёмки\n\n"
        "➡️ Выберите категорию:"
    )

    if ui_msg_id:
        await bot.edit_message_text(
            chat_id=callback.message.chat.id,
            message_id=ui_msg_id,
            text=text,
            reply_markup=families_keyboard(user_id)
        )
    else:
        # на всякий случай (если UI вдруг потерялся)
        msg = await callback.message.answer(
            text,
            reply_markup=families_keyboard(user_id)
        )
        USER_UI_MESSAGE_ID[user_id] = msg.message_id
    await callback.answer()



@dp.callback_query(lambda c: c.data == "finalize_draft")
async def finalize_draft(callback):
    user_id = callback.from_user.id

    draft = DRAFT_RECEPTIONS.get(user_id)
    if not draft:
        await callback.answer("Черновик не найден", show_alert=True)
        return

    if draft.get("finalized"):
        await callback.answer("Приёмка уже сохранена", show_alert=True)
        return

    draft["finalized"] = True

    d = draft["data"]
    shop = d.get("shop", "")
    items = d.get("items", [])

    # ---------- ЗАПИСЬ В ТАБЛИЦУ ----------
    payload = {"shop": shop, "items": items}

    try:
        r = requests.post(
            APPS_SCRIPT_URL,
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            timeout=20
        )
    except Exception as e:
        await callback.message.answer(f"Ошибка записи в таблицу: {e}")
        return

    if r.status_code != 200 or "OK" not in r.text:
        await callback.message.answer(f"Ошибка записи в таблицу: {r.text[:200]}")
        return

    # ---------- ОБНОВЛЯЕМ СООБЩЕНИЕ В ГРУППЕ ----------
    group_msg_id = draft.get("group_msg_id")
    if group_msg_id:
        final_text = build_group_report_text(draft["data"], status="final")
        try:
            await bot.edit_message_text(
                chat_id=TARGET_GROUP_ID,
                message_id=group_msg_id,
                text=final_text
            )
        except Exception:
            pass

    # ---------- ЧИСТИМ ДАННЫЕ ----------
    DRAFT_RECEPTIONS.pop(user_id, None)
    reset_reception(user_id)

    # ---------- СООБЩЕНИЕ ПОЛЬЗОВАТЕЛЮ ----------
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏁 Начать новую приёмку", callback_data="new_reception")]
        ]
    )

    await callback.message.answer(
        "✅ Приёмка сохранена.\n"
        "Журнал заполнен, сообщение в группе обновлено.",
        reply_markup=keyboard
    )

    await callback.answer()


@dp.message(lambda m: m.chat.type == "private")
async def get_text(message: Message):
    user_id = message.from_user.id
    state = USER_STATE.get(user_id, {})
    mode = state.get("mode")

    # Ввод магазина
    if mode == "wait_shop":
        shop = (message.text or "").strip()
        if not shop:
            await message.answer("Введите магазин текстом.")
            return

        USER_DATA.setdefault(user_id, {})
        USER_DATA[user_id]["shop"] = shop

        # фиксируем каталог на начало приёмки
        ensure_user_catalog(user_id)

        USER_STATE[user_id] = {"mode": "choose_family", "fam_id": None, "prod_id": None}

        text = "🧾 Приёмка товара\n\n➡️ Выберите категорию (Продукт общий):"

        ui_msg_id = USER_UI_MESSAGE_ID.get(user_id)

        if ui_msg_id:
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=ui_msg_id,
                text=text,
                reply_markup=families_keyboard(user_id)
            )
        else:
            msg = await message.answer(
                text,
                reply_markup=families_keyboard(user_id)
            )
            USER_UI_MESSAGE_ID[user_id] = msg.message_id

        return

    # Ввод количества
    if mode == "wait_qty":
        raw = (message.text or "").strip().replace(",", ".")

        try:
            qty = float(raw)
        except ValueError:
            await message.answer("Введите количество числом, например 1 или 1.5")
            return

        if qty < 0:
            await message.answer("Количество не может быть отрицательным")
            return

        prod_id = state.get("prod_id")
        fam_id = state.get("fam_id")

        if not isinstance(prod_id, int) or not isinstance(fam_id, int):
            await message.answer("Ошибка состояния. Начните заново: /start")
            return

        # сохраняем количество
        USER_DATA.setdefault(user_id, {})
        USER_DATA[user_id][prod_id] = qty

        # возвращаемся в режим выбора продукта
        USER_STATE[user_id] = {
            "mode": "choose_product",
            "fam_id": fam_id,
            "prod_id": None
        }

        # === ОБНОВЛЯЕМ ОСНОВНОЕ UI-СООБЩЕНИЕ ===
        ui_msg_id = USER_UI_MESSAGE_ID.get(user_id)
    
        if ui_msg_id:
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=ui_msg_id,
                text="➡️ Выберите продукт и укажите количество:",
                reply_markup=products_keyboard(user_id, fam_id)
            )
        else:
            msg = await message.answer(
                "➡️ Выберите продукт и укажите количество:",
                reply_markup=products_keyboard(user_id, fam_id)
            )
            USER_UI_MESSAGE_ID[user_id] = msg.message_id

        return


    # Ввод дополнительной суммы
    if mode == "wait_extra":
        raw = (message.text or "").strip().replace(",", ".")

        try:
            extra = float(raw)
        except ValueError:
            await message.answer("Введите сумму числом, например: 1500 или 0")
            return

        if extra < 0:
            await message.answer("Сумма не может быть отрицательной")
            return

        USER_DATA.setdefault(user_id, {})
        USER_DATA[user_id]["extra"] = extra

        # переходим к фото
        USER_STATE[user_id] = {"mode": "wait_photos", "fam_id": None, "prod_id": None}

        text = (
            "🧾 Приёмка товара\n\n"
            f"💰 Доп. сумма: {fmt(extra)}\n\n"
            "📸 Пришлите фото принятого товара.\n"
            "Когда закончите — нажмите «Готово»."
        )

        ui_msg_id = USER_UI_MESSAGE_ID.get(user_id)

        if ui_msg_id:
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=ui_msg_id,
                text=text,
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="📦 Готово", callback_data="photos_done")]
                    ]
                )
            )
        else:
            msg = await message.answer(
                text,
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                    [InlineKeyboardButton(text="📦 Готово", callback_data="photos_done")]
                    ]
                )
            )
            USER_UI_MESSAGE_ID[user_id] = msg.message_id

        return



async def auto_finalize_drafts():
    while True:
        now = time.time()

        for user_id, draft in list(DRAFT_RECEPTIONS.items()):
            # если уже сохранено — не трогаем
            if draft.get("finalized"):
                continue

            # если ещё не прошло 10 минут — ждём
            if now - draft["created_at"] < 600:
                continue

            # помечаем как сохранённый, чтобы не было гонок
            draft["finalized"] = True

            d = draft["data"]
            shop = d.get("shop", "")
            items = d.get("items", [])

            payload = {"shop": shop, "items": items}

            try:
                r = requests.post(
                    APPS_SCRIPT_URL,
                    data=json.dumps(payload),
                    headers={"Content-Type": "application/json"},
                    timeout=20
                )
                if r.status_code != 200 or "OK" not in r.text:
                    # если не записалось — откатываем флаг и попробуем позже
                    draft["finalized"] = False
                    continue
            except Exception:
                draft["finalized"] = False
                continue

            # --- ОБНОВЛЯЕМ СООБЩЕНИЕ В ГРУППЕ ---
            group_msg_id = draft.get("group_msg_id")
            if group_msg_id:
                try:
                    final_text = build_group_report_text(
                        draft["data"],
                        status="final"
                    )
                    await bot.edit_message_text(
                        chat_id=TARGET_GROUP_ID,
                        message_id=group_msg_id,
                        text=final_text
                    )
                except Exception:
                    pass

            # --- ЧИСТИМ СОСТОЯНИЕ ---
            DRAFT_RECEPTIONS.pop(user_id, None)
            USER_STATE.pop(user_id, None)
            USER_DATA.pop(user_id, None)

        await asyncio.sleep(30)


# 1. Добавьте этот блок перед функцией main
@dp.update.outer_middleware()
async def access_middleware(handler, event, data):
    # Проверяем только сообщения и нажатия кнопок
    user = data.get("event_from_user")
    if user and user.id not in ALLOWED_USERS:
        if event.message:
            await event.message.answer("Ошибка")
        elif event.callback_query:
            await event.callback_query.answer("Ошибка", show_alert=True)
        return
    return await handler(event, data)

# 2. Ваша функция main теперь будет выглядеть так:
async def main():
    # Никаких лишних строк в других частях кода не нужно!
    asyncio.create_task(auto_finalize_drafts())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
