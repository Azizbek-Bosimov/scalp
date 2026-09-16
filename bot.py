import requests
import time
import os
import threading
from flask import Flask
import google.generativeai as genai

# ==========================================
# 1. SOZLAMALAR
# ==========================================
# Telegram sozlamalari (O'zingiznikini kiriting!)
BOT_TOKEN = "SIZNING_TELEGRAM_BOT_TOKENINGIZ" 
CHAT_ID = "SIZNING_CHAT_ID_RAQAMINGIZ"

# Gemini AI sozlamalari
GEMINI_API_KEY = "AQ.Ab8RN6J3IyJd2SOyKRdkNthn840MMZ3V9MAxggw_Of22Z5Jj0Q"
genai.configure(api_key=GEMINI_API_KEY)
ai_model = genai.GenerativeModel('gemini-1.5-flash')

# Savdo sozlamalari
SYMBOL = "XAUUSDT"
LIMIT = 200
O, H, L, C = 1, 2, 3, 4
ZONE_MAX_DISTANCE_PCT = 0.4  
MAX_RISK_PCT = 0.02          
SL_BUFFER_ATR_MULT = 0.15
ATR_PERIOD = 14
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# ==========================================
# 2. RENDER UCHUN MITTI VEB-SERVER
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    return "Scalp Bot 24/7 ishlamoqda!"

def keep_alive():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# ==========================================
# 3. TELEGRAM VA AI FUNKSIYALARI
# ==========================================
def send_telegram(text):
    """Telegram bot orqali xabar yuborish"""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        print(f"Telegramga xabar yuborishda xato: {e}")

def ask_gemini_analysis(asset, direction, entry, sl):
    """SMC signalini Gemini orqali tasdiqlash va xulosa olish"""
    prompt = f"""
    Sen professional SMC va ICT treydersan. Hozirgi vaqtda {asset} aktivi bo'yicha 
    {direction} (kirish: {entry}, SL: {sl}) scalping signali chiqdi. 
    Iltimos, ushbu signal haqida juda qisqa (2-3 ta gap) xulosa ber va hozirgi 
    iqtisodiy vaziyat (dollar, inflyatsiya) yoki volatillik xavflari haqida ogohlantir.
    """
    try:
        response = ai_model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        print(f"Gemini bilan ulanishda xato: {e}")
        return "Fundamental analiz vaqtinchalik mavjud emas."

# ==========================================
# 4. SMC ANALIZ YADROSI
# ==========================================
def fetch_ohlcv(timeframe, symbol=SYMBOL):
    interval_map = {"1m": "1", "3m": "3", "5m": "5", "15m": "15"}
    interval = interval_map.get(timeframe, timeframe)
    response = requests.get(
        "https://api.bybit.com/v5/market/kline",
        params={"category": "linear", "symbol": symbol, "interval": interval, "limit": LIMIT},
        headers=HEADERS,
        timeout=15,
    )
    data = response.json()
    rows = data.get("result", {}).get("list", [])
    rows.sort(key=lambda row: int(row[0]))
    return [[int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])] for row in rows]

def closed_only(candles):
    return candles[:-1] if len(candles) > 1 else candles

def find_swings(candles, left=3, right=3):
    highs, lows = [], []
    for index in range(left, len(candles) - right):
        window = candles[index - left : index + right + 1]
        if candles[index][H] == max(candle[H] for candle in window):
            highs.append((index, candles[index][H]))
        if candles[index][L] == min(candle[L] for candle in window):
            lows.append((index, candles[index][L]))
    return highs, lows

def confirmed_structure_bias(candles):
    highs, lows = find_swings(candles)
    if not highs or not lows: return None
    last_high_price, last_low_price = highs[-1][1], lows[-1][1]
    start_index = max(highs[-1][0], lows[-1][0]) + 1
    bias = None
    for index in range(start_index, len(candles)):
        close = candles[index][C]
        if close > last_high_price:
            bias = "bullish"
            last_low_price = min(last_low_price, candles[index][L])
        elif close < last_low_price:
            bias = "bearish"
            last_high_price = max(last_high_price, candles[index][H])
    return bias

def detect_fvg(candles):
    fvgs = []
    for index in range(2, len(candles)):
        first, third = candles[index - 2], candles[index]
        if third[L] > first[H]:
            fvgs.append({"type": "bullish", "kind": "fvg", "top": third[L], "bottom": first[H], "index": index})
        elif third[H] < first[L]:
            fvgs.append({"type": "bearish", "kind": "fvg", "top": first[L], "bottom": third[H], "index": index})
    return fvgs

def detect_order_blocks(candles):
    bodies = [abs(candle[C] - candle[O]) for candle in candles]
    order_blocks = []
    for index in range(10, len(candles) - 1):
        average_body = sum(bodies[index - 10 : index]) / 10
        if average_body == 0: continue
        impulsive = bodies[index + 1] > average_body * 1.5
        current, following = candles[index], candles[index + 1]
        bullish_ob = current[C] < current[O] and following[C] > following[O]
        bearish_ob = current[C] > current[O] and following[C] < following[O]
        if impulsive and bullish_ob:
            order_blocks.append({"type": "bullish", "kind": "ob", "top": current[O], "bottom": current[L], "index": index})
        if impulsive and bearish_ob:
            order_blocks.append({"type": "bearish", "kind": "ob", "top": current[H], "bottom": current[O], "index": index})
    return order_blocks

def calculate_atr(candles, period=ATR_PERIOD):
    if len(candles) < period + 1: return None
    true_ranges = []
    for index in range(1, len(candles)):
        high, low, prev_close = candles[index][H], candles[index][L], candles[index - 1][C]
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(true_ranges[-period:]) / period

def build_scalp_trade(candles, bias, price, atr):
    all_zones = [z for z in detect_fvg(candles) + detect_order_blocks(candles) if z["type"] == bias]
    if not all_zones: return None
    all_zones.sort(key=lambda zone: abs(price - (zone["top"] + zone["bottom"]) / 2))
    zone = all_zones[0]

    lower, upper = min(zone["bottom"], zone["top"]), max(zone["bottom"], zone["top"])
    tolerance = price * (ZONE_MAX_DISTANCE_PCT / 100)
    if not (lower - tolerance <= price <= upper + tolerance): return None

    sl_buffer = atr * SL_BUFFER_ATR_MULT if atr else price * 0.001
    entry = price

    if bias == "bullish":
        stop_loss = zone["bottom"] - sl_buffer
        risk = entry - stop_loss
        if risk <= 0 or risk > entry * MAX_RISK_PCT: return None
        return {"signal": "LONG 🟢", "entry": entry, "sl": stop_loss, "tp1": entry + risk * 1.5, "tp2": entry + risk * 3.0}
    else:
        stop_loss = zone["top"] + sl_buffer
        risk = stop_loss - entry
        if risk <= 0 or risk > entry * MAX_RISK_PCT: return None
        return {"signal": "SHORT 🔴", "entry": entry, "sl": stop_loss, "tp1": entry - risk * 1.5, "tp2": entry - risk * 3.0}

# ==========================================
# 5. ASOSIY SIKL VA ISHGA TUSHIRISH
# ==========================================
def run_scalp_analyser():
    candles5_raw = fetch_ohlcv("5m")
    bias5 = confirmed_structure_bias(closed_only(candles5_raw))
    
    candles1_raw = fetch_ohlcv("1m")
    closed1 = closed_only(candles1_raw)
    price = candles1_raw[-1][C] 
    atr1 = calculate_atr(closed1)
    
    if bias5 is None:
        print(f"[{price}] Konsolidatsiya. Trend kutilmoqda...")
        return

    trade = build_scalp_trade(closed1, bias5, price, atr1)
    
    if trade:
        print("Signal topildi! Gemini fikri olinmoqda...")
        ai_xulosa = ask_gemini_analysis(SYMBOL, trade['signal'], round(trade['entry'], 2), round(trade['sl'], 2))
        
        xabar = (
            f"⚡ **SCALP SIGNAL YARATILDI** ⚡\n\n"
            f"📊 **Aktiv:** {SYMBOL}\n"
            f"🔥 **Yo'nalish:** {trade['signal']} ({bias5})\n"
            f"🎯 **Kirish:** {round(trade['entry'], 2)}\n"
            f"🛡 **Stop Loss:** {round(trade['sl'], 2)}\n"
            f"✅ **TP1:** {round(trade['tp1'], 2)}\n"
            f"✅ **TP2:** {round(trade['tp2'], 2)}\n\n"
            f"🤖 **Gemini AI Xulosasi:**\n_{ai_xulosa}_"
        )
        send_telegram(xabar)
        print("Telegramga signal yuborildi. Keyingi tekshiruvgacha pauza...")
        time.sleep(300) # Bitta signal bergach, takrorlanmasligi uchun 5 daqiqa kutadi
    else:
        print(f"[{price}] - 5m Trend: {bias5}. Zonalarda retest kutilmoqda...")

if __name__ == "__main__":
    # 1. Veb-serverni orqa fonda ishga tushirish (Render uchun)
    server_thread = threading.Thread(target=keep_alive, daemon=True)
    server_thread.start()
    
    print("AI Scalping Bot va Veb-server ishga tushdi...")
    
    # 2. Asosiy bot sikli (1 soniyada tekshiradi)
    while True:
        try:
            run_scalp_analyser()
        except Exception as e:
            print(f"Kutilmagan xatolik: {e}")
        time.sleep(1)