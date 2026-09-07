"""
بات شماره ۱ — استراتژی Supertrend+ADX (همون استراتژی فعال ربات Dasttrade)
سیگنال از داده‌ی دلاری بایننس گرفته می‌شود؛ قیمت اجرا و بستن پوزیشن از
قیمت لحظه‌ای *واقعی* تبدیل (Depth) خوانده می‌شود.

دو حالت اجرا (با Variable گیت‌هابی TABDEAL_DRY_RUN کنترل می‌شود):
    TABDEAL_DRY_RUN=true  -> حالت آزمایشی/فرضی (Paper): هیچ سفارش واقعی ثبت
                             نمی‌شود، ولی قیمت ورود/خروج از قیمت واقعی لحظه‌ای
                             تبدیل خوانده می‌شود و در common/paper_ledger.py
                             (فایل bot1_live/paper_trades_log.csv) سود/زیان
                             فرضی و موجودی فرضی ثبت می‌شود — دقیقاً برای اینکه
                             ببینی این استراتژی روی قیمت واقعیِ این صرافی
                             (با اسپرد و شرایط خودش) هنوز سودده هست یا نه.
    TABDEAL_DRY_RUN=false -> حالت زنده: سفارش واقعی با پول واقعی ثبت می‌شود.

نمادهای فعال هر بار اجرا با discover_irt_margin_symbols به‌صورت زنده از
تبدیل خوانده می‌شوند (نه یک لیست ثابت) — یعنی خودکار هر ارزی که الان
بازار تومانی مارجین‌دار دارد وارد می‌شود.

جهت سفارش دقیقاً طبق یافته‌ی بک‌تست پروژه معکوس اجرا می‌شود:
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
    get_klines, check_strategy_supertrend, TIMEFRAME, KLINES_LIMIT, ST_TP1_RR, ST_TP2_RR,
)
from common.tabdeal_broker import (  # noqa: E402
    open_margin_position, close_margin_position, get_public_client, get_mid_price,
    discover_irt_margin_symbols, extract_real_price, get_market_info, split_into_two_lots,
    BrokerError, DRY_RUN,
)
from common.telegram_notify import send_telegram  # noqa: E402
from common import paper_ledger  # noqa: E402

# ارزهای پایه‌ی کاندید این استراتژی (از روی SYMBOLS در signal_bot.py)
CANDIDATE_BASES = ["BTC", "ETH", "SOL", "BNB", "DOGE", "CRV", "ROSE", "CHZ", "ONE", "VET", "MASK", "MANA", "GALA"]

OWN_MARGIN_IRT = float(os.environ.get("BOT1_MARGIN_IRT", "150000"))
LEVERAGE = float(os.environ.get("BOT1_LEVERAGE", "3"))
PAPER_STARTING_BALANCE_IRT = float(os.environ.get("PAPER_STARTING_BALANCE_IRT", "5000000"))

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


def notify(text: str):
    print(text)
    send_telegram(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, text)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_direction_and_levels(raw_direction: str, entry: float, raw_sl: float):
    """دقیقاً همون منطق trading_bot.py: سیگنال خام معکوس اجرا می‌شود."""
    risk = abs(entry - raw_sl)
    if raw_direction == "buy":
        return "short", entry + risk, entry - risk * ST_TP1_RR, entry - risk * ST_TP2_RR
    else:
        return "long", entry - risk, entry + risk * ST_TP1_RR, entry + risk * ST_TP2_RR


def _close_lot(state, tabdeal_symbol, pos, lot_key, price, reason, source_label):
    """
    یک لات (a یا b) را می‌بندد - چه در حالت آزمایشی (فقط شبیه‌سازی) چه در
    حالت زنده (سفارش واقعی معکوس). در صورت خطای واقعی، لات را «باز» نگه
    می‌دارد تا دور بعد دوباره تلاش شود، و False برمی‌گرداند.
    """
    lot = pos[lot_key]
    opposite = "SELL" if pos["direction"] == "long" else "BUY"

    if DRY_RUN:
        balance = state.get("_paper_balance_irt", PAPER_STARTING_BALANCE_IRT)
        pnl = paper_ledger.record_close(
            PAPER_LOG_FILE, now_iso(), tabdeal_symbol, source_label, pos["direction"],
            pos["entry"], price, lot["qty"], balance, reason,
        )
        new_balance = balance + pnl
        state["_paper_balance_irt"] = new_balance
        notify(
            f"⚪ [آزمایشی] بسته شدن {lot_key} پوزیشن {tabdeal_symbol} ({pos['direction']}) — دلیل: {reason}\n"
            f"قیمت خروج≈{price:,.0f} | PnL فرضی این لات≈{pnl:,.0f} تومان | موجودی فرضی≈{new_balance:,.0f} تومان"
        )
    else:
        try:
            close_margin_position(tabdeal_symbol, opposite, lot["qty"], logger=print)
        except BrokerError as e:
            notify(f"❌ خطا در بستن {lot_key} پوزیشن واقعی {tabdeal_symbol}: {e}")
            return False
        notify(f"⚪ بسته شدن {lot_key} پوزیشن واقعی {tabdeal_symbol} ({pos['direction']}) — دلیل: {reason} — قیمت≈{price:,.0f}")

    lot["status"] = "closed"
    return True


def manage_open_position(state: dict, tabdeal_symbol: str):
    """
    معماری دو-لاتی - دقیقاً مطابق trading_bot.py اصلی:
    لات a هدفش TP1 است؛ وقتی TP1 خورد، SL برای لات b (باقی‌مانده) به نقطه‌ی
    ورود (Breakeven) منتقل می‌شود. لات b هدفش TP2 است (یا همون SL، که ممکنه
    breakeven شده باشه). اگر هر دو لات بسته شدند، پوزیشن از state حذف می‌شود.
    """
    pos = state.get(tabdeal_symbol)
    if not pos:
        return
    spot_client = get_public_client()
    price = get_mid_price(spot_client, tabdeal_symbol)
    is_long = pos["direction"] == "long"

    lot_a = pos["lot_a"]
    if lot_a["status"] == "open":
        hit_sl = (price <= pos["sl"]) if is_long else (price >= pos["sl"])
        hit_tp1 = (price >= pos["tp1"]) if is_long else (price <= pos["tp1"])
        if hit_sl or hit_tp1:
            reason = "SL" if hit_sl else "TP1"
            ok = _close_lot(state, tabdeal_symbol, pos, "lot_a", price, reason, pos["source_label"])
            if ok and reason == "TP1" and pos["lot_b"]["status"] == "open":
                pos["sl"] = pos["entry"]
                notify(f"🔵 SL لات باقی‌مانده‌ی {tabdeal_symbol} به نقطه‌ی ورود (Breakeven={pos['entry']:,.0f}) منتقل شد.")

    lot_b = pos["lot_b"]
    if lot_b["status"] == "open":
        hit_sl = (price <= pos["sl"]) if is_long else (price >= pos["sl"])
        hit_tp2 = (price >= pos["tp2"]) if is_long else (price <= pos["tp2"])
        if hit_sl or hit_tp2:
            reason = "SL" if hit_sl else "TP2"
            _close_lot(state, tabdeal_symbol, pos, "lot_b", price, reason, pos["source_label"])

    if pos["lot_a"]["status"] == "closed" and pos["lot_b"]["status"] == "closed":
        del state[tabdeal_symbol]


def main():
    state = load_state()
    spot_client = get_public_client()

    active = discover_irt_margin_symbols(spot_client, CANDIDATE_BASES)
    if not active:
        notify("⚠️ در حال حاضر هیچ‌کدام از نمادهای این استراتژی روی تبدیل بازار تومانی مارجین‌دار ندارند.")
        return
    binance_to_tabdeal = {f"{base}USDT": tabdeal_symbol for base, tabdeal_symbol in active.items()}

    mode_label = "آزمایشی (Paper — بدون پول واقعی)" if DRY_RUN else "زنده (پول واقعی)"
    print(f"حالت اجرا: {mode_label} | نمادهای فعال: {list(binance_to_tabdeal.values())}")

    for binance_symbol, tabdeal_symbol in binance_to_tabdeal.items():
        try:
            manage_open_position(state, tabdeal_symbol)
        except Exception as e:
            notify(f"❌ خطا در مدیریت پوزیشن باز {tabdeal_symbol}: {e}")

        if tabdeal_symbol in state:
            time.sleep(0.5)
            continue

        try:
            df = get_klines(binance_symbol, TIMEFRAME, KLINES_LIMIT)
            buy, sell, candle_time, price, st_line, adx_value = check_strategy_supertrend(df)
        except Exception as e:
            notify(f"❌ خطا در گرفتن سیگنال {binance_symbol}: {e}")
            continue

        raw_direction = "buy" if buy else ("sell" if sell else None)
        if raw_direction is None:
            time.sleep(0.5)
            continue

        signal_key = f"{tabdeal_symbol}__last_candle"
        if state.get(signal_key) == str(candle_time):
            continue

        direction, sl, tp1, tp2 = resolve_direction_and_levels(raw_direction, price, st_line)
        side = "BUY" if direction == "long" else "SELL"

        try:
            order = open_margin_position(tabdeal_symbol, side, OWN_MARGIN_IRT, LEVERAGE, logger=print)
        except BrokerError as e:
            notify(f"❌ سیگنال {direction} روی {tabdeal_symbol} رد شد: {e}")
            state[signal_key] = str(candle_time)
            save_state(state)
            continue

        # قیمت واقعی تبدیل (نه قیمت بایننس که سیگنال رویش حساب شد)
        real_price = extract_real_price(order, fallback_price=price)
        qty = float(order.get("origQty") or order.get("quantity"))
        notional_irt = float(order.get("_notional_irt", order.get("notional_irt", real_price * qty)))

        try:
            market = get_market_info(spot_client, tabdeal_symbol)
            qty_a, qty_b = split_into_two_lots(market, qty)
        except Exception:
            qty_a, qty_b = qty, 0.0  # اگر گرفتن اطلاعات بازار شکست خورد، تک‌لاتی (فقط TP2) ادامه بده

        if qty_b <= 0:
            # حجم برای تقسیم به دو لات خیلی کوچک بود؛ تک‌لاتی با هدف TP2 (بدون TP1/Breakeven)
            state[tabdeal_symbol] = {
                "direction": direction, "entry": real_price, "sl": sl, "tp1": tp1, "tp2": tp2,
                "source_label": "Supertrend+ADX", "opened_at": str(candle_time), "adx": adx_value,
                "lot_a": {"qty": qty_a, "status": "closed"},  # از قبل بسته یعنی هدف TP1 وجود ندارد
                "lot_b": {"qty": qty_a, "status": "open"},
            }
        else:
            state[tabdeal_symbol] = {
                "direction": direction, "entry": real_price, "sl": sl, "tp1": tp1, "tp2": tp2,
                "source_label": "Supertrend+ADX", "opened_at": str(candle_time), "adx": adx_value,
                "lot_a": {"qty": qty_a, "status": "open"},
                "lot_b": {"qty": qty_b, "status": "open"},
            }
        state[signal_key] = str(candle_time)

        if DRY_RUN:
            paper_ledger.record_open(
                PAPER_LOG_FILE, now_iso(), tabdeal_symbol, "Supertrend+ADX", direction,
                real_price, sl, tp1, tp2, qty, notional_irt,
            )
            notify(
                f"🟢 [آزمایشی] پوزیشن {direction.upper()} باز شد | {tabdeal_symbol} | Supertrend+ADX\n"
                f"ورود واقعی تبدیل≈{real_price:,.0f} | SL={sl:,.0f} | TP1={tp1:,.0f} | TP2={tp2:,.0f} | ADX={adx_value:.1f}\n"
                f"مقدار: {qty} | حجم فرضی≈{notional_irt:,.0f} تومان (هیچ سفارش واقعی ثبت نشد)"
            )
        else:
            notify(
                f"🟢 پوزیشن واقعی {direction.upper()} باز شد | {tabdeal_symbol} | Supertrend+ADX\n"
                f"ورود≈{real_price:,.0f} | SL={sl:,.0f} | TP1={tp1:,.0f} | TP2={tp2:,.0f} | ADX={adx_value:.1f}\n"
                f"مقدار: {qty}"
            )

        save_state(state)
        time.sleep(1)

    save_state(state)


if __name__ == "__main__":
    main()
