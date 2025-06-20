import telebot
import requests
import time
import json
import os
import random
import string
import hashlib # Thêm thư viện hashlib cho MD5
from datetime import datetime, timedelta
from threading import Thread, Event, Lock

from flask import Flask, request

# --- Cấu hình Bot (ĐẶT TRỰC TIẾP TẠI ĐÂY) ---
# THAY THẾ 'YOUR_BOT_TOKEN_HERE' BẰNG TOKEN THẬT CỦA BẠN
BOT_TOKEN = "7949117582:AAG-vTt2h_IEQpZ2TtlAIxjd_U9u3h_XLDc" 
# THAY THẾ BẰNG ID ADMIN THẬT CỦA BẠN. Có thể có nhiều ID, cách nhau bởi dấu phẩy.
ADMIN_IDS = [6915752059] # Ví dụ: [6915752059, 123456789]

DATA_FILE = 'user_data.json'
CAU_PATTERNS_FILE = 'cau_patterns.json'
CODES_FILE = 'codes.json'

# --- Cấu hình nâng cao ---
TX_HISTORY_LENGTH = 7 # Chiều dài lịch sử cầu để học mẫu (trước đây là 5)
MD5_LOOKBACK_LENGTH = 3 # Số lượng kết quả MD5 gần nhất để kiểm tra (2 Gãy => 1 Khác)

# --- Khởi tạo Flask App và Telegram Bot ---
app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN)

# Global flags và objects
bot_enabled = True
bot_disable_reason = "Không có"
bot_disable_admin_id = None
prediction_stop_event = Event() # Để kiểm soát luồng dự đoán
bot_initialized = False # Cờ để đảm bảo bot chỉ được khởi tạo một lần
bot_init_lock = Lock() # Khóa để tránh race condition khi khởi tạo

# Global data structures
user_data = {}
# Thay thế CAU_XAU/CAU_DEP bằng CAU_PATTERNS với confidence score
CAU_PATTERNS = {} # {pattern_string: confidence_score (float)}
GENERATED_CODES = {} # {code: {"value": 1, "type": "day", "used_by": null, "used_time": null}}
md5_results_history = [] # Lịch sử kết quả MD5 ('Gãy' hoặc 'Khác')

# --- Quản lý dữ liệu người dùng, mẫu cầu và code ---
def load_user_data():
    global user_data
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r') as f:
            try:
                user_data = json.load(f)
            except json.JSONDecodeError:
                print(f"Lỗi đọc {DATA_FILE}. Khởi tạo lại dữ liệu người dùng.")
                user_data = {}
    else:
        user_data = {}
    print(f"Loaded {len(user_data)} user records from {DATA_FILE}")

def save_user_data(data):
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f, indent=4)

def load_cau_patterns():
    global CAU_PATTERNS
    if os.path.exists(CAU_PATTERNS_FILE):
        with open(CAU_PATTERNS_FILE, 'r') as f:
            try:
                CAU_PATTERNS = json.load(f)
            except json.JSONDecodeError:
                print(f"Lỗi đọc {CAU_PATTERNS_FILE}. Khởi tạo lại mẫu cầu.")
                CAU_PATTERNS = {}
    else:
        CAU_PATTERNS = {}
    print(f"Loaded {len(CAU_PATTERNS)} patterns.")

def save_cau_patterns():
    with open(CAU_PATTERNS_FILE, 'w') as f:
        json.dump(CAU_PATTERNS, f, indent=4)

def load_codes():
    global GENERATED_CODES
    if os.path.exists(CODES_FILE):
        with open(CODES_FILE, 'r') as f:
            try:
                GENERATED_CODES = json.load(f)
            except json.JSONDecodeError:
                print(f"Lỗi đọc {CODES_FILE}. Khởi tạo lại mã code.")
                GENERATED_CODES = {}
    else:
        GENERATED_CODES = {}
    print(f"Loaded {len(GENERATED_CODES)} codes from {CODES_FILE}")

def save_codes():
    with open(CODES_FILE, 'w') as f:
        json.dump(GENERATED_CODES, f, indent=4)

def is_admin(user_id):
    return user_id in ADMIN_IDS

def is_ctv(user_id):
    return is_admin(user_id) or (str(user_id) in user_data and user_data[str(user_id)].get('is_ctv'))

def check_subscription(user_id):
    user_id_str = str(user_id)
    if is_admin(user_id) or is_ctv(user_id):
        return True, "Bạn là Admin/CTV, quyền truy cập vĩnh viễn."

    if user_id_str not in user_data or user_data[user_id_str].get('expiry_date') is None:
        return False, "⚠️ Bạn chưa đăng ký hoặc tài khoản chưa được gia hạn."

    expiry_date_str = user_data[user_id_str]['expiry_date']
    expiry_date = datetime.strptime(expiry_date_str, '%Y-%m-%d %H:%M:%S')

    if datetime.now() < expiry_date:
        remaining_time = expiry_date - datetime.now()
        days = remaining_time.days
        hours = remaining_time.seconds // 3600
        minutes = (remaining_time.seconds % 3600) // 60
        seconds = remaining_time.seconds % 60
        return True, f"✅ Tài khoản của bạn còn hạn đến: `{expiry_date_str}` ({days} ngày {hours} giờ {minutes} phút {seconds} giây)."
    else:
        return False, "❌ Tài khoản của bạn đã hết hạn."

# --- Logic dự đoán Tài Xỉu ---
def du_doan_theo_xi_ngau(dice_list):
    if not dice_list:
        return "Đợi thêm dữ liệu"
    d1, d2, d3 = dice_list[-1]
    total = d1 + d2 + d3

    # Một phương pháp dự đoán đơn giản dựa trên tổng và từng con xúc xắc
    # Có thể phức tạp hơn với các quy tắc khác
    results = []
    for d in [d1, d2, d3]:
        tmp = d + total
        while tmp > 6: # Đảm bảo nằm trong phạm vi 1-6
            tmp -= 6
        if tmp % 2 == 0:
            results.append("Tài")
        else:
            results.append("Xỉu")

    # Chọn kết quả xuất hiện nhiều nhất, nếu hòa thì ưu tiên Tài
    tai_count = results.count("Tài")
    xiu_count = results.count("Xỉu")
    if tai_count >= xiu_count:
        return "Tài"
    else:
        return "Xỉu"


def tinh_tai_xiu(dice):
    total = sum(dice)
    return "Tài" if total >= 11 else "Xỉu", total

# --- Cập nhật mẫu cầu động và độ tin cậy ---
def update_cau_patterns(pattern_str, prediction_correct):
    global CAU_PATTERNS
    # Tăng/giảm confidence score. Giả sử 1.0 là điểm khởi đầu, min 0.1, max 5.0
    initial_confidence = 1.0
    increase_factor = 0.2
    decrease_factor = 0.5 # Giảm nhiều hơn khi sai để nhanh chóng loại bỏ mẫu xấu

    current_confidence = CAU_PATTERNS.get(pattern_str, initial_confidence)

    if prediction_correct:
        new_confidence = min(current_confidence + increase_factor, 5.0)
    else:
        new_confidence = max(current_confidence - decrease_factor, 0.1)
    
    CAU_PATTERNS[pattern_str] = new_confidence
    save_cau_patterns()
    # print(f"Cập nhật mẫu cầu '{pattern_str}': Confidence mới = {new_confidence:.2f}")

def get_pattern_prediction_adjustment(pattern_str):
    """
    Trả về yếu tố điều chỉnh dự đoán dựa trên confidence score của mẫu cầu.
    Nếu confidence cao (> ngưỡng), có thể tin tưởng. Nếu thấp (< ngưỡng), có thể đảo chiều.
    """
    confidence = CAU_PATTERNS.get(pattern_str, 1.0)
    
    if confidence >= 2.5: # Ngưỡng để coi là cầu đẹp đáng tin
        return "giữ nguyên"
    elif confidence <= 0.5: # Ngưỡng để coi là cầu xấu, cần đảo chiều
        return "đảo chiều"
    else:
        return "theo xí ngầu" # Không đủ độ tin cậy để điều chỉnh

# --- Lấy dữ liệu từ API ---
def lay_du_lieu():
    try:
        response = requests.get("https://1.bot/GetNewLottery/LT_Taixiu")
        response.raise_for_status() # Báo lỗi nếu status code là lỗi HTTP
        data = response.json()
        if data.get("state") != 1:
            # print(f"API trả về state không thành công: {data.get('state')}")
            return None
        return data.get("data")
    except requests.exceptions.RequestException as e:
        print(f"Lỗi khi lấy dữ liệu từ API: {e}")
        return None
    except json.JSONDecodeError:
        print("Lỗi giải mã JSON từ API. Phản hồi không phải JSON hợp lệ.")
        return None

def calculate_md5_outcome(open_code_str, expect_str):
    """
    Giả định cách tính MD5 và so sánh để xác định 'Gãy' hoặc 'Khác'.
    Đây là phần bạn cần điều chỉnh dựa trên quy tắc MD5 thực tế của game.
    Ví dụ: so sánh MD5 của chuỗi "OpenCode-Expect" với một giá trị cố định hoặc một quy tắc nào đó.
    Ở đây, tôi tạo một ví dụ đơn giản: nếu MD5 của (OpenCode + Expect) chứa '00' thì là 'Gãy', ngược lại là 'Khác'.
    """
    combined_string = f"{open_code_str}-{expect_str}"
    md5_hash = hashlib.md5(combined_string.encode('utf-8')).hexdigest()
    
    # Đây là ví dụ logic MD5. Bạn cần thay thế bằng logic thực tế của bạn.
    # Ví dụ: kiểm tra các ký tự cụ thể trong hash, hoặc so sánh với một hash mẫu.
    if '00' in md5_hash[-2:]: # Ví dụ: 2 ký tự cuối cùng là '00' thì gãy
        return 'Gãy'
    else:
        return 'Khác'

# --- Logic chính của Bot dự đoán (chạy trong luồng riêng) ---
def prediction_loop(stop_event: Event):
    last_id = None
    tx_history = []
    global md5_results_history # Dùng biến global
    
    print("Prediction loop started.")
    while not stop_event.is_set():
        if not bot_enabled:
            # print(f"Bot dự đoán đang tạm dừng. Lý do: {bot_disable_reason}")
            time.sleep(10) # Ngủ lâu hơn khi bot bị tắt
            continue

        data = lay_du_lieu()
        if not data:
            # print("❌ Không lấy được dữ liệu từ API hoặc dữ liệu không hợp lệ.")
            time.sleep(5)
            continue

        issue_id = data.get("ID")
        expect = data.get("Expect")
        open_code = data.get("OpenCode")

        if not all([issue_id, expect, open_code]):
            # print("Dữ liệu API không đầy đủ (thiếu ID, Expect, hoặc OpenCode). Bỏ qua phiên này.")
            time.sleep(5)
            continue

        if issue_id != last_id:
            try:
                dice = tuple(map(int, open_code.split(",")))
            except ValueError:
                print(f"Lỗi phân tích OpenCode: '{open_code}'. Bỏ qua phiên này.")
                last_id = issue_id # Vẫn cập nhật last_id để không lặp lại lỗi
                time.sleep(5)
                continue
            
            ket_qua_tx, tong = tinh_tai_xiu(dice)

            # Cập nhật lịch sử MD5
            md5_outcome = calculate_md5_outcome(open_code, expect)
            md5_results_history.append(md5_outcome)
            if len(md5_results_history) > MD5_LOOKBACK_LENGTH:
                md5_results_history.pop(0)

            # Lưu lịch sử phiên Tài Xỉu
            if len(tx_history) >= TX_HISTORY_LENGTH:
                tx_history.pop(0)
            tx_history.append("T" if ket_qua_tx == "Tài" else "X")

            next_expect = str(int(expect) + 1).zfill(len(expect))
            du_doan_co_so = du_doan_theo_xi_ngau([dice]) # Dự đoán ban đầu theo xí ngầu
            du_doan_cuoi_cung = du_doan_co_so
            ly_do = ""
            current_cau_str = ""

            # --- Logic điều chỉnh dự đoán ---
            # 1. Áp dụng quy tắc MD5 (nếu có đủ lịch sử và quy tắc MD5 chỉ ra)
            md5_rule_applied = False
            if len(md5_results_history) >= 2 and md5_results_history[-2:] == ['Gãy', 'Gãy']:
                # Nếu 2 lần gần nhất là 'Gãy', thì lần này sẽ là 'Khác'
                # 'Khác' nghĩa là nó không gãy, tức là dự đoán theo xí ngầu có khả năng đúng
                # Nếu dự đoán MD5 "gãy" thì ta đảo chiều, nếu "không gãy" thì ta giữ nguyên
                # Trong trường hợp này, nếu 2 lần liên tiếp là Gãy, thì lần này là "Khác" (không gãy)
                # nghĩa là dự đoán cơ sở (xí ngầu) sẽ đúng.
                # Tuy nhiên, nếu "Khác" nghĩa là sẽ "gãy" (đảo chiều) thì phải đảo.
                # Cần làm rõ "kết quả khác" là gì trong ngữ cảnh dự đoán.
                # Giả sử "Khác" trong ngữ cảnh này là dự đoán cơ sở sẽ đúng.
                # Nếu không, bạn cần điều chỉnh logic ở đây.
                
                # Với ngữ cảnh "2 Gãy -> 1 Khác", giả định "Khác" nghĩa là dự đoán cơ sở sẽ "không Gãy" -> đúng
                # Vậy nếu dự đoán cơ sở là Tài, và MD5 là "Khác" (nghĩa là không gãy), thì giữ nguyên Tài.
                # Logic này có thể phức tạp tùy thuộc vào ý nghĩa thực sự của 'Gãy'/'Khác' trong game của bạn.
                # TẠM THỜI BỎ QUA VIỆC ĐẢO CHIỀU DỰ ĐOÁN VÀ CHỈ GHI NHẬN LÝ DO
                # Nếu "Khác" có nghĩa là phải đảo chiều dự đoán, bạn cần uncomment và chỉnh sửa dòng dưới
                # du_doan_cuoi_cung = "Xỉu" if du_doan_co_so == "Tài" else "Tài"
                ly_do = f"AI Theo quy tắc MD5 (2 Gãy -> 1 Khác) → {du_doan_co_so}"
                md5_rule_applied = True
                print(f"MD5 history: {md5_results_history}. Quy tắc 2 Gãy -> 1 Khác được áp dụng.")


            # 2. Áp dụng mẫu cầu động (chỉ nếu quy tắc MD5 chưa được áp dụng rõ ràng)
            if not md5_rule_applied and len(tx_history) == TX_HISTORY_LENGTH:
                current_cau_str = ''.join(tx_history)
                pattern_adjustment = get_pattern_prediction_adjustment(current_cau_str)

                if pattern_adjustment == "giữ nguyên":
                    ly_do = f"AI Cầu đẹp ({current_cau_str}) → Giữ nguyên kết quả"
                elif pattern_adjustment == "đảo chiều":
                    du_doan_cuoi_cung = "Xỉu" if du_doan_co_so == "Tài" else "Tài" # Đảo chiều
                    ly_do = f"AI Cầu xấu ({current_cau_str}) → Đảo chiều kết quả"
                else:
                    ly_do = f"AI Không rõ/Đang học mẫu cầu ({current_cau_str}) → Dự đoán theo xí ngầu"
            elif not md5_rule_applied:
                ly_do = "AI Dự đoán theo xí ngầu (chưa đủ lịch sử cầu)"


            # Cập nhật độ tin cậy của mẫu cầu dựa trên kết quả thực tế
            if len(tx_history) == TX_HISTORY_LENGTH:
                prediction_correct = (du_doan_cuoi_cung == "Tài" and ket_qua_tx == "Tài") or \
                                     (du_doan_cuoi_cung == "Xỉu" and ket_qua_tx == "Xỉu")
                update_cau_patterns(current_cau_str, prediction_correct)

            # Gửi tin nhắn dự đoán tới tất cả người dùng có quyền truy cập
            for user_id_str, user_info in list(user_data.items()): # Dùng list() để tránh lỗi khi user_data thay đổi
                user_id = int(user_id_str)
                is_sub, sub_message = check_subscription(user_id)
                if is_sub:
                    try:
                        prediction_message = (
                            "🎮 **KẾT QUẢ PHIÊN HIỆN TẠI** 🎮\n"
                            f"Phiên: `{expect}` | Kết quả: **{ket_qua_tx}** (Tổng: **{tong}**)\n"
                            f"Kết quả MD5: `{md5_outcome}`\n\n" # Hiển thị kết quả MD5
                            f"**Dự đoán cho phiên tiếp theo:**\n"
                            f"🔢 Phiên: `{next_expect}`\n"
                            f"🤖 Dự đoán: **{du_doan_cuoi_cung}**\n"
                            f"📌 Lý do: _{ly_do}_\n"
                            f"⚠️ **Hãy đặt cược sớm trước khi phiên kết thúc!**"
                        )
                        bot.send_message(user_id, prediction_message, parse_mode='Markdown')
                    except telebot.apihelper.ApiTelegramException as e:
                        if "bot was blocked by the user" in str(e) or "user is deactivated" in str(e):
                            # print(f"Người dùng {user_id} đã chặn bot hoặc bị vô hiệu hóa.")
                            pass
                        else:
                            print(f"Lỗi gửi tin nhắn cho user {user_id}: {e}")
                    except Exception as e:
                        print(f"Lỗi không xác định khi gửi tin nhắn cho user {user_id}: {e}")

            print("-" * 50)
            print("🎮 Kết quả phiên hiện tại: {} (Tổng: {}), MD5: {}".format(ket_qua_tx, tong, md5_outcome))
            print("🔢 Phiên: {} → {}".format(expect, next_expect))
            print("🤖 Dự đoán: {}".format(du_doan_cuoi_cung))
            print("📌 Lý do: {}".format(ly_do))
            print("Lịch sử TX: {}. Lịch sử MD5: {}".format(''.join(tx_history), md5_results_history))
            print("-" * 50)

            last_id = issue_id

        time.sleep(5) # Đợi 5 giây trước khi kiểm tra phiên mới
    print("Prediction loop stopped.")

# --- Xử lý lệnh Telegram ---

@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = str(message.chat.id)
    username = message.from_user.username or message.from_user.first_name
    
    if user_id not in user_data:
        user_data[user_id] = {
            'username': username,
            'expiry_date': None,
            'is_ctv': False
        }
        save_user_data(user_data)
        bot.reply_to(message, 
                     "Chào mừng bạn đến với **BOT DỰ ĐOÁN TÀI XỈU SUNWIN**!\n"
                     "Hãy dùng lệnh /help để xem danh sách các lệnh hỗ trợ.", 
                     parse_mode='Markdown')
    else:
        user_data[user_id]['username'] = username # Cập nhật username nếu có thay đổi
        save_user_data(user_data)
        bot.reply_to(message, "Bạn đã khởi động bot rồi. Dùng /help để xem các lệnh.")

@bot.message_handler(commands=['help'])
def show_help(message):
    help_text = (
        "🤖 **DANH SÁCH LỆNH HỖ TRỢ** 🤖\n\n"
        "**Lệnh người dùng:**\n"
        "🔸 `/start`: Khởi động bot và thêm bạn vào hệ thống.\n"
        "🔸 `/help`: Hiển thị danh sách các lệnh.\n"
        "🔸 `/support`: Thông tin hỗ trợ Admin.\n"
        "🔸 `/gia`: Xem bảng giá dịch vụ.\n"
        "🔸 `/gopy <nội dung>`: Gửi góp ý/báo lỗi cho Admin.\n"
        "🔸 `/nap`: Hướng dẫn nạp tiền.\n"
        "🔸 `/dudoan`: Bắt đầu nhận dự đoán từ bot.\n"
        "🔸 `/maucau`: Hiển thị các mẫu cầu bot đã thu thập (xấu/đẹp).\n"
        "🔸 `/code <mã_code>`: Nhập mã code để gia hạn tài khoản.\n\n"
    )
    
    if is_ctv(message.chat.id):
        help_text += (
            "**Lệnh Admin/CTV:**\n"
            "🔹 `/full <id>`: Xem thông tin người dùng (để trống ID để xem của bạn).\n"
            "🔹 `/giahan <id> <số ngày/giờ>`: Gia hạn tài khoản người dùng. Ví dụ: `/giahan 12345 1 ngày` hoặc `/giahan 12345 24 giờ`.\n\n"
        )
    
    if is_admin(message.chat.id):
        help_text += (
            "**Lệnh Admin Chính:**\n"
            "👑 `/ctv <id>`: Thêm người dùng làm CTV.\n"
            "👑 `/xoactv <id>`: Xóa người dùng khỏi CTV.\n"
            "👑 `/tb <nội dung>`: Gửi thông báo đến tất cả người dùng.\n"
            "👑 `/tatbot <lý do>`: Tắt mọi hoạt động của bot dự đoán.\n"
            "👑 `/mokbot`: Mở lại hoạt động của bot dự đoán.\n"
            "👑 `/taocode <giá trị> <ngày/giờ> <số lượng>`: Tạo mã code gia hạn. Ví dụ: `/taocode 1 ngày 5` (tạo 5 code 1 ngày).\n"
        )
    
    bot.reply_to(message, help_text, parse_mode='Markdown')

@bot.message_handler(commands=['support'])
def show_support(message):
    bot.reply_to(message, 
        "Để được hỗ trợ, vui lòng liên hệ Admin:\n"
        "@heheviptool hoặc @Besttaixiu999"
    )

@bot.message_handler(commands=['gia'])
def show_price(message):
    price_text = (
        "📊 **BOT SUNWIN XIN THÔNG BÁO BẢNG GIÁ SUN BOT** 📊\n\n"
        "💸 **20k**: 1 Ngày\n"
        "💸 **50k**: 1 Tuần\n"
        "💸 **80k**: 2 Tuần\n"
        "💸 **130k**: 1 Tháng\n\n"
        "🤖 BOT SUN TỈ Lệ **85-92%**\n"
        "⏱️ ĐỌC 24/24\n\n"
        "Vui Lòng ib @heheviptool hoặc @Besttaixiu999 Để Gia Hạn"
    )
    bot.reply_to(message, price_text, parse_mode='Markdown')

@bot.message_handler(commands=['gopy'])
def send_feedback(message):
    feedback_text = telebot.util.extract_arguments(message.text)
    if not feedback_text:
        bot.reply_to(message, "Vui lòng nhập nội dung góp ý. Ví dụ: `/gopy Bot dự đoán rất chuẩn!`", parse_mode='Markdown')
        return
    
    admin_id = ADMIN_IDS[0] # Gửi cho Admin đầu tiên trong danh sách
    user_name = message.from_user.username or message.from_user.first_name
    bot.send_message(admin_id, 
                     f"📢 **GÓP Ý MỚI TỪ NGƯỜI DÙNG** 📢\n\n"
                     f"**ID:** `{message.chat.id}`\n"
                     f"**Tên:** @{user_name}\n\n"
                     f"**Nội dung:**\n`{feedback_text}`",
                     parse_mode='Markdown')
    bot.reply_to(message, "Cảm ơn bạn đã gửi góp ý! Admin đã nhận được.")

@bot.message_handler(commands=['nap'])
def show_deposit_info(message):
    user_id = message.chat.id
    deposit_text = (
        "⚜️ **NẠP TIỀN MUA LƯỢT** ⚜️\n\n"
        "Để mua lượt, vui lòng chuyển khoản đến:\n"
        "- Ngân hàng: **MB BANK**\n"
        "- Số tài khoản: **0939766383**\n"
        "- Tên chủ TK: **Nguyen Huynh Nhut Quang**\n\n"
        "**NỘI DUNG CHUYỂN KHOẢN (QUAN TRỌNG):**\n"
        "`mua luot {user_id}`\n\n"
        f"❗️ Nội dung bắt buộc của bạn là:\n"
        f"`mua luot {user_id}`\n\n"
        "(Vui lòng sao chép đúng nội dung trên để được cộng lượt tự động)\n"
        "Sau khi chuyển khoản, vui lòng chờ 1-2 phút. Nếu có sự cố, hãy dùng lệnh /support."
    )
    bot.reply_to(message, deposit_text, parse_mode='Markdown')

@bot.message_handler(commands=['dudoan'])
def start_prediction_command(message):
    user_id = message.chat.id
    is_sub, sub_message = check_subscription(user_id)
    
    if not is_sub:
        bot.reply_to(message, sub_message + "\nVui lòng liên hệ Admin @heheviptool hoặc @Besttaixiu999 để được hỗ trợ.", parse_mode='Markdown')
        return
    
    if not bot_enabled:
        bot.reply_to(message, f"❌ Bot dự đoán hiện đang tạm dừng bởi Admin. Lý do: `{bot_disable_reason}`", parse_mode='Markdown')
        return

    bot.reply_to(message, "✅ Bạn đang có quyền truy cập. Bot sẽ tự động gửi dự đoán các phiên mới nhất tại đây.")

@bot.message_handler(commands=['maucau'])
def show_cau_patterns_command(message):
    if not is_ctv(message.chat.id): # Chỉ Admin/CTV mới được xem mẫu cầu chi tiết
        bot.reply_to(message, "Bạn không có quyền sử dụng lệnh này.")
        return

    if not CAU_PATTERNS:
        pattern_text = "📚 **CÁC MẪU CẦU ĐÃ THU THUẬT** 📚\n\nKhông có mẫu cầu nào được thu thập."
    else:
        sorted_patterns = sorted(CAU_PATTERNS.items(), key=lambda item: item[1], reverse=True)
        dep_patterns_list = []
        xau_patterns_list = []

        for pattern, confidence in sorted_patterns:
            if confidence >= 2.5: # Ngưỡng "đẹp"
                dep_patterns_list.append(f"{pattern} ({confidence:.2f})")
            elif confidence <= 0.5: # Ngưỡng "xấu"
                xau_patterns_list.append(f"{pattern} ({confidence:.2f})")
            # Các mẫu ở giữa không rõ ràng thì không liệt kê vào đây

        dep_patterns_str = "\n".join(dep_patterns_list) if dep_patterns_list else "Không có"
        xau_patterns_str = "\n".join(xau_patterns_list) if xau_patterns_list else "Không có"

        pattern_text = (
            "📚 **CÁC MẪU CẦU ĐÃ THU THUẬT** 📚\n\n"
            "**🟢 Cầu Đẹp (Confidence >= 2.5):**\n"
            f"```\n{dep_patterns_str}\n```\n\n"
            "**🔴 Cầu Xấu (Confidence <= 0.5):**\n"
            f"```\n{xau_patterns_str}\n```\n"
            "*(Các mẫu cầu này được bot tự động học hỏi theo thời gian. Số trong ngoặc là điểm tin cậy)*"
        )
    bot.reply_to(message, pattern_text, parse_mode='Markdown')


@bot.message_handler(commands=['code'])
def use_code(message):
    code_str = telebot.util.extract_arguments(message.text)
    user_id = str(message.chat.id)

    if not code_str:
        bot.reply_to(message, "Vui lòng nhập mã code. Ví dụ: `/code ABCXYZ`", parse_mode='Markdown')
        return
    
    if code_str not in GENERATED_CODES:
        bot.reply_to(message, "❌ Mã code không tồn tại hoặc đã hết hạn.")
        return

    code_info = GENERATED_CODES[code_str]
    if code_info.get('used_by') is not None:
        bot.reply_to(message, "❌ Mã code này đã được sử dụng rồi.")
        return

    # Apply extension
    current_expiry_str = user_data.get(user_id, {}).get('expiry_date')
    if current_expiry_str:
        current_expiry_date = datetime.strptime(current_expiry_str, '%Y-%m-%d %H:%M:%S')
        # If current expiry is in the past, start from now
        if datetime.now() > current_expiry_date:
            new_expiry_date = datetime.now()
        else:
            new_expiry_date = current_expiry_date
    else:
        new_expiry_date = datetime.now() # Start from now if no previous expiry

    value = code_info['value']
    if code_info['type'] == 'ngày':
        new_expiry_date += timedelta(days=value)
    elif code_info['type'] == 'giờ':
        new_expiry_date += timedelta(hours=value)
    
    user_data.setdefault(user_id, {})['expiry_date'] = new_expiry_date.strftime('%Y-%m-%d %H:%M:%S')
    user_data[user_id]['username'] = message.from_user.username or message.from_user.first_name
    
    GENERATED_CODES[code_str]['used_by'] = user_id
    GENERATED_CODES[code_str]['used_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    save_user_data(user_data)
    save_codes()

    bot.reply_to(message, 
                 f"🎉 Bạn đã đổi mã code thành công! Tài khoản của bạn đã được gia hạn thêm **{value} {code_info['type']}**.\n"
                 f"Ngày hết hạn mới: `{user_expiry_date(user_id)}`", 
                 parse_mode='Markdown')

def user_expiry_date(user_id):
    if str(user_id) in user_data and user_data[str(user_id)].get('expiry_date'):
        return user_data[str(user_id)]['expiry_date']
    return "Không có"

# --- Lệnh Admin/CTV ---
@bot.message_handler(commands=['full'])
def get_user_info(message):
    if not is_ctv(message.chat.id):
        bot.reply_to(message, "Bạn không có quyền sử dụng lệnh này.")
        return
    
    args = telebot.util.extract_arguments(message.text).split()
    target_user_id_str = str(message.chat.id)
    if args and args[0].isdigit():
        target_user_id_str = args[0]
    
    if target_user_id_str not in user_data:
        bot.reply_to(message, f"Không tìm thấy thông tin cho người dùng ID `{target_user_id_str}`.")
        return

    user_info = user_data[target_user_id_str]
    expiry_date_str = user_info.get('expiry_date', 'Không có')
    username = user_info.get('username', 'Không rõ')
    is_ctv_status = "Có" if is_ctv(int(target_user_id_str)) else "Không"

    info_text = (
        f"**THÔNG TIN NGƯỜNG DÙNG**\n"
        f"**ID:** `{target_user_id_str}`\n"
        f"**Tên:** @{username}\n"
        f"**Ngày hết hạn:** `{expiry_date_str}`\n"
        f"**Là CTV/Admin:** {is_ctv_status}"
    )
    bot.reply_to(message, info_text, parse_mode='Markdown')

@bot.message_handler(commands=['giahan'])
def extend_subscription(message):
    if not is_ctv(message.chat.id):
        bot.reply_to(message, "Bạn không có quyền sử dụng lệnh này.")
        return
    
    args = telebot.util.extract_arguments(message.text).split()
    if len(args) != 3 or not args[0].isdigit() or not args[1].isdigit() or args[2].lower() not in ['ngày', 'giờ']:
        bot.reply_to(message, "Cú pháp sai. Ví dụ: `/giahan <id_nguoi_dung> <số_lượng> <ngày/giờ>`\n"
                              "Ví dụ: `/giahan 12345 1 ngày` hoặc `/giahan 12345 24 giờ`", parse_mode='Markdown')
        return
    
    target_user_id_str = args[0]
    value = int(args[1])
    unit = args[2].lower() # 'ngày' or 'giờ'
    
    if target_user_id_str not in user_data:
        user_data[target_user_id_str] = {
            'username': "UnknownUser",
            'expiry_date': None,
            'is_ctv': False
        }
        bot.send_message(message.chat.id, f"Đã tạo tài khoản mới cho user ID `{target_user_id_str}`.")

    current_expiry_str = user_data[target_user_id_str].get('expiry_date')
    if current_expiry_str:
        current_expiry_date = datetime.strptime(current_expiry_str, '%Y-%m-%d %H:%M:%S')
        if datetime.now() > current_expiry_date:
            new_expiry_date = datetime.now()
        else:
            new_expiry_date = current_expiry_date
    else:
        new_expiry_date = datetime.now() # Start from now if no previous expiry

    if unit == 'ngày':
        new_expiry_date += timedelta(days=value)
    elif unit == 'giờ':
        new_expiry_date += timedelta(hours=value)
    
    user_data[target_user_id_str]['expiry_date'] = new_expiry_date.strftime('%Y-%m-%d %H:%M:%S')
    save_user_data(user_data)
    
    bot.reply_to(message, 
                 f"Đã gia hạn thành công cho user ID `{target_user_id_str}` thêm **{value} {unit}**.\n"
                 f"Ngày hết hạn mới: `{user_data[target_user_id_str]['expiry_date']}`",
                 parse_mode='Markdown')
    
    try:
        bot.send_message(int(target_user_id_str), 
                         f"🎉 Tài khoản của bạn đã được gia hạn thêm **{value} {unit}** bởi Admin/CTV!\n"
                         f"Ngày hết hạn mới của bạn là: `{user_data[target_user_id_str]['expiry_date']}`",
                         parse_mode='Markdown')
    except telebot.apihelper.ApiTelegramException as e:
        if "bot was blocked by the user" in str(e):
            # print(f"Không thể thông báo gia hạn cho user {target_user_id_str}: Người dùng đã chặn bot.")
            pass
        else:
            print(f"Không thể thông báo gia hạn cho user {target_user_id_str}: {e}")

# --- Lệnh Admin Chính ---
@bot.message_handler(commands=['ctv'])
def add_ctv(message):
    if not is_admin(message.chat.id):
        bot.reply_to(message, "Bạn không có quyền sử dụng lệnh này.")
        return
    
    args = telebot.util.extract_arguments(message.text).split()
    if not args or not args[0].isdigit():
        bot.reply_to(message, "Cú pháp sai. Ví dụ: `/ctv <id_nguoi_dung>`", parse_mode='Markdown')
        return
    
    target_user_id_str = args[0]
    if target_user_id_str not in user_data:
        user_data[target_user_id_str] = {
            'username': "UnknownUser",
            'expiry_date': None,
            'is_ctv': True
        }
    else:
        user_data[target_user_id_str]['is_ctv'] = True
    
    save_user_data(user_data)
    bot.reply_to(message, f"Đã cấp quyền CTV cho user ID `{target_user_id_str}`.")
    try:
        bot.send_message(int(target_user_id_str), "🎉 Bạn đã được cấp quyền CTV!")
    except Exception:
        pass

@bot.message_handler(commands=['xoactv'])
def remove_ctv(message):
    if not is_admin(message.chat.id):
        bot.reply_to(message, "Bạn không có quyền sử dụng lệnh này.")
        return
    
    args = telebot.util.extract_arguments(message.text).split()
    if not args or not args[0].isdigit():
        bot.reply_to(message, "Cú pháp sai. Ví dụ: `/xoactv <id_nguoi_dung>`", parse_mode='Markdown')
        return
    
    target_user_id_str = args[0]
    if target_user_id_str in user_data:
        user_data[target_user_id_str]['is_ctv'] = False
        save_user_data(user_data)
        bot.reply_to(message, f"Đã xóa quyền CTV của user ID `{target_user_id_str}`.")
        try:
            bot.send_message(int(target_user_id_str), "❌ Quyền CTV của bạn đã bị gỡ bỏ.")
        except Exception:
            pass
    else:
        bot.reply_to(message, f"Không tìm thấy người dùng có ID `{target_user_id_str}`.")

@bot.message_handler(commands=['tb'])
def send_broadcast(message):
    if not is_admin(message.chat.id):
        bot.reply_to(message, "Bạn không có quyền sử dụng lệnh này.")
        return
    
    broadcast_text = telebot.util.extract_arguments(message.text)
    if not broadcast_text:
        bot.reply_to(message, "Vui lòng nhập nội dung thông báo. Ví dụ: `/tb Bot sẽ bảo trì vào 2h sáng mai.`", parse_mode='Markdown')
        return
    
    success_count = 0
    fail_count = 0
    for user_id_str in list(user_data.keys()):
        try:
            bot.send_message(int(user_id_str), f"📢 **THÔNG BÁO TỪ ADMIN** 📢\n\n{broadcast_text}", parse_mode='Markdown')
            success_count += 1
            time.sleep(0.1) # Tránh bị rate limit
        except telebot.apihelper.ApiTelegramException as e:
            # print(f"Không thể gửi thông báo cho user {user_id_str}: {e}")
            fail_count += 1
            if "bot was blocked by the user" in str(e) or "user is deactivated" in str(e):
                # print(f"Người dùng {user_id_str} đã chặn bot hoặc bị vô hiệu hóa. Có thể xóa khỏi user_data.")
                pass
        except Exception as e:
            print(f"Lỗi không xác định khi gửi thông báo cho user {user_id_str}: {e}")
            fail_count += 1
                
    bot.reply_to(message, f"Đã gửi thông báo đến {success_count} người dùng. Thất bại: {fail_count}.")
    save_user_data(user_data) # Lưu lại nếu có user bị xóa

@bot.message_handler(commands=['tatbot'])
def disable_bot_command(message):
    global bot_enabled, bot_disable_reason, bot_disable_admin_id
    if not is_admin(message.chat.id):
        bot.reply_to(message, "Bạn không có quyền sử dụng lệnh này.")
        return

    reason = telebot.util.extract_arguments(message.text)
    if not reason:
        bot.reply_to(message, "Vui lòng nhập lý do tắt bot. Ví dụ: `/tatbot Bot đang bảo trì.`", parse_mode='Markdown')
        return

    bot_enabled = False
    bot_disable_reason = reason
    bot_disable_admin_id = message.chat.id
    bot.reply_to(message, f"✅ Bot dự đoán đã được tắt bởi Admin `{message.from_user.username or message.from_user.first_name}`.\nLý do: `{reason}`", parse_mode='Markdown')
    
@bot.message_handler(commands=['mokbot'])
def enable_bot_command(message):
    global bot_enabled, bot_disable_reason, bot_disable_admin_id
    if not is_admin(message.chat.id):
        bot.reply_to(message, "Bạn không có quyền sử dụng lệnh này.")
        return

    if bot_enabled:
        bot.reply_to(message, "Bot dự đoán đã và đang hoạt động rồi.")
        return

    bot_enabled = True
    bot_disable_reason = "Không có"
    bot_disable_admin_id = None
    bot.reply_to(message, "✅ Bot dự đoán đã được mở lại bởi Admin.")
    
@bot.message_handler(commands=['taocode'])
def generate_code_command(message):
    if not is_admin(message.chat.id):
        bot.reply_to(message, "Bạn không có quyền sử dụng lệnh này.")
        return
    
    args = telebot.util.extract_arguments(message.text).split()
    if len(args) < 2 or len(args) > 3: # Giá trị, đơn vị, số lượng (tùy chọn)
        bot.reply_to(message, "Cú pháp sai. Ví dụ:\n"
                              "`/taocode <giá_trị> <ngày/giờ> <số_lượng>`\n"
                              "Ví dụ: `/taocode 1 ngày 5` (tạo 5 code 1 ngày)\n"
                              "Hoặc: `/taocode 24 giờ` (tạo 1 code 24 giờ)", parse_mode='Markdown')
        return
    
    try:
        value = int(args[0])
        unit = args[1].lower()
        quantity = int(args[2]) if len(args) == 3 else 1 # Mặc định tạo 1 code nếu không có số lượng
        
        if unit not in ['ngày', 'giờ']:
            bot.reply_to(message, "Đơn vị không hợp lệ. Chỉ chấp nhận `ngày` hoặc `giờ`.", parse_mode='Markdown')
            return
        if value <= 0 or quantity <= 0:
            bot.reply_to(message, "Giá trị hoặc số lượng phải lớn hơn 0.", parse_mode='Markdown')
            return

        generated_codes_list = []
        for _ in range(quantity):
            new_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8)) # 8 ký tự ngẫu nhiên
            GENERATED_CODES[new_code] = {
                "value": value,
                "type": unit,
                "used_by": None,
                "used_time": None
            }
            generated_codes_list.append(new_code)
        
        save_codes()
        
        response_text = f"✅ Đã tạo thành công {quantity} mã code gia hạn **{value} {unit}**:\n\n"
        response_text += "\n".join([f"`{code}`" for code in generated_codes_list])
        response_text += "\n\n_(Các mã này chưa được sử dụng)_"
        
        bot.reply_to(message, response_text, parse_mode='Markdown')

    except ValueError:
        bot.reply_to(message, "Giá trị hoặc số lượng không hợp lệ. Vui lòng nhập số nguyên.", parse_mode='Markdown')
    except Exception as e:
        bot.reply_to(message, f"Đã xảy ra lỗi khi tạo code: {e}", parse_mode='Markdown')


# --- Flask Routes cho Keep-Alive ---
@app.route('/')
def home():
    return "Bot is alive and running!"

@app.route('/health')
def health_check():
    return "OK", 200

# --- Khởi tạo bot và các luồng khi Flask app khởi động ---
@app.before_request
def start_bot_threads():
    global bot_initialized
    with bot_init_lock:
        if not bot_initialized:
            print("Initializing bot and prediction threads...")
            # Load initial data
            load_user_data()
            load_cau_patterns()
            load_codes()

            # Start prediction loop in a separate thread
            prediction_thread = Thread(target=prediction_loop, args=(prediction_stop_event,))
            prediction_thread.daemon = True
            prediction_thread.start()
            print("Prediction loop thread started.")

            # Start bot polling in a separate thread
            # Use bot.infinity_polling() for robust polling
            polling_thread = Thread(target=bot.infinity_polling, kwargs={'none_stop': True})
            polling_thread.daemon = True
            polling_thread.start()
            print("Telegram bot polling thread started.")
            
            bot_initialized = True

# --- Điểm khởi chạy chính cho Gunicorn/Render ---
if __name__ == '__main__':
    # When running locally, ensure threads are started
    # For Render, gunicorn will call the Flask app, and @app.before_request will handle initialization
    # No need to call app.run() directly if Gunicorn is used as main entry point
    port = int(os.environ.get('PORT', 5000))
    print(f"Starting Flask app locally on port {port}")
    app.run(host='0.0.0.0', port=port, debug=True)

