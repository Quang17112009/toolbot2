import telebot
import requests
import time
import json
import os
import random
import string
from datetime import datetime, timedelta
from threading import Thread, Event, Lock

from flask import Flask, request

# --- Cấu hình Bot (ĐẶT TRỰC TI TIẾP TẠY ĐÂY) ---
# THAY THẾ 'YOUR_BOT_TOKEN_HERE' BẰNG TOKEN THẬT CỦA BẠN
BOT_TOKEN = "7630248769:AAG36CSLxWWovAfa-Byjh_DohcpN3pA94Iw" # <-- ĐIỀN TOKEN CỦA BẠN VÀO ĐÂY
# THAY THẾ BẰNG ID ADMIN THẬT CỦA BẠN. Có thể có nhiều ID, cách nhau bởi dấu phẩy.
ADMIN_IDS = [6915752059] # <-- ĐIỀN ID ADMIN CỦA BẠN VÀO ĐÂY

DATA_FILE = 'user_data.json'
CAU_PATTERNS_FILE = 'cau_patterns.json'
CODES_FILE = 'codes.json'

# --- Cấu hình nâng cao ---
TX_HISTORY_LENGTH_LEARN = 200 # Chiều dài lịch sử phiên để học hỏi tổng thể
TX_HISTORY_LENGTH_ANALYZE = 50 # Chiều dài lịch sử phiên để phân tích chuyên sâu và đưa ra dự đoán
MIN_HISTORY_FOR_SMART_PREDICT = 10 # Số phiên tối thiểu để kích hoạt logic smart_predict (nên là ~10-20)

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
CAU_PATTERNS = {} # {pattern_string: confidence_score (float)}
GENERATED_CODES = {} # {code: {"value": 1, "type": "day", "used_by": null, "used_time": null}}
tx_full_history = [] # Sẽ lưu chi tiết 200 phiên gần nhất
prediction_stats = {'correct': 0, 'wrong': 0} # Thống kê dự đoán

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

# --- Logic dự đoán Tài Xỉu cơ bản (dựa trên một viên xí ngầu) ---
def du_doan_theo_xi_ngau(dice_list):
    if not dice_list:
        return "Đợi thêm dữ liệu"
    
    d1, d2, d3 = dice_list[-1]
    total = d1 + d2 + d3

    results = []
    for d in [d1, d2, d3]:
        tmp = d + total
        while tmp > 6:
            tmp -= 6
        if tmp % 2 == 0:
            results.append("Tài")
        else:
            results.append("Xỉu")

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
    initial_confidence = 1.0
    increase_factor = 0.2
    decrease_factor = 0.5

    current_confidence = CAU_PATTERNS.get(pattern_str, initial_confidence)

    if prediction_correct:
        new_confidence = min(current_confidence + increase_factor, 5.0)
    else:
        new_confidence = max(current_confidence - decrease_factor, 0.1)
    
    CAU_PATTERNS[pattern_str] = new_confidence
    save_cau_patterns()

def get_pattern_prediction_adjustment(pattern_str):
    confidence = CAU_PATTERNS.get(pattern_str, 1.0)
    
    if confidence >= 2.5:
        return "giữ nguyên"
    elif confidence <= 0.5:
        return "đảo chiều"
    else:
        return "không rõ"

# --- Các hàm hỗ trợ phân tích lịch sử ---
def get_current_streak_info(simplified_history_str):
    if not simplified_history_str:
        return 0, None
    
    current_result = simplified_history_str[0]
    streak_length = 0
    for res in simplified_history_str:
        if res == current_result:
            streak_length += 1
        else:
            break
    return streak_length, current_result

def calculate_average_streak_length(simplified_history_str):
    if not simplified_history_str:
        return 0
    
    streaks = []
    if not simplified_history_str:
        return 0
    
    current_streak_len = 0
    current_char = ''
    
    for char in simplified_history_str:
        if char == current_char:
            current_streak_len += 1
        else:
            if current_streak_len > 0:
                streaks.append(current_streak_len)
            current_char = char
            current_streak_len = 1
    if current_streak_len > 0:
        streaks.append(current_streak_len)

    return sum(streaks) / len(streaks) if streaks else 0

def analyze_dice_frequencies(history_data):
    dice_counts = {i: 0 for i in range(1, 7)}
    total_sum_counts = {s: 0 for s in range(3, 19)}
    
    for session in history_data:
        for die in session['dice']:
            dice_counts[die] += 1
        total_sum_counts[session['total']] += 1
    
    return dice_counts, total_sum_counts

# --- Logic Dự Đoán Thông Minh (Hàm chính) ---
def smart_predict(full_history, analyze_history, current_dice):
    
    du_doan_co_so = du_doan_theo_xi_ngau([current_dice])
    
    if len(analyze_history) < MIN_HISTORY_FOR_SMART_PREDICT:
        return du_doan_co_so, "AI Dự đoán theo xí ngầu (chưa đủ lịch sử để phân tích sâu)"

    analyze_history_simplified_str = ''.join(["T" if p['result'] == "Tài" else "X" for p in analyze_history])
    full_history_simplified_str = ''.join(["T" if p['result'] == "Tài" else "X" for p in full_history])

    score_tai = 0.0
    score_xiu = 0.0
    reasons = []

    if du_doan_co_so == "Tài":
        score_tai += 1.0
        reasons.append("Xí ngầu cơ sở dự đoán Tài")
    else:
        score_xiu += 1.0
        reasons.append("Xí ngầu cơ sở dự đoán Xỉu")

    if len(analyze_history_simplified_str) >= 7:
        current_cau_pattern = analyze_history_simplified_str[:7]
        pattern_adjustment = get_pattern_prediction_adjustment(current_cau_pattern)
        
        if pattern_adjustment == "giữ nguyên":
            if du_doan_co_so == "Tài": score_tai += 1.5
            else: score_xiu += 1.5
            reasons.append(f"Cầu đẹp ({current_cau_pattern}) củng cố")
        elif pattern_adjustment == "đảo chiều":
            if du_doan_co_so == "Tài": score_xiu += 2.0
            else: score_tai += 2.0
            reasons.append(f"Cầu xấu ({current_cau_pattern}) → Đảo chiều")
        else:
            reasons.append(f"Mẫu cầu ({current_cau_pattern}) không rõ")

    current_streak_len, current_streak_result = get_current_streak_info(analyze_history_simplified_str)
    avg_bette_length = calculate_average_streak_length(full_history_simplified_str)

    if current_streak_len > 0:
        if avg_bette_length > 0 and current_streak_len >= avg_bette_length * 1.5:
            if du_doan_co_so != current_streak_result:
                if du_doan_co_so == "Tài": score_tai += 3.0
                else: score_xiu += 3.0
                reasons.append(f"TÍN HIỆU BẺ CẦU MẠNH! (Bệt {current_streak_len}, dự đoán ngược)")
            else:
                if du_doan_co_so == "Tài": score_tai += 1.0
                else: score_xiu += 1.0
                reasons.append(f"Cầu bệt dài ({current_streak_len}) tiếp diễn")
        else:
            if du_doan_co_so == "Tài": score_tai += 0.5
            else: score_xiu += 0.5
            reasons.append(f"Cầu bệt {current_streak_len} đang chạy (chưa đến ngưỡng bẻ)")

    dice_freq, total_sum_freq = analyze_dice_frequencies(analyze_history)
    total_sessions_analyzed = len(analyze_history)

    if total_sessions_analyzed > 0:
        low_dice_count = dice_freq[1] + dice_freq[2]
        high_dice_count = dice_freq[5] + dice_freq[6]
        
        expected_low_dice = (total_sessions_analyzed * 3 * 2) / 6
        expected_high_dice = (total_sessions_analyzed * 3 * 2) / 6

        if low_dice_count < expected_low_dice * 0.7:
            score_tai += 0.7
            reasons.append("Xí ngầu nhỏ ít xuất hiện (hướng Tài)")
        if high_dice_count < expected_high_dice * 0.7:
            score_xiu += 0.7
            reasons.append("Xí ngầu lớn ít xuất hiện (hướng Xỉu)")

        small_sums_count = sum(total_sum_freq.get(s, 0) for s in range(3, 11))
        large_sums_count = sum(total_sum_freq.get(s, 0) for s in range(11, 19))

        if small_sums_count > large_sums_count * 1.2:
            score_xiu += 1.0
            reasons.append("Tổng điểm nhỏ chiếm ưu thế")
        elif large_sums_count > small_sums_count * 1.2:
            score_tai += 1.0
            reasons.append("Tổng điểm lớn chiếm ưu thế")
            
    final_prediction = ""
    
    if score_tai > score_xiu:
        final_prediction = "Tài"
    elif score_xiu > score_tai:
        final_prediction = "Xỉu"
    else:
        final_prediction = du_doan_co_so
        reasons.append("Điểm số cân bằng, theo xí ngầu cơ sở")

    final_reason_str = "AI Tổng hợp: " + ", ".join(reasons)
    
    return final_prediction, final_reason_str

# --- Lấy dữ liệu từ API ---
def lay_du_lieu():
    try:
        response = requests.get("https://1.bot/GetNewLottery/LT_Taixiu")
        response.raise_for_status()
        data = response.json()
        if data.get("state") != 1:
            return None
        return data.get("data")
    except requests.exceptions.RequestException as e:
        print(f"Lỗi khi lấy dữ liệu từ API: {e}")
        return None
    except json.JSONDecodeError:
        print("Lỗi giải mã JSON từ API. Phản hồi không phải JSON hợp lệ.")
        return None

# --- Logic chính của Bot dự đoán (chạy trong luồng riêng) ---
def prediction_loop(stop_event: Event):
    global prediction_stats
    last_id = None
    
    print("Prediction loop started.")
    while not stop_event.is_set():
        if not bot_enabled:
            time.sleep(10)
            continue

        data = lay_du_lieu()
        if not data:
            time.sleep(5)
            continue

        issue_id = data.get("ID")
        expect = data.get("Expect")
        open_code_str = data.get("OpenCode")

        if not all([issue_id, expect, open_code_str]):
            time.sleep(5)
            continue

        if issue_id != last_id:
            try:
                dice = tuple(map(int, open_code_str.split(",")))
            except ValueError:
                print(f"Lỗi phân tích OpenCode: '{open_code_str}'. Bỏ qua phiên này.")
                last_id = issue_id
                time.sleep(5)
                continue
            
            ket_qua_tx, tong = tinh_tai_xiu(dice)

            tx_full_history.insert(0, {
                'id': issue_id,
                'expect': expect,
                'dice': dice,
                'total': tong,
                'result': ket_qua_tx
            })
            if len(tx_full_history) > TX_HISTORY_LENGTH_LEARN:
                tx_full_history.pop()

            analyze_history_for_predict = tx_full_history[:TX_HISTORY_LENGTH_ANALYZE]

            next_expect = str(int(expect) + 1).zfill(len(expect))
            du_doan_cuoi_cung, ly_do = smart_predict(tx_full_history, analyze_history_for_predict, dice)

            if len(analyze_history_for_predict) >= 7:
                current_cau_str_for_pattern = ''.join(["T" if p['result'] == "Tài" else "X" for p in analyze_history_for_predict[:7]])
                prediction_correct = (du_doan_cuoi_cung == ket_qua_tx)
                update_cau_patterns(current_cau_str_for_pattern, prediction_correct)
                
                if prediction_correct:
                    prediction_stats['correct'] += 1
                else:
                    prediction_stats['wrong'] += 1

            for user_id_str, user_info in list(user_data.items()):
                user_id = int(user_id_str)
                is_sub, sub_message = check_subscription(user_id)
                if is_sub:
                    try:
                        prediction_message = (
                            "🎮 **KẾT QUẢ PHIÊN HIỆN TẠI** 🎮\n"
                            f"Phiên: `{expect}` | Kết quả: **{ket_qua_tx}** (Tổng: **{tong}**)\n\n"
                            f"**Dự đoán cho phiên tiếp theo:**\n"
                            f"🔢 Phiên: `{next_expect}`\n"
                            f"🤖 Dự đoán: **{du_doan_cuoi_cung}**\n"
                            f"📌 Lý do: _{ly_do}_\n"
                            f"⚠️ **Hãy đặt cược sớm trước khi phiên kết thúc!**"
                        )
                        bot.send_message(user_id, prediction_message, parse_mode='Markdown')
                    except telebot.apihelper.ApiTelegramException as e:
                        if "bot was blocked by the user" in str(e) or "user is deactivated" in str(e):
                            pass
                        else:
                            print(f"Lỗi gửi tin nhắn cho user {user_id}: {e}")
                    except Exception as e:
                        print(f"Lỗi không xác định khi gửi tin nhắn cho user {user_id}: {e}")

            print("-" * 50)
            print("🎮 Kết quả phiên hiện tại: {} (Tổng: {})".format(ket_qua_tx, tong))
            print("🔢 Phiên: {} → {}".format(expect, next_expect))
            print("🤖 Dự đoán: {}".format(du_doan_cuoi_cung))
            print("📌 Lý do: {}".format(ly_do))
            simple_history_display = ''.join(["T" if p['result'] == "Tài" else "X" for p in tx_full_history[:10]])
            print("Lịch sử TX (10 gần nhất): {}. ".format(simple_history_display))
            print("-" * 50)

            last_id = issue_id

        time.sleep(5)
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
                     "Chào mừng bạn đến với **BOT DỰ ĐOÁN TÀI XỈU LUCKYWIN**!\n"
                     "Hãy dùng lệnh /help để xem danh sách các lệnh hỗ trợ.", 
                     parse_mode='Markdown')
    else:
        user_data[user_id]['username'] = username
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
        "🔸 `/nap`: Hướng dẫn nạp tiền.\n"
        "🔸 `/dudoan`: Bắt đầu nhận dự đoán từ bot.\n"
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
            "👑 `/thongke`: Xem thống kê dự đoán của bot (đúng/sai).\n"
        )
    
    bot.reply_to(message, help_text, parse_mode='Markdown')

@bot.message_handler(commands=['support'])
def show_support(message):
    bot.reply_to(message, 
        "Để được hỗ trợ, vui lòng liên hệ Admin:\n"
        "@nhutquangdz"
    )

@bot.message_handler(commands=['gia'])
def show_price(message):
    price_text = (
        "📊 **BOT LUCKYWIN XIN THÔNG BÁO BẢNG GIÁ LUCKYWIN BOT** 📊\n\n"
        "💸 **20k**: 1 Ngày\n"
        "💸 **50k**: 1 Tuần\n"
        "💸 **80k**: 2 Tuần\n"
        "💸 **130k**: 1 Tháng\n\n"
        "🤖 BOT LUCKYWIn TỈ Lệ **85-92%**\n"
        "⏱️ ĐỌC 24/24\n\n"
        "Vui Lòng ib @heheviptool hoặc @Besttaixiu999 Để Gia Hạn"
    )
    bot.reply_to(message, price_text, parse_mode='Markdown')

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
        bot.reply_to(message, sub_message + "\nVui lòng liên hệ Admin @nhutquangdz để được hỗ trợ.", parse_mode='Markdown')
        return
    
    if not bot_enabled:
        bot.reply_to(message, f"❌ Bot dự đoán hiện đang tạm dừng bởi Admin. Lý do: `{bot_disable_reason}`", parse_mode='Markdown')
        return

    bot.reply_to(message, "✅ Bạn đang có quyền truy cập. Bot sẽ tự động gửi dự đoán các phiên mới nhất tại đây.")

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

    current_expiry_str = user_data.get(user_id, {}).get('expiry_date')
    if current_expiry_str:
        current_expiry_date = datetime.strptime(current_expiry_str, '%Y-%m-%d %H:%M:%S')
        if datetime.now() > current_expiry_date:
            new_expiry_date = datetime.now()
        else:
            new_expiry_date = current_expiry_date
    else:
        new_expiry_date = datetime.now()

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
    unit = args[2].lower()
    
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
        new_expiry_date = datetime.now()

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
            time.sleep(0.1)
        except telebot.apihelper.ApiTelegramException as e:
            fail_count += 1
            if "bot was blocked by the user" in str(e) or "user is deactivated" in str(e):
                pass
        except Exception as e:
            print(f"Lỗi không xác định khi gửi thông báo cho user {user_id_str}: {e}")
            fail_count += 1
                
    bot.reply_to(message, f"Đã gửi thông báo đến {success_count} người dùng. Thất bại: {fail_count}.")
    save_user_data(user_data)

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
    if len(args) < 2 or len(args) > 3:
        bot.reply_to(message, "Cú pháp sai. Ví dụ:\n"
                              "`/taocode <giá_trị> <ngày/giờ> <số_lượng>`\n"
                              "Ví dụ: `/taocode 1 ngày 5` (tạo 5 code 1 ngày)\n"
                              "Hoặc: `/taocode 24 giờ` (tạo 1 code 24 giờ)", parse_mode='Markdown')
        return
    
    try:
        value = int(args[0])
        unit = args[1].lower()
        quantity = int(args[2]) if len(args) == 3 else 1
        
        if unit not in ['ngày', 'giờ']:
            bot.reply_to(message, "Đơn vị không hợp lệ. Chỉ chấp nhận `ngày` hoặc `giờ`.", parse_mode='Markdown')
            return
        if value <= 0 or quantity <= 0:
            bot.reply_to(message, "Giá trị hoặc số lượng phải lớn hơn 0.", parse_mode='Markdown')
            return

        generated_codes_list = []
        for _ in range(quantity):
            new_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
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

@bot.message_handler(commands=['thongke'])
def show_statistics(message):
    if not is_admin(message.chat.id):
        bot.reply_to(message, "Bạn không có quyền sử dụng lệnh này.")
        return
    
    total_predictions = prediction_stats['correct'] + prediction_stats['wrong']
    
    if total_predictions == 0:
        stats_text = "📊 **THỐNG KÊ DỰ ĐOÁN BOT** 📊\n\nChưa có đủ dữ liệu dự đoán."
    else:
        accuracy = (prediction_stats['correct'] / total_predictions) * 100
        stats_text = (
            "📊 **THỐNG KÊ DỰ ĐOÁN BOT** 📊\n\n"
            f"Tổng số dự đoán: **{total_predictions}**\n"
            f"Số lần đúng: **{prediction_stats['correct']}**\n"
            f"Số lần sai: **{prediction_stats['wrong']}**\n"
            f"Tỷ lệ chính xác: **{accuracy:.2f}%**"
        )
    bot.reply_to(message, stats_text, parse_mode='Markdown')


# --- Flask Routes cho Keep-Alive ---
@app.route('/')
def home():
    return "Bot is alive and running!"

@app.route('/health')
def health_check():
    return "OK", 200

# --- Khởi tạo bot và các luồng khi Flask app khởi động ---
# @app.before_request không còn dùng để khởi tạo luồng chính, thay vào đó dùng if __name__ == "__main__"

def run_flask_app():
    print("Starting Flask app locally on port 5000...")
    # use_reloader=False là quan trọng để tránh lỗi khởi động hai lần
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)

def run_bot_polling():
    print("Starting Telegram bot polling thread...")
    try:
        # Sử dụng infinity_polling để bot tự động kết nối lại khi có lỗi mạng
        bot.infinity_polling(none_stop=True)
    except Exception as e:
        print(f"Lỗi trong quá trình polling của bot Telegram: {e}")
        time.sleep(10) # Đợi một chút rồi thử lại
        run_bot_polling() # Thử chạy lại polling

# --- Điểm khởi chạy chính ---
if __name__ == '__main__':
    print("Initializing bot components and starting threads...")
    
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
    bot_polling_thread = Thread(target=run_bot_polling, daemon=True)
    bot_polling_thread.start()
    print("Telegram bot polling thread started.")

    # Start Flask app in a separate thread
    flask_app_thread = Thread(target=run_flask_app, daemon=True)
    flask_app_thread.start()
    print("Flask app thread started.")

    print("\nBot và Flask app đã khởi động thành công.")
    print("Bạn có thể tương tác với bot trên Telegram.")
    print("Giữ cửa sổ Command Prompt này mở để bot tiếp tục hoạt động.")
    print("Nhấn Ctrl+C để thoát.")

    # Giữ luồng chính không thoát để các luồng con (daemon=True) tiếp tục chạy
    while True:
        try:
            time.sleep(1)
        except KeyboardInterrupt:
            print("\nĐang tắt bot...")
            prediction_stop_event.set() # Báo hiệu luồng dự đoán dừng
            # Để bot.infinity_polling() dừng, đôi khi cần Ctrl+C lần nữa hoặc đợi timeout của thư viện
            break

