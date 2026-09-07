"""ارسال پیام تلگرام - مشترک بین هر دو بات زنده."""
import requests


def send_telegram(token: str, chat_id_csv: str, text: str):
    if not token or not chat_id_csv:
        print("توکن یا چت‌آیدی تنظیم نشده. پیام ارسال نشد:\n", text)
        return
    chat_ids = [c.strip() for c in chat_id_csv.split(",") if c.strip()]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chat_id in chat_ids:
        try:
            r = requests.post(url, data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=15)
            r.raise_for_status()
        except Exception as e:
            print(f"خطا در ارسال پیام تلگرام به {chat_id}:", e)
