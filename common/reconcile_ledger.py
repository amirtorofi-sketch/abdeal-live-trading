"""
آشتی‌دادن (reconcile) لاگ معاملات فرضی + موجودی state.json.

چرا این لازم شد؟
----------------
کشف شد که وقتی دو اجرای این ورک‌فلو (به‌خاطر تریگر دوگانه‌ی بیرونی، مثلاً
cron-job.org) تقریباً هم‌زمان اجرا می‌شن، هر دو از روی همون state.json قدیمی
می‌بینن یه پوزیشن SL/TP خورده، هر دو مستقل می‌بندنش و می‌نویسنش، و بعد
`git pull --rebase` هر دو append رو (چون فایل CSV فقط append می‌شه و از نظر
git تعارض واقعی نداره) توی همون فایل نهایی نگه می‌داره - نتیجه: هر معامله
گاهی دوبار توی CSV ثبت می‌شه و موجودی هم دوبار کم/زیاد می‌شه.

این اسکریپت به‌عنوان لایه‌ی دوم دفاعی (علاوه بر رفع خودِ علت در منبع تریگر)
درست بعد از `git pull --rebase` و درست قبل از commit نهایی اجرا می‌شه: هر
ردیف «close» ی که دقیقاً با یه ردیف دیگه (نماد+منبع+جهت+قیمت ورود/خروج+حجم+
دلیل خروج) یکیه و فاصله‌ی زمانی‌شون کمتر از یه آستانه‌ست (پیش‌فرض ۵ دقیقه -
یه معامله‌ی واقعیِ متفاوت با همین دقتِ کامل توی چند دقیقه‌ی هم تقریباً
غیرممکنه) رو تکراری در نظر می‌گیره، فقط اولی رو نگه می‌داره، ستون
balance_after را برای کل فایل از نو (از روی موجودی شروع) می‌سازه، و
_paper_balance_irt در state.json را دقیقاً برابر همین عدد بازسازی‌شده تنظیم
می‌کند - یعنی موجودی همیشه از روی CSقطعی (نه از یه شمارنده‌ی راهرو که ممکنه
دوبار افزایش/کاهش پیدا کرده باشه) مشتق می‌شه.

اگه موقعی این اسکریپت لازم نبود (هیچ تکراری‌ای نبود)، فایل‌ها دست‌نخورده
می‌مونن (idempotent - اجرای دوباره‌ش هیچ اثر اضافه‌ای نداره).
"""
import sys
import json
import argparse
import pandas as pd

DUP_WINDOW_SECONDS = 300  # ۵ دقیقه - آستانه‌ی «همون معامله، دوبار ثبت شده»
# ⚠️ "lot" حتماً باید توی کلید باشه. وقتی پوزیشن مستقیم SL بخوره (بدون TP1)،
# لات a و لات b با هم و با یک قیمت SL بسته می‌شن - یعنی symbol/source/direction/
# entry_price/exit_price/quantity/exit_reason هر دو دقیقاً یکیه. بدون "lot" این
# دو ردیفِ کاملاً واقعی به اشتباه «تکراری» تشخیص داده می‌شدن و لات b حذف می‌شد
# (باگی که مکرراً موجودی را بالاتر از واقعیت نشون می‌داد).
KEY_COLS = ["symbol", "source", "direction", "entry_price", "exit_price", "quantity", "exit_reason", "lot"]


def _amount_col(df: pd.DataFrame) -> str:
    for c in ("notional_irt", "notional_usdt"):
        if c in df.columns:
            return c.replace("notional", "pnl"), c.replace("notional", "balance_after")
    raise ValueError("ستون notional_irt یا notional_usdt توی فایل پیدا نشد.")


def dedupe_closes(df: pd.DataFrame, pnl_col: str) -> tuple[pd.DataFrame, int, float]:
    """ردیف‌های close تکراری (طبق KEY_COLS + پنجره‌ی زمانی) را حذف می‌کند."""
    df = df.copy()
    df["_t"] = pd.to_datetime(df["event_time_utc"], utc=True)
    df["_orig_order"] = range(len(df))

    is_close = df["event_type"] == "close"
    drop_idx = []
    extra_pnl = 0.0

    closes = df[is_close].sort_values(KEY_COLS + ["_t"])
    for _, group in closes.groupby(KEY_COLS, dropna=False, sort=False):
        group = group.sort_values("_t")
        last_kept_t = None
        for idx, row in group.iterrows():
            if last_kept_t is not None and (row["_t"] - last_kept_t).total_seconds() <= DUP_WINDOW_SECONDS:
                drop_idx.append(idx)
                extra_pnl += row[pnl_col]
            else:
                last_kept_t = row["_t"]

    cleaned = df.drop(index=drop_idx).sort_values("_orig_order").drop(columns=["_t", "_orig_order"])
    return cleaned.reset_index(drop=True), len(drop_idx), extra_pnl


def rebuild_balance(df: pd.DataFrame, pnl_col: str, balance_col: str, starting_balance: float) -> pd.DataFrame:
    """ستون موجودی بعد از هر رویداد را از صفر، فقط برای ردیف‌های close، بازسازی می‌کند."""
    df = df.copy()
    running = starting_balance
    new_balance_col = []
    for _, row in df.iterrows():
        if row["event_type"] == "close":
            running += float(row[pnl_col])
            new_balance_col.append(running)
        else:
            new_balance_col.append("")
    df[balance_col] = new_balance_col
    return df, running


def reconcile(bot_dir: str, starting_balance: float, dry_run_preview: bool = False) -> dict:
    csv_path = f"{bot_dir}/paper_trades_log.csv"
    state_path = f"{bot_dir}/state.json"

    df = pd.read_csv(csv_path)
    pnl_col, balance_col = _amount_col(df)
    balance_key = balance_col.replace("balance_after", "_paper_balance")

    cleaned, n_removed, extra_pnl = dedupe_closes(df, pnl_col)
    cleaned, final_balance = rebuild_balance(cleaned, pnl_col, balance_col, starting_balance)

    report = {
        "bot_dir": bot_dir,
        "rows_before": len(df),
        "rows_after": len(cleaned),
        "duplicate_rows_removed": n_removed,
        "double_counted_pnl": extra_pnl,
        "starting_balance": starting_balance,
        "final_balance_corrected": final_balance,
    }

    if not dry_run_preview:
        cleaned.to_csv(csv_path, index=False)
        state = json.load(open(state_path, encoding="utf-8"))
        old_balance = state.get(balance_key)
        state[balance_key] = final_balance
        report["state_balance_before"] = old_balance
        json.dump(state, open(state_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("bot_dir", help="مسیر پوشه‌ی بات، مثل bot1_live")
    parser.add_argument("--starting-balance", type=float, required=True)
    parser.add_argument("--dry-run", action="store_true", help="فقط گزارش بده، فایلی ننویس")
    args = parser.parse_args()

    rep = reconcile(args.bot_dir, args.starting_balance, dry_run_preview=args.dry_run)
    print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    if rep["duplicate_rows_removed"] == 0:
        print("✅ هیچ ردیف تکراری‌ای پیدا نشد - فایل‌ها دست‌نخورده موندن.")
    else:
        print(f"🔧 {rep['duplicate_rows_removed']} ردیف تکراری حذف شد و موجودی بازسازی شد.")
