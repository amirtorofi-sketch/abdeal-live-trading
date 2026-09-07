"""
لایه‌ی اتصال واقعی به صرافی تبدیل — بر پایه‌ی پکیج رسمی pip: tabdeal-python
(نه کلاینت دست‌ساز HTTP قبلی که مسیرهایش ASSUMED/حدسی بودند)

⚠️ این ماژول سفارش *واقعی* با پول واقعی ثبت می‌کند. قبل از فعال‌کردن اجرای
خودکار زمان‌بندی‌شده، حتماً یک‌بار با کمترین مبلغ ممکن و به‌صورت دستی تست کن
(بخش «تست قبل از روشن‌کردن» در README را ببین).

نکات مهم که هنوز با API واقعی تایید نشده‌اند (چون کلید API در دسترس نبود):
    ۱) اینکه بازارهای BTCIRT و ETHIRT واقعاً isMarginTradingAllowed=true دارند یا نه.
       (نمونه‌ی مستندات رسمی برای BTCIRT مقدار false نشان می‌دهد، ولی آن فقط یک
        مثال است، نه لزوماً وضعیت واقعی امروز بازار.)
    ۲) رفتار دقیق بازپرداخت خودکار وام (Repay) هنگام بستن پوزیشن با سفارش معکوس.
       فرض این کد (مطابق الگوی رایج صرافی‌های مشابه بایننس) این است که وقتی
       دارایی قرض‌گرفته‌شده را با سفارش معکوس برمی‌گردانی، بازپرداخت خودکار
       انجام می‌شود؛ این فرض باید بعد از اولین معامله‌ی واقعی با
       get_repays()/get_isolated_margin_account() بررسی و تایید شود.
هر دو مورد بالا در تابع verify_symbol_ready() این فایل چک می‌شوند تا بات
قبل از هر معامله خودش را از ثبت سفارش روی بازاری که مارجین ندارد متوقف کند.
"""

import os
import json
import math
from decimal import Decimal, ROUND_DOWN

from tabdeal.spot import Spot
from tabdeal.isolated_margin import IsolatedMargin
from tabdeal.exceptions import ClientException, ServerException, SecurityException
from tabdeal.enums import OrderSides, OrderTypes

# نگاشت رشته‌ی ساده به Enum رسمی پکیج (دقیقاً طبق نمونه‌ی README رسمی tabdeal-python)
_SIDE_MAP = {"BUY": OrderSides.BUY, "SELL": OrderSides.SELL}

def _float_env(name: str, default: str) -> float:
    """
    مثل os.environ.get ولی رشته‌ی خالی رو هم «تنظیم‌نشده» در نظر می‌گیره.
    لازمه چون گیت‌هاب اکشنز وقتی یه Variable وجود نداره، ${{ vars.X }} رو به
    رشته‌ی خالی resolve می‌کنه (نه اینکه اصلاً env var رو ست نکنه)، و
    os.environ.get(name, default) در اون حالت رشته‌ی خالی برمی‌گردونه، نه default.
    """
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else float(default)


API_KEY = os.environ.get("TABDEAL_API_KEY", "")
API_SECRET = os.environ.get("TABDEAL_API_SECRET", "")

# قفل ایمنی سراسری - پیش‌فرض همیشه "true" (آزمایشی/بی‌خطر) است.
# فقط با ست‌کردن صریح Variable گیت‌هابی TABDEAL_DRY_RUN=false، حالت زنده
# روشن می‌شود. این یعنی اگر این متغیر را فراموش کنی یا اصلاً تنظیمش نکنی،
# بات هرگز خودش را به‌طور خودکار در حالت زنده پیدا نمی‌کند - fail-safe.
DRY_RUN = os.environ.get("TABDEAL_DRY_RUN", "true").strip().lower() != "false"

# سقف امنیتی سخت — این کد هرگز بیشتر از این مبلغ (تومان) را به‌عنوان مارجین
# خودِ کاربر در یک معامله قفل نمی‌کند، حتی اگر مقدار اشتباه از جایی دیگر بیاید.
HARD_CAP_MARGIN_IRT = _float_env("TABDEAL_HARD_CAP_MARGIN_IRT", "500000")


class BrokerError(Exception):
    pass


def get_public_client() -> Spot:
    return Spot()


def get_margin_client() -> IsolatedMargin:
    if not API_KEY or not API_SECRET:
        raise BrokerError("TABDEAL_API_KEY / TABDEAL_API_SECRET ست نشده است.")
    return IsolatedMargin(API_KEY, API_SECRET)


def verify_symbol_ready(spot_client: Spot, spot_symbol: str) -> dict:
    """
    قبل از هر معامله چک می‌کند که بازار وجود دارد و در حال معامله است.
    اگر نبود، BrokerError پرتاب می‌کند تا بات هیچ سفارشی نزند.

    ⚠️ توجه: چک isMarginTradingAllowed/permissions عمداً اینجا مسدودکننده
    نیست — چون با تست واقعی مشخص شد این فیلدها اصلاً بازتاب‌دهنده‌ی «اهرم
    کلاسیک» تبدیل نیستن (احتمالاً یک فضای بازار جداست). فقط status=TRADING
    را چک می‌کنیم که همچنان معنادار و امنه.
    """
    info = spot_client.exchange_info(symbols=[spot_symbol])
    markets = info.get("symbols", info) if isinstance(info, dict) else info
    if not markets:
        raise BrokerError(f"بازار {spot_symbol} در exchange_info پیدا نشد.")
    market = markets[0]
    if market.get("status") != "TRADING":
        raise BrokerError(f"بازار {spot_symbol} در وضعیت TRADING نیست (status={market.get('status')}).")
    return market


def _get_margin_assets_index(_cache={}) -> dict:
    """
    endpoint اختصاصی مارجین (نه اسپات) - Security=NONE یعنی نیاز به API Key
    نداره. طبق تست واقعی، هر ردیفش یه دارایی درگیر یک بازار مارجینه (هم پایه
    هم مظنه، هر کدوم ردیف جدا) و شامل symbol (سبک بایننس، مثل BTCUSDT),
    tabdealSymbol (زیرخط‌دار، مثل BTC_USDT), maxLeverage, isBorrowable و... هست.

    این تابع کل لیست را یک‌بار می‌گیرد (کش می‌شود) و بر اساس symbol ایندکس
    می‌کند تا بشه سریع فهمید یک بازار خاص (مثلاً BTCIRT) اصلاً در این فهرست
    مارجین هست یا نه - این دقیق‌تر از exchange_info اسپاته که معلوم شد
    isMarginTradingAllowed/permissions درستی برای اهرم کلاسیک برنمی‌گردونه.
    """
    if "index" in _cache:
        return _cache["index"]
    index = {}
    try:
        margin_client = IsolatedMargin(api_key=None, api_secret=None)
        assets = margin_client.get_all_assets()
        for row in assets or []:
            sym = row.get("symbol")
            if sym:
                index.setdefault(sym, []).append(row)
        irt_symbols = sorted(s for s in index if s.endswith("IRT"))
        print(f"🔍 get_all_assets(): {len(assets or [])} ردیف خام، {len(index)} نماد یکتا، "
              f"{len(irt_symbols)} نماد ختم‌شده به IRT -> {irt_symbols}")
    except Exception as e:
        print(f"🔍 get_all_assets() شکست خورد: {type(e).__name__}: {e}")
    _cache["index"] = index
    return index


def discover_irt_margin_symbols(spot_client: Spot, candidate_bases: list) -> dict:
    """
    ⚠️ فقط از endpointهای عمومی استفاده می‌کند (بدون نیاز به API Key).

    منبع اصلی تشخیص «مارجین‌دار بودن» حالا get_all_assets() است (نه
    exchange_info اسپات، که معلوم شد isMarginTradingAllowed/permissions
    نادرستی برمی‌گردونه). یک بازار وقتی واجد شرایط است که symbol آن
    (مثل "BTCIRT") در فهرست get_all_assets() پیدا بشه.

    خروجی: {base: {"spot": "BTCIRT", "margin": "BTC_IRT", "max_leverage": "10.0"}}
    """
    margin_index = _get_margin_assets_index()

    ready = {}
    diagnostics = []
    for base in candidate_bases:
        spot_symbol = f"{base}IRT"
        rows = margin_index.get(spot_symbol)
        if not rows:
            diagnostics.append(f"  {spot_symbol}: توی get_all_assets پیدا نشد -> مارجین/اهرم کلاسیک نداره.")
            continue
        margin_symbol = rows[0].get("tabdealSymbol") or f"{base}_IRT"
        max_leverage = rows[0].get("maxLeverage")
        ready[base] = {"spot": spot_symbol, "margin": margin_symbol, "max_leverage": max_leverage}

    if ready:
        print(f"✅ discover_irt_margin_symbols: {len(ready)} نماد واقعاً مارجین‌دار -> {ready}")
    if diagnostics:
        print("🔍 نمادهایی که مارجین ندارن:")
        for line in diagnostics:
            print(line)

    return ready

def get_mid_price(spot_client: Spot, symbol: str) -> float:
    """قیمت لحظه‌ای تقریبی = میانگین بهترین Bid/Ask از دفتر سفارش."""
    book = spot_client.depth(symbol=symbol, limit=5)
    best_bid = float(book["bids"][0][0])
    best_ask = float(book["asks"][0][0])
    return (best_bid + best_ask) / 2.0


def extract_real_price(order: dict, fallback_price: float) -> float:
    """
    قیمت واقعی اجراشده را از پاسخ سفارش استخراج می‌کند.

    ⚠️ نکته‌ی مهم: در سفارش‌های MARKET سبک بایننس (که تبدیل هم از آن کپی کرده)،
    فیلد بالادستی "price" معمولاً "0.00000000" است و قیمت واقعی اجراشده در
    آرایه‌ی "fills" (میانگین وزنی) قرار دارد. اگر این حالت را در نظر نگیریم،
    entry ممکن است صفر ثبت شود و کل محاسبه‌ی SL/TP/PnL بعدی خراب شود. این تابع
    هنوز با پاسخ واقعی API تبدیل تست نشده - در اولین معامله‌ی واقعی، مقدار
    برگشتی را با پنل تبدیل مقایسه کن.
    """
    fills = order.get("fills")
    if fills:
        total_qty = sum(float(f["qty"]) for f in fills)
        if total_qty > 0:
            weighted = sum(float(f["price"]) * float(f["qty"]) for f in fills)
            return weighted / total_qty

    raw_price = order.get("price")
    try:
        raw_price_f = float(raw_price) if raw_price is not None else 0.0
    except (TypeError, ValueError):
        raw_price_f = 0.0
    if raw_price_f > 0:
        return raw_price_f

    planned = order.get("_planned_price")
    if planned:
        return float(planned)

    return float(fallback_price)


def get_market_info(spot_client: Spot, symbol: str) -> dict:
    info = spot_client.exchange_info(symbols=[symbol])
    markets = info.get("symbols", info) if isinstance(info, dict) else info
    if not markets:
        raise BrokerError(f"بازار {symbol} در exchange_info پیدا نشد.")
    return markets[0]


def split_into_two_lots(market: dict, quantity: float) -> tuple:
    """
    مقدار کل پوزیشن را برای معماری دو-لاتی (TP1 نیمی + TP2 نیمی، مطابق
    trading_bot.py اصلی) به دو نیمه تقسیم می‌کند - با رعایت LOT_SIZE بازار.

    اگر حجم آنقدر کوچک باشد که بعد از رند-کردن به step، یکی از دو نیمه صفر
    شود، تقسیم انجام نمی‌شود و (quantity, 0.0) برگردانده می‌شود؛ فراخوان باید
    در این حالت پوزیشن را تک‌لاتی (فقط هدف TP2) در نظر بگیرد.
    """
    step = get_lot_step(market)
    half = _round_step(quantity / 2, step)
    if half <= 0:
        return quantity, 0.0
    remainder = _round_step(quantity - half, step)
    if remainder <= 0:
        return quantity, 0.0
    return half, remainder


def _round_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    d = Decimal(str(value)).quantize(Decimal(str(step)), rounding=ROUND_DOWN)
    return float(d)


def get_lot_step(market: dict) -> float:
    for f in market.get("filters", []):
        if f.get("filterType") == "LOT_SIZE":
            return float(f.get("stepSize", "0.00000001"))
    return 0.00000001


def plan_order(market: dict, side: str, price: float, own_margin_irt: float, leverage: float) -> dict:
    """
    محاسبه‌ی quantity و borrow_quantity برای create_margin_order.
    side: "BUY" (لانگ) یا "SELL" (شورت)
    - BUY: قرض به ارز دوم (IRT) گرفته می‌شود.
    - SELL: قرض به ارز اول (مثلاً BTC/ETH) گرفته می‌شود.
    """
    own_margin_irt = min(own_margin_irt, HARD_CAP_MARGIN_IRT)
    notional_irt = own_margin_irt * leverage
    step = get_lot_step(market)
    quantity = _round_step(notional_irt / price, step)
    if quantity <= 0:
        raise BrokerError("مقدار محاسبه‌شده برای سفارش صفر یا منفی شد — own_margin/leverage/price را چک کن.")

    if side == "BUY":
        total_quote_needed = quantity * price
        borrow_quantity = max(0.0, total_quote_needed - own_margin_irt)
    else:  # SELL / short
        own_qty_equiv = own_margin_irt / price
        borrow_quantity = max(0.0, quantity - own_qty_equiv)

    return {
        "quantity": f"{quantity:.8f}".rstrip("0").rstrip("."),
        "borrow_quantity": f"{borrow_quantity:.8f}".rstrip("0").rstrip("."),
        "notional_irt": notional_irt,
    }


def open_margin_position(spot_symbol: str, margin_symbol: str, side: str, own_margin_irt: float, leverage: float, logger=print) -> dict:
    """
    یک پوزیشن مارجین ایزوله واقعی باز می‌کند (side: BUY برای لانگ، SELL برای شورت).
    اگر DRY_RUN=true باشد، فقط پلن سفارش را چاپ/برمی‌گرداند و هیچ سفارشی نمی‌فرستد.

    spot_symbol (مثل "BTCIRT") برای گرفتن قیمت/فیلترهای بازار از endpointهای
    اسپات استفاده می‌شود. margin_symbol (مثل "BTC_IRT"، با زیرخط) دقیقاً همان
    چیزیه که به create_margin_order داده می‌شود - چون طبق کد رسمی پکیج، هر
    نمادی که زیرخط داشته باشه به‌عنوان فیلد جدای tabdealSymbol فرستاده می‌شه.
    """
    spot_client = get_public_client()
    market = verify_symbol_ready(spot_client, spot_symbol)
    price = get_mid_price(spot_client, spot_symbol)
    plan = plan_order(market, side, price, own_margin_irt, leverage)

    logger(f"[tabdeal] پلن سفارش {margin_symbol} side={side} price≈{price} -> {plan}")

    if DRY_RUN:
        logger("[tabdeal] DRY_RUN فعاله — سفارش واقعی ارسال نشد.")
        return {"dry_run": True, "symbol": margin_symbol, "side": side, "price": price, **plan}

    margin_client = get_margin_client()
    try:
        order = margin_client.create_margin_order(
            symbol=margin_symbol,
            side=_SIDE_MAP[side],
            type=OrderTypes.MARKET,
            quantity=plan["quantity"],
            borrow_quantity=plan["borrow_quantity"],
        )
    except SecurityException as e:
        raise BrokerError(f"خطای امنیتی هنگام ثبت سفارش: {e}")
    except (ClientException, ServerException) as e:
        raise BrokerError(f"خطای تبدیل هنگام ثبت سفارش ({margin_symbol}, {side}): {e}")

    order["_planned_price"] = price
    order["_notional_irt"] = plan["notional_irt"]
    return order


def close_margin_position(margin_symbol: str, opposite_side: str, quantity: str, logger=print) -> dict:
    """
    بستن پوزیشن با سفارش معکوس (MARKET). margin_symbol باید همون فرمت زیرخط‌دار
    باشه (مثل "BTC_IRT") که موقع باز کردن پوزیشن استفاده شد.
    فرض بر این است که بازپرداخت وام خودکار انجام می‌شود — این فرض را حتماً بعد
    از اولین معامله با get_isolated_margin_account() بررسی کن.
    """
    if DRY_RUN:
        logger(f"[tabdeal] DRY_RUN فعاله — بستن پوزیشن {margin_symbol} شبیه‌سازی شد (side={opposite_side}, qty={quantity}).")
        return {"dry_run": True, "symbol": margin_symbol, "side": opposite_side, "quantity": quantity}

    margin_client = get_margin_client()
    try:
        order = margin_client.create_margin_order(
            symbol=margin_symbol,
            side=_SIDE_MAP[opposite_side],
            type=OrderTypes.MARKET,
            quantity=quantity,
            borrow_quantity="0",
        )
    except SecurityException as e:
        raise BrokerError(f"خطای امنیتی هنگام بستن پوزیشن: {e}")
    except (ClientException, ServerException) as e:
        raise BrokerError(f"خطای تبدیل هنگام بستن پوزیشن ({margin_symbol}): {e}")
    return order
