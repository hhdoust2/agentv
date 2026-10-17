from fastapi import FastAPI, Request
import os, json, httpx, traceback, io, contextlib, asyncio
from openai import OpenAI
from tavily import TavilyClient
from bs4 import BeautifulSoup

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
MODEL_NAME = os.getenv("MODEL_NAME", "gpt-4o")
TAVILY_KEY = os.getenv("TAVILY_API_KEY")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
REDIS_URL = os.getenv("REDIS_URL") or os.getenv("KV_URL")
KV_URL = os.getenv("KV_REST_API_URL") or os.getenv("UPSTASH_REDIS_REST_URL")
KV_TOKEN = os.getenv("KV_REST_API_TOKEN") or os.getenv("UPSTASH_REDIS_REST_TOKEN")

print(f"REDIS_URL: {bool(REDIS_URL)} | KV_URL: {bool(KV_URL)}")

# --- DB ---
def db_get(key, default):
    if REDIS_URL and REDIS_URL.startswith("redis"):
        try:
            import redis
            r = redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=5)
            v = r.get(key)
            if v: return json.loads(v)
            return default
        except Exception as e: print(f"DB GET REDIS err: {e}")
    if KV_URL and KV_TOKEN and KV_URL.startswith("http"):
        try:
            resp = httpx.post(KV_URL, headers={"Authorization": f"Bearer {KV_TOKEN}"}, json=["GET", key], timeout=10)
            res = resp.json().get("result")
            if res: return json.loads(res)
            return default
        except Exception as e: print(f"DB GET KV err: {e}")
    return default

def db_set(key, value):
    val_str = json.dumps(value, ensure_ascii=False)
    if REDIS_URL and REDIS_URL.startswith("redis"):
        try:
            import redis
            r = redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=5)
            r.set(key, val_str)
            print(f"DB SET REDIS {key}: ok")
            return
        except Exception as e: print(f"DB SET REDIS err: {e}")
    if KV_URL and KV_TOKEN and KV_URL.startswith("http"):
        try:
            resp = httpx.post(KV_URL, headers={"Authorization": f"Bearer {KV_TOKEN}"}, json=["SET", key, val_str], timeout=10)
            print(f"DB SET KV {key}: {resp.status_code}")
            return
        except Exception as e: print(f"DB SET KV err: {e}")

client = OpenAI(api_key=OPENAI_KEY, base_url=BASE_URL)
tavily = TavilyClient(api_key=TAVILY_KEY) if TAVILY_KEY else None

# --- TOOLS ---
def search_web(query: str):
    if not tavily: return "Tavily API Key تنظیم نشده"
    try:
        res = tavily.search(query=query, max_results=5, include_answer=True, search_depth="advanced")
        out = f"Answer: {res.get('answer','')}\n\n"
        for r in res.get('results', []):
            out += f"- {r['title']}: {r['content'][:500]}\nURL: {r['url']}\n"
        return out[:5000]
    except Exception as e: return f"خطا در جستجو: {e}"

def browse_url(url: str):
    try:
        r = httpx.get(url, timeout=15, follow_redirects=True, headers={"User-Agent":"Mozilla/5.0"})
        soup = BeautifulSoup(r.text, 'html.parser')
        for s in soup(["script","style","nav","footer"]): s.decompose()
        text = soup.get_text(separator="\n")
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        return "\n".join(lines)[:6000]
    except Exception as e: return f"خطا در باز کردن URL: {e}"

def execute_python(code: str):
    try:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exec(code, {})
        val = output.getvalue()
        return val[:4000] if val else "کد بدون خروجی اجرا شد ✅"
    except Exception as e:
        return f"خطا در اجرا:\n{traceback.format_exc()[:3000]}"

def manage_task(action: str, task: str = "", task_id: int = 0):
    tasks = db_get("tasks", [])
    if action == "add":
        tasks.append({"id": len(tasks)+1, "task": task, "status": "pending"})
        db_set("tasks", tasks)
        return f"تسک اضافه شد: {task}"
    if action == "list":
        if not tasks: return "هیچ تسکی وجود ندارد"
        return "\n".join([f"{t['id']}. {t['task']} [{t['status']}]" for t in tasks])
    if action == "done":
        for t in tasks:
            if t['id']==task_id: t['status']="done"
        db_set("tasks", tasks)
        return f"تسک {task_id} انجام شد"
    if action == "delete":
        tasks = [t for t in tasks if t['id']!=task_id]
        db_set("tasks", tasks)
        return f"تسک {task_id} حذف شد"
    return "عملیات نامعتبر"

def remember_info(key: str, value: str = "", action: str = "save"):
    m = db_get("memory", {})
    if action == "save":
        m[key]=value
        db_set("memory", m)
        return f"ذخیره شد {key}={value}"
    if action == "get": return m.get(key, "یافت نشد")
    if action == "list": return json.dumps(m, ensure_ascii=False, indent=2) if m else "حافظه خالی است"
    return "خطا"

def file_store(action: str, filename: str = "", content: str = ""):
    files = db_get("files", {})
    if action == "write":
        files[filename]=content
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

async def agent_brain(user_text: str):
    messages = [{"role":"system","content":SYSTEM_PROMPT}, {"role":"user","content":user_text}]
    for step in range(4):
        try:
            r = client.chat.completions.create(model=MODEL_NAME, messages=messages, tools=tools, tool_choice="auto", timeout=30)
            msg = r.choices[0].message
            if not msg.tool_calls:
                return msg.content or "انجام شد ✅"
            messages.append(msg)
            for tc in msg.tool_calls:
                name = tc.function.name
                args = json.loads(tc.function.arguments)
                print(f"AGENT step {step}: {name} {args}")
                if name=="search_web": res=search_web(args["query"])
                elif name=="browse_url": res=browse_url(args["url"])
                elif name=="execute_python": res=execute_python(args["code"])
                elif name=="manage_task": res=manage_task(args["action"], args.get("task",""), args.get("task_id",0))
                elif name=="remember_info": res=remember_info(args["key"], args.get("value",""), args["action"])
                elif name=="file_store": res=file_store(args["action"], args["filename"], args.get("content",""))
                else: res="ابزار ناشناخته"
                messages.append({"role":"tool","tool_call_id":tc.id,"content":str(res)[:3500]})
        except Exception as e:
            if "429" in str(e):
                print("429 hit, waiting 5s...")
                await asyncio.sleep(5)
                continue
            print(f"AGENT err step {step}: {e}")
            return f"خطا: {e}"
    try:
        final = client.chat.completions.create(model=MODEL_NAME, messages=messages, timeout=30)
        return final.choices[0].message.content or "تحقیق کامل شد ✅"
    except Exception as e: return f"خطا در جمع بندی: {e}"

async def send_message(chat_id, text):
    async with httpx.AsyncClient() as c:
        for i in range(0, len(text), 4000):
            chunk = text[i:i+4000]
            await c.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={"chat_id":chat_id,"text":chunk})
            print(f"Sent to {chat_id}")

app = FastAPI()

@app.get("/")
async def root():
    return {"status":"Agent running","redis":bool(REDIS_URL),"kv":bool(KV_URL)}

@app.post("/api/index")
async def webhook(req: Request):
    try:
        data = await req.json()
        print(f"Incoming: {str(data)[:500]}")
        msg = data.get("message") or data.get("edited_message")
        if not msg: return {"ok":True}
        chat_id = msg["chat"]["id"]
        user_id = msg["from"]["id"]
        text = (msg.get("text") or msg.get("caption") or "").strip()
        print(f"From {user_id}: {text}")
        if user_id != ALLOWED_USER_ID:
            await send_message(chat_id,"⛔ شما دسترسی ندارید")
            return {"ok":True}
        if text.startswith("/start"):
            await send_message(chat_id,"سلام! من ایجنت هوشمند تو هستم 🤖\n• تحقیق عمیق: بگو تحقیق کن ...\n• اجرای کد: بگو کد بنویس و اجرا کن\n• لینک بفرست تا خلاصه کنم\n• حافظه دائمی + تسک + فایل")
            return {"ok":True}
        if text.startswith("یاد بگیر") or text.startswith("ذخیره کن"):
            content=text.replace("یاد بگیر","").replace("ذخیره کن","").strip()
            if "اسم من" in content:
                val=content.split("اسم من")[-1].replace("هست","").replace("است","").replace("هستم","").strip(" :")
                res=remember_info("اسم",val,"save")
                await send_message(chat_id,f"✅ {res} سلام {val}!")
                return {"ok":True}
            if "=" in content:
                k,v=content.split("=",1)
                res=remember_info(k.strip(),v.strip(),"save")
            else:
                res=remember_info("یادداشت",content,"save")
            await send_message(chat_id,f"✅ {res}")
            return {"ok":True}
        if "اسم من چیه" in text or "نام من چیه" in text:
            res=remember_info("اسم","","get")
            await send_message(chat_id,f"اسم شما {res} هست 😊" if res!="یافت نشد" else "هنوز اسمت رو نمیدونم")
            return {"ok":True}
        if text.startswith("/tasks"):
            await send_message(chat_id, manage_task("list"))
            return {"ok":True}
        if text.startswith("/task "):
            await send_message(chat_id, manage_task("add", task=text[6:]))
            return {"ok":True}
        if text.startswith("/memory"):
            await send_message(chat_id, remember_info("all","","list"))
            return {"ok":True}
        ans = await agent_brain(text)
        await send_message(chat_id, ans or "انجام شد ✅")
        return {"ok":True}
    except Exception as e:
        print(f"Webhook err: {e} {traceback.format_exc()}")
        return {"ok":False}
