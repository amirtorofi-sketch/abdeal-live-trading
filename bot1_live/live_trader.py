"""
بات شماره ۱ — دو استراتژی (Supertrend+ADX + ICT/SMC Scalp Pro)، هر دو معکوس،
روی تمام ارزهایی که همین الان روی تبدیل بازار تومانیِ مارجین‌دار دارند
(نه یک لیست ثابت و دستی) — دقیقاً همون چیزی که ربات Paper Trading اول
(Dasttrade) انجام می‌ده: هر دو استراتژی به‌طور مستقل روی هر نماد اجرا می‌شن
و می‌تونن هم‌زمان پوزیشن جدا داشته باشن (کلید پوزیشن = نماد + نام استراتژی).

نماد اینجا دست‌نخورده مونده (کشف پویای کل بازار)؛ فقط برای مقایسه‌ی
کنترل‌شده‌تر با بات ۲، حجم/مارجین هر استراتژی با بات ۲ یکی شده (به درخواست
کاربر) — نماد و جهت (معکوس/غیرمعکوس) دست‌نخورده باقی موندن.

سیگنال از داده‌ی دلاری بایننس گرفته می‌شود؛ قیمت اجرا و بستن پوزیشن از
قیمت لحظه‌ای *واقعی* تبدیل (Depth) خوانده می‌شود.

دو حالت اجرا (با Variable گیت‌هابی TABDEAL_DRY_RUN کنترل می‌شود):
    TABDEAL_DRY_RUN=true  -> حالت آزمایشی/فرضی (Paper): هیچ سفارش واقعی ثبت
                             نمی‌شود، ولی قیمت ورود/خروج از قیمت واقعی لحظه‌ای
                             تبدیل خوانده می‌شود و در common/paper_ledger.py
                             (فایل bot1_live/paper_trades_log.csv) سود/زیان
                             فرضی و موجودی فرضی ثبت می‌شود.
    TABDEAL_DRY_RUN=false -> حالت زنده: سفارش واقعی با پول واقعی ثبت می‌شود.

نمادهای فعال هر بار اجرا با discover_all_irt_margin_bases به‌صورت زنده از
تبدیل خوانده می‌شوند (نه یک لیست ثابت) — یعنی خودکار هر ارزی که الان
بازار تومانی مارجین‌دار دارد وارد می‌شود، حتی اگر بعداً به این فهرست اضافه شود.

جهت هر دو استراتژی دقیقاً طبق یافته‌ی بک‌تست پروژه معکوس اجرا می‌شود:
    سیگنال خام خرید -> پوزیشن Short باز می‌شود
    سیگنال خام فروش -> پوزیشن Long باز می‌شود
"""

import os
import sys
import json
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from signal_bot import (  # noqa: E402  (وارد کردن بدون تغییر از استراتژی اصلی)
    get_klines, check_strategy_supertrend, check_strategy_smc, get_htf_bias,
    TIMEFRAME, KLINES_LIMIT, ST_TP1_RR, ST_TP2_RR, SL_ATR_MULT, TP1_RR, TP2_RR,
)
from common.tabdeal_broker import (  # noqa: E402
    open_margin_position, close_margin_position, get_public_client, get_mid_price,
    discover_all_irt_margin_bases, discover_irt_margin_symbols, extract_real_price,
    get_market_info, split_into_two_lots, currency_label, BrokerError, DRY_RUN, _float_env,
    get_price_range_since, now_ms, HARD_CAP_MARGIN_IRT,
)

# اگه پوزیشنی از نسخه‌ی قبلی (بدون last_checked_ms) باقی مونده باشه، برای
# اولین چک این‌قدر عقب‌تر می‌ریم تا بازه‌ی معقولی از معاملات اخیر رو ببینیم.
FALLBACK_LOOKBACK_MS = 30 * 60 * 1000
from common.telegram_notify import send_telegram  # noqa: E402
from common import paper_ledger  # noqa: E402

# --- هم‌سان‌سازی حجم با بات ۲ برای مقایسه‌ی کنترل‌شده‌تر (به درخواست کاربر) ---
# نماد دست‌نخورده مونده (همچنان کل بازار تومانی مارجین‌دار، مثل قبل)، فقط
# مارجین/اهرم هر استراتژی با بات ۲ یکی شده (۱۰۰هزار × ۲x برای هر دو استراتژی)
# تا حجم پوزیشن یه متغیر کنترل‌نشده‌ی دیگه نباشه. قبلاً Supertrend سه برابر
# ICT/SMC حجم داشت که با توجه به نرخ برد پایینش ریسک رو غیرمنطقی بزرگ می‌کرد.
SOURCE_ST = "Supertrend+ADX"
SOURCE_SMC = "ICT/SMC Scalp Pro"
SOURCE_CONFIG = {
    SOURCE_ST: {
        "margin_irt": _float_env("BOT1_MARGIN_IRT", "100000"),
        "leverage": _float_env("BOT1_LEVERAGE", "2"),
    },
    SOURCE_SMC: {
        "margin_irt": _float_env("BOT1_SMC_MARGIN_IRT", "100000"),
        "leverage": _float_env("BOT1_SMC_LEVERAGE", "2"),
    },
}
PAPER_STARTING_BALANCE_IRT = _float_env("PAPER_STARTING_BALANCE_IRT", "5000000")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
PAPER_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper_trades_log.csv")


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def next_trade_id(state: dict) -> int:
    """شماره‌ی پیوسته برای هر معامله‌ی جدید - تا بشه توی تلگرام باز/بسته‌شدن هر معامله رو با هم جفت کرد."""
    n = int(state.get("_trade_counter", 0)) + 1
    state["_trade_counter"] = n
    return n


def notify(text: str):
    print(text)
    send_telegram(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, text)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


DIRECTION_FA = {"long": "خرید (Long)", "short": "فروش (Short)"}
DIRECTION_EMOJI = {"long": "🟢", "short": "🔴"}


def format_candle_time(candle_time) -> str:
    """زمان کندل را به فرمت HH:MM:SS DD-MM-YYYY برمی‌گرداند (برای نمایش در تلگرام)."""
    try:
        return candle_time.strftime("%H:%M:%S %d-%m-%Y")
    except Exception:
        return str(candle_time)


def open_positions_totals(state: dict):
    """
    جمع مارجین و ارزش اسمی *واقعاً درگیر* در پوزیشن‌های باز فعلی — بر مبنای
    سهمِ لات‌هایی که هنوز واقعاً بازن (نه کل پوزیشن)، چون وقتی لات a با TP1
    بسته می‌شه، نیمی از مارجین آزاد می‌شه ولی رکورد پوزیشن هنوز توی state
    هست (تا لات b هم بسته بشه). دقیقاً معادل open_margin_sum در
    trading_bot.py قدیمی.
    """
    total_margin = 0.0
    total_notional = 0.0
    for key, val in state.items():
        if isinstance(val, dict) and "margin_irt" in val and "lot_a" in val and "lot_b" in val:
            open_fraction = 0.0
            if val["lot_a"]["status"] == "open":
                open_fraction += 0.5
            if val["lot_b"]["status"] == "open":
                open_fraction += 0.5
            total_margin += (val.get("margin_irt", 0.0) or 0.0) * open_fraction
            total_notional += (val.get("notional_irt", 0.0) or 0.0) * open_fraction
    return total_margin, total_notional


def resolve_direction_and_risk_pct(raw_direction: str, entry: float, raw_sl: float):
    """
    دقیقاً همون منطق trading_bot.py برای معکوس‌کردن جهت. ولی برخلاف نسخه‌ی
    قبلی، دیگه سطوح مطلق SL/TP رو اینجا حساب نمی‌کنیم - چون entry/raw_sl روی
    مقیاس قیمت دلاری بایننسه، در حالی که پوزیشن واقعی روی قیمت تومانی تبدیل
    باز می‌شه (دو مقیاس کاملاً متفاوت؛ همون مشکلی که باعث شد TP1/TP2 فوراً و
    اشتباه فایر بشن). به‌جاش فقط درصد فاصله‌ی SL رو برمی‌گردونیم؛ سطوح واقعی
    بعد از گرفتن real_price (قیمت واقعی تبدیل) با همین درصد ساخته می‌شن.
    """
    risk_pct = (abs(entry - raw_sl) / entry) if entry else 0.0
    direction = "short" if raw_direction == "long" else "long"
    return direction, risk_pct


def _close_lot(state, spot_symbol, pos, lot_key, price, reason, source_label):
    """
    یک لات (a یا b) را می‌بندد - چه در حالت آزمایشی (فقط شبیه‌سازی) چه در
    حالت زنده (سفارش واقعی معکوس). در صورت خطای واقعی، لات را «باز» نگه
    می‌دارد تا دور بعد دوباره تلاش شود، و False برمی‌گرداند.

    spot_symbol فقط برای نمایش/لاگ استفاده می‌شود؛ سفارش واقعی با
    pos["margin_symbol"] (فرمت زیرخط‌دار) ثبت می‌شود.
    """
    lot = pos[lot_key]
    opposite = "SELL" if pos["direction"] == "long" else "BUY"
    margin_symbol = pos["margin_symbol"]

    cur = currency_label(spot_symbol)
    price_pct = (price - pos["entry"]) / pos["entry"] * 100 if pos["entry"] else 0.0
    if pos["direction"] == "short":
        price_pct = -price_pct
    pct_sign = "+" if price_pct >= 0 else ""

    if DRY_RUN:
        balance = state.get("_paper_balance_irt", PAPER_STARTING_BALANCE_IRT)
        pnl = paper_ledger.record_close(
            PAPER_LOG_FILE, now_iso(), spot_symbol, source_label, pos["direction"],
            pos["entry"], price, lot["qty"], balance, reason,
        )
        new_balance = balance + pnl
        state["_paper_balance_irt"] = new_balance
        pos["realized_pnl"] = pos.get("realized_pnl", 0.0) + pnl
        pnl_sign = "+" if pnl >= 0 else ""
        notify(
            f"⚪ [آزمایشی] معامله #{pos.get('trade_id','؟')} — بسته شدن {lot_key} | {spot_symbol} ({source_label}, {pos['direction']}) — دلیل: {reason}\n"
            f"قیمت خروج≈{price:,.0f} {cur} | تغییر قیمت: {pct_sign}{price_pct:.2f}٪\n"
            f"سود/زیان این لات: {pnl_sign}{pnl:,.0f} {cur} | موجودی فعلی: {new_balance:,.0f} {cur}"
        )
    else:
        try:
            close_margin_position(margin_symbol, opposite, lot["qty"], logger=print)
        except BrokerError as e:
            notify(f"❌ خطا در بستن {lot_key} پوزیشن واقعی {spot_symbol} ({source_label}): {e}")
            return False
        notify(
            f"⚪ معامله #{pos.get('trade_id','؟')} — بسته شدن {lot_key} واقعی | {spot_symbol} ({source_label}, {pos['direction']}) — دلیل: {reason}\n"
            f"قیمت خروج≈{price:,.0f} {cur} | تغییر قیمت: {pct_sign}{price_pct:.2f}٪ (سود/زیان دقیق تومانی رو از پنل تبدیل چک کن)"
        )

    lot["status"] = "closed"
    return True


def manage_open_position(state: dict, position_key: str, spot_symbol: str):
    """
    معماری دو-لاتی - دقیقاً مطابق trading_bot.py اصلی:
    لات a هدفش TP1 است؛ وقتی TP1 خورد، SL برای لات b (باقی‌مانده) به نقطه‌ی
    ورود (Breakeven) منتقل می‌شود. لات b هدفش TP2 است. اگر هر دو لات بسته
    شدند، پوزیشن از state حذف می‌شود.

    ⚠️ به‌جای چک قیمت لحظه‌ای تک‌نقطه‌ای، بازه‌ی کامل نوسان قیمت (Low/High)
    از آخرین باری که این پوزیشن چک شده تا الان بررسی می‌شود (با استفاده از
    آخرین معاملات عمومی تبدیل) - دقیقاً مثل چک High/Low کندل در
    trading_bot.py قدیمی، ولی روی داده‌ی واقعی تبدیل نه بایننس. این یعنی
    اگه قیمت توی این فاصله فقط یه لحظه SL/TP رو لمس کرده باشه هم دیده می‌شه،
    نه فقط اگه دقیقاً لحظه‌ی اجرای کرون آنجا بوده باشه.
    """
    pos = state.get(position_key)
    if not pos:
        return
    spot_client = get_public_client()
    mid_price = get_mid_price(spot_client, spot_symbol)

    since_ms = int(pos.get("last_checked_ms") or 0) or (now_ms() - FALLBACK_LOOKBACK_MS)
    window_low, window_high = get_price_range_since(spot_client, spot_symbol, since_ms, fallback_mid=mid_price)
    if window_low is None:
        window_low = window_high = mid_price
    pos["last_checked_ms"] = now_ms()

    is_long = pos["direction"] == "long"
    source_label = pos["source_label"]

    lot_a = pos["lot_a"]
    if lot_a["status"] == "open":
        hit_sl = (window_low <= pos["sl"]) if is_long else (window_high >= pos["sl"])
        hit_tp1 = (window_high >= pos["tp1"]) if is_long else (window_low <= pos["tp1"])
        if hit_sl or hit_tp1:
            reason = "SL" if hit_sl else "TP1"
            exit_price = pos["sl"] if hit_sl else pos["tp1"]
            ok = _close_lot(state, spot_symbol, pos, "lot_a", exit_price, reason, source_label)
            if ok and reason == "TP1" and pos["lot_b"]["status"] == "open":
                pos["sl"] = pos["entry"]
                notify(f"🔵 معامله #{pos.get('trade_id','؟')} — SL لات باقی‌مانده‌ی {spot_symbol} ({source_label}) به نقطه‌ی ورود (Breakeven={pos['entry']:,.0f}) منتقل شد.")

    lot_b = pos["lot_b"]
    if lot_b["status"] == "open":
        hit_sl = (window_low <= pos["sl"]) if is_long else (window_high >= pos["sl"])
        hit_tp2 = (window_high >= pos["tp2"]) if is_long else (window_low <= pos["tp2"])
        if hit_sl or hit_tp2:
            reason = "SL" if hit_sl else "TP2"
            exit_price = pos["sl"] if hit_sl else pos["tp2"]
            _close_lot(state, spot_symbol, pos, "lot_b", exit_price, reason, source_label)

    if pos["lot_a"]["status"] == "closed" and pos["lot_b"]["status"] == "closed":
        cur = currency_label(spot_symbol)
        if DRY_RUN:
            total_pnl = pos.get("realized_pnl", 0.0)
            balance = state.get("_paper_balance_irt", PAPER_STARTING_BALANCE_IRT)
            margin_used = pos.get("margin_irt") or None
            roi_txt = f" ({'+' if total_pnl >= 0 else ''}{total_pnl / margin_used * 100:.1f}٪ نسبت به مارجین این معامله)" if margin_used else ""
            sign = "+" if total_pnl >= 0 else ""
            notify(
                f"🏁 معامله #{pos.get('trade_id','؟')} کاملاً بسته شد | {spot_symbol} ({source_label})\n"
                f"مجموع سود/زیان این معامله: {sign}{total_pnl:,.0f} {cur}{roi_txt}\n"
                f"موجودی نهایی بعد از این معامله: {balance:,.0f} {cur}"
            )
        else:
            notify(f"🏁 معامله #{pos.get('trade_id','؟')} کاملاً بسته شد | {spot_symbol} ({source_label}) — سود/زیان و موجودی دقیق رو از پنل تبدیل چک کن.")
        del state[position_key]


def try_open_position(state, spot_client, spot_symbol, margin_symbol, position_key, signal_key,
                       raw_direction, entry_price, raw_sl, candle_time, rr1, rr2, source_label, extra_label=""):
    """
    منطق مشترک باز کردن پوزیشن (دو-لاتی) برای هر دو استراتژی - تا کد برای
    Supertrend و SMC دوباره‌نویسی نشود. مارجین/اهرم از SOURCE_CONFIG بر اساس
    source_label خوانده می‌شود (هر استراتژی مقدار خودش را دارد).
    """
    if state.get(signal_key) == str(candle_time):
        return

    direction, risk_pct = resolve_direction_and_risk_pct(raw_direction, entry_price, raw_sl)
    side = "BUY" if direction == "long" else "SELL"
    cfg = SOURCE_CONFIG[source_label]

    # --- چک سرمایه‌ی آزاد - دقیقاً معادل open_margin_sum/free_margin در
    # trading_bot.py قدیمی: مجموع مارجین درگیر در همه‌ی پوزیشن‌های باز
    # (این بات) نباید از موجودی فرضی بیشتر بشه. margin_needed پیش از گرفتن
    # real_price هم معلومه چون مارجین یه مقدار ثابت پیکربندی‌شده‌ست، نه
    # وابسته به قیمت لحظه‌ای. ---
    margin_needed = min(cfg["margin_irt"], HARD_CAP_MARGIN_IRT)
    balance = state.get("_paper_balance_irt", PAPER_STARTING_BALANCE_IRT)
    used_margin, _ = open_positions_totals(state)
    free_margin = balance - used_margin
    if margin_needed > free_margin:
        cur = currency_label(spot_symbol)
        notify(
            f"⛔ سیگنال {direction} روی {spot_symbol} ({source_label}) رد شد: سرمایه‌ی آزاد کافی نیست.\n"
            f"مارجین موردنیاز: {margin_needed:,.0f} {cur} | مارجین آزاد: {free_margin:,.0f} {cur}\n"
            f"(موجودی نقدی: {balance:,.0f} {cur} | مارجین درگیر: {used_margin:,.0f} {cur})"
        )
        state[signal_key] = str(candle_time)
        save_state(state)
        return

    try:
        order = open_margin_position(spot_symbol, margin_symbol, side, cfg["margin_irt"], cfg["leverage"], logger=print)
    except BrokerError as e:
        notify(f"❌ سیگنال {direction} روی {spot_symbol} ({source_label}) رد شد: {e}")
        state[signal_key] = str(candle_time)
        save_state(state)
        return

    real_price = extract_real_price(order, fallback_price=entry_price)
    qty = float(order.get("origQty") or order.get("quantity"))
    notional_irt = float(order.get("_notional_irt", order.get("notional_irt", real_price * qty)))

    # سطوح SL/TP واقعی را روی مقیاس قیمت *واقعی تبدیل* می‌سازیم (نه مقیاس
    # دلاری بایننس) - همون درصد ریسکی که از سیگنال دلاری محاسبه شد، اینجا
    # روی real_price اعمال می‌شود.
    if direction == "long":
        sl = real_price * (1 - risk_pct)
        tp1 = real_price * (1 + risk_pct * rr1)
        tp2 = real_price * (1 + risk_pct * rr2)
    else:
        sl = real_price * (1 + risk_pct)
        tp1 = real_price * (1 - risk_pct * rr1)
        tp2 = real_price * (1 - risk_pct * rr2)

    try:
        market = get_market_info(spot_client, spot_symbol)
        qty_a, qty_b = split_into_two_lots(market, qty)
    except Exception:
        qty_a, qty_b = qty, 0.0

    trade_id = next_trade_id(state)
    base_fields = {
        "direction": direction, "entry": real_price, "sl": sl, "tp1": tp1, "tp2": tp2,
        "source_label": source_label, "opened_at": str(candle_time), "margin_symbol": margin_symbol,
        "trade_id": trade_id, "margin_irt": cfg["margin_irt"], "notional_irt": notional_irt,
        "last_checked_ms": now_ms(),
    }
    if qty_b <= 0:
        state[position_key] = {**base_fields, "lot_a": {"qty": qty_a, "status": "closed"}, "lot_b": {"qty": qty_a, "status": "open"}}
    else:
        state[position_key] = {**base_fields, "lot_a": {"qty": qty_a, "status": "open"}, "lot_b": {"qty": qty_b, "status": "open"}}
    state[signal_key] = str(candle_time)

    cur = currency_label(spot_symbol)
    emoji = DIRECTION_EMOJI[direction]
    dir_fa = DIRECTION_FA[direction]
    candle_str = format_candle_time(candle_time)
    leverage = cfg["leverage"]
    total_margin, total_notional = open_positions_totals(state)

    if DRY_RUN:
        balance_before = state.get("_paper_balance_irt", PAPER_STARTING_BALANCE_IRT)
        paper_ledger.record_open(
            PAPER_LOG_FILE, now_iso(), spot_symbol, source_label, direction,
            real_price, sl, tp1, tp2, qty, notional_irt,
        )
        notify(
            f"#{trade_id} {emoji} پوزیشن فرضی {dir_fa} باز شد\n"
            f"{source_label}{extra_label} |\n"
            f"نماد: {spot_symbol}\n"
            f"زمان کندل: {candle_str}\n"
            f"حجم: {qty}\n"
            f"ارزش معامله: {notional_irt:,.0f} {cur} (لوریج {leverage:g}x)\n"
            f"مارجین این معامله: {cfg['margin_irt']:,.0f} {cur}\n"
            f"ورود: {real_price:,.0f}\n"
            f"SL: {sl:,.0f}\n"
            f"TP1: {tp1:,.0f}\n"
            f"TP2: {tp2:,.0f}\n"
            f"—\n"
            f"موجودی نقدی: {balance_before:,.0f} {cur}\n"
            f"مارجین درگیر در پوزیشن‌های باز: {total_margin:,.0f} {cur}\n"
            f"ارزش کل پوزیشن‌های باز (اسمی): {total_notional:,.0f} {cur}"
        )
    else:
        notify(
            f"#{trade_id} {emoji} پوزیشن {dir_fa} باز شد [واقعی]\n"
            f"{source_label}{extra_label} |\n"
            f"نماد: {spot_symbol}\n"
            f"زمان کندل: {candle_str}\n"
            f"حجم: {qty}\n"
            f"ارزش معامله: {notional_irt:,.0f} {cur} (لوریج {leverage:g}x)\n"
            f"مارجین این معامله: {cfg['margin_irt']:,.0f} {cur}\n"
            f"ورود: {real_price:,.0f}\n"
            f"SL: {sl:,.0f}\n"
            f"TP1: {tp1:,.0f}\n"
            f"TP2: {tp2:,.0f}\n"
            f"—\n"
            f"مارجین درگیر در پوزیشن‌های باز: {total_margin:,.0f} {cur}\n"
            f"ارزش کل پوزیشن‌های باز (اسمی): {total_notional:,.0f} {cur}"
        )

    save_state(state)


def main():
    state = load_state()
    spot_client = get_public_client()

    all_bases = discover_all_irt_margin_bases()
    active = discover_irt_margin_symbols(spot_client, all_bases)
    if not active:
        notify("⚠️ در حال حاضر هیچ ارزی روی تبدیل بازار تومانی مارجین‌دار ندارد.")
        return
    binance_to_symbols = {f"{base}USDT": syms for base, syms in active.items()}

    mode_label = "آزمایشی (Paper — بدون پول واقعی)" if DRY_RUN else "زنده (پول واقعی)"
    print(f"حالت اجرا: {mode_label} | تعداد نمادهای تومانی مارجین‌دار: {len(binance_to_symbols)}")
    print(f"نمادهای فعال: {[s['spot'] for s in binance_to_symbols.values()]}")

    for binance_symbol, syms in binance_to_symbols.items():
        spot_symbol = syms["spot"]
        margin_symbol = syms["margin"]

        # --- مدیریت پوزیشن‌های باز موجود (هر استراتژی مستقل) ---
        st_key = f"{spot_symbol}__st"
        smc_key = f"{spot_symbol}__smc"
        try:
            manage_open_position(state, st_key, spot_symbol)
        except Exception as e:
            notify(f"❌ خطا در مدیریت پوزیشن باز {spot_symbol} (Supertrend+ADX): {e}")
        try:
            manage_open_position(state, smc_key, spot_symbol)
        except Exception as e:
            notify(f"❌ خطا در مدیریت پوزیشن باز {spot_symbol} (ICT/SMC): {e}")

        try:
            df = get_klines(binance_symbol, TIMEFRAME, KLINES_LIMIT)
        except Exception as e:
            notify(f"❌ خطا در گرفتن داده‌ی {binance_symbol}: {e}")
            time.sleep(0.5)
            continue

        # --- استراتژی ۱: Supertrend + ADX ---
        if st_key not in state:
            try:
                buy, sell, candle_time, price, st_line, adx_value = check_strategy_supertrend(df)
            except Exception as e:
                notify(f"❌ خطا در سیگنال Supertrend {binance_symbol}: {e}")
            else:
                raw_direction = "long" if buy else ("short" if sell else None)
                if raw_direction is not None:
                    try_open_position(
                        state, spot_client, spot_symbol, margin_symbol, st_key,
                        signal_key=f"{spot_symbol}__st__last_candle",
                        raw_direction=raw_direction, entry_price=price, raw_sl=st_line,
                        candle_time=candle_time, rr1=ST_TP1_RR, rr2=ST_TP2_RR,
                        source_label=SOURCE_ST, extra_label=f" | ADX={adx_value:.1f}",
                    )

        # --- استراتژی ۲: ICT/SMC Scalp Pro ---
        if smc_key not in state:
            try:
                htf_bullish, htf_bearish = get_htf_bias(binance_symbol)
            except Exception:
                htf_bullish, htf_bearish = True, True
            try:
                res = check_strategy_smc(df, htf_bullish, htf_bearish)
            except Exception as e:
                notify(f"❌ خطا در سیگنال ICT/SMC {binance_symbol}: {e}")
                res = None
            if res is not None:
                raw_direction = "long" if res["buy"] else ("short" if res["sell"] else None)
                if raw_direction is not None:
                    price2 = res["price"]
                    atr2 = res["atr"]
                    raw_sl = price2 - atr2 * SL_ATR_MULT if raw_direction == "long" else price2 + atr2 * SL_ATR_MULT
                    score = res["bull_score"] if raw_direction == "long" else res["bear_score"]
                    try_open_position(
                        state, spot_client, spot_symbol, margin_symbol, smc_key,
                        signal_key=f"{spot_symbol}__smc__last_candle",
                        raw_direction=raw_direction, entry_price=price2, raw_sl=raw_sl,
                        candle_time=res["candle_time"], rr1=TP1_RR, rr2=TP2_RR,
                        source_label=SOURCE_SMC, extra_label=f" | امتیاز={score}/7",
                    )

        time.sleep(0.5)

    save_state(state)


if __name__ == "__main__":
    main()
