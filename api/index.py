from fastapi import FastAPI, Request, Response
import os, time, json, httpx, traceback, asyncio, logging, ipaddress, socket, subprocess, sys
from urllib.parse import urlparse, urljoin
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

def _exec_inprocess(code):
    import io, contextlib
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out): exec(code, {})
        return out.getvalue()
    except Exception:
        return f"خطا در اجرا:\n{traceback.format_exc()[:3000]}"

def execute_python(code: str):
    if not ENABLE_PYTHON: return "اجرای کد غیرفعال است"
    try:
        p = subprocess.run([sys.executable or "python3", "-I", "-c", code], capture_output=True, text=True,
                           timeout=20, env={"PATH": "/usr/local/bin:/usr/bin:/bin"})
        val = (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired: return "خطا: اجرای کد بیش از ۲۰ ثانیه طول کشید"
    except OSError as e:  # پایتون جدا در دسترس نبود؛ مثل نسخه‌ی اولیه داخل خود برنامه اجرا می‌کنیم
        print(f"subprocess unavailable ({e}); in-process exec")
        val = _exec_inprocess(code)
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
"""

def run_tool(name, args):
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

async def agent_brain(user_id, user_text: str):
    t0 = time.monotonic()
    cfg = await asyncio.to_thread(get_cfg)
    if not cfg["api_key"]:
        return "کلید API تنظیم نشده. با دستور /key کلید را بفرست."
    cli = get_client(cfg)
    def to(): return max(5, min(30, DEADLINE - 5 - (time.monotonic() - t0)))
    history = await asyncio.to_thread(_load_history, user_id)
    messages = [{"role":"system","content":SYSTEM_PROMPT}]
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
            r = await asyncio.to_thread(lambda: cli.chat.completions.create(
                model=cfg["model"], messages=messages, tools=tools, tool_choice="auto", timeout=to()))
            msg = r.choices[0].message
            if not msg.tool_calls:
                await asyncio.to_thread(_save_history, user_id, user_text, msg.content)
                return msg.content or "انجام شد ✅"
            messages.append(msg)
            results = await asyncio.gather(*[call_tool(tc, step) for tc in msg.tool_calls])  # موازی
            for tc, res in zip(msg.tool_calls, results):
                messages.append({"role":"tool","tool_call_id":tc.id,"content":str(res)[:3500]})
            step += 1
        except Exception as e:
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
    try:
        final = await asyncio.to_thread(lambda: cli.chat.completions.create(model=cfg["model"], messages=messages, timeout=to()))
        await asyncio.to_thread(_save_history, user_id, user_text, final.choices[0].message.content)
        return final.choices[0].message.content or "تحقیق کامل شد ✅"
    except Exception as e: return f"خطا در جمع بندی: {e}"

async def send_message(chat_id, text):
    text = text or "انجام شد ✅"
    async with httpx.AsyncClient() as c:
        for i in range(0, len(text), 4000):
            chunk = text[i:i+4000]
            try:
                await c.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={"chat_id":chat_id,"text":chunk}, timeout=15)
                print(f"Sent to {chat_id}")
            except Exception as e: print(f"Send err: {e}")

async def delete_message(chat_id, message_id):
    try:
        async with httpx.AsyncClient() as c:
            await c.post(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage",
                         json={"chat_id": chat_id, "message_id": message_id}, timeout=10)
    except Exception as e: print(f"Delete err: {e}")

def norm(t):
    return t.replace("ي","ی").replace("ك","ک").replace("\u200c"," ").replace("\u200f","").strip()

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
        print(f"From {user_id}: {text[:300]}")
        if user_id != ALLOWED_USER_ID:
            await send_message(chat_id,"⛔ شما دسترسی ندارید")
            return {"ok":True}
        if not text:
            await send_message(chat_id,"فعلاً فقط پیام متنی را می‌فهمم 🙂")
            return {"ok":True}
        if text.startswith("/start"):
            await send_message(chat_id,"سلام! من ایجنت هوشمند تو هستم 🤖\n• تحقیق عمیق: بگو تحقیق کن ...\n• اجرای کد: بگو کد بنویس و اجرا کن\n• لینک بفرست تا خلاصه کنم\n• حافظه دائمی + تسک + فایل\n• تنظیمات مدل: /settings ، /provider ، /model ، /key ، /test")
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
        if text.startswith("/reset"):
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
        try: ans = await asyncio.wait_for(agent_brain(user_id, text), timeout=DEADLINE)
        except asyncio.TimeoutError:
            print("DEADLINE hit")
            ans = "⏱ زمان پردازش تمام شد (سرویس مدل کند بود). یک بار دوباره بفرست یا سؤال را کوتاه‌تر کن."
        await send_message(chat_id, ans or "انجام شد ✅")
        return {"ok":True}
    except Exception as e:
        print(f"Webhook err: {e} {traceback.format_exc()}")
        if chat_id: await send_message(chat_id, f"خطا: {e}")
        return {"ok":True}
