"""
بات شماره ۱ — دو استراتژی (Supertrend+ADX + ICT/SMC Scalp Pro)، هر دو معکوس،
روی تمام ارزهایی که همین الان روی تبدیل بازار تومانیِ مارجین‌دار دارند
(نه یک لیست ثابت و دستی) — دقیقاً همون چیزی که ربات Paper Trading اول
(Dasttrade) انجام می‌ده: هر دو استراتژی به‌طور مستقل روی هر نماد اجرا می‌شن
و می‌تونن هم‌زمان پوزیشن جدا داشته باشن (کلید پوزیشن = نماد + نام استراتژی).

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
    get_market_info, split_into_two_lots, BrokerError, DRY_RUN, _float_env,
)
from common.telegram_notify import send_telegram  # noqa: E402
from common import paper_ledger  # noqa: E402

OWN_MARGIN_IRT = _float_env("BOT1_MARGIN_IRT", "150000")
LEVERAGE = _float_env("BOT1_LEVERAGE", "3")
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


def notify(text: str):
    print(text)
    send_telegram(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, text)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_direction_and_levels(raw_direction: str, entry: float, raw_sl: float, rr1: float, rr2: float):
    """
    دقیقاً همون منطق trading_bot.py: سیگنال خام همیشه معکوس اجرا می‌شود.
    raw_direction اینجا "long"/"short" است (جهت خامی که خودِ استراتژی حساب
    کرده، نه جهت نهایی).
    """
    risk = abs(entry - raw_sl)
    if raw_direction == "long":
        return "short", entry + risk, entry - risk * rr1, entry - risk * rr2
    else:
        return "long", entry - risk, entry + risk * rr1, entry + risk * rr2


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

    if DRY_RUN:
        balance = state.get("_paper_balance_irt", PAPER_STARTING_BALANCE_IRT)
        pnl = paper_ledger.record_close(
            PAPER_LOG_FILE, now_iso(), spot_symbol, source_label, pos["direction"],
            pos["entry"], price, lot["qty"], balance, reason,
        )
        new_balance = balance + pnl
        state["_paper_balance_irt"] = new_balance
        notify(
            f"⚪ [آزمایشی] بسته شدن {lot_key} پوزیشن {spot_symbol} ({source_label}, {pos['direction']}) — دلیل: {reason}\n"
            f"قیمت خروج≈{price:,.0f} | PnL فرضی این لات≈{pnl:,.0f} تومان | موجودی فرضی≈{new_balance:,.0f} تومان"
        )
    else:
        try:
            close_margin_position(margin_symbol, opposite, lot["qty"], logger=print)
        except BrokerError as e:
            notify(f"❌ خطا در بستن {lot_key} پوزیشن واقعی {spot_symbol} ({source_label}): {e}")
            return False
        notify(f"⚪ بسته شدن {lot_key} پوزیشن واقعی {spot_symbol} ({source_label}, {pos['direction']}) — دلیل: {reason} — قیمت≈{price:,.0f}")

    lot["status"] = "closed"
    return True


def manage_open_position(state: dict, position_key: str, spot_symbol: str):
    """
    معماری دو-لاتی - دقیقاً مطابق trading_bot.py اصلی:
    لات a هدفش TP1 است؛ وقتی TP1 خورد، SL برای لات b (باقی‌مانده) به نقطه‌ی
    ورود (Breakeven) منتقل می‌شود. لات b هدفش TP2 است. اگر هر دو لات بسته
    شدند، پوزیشن از state حذف می‌شود.
    """
    pos = state.get(position_key)
    if not pos:
        return
    spot_client = get_public_client()
    price = get_mid_price(spot_client, spot_symbol)
    is_long = pos["direction"] == "long"
    source_label = pos["source_label"]

    lot_a = pos["lot_a"]
    if lot_a["status"] == "open":
        hit_sl = (price <= pos["sl"]) if is_long else (price >= pos["sl"])
        hit_tp1 = (price >= pos["tp1"]) if is_long else (price <= pos["tp1"])
        if hit_sl or hit_tp1:
            reason = "SL" if hit_sl else "TP1"
            ok = _close_lot(state, spot_symbol, pos, "lot_a", price, reason, source_label)
            if ok and reason == "TP1" and pos["lot_b"]["status"] == "open":
                pos["sl"] = pos["entry"]
                notify(f"🔵 SL لات باقی‌مانده‌ی {spot_symbol} ({source_label}) به نقطه‌ی ورود (Breakeven={pos['entry']:,.0f}) منتقل شد.")

    lot_b = pos["lot_b"]
    if lot_b["status"] == "open":
        hit_sl = (price <= pos["sl"]) if is_long else (price >= pos["sl"])
        hit_tp2 = (price >= pos["tp2"]) if is_long else (price <= pos["tp2"])
        if hit_sl or hit_tp2:
            reason = "SL" if hit_sl else "TP2"
            _close_lot(state, spot_symbol, pos, "lot_b", price, reason, source_label)

    if pos["lot_a"]["status"] == "closed" and pos["lot_b"]["status"] == "closed":
        del state[position_key]


def try_open_position(state, spot_client, spot_symbol, margin_symbol, position_key, signal_key,
                       raw_direction, entry_price, raw_sl, candle_time, rr1, rr2, source_label, extra_label=""):
    """
    منطق مشترک باز کردن پوزیشن (دو-لاتی) برای هر دو استراتژی - تا کد برای
    Supertrend و SMC دوباره‌نویسی نشود.
    """
    if state.get(signal_key) == str(candle_time):
        return

    direction, sl, tp1, tp2 = resolve_direction_and_levels(raw_direction, entry_price, raw_sl, rr1, rr2)
    side = "BUY" if direction == "long" else "SELL"

    try:
        order = open_margin_position(spot_symbol, margin_symbol, side, OWN_MARGIN_IRT, LEVERAGE, logger=print)
    except BrokerError as e:
        notify(f"❌ سیگنال {direction} روی {spot_symbol} ({source_label}) رد شد: {e}")
        state[signal_key] = str(candle_time)
        save_state(state)
        return

    real_price = extract_real_price(order, fallback_price=entry_price)
    qty = float(order.get("origQty") or order.get("quantity"))
    notional_irt = float(order.get("_notional_irt", order.get("notional_irt", real_price * qty)))

    try:
        market = get_market_info(spot_client, spot_symbol)
        qty_a, qty_b = split_into_two_lots(market, qty)
    except Exception:
        qty_a, qty_b = qty, 0.0

    base_fields = {
        "direction": direction, "entry": real_price, "sl": sl, "tp1": tp1, "tp2": tp2,
        "source_label": source_label, "opened_at": str(candle_time), "margin_symbol": margin_symbol,
    }
    if qty_b <= 0:
        state[position_key] = {**base_fields, "lot_a": {"qty": qty_a, "status": "closed"}, "lot_b": {"qty": qty_a, "status": "open"}}
    else:
        state[position_key] = {**base_fields, "lot_a": {"qty": qty_a, "status": "open"}, "lot_b": {"qty": qty_b, "status": "open"}}
    state[signal_key] = str(candle_time)

    if DRY_RUN:
        paper_ledger.record_open(
            PAPER_LOG_FILE, now_iso(), spot_symbol, source_label, direction,
            real_price, sl, tp1, tp2, qty, notional_irt,
        )
        notify(
            f"🟢 [آزمایشی] پوزیشن {direction.upper()} باز شد | {spot_symbol} | {source_label}{extra_label}\n"
            f"ورود واقعی تبدیل≈{real_price:,.0f} | SL={sl:,.0f} | TP1={tp1:,.0f} | TP2={tp2:,.0f}\n"
            f"مقدار: {qty} | حجم فرضی≈{notional_irt:,.0f} تومان (هیچ سفارش واقعی ثبت نشد)"
        )
    else:
        notify(
            f"🟢 پوزیشن واقعی {direction.upper()} باز شد | {spot_symbol} | {source_label}{extra_label}\n"
            f"ورود≈{real_price:,.0f} | SL={sl:,.0f} | TP1={tp1:,.0f} | TP2={tp2:,.0f} | مقدار: {qty}"
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
                        source_label="Supertrend+ADX", extra_label=f" | ADX={adx_value:.1f}",
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
                        source_label="ICT/SMC Scalp Pro", extra_label=f" | امتیاز={score}/7",
                    )

        time.sleep(0.5)

    save_state(state)


if __name__ == "__main__":
    main()
