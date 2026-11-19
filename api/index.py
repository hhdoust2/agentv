from fastapi import FastAPI, Request, Response
import os, time, json, httpx, traceback, asyncio, logging, ipaddress, socket, subprocess, sys
import re, html
from urllib.parse import urlparse, urljoin
import inspect
from datetime import datetime, timezone, timedelta
from openai import OpenAI
from tavily import TavilyClient
from bs4 import BeautifulSoup

logging.getLogger("httpx").setLevel(logging.WARNING)  # جلوگیری از چاپ توکن ربات در لاگ

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
MODEL_NAME = os.getenv("MODEL_NAME", "gpt-4o")
TAVILY_KEY = os.getenv("TAVILY_API_KEY")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")        # اختیاری ولی توصیه می‌شود
ENABLE_PYTHON = os.getenv("ENABLE_PYTHON", "1") == "1"  # مثل قبل روشن؛ برای خاموش کردن: 0
try: ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
except ValueError: ALLOWED_USER_ID = 0
REDIS_URL = os.getenv("REDIS_URL") or os.getenv("KV_URL")
KV_URL = os.getenv("KV_REST_API_URL") or os.getenv("UPSTASH_REDIS_REST_URL")
KV_TOKEN = os.getenv("KV_REST_API_TOKEN") or os.getenv("UPSTASH_REDIS_REST_TOKEN")
try: HISTORY_LEN = int(os.getenv("HISTORY_LEN", "6"))
except ValueError: HISTORY_LEN = 6

print(f"REDIS_URL: {bool(REDIS_URL)} | KV_URL: {bool(KV_URL)}")

# --- DB ---
_redis = None
_seen = set()

def _r():
    global _redis
    if _redis is None:
        import redis
        _redis = redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=5, socket_connect_timeout=5, health_check_interval=30)
    return _redis

def _reset():
    global _redis
    _redis = None

def _use_redis(): return bool(REDIS_URL and REDIS_URL.startswith("redis"))
def _use_kv(): return bool(KV_URL and KV_TOKEN and KV_URL.startswith("http"))
def _kv(cmd):
    resp = httpx.post(KV_URL, headers={"Authorization": f"Bearer {KV_TOKEN}"}, json=cmd, timeout=10)
    return resp.json().get("result")

def db_get(key, default):
    if _use_redis():
        try:
            v = _r().get(key)
            return json.loads(v) if v else default
        except Exception as e: print(f"DB GET REDIS err: {e}"); _reset()
    if _use_kv():
        try:
            res = _kv(["GET", key])
            return json.loads(res) if res else default
        except Exception as e: print(f"DB GET KV err: {e}")
    return default

def db_set(key, value):
    val_str = json.dumps(value, ensure_ascii=False)
    if _use_redis():
        try:
            _r().set(key, val_str); print(f"DB SET REDIS {key}: ok"); return
        except Exception as e: print(f"DB SET REDIS err: {e}"); _reset()
    if _use_kv():
        try:
            _kv(["SET", key, val_str]); print(f"DB SET KV {key}: ok"); return
        except Exception as e: print(f"DB SET KV err: {e}")

def is_new_update(uid):
    """جلوگیری از پردازش دوباره‌ی یک پیام وقتی تلگرام دوباره می‌فرستد."""
    if uid is None: return True
    k = f"upd:{uid}"
    try:
        if _use_redis(): return bool(_r().set(k, "1", nx=True, ex=600))
        if _use_kv(): return _kv(["SET", k, "1", "NX", "EX", "600"]) == "OK"
    except Exception as e: print(f"DEDUP err: {e}"); _reset()
    if k in _seen: return False
    _seen.add(k); return True

_clients = {}

def get_cfg():
    """تنظیمات ذخیره‌شده داخل ربات اولویت دارد؛ اگر نبود از Environment Variables."""
    st = db_get("settings", {}) or {}
    return {"base_url": st.get("base_url") or BASE_URL,
            "model": st.get("model") or MODEL_NAME,
            "api_key": st.get("api_key") or OPENAI_KEY,
            "saved": [k for k in ("base_url", "model", "api_key") if st.get(k)]}

def get_client(cfg):
    k = (cfg["base_url"], cfg["api_key"])
    if k not in _clients:
        _clients[k] = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"], max_retries=0)
    return _clients[k]

def set_setting(name, value):
    st = db_get("settings", {}) or {}
    if value: st[name] = value
    else: st.pop(name, None)
    db_set("settings", st)

def mask(k):
    return "—" if not k else (k[:3] + "..." + k[-4:] if len(k) > 10 else "***")
tavily = TavilyClient(api_key=TAVILY_KEY) if TAVILY_KEY else None

# --- TOOLS ---
def search_web(query: str):
    if not tavily: return "Tavily API Key تنظیم نشده"
    try:
        res = tavily.search(query=query, max_results=5, include_answer=True, search_depth="advanced")
        out = f"Answer: {res.get('answer','')}\n\n"
        for r in res.get('results', []):
            out += f"- {r.get('title','')}: {r.get('content','')[:500]}\nURL: {r.get('url','')}\n"
        return out[:5000]
    except Exception as e: return f"خطا در جستجو: {e}"

def _safe_url(url: str) -> bool:
    """جلوگیری از باز کردن آدرس‌های داخلی/خصوصی (SSRF)."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname: return False
    try: infos = socket.getaddrinfo(p.hostname, None)
    except Exception: return False
    for i in infos:
        ip = ipaddress.ip_address(i[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast: return False
    return True

def browse_url(url: str):
    try:
        with httpx.Client(timeout=10, headers={"User-Agent": "Mozilla/5.0"}) as c:
            for _ in range(5):
                if not _safe_url(url): return "خطا: این آدرس مجاز نیست"
                r = c.get(url, follow_redirects=False)
                if r.is_redirect and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"]); continue
                break
            else: return "خطا: ریدایرکت بیش از حد"
        soup = BeautifulSoup(r.text[:400000], 'html.parser')
        for s in soup(["script", "style", "nav", "footer"]): s.decompose()
        lines = [l.strip() for l in soup.get_text(separator="\n").splitlines() if l.strip()]
        return "\n".join(lines)[:6000]
    except Exception as e: return f"خطا در باز کردن URL: {e}"

def execute_python(code: str):
    if not ENABLE_PYTHON: return "اجرای کد غیرفعال است"
    try:
        p = subprocess.run([sys.executable or "python3", "-I", "-c", code], capture_output=True, text=True,
                           timeout=20, env={"PATH": "/usr/local/bin:/usr/bin:/bin"})
        val = (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired: return "خطا: اجرای کد بیش از ۲۰ ثانیه طول کشید"
    except OSError as e:
        # دیگر fallback به exec داخل خود پروسس نیست: آن مسیر ایزولاسیون subprocess
        # را دور می‌زد و کد مدل را با دسترسی کامل سرور اجرا می‌کرد.
        return f"خطا: اجرای جداگانه‌ی کد در دسترس نیست ({e})؛ کد اجرا نشد."
    return val[:4000] if val else "کد بدون خروجی اجرا شد ✅"

def manage_task(action: str, task: str = "", task_id: int = 0):
    tasks = db_get("tasks", [])
    if action == "add":
        nid = max([t["id"] for t in tasks], default=0) + 1
        tasks.append({"id": nid, "task": task, "status": "pending"})
        db_set("tasks", tasks)
        return f"تسک اضافه شد: {task}"
    if action == "list":
        if not tasks: return "هیچ تسکی وجود ندارد"
        return "\n".join([f"{t['id']}. {t['task']} [{t['status']}]" for t in tasks])
    if action == "done":
        for t in tasks:
            if t['id'] == task_id: t['status'] = "done"
        db_set("tasks", tasks)
        return f"تسک {task_id} انجام شد"
    if action == "delete":
        tasks = [t for t in tasks if t['id'] != task_id]
        db_set("tasks", tasks)
        return f"تسک {task_id} حذف شد"
    return "عملیات نامعتبر"

def remember_info(key: str, value: str = "", action: str = "save"):
    m = db_get("memory", {})
    if action == "save":
        m[key] = value
        db_set("memory", m)
        return f"ذخیره شد {key}={value}"
    if action == "get": return m.get(key, "یافت نشد")
    if action == "list": return json.dumps(m, ensure_ascii=False, indent=2) if m else "حافظه خالی است"
    return "خطا"

def file_store(action: str, filename: str = "", content: str = ""):
    files = db_get("files", {})
    if action == "write":
        files[filename] = content
        db_set("files", files)
        return f"فایل {filename} ذخیره شد"
    if action == "read": return files.get(filename, "فایل یافت نشد")
    if action == "list": return ", ".join(files.keys()) if files else "فایلی نیست"
    return "نامعتبر"

tools = [
 {"type":"function","function":{"name":"search_web","description":"جستجوی پیشرفته وب برای تحقیق عمیق","parameters":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}}},
 {"type":"function","function":{"name":"browse_url","description":"باز کردن و خواندن محتوای یک URL","parameters":{"type":"object","properties":{"url":{"type":"string"}},"required":["url"]}}},
 {"type":"function","function":{"name":"execute_python","description":"نوشتن و اجرای کد پایتون و برگرداندن خروجی","parameters":{"type":"object","properties":{"code":{"type":"string"}},"required":["code"]}}},
 {"type":"function","function":{"name":"manage_task","description":"مدیریت تسک پروژه - فقط وقتی کاربر صریحا تسک گفت","parameters":{"type":"object","properties":{"action":{"type":"string","enum":["add","list","done","delete"]},"task":{"type":"string"},"task_id":{"type":"integer"}},"required":["action"]}}},
 {"type":"function","function":{"name":"remember_info","description":"حافظه بلندمدت کاربر","parameters":{"type":"object","properties":{"key":{"type":"string"},"value":{"type":"string"},"action":{"type":"string","enum":["save","get","list"]}},"required":["key","action"]}}},
 {"type":"function","function":{"name":"file_store","description":"ذخیره و خواندن فایل متنی در دیتابیس","parameters":{"type":"object","properties":{"action":{"type":"string","enum":["write","read","list"]},"filename":{"type":"string"},"content":{"type":"string"}},"required":["action","filename"]}}},
]
if not ENABLE_PYTHON:
    tools = [t for t in tools if t["function"]["name"] != "execute_python"]

SYSTEM_PROMPT = """
تو یک ایجنت هوشمند و خودمختار هستی.
قوانین:
- برای کارهای پیچیده چند مرحله فکر کن و چند ابزار پشت سر هم صدا بزن
- فقط وقتی کاربر گفت "تسک" یا "وظیفه" از manage_task استفاده کن، برای "تحقیق کن" فقط search_web و browse_url بزن
- اگر کاربر گفت "تحقیق کن" اول search_web بعد browse_url بزن بعد تحلیل کن
- اگر کد یا تحلیل دیتا خواست با execute_python بنویس و اجرا کن
- حافظه را با remember_info مدیریت کن
- همیشه فارسی، دقیق و کاربردی جواب بده
- هرگز نگو نمی‌توانم، تلاش کن
- فرمت جواب برای تلگرام (مهم — خروجی خام در تلگرام به‌هم می‌ریزد):
  • فارسی، کوتاه و بخش‌بندی‌شده؛ تیتر را فقط با **متن** بنویس، هرگز با # یا ##
  • جدول Markdown ننویس (در تلگرام جدول رندر نمی‌شود)؛ به‌جای هر ردیف جدول یک بولت بنویس: • نام: مقدار
  • کد را فقط داخل بلاک ```زبان ... ``` کامل و قابل‌اجرا بنویس؛ وسط کد «...» یا خلاصه نگذار
  • اول خلاصه/نتیجه، بعد جزئیات؛ اگر جواب خیلی طولانی شد مهم‌ترین‌ها اول باشند
  • لینک را به‌صورت [متن](https://...) بنویس، نه URL لخت وسط جمله
"""

# ---------- مهارت‌های کدی (ابزارهای واقعی) ----------
# برای اضافه کردن یک ابزار جدید فقط یک تابع با دکوراتور @code_tool بنویس؛ بقیه‌ی کارها خودکار است.
CODE_TOOLS = {}

def code_tool(name, desc, props, required, label):
    def deco(fn):
        CODE_TOOLS[name] = {"fn": fn, "label": label, "desc": desc, "params": list(props.keys()),
            "schema": {"type": "function", "function": {"name": name, "description": desc,
                       "parameters": {"type": "object", "properties": props, "required": required}}}}
        return fn
    return deco

def call_code_tool(name, args):
    t = CODE_TOOLS[name]
    ok = inspect.signature(t["fn"]).parameters
    try: return str(t["fn"](**{k: v for k, v in (args or {}).items() if k in ok}))
    except Exception as e:
        print(f"CODE TOOL {name} err: {e}")
        return f"خطا در ابزار {name}: {e}"

def cli_tool(name, rest):
    """برای تست مستقیم از چت: /tool نام آرگومان   یا   /tool نام {json}"""
    t = CODE_TOOLS.get(name)
    if not t: return "ابزاری با این نام نیست. لیست: /tools"
    rest = (rest or "").strip()
    try:
        args = json.loads(rest) if rest.startswith("{") else ({t["params"][0]: rest} if rest and t["params"] else {})
    except Exception as e: return f"JSON نامعتبر: {e}"
    return call_code_tool(name, args)

_UA = {"User-Agent": "Mozilla/5.0"}

def _get(url, **params):
    r = httpx.get(url, params=params or None, timeout=10, headers=_UA)
    r.raise_for_status()
    return r.json()

def _g2j(gy, gm, gd):
    gdm = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400 + gd + gdm[gm - 1]
    jy = -1595 + 33 * (days // 12053); days %= 12053
    jy += 4 * (days // 1461); days %= 1461
    if days > 365: jy += (days - 1) // 365; days = (days - 1) % 365
    if days < 186: jm = 1 + days // 31; jd = 1 + days % 31
    else: jm = 7 + (days - 186) // 30; jd = 1 + (days - 186) % 30
    return jy, jm, jd

_J_MONTHS = ["فروردین","اردیبهشت","خرداد","تیر","مرداد","شهریور","مهر","آبان","آذر","دی","بهمن","اسفند"]
_WEEKDAYS = ["دوشنبه","سه‌شنبه","چهارشنبه","پنجشنبه","جمعه","شنبه","یکشنبه"]

@code_tool("current_datetime", "تاریخ و ساعت دقیق همین الان (میلادی و شمسی). برای هر سؤال درباره‌ی امروز/ساعت/تاریخ از این ابزار استفاده کن.",
           {"timezone": {"type": "string", "description": "مثل Asia/Tehran (پیش‌فرض)"}}, [], "🕒 در حال گرفتن زمان...")
def current_datetime(timezone: str = "Asia/Tehran"):
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(timezone))
    except Exception:
        now = datetime.now(__import__("datetime").timezone(timedelta(hours=3, minutes=30)))
        timezone = "Asia/Tehran (UTC+3:30)"
    jy, jm, jd = _g2j(now.year, now.month, now.day)
    return (f"منطقه‌ی زمانی: {timezone}\n"
            f"میلادی: {now.strftime('%Y-%m-%d')} ساعت {now.strftime('%H:%M')}\n"
            f"شمسی: {_WEEKDAYS[now.weekday()]} {jd} {_J_MONTHS[jm-1]} {jy}")

def _fmt(x):
    x = float(x)
    if x >= 1: return f"{x:,.2f}"
    if x == 0: return "0"
    # ۸ رقم اعشار تا قیمت‌های خیلی ریز (مثلاً 0.00000012) به "0" تبدیل نشوند
    return f"{x:.8f}".rstrip("0").rstrip(".")

@code_tool("crypto_price", "قیمت لحظه‌ای و آمار ۲۴ ساعته‌ی یک ارز دیجیتال (دلار). برای هر سؤال قیمت کریپتو حتماً از این ابزار استفاده کن و از حافظه عدد نگو.",
           {"symbol": {"type": "string", "description": "نماد مثل BTC یا ADA یا ETH"}}, ["symbol"], "💰 در حال گرفتن قیمت...")
def crypto_price(symbol: str):
    sym = symbol.upper().replace("/", "").replace("-", "").replace(" ", "")
    pair = sym if sym.endswith(("USDT", "USDC")) else sym + "USDT"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    try:
        d = _get("https://api.mexc.com/api/v3/ticker/24hr", symbol=pair)
        last, op = float(d["lastPrice"]), float(d["openPrice"])
        ch = (last - op) / op * 100 if op else 0
        return (f"{pair} (MEXC) — {now}\nقیمت: {_fmt(last)} دلار\nتغییر ۲۴ساعته: {ch:+.2f}%\n"
                f"بالاترین ۲۴ساعت: {_fmt(d['highPrice'])}\nپایین‌ترین ۲۴ساعت: {_fmt(d['lowPrice'])}\n"
                f"حجم ۲۴ساعت: {_fmt(d.get('quoteVolume', 0))} دلار")
    except Exception as e:
        print(f"MEXC err: {e}; trying CoinGecko")
    base = pair[:-4]
    res = _get("https://api.coingecko.com/api/v3/search", query=base)
    coins = [c for c in res.get("coins", []) if c.get("symbol", "").upper() == base] or res.get("coins", [])
    if not coins: return f"ارز {base} پیدا نشد."
    markets = _get("https://api.coingecko.com/api/v3/coins/markets", vs_currency="usd", ids=coins[0]["id"])
    if not markets: return f"قیمت {base} از CoinGecko دریافت نشد."
    m = markets[0]
    return (f"{m['name']} ({m['symbol'].upper()}) (CoinGecko) — {now}\nقیمت: {_fmt(m['current_price'])} دلار\n"
            f"تغییر ۲۴ساعته: {m.get('price_change_percentage_24h') or 0:+.2f}%\n"
            f"بالاترین/پایین‌ترین ۲۴ساعت: {_fmt(m['high_24h'])} / {_fmt(m['low_24h'])}\n"
            f"ارزش بازار: {_fmt(m['market_cap'])} دلار")

_WCODE = {0: "آسمان صاف", 1: "تقریباً صاف", 2: "کمی ابری", 3: "ابری", 45: "مه", 48: "مه یخی",
          51: "نمنم باران", 53: "نمنم باران", 55: "نمنم باران", 61: "باران ملایم", 63: "باران", 65: "باران شدید",
          71: "برف ملایم", 73: "برف", 75: "برف شدید", 80: "رگبار", 81: "رگبار", 82: "رگبار شدید", 95: "رعد و برق", 96: "رعد و تگرگ", 99: "رعد و تگرگ"}

@code_tool("weather", "وضعیت هوای فعلی و پیش‌بینی ۳ روز آینده‌ی یک شهر.",
           {"city": {"type": "string", "description": "نام شهر؛ فارسی یا انگلیسی"}}, ["city"], "🌤 در حال گرفتن آب‌وهوا...")
def weather(city: str):
    g = _get("https://geocoding-api.open-meteo.com/v1/search", name=city, count=1, language="fa")
    if not g.get("results"): return f"شهر «{city}» پیدا نشد."
    c = g["results"][0]
    w = _get("https://api.open-meteo.com/v1/forecast", latitude=c["latitude"], longitude=c["longitude"],
             current="temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code",
             daily="temperature_2m_max,temperature_2m_min,precipitation_probability_max", timezone="auto", forecast_days=3)
    cur, dl = w["current"], w["daily"]
    out = (f"{c.get('name')}، {c.get('country','')}\nالان: {cur['temperature_2m']}°C، {_WCODE.get(cur['weather_code'], 'نامشخص')}، "
           f"رطوبت {cur['relative_humidity_2m']}٪، باد {cur['wind_speed_10m']} km/h\nپیش‌بینی:")
    for i, day in enumerate(dl["time"]):
        out += f"\n• {day}: {dl['temperature_2m_min'][i]} تا {dl['temperature_2m_max'][i]}°C، احتمال بارش {dl['precipitation_probability_max'][i]}٪"
    return out

@code_tool("usdt_toman", "قیمت لحظه‌ای تتر (USDT) به تومان از بازار ایران (نوبیتکس).", {}, [], "💵 در حال گرفتن قیمت تتر...")
def usdt_toman():
    d = _get("https://api.nobitex.ir/market/stats", srcCurrency="usdt", dstCurrency="rls")
    st = (d.get("stats") or {}).get("usdt-rls") or {}
    if not st.get("latest"): return "قیمت تتر از نوبیتکس دریافت نشد."
    t = lambda k: f"{int(float(st[k]) / 10):,}" if st.get(k) else "—"
    return (f"تتر/تومان (Nobitex) — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
            f"آخرین قیمت: {t('latest')} تومان\nبهترین خرید: {t('bestBuy')} | بهترین فروش: {t('bestSell')}\n"
            f"بالاترین/پایین‌ترین امروز: {t('dayHigh')} / {t('dayLow')}")

tools.extend(t["schema"] for t in CODE_TOOLS.values())

# ---------- مهارت‌ها (دستورالعمل‌هایی که خودت می‌نویسی) ----------
MAX_SKILLS = 30

def get_skills():
    sk = db_get("skills", {}) or {}
    return sk if isinstance(sk, dict) else {}

def parse_addskill(raw):
    """فرمت: /addskill نام | توضیح کوتاه  (خط بعد به بعد = دستورالعمل)  یا همه در یک خط با | """
    rest = raw.split(None, 1)[1] if len(raw.split(None, 1)) > 1 else ""
    if "\n" in rest:
        head, body = rest.split("\n", 1)
        parts = [x.strip() for x in head.split("|", 1)]
        name, desc = parts[0], (parts[1] if len(parts) > 1 else "")
    else:
        parts = [x.strip() for x in rest.split("|", 2)]
        name = parts[0] if parts else ""
        desc = parts[1] if len(parts) > 1 else ""
        body = parts[2] if len(parts) > 2 else ""
    return name.strip(), desc.strip(), body.strip()

def add_skill(name, desc, body):
    if not name or " " in name or len(name) > 40: return "❌ نام مهارت باید یک کلمه (بدون فاصله) و حداکثر ۴۰ حرف باشد."
    if not desc or len(desc) > 200: return "❌ توضیح کوتاه لازم است (حداکثر ۲۰۰ حرف)."
    if not body or len(body) > 6000: return "❌ دستورالعمل لازم است (حداکثر ۶۰۰۰ حرف)."
    sk = get_skills()
    if name not in sk and len(sk) >= MAX_SKILLS: return f"❌ حداکثر {MAX_SKILLS} مهارت."
    new = name not in sk
    sk[name] = {"desc": desc, "body": body}
    db_set("skills", sk)
    return f"✅ مهارت «{name}» {'اضافه' if new else 'به‌روز'} شد."

def del_skill(name):
    sk = get_skills()
    if name not in sk: return "❌ چنین مهارتی نیست."
    sk.pop(name); db_set("skills", sk)
    return f"🗑 مهارت «{name}» حذف شد."

def use_skill(name):
    sk = get_skills()
    if name in sk: return sk[name]["body"]
    return "مهارت پیدا نشد. مهارت‌های موجود: " + ", ".join(sk.keys())

def skills_prompt(sk):
    if not sk: return ""
    lines = "\n".join(f"- {n}: {v['desc']}" for n, v in sk.items())
    return ("\n\nمهارت‌های ذخیره‌شده‌ی کاربر:\n" + lines +
            "\nاگر درخواست کاربر به یکی از این مهارت‌ها مربوط بود، اول ابزار use_skill را با نام آن صدا بزن و بعد دقیقاً طبق دستورالعمل آن عمل کن. اگر ربط نداشت از مهارت‌ها استفاده نکن.")

SKILL_TOOL = {"type":"function","function":{"name":"use_skill","description":"بارگذاری دستورالعمل کامل یک مهارت ذخیره‌شده","parameters":{"type":"object","properties":{"name":{"type":"string"}},"required":["name"]}}}

def run_tool(name, args):
    if name == "use_skill": return use_skill(args["name"])
    if name in CODE_TOOLS: return call_code_tool(name, args)
    if name == "search_web": return search_web(args["query"])
    if name == "browse_url": return browse_url(args["url"])
    if name == "execute_python": return execute_python(args["code"])
    if name == "manage_task": return manage_task(args["action"], args.get("task", ""), int(args.get("task_id", 0) or 0))
    if name == "remember_info": return remember_info(args["key"], args.get("value", ""), args["action"])
    if name == "file_store": return file_store(args["action"], args["filename"], args.get("content", ""))
    return "ابزار ناشناخته"

DEADLINE = 50          # کل زمان مجاز برای یک درخواست (ثانیه)
MAX_STEPS = 6          # حداکثر مرحله‌ی ابزار
TIME_BUDGET = 28       # بعد از این ثانیه مرحله‌ی جدید شروع نمی‌شود (محدودیت Vercel ۶۰ ثانیه است)

# --- مدیریت سرریز کانتکست (جایگزین خلاصه‌سازی که در index.py نبود) ---
# بدون این بخش، تاریخچه‌ی طولانی فقط با خطا برمی‌گشت و تا /reset درست نمی‌شد.
_OVERFLOW_MARKERS = ("context_length_exceeded", "context length", "maximum context",
                     "too long", "token limit", "exceeds the context", "input too long")

def _is_context_overflow(err) -> bool:
    s = str(err).lower()
    return any(m in s for m in _OVERFLOW_MARKERS) or ("400" in s and "context" in s)

def _shrink_messages(messages):
    """قدیمی‌ترین تبادل تاریخچه را حذف می‌کند (سیستم و آخرین سؤال کاربر می‌مانند)؛
    اگر تبادلی برای حذف نبود، طولانی‌ترین خروجی ابزار را کوتاه می‌کند.
    اگر چیزی برای کوتاه کردن نبود، همان لیست برمی‌گردد."""
    msgs = list(messages)
    for i in range(1, len(msgs) - 1):
        m = msgs[i]
        if isinstance(m, dict) and m.get("role") == "user":
            for j in range(i + 1, len(msgs)):
                if isinstance(msgs[j], dict) and msgs[j].get("role") == "assistant":
                    return msgs[:i] + msgs[j + 1:]
            return msgs[:i] + msgs[i + 1:]
    longest, longest_len = -1, 1000
    for i, m in enumerate(msgs):
        if isinstance(m, dict) and m.get("role") == "tool":
            c = str(m.get("content") or "")
            if len(c) > longest_len:
                longest, longest_len = i, len(c)
    if longest >= 0:
        out = list(msgs)
        out[longest] = {**out[longest], "content": str(out[longest].get("content"))[:500] + "\n… (کوتاه شد)"}
        return out
    return msgs

def _trim_to_budget(messages, limit=30000):
    """پیش از فراخوانی مدل، پیام‌ها را تا سقف کاراکتر کوتاه می‌کند."""
    msgs = list(messages)
    def _chars(ms):
        total = 0
        for m in ms:
            if isinstance(m, dict): total += len(str(m.get("content") or ""))
            else: total += len(str(getattr(m, "content", "") or ""))
        return total
    while _chars(msgs) > limit:
        shrunk = _shrink_messages(msgs)
        if shrunk == msgs: break
        msgs = shrunk
    return msgs

def _chat_create(cli, cfg, messages, **kwargs):
    """فراخوانی مدل با retry در سرریز کانتکست: هر بار قدیمی‌ترین تبادل حذف می‌شود."""
    msgs = _trim_to_budget(messages)
    for attempt in range(4):
        try:
            return cli.chat.completions.create(model=cfg["model"], messages=msgs, **kwargs)
        except Exception as e:
            if not _is_context_overflow(e) or attempt == 3:
                raise
            shrunk = _shrink_messages(msgs)
            if shrunk == msgs:
                raise
            print(f"Context overflow, shrinking history (attempt {attempt + 1}); messages {len(msgs)} -> {len(shrunk)}")
            msgs = shrunk
    raise RuntimeError("unreachable")

def _load_history(user_id):
    """آخرین HISTORY_LEN تبادل (هر تبادل = یک سؤال + یک جواب) از دیتابیس."""
    if HISTORY_LEN <= 0:
        return []
    h = db_get(f"hist:{user_id}", []) or []
    h = [m for m in h if isinstance(m, dict) and m.get("role") in ("user", "assistant") and m.get("content")]
    return h[-HISTORY_LEN * 2:]

def _save_history(user_id, user_text, answer):
    """فقط همین یک تبادل (سؤال + جواب نهایی) به تاریخچه اضافه می‌شود."""
    if HISTORY_LEN <= 0 or not user_text or not answer:
        return
    h = db_get(f"hist:{user_id}", []) or []
    h = [m for m in h if isinstance(m, dict) and m.get("role") in ("user", "assistant") and m.get("content")]
    h.append({"role": "user", "content": user_text[:3000]})
    h.append({"role": "assistant", "content": answer[:3000]})
    db_set(f"hist:{user_id}", h[-HISTORY_LEN * 2:])

TOOL_LABELS = {
    "search_web": "🔎 در حال جستجوی وب...",
    "browse_url": "🌐 در حال خواندن صفحه...",
    "execute_python": "🐍 در حال اجرای کد...",
    "manage_task": "📋 در حال کار با تسک‌ها...",
    "remember_info": "🧠 در حال کار با حافظه...",
    "file_store": "📁 در حال کار با فایل...",
    "use_skill": "📚 در حال بارگذاری مهارت...",
}

async def agent_brain(user_id, user_text: str, on_status=None):
    async def status(t):
        if on_status:
            try: await on_status(t)
            except Exception as e: print(f"status err: {e}")
    t0 = time.monotonic()
    cfg = await asyncio.to_thread(get_cfg)
    if not cfg["api_key"]:
        return "کلید API تنظیم نشده. با دستور /key کلید را بفرست."
    cli = get_client(cfg)
    def to(): return max(5, min(30, DEADLINE - 5 - (time.monotonic() - t0)))
    history = await asyncio.to_thread(_load_history, user_id)
    skills = await asyncio.to_thread(get_skills)
    tl = tools + ([SKILL_TOOL] if skills else [])
    messages = [{"role":"system","content":SYSTEM_PROMPT + skills_prompt(skills)}]
    messages.extend(history)
    messages.append({"role":"user","content":user_text})

    async def call_tool(tc, step):
        name = tc.function.name
        try:
            args = json.loads(tc.function.arguments or "{}")
            print(f"AGENT step {step}: {name} {str(args)[:200]}")
            return await asyncio.to_thread(run_tool, name, args)
        except Exception as e:
            return f"خطای ابزار {name}: {e}"

    step = 0
    while step < MAX_STEPS and time.monotonic() - t0 < TIME_BUDGET:
        try:
            r = await asyncio.to_thread(lambda: _chat_create(cli, cfg, messages, tools=tl, tool_choice="auto", timeout=to()))
            msg = r.choices[0].message
            if not msg.tool_calls:
                await asyncio.to_thread(_save_history, user_id, user_text, msg.content)
                return msg.content or "انجام شد ✅"
            messages.append(msg)
            labels = []
            for tc in msg.tool_calls:
                n_ = tc.function.name
                l = TOOL_LABELS.get(n_) or (CODE_TOOLS.get(n_) or {}).get("label") or "⚙️ در حال اجرای ابزار..."
                if l not in labels: labels.append(l)
            await status("\n".join(labels))
            results = await asyncio.gather(*[call_tool(tc, step) for tc in msg.tool_calls])  # موازی
            for tc, res in zip(msg.tool_calls, results):
                messages.append({"role":"tool","tool_call_id":tc.id,"content":str(res)[:3500]})
            step += 1
            await status("🤔 در حال بررسی نتایج...")
        except Exception as e:
            if _is_context_overflow(e):
                print(f"AGENT context overflow, could not shrink further: {e}")
                return "خطا: گفتگو خیلی طولانی شد و مدل آن را قبول نکرد. با /reset حافظه را پاک کن و دوباره بفرست."
            if "429" in str(e):
                print("429 hit, waiting 5s...")
                await asyncio.sleep(5)
                step += 1
                continue
            print(f"AGENT err step {step}: {e}")
            return f"خطا: {e}"
    # جمع‌بندی نهایی: بدون ابزار، با دستور صریح
    print(f"FINAL summary after {step} steps, {time.monotonic()-t0:.0f}s")
    messages.append({"role":"user","content":"زمان یا مراحل ابزار تمام شد. دیگر ابزار نخواه و ننویس «بررسی می‌کنم». همین الان با اطلاعاتی که تا اینجا جمع کردی جواب نهایی کامل و مرتب بده؛ اگر چیزی را پیدا نکردی صادقانه بگو."})
    await status("✍️ در حال نوشتن جواب نهایی...")
    try:
        final = await asyncio.to_thread(lambda: _chat_create(cli, cfg, messages, timeout=to()))
        await asyncio.to_thread(_save_history, user_id, user_text, final.choices[0].message.content)
        return final.choices[0].message.content or "تحقیق کامل شد ✅"
    except Exception as e: return f"خطا در جمع بندی: {e}"

# ---------- فرمت خروجی تلگرام ----------
# علت به‌هم‌ریختگی (اسکرین‌شات ۱۹ نوامبر): خروجی مدل Markdown است، ولی
# send_message قبلی نه parse_mode داشت و نه متن را درست می‌شکست؛ پس
# `##` و `**` و جدول `|...|` همان‌طور خام دیده می‌شد، کد بدون <pre>
# با متن فارسی قاطی می‌شد (RTL: پرانتز و رشته‌های فارسی داخل کد جابه‌جا
# می‌شد) و پیام‌های ۴۰۰۰ کاراکتری وسط خط و وسط بلاک کد نصف می‌شدند.
# راه‌حل این لایه:
#   ۱) Markdown -> HTML تلگرام (نه MarkdownV2 که با `_` و `*` و `__name__`
#      در کد فارسی/پایتون مدام خطا می‌دهد)؛ جدول به لیست بولت تبدیل می‌شود.
#   ۲) تقسیم هوشمند در مرز پاراگراف/بلاک، هرگز وسط <pre> و هرگز وسط کلمه
#      اگر ممکن باشد؛ هر پیام زیر ۴۰۹۶ کاراکتر تلگرام می‌ماند.
#   ۳) کد طولانی -> فایل (sendDocument) به‌جای سیل پیام؛ توضیح فارسی و
#      کد در پیام‌های جدا می‌مانند تا RTL متن، کد LTR را خراب نکند.
TG_LIMIT = 4096
TG_SAFE = 3800  # حاشیه برای تگ‌های HTML
CODE_FILE_MIN_CHARS = 1200
CODE_FILE_MIN_LINES = 30

_LANG_EXT = {"python": "py", "py": "py", "javascript": "js", "js": "js",
             "typescript": "ts", "ts": "ts", "bash": "sh", "sh": "sh",
             "shell": "sh", "json": "json", "html": "html", "css": "css",
             "sql": "sql", "go": "go", "rust": "rs", "java": "java",
             "c": "c", "cpp": "cpp", "yaml": "yaml", "yml": "yaml",
             "markdown": "md", "md": "md", "text": "txt", "txt": "txt", "": "txt"}

def _tg_escape(s):
    return html.escape(s, quote=False)

def _md_inline_to_html(s):
    # s از قبل escape شده است؛ < و > و & در آن به‌صورت &lt; و ... هستند.
    # ۱) لینک [متن](url) — قبل از URL لخت، تا دوبار تگ نخورد.
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', s)
    # ۲) کد درون‌خطی `x`
    s = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", s)
    # ۳) بولد **x** — عمداً __x__ تبدیل نمی‌شود: __name__ در پایتون خراب می‌شد.
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    # ۴) خط‌خورده
    s = re.sub(r"~~(.+?)~~", r"<s>\1</s>", s)
    # ۵) ایتالیک *x* — عمداً _x_ تبدیل نمی‌شود: snake_case خراب می‌شد.
    s = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", s)
    # ۶) URL لخت (نه داخل href و نه متن لینک)
    s = re.sub(r"(?<![\"'>=])(https?://[^\s<]+)", r'<a href="\1">\1</a>', s)
    return s

def _md_to_tg_html(text):
    """Markdown مدل را به HTML امن تلگرام تبدیل می‌کند."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    # ۱) بلاک‌های کد ``` را جدا کن تا escape و فرمت روی آن‌ها اثر نکند.
    code_blocks = []
    def _code_repl(m):
        lang = (m.group(1) or "").strip().lower()
        code = (m.group(2) or "").rstrip("\n")
        idx = len(code_blocks)
        code_blocks.append((lang, code))
        return f"\x00CODE{idx}\x00"
    # ``` ناتمام هم تا آخر متن کد حساب می‌شود (مدل گاهی بستن را جا می‌اندازد).
    text = re.sub(r"```([A-Za-z0-9_+-]*)[ \t]*\n?(.*?)(?:```|$)", _code_repl, text, flags=re.S)
    # ۲) بقیه را escape کن (تزریق <script> و <b> از سمت مدل خنثی می‌شود).
    text = html.escape(text, quote=False)
    lines = text.split("\n")
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if "\x00CODE" in line:
            out.append(line)
            i += 1
            continue
        # جدول Markdown -> لیست بولت (تلگرام جدول ندارد)
        # _strip_b: سلول‌ها ممکن است خودشان **بولد** باشند؛ بولد تودرتو
        # (<b><b>...</b></b>) را تلگرام رد می‌کند، پس قبل از پیچیدن دوباره، تگ b را برمی‌داریم.
        if line.strip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|[\s:|\-]+\|\s*$", lines[i + 1]):
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            i += 2
            out.append(f"<b>{' — '.join(re.sub(r'</?b>', '', _md_inline_to_html(c)) for c in header)}</b>")
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [_md_inline_to_html(c.strip()) for c in lines[i].strip().strip("|").split("|")]
                if len(cells) >= 2:
                    out.append(f"• <b>{re.sub(r'</?b>', '', cells[0])}</b>: {' — '.join(cells[1:])}")
                elif cells:
                    out.append(f"• {cells[0]}")
                i += 1
            continue
        if re.match(r"^\s*(-{3,}|_{3,}|\*{3,})\s*$", line):
            out.append("")
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.+)$", line)
        if m:
            out.append(f"<b>{re.sub(r'</?b>', '', _md_inline_to_html(m.group(2)))}</b>")
            i += 1
            continue
        m = re.match(r"^&gt;\s?(.*)$", line)
        if m:
            out.append(f"<blockquote>{_md_inline_to_html(m.group(1))}</blockquote>")
            i += 1
            continue
        m = re.match(r"^\s*[-*]\s+(.+)$", line)
        if m:
            out.append(f"• {_md_inline_to_html(m.group(1))}")
            i += 1
            continue
        out.append(_md_inline_to_html(line))
        i += 1
    html_text = "\n".join(out)
    for idx, (lang, code) in enumerate(code_blocks):
        esc = html.escape(code, quote=False)
        if lang and re.match(r"^[A-Za-z0-9_+-]+$", lang):
            pre = f'<pre language="{lang}">{esc}</pre>'
        else:
            pre = f"<pre>{esc}</pre>"
        html_text = html_text.replace(f"\x00CODE{idx}\x00", pre)
    return html_text

def _split_pre_block(block):
    m = re.match(r'<pre(?: language="([^"]+)")?>(.*?)</pre>$', block, flags=re.S)
    if not m:
        return [block]
    lang, inner = m.group(1), m.group(2)
    open_tag = f'<pre language="{lang}">' if lang else "<pre>"
    chunks, cur = [], ""
    for ln in inner.split("\n"):
        add = ln + "\n"
        if cur and len(open_tag) + len(cur) + len(add) + len("</pre>") > TG_SAFE:
            chunks.append(open_tag + cur.rstrip("\n") + "</pre>")
            cur = add
        else:
            cur += add
    if cur:
        chunks.append(open_tag + cur.rstrip("\n") + "</pre>")
    return chunks or [block]

def _split_text_block(block):
    """متن بدون <pre> را در مرز پاراگراف/خط/کلمه می‌شکند."""
    if len(block) <= TG_SAFE:
        return [block]
    chunks, cur = [], ""
    for para in block.split("\n\n"):
        if cur and len(cur) + len(para) + 2 <= TG_SAFE:
            cur += "\n\n" + para
            continue
        if cur:
            chunks.append(cur)
            cur = ""
        if len(para) <= TG_SAFE:
            cur = para
            continue
        for line in para.split("\n"):
            if cur and len(cur) + len(line) + 1 <= TG_SAFE:
                cur += "\n" + line
                continue
            if cur:
                chunks.append(cur)
                cur = ""
            if len(line) <= TG_SAFE:
                cur = line
                continue
            for word in line.split(" "):
                if cur and len(cur) + len(word) + 1 <= TG_SAFE:
                    cur += " " + word
                else:
                    if cur:
                        chunks.append(cur)
                    cur = word
    if cur:
        chunks.append(cur)
    return [c for c in chunks if c]

def _split_tg_html(html_text):
    """HTML تلگرام را به پیام‌های زیر سقف می‌شکند؛ <pre> هرگز نصف نمی‌شود."""
    parts = re.split(r"(<pre(?:\s+language=\"[A-Za-z0-9_+-]+\")?>.*?</pre>)", html_text, flags=re.S)
    chunks, cur = [], ""
    def flush():
        nonlocal cur
        if cur:
            chunks.append(cur)
            cur = ""
    for part in parts:
        if not part:
            continue
        if part.startswith("<pre"):
            if len(part) > TG_SAFE:
                flush()
                chunks.extend(_split_pre_block(part))
            elif cur and len(cur) + len(part) + 1 > TG_SAFE:
                flush()
                cur = part
            else:
                cur = (cur + "\n" + part) if cur else part
        else:
            for piece in _split_text_block(part):
                if cur and len(cur) + len(piece) + 1 <= TG_SAFE:
                    cur += "\n" + piece
                elif cur:
                    flush()
                    cur = piece
                else:
                    cur = piece
    flush()
    return [c for c in chunks if c.strip()]

def _extract_long_code(markdown_text):
    """بلاک‌های کد طولانی را از متن جدا می‌کند تا به‌صورت فایل فرستاده شوند.
    برمی‌گرداند: (متن بدون آن بلاک‌ها، [(filename, code, lang), ...])"""
    found = list(re.finditer(r"```([A-Za-z0-9_+-]*)[ \t]*\n?(.*?)(?:```|$)", markdown_text or "", flags=re.S))
    keep, files = markdown_text or "", []
    for m in reversed(found):
        lang = (m.group(1) or "").strip().lower()
        code = (m.group(2) or "").rstrip("\n")
        nlines = code.count("\n") + 1 if code else 0
        if len(code) >= CODE_FILE_MIN_CHARS or nlines >= CODE_FILE_MIN_LINES:
            ext = _LANG_EXT.get(lang, "txt")
            n = len(files) + 1
            fname = f"script.{ext}" if n == 1 else f"script-{n}.{ext}"
            files.append((fname, code, lang))
            keep = keep[:m.start()] + f"📎 کد کامل در فایل پیوست است ({fname}، {nlines} خط)" + keep[m.end():]
    files.reverse()
    return keep, files

async def _post_tg_html(chat_id, chunk):
    """یک پیام HTML می‌فرستد؛ اگر تلگرام parse را رد کرد، به متن ساده برمی‌گردد."""
    async with httpx.AsyncClient() as c:
        try:
            r = await c.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                             json={"chat_id": chat_id, "text": chunk, "parse_mode": "HTML",
                                   "disable_web_page_preview": True}, timeout=15)
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if r.status_code == 200 and body.get("ok") is not False:
                print(f"Sent to {chat_id}")
                return True
            desc = str(body.get("description", ""))
            print(f"Send err: HTTP {r.status_code} {desc[:200]}")
            if "parse" in desc.lower() or "entities" in desc.lower():
                plain = html.unescape(re.sub(r"<[^>]+>", "", chunk))
                r2 = await c.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                                  json={"chat_id": chat_id, "text": plain[:TG_LIMIT]}, timeout=15)
                print(f"Send plain fallback: HTTP {r2.status_code}")
                return r2.status_code == 200
            return False
        except Exception as e:
            print(f"Send err: {e}")
            return False

async def send_document(chat_id, filename, content, caption=None):
    try:
        async with httpx.AsyncClient() as c:
            data = {"chat_id": str(chat_id)}
            if caption:
                data["caption"] = _md_to_tg_html(caption)[:1024]
                data["parse_mode"] = "HTML"
            files = {"document": (filename, content.encode("utf-8"), "text/plain; charset=utf-8")}
            r = await c.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
                             data=data, files=files, timeout=30)
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if r.status_code != 200 or body.get("ok") is False:
                print(f"SendDocument err: HTTP {r.status_code} {str(body.get('description', ''))[:200]}")
                await send_message(chat_id, f"📎 {filename}\n```\n{content[:3500]}\n```", allow_file=False)
            else:
                print(f"Sent document {filename} to {chat_id}")
    except Exception as e:
        print(f"SendDocument err: {e}")

async def send_message(chat_id, text, allow_file=True):
    """نقطه‌ی اصلی خروجی: Markdown -> HTML تلگرام، تقسیم هوشمند، کد طولانی -> فایل.
    امضایش با قبل سازگار است؛ همه‌ی فراخوانی‌های قدیمی بدون تغییر کار می‌کنند."""
    text = text or "انجام شد ✅"
    files = []
    body = text
    if allow_file:
        body, files = _extract_long_code(text)
    chunks = _split_tg_html(_md_to_tg_html(body)) or [_tg_escape(text)]
    for chunk in chunks:
        await _post_tg_html(chat_id, chunk)
        await asyncio.sleep(0.15)
    for fname, code, lang in files:
        await send_document(chat_id, fname, code, caption=f"📎 {fname}")

async def tg(method, **payload):
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=payload, timeout=10)
            return r.json()
    except Exception as e:
        print(f"TG {method} err: {e}"); return {}

async def typing_loop(chat_id, stop):
    """تا وقتی جواب آماده نشده، بالای چت «در حال تایپ...» نشان می‌دهد."""
    while not stop.is_set():
        await tg("sendChatAction", chat_id=chat_id, action="typing")
        try: await asyncio.wait_for(stop.wait(), timeout=4)
        except asyncio.TimeoutError: pass

async def delete_message(chat_id, message_id):
    try:
        async with httpx.AsyncClient() as c:
            await c.post(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage",
                         json={"chat_id": chat_id, "message_id": message_id}, timeout=10)
    except Exception as e: print(f"Delete err: {e}")

def norm(t):
    return t.replace("ي","ی").replace("ك","ک").replace("\u200f","").strip()

async def run(fn, *a): return await asyncio.to_thread(fn, *a)

app = FastAPI()

@app.get("/")
@app.get("/api/index")
async def root():
    return {"status":"Agent running","redis":bool(REDIS_URL),"kv":bool(KV_URL),"python_tool":ENABLE_PYTHON,"search":bool(tavily)}

@app.post("/")
@app.post("/api/index")
async def webhook(req: Request):
    if WEBHOOK_SECRET and req.headers.get("x-telegram-bot-api-secret-token") != WEBHOOK_SECRET:
        return Response(status_code=403)
    chat_id = None
    try:
        data = await req.json()
        msg = data.get("message") or data.get("edited_message")
        if not msg: return {"ok":True}
        if not await run(is_new_update, data.get("update_id")): return {"ok":True}
        chat_id = msg["chat"]["id"]
        user_id = msg["from"]["id"]
        text = norm(msg.get("text") or msg.get("caption") or "")
        # مقدار /key هرگز در لاگ چاپ نمی‌شود (قبلاً متن کامل پیام، شامل کلید، لاگ می‌شد)
        if text.startswith("/key"):
            print(f"From {user_id}: /key *** (مقدار در لاگ ثبت نشد)")
        else:
            print(f"From {user_id}: {text[:300]}")
        if user_id != ALLOWED_USER_ID:
            await send_message(chat_id,"⛔ شما دسترسی ندارید")
            return {"ok":True}
        if not text:
            await send_message(chat_id,"فعلاً فقط پیام متنی را می‌فهمم 🙂")
            return {"ok":True}
        if text.startswith("/start"):
            await send_message(chat_id,"سلام! من ایجنت هوشمند تو هستم 🤖\n• تحقیق عمیق: بگو تحقیق کن ...\n• اجرای کد: بگو کد بنویس و اجرا کن\n• لینک بفرست تا خلاصه کنم\n• حافظه دائمی + تسک + فایل\n• تنظیمات مدل: /settings ، /provider ، /model ، /key ، /test\n• مهارت‌ها: /skills ، /addskill ، /viewskill ، /delskill\n• ابزارها: /tools ، /tool")
            return {"ok":True}
        if text.startswith("/settings"):
            cfg = await run(get_cfg)
            src = lambda k: "ذخیره‌شده در ربات" if k in cfg["saved"] else "از Vercel"
            await send_message(chat_id, f"⚙️ تنظیمات فعلی:\n• پرووایدر: {cfg['base_url']} ({src('base_url')})\n• مدل: {cfg['model']} ({src('model')})\n• کلید: {mask(cfg['api_key'])} ({src('api_key')})\n\nتغییر: /provider آدرس ، /model نام ، /key کلید\nتست: /test ، بازگشت به Vercel: /resetsettings")
            return {"ok":True}
        if text.startswith("/resetsettings"):
            await run(db_set, "settings", {})
            await send_message(chat_id, "✅ تنظیمات ذخیره‌شده پاک شد؛ دوباره از Vercel خوانده می‌شود.")
            return {"ok":True}
        if text.startswith("/provider") or text.startswith("/model") or text.startswith("/key"):
            cmd, _, val = text.partition(" ")
            val = val.strip()
            name = {"/provider": "base_url", "/model": "model", "/key": "api_key"}.get(cmd)
            if name:
                if name == "api_key": await delete_message(chat_id, msg.get("message_id"))
                if not val:
                    await send_message(chat_id, f"مقدار را بعد از دستور بنویس. مثال: {cmd} ..."); return {"ok":True}
                if name == "base_url":
                    val = val.rstrip("/")
                    if not val.startswith("http"):
                        await send_message(chat_id, "آدرس باید با https:// شروع شود. مثال: /provider https://api.openai.com/v1"); return {"ok":True}
                await run(set_setting, name, val)
                shown = mask(val) if name == "api_key" else val
                await send_message(chat_id, f"✅ ذخیره شد ({name}): {shown}\nبرای امتحان: /test" + ("\n(پیام کلید از چت پاک شد)" if name == "api_key" else ""))
                return {"ok":True}
        if text.startswith("/test"):
            cfg = await run(get_cfg)
            if not cfg["api_key"]:
                await send_message(chat_id, "کلید تنظیم نشده. /key کلید"); return {"ok":True}
            try:
                r = await asyncio.wait_for(asyncio.to_thread(lambda: get_client(cfg).chat.completions.create(
                    model=cfg["model"], messages=[{"role":"user","content":"فقط بنویس OK"}], max_tokens=10, timeout=20)), 25)
                await send_message(chat_id, f"✅ کار می‌کند\nمدل: {cfg['model']}\nجواب: {(r.choices[0].message.content or '').strip()[:100]}")
            except Exception as e:
                await send_message(chat_id, f"❌ خطا: {str(e)[:300]}")
            return {"ok":True}
        cmd0 = text.split(None, 1)[0] if text else ""
        if cmd0 in ("/skills", "/addskill", "/viewskill", "/delskill"):
            if cmd0 == "/skills":
                sk = await run(get_skills)
                if not sk:
                    await send_message(chat_id, "هنوز مهارتی نداری.\n\nساخت:\n/addskill نام | توضیح کوتاه\nدستورالعمل کامل از خط بعد...")
                else:
                    await send_message(chat_id, "📚 مهارت‌ها:\n" + "\n".join(f"• {n} — {v['desc']}" for n, v in sk.items()) + "\n\nمشاهده: /viewskill نام\nحذف: /delskill نام")
            elif cmd0 == "/addskill":
                raw = msg.get("text") or msg.get("caption") or ""
                n, d, b = parse_addskill(raw)
                if not (n and d and b):
                    await send_message(chat_id, "فرمت:\n/addskill نام | توضیح کوتاه\nدستورالعمل کامل (از خط بعد، هر چند خط)\n\nیا همه در یک خط:\n/addskill نام | توضیح | دستورالعمل")
                else:
                    await send_message(chat_id, await run(add_skill, n, d, b))
            else:
                arg = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""
                if cmd0 == "/delskill": await send_message(chat_id, await run(del_skill, arg) if arg else "نام مهارت را بنویس: /delskill نام")
                else:
                    sk = await run(get_skills)
                    await send_message(chat_id, f"📚 {arg}\n{sk[arg]['desc']}\n\n{sk[arg]['body']}" if arg in sk else "چنین مهارتی نیست. /skills")
            return {"ok":True}
        if cmd0 in ("/tools", "/tool"):
            if cmd0 == "/tools":
                await send_message(chat_id, "🧰 ابزارهای کدی:\n" + "\n".join(f"• {n} — {t['desc'][:70]}" for n, t in CODE_TOOLS.items()) + "\n\nتست مستقیم: /tool نام آرگومان\nمثال: /tool crypto_price ADA")
            else:
                parts = text.split(None, 2)
                if len(parts) < 2: await send_message(chat_id, "مثال: /tool crypto_price ADA")
                else: await send_message(chat_id, await run(cli_tool, parts[1], parts[2] if len(parts) > 2 else ""))
            return {"ok":True}
        if text.startswith("یاد بگیر") or text.startswith("ذخیره کن"):
            content=text.replace("یاد بگیر","").replace("ذخیره کن","").strip()
            if "اسم من" in content:
                val=content.split("اسم من")[-1].replace("هست","").replace("است","").replace("هستم","").strip(" :")
                res=await run(remember_info,"اسم",val,"save")
                await send_message(chat_id,f"✅ {res} سلام {val}!")
                return {"ok":True}
            if "=" in content:
                k,v=content.split("=",1)
                res=await run(remember_info,k.strip(),v.strip(),"save")
            else:
                res=await run(remember_info,"یادداشت",content,"save")
            await send_message(chat_id,f"✅ {res}")
            return {"ok":True}
        if "اسم من چی" in text or "نام من چی" in text:
            print("PATH: name-question")
            res=await run(remember_info,"اسم","","get")
            await send_message(chat_id,f"اسم شما {res} هست 😊" if res!="یافت نشد" else "هنوز اسمت رو نمیدونم")
            return {"ok":True}
        if text.startswith("/reset") or text.startswith("/clear"):
            await run(db_set, f"hist:{user_id}", [])
            await send_message(chat_id, "✅ حافظه مکالمه پاک شد")
            return {"ok":True}
        if text.startswith("/tasks"):
            await send_message(chat_id, await run(manage_task,"list"))
            return {"ok":True}
        if text.startswith("/task "):
            await send_message(chat_id, await run(manage_task,"add",text[6:]))
            return {"ok":True}
        if text.startswith("/memory"):
            await send_message(chat_id, await run(remember_info,"all","","list"))
            return {"ok":True}
        print("PATH: agent")
        first = "⏳ دریافت شد، در حال پردازش..."
        st = await tg("sendMessage", chat_id=chat_id, text=first, disable_notification=True)
        status_id = (st.get("result") or {}).get("message_id")
        stop = asyncio.Event()
        typing_task = asyncio.create_task(typing_loop(chat_id, stop))
        last = {"t": first}
        async def on_status(t):
            if status_id and t != last["t"]:
                last["t"] = t
                await tg("editMessageText", chat_id=chat_id, message_id=status_id, text=t)
        try:
            try: ans = await asyncio.wait_for(agent_brain(user_id, text, on_status), timeout=DEADLINE)
            except asyncio.TimeoutError:
                print("DEADLINE hit")
                ans = "⏱ زمان پردازش تمام شد (سرویس مدل کند بود). یک بار دوباره بفرست یا سؤال را کوتاه‌تر کن."
        finally:
            stop.set(); typing_task.cancel()
            if status_id: await delete_message(chat_id, status_id)
        await send_message(chat_id, ans or "انجام شد ✅")
        return {"ok":True}
    except Exception as e:
        print(f"Webhook err: {e} {traceback.format_exc()}")
        if chat_id: await send_message(chat_id, f"خطا: {e}")
        return {"ok":True}
