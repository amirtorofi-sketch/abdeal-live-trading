"""
دفتر معاملات آزمایشی (Paper Ledger) — برای دوره‌ی «فعلاً زنده خاموش، فقط تست
سودآوری روی قیمت واقعی تبدیل». هیچ سفارش واقعی ثبت نمی‌کند؛ فقط با قیمت
لحظه‌ای *واقعی* تبدیل (از common.tabdeal_broker.get_mid_price) باز/بسته‌شدن
پوزیشن را شبیه‌سازی می‌کند و سود/زیان فرضی + موجودی فرضی را در یک CSV
ثبت می‌کند تا بعداً ببینی استراتژی روی این صرافی (با اسپرد/قیمت واقعی‌اش)
هنوز سودده هست یا نه.

ستون‌های bot_name، trade_id و lot اضافه شدند تا وقتی چند پوزیشن هم‌زمان روی
هر نماد باز است، بشود دقیقاً فهمید کدام ردیف open به کدام close مربوط است و
مال کدام بات (بات۱/بات۲) است. ستون‌های adx_value و signal_score هم فقط روی
ردیف‌های open پر می‌شوند: adx_value برای Supertrend+ADX، signal_score برای
ICT/SMC (امتیاز خام از ۷) - تا بشود بعداً بررسی کرد آیا سیگنال‌های قوی‌تر
واقعاً نتیجه‌ی بهتری داشته‌اند یا نه.
"""

import csv
import os

FIELDS = [
    "event_time_utc", "event_type", "bot_name", "symbol", "source", "direction",
    "trade_id", "lot", "entry_price", "exit_price", "sl", "tp1", "tp2", "quantity",
    "notional_irt", "pnl_irt", "balance_after_irt", "exit_reason",
    "adx_value", "signal_score",
]


def _ensure_header(path: str):
    if not os.path.exists(path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=FIELDS).writeheader()
        return

    # مهاجرت خودکار: اگه فایل از قبل با سرستون قدیمی‌تر (بدون یکی از
    # ستون‌های جدید) وجود داشته، یه‌بار کل فایل رو با سرستون جدید بازنویسی
    # می‌کنیم - ردیف‌های قدیمی برای ستون‌های تازه فقط خالی می‌مونن، هیچ
    # داده‌ای از دست نمی‌ره. اگه از قبل به‌روز بود، هیچ کاری نمی‌کنه.
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
        csv.DictWriter(f, fieldnames=FIELDS).writerow(row)


def record_open(path: str, now_iso: str, bot_name: str, symbol: str, source: str, direction: str,
                 trade_id, entry_price: float, sl: float, tp1: float, tp2: float,
                 quantity: float, notional_irt: float, adx_value=None, signal_score=None):
    _append_row(path, {
        "event_time_utc": now_iso, "event_type": "open", "bot_name": bot_name,
        "symbol": symbol, "source": source, "direction": direction,
        "trade_id": trade_id, "lot": "", "entry_price": entry_price, "exit_price": "",
        "sl": sl, "tp1": tp1, "tp2": tp2, "quantity": quantity,
        "notional_irt": notional_irt, "pnl_irt": "", "balance_after_irt": "", "exit_reason": "",
        "adx_value": f"{adx_value:.2f}" if adx_value is not None else "",
        "signal_score": signal_score if signal_score is not None else "",
    })


def record_close(path: str, now_iso: str, bot_name: str, symbol: str, source: str, direction: str,
                  trade_id, lot: str, entry_price: float, exit_price: float, quantity: float,
                  balance_before_irt: float, exit_reason: str) -> float:
    """
    PnL فرضی را حساب می‌کند، موجودیِ فرضیِ *بعد از* همین معامله را در CSV
    ثبت می‌کند، و PnL را برمی‌گرداند (به تومان، بر پایه‌ی حرکت قیمت پایه).
    """
    sign = 1 if direction == "long" else -1
    pnl_irt = (exit_price - entry_price) * quantity * sign
    balance_after_irt = balance_before_irt + pnl_irt
    _append_row(path, {
        "event_time_utc": now_iso, "event_type": "close", "bot_name": bot_name,
        "symbol": symbol, "source": source, "direction": direction,
        "trade_id": trade_id, "lot": lot, "entry_price": entry_price, "exit_price": exit_price,
        "sl": "", "tp1": "", "tp2": "", "quantity": quantity,
        "notional_irt": "", "pnl_irt": pnl_irt, "balance_after_irt": balance_after_irt,
        "exit_reason": exit_reason, "adx_value": "", "signal_score": "",
    })
    return pnl_irt
