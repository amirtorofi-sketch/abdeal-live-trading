"""
اسکریپت کشفِ بازارهای تومانی مارجین‌دار روی تبدیل — بدون نیاز به API Key.

این اسکریپت را همین الان (قبل از گرفتن API Key) اجرا کن تا ببینی از بین
نمادهای دو استراتژی، الان کدوم‌ها روی تبدیل بازار تومانی (IRT) دارن و
مارجین‌شون فعاله:

    python discover_markets.py
"""

from common.tabdeal_broker import get_public_client, discover_irt_margin_symbols

# ارزهای پایه‌ی هر دو استراتژی (از روی SYMBOLS در signal_bot.py و signal_bot_v2.py)
BOT1_BASES = ["BTC", "ETH", "SOL", "BNB", "DOGE", "CRV", "ROSE", "CHZ", "ONE", "VET", "MASK", "MANA", "GALA"]
BOT2_BASES = ["BTC", "ETH", "SOL", "BNB", "DOGE"]  # EUR/PAXG بی‌معنی‌ان چون معادل تومانی مستقیم ندارن


def main():
    client = get_public_client()

    print("=" * 60)
    print("بررسی نمادهای بات ۱ (Supertrend+ADX) روی بازار تومانی تبدیل")
    print("=" * 60)
    ready1 = discover_irt_margin_symbols(client, BOT1_BASES)
    for base in BOT1_BASES:
        status = "✅ فعال (مارجین دارد)" if base in ready1 else "❌ ندارد / مارجین غیرفعال"
        print(f"  {base}IRT: {status}")

    print()
    print("=" * 60)
    print("بررسی نمادهای بات ۲ (ICT/SMC v2) روی بازار تومانی تبدیل")
    print("=" * 60)
    ready2 = discover_irt_margin_symbols(client, BOT2_BASES)
    for base in BOT2_BASES:
        status = "✅ فعال (مارجین دارد)" if base in ready2 else "❌ ندارد / مارجین غیرفعال"
        print(f"  {base}IRT: {status}")

    print()
    print("نتیجه‌ی نهایی (این‌ها را می‌توان در حالت آزمایشی/زنده استفاده کرد):")
    print("  بات ۱:", list(ready1.values()) or "هیچ‌کدام")
    print("  بات ۲:", list(ready2.values()) or "هیچ‌کدام")


if __name__ == "__main__":
    main()
