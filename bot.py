"""
GOLD (XAUUSDT) 5m/15m SMC + ICT Bot - Birlashtirilgan to'liq versiya (TUZATILGAN)
Barcha yangilanishlar: ICT Killzones, Liquidity Sweeps, 4H HTF Filter, Fresh Zones, ATR SL Buffer, 
KIRISH ZONALARI (Entry Zones) va GEMINI AI Analizi.
"""

import datetime
import html
import json
import logging
import math
import os
import threading
import time
import traceback
from collections import deque

import requests
from flask import Flask
import google.generativeai as genai

# ==================== LOGGING ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("gold_smc_bot")

# ==================== GEMINI AI SOZLAMALARI ====================
GEMINI_API_KEY = "AQ.Ab8RN6J3IyJd2SOyKRdkNthn840MMZ3V9MAxggw_Of22Z5Jj0Q"
genai.configure(api_key=GEMINI_API_KEY)
ai_model = genai.GenerativeModel('gemini-1.5-flash')

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
        logger.error(f"Gemini bilan ulanishda xato: {e}")
        return "Fundamental analiz vaqtinchalik mavjud emas."

# ==================== ASOSIY SOZLAMALAR ====================
SYMBOL = "XAUUSDT"
LIMIT = 200
RR1, RR2 = 1.5, 3.0
SWING_LEFT, SWING_RIGHT = 3, 3
CHECK_INTERVAL_SEC = 1  
ZONE_MAX_DISTANCE_PCT = 0.4       
MAX_RISK_PCT = 0.02               
TRADE_STALE_WARNING_HOURS = 6     
LOG_FILE = os.path.join(os.path.dirname(__file__), "trade_log.json")
STATUS_FILE = os.path.join(os.path.dirname(__file__), "status.json")

# ==================== POSITION SIZING ====================
ACCOUNT_FILE = os.path.join(os.path.dirname(__file__), "account.json")
DEFAULT_RISK_PER_TRADE_PCT = 1.0   
MAX_RISK_PER_TRADE_PCT = 5.0       
XAUUSD_LOT_UNITS = 100              
MIN_LOT_STEP = 0.01                 

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

KILLZONES_ENABLED = True
KILLZONES = [
    {"start": datetime.time(7, 0), "end": datetime.time(11, 0)},   
    {"start": datetime.time(12, 0), "end": datetime.time(16, 0)}   
]

def _get_secret(name):
    value = os.environ.get(name)
    if value:
        return value
    try:
        import config
        return getattr(config, name)
    except (ImportError, AttributeError):
        return None

# ==================== TELEGRAM SOZLAMALARI ====================
# SHU YERGA O'ZINGIZNING TOKEN VA CHAT ID RAQAMINGIZNI YOZING!
BOT_TOKEN = _get_secret("BOT_TOKEN") or "SIZNING_BOT_TOKENINGIZNI_SHU_YERGA_YOZING"
CHAT_ID = _get_secret("CHAT_ID") or "SIZNING_CHAT_ID_RAQAMINGIZ"
ADMIN_CHAT_ID = str(CHAT_ID)  
SUBSCRIBERS_FILE = os.path.join(os.path.dirname(__file__), "subscribers.json")
PRICE_OFFSET = -5.0

def load_subscribers():
    if os.path.exists(SUBSCRIBERS_FILE):
        try:
            with open(SUBSCRIBERS_FILE, encoding='utf-8') as file:
                return set(json.load(file))
        except Exception as error:
            logger.error(f"Obunachilarni o'qishda xato: {error}")
    return {ADMIN_CHAT_ID}

def save_subscribers():
    try:
        with open(SUBSCRIBERS_FILE, 'w', encoding='utf-8') as file:
            json.dump(list(SUBSCRIBERS), file)
    except Exception as error:
        logger.error(f"Obunachilarni saqlashda xato: {error}")

SUBSCRIBERS = load_subscribers()

def load_account():
    if os.path.exists(ACCOUNT_FILE):
        try:
            with open(ACCOUNT_FILE, encoding='utf-8') as file:
                data = json.load(file)
                return {
                    "balance": data.get("balance"),
                    "risk_pct": data.get("risk_pct", DEFAULT_RISK_PER_TRADE_PCT),
                }
        except Exception as error:
            logger.error(f"Hisob ma'lumotini o'qishda xato: {error}")
    return {"balance": None, "risk_pct": DEFAULT_RISK_PER_TRADE_PCT}

def _atomic_write(path, data):
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding='utf-8') as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)

def save_account():
    _atomic_write(ACCOUNT_FILE, ACCOUNT)

ACCOUNT = load_account()

O, H, L, C = 1, 2, 3, 4

state_lock = threading.RLock()
current_trade = None
warned_flip = False
paused = False
last_impulse_ts = None
IMPULSE_LOOKBACK = 20
IMPULSE_THRESHOLD = 2.5
post_trade = None
POST_TRADE_CHECKS = 20
POST_TRADE_MIN_CONTINUATION = 20

PRICE_HISTORY_MAXLEN = 300   
PRICE_HISTORY = deque(maxlen=PRICE_HISTORY_MAXLEN)
last_impulse_info = None   

last_status = {
    "price": None, "bias5": None, "bias15": None,
    "bias1h": None, "bias4h": None, "bias1d": None, "checked_at": None,
}

RSI_PERIOD = 14
ATR_PERIOD = 14
VWAP_LOOKBACK = 48
MIN_CONFIRMATIONS = 3
ATR_MIN_RISK_MULT = 0.3
BTC_SYMBOL = "BTCUSDT"
HTF_FILTER_ENABLED = True
STRONG_HTF_FILTER_ENABLED = True
DAILY_HTF_FILTER_ENABLED = True   
ZONE_MAX_AGE_BARS = 40
SL_BUFFER_ATR_MULT = 0.15
NEWS_FILTER_ENABLED = True
NEWS_BLOCK_MINUTES_BEFORE = 30
NEWS_BLOCK_MINUTES_AFTER = 30
NEWS_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
NEWS_CACHE_TTL_SEC = 3600
_news_cache = {"events": None, "fetched_at": None}

def closed_only(candles):
    if len(candles) > 1:
        return candles[:-1]
    return candles

def in_killzone():
    if not KILLZONES_ENABLED:
        return True
    now_utc = datetime.datetime.now(datetime.timezone.utc).time()
    for kz in KILLZONES:
        if kz["start"] <= now_utc <= kz["end"]:
            return True
    return False

def detect_impulse(candles, lookback=IMPULSE_LOOKBACK, threshold=IMPULSE_THRESHOLD):
    if len(candles) < lookback + 1:
        return None
    ranges = [c[H] - c[L] for c in candles]
    avg_range = sum(ranges[-lookback - 1 : -1]) / lookback
    last = candles[-1]
    last_range = last[H] - last[L]
    if avg_range == 0:
        return None
    ratio = last_range / avg_range
    if ratio >= threshold:
        direction = "yuqoriga" if last[C] > last[O] else "pastga"
        return {
            "ts": last[0], "ratio": round(ratio, 1), "direction": direction,
            "range": round(last_range, 2), "price": round(last[C], 2),
        }
    return None

def load_log():
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, encoding='utf-8') as file:
                return json.load(file)
        except json.JSONDecodeError:
            return []
    return []

def save_log(log):
    _atomic_write(LOG_FILE, log)

def save_status(price, bias5, bias15, bias1h=None, bias4h=None, bias1d=None):
    with state_lock:
        trade_snapshot = current_trade
    _atomic_write(
        STATUS_FILE,
        {
            "currentTrade": trade_snapshot, "lastPrice": price,
            "lastCheckedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "bias5m": bias5, "bias15m": bias15, "bias1h": bias1h,
            "bias4h": bias4h, "bias1d": bias1d,
        },
    )

def win_rate_text(log):
    if not log: return "Hali statistika yo'q (birinchi savdolar)"
    wins = sum(1 for trade in log if trade["result"] in ("TP1", "TP2"))
    breakevens = sum(1 for trade in log if trade["result"] == "BE")
    total = len(log)
    return f"{wins / total * 100:.1f}% g'alaba, {breakevens} ta breakeven, jami {total} ta savdo"

def fetch_ohlcv(timeframe, symbol=SYMBOL):
    interval_map = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "4h": "240", "1d": "D"}
    interval = interval_map.get(timeframe, timeframe)
    response = requests.get(
        "https://api.bybit.com/v5/market/kline",
        params={"category": "linear", "symbol": symbol, "interval": interval, "limit": LIMIT},
        headers=HEADERS,
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()
    rows = data.get("result", {}).get("list", [])
    if not rows: raise RuntimeError(f"Bybit'dan candle ma'lumoti kelmadi: {data}")
    rows.sort(key=lambda row: int(row[0]))
    return [[int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])] for row in rows]

def find_swings(candles, left=SWING_LEFT, right=SWING_RIGHT):
    highs, lows = [], []
    for index in range(left, len(candles) - right):
        window = candles[index - left : index + right + 1]
        if candles[index][H] == max(candle[H] for candle in window):
            highs.append((index, candles[index][H]))
        if candles[index][L] == min(candle[L] for candle in window):
            lows.append((index, candles[index][L]))
    return highs, lows

def confirmed_structure_bias(candles, left=SWING_LEFT, right=SWING_RIGHT):
    highs, lows = find_swings(candles, left, right)
    if not highs or not lows: return None
    last_high_index, last_high_price = highs[-1]
    last_low_index, last_low_price = lows[-1]
    start_index = max(last_high_index, last_low_index) + 1
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

def detect_liquidity_sweep(candles, left=SWING_LEFT, right=SWING_RIGHT):
    highs, lows = find_swings(candles, left, right)
    if not highs or not lows: return None
    last_high_price = highs[-1][1]
    last_low_price = lows[-1][1]
    last_candle = candles[-1]
    if last_candle[H] > last_high_price and last_candle[C] < last_high_price: return "bearish_sweep"
    if last_candle[L] < last_low_price and last_candle[C] > last_low_price: return "bullish_sweep"
    return None

def detect_fvg(candles):
    fvgs = []
    for index in range(2, len(candles)):
        first, third = candles[index - 2], candles[index]
        if third[L] > first[H]: fvgs.append({"type": "bullish", "kind": "fvg", "top": third[L], "bottom": first[H], "index": index})
        elif third[H] < first[L]: fvgs.append({"type": "bearish", "kind": "fvg", "top": first[L], "bottom": third[H], "index": index})
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

def calculate_rsi(candles, period=RSI_PERIOD):
    closes = [candle[C] for candle in candles]
    count = len(closes)
    if count < period + 1: return [None] * count
    rsis = [None] * period
    gains, losses = [], []
    for index in range(1, period + 1):
        difference = closes[index] - closes[index - 1]
        gains.append(max(difference, 0))
        losses.append(max(-difference, 0))
    average_gain = sum(gains) / period
    average_loss = sum(losses) / period
    rsis.append(100 if average_loss == 0 else 100 - (100 / (1 + average_gain / average_loss)))
    for index in range(period + 1, count):
        difference = closes[index] - closes[index - 1]
        gain = max(difference, 0)
        loss = max(-difference, 0)
        average_gain = (average_gain * (period - 1) + gain) / period
        average_loss = (average_loss * (period - 1) + loss) / period
        rsis.append(100 if average_loss == 0 else 100 - (100 / (1 + average_gain / average_loss)))
    return rsis

def calculate_atr(candles, period=ATR_PERIOD):
    if len(candles) < period + 1: return None
    true_ranges = []
    for index in range(1, len(candles)):
        high, low, previous_close = candles[index][H], candles[index][L], candles[index - 1][C]
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return sum(true_ranges[-period:]) / period

def calculate_vwap(candles, lookback=VWAP_LOOKBACK):
    cumulative_price_volume = 0
    cumulative_volume = 0
    for candle in candles[-lookback:]:
        typical_price = (candle[H] + candle[L] + candle[C]) / 3
        volume = candle[5] if len(candle) > 5 else 0
        cumulative_price_volume += typical_price * volume
        cumulative_volume += volume
    if cumulative_volume == 0: return None
    return cumulative_price_volume / cumulative_volume

def detect_rsi_divergence(candles, rsis, lookback=20):
    if len(candles) < lookback: return False, False
    window = candles[-lookback:]
    offset = len(candles) - lookback
    highs, lows = find_swings(window)
    bullish_divergence, bearish_divergence = False, False
    if len(lows) >= 2:
        first_index, first_price = lows[-2]
        second_index, second_price = lows[-1]
        first_rsi, second_rsi = rsis[offset + first_index], rsis[offset + second_index]
        if first_rsi is not None and second_rsi is not None and second_price < first_price and second_rsi > first_rsi:
            bullish_divergence = True
    if len(highs) >= 2:
        first_index, first_price = highs[-2]
        second_index, second_price = highs[-1]
        first_rsi, second_rsi = rsis[offset + first_index], rsis[offset + second_index]
        if first_rsi is not None and second_rsi is not None and second_price > first_price and second_rsi < first_rsi:
            bearish_divergence = True
    return bullish_divergence, bearish_divergence

def confirmation_check(bias, price, vwap, bullish_divergence, bearish_divergence, btc_bias=None, zone_confluence=False, sweep=None):
    confirmations = []
    if bias == "bullish":
        if bullish_divergence: confirmations.append("RSI divergence (bullish)")
        if vwap is not None and price > vwap: confirmations.append("Narx VWAP ustida")
        if btc_bias is not None and btc_bias != "bullish": confirmations.append("SMT: BTC Gold bilan bullish'da mos kelmadi")
        if sweep == "bullish_sweep": confirmations.append("Likvidlik yig'ildi (Bullish Sweep)")
    else:
        if bearish_divergence: confirmations.append("RSI divergence (bearish)")
        if vwap is not None and price < vwap: confirmations.append("Narx VWAP ostida")
        if btc_bias is not None and btc_bias != "bearish": confirmations.append("SMT: BTC Gold bilan bearish'da mos kelmadi")
        if sweep == "bearish_sweep": confirmations.append("Likvidlik yig'ildi (Bearish Sweep)")
    if zone_confluence: confirmations.append("Zone confluence (FVG + OB bir joyda)")
    return confirmations

def _zone_is_fresh(zone, candles, max_age_bars=ZONE_MAX_AGE_BARS):
    formation_index = zone["index"]
    last_index = len(candles) - 1
    if last_index - formation_index > max_age_bars: return False
    for index in range(formation_index + 1, len(candles)):
        close = candles[index][C]
        if zone["type"] == "bullish" and close < zone["bottom"]: return False
        if zone["type"] == "bearish" and close > zone["top"]: return False
    return True

def build_trade(candles, bias, price, atr=None):
    all_zones = [
        zone for zone in detect_fvg(candles) + detect_order_blocks(candles)
        if zone["type"] == bias and _zone_is_fresh(zone, candles)
    ]
    if not all_zones: return None
    all_zones.sort(key=lambda zone: abs(price - (zone["top"] + zone["bottom"]) / 2))
    zone = all_zones[0]

    lower, upper = min(zone["bottom"], zone["top"]), max(zone["bottom"], zone["top"])
    tolerance = price * (ZONE_MAX_DISTANCE_PCT / 100)
    if not (lower - tolerance <= price <= upper + tolerance):
        return None

    confluence = any(
        other is not zone and other["kind"] != zone["kind"] and
        other["bottom"] <= zone["top"] and other["top"] >= zone["bottom"]
        for other in all_zones
    )
    sl_buffer = atr * SL_BUFFER_ATR_MULT if atr else price * 0.001
    entry = price

    if bias == "bullish":
        stop_loss = zone["bottom"] - sl_buffer
        risk = entry - stop_loss
        if risk <= 0 or risk > entry * MAX_RISK_PCT: return None
        take_profit_1, take_profit_2 = entry + risk * RR1, entry + risk * RR2
        side = "LONG"
    else:
        stop_loss = zone["top"] + sl_buffer
        risk = stop_loss - entry
        if risk <= 0 or risk > entry * MAX_RISK_PCT: return None
        take_profit_1, take_profit_2 = entry - risk * RR1, entry - risk * RR2
        side = "SHORT"

    return {
        "signal": side,
        "bias": bias,
        "entry": round(entry, 2),
        "zone_bottom": round(lower, 2),  
        "zone_top": round(upper, 2),     
        "sl": round(stop_loss, 2),
        "tp1": round(take_profit_1, 2),
        "tp2": round(take_profit_2, 2),
        "confluence": confluence,
        "opened_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }

def calculate_position_size(entry, sl, balance, risk_pct):
    if not balance or balance <= 0: return None
    price_risk = abs(entry - sl)
    if price_risk <= 0: return None
    risk_usd_target = balance * (risk_pct / 100)
    raw_lot = risk_usd_target / (price_risk * XAUUSD_LOT_UNITS)
    lot = math.floor(raw_lot / MIN_LOT_STEP) * MIN_LOT_STEP
    lot = round(lot, 2)
    undersized = lot < MIN_LOT_STEP
    if undersized: lot = MIN_LOT_STEP
    risk_usd_actual = lot * price_risk * XAUUSD_LOT_UNITS
    risk_pct_actual = (risk_usd_actual / balance) * 100
    return {
        "lot": lot, "risk_usd": round(risk_usd_actual, 2),
        "risk_pct_actual": round(risk_pct_actual, 2), "undersized": undersized,
    }

def send_telegram(text, with_keyboard=False, chat_id=None):
    targets = [chat_id] if chat_id else list(SUBSCRIBERS)
    reply_markup = json.dumps({"keyboard": [["📊 Signal"]], "resize_keyboard": True}) if with_keyboard else None
    for target in targets:
        payload = {"chat_id": target, "text": text}
        if reply_markup: payload["reply_markup"] = reply_markup
        try:
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=payload, timeout=15)
        except Exception as e:
            logger.error(f"Telegram'ga xabar yuborishda xato ({target}): {e}")

def _trade_age_text(opened_at_iso):
    if not opened_at_iso: return ""
    try:
        opened_at = datetime.datetime.fromisoformat(opened_at_iso)
        elapsed = datetime.datetime.now(datetime.timezone.utc) - opened_at
        hours = elapsed.total_seconds() / 3600
        return f" (ochilganiga {hours:.1f} soat bo'ldi)"
    except Exception:
        return ""

def build_signal_status_text():
    if last_status["price"] is None: return "Bot hali birinchi tekshiruvni bajarmadi, biroz kuting."
    lines = [
        f"Holat: {'⏸ PAUZADA' if paused else '▶️ Ishlamoqda'}",
        f"Oxirgi tekshiruv: {last_status['checked_at']}",
        f"Narx: {round(last_status['price'], 2)}",
        f"5m bias: {last_status['bias5'] or 'aniqlanmadi'}",
        f"15m bias: {last_status['bias15'] or 'aniqlanmadi'}",
        f"1h bias: {last_status.get('bias1h') or 'aniqlanmadi'}",
        f"4h bias: {last_status.get('bias4h') or 'aniqlanmadi'}",
        f"1D bias: {last_status.get('bias1d') or 'aniqlanmadi'}",
    ]
    if current_trade:
        age_text = _trade_age_text(current_trade.get("opened_at"))
        lines.extend([
            "", f"OCHIQ BITIM: {current_trade['signal']}{age_text}",
            f"Entry: {current_trade['entry']} | ZONA: {current_trade.get('zone_bottom')} - {current_trade.get('zone_top')}",
            f"SL: {current_trade['sl']} | TP1: {current_trade['tp1']} | TP2: {current_trade['tp2']}"
        ])
        with state_lock:
            balance, risk_pct = ACCOUNT.get("balance"), ACCOUNT.get("risk_pct", DEFAULT_RISK_PER_TRADE_PCT)
        if balance:
            sizing = calculate_position_size(current_trade["entry"], current_trade["sl"], balance, risk_pct)
            if sizing: lines.append(f"Tavsiya etilgan lot: {sizing['lot']} (~{sizing['risk_usd']}$ risk)")
    else:
        lines.extend(["", "Hozir ochiq bitim yo'q - bot signal kutmoqda."])
    with state_lock:
        balance, risk_pct = ACCOUNT.get("balance"), ACCOUNT.get("risk_pct", DEFAULT_RISK_PER_TRADE_PCT)
    if balance: lines.append(f"\nHisob: {balance:.2f}$ | Risk/savdo: {risk_pct}%")
    else: lines.append("\nLot hajmini avtomatik hisoblash uchun /balance <miqdor> kiriting.")
    lines.append(f"Statistika: {win_rate_text(load_log())}")
    return "\n".join(lines)

def register_bot_commands():
    commands = [
        {"command": "start", "description": "Obuna bo'lish / holatni ko'rish"},
        {"command": "stop", "description": "Obunani bekor qilish"},
        {"command": "signal", "description": "Joriy holatni ko'rish"},
        {"command": "pause", "description": "Botni pauzaga qo'yish (admin)"},
        {"command": "resume", "description": "Botni davom ettirish (admin)"},
        {"command": "close", "description": "Ochiq bitimni yopish (admin)"},
        {"command": "balance", "description": "Hisob balansini kiritish (admin)"},
        {"command": "risk", "description": "Savdodagi risk foizini belgilash (admin)"},
    ]
    try:
        response = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/setMyCommands", json={"commands": commands}, timeout=15)
        if response.status_code == 200 and response.json().get("ok"): logger.info("Telegram buyruqlar menyusi o'rnatildi.")
    except Exception as error:
        logger.error(f"Buyruqlar menyusini o'rnatishda xato: {error}")

def telegram_listener():
    global paused
    offset = None
    backoff = 5
    max_backoff = 60
    try:
        response = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates", params={"timeout": 1}, timeout=10)
        if response.status_code == 200:
            results = response.json().get("result", [])
            if results: offset = results[-1]["update_id"] + 1
    except Exception: pass
    while True:
        try:
            params = {"timeout": 25}
            if offset is not None: params["offset"] = offset
            response = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates", params=params, timeout=30)
            if response.status_code != 200:
                time.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
                continue
            backoff = 5  
            for update in response.json().get("result", []):
                offset = update["update_id"] + 1
                try:
                    message = update.get("message", {})
                    text = (message.get("text") or "").strip()
                    chat_id = str(message.get("chat", {}).get("id", ""))
                    if not chat_id: continue
                    is_admin = chat_id == ADMIN_CHAT_ID
                    if text == "/start":
                        with state_lock:
                            is_new = chat_id not in SUBSCRIBERS
                            SUBSCRIBERS.add(chat_id)
                            save_subscribers()
                        if is_new:
                            send_telegram("✅ Obuna bo'ldingiz! /signal yoki /balance buyruqlaridan foydalaning.", with_keyboard=True, chat_id=chat_id)
                        else: send_telegram(build_signal_status_text(), with_keyboard=True, chat_id=chat_id)
                    elif text == "/stop":
                        with state_lock:
                            was_subscribed = chat_id in SUBSCRIBERS
                            SUBSCRIBERS.discard(chat_id)
                            save_subscribers()
                        if was_subscribed: send_telegram("🔕 Obuna bekor qilindi.", chat_id=chat_id)
                    elif text in ("/signal", "📊 Signal"): send_telegram(build_signal_status_text(), with_keyboard=True, chat_id=chat_id)
                    elif text == "/pause" and is_admin:
                        paused = True
                        send_telegram("⏸ Bot pauzaga qo'yildi.")
                    elif text == "/resume" and is_admin:
                        paused = False
                        send_telegram("▶️ Bot davom ettirildi.")
                    elif text == "/close" and is_admin:
                        with state_lock:
                            if current_trade:
                                price = last_status.get("price")
                                if price:
                                    close_trade("MANUAL", price)
                                    send_telegram("🛑 Bitim qo'lda yopildi.")
                    elif text.startswith("/balance") and is_admin:
                        try:
                            new_balance = float(text.split()[1].replace(",", "."))
                            with state_lock:
                                ACCOUNT["balance"] = new_balance
                                save_account()
                            send_telegram(f"✅ Hisob balansi {new_balance:.2f}$ deb saqlandi.")
                        except: send_telegram("Format: /balance 500", chat_id=chat_id)
                    elif text.startswith("/risk") and is_admin:
                        try:
                            new_risk = float(text.split()[1].replace(",", "."))
                            with state_lock:
                                ACCOUNT["risk_pct"] = new_risk
                                save_account()
                            send_telegram(f"✅ Risk {new_risk}% qilib saqlandi.")
                        except: send_telegram("Format: /risk 1", chat_id=chat_id)
                except: pass
        except:
            time.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)

def start_post_trade_tracking(side, close_price):
    global post_trade
    post_trade = {"side": side, "close_price": close_price, "extreme": close_price, "checks": 0}

def update_post_trade(price):
    global post_trade
    if post_trade is None: return
    if post_trade["side"] == "LONG": post_trade["extreme"] = max(post_trade["extreme"], price)
    else: post_trade["extreme"] = min(post_trade["extreme"], price)
    post_trade["checks"] += 1
    if post_trade["checks"] >= POST_TRADE_CHECKS:
        moved = abs(post_trade["extreme"] - post_trade["close_price"])
        if moved >= POST_TRADE_MIN_CONTINUATION:
            send_telegram(f"Ma'lumot: oldingi {post_trade['side']} bitim yopilgandan keyin narx yana {round(moved, 2)}$ davom etdi.")
        post_trade = None

def close_trade(result, price):
    global current_trade, warned_flip
    with state_lock:
        trade = current_trade
        if trade is None: return
        log = load_log()
        log.append({
            "id": len(log) + 1, "symbol": SYMBOL, "signal": trade["signal"],
            "entry": trade["entry"], "result": result, "close_price": price,
            "closed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        })
        save_log(log)
        emoji = {"TP2": "✅", "SL": "❌", "BE": "➖", "MANUAL": "🛑"}.get(result, "ℹ️")
        send_telegram(f"GOLD {trade['signal']} bitim yopildi {emoji} - {result} (narx {price})\nStatistika: {win_rate_text(log)}")
        start_post_trade_tracking(trade["signal"], price)
        current_trade = None
        warned_flip = False

def monitor_open_trade(price, bias15):
    global current_trade, warned_flip
    with state_lock:
        trade = current_trade
        if trade is None: return
        side = trade["signal"]
        sl_hit = price <= trade["sl"] if side == "LONG" else price >= trade["sl"]
        tp2_hit = price >= trade["tp2"] if side == "LONG" else price <= trade["tp2"]
        tp1_hit = price >= trade["tp1"] if side == "LONG" else price <= trade["tp1"]
        if sl_hit:
            close_trade("BE" if trade.get("breakeven") else "SL", price)
            return
        if tp2_hit:
            close_trade("TP2", price)
            return
        if tp1_hit and not trade.get("tp1_notified"):
            trade["tp1_notified"] = True
            trade["breakeven"] = True
            trade["sl"] = trade["entry"]
            send_telegram(f"GOLD {side} - TP1 oldi ✅ (narx {price})\nSL breakeven'ga ko'chirildi.")
        if bias15 is not None and bias15 != trade["bias"] and not warned_flip:
            send_telegram(f"DIQQAT: Bozor struktura {bias15}ga o'zgardi. Narx: {price} - Yopishni ko'rib chiqing.")
            warned_flip = True

def fetch_high_impact_news():
    now = datetime.datetime.now(datetime.timezone.utc)
    cached, fetched_at = _news_cache.get("events"), _news_cache.get("fetched_at")
    if cached is not None and fetched_at is not None and (now - fetched_at).total_seconds() < NEWS_CACHE_TTL_SEC: return cached
    events = []
    try:
        response = requests.get(NEWS_CALENDAR_URL, headers=HEADERS, timeout=10)
        response.raise_for_status()
        for item in response.json():
            if item.get("country") != "USD" or item.get("impact") != "High": continue
            event_time = datetime.datetime.fromisoformat(item["date"].replace("Z", "+00:00"))
            events.append({"title": item.get("title", "?"), "time": event_time})
    except: return cached or []
    _news_cache["events"] = events
    _news_cache["fetched_at"] = now
    return events

def is_news_blackout():
    if not NEWS_FILTER_ENABLED: return False, None
    now = datetime.datetime.now(datetime.timezone.utc)
    for event in fetch_high_impact_news():
        delta_minutes = (event["time"] - now).total_seconds() / 60
        if -NEWS_BLOCK_MINUTES_AFTER <= delta_minutes <= NEWS_BLOCK_MINUTES_BEFORE: return True, event["title"]
    return False, None

def run():
    global current_trade, last_impulse_ts, last_impulse_info
    try:
        candles15_raw = fetch_ohlcv("15m")
        candles5_raw = fetch_ohlcv("5m")
    except Exception as e: return
    closed15 = closed_only(candles15_raw)
    closed5 = closed_only(candles5_raw)
    price = candles15_raw[-1][C] + PRICE_OFFSET
    bias15 = confirmed_structure_bias(closed15)
    bias5 = confirmed_structure_bias(closed5)
    atr15 = calculate_atr(closed15)
    bias1h, bias4h, bias1d = None, None, None
    try: bias1h = confirmed_structure_bias(closed_only(fetch_ohlcv("1h")))
    except: pass
    try: bias4h = confirmed_structure_bias(closed_only(fetch_ohlcv("4h")))
    except: pass
    try: bias1d = confirmed_structure_bias(closed_only(fetch_ohlcv("1d")))
    except: pass

    last_status.update({
        "price": price, "bias5": bias5, "bias15": bias15,
        "bias1h": bias1h, "bias4h": bias4h, "bias1d": bias1d,
        "checked_at": time.strftime("%H:%M:%S"),
    })
    save_status(round(price, 2), bias5, bias15, bias1h, bias4h, bias1d)
    with state_lock: PRICE_HISTORY.append(round(price, 2))

    impulse = detect_impulse(closed5)
    if impulse and impulse["ts"] != last_impulse_ts:
        last_impulse_ts = impulse["ts"]
        with state_lock:
            last_impulse_info = dict(impulse)
            last_impulse_info["detected_at"] = time.strftime("%H:%M:%S")
        send_telegram(f"⚡ IMPULSIV HARAKAT (5m): {impulse['range']}$ siljidi. Ehtiyot bo'ling.")

    with state_lock:
        if current_trade:
            monitor_open_trade(price, bias15)
            save_status(round(price, 2), bias5, bias15, bias1h, bias4h, bias1d)
            return
        update_post_trade(price)
        if paused or (KILLZONES_ENABLED and not in_killzone()): return
        blackout, event_title = is_news_blackout()
        if blackout: return
        if bias5 is None or bias15 is None or bias5 != bias15: return
        if HTF_FILTER_ENABLED and bias1h is not None and bias1h != bias15: return
        if STRONG_HTF_FILTER_ENABLED and bias4h is not None and bias4h != bias15: return
        if DAILY_HTF_FILTER_ENABLED and bias1d is not None and bias1d != bias15: return
        trade = build_trade(closed15, bias15, price, atr15)

    if trade is None: return

    risk = abs(trade["entry"] - trade["sl"])
    if atr15 and risk < atr15 * ATR_MIN_RISK_MULT: return
    rsis15 = calculate_rsi(closed15)
    vwap15 = calculate_vwap(closed15)
    bullish_divergence, bearish_divergence = detect_rsi_divergence(closed15, rsis15)
    sweep15 = detect_liquidity_sweep(closed15)
    btc_bias15 = None
    try: btc_bias15 = confirmed_structure_bias(closed_only(fetch_ohlcv("15m", symbol=BTC_SYMBOL)))
    except: pass
    confirmations = confirmation_check(bias15, price, vwap15, bullish_divergence, bearish_divergence, btc_bias15, trade.get("confluence", False), sweep=sweep15)
    if len(confirmations) < MIN_CONFIRMATIONS: return

    log = load_log()
    confirmation_text = ", ".join(confirmations) if confirmations else "-"
    confluence_text = "ha (FVG+OB)" if trade.get("confluence") else "yo'q"

    with state_lock:
        balance, risk_pct = ACCOUNT.get("balance"), ACCOUNT.get("risk_pct", DEFAULT_RISK_PER_TRADE_PCT)
    if balance:
        sizing = calculate_position_size(trade["entry"], trade["sl"], balance, risk_pct)
        if sizing is None: position_line = "\nLot: hisoblab bo'lmadi."
        elif sizing["undersized"]: position_line = f"\n⚠️ Lot: {sizing['lot']} (minimal) - xavfli risk ~{sizing['risk_pct_actual']}%! Ehtiyot bo'ling."
        else: position_line = f"\nTavsiya etilgan lot: {sizing['lot']} (~{sizing['risk_usd']}$ risk)"
    else: position_line = "\nLot tavsiyasi uchun /balance kiriting."

    logger.info("Signal tasdiqlandi. Gemini fikri olinmoqda...")
    ai_xulosa = ask_gemini_analysis(SYMBOL, trade['signal'], trade['entry'], trade['sl'])

    message = (
        f"GOLD {SYMBOL} - {trade['signal']}\n"
        f"5m/15m bias: {bias15} | 1h: {bias1h or 'n/a'} | 4h: {bias4h or 'n/a'} | 1D: {bias1d or 'n/a'}\n"
        f"Narx: {round(price, 2)}\n"
        f"🎯 Entry (Joriy): {trade['entry']}\n"
        f"📌 KIRISH ZONASI: {trade['zone_bottom']} - {trade['zone_top']}\n"
        f"🛡 SL: {trade['sl']}\n"
        f"✅ TP1: {trade['tp1']} | TP2: {trade['tp2']}\n"
        f"ATR(15m): {round(atr15, 2) if atr15 else 'n/a'}\n"
        f"BTC bias (15m): {btc_bias15 or 'n/a'}\n"
        f"Zone confluence: {confluence_text}\n"
        f"Tasdiqlash: {confirmation_text}"
        f"{position_line}\n\n"
        f"🤖 **Gemini AI Xulosasi:**\n_{ai_xulosa}_\n\n"
        f"Win rate: {win_rate_text(log)}"
    )
    logger.info(message.replace("\n", " | "))
    send_telegram(message)
    with state_lock:
        current_trade = trade
        save_status(round(price, 2), bias5, bias15, bias1h, bias4h, bias1d)

app = Flask(__name__)
# HTML CSS QISMLARINI MUHIM BO'LMAGANI UCHUN VAZIFANI YENGILLASHTIRISH MAQSADIDA QISQARTIRILDI
@app.route("/")
def home():
    return "SMC + AI BOT 24/7 ISHLAMOQDA!"

def loop():
    logger.info(f"Aqlli bot ishga tushdi - har {CHECK_INTERVAL_SEC}s tekshiradi")
    while True:
        try: run()
        except Exception as error: logger.error(f"Xatolik: {error}")
        time.sleep(CHECK_INTERVAL_SEC)

def restore_state():
    global current_trade
    if not os.path.exists(STATUS_FILE): return
    try:
        with open(STATUS_FILE, encoding='utf-8') as file: status = json.load(file)
        saved_trade = status.get("currentTrade")
        if saved_trade:
            with state_lock: current_trade = saved_trade
    except: pass

if __name__ == "__main__":
    restore_state()
    threading.Thread(target=loop, daemon=True).start()
    register_bot_commands()
    threading.Thread(target=telegram_listener, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8100)))