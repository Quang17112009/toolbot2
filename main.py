import telebot
import requests
import json
import time
import threading
import sys
import os
import datetime

# --- Cấu hình Bot ---
# THAY THẾ 'YOUR_BOT_TOKEN' BẰNG TOKEN BOT TELEGRAM THẬT CỦA BẠN
TOKEN = '7630248769:AAG36CSLxWWovAfa-Byjh_DohcpN3pA94Iw'
# THAY THẾ -100123456789 BẰNG CHAT_ID CỦA NHÓM/KÊNH MÀ BOT SẼ GỬI TIN NHẮN
# Chat ID của kênh/nhóm thường bắt đầu bằng -100. Bạn có thể thêm @userinfobot vào nhóm và gõ /info để lấy ID.
CHAT_ID = -4954584885 

bot = telebot.TeleBot(TOKEN)

# --- Cấu hình Admin ---
# THAY THẾ CÁC SỐ TRONG DANH SÁCH NÀY BẰNG TELEGRAM USER ID CỦA ADMIN THẬT CỦA BẠN!
# Bạn có thể tìm ID của mình bằng cách chat với @userinfobot trên Telegram và gõ /start.
ADMIN_IDS = [6915752059, 6915752059]

# --- Biến toàn cục để quản lý trạng thái bot ---
bot_enabled = True
bot_disable_reason = "Bot đang hoạt động bình thường."
prediction_thread = None
stop_event = threading.Event()
bot_init_lock = threading.Lock()
bot_initialized = False

# --- Biến toàn cục lưu trữ dữ liệu người dùng và mã giới thiệu ---
USER_DATA_FILE = 'user_data.json'
CODES_FILE = 'codes.json'

user_data = {}  # {user_id: {subscribed: True/False, ref_by: None/referrer_id, sub_end_date: None/timestamp}}
codes = {}      # {code: {used_by: None/user_id, expires: None/timestamp, type: 'trial'/'premium'}}

# --- Biến toàn cục cho thuật toán dự đoán ---
HISTORY_FILE = 'prediction_history.json'
PERFORMANCE_FILE = 'prediction_performance.json'
WEIGHTS_FILE = 'strategy_weights.json'
API_URL = "https://1.bot/GetNewLottery/LT_Taixiu" # API để lấy dữ liệu mới

pattern_history = []  # Lưu dãy T/X gần nhất (lên đến 200 phiên)
dice_history = []     # Lưu lịch sử các mặt xúc xắc chi tiết
last_raw_predictions = [] # Lưu trữ các dự đoán thô của phiên trước để cập nhật trọng số chính xác hơn

prediction_performance = {} # { strategyGroup: { correct: 0, total: 0 } }

# Các trọng số này sẽ tự động điều chỉnh theo thời gian dựa trên hiệu suất
# Cố định tên nhóm chiến lược để trọng số được học hỏi và áp dụng nhất quán
strategy_weights = {
    # Trọng số ban đầu cho các loại mẫu cầu chung
    "Cầu Bệt": 1.0,
    "Cầu 1-1": 1.0,
    "Cầu Lặp 2-1": 1.0,
    "Cầu Lặp 2-2": 1.0,
    "Cầu Lặp 3-1": 1.0,
    "Cầu Lặp 3-2": 1.0,
    "Cầu Lặp 3-3": 1.0,
    "Cầu Lặp 4-1": 1.0,
    "Cầu Lặp 4-2": 1.0,
    "Cầu Lặp 4-3": 1.0,
    "Cầu Lặp 4-4": 1.0,
    "Cầu Đối Xứng": 1.2,
    "Cầu Đảo Ngược": 1.1,
    "Cầu Ziczac Ngắn": 0.8,
    "Cầu Lặp Chuỗi Khác": 1.0,
    # Trọng số cho các chiến lược đặc biệt không thuộc nhóm mẫu
    "Xu hướng Tài mạnh (Ngắn)": 1.0,
    "Xu hướng Xỉu mạnh (Ngắn)": 1.0,
    "Xu hướng Tài rất mạnh (Dài)": 1.2,
    "Xu hướng Xỉu rất mạnh (Dài)": 1.2,
    "Xu hướng tổng điểm": 0.9,
    "Bộ ba": 1.3,
    "Điểm 10": 0.8,
    "Điểm 11": 0.8,
    "Bẻ cầu bệt dài": 1.6,
    "Bẻ cầu 1-1 dài": 1.6,
    "Reset Cầu/Bẻ Sâu": 1.9
}

# --- Hàm tạo mẫu tự động để đạt 1000+ mẫu ---
def generate_common_patterns():
    patterns = []

    # 1. Cầu Bệt (Streaks): TTT... và XXX... (từ 3 đến 20 lần)
    for i in range(3, 21):
        patterns.append({
            "name": f"Cầu Bệt Tài ({i})",
            "pattern": "T" * i,
            "predict": "T",
            "conf": 0.05 + (i * 0.005),
            "minHistory": i,
            "strategyGroup": "Cầu Bệt"
        })
        patterns.append({
            "name": f"Cầu Bệt Xỉu ({i})",
            "pattern": "X" * i,
            "predict": "X",
            "conf": 0.05 + (i * 0.005),
            "minHistory": i,
            "strategyGroup": "Cầu Bệt"
        })

    # 2. Cầu 1-1 (Alternating): TXT... và XTX... (từ 3 đến 20 phiên)
    for i in range(3, 21):
        pattern_tx = "".join(["T" if j % 2 == 0 else "X" for j in range(i)])
        pattern_xt = "".join(["X" if j % 2 == 0 else "T" for j in range(i)])
        patterns.append({
            "name": f"Cầu 1-1 (TX - {i})",
            "pattern": pattern_tx,
            "predict": "T" if i % 2 == 0 else "X",
            "conf": 0.05 + (i * 0.005),
            "minHistory": i,
            "strategyGroup": "Cầu 1-1"
        })
        patterns.append({
            "name": f"Cầu 1-1 (XT - {i})",
            "pattern": pattern_xt,
            "predict": "X" if i % 2 == 0 else "T",
            "conf": 0.05 + (i * 0.005),
            "minHistory": i,
            "strategyGroup": "Cầu 1-1"
        })

    # 3. Cầu Lặp lại cơ bản
    base_repeated_patterns = [
        {"base": "TTX", "group": "Cầu Lặp 2-1"}, {"base": "XXT", "group": "Cầu Lặp 2-1"},
        {"base": "TTXX", "group": "Cầu Lặp 2-2"}, {"base": "XXTT", "group": "Cầu Lặp 2-2"},
        {"base": "TTTX", "group": "Cầu Lặp 3-1"}, {"base": "XXXT", "group": "Cầu Lặp 3-1"},
        {"base": "TTTXX", "group": "Cầu Lặp 3-2"}, {"base": "XXXTT", "group": "Cầu Lặp 3-2"},
        {"base": "TTTXXX", "group": "Cầu Lặp 3-3"}, {"base": "XXXTTT", "group": "Cầu Lặp 3-3"},
        {"base": "TTTTX", "group": "Cầu Lặp 4-1"}, {"base": "XXXXT", "group": "Cầu Lặp 4-1"},
        {"base": "TTTTXX", "group": "Cầu Lặp 4-2"}, {"base": "XXXXTT", "group": "Cầu Lặp 4-2"},
        {"base": "TTTTXXX", "group": "Cầu Lặp 4-3"}, {"base": "XXXXTTT", "group": "Cầu Lặp 4-3"},
        {"base": "TTTTXXXX", "group": "Cầu Lặp 4-4"}, {"base": "XXXXTTTT", "group": "Cầu Lặp 4-4"}
    ]

    for pattern_info in base_repeated_patterns:
        for num_repeats in range(1, 6):
            current_pattern = pattern_info["base"] * num_repeats
            predict_char = pattern_info["base"][0]
            patterns.append({
                "name": f"{pattern_info['group']} ({pattern_info['base']} x{num_repeats})",
                "pattern": current_pattern,
                "predict": predict_char,
                "conf": 0.08 + (num_repeats * 0.01),
                "minHistory": len(current_pattern),
                "strategyGroup": pattern_info["group"]
            })

    # 4. Cầu Đối Xứng (Symmetric) và Đảo Ngược (Inverse)
    symmetric_and_inverse_patterns = [
        {"base": "TX", "predict": "T", "group": "Cầu Đối Xứng"},
        {"base": "XT", "predict": "X", "group": "Cầu Đối Xứng"},
        {"base": "TXXT", "predict": "T", "group": "Cầu Đối Xứng"},
        {"base": "XTTX", "predict": "X", "group": "Cầu Đối Xứng"},
        {"base": "TTXT", "predict": "X", "group": "Cầu Đảo Ngược"},
        {"base": "XXTX", "predict": "T", "group": "Cầu Đảo Ngược"},
        {"base": "TXTXT", "predict": "X", "group": "Cầu Đối Xứng"},
        {"base": "XTXTX", "predict": "T", "group": "Cầu Đối Xứng"},
    ]

    for pattern_info in symmetric_and_inverse_patterns:
        for num_repeats in range(1, 4):
            current_pattern = pattern_info["base"] * num_repeats
            patterns.append({
                "name": f"{pattern_info['group']} ({pattern_info['base']} x{num_repeats})",
                "pattern": current_pattern,
                "predict": pattern_info["predict"],
                "conf": 0.1 + (num_repeats * 0.015),
                "minHistory": len(current_pattern),
                "strategyGroup": pattern_info["group"]
            })
        if len(pattern_info["base"]) == 2:
            pattern_abba = pattern_info["base"] + pattern_info["base"][::-1]
            patterns.append({
                "name": f"{pattern_info['group']} ({pattern_abba})",
                "pattern": pattern_abba,
                "predict": pattern_info["base"][0],
                "conf": 0.15,
                "minHistory": len(pattern_abba),
                "strategyGroup": pattern_info["group"]
            })
            pattern_abccba = pattern_info["base"] * 2 + (pattern_info["base"][::-1]) * 2
            if len(pattern_abccba) <= 10:
                patterns.append({
                    "name": f"{pattern_info['group']} ({pattern_abccba})",
                    "pattern": pattern_abccba,
                    "predict": pattern_info["base"][0],
                    "conf": 0.18,
                    "minHistory": len(pattern_abccba),
                    "strategyGroup": pattern_info["group"]
                })

    # 5. Cầu Ziczac Ngắn
    short_ziczac_patterns = [
        {"pattern": "TTX", "predict": "T"}, {"pattern": "XXT", "predict": "X"},
        {"pattern": "TXT", "predict": "X"}, {"pattern": "XTX", "predict": "T"},
        {"pattern": "TXX", "predict": "X"}, {"pattern": "XTT", "predict": "T"},
        {"pattern": "TTXX", "predict": "T"}, {"pattern": "XXTT", "predict": "X"},
        {"pattern": "TXTX", "predict": "T"}, {"pattern": "XTXT", "predict": "X"},
        {"pattern": "XTTX", "predict": "X"}, {"pattern": "TXXT", "predict": "T"}
    ]
    for p in short_ziczac_patterns:
        patterns.append({
            "name": f"Cầu Ziczac Ngắn ({p['pattern']})",
            "pattern": p['pattern'],
            "predict": p['predict'],
            "conf": 0.05,
            "minHistory": len(p['pattern']),
            "strategyGroup": "Cầu Ziczac Ngắn"
        })

    # Tăng cường số lượng bằng các mẫu lặp lại phức tạp hơn
    complex_repeats = ["TTX", "XXT", "TXT", "TXX", "XTT"]
    for base in complex_repeats:
        for i in range(2, 5):
            current_pattern = base * i
            if len(current_pattern) <= 15:
                patterns.append({
                    "name": f"Cầu Lặp Chuỗi Khác ({base} x{i})",
                    "pattern": current_pattern,
                    "predict": base[0],
                    "conf": 0.07 + (i * 0.01),
                    "minHistory": len(current_pattern),
                    "strategyGroup": "Cầu Lặp Chuỗi Khác"
                })

    return patterns

all_pattern_strategies = generate_common_patterns()

# Ensure all strategy groups have initial weights and performance records
for pattern in all_pattern_strategies:
    if pattern['strategyGroup'] not in strategy_weights:
        strategy_weights[pattern['strategyGroup']] = 1.0
    if pattern['strategyGroup'] not in prediction_performance:
        prediction_performance[pattern['strategyGroup']] = {'correct': 0, 'total': 0}

# --- Hàm tải/lưu dữ liệu ---
def load_user_data():
    global user_data
    if os.path.exists(USER_DATA_FILE):
        with open(USER_DATA_FILE, 'r') as f:
            try:
                user_data = json.load(f)
                print("DEBUG: Đã tải dữ liệu người dùng.")
            except json.JSONDecodeError:
                print("LỖI: Lỗi đọc user_data.json. Khởi tạo lại dữ liệu người dùng.")
                user_data = {}
    else:
        print("DEBUG: Không tìm thấy user_data.json. Khởi tạo dữ liệu người dùng mới.")

def save_user_data():
    with open(USER_DATA_FILE, 'w') as f:
        json.dump(user_data, f, indent=4)
    print("DEBUG: Đã lưu dữ liệu người dùng.")

def load_codes():
    global codes
    if os.path.exists(CODES_FILE):
        with open(CODES_FILE, 'r') as f:
            try:
                codes = json.load(f)
                print("DEBUG: Đã tải mã giới thiệu.")
            except json.JSONDecodeError:
                print("LỖI: Lỗi đọc codes.json. Khởi tạo lại mã giới thiệu.")
                codes = {}
    else:
        print("DEBUG: Không tìm thấy codes.json. Khởi tạo mã giới thiệu mới.")

def save_codes():
    with open(CODES_FILE, 'w') as f:
        json.dump(codes, f, indent=4)
    print("DEBUG: Đã lưu mã giới thiệu.")

def load_prediction_data():
    global pattern_history, dice_history, prediction_performance, strategy_weights

    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, 'r') as f:
            try:
                data = json.load(f)
                pattern_history = data.get('pattern_history', [])
                dice_history = data.get('dice_history', [])
                print(f"DEBUG: Tải lịch sử dự đoán từ {HISTORY_FILE}")
            except json.JSONDecodeError:
                print(f"LỖI: Lỗi đọc {HISTORY_FILE}. Khởi tạo lịch sử dự đoán.")
                pattern_history = []
                dice_history = []
            except Exception as e:
                print(f"LỖI: Lỗi không xác định khi tải {HISTORY_FILE}: {e}")
                pattern_history = []
                dice_history = []

    if os.path.exists(PERFORMANCE_FILE):
        with open(PERFORMANCE_FILE, 'r') as f:
            try:
                prediction_performance = json.load(f)
                print(f"DEBUG: Tải hiệu suất dự đoán từ {PERFORMANCE_FILE}")
            except json.JSONDecodeError:
                print(f"LỖI: Lỗi đọc {PERFORMANCE_FILE}. Khởi tạo hiệu suất dự đoán.")
                prediction_performance = {}
            except Exception as e:
                print(f"LỖI: Lỗi không xác định khi tải {PERFORMANCE_FILE}: {e}")
                prediction_performance = {}

    if os.path.exists(WEIGHTS_FILE):
        with open(WEIGHTS_FILE, 'r') as f:
            try:
                loaded_weights = json.load(f)
                for key, value in strategy_weights.items():
                    if key in loaded_weights:
                        strategy_weights[key] = loaded_weights[key]
                print(f"DEBUG: Tải trọng số chiến lược từ {WEIGHTS_FILE}")
            except json.JSONDecodeError:
                print(f"LỖI: Lỗi đọc {WEIGHTS_FILE}. Sử dụng trọng số mặc định.")
            except Exception as e:
                print(f"LỖI: Lỗi không xác định khi tải {WEIGHTS_FILE}: {e}")

    # Ensure all strategy groups have initial performance records
    for pattern in all_pattern_strategies:
        if pattern['strategyGroup'] not in prediction_performance:
            prediction_performance[pattern['strategyGroup']] = {'correct': 0, 'total': 0}

def save_prediction_data():
    try:
        with open(HISTORY_FILE, 'w') as f:
            json.dump({'pattern_history': pattern_history, 'dice_history': dice_history}, f, indent=4)
        print(f"DEBUG: Đã lưu lịch sử dự đoán vào {HISTORY_FILE}")
    except Exception as e:
        print(f"LỖI: Không thể lưu lịch sử dự đoán vào {HISTORY_FILE}: {e}")

    try:
        with open(PERFORMANCE_FILE, 'w') as f:
            json.dump(prediction_performance, f, indent=4)
        print(f"DEBUG: Đã lưu hiệu suất dự đoán vào {PERFORMANCE_FILE}")
    except Exception as e:
        print(f"LỖI: Không thể lưu hiệu suất dự đoán vào {PERFORMANCE_FILE}: {e}")

    try:
        with open(WEIGHTS_FILE, 'w') as f:
            json.dump(strategy_weights, f, indent=4)
        print(f"DEBUG: Đã lưu trọng số chiến lược vào {WEIGHTS_FILE}")
    except Exception as e:
        print(f"LỖI: Không thể lưu trọng số chiến lược vào {WEIGHTS_FILE}: {e}")

# --- Các hàm hỗ trợ cho Tài Xỉu ---
def tinh_tai_xiu(dice_rolls):
    tong = sum(dice_rolls)
    if tong >= 4 and tong <= 10:
        return "Xỉu", tong
    elif tong >= 11 and tong <= 17:
        return "Tài", tong
    else:
        return "Bộ Ba", tong # Trường hợp bộ ba 1,1,1 hoặc 6,6,6
    
# --- Hàm lấy dữ liệu từ API mới ---
def lay_du_lieu_moi():
    try:
        response = requests.get(API_URL, timeout=10)
        response.raise_for_status() # Báo lỗi cho các mã trạng thái HTTP xấu (4xx hoặc 5xx)
        data = response.json()
        
        # Kiểm tra cấu trúc dữ liệu trả về
        if data.get("state") == 1 and "data" in data:
            return data["data"]
        else:
            print(f"LỖI: Dữ liệu API không hợp lệ: {data}")
            return None
    except requests.exceptions.Timeout:
        print("LỖI: Hết thời gian chờ khi kết nối API.")
        return None
    except requests.exceptions.ConnectionError as e:
        print(f"LỖI: Lỗi kết nối API: {e}")
        return None
    except requests.exceptions.RequestException as e:
        print(f"LỖI: Lỗi yêu cầu API: {e}")
        return None
    except json.JSONDecodeError:
        print("LỖI: Không thể phân tích JSON từ phản hồi API.")
        return None
    except Exception as e:
        print(f"LỖI: Lỗi không xác định khi lấy dữ liệu API: {e}")
        return None

# === Thuật toán dự đoán nâng cao ===
def analyze_and_predict(history, dice_hist):
    analysis = {
        "totalResults": len(history),
        "taiCount": history.count('T'),
        "xiuCount": history.count('X'),
        "last50Pattern": "".join(history[-50:]),
        "last200Pattern": "".join(history),
        "predictionDetails": [],
        "rawPredictions": []
    }

    final_prediction = "?"
    combined_confidence = 0.0

    recent_history_full = "".join(history)
    recent50 = "".join(history[-50:])
    recent20 = "".join(history[-20:])
    recent10 = "".join(history[-10:])

    def add_prediction(strategy_name, predict, conf_multiplier, detail, strategy_group=None):
        effective_strategy_name = strategy_group if strategy_group else strategy_name

        # Ensure strategyGroup exists in weights and performance
        if effective_strategy_name not in strategy_weights:
            strategy_weights[effective_strategy_name] = 1.0
        if effective_strategy_name not in prediction_performance:
            prediction_performance[effective_strategy_name] = {'correct': 0, 'total': 0}

        weight = strategy_weights[effective_strategy_name]
        confidence = conf_multiplier * weight
        analysis["rawPredictions"].append({
            "strategy": strategy_name,
            "predict": predict,
            "confidence": confidence,
            "detail": detail,
            "strategyGroup": effective_strategy_name
        })

    # --- Áp dụng tất cả các mẫu cầu đã định nghĩa ---
    for p in all_pattern_strategies:
        if len(history) >= p["minHistory"]:
            target_history_string = ""
            if p["minHistory"] <= 10:
                target_history_string = recent10
            elif p["minHistory"] <= 20:
                target_history_string = recent20
            elif p["minHistory"] <= 50:
                target_history_string = recent50
            else:
                target_history_string = recent_history_full

            if target_history_string.endswith(p["pattern"]):
                add_prediction(p["name"], p["predict"], p["conf"], f"Phát hiện: {p['name']}", p["strategyGroup"])

    # --- Chiến lược Bẻ cầu thông minh ---
    if len(history) >= 7:
        if recent_history_full.endswith("TTTTTTT"):
            add_prediction("Bẻ cầu bệt dài", "X", 0.35, "Cầu bệt Tài quá dài (>7), dự đoán bẻ cầu")
        elif recent_history_full.endswith("XXXXXXX"):
            add_prediction("Bẻ cầu bệt dài", "T", 0.35, "Cầu bệt Xỉu quá dài (>7), dự đoán bẻ cầu")

        if recent_history_full.endswith("XTXTXTXT"):
            add_prediction("Bẻ cầu 1-1 dài", "X", 0.3, "Cầu 1-1 quá dài (>8), dự đoán bẻ sang Xỉu")
        elif recent_history_full.endswith("TXTXTXTX"):
            add_prediction("Bẻ cầu 1-1 dài", "T", 0.3, "Cầu 1-1 quá dài (>8), dự đoán bẻ sang Tài")

    # --- Chiến lược: Phân tích xu hướng ---
    tai_in_20 = recent20.count('T')
    xiu_in_20 = recent20.count('X')

    if tai_in_20 > xiu_in_20 + 5:
        add_prediction("Xu hướng Tài mạnh (Ngắn)", "T", 0.25, f"Xu hướng 20 phiên: Nghiêng về Tài ({tai_in_20} Tài / {xiu_in_20} Xỉu)")
    elif xiu_in_20 > tai_in_20 + 5:
        add_prediction("Xu hướng Xỉu mạnh (Ngắn)", "X", 0.25, f"Xu hướng 20 phiên: Nghiêng về Xỉu ({tai_in_20} Tài / {xiu_in_20} Xỉu)")
    else:
        analysis["predictionDetails"].append(f"Xu hướng 20 phiên: Khá cân bằng ({tai_in_20} Tài / {xiu_in_20} Xỉu)")

    tai_in_50 = recent50.count('T')
    xiu_in_50 = recent50.count('X')
    if tai_in_50 > xiu_in_50 + 8:
        add_prediction("Xu hướng Tài rất mạnh (Dài)", "T", 0.3, f"Xu hướng 50 phiên: Rất nghiêng về Tài ({tai_in_50} Tài / {xiu_in_50} Xỉu)")
    elif xiu_in_50 > tai_in_50 + 8:
        add_prediction("Xu hướng Xỉu rất mạnh (Dài)", "X", 0.3, f"Xu hướng 50 phiên: Rất nghiêng về Xỉu ({tai_in_50} Tài / {xiu_in_50} Xỉu)")

    # --- Chiến lược: Phân tích Xúc Xắc và Tổng Điểm Cụ Thể ---
    if len(dice_hist) > 0:
        last_result_dice = dice_hist[-1]
        total = last_result_dice['d1'] + last_result_dice['d2'] + last_result_dice['d3']
        analysis["predictionDetails"].append(f"Kết quả xúc xắc gần nhất: {last_result_dice['d1']}-{last_result_dice['d2']}-{last_result_dice['d3']} (Tổng: {total})")

        last10_totals = [d['total'] for d in dice_hist[-10:]]
        sum_counts = {}
        for val in last10_totals:
            sum_counts[val] = sum_counts.get(val, 0) + 1

        most_frequent_total = 0
        max_count = 0
        for s, count in sum_counts.items():
            if count > max_count:
                max_count = count
                most_frequent_total = s

        if max_count >= 4:
            predict = "T" if most_frequent_total > 10 else "X"
            add_prediction("Xu hướng tổng điểm", predict, 0.15, f"Tổng điểm {most_frequent_total} xuất hiện nhiều trong 10 phiên gần nhất")

        if last_result_dice['d1'] == last_result_dice['d2'] and last_result_dice['d2'] == last_result_dice['d3']:
            predict = "T" if last_result_dice['d1'] <= 3 else "X" # Bộ ba Tài (4,5,6) thì bẻ Xỉu, bộ ba Xỉu (1,2,3) thì bẻ Tài
            add_prediction("Bộ ba", predict, 0.25, f"Phát hiện bộ ba {last_result_dice['d1']}, dự đoán bẻ cầu")

        if total == 10:
            add_prediction("Điểm 10", "X", 0.08, "Tổng 10 (Xỉu) vừa ra, thường là điểm dao động hoặc bẻ cầu")
        elif total == 11:
            add_prediction("Điểm 11", "T", 0.08, "Tổng 11 (Tài) vừa ra, thường là điểm dao động hoặc bẻ cầu")

    # --- Chiến lược: "Reset Cầu" hoặc "Bẻ Sâu" ---
    if len(history) > 20:
        last10 = history[-10:]
        tai_in_10 = last10.count('T')
        xiu_in_10 = last10.count('X')

        if abs(tai_in_10 - xiu_in_10) <= 2:
            if not analysis["rawPredictions"] or analysis["rawPredictions"][0]["confidence"] < 0.2:
                last_result_pattern = history[-1]
                predict = 'X' if last_result_pattern == 'T' else 'T'
                add_prediction("Reset Cầu/Bẻ Sâu", predict, 0.28, "Cầu đang loạn hoặc khó đoán, dự đoán reset.")

        if recent_history_full.endswith("TTTTTTTTT"):
            add_prediction("Reset Cầu/Bẻ Sâu", "X", 0.4, "Cầu bệt Tài cực dài (>9), dự đoán bẻ mạnh!")
        elif recent_history_full.endswith("XXXXXXXXX"):
            add_prediction("Reset Cầu/Bẻ Sâu", "T", 0.4, "Cầu bệt Xỉu cực dài (>9), dự đoán bẻ mạnh!")


    # --- KẾT HỢP CÁC DỰ ĐOÁN VÀ TÍNH ĐỘ TIN CẬY CUỐI CÙNG ---
    analysis["rawPredictions"].sort(key=lambda x: x["confidence"], reverse=True)

    vote_tai = 0.0
    vote_xiu = 0.0

    number_of_top_predictions = min(len(analysis["rawPredictions"]), 5)
    top_predictions = analysis["rawPredictions"][:number_of_top_predictions]

    for p in top_predictions:
        if p["predict"] == 'T':
            vote_tai += p["confidence"]
        elif p["predict"] == 'X':
            vote_xiu += p["confidence"]

    if vote_tai == 0 and vote_xiu == 0:
        final_prediction = "?"
        combined_confidence = 0.0
    elif vote_tai > vote_xiu * 1.3:
        final_prediction = "T"
        combined_confidence = vote_tai / (vote_tai + vote_xiu)
    elif vote_xiu > vote_tai * 1.3:
        final_prediction = "X"
        combined_confidence = vote_xiu / (vote_tai + vote_xiu)
    else:
        if analysis["rawPredictions"]:
            final_prediction = analysis["rawPredictions"][0]["predict"]
            combined_confidence = analysis["rawPredictions"][0]["confidence"]
        else:
            final_prediction = "?"
            combined_confidence = 0.0

    # --- ÁNH XẠ ĐỘ TIN CẬY ---
    min_output_confidence = 0.55
    max_output_confidence = 0.92
    original_min_confidence = 0.0
    original_max_confidence = 1.0

    normalized_confidence = min(max(combined_confidence, original_min_confidence), original_max_confidence)
    final_mapped_confidence = ((normalized_confidence - original_min_confidence) / (original_max_confidence - original_min_confidence)) * (max_output_confidence - min_output_confidence) + min_output_confidence
    final_mapped_confidence = min(max(final_mapped_confidence, min_output_confidence), max_output_confidence)

    analysis["finalPrediction"] = final_prediction
    analysis["confidence"] = final_mapped_confidence

    analysis["predictionDetails"] = [
        f"{p['strategy']}: {p['predict']} (Conf: {p['confidence'] * 100:.1f}%) - {p.get('detail', '')}"
        for p in analysis["rawPredictions"]
    ]

    return analysis

def update_strategy_weight(strategy_name, predicted_result, actual_result):
    global prediction_performance, strategy_weights

    strategy_info = next((p for p in all_pattern_strategies if p['name'] == strategy_name), None)
    effective_strategy_name = strategy_info['strategyGroup'] if strategy_info else strategy_name

    if effective_strategy_name not in prediction_performance:
        prediction_performance[effective_strategy_name] = {'correct': 0, 'total': 0}

    prediction_performance[effective_strategy_name]['total'] += 1

    if predicted_result == actual_result:
        prediction_performance[effective_strategy_name]['correct'] += 1

    correct = prediction_performance[effective_strategy_name]['correct']
    total = prediction_performance[effective_strategy_name]['total']

    if total >= 5: # Chỉ điều chỉnh sau một số lần thử nhất định
        accuracy = correct / total
        adjustment_factor = 0.05

        if accuracy > 0.6: # Tăng trọng số nếu độ chính xác tốt
            strategy_weights[effective_strategy_name] = min(strategy_weights.get(effective_strategy_name, 1.0) + adjustment_factor, 2.5)
        elif accuracy < 0.4: # Giảm trọng số nếu độ chính xác kém
            strategy_weights[effective_strategy_name] = max(strategy_weights.get(effective_strategy_name, 1.0) - adjustment_factor, 0.5)

# --- Logic Đăng ký & Gia hạn ---
def check_subscription(user_id):
    user_id_str = str(user_id)
    if user_id_str not in user_data:
        user_data[user_id_str] = {"subscribed": False, "sub_end_date": None, "ref_by": None}
        save_user_data()
        return False, "Bạn chưa đăng ký sử dụng dịch vụ."

    sub_end_date_ts = user_data[user_id_str].get("sub_end_date")
    if sub_end_date_ts:
        sub_end_datetime = datetime.datetime.fromtimestamp(sub_end_date_ts)
        if sub_end_datetime > datetime.datetime.now():
            return True, f"Gói của bạn còn hạn đến: {sub_end_datetime.strftime('%H:%M %d/%m/%Y')}."
        else:
            user_data[user_id_str]["subscribed"] = False
            user_data[user_id_str]["sub_end_date"] = None
            save_user_data()
            return False, "Gói của bạn đã hết hạn."
    return False, "Bạn chưa đăng ký sử dụng dịch vụ."

def add_subscription(user_id, duration_days, code_type):
    user_id_str = str(user_id)
    current_time = datetime.datetime.now()
    current_sub_end_date_ts = user_data[user_id_str].get("sub_end_date")

    if current_sub_end_date_ts and datetime.datetime.fromtimestamp(current_sub_end_date_ts) > current_time:
        # Nếu đang có gói, gia hạn từ ngày hết hạn hiện tại
        base_time = datetime.datetime.fromtimestamp(current_sub_end_date_ts)
    else:
        # Nếu chưa có gói hoặc đã hết hạn, bắt đầu từ bây giờ
        base_time = current_time

    new_sub_end_date = base_time + datetime.timedelta(days=duration_days)
    user_data[user_id_str]["subscribed"] = True
    user_data[user_id_str]["sub_end_date"] = new_sub_end_date.timestamp()
    save_user_data()
    return new_sub_end_date

# --- Lệnh Bot Telegram ---
@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id_str = str(message.from_user.id)
    if user_id_str not in user_data:
        user_data[user_id_str] = {"subscribed": False, "sub_end_date": None, "ref_by": None}
        save_user_data()

    bot.reply_to(message, "Chào mừng bạn đến với Bot Dự Đoán Tài Xỉu!\n"
                           "Sử dụng /dangky để đăng ký hoặc /checksub để kiểm tra trạng thái gói.")

@bot.message_handler(commands=['dangky'])
def register(message):
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "Vui lòng nhập mã đăng ký. Ví dụ: `/dangky MA_CUA_BAN`")
        return

    code_input = args[1].strip()
    user_id = str(message.from_user.id)

    if code_input not in codes:
        bot.reply_to(message, "Mã đăng ký không hợp lệ hoặc không tồn tại.")
        return

    code_info = codes[code_input]
    if code_info["used_by"] is not None:
        bot.reply_to(message, "Mã này đã được sử dụng bởi người khác.")
        return

    # Kiểm tra hạn sử dụng của mã (nếu là mã giới thiệu có thời hạn)
    if code_info.get("expires") and datetime.datetime.fromtimestamp(code_info["expires"]) < datetime.datetime.now():
        bot.reply_to(message, "Mã này đã hết hạn sử dụng.")
        del codes[code_input] # Xóa mã hết hạn
        save_codes()
        return

    # Xác định loại mã và thời gian gia hạn
    duration_days = 0
    code_type = code_info.get("type", "unknown")
    if code_type == "trial":
        duration_days = 1 # 1 ngày dùng thử
    elif code_type == "premium":
        duration_days = 7 # 7 ngày cho gói premium (có thể thay đổi)
    elif code_type == "referral":
        duration_days = 3 # 3 ngày cho mã giới thiệu
    else:
        bot.reply_to(message, "Mã không xác định loại.")
        return

    # Kích hoạt gói cho người dùng
    new_end_date = add_subscription(user_id, duration_days, code_type)
    codes[code_input]["used_by"] = user_id
    save_codes()

    bot.reply_to(message, f"Bạn đã đăng ký/gia hạn thành công gói {code_type.upper()}!\n"
                           f"Gói của bạn có hiệu lực đến: {new_end_date.strftime('%H:%M %d/%m/%Y')}.\n"
                           "Bot sẽ bắt đầu gửi kết quả và dự đoán cho bạn.")

@bot.message_handler(commands=['checksub'])
def check_sub_status(message):
    user_id = message.from_user.id
    is_sub, sub_message = check_subscription(user_id)
    if is_sub:
        bot.reply_to(message, f"Gói của bạn còn hạn. {sub_message}")
    else:
        bot.reply_to(message, f"Bạn chưa có gói hoạt động. {sub_message}\n"
                               "Sử dụng /dangky <mã> để đăng ký hoặc /goi để xem các gói.")

@bot.message_handler(commands=['goi'])
def show_packages(message):
    bot.reply_to(message, "Hiện tại bot cung cấp các gói sau:\n"
                           "- Gói dùng thử: 1 ngày (liên hệ admin để nhận mã)\n"
                           "- Gói Premium: 7 ngày (liên hệ admin để mua)\n"
                           "Để đăng ký, sử dụng lệnh: `/dangky MA_CUA_BAN`")

---
## Lệnh Admin

Các lệnh dưới đây chỉ có thể được sử dụng bởi các User ID đã được định nghĩa trong biến `ADMIN_IDS`.

```python
def is_admin(user_id):
    return user_id in ADMIN_IDS

@bot.message_handler(commands=['adminhelp'])
def admin_help(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "Bạn không có quyền sử dụng lệnh này.")
        return
    bot.reply_to(message, "Các lệnh Admin:\n"
                           "/gen_code <type> <duration_days> - Tạo mã đăng ký. Ví dụ: /gen_code trial 1 hoặc /gen_code premium 7\n"
                           "/bot_status - Kiểm tra trạng thái bot\n"
                           "/pause_bot <lý do> - Tạm dừng bot\n"
                           "/resume_bot - Khởi động lại bot\n"
                           "/sub_info <user_id> - Xem thông tin đăng ký của user\n"
                           "/all_users - Xem danh sách tất cả người dùng và trạng thái đăng ký của họ\n"
                           "/clear_expired_codes - Xóa các mã đã hết hạn")

@bot.message_handler(commands=['gen_code'])
def generate_code(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "Bạn không có quyền sử dụng lệnh này.")
        return

    args = message.text.split()
    if len(args) < 3:
        bot.reply_to(message, "Sử dụng: `/gen_code <type> <duration_days>`. Ví dụ: `/gen_code trial 1` hoặc `/gen_code premium 7`")
        return

    code_type = args[1].lower()
    try:
        duration_days = int(args[2])
        if duration_days <= 0:
            raise ValueError
    except ValueError:
        bot.reply_to(message, "Số ngày không hợp lệ. Vui lòng nhập số nguyên dương.")
        return

    new_code = os.urandom(8).hex() # Mã ngẫu nhiên 16 ký tự

    expires_at = (datetime.datetime.now() + datetime.timedelta(days=duration_days)).timestamp()

    codes[new_code] = {
        "used_by": None,
        "expires": expires_at,
        "type": code_type
    }
    save_codes()
    bot.reply_to(message, f"Đã tạo mã mới: `{new_code}` (Loại: {code_type}, Hạn: {duration_days} ngày)")

@bot.message_handler(commands=['bot_status'])
def check_bot_status(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "Bạn không có quyền sử dụng lệnh này.")
        return
    status = "ĐANG HOẠT ĐỘNG" if bot_enabled else "ĐANG TẠM DỪNG"
    bot.reply_to(message, f"Trạng thái bot: **{status}**\nLý do: {bot_disable_reason}", parse_mode='Markdown')

@bot.message_handler(commands=['pause_bot'])
def pause_bot(message):
    global bot_enabled, bot_disable_reason
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "Bạn không có quyền sử dụng lệnh này.")
        return
    
    reason = " ".join(message.text.split()[1:]) if len(message.text.split()) > 1 else "Không có lý do cụ thể."
    bot_enabled = False
    bot_disable_reason = reason
    bot.reply_to(message, f"Bot đã tạm dừng. Lý do: {reason}")

@bot.message_handler(commands=['resume_bot'])
def resume_bot(message):
    global bot_enabled, bot_disable_reason
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "Bạn không có quyền sử dụng lệnh này.")
        return
    
    bot_enabled = True
    bot_disable_reason = "Bot đang hoạt động bình thường."
    bot.reply_to(message, "Bot đã được khởi động lại.")

@bot.message_handler(commands=['sub_info'])
def sub_info(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "Bạn không có quyền sử dụng lệnh này.")
        return
    
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "Vui lòng cung cấp ID người dùng. Ví dụ: `/sub_info 123456789`")
        return
    
    target_user_id = args[1].strip()
    
    if target_user_id not in user_data:
        bot.reply_to(message, f"Không tìm thấy người dùng với ID: `{target_user_id}`")
        return
    
    user_info = user_data[target_user_id]
    is_sub, sub_msg = check_subscription(int(target_user_id)) # Dùng hàm check_subscription để cập nhật trạng thái
    
    sub_status = "Đã đăng ký" if is_sub else "Chưa đăng ký/Hết hạn"
    end_date_str = "N/A"
    if user_info.get("sub_end_date"):
        end_date_str = datetime.datetime.fromtimestamp(user_info["sub_end_date"]).strftime('%H:%M %d/%m/%Y')
    
    ref_by = user_info.get("ref_by", "Không")
    
    info_message = (
        f"**Thông tin đăng ký của User ID**: `{target_user_id}`\n"
        f"Trạng thái: **{sub_status}**\n"
        f"Hạn sử dụng: `{end_date_str}`\n"
        f"Được giới thiệu bởi: `{ref_by}`"
    )
    bot.reply_to(message, info_message, parse_mode='Markdown')

@bot.message_handler(commands=['all_users'])
def list_all_users(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "Bạn không có quyền sử dụng lệnh này.")
        return

    if not user_data:
        bot.reply_to(message, "Chưa có người dùng nào trong cơ sở dữ liệu.")
        return

    response_messages = []
    current_message = "**Danh sách Người dùng:**\n\n"

    for user_id_str, user_info in user_data.items():
        is_sub, _ = check_subscription(int(user_id_str)) # Cập nhật trạng thái
        sub_status = "✅ Active" if is_sub else "❌ Hết hạn/Chưa đăng ký"
        end_date_str = "N/A"
        if user_info.get("sub_end_date"):
            end_date_str = datetime.datetime.fromtimestamp(user_info["sub_end_date"]).strftime('%d/%m/%Y %H:%M')

        user_line = f"ID: `{user_id_str}` | Trạng thái: {sub_status} | Hạn: {end_date_str}\n"

        if len(current_message) + len(user_line) > 4000: # Giới hạn tin nhắn Telegram
            response_messages.append(current_message)
            current_message = ""
        current_message += user_line
    
    if current_message:
        response_messages.append(current_message)

    for msg in response_messages:
        try:
            bot.send_message(message.chat.id, msg, parse_mode='Markdown')
            time.sleep(0.5) # Giãn cách để tránh bị giới hạn tốc độ của Telegram
        except telebot.apihelper.ApiTelegramException as e:
            print(f"LỖI: Không thể gửi tin nhắn cho admin: {e}")
            break # Dừng nếu có lỗi nghiêm trọng

@bot.message_handler(commands=['clear_expired_codes'])
def clear_expired_codes(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "Bạn không có quyền sử dụng lệnh này.")
        return

    removed_count = 0
    codes_to_delete = []
    current_time = datetime.datetime.now().timestamp()

    for code, info in codes.items():
        if info.get("expires") and info["expires"] < current_time:
            codes_to_delete.append(code)

    for code in codes_to_delete:
        del codes[code]
        removed_count += 1
    
    save_codes()
    bot.reply_to(message, f"Đã xóa {removed_count} mã đã hết hạn.")

---
## Logic chính của Bot (chạy trong luồng riêng)

Phần này của code chạy độc lập, liên tục lấy dữ liệu từ API và gửi dự đoán.

```python
def prediction_loop(stop_event: threading.Event):
    global last_raw_predictions
    last_id = None

    print("LOG: Luồng hiển thị kết quả đã khởi động.")
    sys.stdout.flush()

    while not stop_event.is_set():
        if not bot_enabled:
            print(f"LOG: Bot đang tạm dừng. Lý do: {bot_disable_reason}")
            sys.stdout.flush()
            time.sleep(10) # Dừng lâu hơn khi bot bị tạm dừng
            continue

        data = lay_du_lieu_moi()
        if not data:
            print("LOG: ❌ Không lấy được dữ liệu từ API hoặc dữ liệu không hợp lệ. Đang chờ phiên mới...")
            sys.stdout.flush()
            time.sleep(5)
            continue

        issue_id = data.get("ID")
        expect = data.get("Expect")
        open_code_str = data.get("OpenCode") # Lấy OpenCode dưới dạng chuỗi
        
        if not all([issue_id, expect, open_code_str]):
            print(f"LOG: Dữ liệu API không đầy đủ (thiếu ID, Expect, hoặc OpenCode) cho phiên {expect}. Bỏ qua phiên này. Dữ liệu: {data}")
            sys.stdout.flush()
            last_id = issue_id # Đảm bảo không xử lý lại ID này nếu nó vẫn là ID cuối cùng
            time.sleep(5)
            continue

        if issue_id != last_id:
            try:
                # Phân tích chuỗi "3,4,5" thành list các số nguyên [3, 4, 5]
                dice = [int(d.strip()) for d in open_code_str.split(',')]
                if len(dice) != 3:
                    raise ValueError("OpenCode không chứa 3 giá trị xúc xắc.")
            except ValueError as e:
                print(f"LỖI: Lỗi phân tích OpenCode: '{open_code_str}'. {e}. Bỏ qua phiên này.")
                sys.stdout.flush()
                last_id = issue_id
                time.sleep(5)
                continue
            except Exception as e:
                print(f"LỖI: Lỗi không xác định khi xử lý OpenCode '{open_code_str}': {e}. Bỏ qua phiên này.")
                sys.stdout.flush()
                last_id = issue_id
                time.sleep(5)
                continue

            ket_qua_tx, tong = tinh_tai_xiu(dice)

            # --- CẬP NHẬT LỊCH SỬ DỰ ĐOÁN VÀ ĐIỀU CHỈNH TRỌNG SỐ ---
            if last_raw_predictions: # Nếu có dự đoán thô từ phiên trước
                actual_result = "T" if ket_qua_tx == "Tài" else ("X" if ket_qua_tx == "Xỉu" else "Bộ Ba")
                print(f"DEBUG: Cập nhật trọng số cho phiên trước ({last_id}). Kết quả thực tế: {actual_result}")
                for pred in last_raw_predictions:
                    # Chỉ cập nhật trọng số cho các dự đoán T/X
                    if pred['predict'] == 'T' or pred['predict'] == 'X':
                        update_strategy_weight(pred['strategy'], pred['predict'], actual_result)
                last_raw_predictions = [] # Xóa dự đoán thô sau khi đã cập nhật

            # Cập nhật pattern_history (giới hạn 200 phiên) chỉ với T/X
            if ket_qua_tx in ["Tài", "Xỉu"]:
                pattern_history.append("T" if ket_qua_tx == "Tài" else "X")
                if len(pattern_history) > 200:
                    pattern_history.pop(0)

            # Cập nhật dice_history (giới hạn 50 phiên cho phân tích xúc xắc)
            dice_history.append({"d1": dice[0], "d2": dice[1], "d3": dice[2], "total": tong})
            if len(dice_history) > 50:
                dice_history.pop(0)

            # Lưu dữ liệu dự đoán sau mỗi phiên
            save_prediction_data()

            # --- TIẾN HÀNH DỰ ĐOÁN CHO PHIÊN TIẾP THEO ---
            prediction_analysis = analyze_and_predict(pattern_history, dice_history)
            predicted_result = prediction_analysis["finalPrediction"]
            confidence_percent = prediction_analysis["confidence"] * 100

            # Lưu dự đoán thô của phiên này để cập nhật trọng số trong phiên tiếp theo
            last_raw_predictions = prediction_analysis["rawPredictions"]

            # Gửi tin nhắn kết quả và dự đoán tới tất cả người dùng có quyền truy cập
            for user_id_str, user_info in list(user_data.items()):
                user_id = int(user_id_str)
                is_sub, sub_message = check_subscription(user_id)
                if is_sub:
                    try:
                        result_message = (
                            "🎮 **KẾT QUẢ PHIÊN MỚI NHẤT** 🎮\n"
                            f"Phiên: `{expect}` | Kết quả: **{ket_qua_tx}** (Tổng: **{tong}**)\n"
                            f"🎲 Xúc xắc: `{open_code_str}`\n\n"
                            f"🔮 **DỰ ĐOÁN PHIÊN KẾ TIẾP:**\n"
                            f"Dự đoán: **{predicted_result}** | Tỉ lệ: **{confidence_percent:.2f}%**\n\n"
                            "⚠️ **Chúc bạn may mắn!**"
                        )
                        bot.send_message(user_id, result_message, parse_mode='Markdown')
                        print(f"DEBUG: Đã gửi kết quả & dự đoán cho user {user_id_str}")
                        sys.stdout.flush()
                    except telebot.apihelper.ApiTelegramException as e:
                        print(f"LỖI: Lỗi Telegram API khi gửi tin nhắn cho user {user_id}: {e}")
                        sys.stdout.flush()
                        if "bot was blocked by the user" in str(e) or "user is deactivated" in str(e):
                            print(f"CẢNH BÁO: Người dùng {user_id} đã chặn bot hoặc bị vô hiệu hóa.")
                            sys.stdout.flush()
                            # Tùy chọn: Xóa người dùng khỏi user_data nếu họ chặn bot
                            # del user_data[user_id_str]
                            # save_user_data()
                    except Exception as e:
                        print(f"LỖI: Lỗi không xác định khi gửi tin nhắn cho user {user_id}: {e}")
                        sys.stdout.flush()

            print("-" * 50)
            print("LOG: Phiên {}. Kết quả: {} ({}). Xúc xắc: {}".format(expect, ket_qua_tx, tong, open_code_str))
            print(f"LOG: Dự đoán phiên kế tiếp: {predicted_result} với độ tin cậy {confidence_percent:.2f}%")
            print("-" * 50)
            sys.stdout.flush()

            last_id = issue_id

        time.sleep(5) # Kiểm tra mỗi 5 giây
    print("LOG: Luồng hiển thị kết quả đã dừng.")
    sys.stdout.flush()

---
## Khởi tạo và Chạy Bot

```python
# --- Khởi tạo khi bot bắt đầu ---
def start_bot_threads():
    global bot_initialized
    with bot_init_lock:
        if not bot_initialized:
            print("LOG: Đang khởi tạo luồng bot và hiển thị kết quả...")
            sys.stdout.flush()
            # Tải dữ liệu ban đầu
            load_user_data()
            load_codes()
            load_prediction_data() # Tải dữ liệu dự đoán

            # Khởi động luồng dự đoán
            global prediction_thread, stop_event
            stop_event.clear()
            prediction_thread = threading.Thread(target=prediction_loop, args=(stop_event,))
            prediction_thread.daemon = True # Cho phép luồng kết thúc khi chương trình chính kết thúc
            prediction_thread.start()
            
            bot_initialized = True
            print("LOG: Bot đã sẵn sàng nhận lệnh.")
            sys.stdout.flush()

if __name__ == '__main__':
    start_bot_threads()
    print("LOG: Bot đang chạy polling...")
    sys.stdout.flush()
    bot.polling(none_stop=True)
