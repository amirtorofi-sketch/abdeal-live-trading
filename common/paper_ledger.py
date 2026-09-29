"""
دفتر معاملات آزمایشی (Paper Ledger) — برای دوره‌ی «فعلاً زنده خاموش، فقط تست
سودآوری روی قیمت واقعی تبدیل». هیچ سفارش واقعی ثبت نمی‌کند؛ فقط با قیمت
لحظه‌ای *واقعی* تبدیل (از common.tabdeal_broker.get_mid_price) باز/بسته‌شدن
پوزیشن را شبیه‌سازی می‌کند و سود/زیان فرضی + موجودی فرضی را در یک CSV
ثبت می‌کند تا بعداً ببینی استراتژی روی این صرافی (با اسپرد/قیمت واقعی‌اش)
هنوز سودده هست یا نه.

ساختار لاگ (مثل دست‌ترید):
- هر پوزیشن یک ردیف open دارد و به‌ازای هر لات (a/b) یک ردیف close.
- ردیف‌های close جزئیات ورود را هم تکرار می‌کنند (SL اولیه، TP1/TP2، ارزش کل
  پوزیشن، لوریج، مارجین، امتیاز/ADX و market_snapshot) تا هر ردیف به‌تنهایی
  قابل تحلیل باشد. pnl_irt همیشه سود/زیان همان لات است (نه کل پوزیشن).
- ردیف‌های sl_to_be (انتقال SL به نقطه‌ی ورود) و full_close (بسته‌شدن کامل)
  هم ثبت می‌شوند؛ pnl_irt آن‌ها خالی است تا جمع ستون pnl روی ردیف‌های close
  دوبار حساب نشود.
- market_snapshot: برای SMC اجزای امتیاز (sweep، حجم، RSI، زون، FVG/OB، HTF ...)،
  برای Supertrend مقدار ADX/DI±/EMA/حجم.
"""

import csv
import json
import os
from datetime import datetime

FIELDS = [
    "event_time_utc", "event_type", "bot_name", "symbol", "source", "direction",
    "trade_id", "lot", "entry_price", "exit_price", "sl", "tp1", "tp2", "quantity",
    "notional_irt", "pnl_irt", "balance_after_irt", "exit_reason",
    "adx_value", "signal_score",
    # ستون‌های جدید (هم‌سطح با ثبت دست‌ترید)
    "timeframe", "session", "candle_time", "raw_direction", "leverage", "margin_irt",
    "market_snapshot",
]


def session_of(candle_time) -> str:
    """سشن معاملاتی بر پایه‌ی ساعت UTC کندل (سه بازه‌ی ۸ ساعته - مثل دست‌ترید)."""
    try:
        h = candle_time.hour if hasattr(candle_time, "hour") else datetime.fromisoformat(str(candle_time)).hour
    except Exception:
        return ""
    if 0 <= h < 8:
        return "آسیا"
    if 8 <= h < 16:
        return "لندن"
    return "نیویورک"


def _ensure_header(path: str):
    if not os.path.exists(path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=FIELDS).writeheader()
        return

    # مهاجرت خودکار: اگه فایل از قبل با سرستون قدیمی‌تر وجود داشته، یه‌بار کل
    # فایل با سرستون جدید بازنویسی می‌شه (بر اساس نام ستون) - ردیف‌های قدیمی برای
    # ستون‌های تازه فقط خالی می‌مونن، هیچ داده‌ای از دست نمی‌ره.
    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        existing_fields = reader.fieldnames or []
        if existing_fields == FIELDS:
            return
        rows = list(reader)

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in FIELDS})


def _append_row(path: str, row: dict):
    _ensure_header(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=FIELDS).writerow({k: row.get(k, "") for k in FIELDS})


def _snapshot_json(snap) -> str:
    if snap is None or snap == "":
        return ""
    if isinstance(snap, str):
        return snap
    try:
        return json.dumps(snap, ensure_ascii=False)
    except Exception:
        return ""


def entry_row_from_pos(pos: dict) -> dict:
    """
    جزئیات ورودِ یک پوزیشن (از رکورد state) برای تکرار روی ردیف‌های close /
    sl_to_be / full_close. sl همیشه SL *اولیه* است (بعد از TP1، pos["sl"] به نقطه‌ی
    ورود می‌رود)؛ برای پوزیشن‌های قدیمی که sl_initial ندارن همون sl فعلی می‌آید.
    """
    return {
        "sl": pos.get("sl_initial", pos.get("sl", "")),
        "tp1": pos.get("tp1", ""),
        "tp2": pos.get("tp2", ""),
        "notional_irt": pos.get("notional_irt", ""),
        "adx_value": pos.get("adx_value"),
        "signal_score": pos.get("signal_score"),
        "timeframe": pos.get("timeframe", ""),
        "session": pos.get("session", ""),
        "candle_time": pos.get("candle_time", ""),
        "raw_direction": pos.get("raw_direction", ""),
        "leverage": pos.get("leverage", ""),
        "margin_irt": pos.get("margin_irt", ""),
        "market_snapshot": pos.get("market_snapshot"),
    }


def _fill_entry(row: dict, e: dict):
    adx = e.get("adx_value")
    score = e.get("signal_score")
    row.update({
        "sl": e.get("sl", ""), "tp1": e.get("tp1", ""), "tp2": e.get("tp2", ""),
        "notional_irt": e.get("notional_irt", ""),
        "adx_value": f"{float(adx):.2f}" if adx not in (None, "") else "",
        "signal_score": score if score is not None else "",
        "timeframe": e.get("timeframe", ""), "session": e.get("session", ""),
        "candle_time": e.get("candle_time", ""), "raw_direction": e.get("raw_direction", ""),
        "leverage": e.get("leverage", ""), "margin_irt": e.get("margin_irt", ""),
        "market_snapshot": _snapshot_json(e.get("market_snapshot")),
    })


def record_open(path: str, now_iso: str, bot_name: str, symbol: str, source: str, direction: str,
                trade_id, entry_price: float, sl: float, tp1: float, tp2: float,
                quantity: float, notional_irt: float, adx_value=None, signal_score=None,
                details: dict = None):
    """details: timeframe, session, candle_time, raw_direction, leverage, margin_irt, market_snapshot."""
    row = {
        "event_time_utc": now_iso, "event_type": "open", "bot_name": bot_name,
        "symbol": symbol, "source": source, "direction": direction,
        "trade_id": trade_id, "lot": "", "entry_price": entry_price, "exit_price": "",
        "sl": sl, "tp1": tp1, "tp2": tp2, "quantity": quantity,
        "notional_irt": notional_irt, "pnl_irt": "", "balance_after_irt": "", "exit_reason": "",
        "adx_value": f"{adx_value:.2f}" if adx_value is not None else "",
        "signal_score": signal_score if signal_score is not None else "",
    }
    d = details or {}
    for k in ("timeframe", "session", "candle_time", "raw_direction", "leverage", "margin_irt"):
        row[k] = d.get(k, "")
    row["market_snapshot"] = _snapshot_json(d.get("market_snapshot"))
    _append_row(path, row)


def record_close(path: str, now_iso: str, bot_name: str, symbol: str, source: str, direction: str,
                 trade_id, lot: str, entry_price: float, exit_price: float, quantity: float,
                 balance_before_irt: float, exit_reason: str, entry_row: dict = None) -> float:
    """
    PnL فرضی این لات را حساب می‌کند، موجودیِ فرضیِ *بعد از* همین معامله را در CSV
    ثبت می‌کند، و PnL را برمی‌گرداند (به تومان، بر پایه‌ی حرکت قیمت پایه).
    entry_row (از entry_row_from_pos) جزئیات ورود را روی همین ردیف تکرار می‌کند.
    """
    sign = 1 if direction == "long" else -1
    pnl_irt = (exit_price - entry_price) * quantity * sign
    balance_after_irt = balance_before_irt + pnl_irt
    row = {
        "event_time_utc": now_iso, "event_type": "close", "bot_name": bot_name,
        "symbol": symbol, "source": source, "direction": direction,
        "trade_id": trade_id, "lot": lot, "entry_price": entry_price, "exit_price": exit_price,
        "quantity": quantity, "pnl_irt": pnl_irt, "balance_after_irt": balance_after_irt,
        "exit_reason": exit_reason,
    }
    if entry_row:
        _fill_entry(row, entry_row)
    _append_row(path, row)
    return pnl_irt


def record_event(path: str, now_iso: str, bot_name: str, event_type: str, symbol: str, source: str,
                 direction: str, trade_id, entry_price, entry_row: dict = None,
                 balance_after_irt=None, exit_reason: str = "", sl=None):
    """
    ردیف‌های غیرمالی: sl_to_be (انتقال SL لات باقی‌مانده به نقطه‌ی ورود، با sl=SL جدید)
    و full_close (هر دو لات بسته شدند). pnl_irt عمداً خالی است.
    """
    row = {
        "event_time_utc": now_iso, "event_type": event_type, "bot_name": bot_name,
        "symbol": symbol, "source": source, "direction": direction, "trade_id": trade_id,
        "entry_price": entry_price, "exit_reason": exit_reason,
        "balance_after_irt": balance_after_irt if balance_after_irt is not None else "",
    }
    if entry_row:
        _fill_entry(row, entry_row)
    if sl is not None:
        row["sl"] = sl
    _append_row(path, row)


def recompute_balance(path: str, starting_balance: float):
    """
    موجودیِ فرضی = موجودی شروع + جمع pnl_irt همه‌ی ردیف‌های close. None اگه فایل نیست.
    (فقط ردیف‌های event_type=close؛ sl_to_be/full_close pnl ندارن.)
    """
    if not os.path.exists(path):
        return None
    total = 0.0
    with open(path, "r", newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("event_type") == "close" and r.get("pnl_irt") not in (None, ""):
                try:
                    total += float(r["pnl_irt"])
                except ValueError:
                    pass
    return starting_balance + total
