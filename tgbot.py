import telebot
from telebot import types
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import pandas as pd
import os
import json
from gspread_formatting import *
from flask import Flask
import threading

app = Flask(__name__)

@app.route('/')
def keep_alive():
    return "Bot is alive!"

# Загрузка конфигурации из config.json
try:
    with open('config.json', 'r', encoding='utf-8') as config_file:
        config = json.load(config_file)
except FileNotFoundError:
    print("Ошибка: файл config.json не найден!")
    exit(1)
except json.JSONDecodeError:
    print("Ошибка: некорректный формат config.json!")
    exit(1)

# Инициализация бота
bot = telebot.TeleBot(config['token'])

# Константы из конфига
SPREADSHEET_ID = config['spreadsheet_id']

# Настройка командного меню
def set_bot_commands():
    commands = [
        types.BotCommand("start", "Запустить бота"),
        types.BotCommand("search", "Найти товар")
    ]
    bot.set_my_commands(commands)

# Настройка Google Sheets API
scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
credentials_json = os.environ.get('GOOGLE_CREDENTIALS')
creds_dict = json.loads(credentials_json)
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)

# Состояния пользователей
user_states = {}

# Вспомогательные функции
def find_warehouse_sheet():
    spreadsheet = client.open_by_key(SPREADSHEET_ID)
    sheets = spreadsheet.worksheets()
    for sheet in sheets:
        if 'СКЛАД' in sheet.title:
            return sheet
    return None

def ensure_orders_sheet():
    spreadsheet = client.open_by_key(SPREADSHEET_ID)
    if 'Заказы' not in [sheet.title for sheet in spreadsheet.worksheets()]:
        sheet = spreadsheet.add_worksheet('Заказы', 1000, 5)
        sheet.update(range_name='A1:E1', values=[['📋 Название заказа', '🛒 Товар', '📦 Количество', '💰 Цена', '💵 Сумма']])
        format_orders_sheet(sheet)
    return spreadsheet.worksheet('Заказы')

def format_orders_sheet(sheet):
    set_column_width(sheet, 'A', 200)
    set_column_width(sheet, 'B', 250)
    set_column_width(sheet, 'C', 100)
    set_column_width(sheet, 'D', 100)
    set_column_width(sheet, 'E', 120)

    header_format = CellFormat(
        backgroundColor=Color(0.2, 0.6, 1),
        textFormat=TextFormat(fontFamily='Roboto', fontSize=12, bold=True),
        horizontalAlignment='CENTER',
        verticalAlignment='MIDDLE',
        borders=Borders(custom={'bottom': Border('SOLID', Color(0, 0, 0))}))
    format_cell_range(sheet, 'A1:E1', header_format)

    data_format = CellFormat(
        backgroundColor=Color(0.95, 0.95, 0.95),
        textFormat=TextFormat(fontFamily='Roboto', fontSize=11),
        horizontalAlignment='LEFT',
        borders=Borders(custom={'bottom': Border('DOTTED', Color(0.7, 0.7, 0.7))}))
    format_cell_range(sheet, 'A2:E1000', data_format)

def format_row(row):
    return [x if x else '-' for x in row + ['-'] * (7 - len(row))]

def find_order_block(order_sheet, order_name):
    all_data = order_sheet.get_all_values()
    start_row = None
    end_row = None
    for i, row in enumerate(all_data, 1):
        if row and row[0].replace('📋 ', '') == order_name and start_row is None:
            start_row = i
        elif start_row and (not row or row[0] or (len(row) > 3 and row[3] == 'Итого')):
            end_row = i - 1
            break
    if start_row and not end_row:
        end_row = len(all_data)
    if end_row and end_row < len(all_data) and all_data[end_row][3] == 'Итого':
        end_row += 1
    return start_row, end_row

def get_order_list(order_sheet):
    all_data = order_sheet.get_all_values()[1:]
    orders = {row[0].replace('📋 ', '') for row in all_data if row and row[0] and row[3] != 'Итого'}
    return list(orders)

def get_stock_quantity(item_name):
    warehouse_sheet = find_warehouse_sheet()
    if not warehouse_sheet:
        return None
    all_data = warehouse_sheet.get_all_values()
    for row in all_data:
        if len(row) >= 2 and row[1] == item_name:
            return int(row[2]) if row[2] and row[2] != '-' else 0
    return 0

def format_order_table(block_data, start_row):
    valid_items = [item for item in block_data[1:-1] if item and len(item) >= 4 and item[1]]
    total = block_data[-1][4] if len(block_data[-1]) > 4 else '0'
    total = float(total.replace(',', '.')) if total else 0
    table = "<b>📋 Заказ:</b>\n<code>"
    table += "№  Товар            Кол-во  Цена      Сумма\n"
    table += "═════════════════════════════════════════════\n"
    for i, item in enumerate(valid_items, 1):
        name = item[1].replace('🛒 ', '')[:12] + "..." if len(item[1].replace('🛒 ', '')) > 12 else item[1].replace('🛒 ', '').ljust(15)
        qty = str(item[2]).rjust(6)
        price_str = item[3].replace(',', '.') if item[3] else '0'
        price = f"{float(price_str):.2f} ₽".rjust(8)
        total_line_str = item[4].replace(',', '.') if item[4] else '0'
        total_line = f"{float(total_line_str):.2f} ₽".rjust(8)
        table += f"{str(i).rjust(2)} {name} {qty}  {price} {total_line}\n"
    table += "═════════════════════════════════════════════\n"
    table += f"{'Итого:'.rjust(33)} {total:.2f} ₽".rjust(12) + "\n"
    table += "</code>"
    return table

def export_stock(chat_id):
    warehouse_sheet = find_warehouse_sheet()
    if not warehouse_sheet:
        bot.send_message(chat_id, "❌ Лист 'СКЛАД' не найден. Проверь настройки!")
        return
    
    all_data = warehouse_sheet.get_all_values()[1:]
    stock_items = [(row[1], int(row[2]) if row[2] and row[2] != '-' else 0) for row in all_data 
                  if len(row) >= 3 and row[1] and (row[2] and row[2] != '-' and int(row[2]) > 0)]
    stock_items.sort(key=lambda x: x[0].lower())
    
    if not stock_items:
        bot.send_message(chat_id, "📦 На складе нет товаров с остатками > 0!")
        return
    
    grouped_items = {}
    for item_name, qty in stock_items:
        first_letter = item_name[0].upper()
        if first_letter not in grouped_items:
            grouped_items[first_letter] = []
        grouped_items[first_letter].append((item_name, qty))
    
    for letter, items in sorted(grouped_items.items()):
        message = f"📦 <b>Товары на букву '{letter}':</b>\n"
        for item_name, qty in items:
            message += f"📋 {item_name}\n📏 Количество: {qty}\n\n"
        bot.send_message(chat_id, message.strip(), parse_mode='HTML')
    
    df = pd.DataFrame(stock_items, columns=['Товар', 'Количество'])
    file_path = "stock_remains.xlsx"
    df.to_excel(file_path, index=False)
    with open(file_path, 'rb') as file:
        bot.send_message(chat_id, "📄 <b>Полный список остатков на складе:</b>", parse_mode='HTML')
        bot.send_document(chat_id, file)
    os.remove(file_path)

# Функции для создания кнопок
def create_main_menu():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📋 Создать заказ", callback_data="neworder"))
    markup.add(types.InlineKeyboardButton("📦 Выгрузить остатки", callback_data="export_stock"))
    markup.add(types.InlineKeyboardButton("✏️ Редактировать заказ", callback_data="edit_order"))
    markup.add(types.InlineKeyboardButton("🔍 Найти товар", callback_data="search"))
    markup.add(types.InlineKeyboardButton("ℹ️ Инфо", callback_data="info"))
    return markup

def create_back_button():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("⬅️ Вернуться назад", callback_data="back"))
    return markup

def create_search_buttons():
    markup = types.InlineKeyboardMarkup()
    markup.row(types.InlineKeyboardButton("✏️ Редактировать", callback_data="edit_item"),
               types.InlineKeyboardButton("🛒 В заказ", callback_data="add_to_order"))
    markup.row(types.InlineKeyboardButton("➡️ Далее", callback_data="next"), 
               types.InlineKeyboardButton("⬅️ Назад", callback_data="prev"))
    markup.add(types.InlineKeyboardButton("🔍 Найти товар", callback_data="search"))
    markup.add(types.InlineKeyboardButton("🏠 В меню", callback_data="back_to_menu"))
    return markup

def create_edit_buttons():
    markup = types.InlineKeyboardMarkup()
    markup.row(types.InlineKeyboardButton("📛 Название", callback_data="edit_name"),
               types.InlineKeyboardButton("📦 Количество", callback_data="edit_quantity"))
    markup.row(types.InlineKeyboardButton("🔒 Бронь", callback_data="edit_reserve"),
               types.InlineKeyboardButton("💰 Цена", callback_data="edit_price"))
    markup.row(types.InlineKeyboardButton("🏷 Дилерская цена", callback_data="edit_dealer_price"),
               types.InlineKeyboardButton("🔒 Бронь2", callback_data="edit_reserve2"))
    markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="back_from_edit"))
    return markup

def create_price_type_buttons():
    markup = types.InlineKeyboardMarkup()
    markup.row(types.InlineKeyboardButton("💰 Обычная цена", callback_data="price_regular"),
              types.InlineKeyboardButton("🏷 Дилерская цена", callback_data="price_dealer"))
    markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="back"))
    return markup

def create_order_buttons(orders, page=0, mode="add"):
    markup = types.InlineKeyboardMarkup()
    start_idx = page * 8
    end_idx = min(start_idx + 8, len(orders))
    order_subset = orders[start_idx:end_idx]
    if not order_subset:
        markup.add(types.InlineKeyboardButton("📝 Нет заказов", callback_data="no_orders"))
        markup.add(types.InlineKeyboardButton("⬅️ Вернуться назад", callback_data="back"))
        return markup

    for i in range(0, len(order_subset), 2):
        row = []
        row.append(types.InlineKeyboardButton(f"📋 {order_subset[i]}", callback_data=f"select_order_{order_subset[i]}"))
        if i + 1 < len(order_subset):
            row.append(types.InlineKeyboardButton(f"📋 {order_subset[i+1]}", callback_data=f"select_order_{order_subset[i+1]}"))
        markup.row(*row)
    if len(orders) > 8:
        row = []
        if page > 0:
            row.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"prev_orders_{page-1}_{mode}"))
        if end_idx < len(orders):
            row.append(types.InlineKeyboardButton("Вперёд ➡️", callback_data=f"next_orders_{page+1}_{mode}"))
        if row:
            markup.row(*row)
    markup.add(types.InlineKeyboardButton("⬅️ Вернуться назад", callback_data="back"))
    return markup

def create_order_edit_buttons():
    markup = types.InlineKeyboardMarkup()
    markup.row(types.InlineKeyboardButton("✏️ Изменить количество", callback_data="edit_item_qty"),
               types.InlineKeyboardButton("🗑 Удалить товар", callback_data="delete_item"))
    markup.add(types.InlineKeyboardButton("🗑 Удалить заказ", callback_data="delete_order"))
    markup.add(types.InlineKeyboardButton("✅ Завершить заказ", callback_data="complete_order"))
    markup.add(types.InlineKeyboardButton("⬅️ Вернуться назад", callback_data="back"))
    return markup

def create_item_selection_buttons(valid_items, page=0, action="edit"):
    markup = types.InlineKeyboardMarkup()
    start_idx = page * 5
    end_idx = min(start_idx + 5, len(valid_items))
    item_subset = valid_items[start_idx:end_idx]
    if not item_subset:
        markup.add(types.InlineKeyboardButton("📝 Нет товаров", callback_data="no_items"))
        markup.add(types.InlineKeyboardButton("⬅️ Вернуться назад", callback_data="back"))
        return markup
    for i, item in enumerate(item_subset):
        item_name = item[1].replace('🛒 ', '')
        markup.add(types.InlineKeyboardButton(f"{i + start_idx + 1}. {item_name}", callback_data=f"select_item_{i + start_idx}_{action}"))
    if len(valid_items) > 5:
        row = []
        if page > 0:
            row.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"prev_items_{page-1}_{action}"))
        if end_idx < len(valid_items):
            row.append(types.InlineKeyboardButton("Вперёд ➡️", callback_data=f"next_items_{page+1}_{action}"))
        if row:
            markup.row(*row)
    markup.add(types.InlineKeyboardButton("⬅️ Вернуться назад", callback_data="back"))
    return markup

def get_full_item_info(row_num, row):
    return (f"📦 Товар: {row[1]}\n"
            f"📏 Количество: {row[2]}\n"
            f"🔒 Бронь: {row[3]}\n"
            f"💰 Цена: {row[4]}\n"
            f"🔒 Бронь2: {row[5]}\n"
            f"🏷 Дилерская цена: {row[6]}\n"
            f"📍 Строка: {row_num}")

# Обработчики команд и callback-запросов
@bot.message_handler(commands=['start'])
def send_welcome(message):
    set_bot_commands()
    bot.reply_to(message, "👋 Привет! Я твой складской помощник! 😊\nВыбери, что хочешь сделать:", reply_markup=create_main_menu())

@bot.message_handler(commands=['search'])
def handle_search_command(message):
    user_states[message.chat.id] = 'waiting_for_search'
    bot.reply_to(message, "🔍 Какой товар ищем? Введи название:", reply_markup=create_back_button())

def show_search_result(chat_id, message_id):
    state = user_states.get(chat_id)
    if not state or 'results' not in state or 'index' not in state:
        bot.edit_message_text("❌ Ошибка состояния. Вернись в меню и попробуй снова.", chat_id, message_id, reply_markup=create_main_menu())
        if chat_id in user_states:
            del user_states[chat_id]
        return
    index = state['index']
    total_results = len(state['results'])
    row_num, row = state['results'][index]
    response = f"🔍 <b>Результат {index + 1} из {total_results}:</b>\n{get_full_item_info(row_num, row)}"
    bot.edit_message_text(response, chat_id, message_id, reply_markup=create_search_buttons(), parse_mode='HTML')

def show_order_items(chat_id, message_id):
    state = user_states.get(chat_id)
    if not state or 'block_data' not in state:
        bot.edit_message_text("❌ Ошибка состояния. Вернись в меню и попробуй снова.", chat_id, message_id, reply_markup=create_main_menu())
        if chat_id in user_states:
            del user_states[chat_id]
        return
    block_data = state['block_data']
    valid_items = [item for item in block_data[1:-1] if item and len(item) >= 4 and item[1]]
    if not valid_items:
        response = f"{format_order_table(block_data, state['start_row'])}\n🔚 Нет товаров для редактирования!"
        try:
            bot.edit_message_text(response, chat_id, message_id, reply_markup=create_order_edit_buttons(), parse_mode='HTML')
        except telebot.apihelper.ApiTelegramException as e:
            if "message is not modified" not in str(e):
                raise e
        return
    response = format_order_table(block_data, state['start_row'])
    try:
        bot.edit_message_text(response, chat_id, message_id, reply_markup=create_order_edit_buttons(), parse_mode='HTML')
    except telebot.apihelper.ApiTelegramException as e:
        if "message is not modified" not in str(e):
            raise e

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    chat_id = call.message.chat.id
    
    if call.data == "export_stock":
        bot.edit_message_text("⏳ Выгружаю остатки склада...", chat_id, call.message.message_id)
        export_stock(chat_id)
    
    elif call.data == "neworder":
        user_states[chat_id] = 'waiting_for_neworder'
        bot.edit_message_text("📋 Давай создадим новый заказ! Введи его название:", chat_id, call.message.message_id, reply_markup=create_back_button())
    
    elif call.data == "search":
        user_states[chat_id] = 'waiting_for_search'
        bot.edit_message_text("🔍 Какой товар ищем? Введи название:", chat_id, call.message.message_id, reply_markup=create_back_button())
    
    elif call.data == "info":
        info_message = (
            "✨ <b>Привет! Я твой складской помощник!</b> ✨\n\n"
            "Я создан, чтобы помочь тебе управлять складом и заказами. Вот что я умею:\n\n"
            "📋 <b>Создать заказ</b> — Добавить новый заказ, куда можно положить товары.\n"
            "📦 <b>Выгрузить остатки</b> — Показать, сколько товаров есть на складе, сгруппированных по буквам, и дать файл со списком.\n"
            "✏️ <b>Редактировать заказ</b> — Изменить или удалить товары в заказе, завершить его и скачать файл.\n"
            "🔍 <b>Найти товар</b> — Найти товар на складе, посмотреть его количество, цену, бронь и даже изменить данные.\n"
            "ℹ️ <b>Инфо</b> — Это ты сейчас читаешь! Инструкция для тебя.\n\n"
            "<b>Как пользоваться?</b>\n"
            "1. Нажми кнопку ниже, чтобы начать.\n"
            "2. Или введи команду внизу чата (например, /search для поиска).\n"
            "3. Следуй моим подсказкам — я всё объясню!\n\n"
            "💡 Я простой и понятный, как твой любимый чайник! Если что-то не ясно, пиши мне!"
        )
        bot.edit_message_text(info_message, chat_id, call.message.message_id, reply_markup=create_main_menu(), parse_mode='HTML')
    
    elif call.data.startswith("prev_orders_") or call.data.startswith("next_orders_"):
        if chat_id in user_states and isinstance(user_states[chat_id], dict):
            state = user_states[chat_id]
            parts = call.data.split("_")
            page = int(parts[2])
            mode = parts[3]
            state['order_page'] = page
            order_sheet = ensure_orders_sheet()
            orders = get_order_list(order_sheet)
            if mode == "add" and state.get('state') == 'searching':
                row_num, row = state['results'][state['index']]
                text = f"🛒 Добавляем товар:\n{get_full_item_info(row_num, row)}\nКуда положим?"
            elif mode == "edit" and state.get('state') == 'selecting_order_to_edit':
                text = "📋 Выбери заказ для редактирования:"
            else:
                text = "❌ Ошибка режима. Вернись в меню."
            bot.edit_message_text(text, chat_id, call.message.message_id, reply_markup=create_order_buttons(orders, page, mode))
    
    elif call.data == "back":
        if chat_id in user_states:
            if user_states[chat_id] in ['waiting_for_neworder', 'waiting_for_search']:
                del user_states[chat_id]
                bot.edit_message_text("🏠 Ты вернулся в главное меню! Что дальше? 😊", chat_id, call.message.message_id, reply_markup=create_main_menu())
            elif isinstance(user_states[chat_id], dict):
                state = user_states[chat_id]
                if state.get('state') == 'searching':
                    if state.get('waiting_for_add'):
                        del state['waiting_for_add']
                        show_search_result(chat_id, state['result_message_id'])
                    elif state.get('selecting_order'):
                        del state['selecting_order']
                        show_search_result(chat_id, state['result_message_id'])
                    else:
                        del user_states[chat_id]
                        bot.edit_message_text("🏠 Ты вернулся в главное меню! Что дальше? 😊", chat_id, call.message.message_id, reply_markup=create_main_menu())
                elif state.get('state') == 'editing_order':
                    if state.get('selecting_item'):
                        del state['selecting_item']
                        show_order_items(chat_id, state['result_message_id'])
                    else:
                        del user_states[chat_id]
                        bot.edit_message_text("🏠 Ты вернулся в главное меню! Что дальше? 😊", chat_id, call.message.message_id, reply_markup=create_main_menu())
                else:
                    del user_states[chat_id]
                    bot.edit_message_text("🏠 Ты вернулся в главное меню! Что дальше? 😊", chat_id, call.message.message_id, reply_markup=create_main_menu())
    
    elif call.data == "back_from_edit":
        if chat_id in user_states and isinstance(user_states[chat_id], dict) and user_states[chat_id].get('state') == 'searching':
            show_search_result(chat_id, user_states[chat_id]['result_message_id'])
    
    elif call.data == "back_to_menu":
        if chat_id in user_states and isinstance(user_states[chat_id], dict) and user_states[chat_id].get('state') == 'searching':
            del user_states[chat_id]
            bot.edit_message_text("🏠 Ты вернулся в главное меню! Что дальше? 😊", chat_id, call.message.message_id, reply_markup=create_main_menu())
    
    elif call.data in ["next", "prev"] and chat_id in user_states and isinstance(user_states[chat_id], dict) and user_states[chat_id].get('state') == 'searching':
        state = user_states[chat_id]
        results = state.get('results', [])
        index = state.get('index', 0)
        message_id = state.get('result_message_id')
        if call.data == "next" and index < len(results) - 1:
            state['index'] += 1
            show_search_result(chat_id, message_id)
        elif call.data == "prev" and index > 0:
            state['index'] -= 1
            show_search_result(chat_id, message_id)
        else:
            bot.answer_callback_query(call.id, "🔚 Больше товаров нет!")
    
    elif call.data == "edit_item" and chat_id in user_states and isinstance(user_states[chat_id], dict) and user_states[chat_id].get('state') == 'searching':
        state = user_states[chat_id]
        row_num, row = state['results'][state['index']]
        bot.edit_message_text(f"✏️ Редактируем товар:\n{get_full_item_info(row_num, row)}\nЧто хочешь изменить?",
                            chat_id, call.message.message_id, reply_markup=create_edit_buttons())
    
    elif call.data.startswith("edit_") and chat_id in user_states and isinstance(user_states[chat_id], dict) and user_states[chat_id].get('state') == 'searching':
        state = user_states[chat_id]
        action = call.data.split("_")[1]
        state['edit_action'] = action
        row_num, row = state['results'][state['index']]
        if action == "quantity":
            bot.edit_message_text(f"📏 Текущие данные:\n{get_full_item_info(row_num, row)}\nНовое количество на складе:",
                                chat_id, call.message.message_id, reply_markup=create_back_button())
        elif action == "reserve":
            bot.edit_message_text(f"🔒 Текущие данные:\n{get_full_item_info(row_num, row)}\nСколько забронировать/снять? (например, 20 или -20):",
                                chat_id, call.message.message_id, reply_markup=create_back_button())
        elif action == "name":
            bot.edit_message_text(f"📛 Текущие данные:\n{get_full_item_info(row_num, row)}\nНовое название товара:",
            chat_id, call.message.message_id, reply_markup=create_back_button())
        elif action == "price":
            bot.edit_message_text(f"💰 Текущие данные:\n{get_full_item_info(row_num, row)}\nНовая цена (например, 150.50):",
                                chat_id, call.message.message_id, reply_markup=create_back_button())
        elif action == "dealer_price":
            bot.edit_message_text(f"🏷 Текущие данные:\n{get_full_item_info(row_num, row)}\nНовая дилерская цена (например, 120.00):",
                                chat_id, call.message.message_id, reply_markup=create_back_button())
        elif action == "reserve2":
            bot.edit_message_text(f"🔒 Текущие данные:\n{get_full_item_info(row_num, row)}\nСколько забронировать/снять для Бронь2? (например, 20 или -20):",
                                chat_id, call.message.message_id, reply_markup=create_back_button())
    
    elif call.data == "add_to_order" and chat_id in user_states and isinstance(user_states[chat_id], dict) and user_states[chat_id].get('state') == 'searching':
        state = user_states[chat_id]
        state['selecting_order'] = True
        state['order_page'] = 0
        order_sheet = ensure_orders_sheet()
        orders = get_order_list(order_sheet)
        if not orders:
            bot.edit_message_text("🛒 Сначала создай заказ в меню 'Создать заказ'!", chat_id, call.message.message_id, reply_markup=create_back_button())
            return
        row_num, row = state['results'][state['index']]
        bot.edit_message_text(f"🛒 Добавляем товар:\n{get_full_item_info(row_num, row)}\nКуда положим?",
                            chat_id, call.message.message_id, reply_markup=create_order_buttons(orders, state['order_page'], "add"))
    
    elif call.data.startswith("select_order_") and chat_id in user_states and isinstance(user_states[chat_id], dict) and user_states[chat_id].get('state') == 'searching':
        state = user_states[chat_id]
        order_name = call.data.replace("select_order_", "")
        state['selected_order'] = order_name
        del state['selecting_order']
        row_num, row = state['results'][state['index']]
        stock = get_stock_quantity(row[1])
        bot.edit_message_text(f"🛒 Товар:\n{get_full_item_info(row_num, row)}\nВыбран заказ: {order_name}\nНа складе: {stock} шт.\nПо какой цене добавить?",
                            chat_id, call.message.message_id, reply_markup=create_price_type_buttons())
    
    elif call.data in ["price_regular", "price_dealer"] and chat_id in user_states and isinstance(user_states[chat_id], dict) and user_states[chat_id].get('state') == 'searching':
        state = user_states[chat_id]
        state['waiting_for_add'] = True
        state['price_type'] = call.data
        row_num, row = state['results'][state['index']]
        stock = get_stock_quantity(row[1])
        bot.edit_message_text(f"🛒 Товар:\n{get_full_item_info(row_num, row)}\nВыбран заказ: {state['selected_order']}\nНа складе: {stock} шт.\nСколько штук добавить?",
                            chat_id, call.message.message_id, reply_markup=create_back_button())
    
    elif call.data == "edit_order":
        order_sheet = ensure_orders_sheet()
        orders = get_order_list(order_sheet)
        if not orders:
            bot.edit_message_text("🛒 Нет заказов для редактирования.", chat_id, call.message.message_id, reply_markup=create_back_button())
            return
        user_states[chat_id] = {'state': 'selecting_order_to_edit', 'order_page': 0}
        bot.edit_message_text("📋 Выбери заказ для редактирования:", chat_id, call.message.message_id, reply_markup=create_order_buttons(orders, 0, "edit"))
    
    elif call.data.startswith("select_order_") and chat_id in user_states and isinstance(user_states[chat_id], dict) and user_states[chat_id].get('state') == 'selecting_order_to_edit':
        order_name = call.data.replace("select_order_", "")
        order_sheet = ensure_orders_sheet()
        start_row, end_row = find_order_block(order_sheet, order_name)
        if start_row is None or end_row is None or start_row > end_row:
            bot.edit_message_text(f"❌ Заказ '{order_name}' не найден или повреждён.", chat_id, call.message.message_id, reply_markup=create_back_button())
            return
        block_data = order_sheet.get(f'A{start_row}:E{end_row}')
        user_states[chat_id] = {
            'state': 'editing_order',
            'order_name': order_name,
            'start_row': start_row,
            'end_row': end_row,
            'block_data': block_data,
            'result_message_id': call.message.message_id
        }
        show_order_items(chat_id, call.message.message_id)
    
    elif call.data == "edit_item_qty" and chat_id in user_states and isinstance(user_states[chat_id], dict) and user_states[chat_id].get('state') == 'editing_order':
        state = user_states[chat_id]
        valid_items = [item for item in state['block_data'][1:-1] if item and len(item) >= 4 and item[1]]
        if not valid_items:
            bot.edit_message_text("❌ Нет товаров для редактирования.", chat_id, call.message.message_id, reply_markup=create_order_edit_buttons())
            return
        state['selecting_item'] = True
        state['item_page'] = 0
        state['action'] = 'edit'
        bot.edit_message_text(f"📏 Выбери товар для изменения количества:\n{format_order_table(state['block_data'], state['start_row'])}", 
                             chat_id, call.message.message_id, reply_markup=create_item_selection_buttons(valid_items, 0, "edit"), parse_mode='HTML')
    
    elif call.data == "delete_item" and chat_id in user_states and isinstance(user_states[chat_id], dict) and user_states[chat_id].get('state') == 'editing_order':
        state = user_states[chat_id]
        valid_items = [item for item in state['block_data'][1:-1] if item and len(item) >= 4 and item[1]]
        if not valid_items:
            bot.edit_message_text("❌ Нет товаров для удаления.", chat_id, call.message.message_id, reply_markup=create_order_edit_buttons())
            return
        state['selecting_item'] = True
        state['item_page'] = 0
        state['action'] = 'delete'
        bot.edit_message_text(f"🗑 Выбери товар для удаления:\n{format_order_table(state['block_data'], state['start_row'])}", 
                             chat_id, call.message.message_id, reply_markup=create_item_selection_buttons(valid_items, 0, "delete"), parse_mode='HTML')
    
    elif call.data.startswith("prev_items_") or call.data.startswith("next_items_"):
        if chat_id in user_states and isinstance(user_states[chat_id], dict) and user_states[chat_id].get('state') == 'editing_order':
            state = user_states[chat_id]
            valid_items = [item for item in state['block_data'][1:-1] if item and len(item) >= 4 and item[1]]
            parts = call.data.split("_")
            page = int(parts[2])
            action = parts[3]
            state['item_page'] = page
            if action == "edit":
                text = f"📏 Выбери товар для изменения количества:\n{format_order_table(state['block_data'], state['start_row'])}"
            elif action == "delete":
                text = f"🗑 Выбери товар для удаления:\n{format_order_table(state['block_data'], state['start_row'])}"
            bot.edit_message_text(text, chat_id, call.message.message_id, reply_markup=create_item_selection_buttons(valid_items, page, action), parse_mode='HTML')
    
    elif call.data.startswith("select_item_") and chat_id in user_states and isinstance(user_states[chat_id], dict) and user_states[chat_id].get('state') == 'editing_order':
        state = user_states[chat_id]
        parts = call.data.split("_")
        item_index = int(parts[2])
        action = parts[3]
        valid_items = [item for item in state['block_data'][1:-1] if item and len(item) >= 4 and item[1]]
        if item_index >= len(valid_items):
            bot.edit_message_text("❌ Товар не найден.", chat_id, call.message.message_id, reply_markup=create_order_edit_buttons())
            return
        item = valid_items[item_index]
        if action == "edit":
            state['selected_item_index'] = item_index
            state['waiting_for_qty'] = True
            del state['selecting_item']
            del state['action']
            stock = get_stock_quantity(item[1].replace('🛒 ', ''))
            bot.edit_message_text(f"📏 Введи новое количество для товара '{item[1].replace('🛒 ', '')}' (на складе: {stock} шт.):", 
                                 chat_id, call.message.message_id, reply_markup=create_back_button())
        elif action == "delete":
            row_num = state['start_row'] + state['block_data'].index(item)
            order_sheet = ensure_orders_sheet()
            order_sheet.delete_rows(row_num, row_num)
            state['end_row'] -= 1
            block_data = order_sheet.get(f'A{state["start_row"]}:E{state["end_row"]}')
            total = sum(float(row[4].replace(',', '.')) for row in block_data if len(row) > 4 and row[4] and row[1])
            order_sheet.update_cell(state['end_row'], 5, total)
            total_format = CellFormat(
                backgroundColor=Color(0.9, 1, 0.9),
                textFormat=TextFormat(fontFamily='Roboto', fontSize=11, bold=True),
                horizontalAlignment='RIGHT')
            format_cell_range(order_sheet, f'D{state["end_row"]}:E{state["end_row"]}', total_format)
            state['block_data'] = block_data
            del state['selecting_item']
            del state['action']
            response = f"🗑 Товар '{item[1].replace('🛒 ', '')}' удалён!\n{format_order_table(state['block_data'], state['start_row'])}"
            try:
                bot.edit_message_text(response, chat_id, call.message.message_id, reply_markup=create_order_edit_buttons(), parse_mode='HTML')
            except telebot.apihelper.ApiTelegramException as e:
                if "message is not modified" not in str(e):
                    raise e
    
    elif call.data == "delete_order" and chat_id in user_states and isinstance(user_states[chat_id], dict) and user_states[chat_id].get('state') == 'editing_order':
        state = user_states[chat_id]
        order_name = state['order_name']
        start_row, end_row = find_order_block(ensure_orders_sheet(), order_name)
        if start_row is None or end_row is None or start_row > end_row:
            bot.edit_message_text(f"❌ Заказ '{order_name}' не найден или повреждён.", chat_id, call.message.message_id, reply_markup=create_main_menu())
            del user_states[chat_id]
            return
        order_sheet = ensure_orders_sheet()
        num_rows = end_row - start_row + 1
        order_sheet.delete_rows(start_row, start_row + num_rows - 1)
        bot.edit_message_text(f"🗑 Заказ '{order_name}' удалён!", chat_id, call.message.message_id, reply_markup=create_main_menu())
        del user_states[chat_id]
    
    elif call.data == "complete_order" and chat_id in user_states and isinstance(user_states[chat_id], dict) and user_states[chat_id].get('state') == 'editing_order':
        state = user_states[chat_id]
        order_name = state['order_name']
        start_row, end_row = state['start_row'], state['end_row']
        order_sheet = ensure_orders_sheet()
        block_data = order_sheet.get(f'A{start_row}:E{end_row}')
        df = pd.DataFrame(block_data, columns=['Название заказа', 'Товар', 'Количество', 'Цена', 'Сумма'])
        file_path = f"{order_name}.xlsx"
        df.to_excel(file_path, index=False)
        with open(file_path, 'rb') as file:
            bot.send_document(chat_id, file, caption=f"📄 Заказ '{order_name}' завершён! Вот твой файл.")
        os.remove(file_path)
        bot.send_message(chat_id, "🏠 Ты вернулся в главное меню! Что дальше? 😊", reply_markup=create_main_menu())
        del user_states[chat_id]

# Обработка сообщений в зависимости от состояния
@bot.message_handler(func=lambda message: message.chat.id in user_states)
def process_state(message):
    chat_id = message.chat.id
    state = user_states.get(chat_id)
    
    if state == 'waiting_for_neworder':
        try:
            order_name = message.text.strip()
            if not order_name:
                bot.reply_to(message, "📛 Название не может быть пустым! Попробуй ещё раз:", reply_markup=create_back_button())
                return
            order_sheet = ensure_orders_sheet()
            orders = get_order_list(order_sheet)
            if order_name in orders:
                bot.reply_to(message, f"⚠️ Заказ '{order_name}' уже есть. Придумай другое название:", reply_markup=create_back_button())
                return
            all_data = order_sheet.get_all_values()
            new_start = 2 if len(all_data) <= 1 else len(all_data) + 1
            order_sheet.update(range_name=f'A{new_start}:E{new_start}', values=[[f'📋 {order_name}', '', '', '', '']])
            order_sheet.update(values=[['Итого', 0]], range_name=f'D{new_start + 1}:E{new_start + 1}')
            total_format = CellFormat(
                backgroundColor=Color(0.9, 1, 0.9),
                textFormat=TextFormat(fontFamily='Roboto', fontSize=11, bold=True),
                horizontalAlignment='RIGHT')
            format_cell_range(order_sheet, f'D{new_start + 1}:E{new_start + 1}', total_format)
            bot.reply_to(message, f"✅ Заказ '{order_name}' успешно создан! Теперь можно добавлять товары 🛒", reply_markup=create_main_menu())
            del user_states[chat_id]
        except Exception as e:
            bot.reply_to(message, f"❌ Ошибка: {str(e)}. Попробуй снова!", reply_markup=create_back_button())
    
    elif state == 'waiting_for_search':
        try:
            query = message.text.strip().lower()
            sheet = find_warehouse_sheet()
            if not sheet:
                bot.reply_to(message, "❌ Лист 'СКЛАД' не найден. Проверь настройки!", reply_markup=create_back_button())
                return
            all_data = sheet.get_all_values()
            search_results = []
            for i, row in enumerate(all_data, 1):
                if len(row) >= 2 and row[1].lower().startswith(query):
                    formatted = format_row(row)
                    search_results.append((i, formatted))
            if not search_results:
                bot.reply_to(message, f"🔍 По запросу '{query}' ничего не найдено 😕", reply_markup=create_main_menu())
                del user_states[chat_id]
                return
            result_message = bot.reply_to(message, "⏳ Загружаю результаты...", reply_markup=create_search_buttons())
            user_states[chat_id] = {
                'state': 'searching',
                'results': search_results,
                'index': 0,
                'result_message_id': result_message.message_id
            }
            show_search_result(chat_id, result_message.message_id)
        except Exception as e:
            bot.reply_to(message, f"❌ Ошибка: {str(e)}. Попробуй снова!", reply_markup=create_back_button())
    
    elif isinstance(state, dict) and state.get('state') == 'searching' and 'edit_action' in state:
        try:
            sheet = find_warehouse_sheet()
            if not sheet:
                bot.reply_to(message, "❌ Лист 'СКЛАД' не найден. Проверь настройки!", reply_markup=create_back_button())
                return
            row_num, row_data = state['results'][state['index']]
            action = state['edit_action']
            value = message.text.strip()
            column_map = {'quantity': 3, 'reserve': 4, 'name': 2, 'price': 5, 'reserve2': 6, 'dealer_price': 7}
            if action == 'quantity':
                new_value = int(value)
                stock = get_stock_quantity(row_data[1])
                if new_value < 0:
                    bot.reply_to(message, "⚠️ Количество не может быть меньше 0!", reply_markup=create_back_button())
                    return
                sheet.update_cell(row_num, column_map[action], new_value)
            elif action == 'reserve':
                current_value = int(row_data[column_map[action] - 1]) if row_data[column_map[action] - 1] != '-' else 0
                change = int(value)
                new_value = current_value + change
                if new_value < 0:
                    bot.reply_to(message, f"⚠️ Значение должно быть больше 0. Сейчас: {current_value}", reply_markup=create_back_button())
                    return
                stock = get_stock_quantity(row_data[1])
                if new_value > stock:
                    bot.reply_to(message, f"⚠️ На складе только {stock} шт. Введи меньшее значение!", reply_markup=create_back_button())
                    return
                sheet.update_cell(row_num, column_map[action], new_value)
            elif action == 'reserve2':
                current_value = int(row_data[column_map[action] - 1]) if row_data[column_map[action] - 1] != '-' else 0
                change = int(value)
                new_value = current_value + change
                if new_value < 0:
                    bot.reply_to(message, f"⚠️ Значение должно быть больше 0. Сейчас: {current_value}", reply_markup=create_back_button())
                    return
                stock = get_stock_quantity(row_data[1])
                if new_value > stock:
                    bot.reply_to(message, f"⚠️ На складе только {stock} шт. Введи меньшее значение!", reply_markup=create_back_button())
                    return
                sheet.update_cell(row_num, column_map[action], new_value)
            elif action == 'name':
                sheet.update_cell(row_num, column_map[action], value)
            elif action == 'price':
                price = float(value.replace(',', '.'))
                sheet.update_cell(row_num, column_map[action], price)
            elif action == 'dealer_price':
                price = float(value.replace(',', '.'))
                sheet.update_cell(row_num, column_map[action], price)
            state['results'][state['index']] = (row_num, format_row(sheet.row_values(row_num)))
            del state['edit_action']
            show_search_result(chat_id, state['result_message_id'])
        except ValueError as ve:
            bot.reply_to(message, f"❌ Ошибка: {str(ve)}. Введи корректное значение!", reply_markup=create_back_button())
        except Exception as e:
            bot.reply_to(message, f"❌ Ошибка: {str(e)}. Попробуй снова!", reply_markup=create_back_button())
    
    elif isinstance(state, dict) and state.get('state') == 'searching' and state.get('waiting_for_add'):
        try:
            qty = int(message.text.strip())
            order_name = state['selected_order']
            row_num, row_data = state['results'][state['index']]
            stock = get_stock_quantity(row_data[1])
            if qty <= 0:
                bot.reply_to(message, "⚠️ Количество должно быть больше 0!", reply_markup=create_back_button())
                return
            if qty > stock:
                bot.reply_to(message, f"⚠️ На складе только {stock} шт. Введи меньшее количество!", reply_markup=create_back_button())
                return
            price_col = 4 if state['price_type'] == "price_regular" else 6
            price_str = row_data[price_col].replace(' ₽', '').replace(',', '.') if row_data[price_col] != '-' else '0'
            price = float(price_str)
            line_total = qty * price
            order_sheet = ensure_orders_sheet()
            orders = get_order_list(order_sheet)
            if order_name not in orders:
                bot.reply_to(message, f"❌ Заказ '{order_name}' пропал! Создай новый.", reply_markup=create_back_button())
                return
            start_row, end_row = find_order_block(order_sheet, order_name)
            has_total_row = (order_sheet.cell(end_row, 4).value == 'Итого')
            insert_row = end_row if has_total_row else end_row
            order_sheet.insert_row(['', f'🛒 {row_data[1]}', qty, price, line_total], insert_row)
            total_row = end_row + 1 if has_total_row else end_row
            block_data = order_sheet.get(f'A{start_row}:E{total_row}')
            total = sum(float(row[4].replace(',', '.')) for row in block_data if len(row) > 4 and row[4] and row[1])
            if has_total_row:
                order_sheet.update_cell(total_row, 5, total)
            else:
                order_sheet.update(values=[['Итого', total]], range_name=f'D{total_row}:E{total_row}')
            total_format = CellFormat(
                backgroundColor=Color(0.9, 1, 0.9),
                textFormat=TextFormat(fontFamily='Roboto', fontSize=11, bold=True),
                horizontalAlignment='RIGHT')
            format_cell_range(order_sheet, f'D{total_row}:E{total_row}', total_format)
            del state['waiting_for_add']
            del state['selected_order']
            del state['price_type']
            show_search_result(chat_id, state['result_message_id'])
        except ValueError as ve:
            bot.reply_to(message, f"❌ Ошибка: {str(ve)}. Введи число!", reply_markup=create_back_button())
        except Exception as e:
            bot.reply_to(message, f"❌ Ошибка: {str(e)}. Попробуй снова!", reply_markup=create_back_button())
    
    elif isinstance(state, dict) and state.get('state') == 'editing_order' and state.get('waiting_for_qty'):
        try:
            new_qty = int(message.text.strip())
            valid_items = [item for item in state['block_data'][1:-1] if item and len(item) >= 4 and item[1]]
            item_index = state['selected_item_index']
            if not valid_items or item_index >= len(valid_items):
                bot.reply_to(message, "❌ Нет товаров для редактирования!", reply_markup=create_order_edit_buttons())
                return
            item = valid_items[item_index]
            stock = get_stock_quantity(item[1].replace('🛒 ', ''))
            if new_qty <= 0:
                bot.reply_to(message, "⚠️ Количество должно быть больше 0!", reply_markup=create_back_button())
                return
            if new_qty > stock:
                bot.reply_to(message, f"⚠️ На складе только {stock} шт. Введи меньшее количество!", reply_markup=create_back_button())
                return
            row_num = state['start_row'] + state['block_data'].index(item)
            order_sheet = ensure_orders_sheet()
            order_sheet.update_cell(row_num, 3, new_qty)
            price_str = order_sheet.cell(row_num, 4).value or '0'
            price = float(price_str.replace(',', '.'))
            line_total = new_qty * price
            order_sheet.update_cell(row_num, 5, line_total)
            start_row, end_row = state['start_row'], state['end_row']
            block_data = order_sheet.get(f'A{start_row}:E{end_row}')
            total = sum(float(row[4].replace(',', '.')) for row in block_data if len(row) > 4 and row[4] and row[1])
            order_sheet.update_cell(end_row, 5, total)
            total_format = CellFormat(
                backgroundColor=Color(0.9, 1, 0.9),
                textFormat=TextFormat(fontFamily='Roboto', fontSize=11, bold=True),
                horizontalAlignment='RIGHT')
            format_cell_range(order_sheet, f'D{end_row}:E{end_row}', total_format)
            state['block_data'] = block_data
            bot.reply_to(message, f"✅ Количество обновлено: {new_qty} для '{item[1].replace('🛒 ', '')}'", reply_markup=create_back_button())
            del state['waiting_for_qty']
            del state['selected_item_index']
            show_order_items(chat_id, state['result_message_id'])
        except ValueError:
            bot.reply_to(message, "❌ Введи корректное число!", reply_markup=create_back_button())
        except Exception as e:
            bot.reply_to(message, f"❌ Ошибка: {str(e)}. Попробуй снова!", reply_markup=create_back_button())

@bot.message_handler(func=lambda message: message.chat.id not in user_states)
def default_handler(message):
    bot.reply_to(message, "👇 Выбери действие из меню:", reply_markup=create_main_menu())
    
def run_flask():
    app.run(host='0.0.0.0', port=8080)

if __name__ == "__main__":
    # Проверяем, что бот не запущен повторно
    lock_file = "/tmp/tgbot.lock"
    if os.path.exists(lock_file):
        print("Бот уже запущен, завершаю этот экземпляр.")
        exit(1)
    with open(lock_file, 'w') as f:
        f.write(str(os.getpid()))

    threading.Thread(target=run_flask, daemon=True).start()
    try:
        bot.polling(none_stop=True)
    finally:
        os.remove(lock_file)  # Удаляем lock-файл при завершении
