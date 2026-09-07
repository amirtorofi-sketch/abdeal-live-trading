"""
بات شماره ۲ — استراتژی ICT/SMC Scalp Pro v2 (همون استراتژی ربات dasttrade2)
سیگنال از داده‌ی دلاری بایننس، قیمت اجرا/بستن از قیمت واقعی لحظه‌ای تبدیل.

همون دو حالت اجرا که در bot1_live/live_trader.py توضیح داده شده
(TABDEAL_DRY_RUN=true -> آزمایشی روی قیمت واقعی، بدون پول واقعی).

برخلاف بات ۱، جهت سیگنال معکوس نمی‌شود (طبق مستندات پروژه، معکوس‌سازی فقط
برای Supertrend+ADX تایید شده بود).

نمادهای EURUSDT و PAXGUSDT از ربات تلگرام دوم اینجا نیستند چون معادل تومانی
مستقیم روی تبدیل ندارند/تایید نشدند؛ فقط ارزهایی که discover_irt_margin_symbols
همین الان روی تبدیل پیدا کند فعال می‌شوند.
"""

import os
import sys
import json
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from signal_bot import get_klines, TIMEFRAME, KLINES_LIMIT  # noqa: E402
from signal_bot_v2 import check_strategy_smc_v2, get_htf_bias_v2, SL_ATR_MULT, TP1_RR, TP2_RR  # noqa: E402
from common.tabdeal_broker import (  # noqa: E402
    open_margin_position, close_margin_position, get_public_client, get_mid_price,
    discover_irt_margin_symbols, extract_real_price, get_market_info, split_into_two_lots,
    BrokerError, DRY_RUN,
)
from common.telegram_notify import send_telegram  # noqa: E402
from common import paper_ledger  # noqa: E402

CANDIDATE_BASES = ["BTC", "ETH", "SOL", "BNB", "DOGE"]

OWN_MARGIN_IRT = float(os.environ.get("BOT2_MARGIN_IRT", "100000"))
LEVERAGE = float(os.environ.get("BOT2_LEVERAGE", "2"))
PAPER_STARTING_BALANCE_IRT = float(os.environ.get("PAPER_STARTING_BALANCE_IRT", "5000000"))

TELEGRAM_BOT_TOKEN_2 = os.environ.get("TELEGRAM_BOT_TOKEN_2", "")
TELEGRAM_CHAT_ID_2 = os.environ.get("TELEGRAM_CHAT_ID_2", "")

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
    send_telegram(TELEGRAM_BOT_TOKEN_2, TELEGRAM_CHAT_ID_2, text)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _close_lot(state, tabdeal_symbol, pos, lot_key, price, reason, source_label):
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
            htf_bullish, htf_bearish = get_htf_bias_v2(binance_symbol)
            res = check_strategy_smc_v2(df, htf_bullish, htf_bearish)
        except Exception as e:
            notify(f"❌ خطا در گرفتن سیگنال {binance_symbol}: {e}")
            continue

        if res is None:
            # پیش می‌آید اگر تاریخچه‌ی کندل کافی نباشد؛ این نماد را این دور رد کن.
            continue

        candle_time = res["candle_time"]
        signal_key = f"{tabdeal_symbol}__last_candle"
        if state.get(signal_key) == str(candle_time):
            continue

        direction = "long" if res["buy"] else ("short" if res["sell"] else None)
        if direction is None:
            continue

        price = res["price"]
        atr_v = res["atr"]
        if direction == "long":
            sl = price - atr_v * SL_ATR_MULT
            tp1 = price + (price - sl) * TP1_RR
            tp2 = price + (price - sl) * TP2_RR
            side = "BUY"
        else:
            sl = price + atr_v * SL_ATR_MULT
            tp1 = price - (sl - price) * TP1_RR
            tp2 = price - (sl - price) * TP2_RR
            side = "SELL"

        try:
            order = open_margin_position(tabdeal_symbol, side, OWN_MARGIN_IRT, LEVERAGE, logger=print)
        except BrokerError as e:
            notify(f"❌ سیگنال {direction} روی {tabdeal_symbol} رد شد: {e}")
            state[signal_key] = str(candle_time)
            save_state(state)
            continue

        real_price = extract_real_price(order, fallback_price=price)
        qty = float(order.get("origQty") or order.get("quantity"))
        notional_irt = float(order.get("_notional_irt", order.get("notional_irt", real_price * qty)))
        score = res["bull_score"] if direction == "long" else res["bear_score"]

        try:
            market = get_market_info(spot_client, tabdeal_symbol)
            qty_a, qty_b = split_into_two_lots(market, qty)
        except Exception:
            qty_a, qty_b = qty, 0.0

        if qty_b <= 0:
            state[tabdeal_symbol] = {
                "direction": direction, "entry": real_price, "sl": sl, "tp1": tp1, "tp2": tp2,
                "source_label": "ICT/SMC v2", "opened_at": str(candle_time), "score": score,
                "lot_a": {"qty": qty_a, "status": "closed"},
                "lot_b": {"qty": qty_a, "status": "open"},
            }
        else:
            state[tabdeal_symbol] = {
                "direction": direction, "entry": real_price, "sl": sl, "tp1": tp1, "tp2": tp2,
                "source_label": "ICT/SMC v2", "opened_at": str(candle_time), "score": score,
                "lot_a": {"qty": qty_a, "status": "open"},
                "lot_b": {"qty": qty_b, "status": "open"},
            }
        state[signal_key] = str(candle_time)

        if DRY_RUN:
            paper_ledger.record_open(
                PAPER_LOG_FILE, now_iso(), tabdeal_symbol, "ICT/SMC v2", direction,
                real_price, sl, tp1, tp2, qty, notional_irt,
            )
            notify(
                f"🟢 [آزمایشی] پوزیشن {direction.upper()} باز شد | {tabdeal_symbol} | ICT/SMC v2\n"
                f"ورود واقعی تبدیل≈{real_price:,.0f} | SL={sl:,.0f} | TP1={tp1:,.0f} | TP2={tp2:,.0f}\n"
                f"مقدار: {qty} | حجم فرضی≈{notional_irt:,.0f} تومان (هیچ سفارش واقعی ثبت نشد)"
            )
        else:
            notify(
                f"🟢 پوزیشن واقعی {direction.upper()} باز شد | {tabdeal_symbol} | ICT/SMC v2\n"
                f"ورود≈{real_price:,.0f} | SL={sl:,.0f} | TP1={tp1:,.0f} | TP2={tp2:,.0f}\n"
                f"مقدار: {qty}"
            )

        save_state(state)
        time.sleep(1)

    save_state(state)


if __name__ == "__main__":
    main()
