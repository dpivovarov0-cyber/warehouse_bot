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



@dp.message(Command("id"), lambda m: m.chat.type == "private")
async def show_chat_id(message: Message):
    await message.answer(f"chat_id = {message.chat.id}")


@dp.message(Command("start"), lambda m: m.chat.type == "private")
async def start(message: Message):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="➕ Новая приёмка", callback_data="new_reception")]]
    )
    await message.answer("Привет! Запусти приёмку товара:", reply_markup=keyboard)


@dp.callback_query(lambda c: c.data == "new_reception")
async def new_reception(callback):
    user_id = callback.from_user.id
    USER_DATA[user_id] = {"shop": "", "photos": [], "catalog": {}}
    USER_STATE[user_id] = {"mode": "wait_shop", "fam_id": None, "prod_id": None}

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

    await callback.message.answer(
        "Выберите продукт и укажите количество:",
        reply_markup=products_keyboard(user_id, fam_id)
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data == "back_fams")
async def back_to_families(callback):
    user_id = callback.from_user.id
    USER_STATE[user_id] = {"mode": "choose_family", "fam_id": None, "prod_id": None}

    await callback.message.answer(
        "Выберите категорию (Продукт общий):",
        reply_markup=families_keyboard(user_id)
    )
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

    # запоминаем текущую категорию, чтобы после ввода количества вернуться в неё
    USER_STATE[user_id] = {"mode": "wait_qty", "fam_id": p["fam_id"], "prod_id": prod_id}

    await callback.message.answer(f"📦 {p['name']}\nВведите количество:")
    await callback.answer()


@dp.callback_query(lambda c: c.data == "finish")
async def finish_reception(callback):
    user_id = callback.from_user.id
    state = USER_STATE.get(user_id, {})

    # если магазин ещё не введён
    if state.get("mode") == "wait_shop":
        await callback.message.answer(
            "Сначала введите магазин, потом завершайте приёмку."
        )
        await callback.answer()
        return

    # новый шаг: ждём доп. сумму
    USER_STATE[user_id] = {
        "mode": "wait_extra",
        "fam_id": state.get("fam_id"),
        "prod_id": None,
    }

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Сбросить приёмку", callback_data="reset_confirm")]
        ]
    )

    await callback.message.answer(
        "Введите дополнительную сумму.\n"
        "Если доплаты нет — введите 0",
        reply_markup=keyboard
    )
    await callback.answer()



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
    await callback.message.answer(
        "Ок, продолжаем приёмку 👌",
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
            items.append({"family": p["family"], "name": p["name"], "qty": qty})

    payload = {"shop": shop, "items": items}

    # 1) журнал
    try:
        r = requests.post(
            APPS_SCRIPT_URL,
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            timeout=20
        )
    except Exception as e:
        await callback.message.answer(f"Ошибка отправки в таблицу: {e}")
        return

    if r.status_code != 200 or "OK" not in r.text:
        await callback.message.answer(f"Ошибка записи в таблицу: {r.text[:200]}")
        return

    # 2) итог в группу (картинка) — цены из прайса
    try:
        price_map, _ = fetch_price_and_catalog()
        report_rows = []
        for it in items:
            price = float(price_map.get((it["family"], it["name"]), 0.0))
            qty = float(it["qty"])
            report_rows.append({"name": it["name"], "qty": qty, "price": price, "sum": qty * price})

        png = render_report_image(
            shop=shop,
            rows=report_rows,
            extra=extra
        )
        await bot.send_photo(
            chat_id=TARGET_GROUP_ID,
            photo=png,
            caption=f"Итог приёмки\nМагазин: {shop}"
        )
    except Exception:
        # запасной вариант — текстом
        try:
            price_map, _ = fetch_price_and_catalog()
            lines = [f"Итог приёмки\nМагазин: {shop}"]
            total = 0.0

            for it in items:
                price = float(price_map.get((it["family"], it["name"]), 0.0))
                s = float(it["qty"]) * price
                total += s
                lines.append(f"{it['name']} — {it['qty']} × {fmt(price)} = {fmt(s)}")

            if extra > 0:
                lines.append(f"Доп. сумма: {fmt(extra)}")
                total += extra

            lines.append(f"Итого: {fmt(total)}")

            await bot.send_message(chat_id=TARGET_GROUP_ID, text="\n".join(lines))
        except Exception:
            pass

                # ---------- итог пользователю + кнопка ----------
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🏁 Начать новую приёмку", callback_data="new_reception")]
            ]
        )

        await callback.message.answer(
            "Приёмка завершена ✅\nЖурнал записан. Фото и итог отправлены в группу.",
            reply_markup=keyboard
        )

    USER_STATE.pop(user_id, None)
    USER_DATA.pop(user_id, None)
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

        await message.answer(
            f"Магазин: {shop}\nВыберите категорию (Продукт общий):",
            reply_markup=families_keyboard(user_id)
        )
        return

    # Ввод количества
    if mode == "wait_qty":
        raw = (message.text or "").strip().replace(",", ".")

        try:
            qty = float(raw)
        except ValueError:
            await message.answer("Введите количество числом, например: 1,5")
            return

        if qty < 0:
            await message.answer("Количество не может быть отрицательным")
            return

        prod_id = state.get("prod_id")
        fam_id = state.get("fam_id")


        if not isinstance(prod_id, int) or not isinstance(fam_id, int):
            await message.answer("Ошибка состояния. Начните заново: /start")
            return

        USER_DATA.setdefault(user_id, {})
        USER_DATA[user_id][prod_id] = qty

        # возвращаемся в список продуктов текущей категории
        USER_STATE[user_id] = {"mode": "choose_product", "fam_id": fam_id, "prod_id": None}
        await message.answer("Количество сохранено", reply_markup=products_keyboard(user_id, fam_id))
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

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📦 Готово", callback_data="photos_done")]
            ]
        )

        await message.answer(
            f"Дополнительная сумма принята: {fmt(extra)}\n\n"
            "Теперь пришлите фото принятого товара.\n"
            "Когда закончите — нажмите «Готово».",
            reply_markup=keyboard
        )
        return

        return


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
