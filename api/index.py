from fastapi import FastAPI, Request
import os
import json
import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from openai import OpenAI
from tavily import TavilyClient

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
MODEL_NAME = os.getenv("MODEL_NAME", "gpt-4o")
TAVILY_KEY = os.getenv("TAVILY_API_KEY")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))

# --- Redis Config (Vercel KV / Upstash) ---
KV_URL = os.getenv("KV_REST_API_URL") or os.getenv("UPSTASH_REDIS_REST_URL")
KV_TOKEN = os.getenv("KV_REST_API_TOKEN") or os.getenv("UPSTASH_REDIS_REST_TOKEN")

def db_get(key, default):
    if KV_URL and KV_TOKEN:
        try:
            r = httpx.post(KV_URL, headers={"Authorization": f"Bearer {KV_TOKEN}"}, json=["GET", key], timeout=10)
            res = r.json().get("result")
            if res:
                return json.loads(res)
            return default
        except Exception as e:
            print(f"DB GET error {key}: {e}")
            return default
    else:
        path = f"/tmp/{key}.json"
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except:
                return default
        return default

def db_set(key, value):
    if KV_URL and KV_TOKEN:
        try:
            val_str = json.dumps(value, ensure_ascii=False)
            r = httpx.post(KV_URL, headers={"Authorization": f"Bearer {KV_TOKEN}"}, json=["SET", key, val_str], timeout=10)
            print(f"DB SET {key}: {r.status_code}")
        except Exception as e:
            print(f"DB SET error {key}: {e}")
    else:
        path = f"/tmp/{key}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)

client = OpenAI(api_key=OPENAI_KEY, base_url=BASE_URL)
tavily = TavilyClient(api_key=TAVILY_KEY) if TAVILY_KEY else None

def search_web(query: str):
    if not tavily:
        return "کلید Tavily تنظیم نشده"
    try:
        result = tavily.search(query=query, max_results=5, include_answer=True)
        answer = result.get('answer', '')
        results = result.get('results', [])[:2]
        return answer + "\n\n" + str(results)
    except Exception as e:
        return f"خطا در جستجو: {e}"

def manage_task(action: str, task: str = "", task_id: int = 0):
    tasks = db_get("tasks", [])
    if action == "add":
        tasks.append({"id": len(tasks)+1, "task": task, "status": "pending"})
        db_set("tasks", tasks)
        return f"تسک اضافه شد: {task}"
    elif action == "list":
        if not tasks:
            return "هیچ تسکی وجود ندارد"
        return "\n".join([f"{t['id']}. {t['task']} - {t['status']}" for t in tasks])
    elif action == "done":
        for t in tasks:
            if t['id'] == task_id:
                t['status'] = "done"
        db_set("tasks", tasks)
        return f"تسک {task_id} انجام شد"
    elif action == "delete":
        tasks = [t for t in tasks if t['id'] != task_id]
        db_set("tasks", tasks)
        return f"تسک {task_id} حذف شد"
    return "عملیات نامعتبر"

def remember_info(key: str, value: str = "", action: str = "save"):
    memory = db_get("memory", {})
    if action == "save":
        memory[key] = value
        db_set("memory", memory)
        return f"ذخیره شد: {key} = {value}"
    elif action == "get":
        return memory.get(key, "چیزی یافت نشد")
    elif action == "list":
        if not memory:
            return "حافظه خالی است"
        return json.dumps(memory, ensure_ascii=False, indent=2)
    return "خطا"

tools = [
    {"type": "function", "function": {"name": "search_web", "description": "جستجو در اینترنت برای اطلاعات روز", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "manage_task", "description": "مدیریت تسک ها و کارها", "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": ["add", "list", "done", "delete"]}, "task": {"type": "string"}, "task_id": {"type": "integer"}}, "required": ["action"]}}},
    {"type": "function", "function": {"name": "remember_info", "description": "ذخیره یا یادآوری اطلاعات مهم کاربر", "parameters": {"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}, "action": {"type": "string", "enum": ["save", "get", "list"]}}, "required": ["key", "action"]}}}
]

SYSTEM_PROMPT = """
تو یک دستیار هوشمند خودمختار و بسیار دقیق هستی.
وظیفه تو اینه که دستورات کاربر که از تلگرام میاد رو با فکر کردن مرحله به مرحله و استفاده از ابزارها اجرا کنی.
- اگر کاربر گفت "یاد بگیر" یا "ذخیره کن" از remember_info استفاده کن
- اگر تسکی داد از manage_task استفاده کن
- اگر سوالی پرسید که نیاز به اطلاعات روز داره از search_web استفاده کن
- همیشه فارسی و صمیمی جواب بده و در آخر گزارش بده چه کردی
"""

async def agent_brain(user_text: str):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_text}]
    try:
        response = client.chat.completions.create(model=MODEL_NAME, messages=messages, tools=tools, tool_choice="auto")
        msg = response.choices[0].message
        if msg.tool_calls:
            messages.append(msg)
            for tool_call in msg.tool_calls:
                name = tool_call.function.name
                args = json.loads(tool_call.function.arguments)
                print(f"Agent tool: {name} {args}")
                if name == "search_web":
                    result = search_web(args["query"])
                elif name == "manage_task":
                    result = manage_task(args["action"], args.get("task",""), args.get("task_id",0))
                elif name == "remember_info":
                    result = remember_info(args["key"], args.get("value",""), args["action"])
                else:
                    result = "ابزار یافت نشد"
                messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": result})
            final = client.chat.completions.create(model=MODEL_NAME, messages=messages)
            final_answer = final.choices[0].message.content
            if not final_answer or final_answer.strip() == "":
                return "انجام شد ✅"
            return final_answer
        else:
            return msg.content if msg.content else "متوجه نشدم، میشه دوباره بگی؟"
    except Exception as e:
        print(f"agent_brain error: {e}")
        return f"متاسفانه خطایی رخ داد: {str(e)[:200]}"

tg_app = Application.builder().token(BOT_TOKEN).build()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    await update.message.reply_text("سلام! من دستیار هوشمند شما هستم 🤖\nربات روی Vercel + دیتابیس روشنه ✅")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        await update.message.reply_text("⛔ شما دسترسی ندارید")
        return
    user_text = update.message.text
    if user_text.startswith("/task "):
        result = manage_task("add", task=user_text[6:])
        await update.message.reply_text(result)
        return
    if user_text.startswith("/tasks"):
        result = manage_task("list")
        await update.message.reply_text(result)
        return
    if user_text.startswith("/done "):
        try:
            tid = int(user_text.split()[1])
            result = manage_task("done", task_id=tid)
            await update.message.reply_text(result)
        except:
            await update.message.reply_text("فرمت: /done 1")
        return
    answer = await agent_brain(user_text)
    if not answer or answer.strip() == "":
        answer = "انجام شد ✅"
    await update.message.reply_text(answer)

tg_app.add_handler(CommandHandler("start", start))
tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
tg_app.add_handler(MessageHandler(filters.COMMAND, handle_message))

# --- FastAPI برای Vercel ---
fastapi_app = FastAPI()

@fastapi_app.get("/")
async def root():
    return {"status": "Bot is running on Vercel with DB"}

@fastapi_app.post("/api/index")
async def webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.initialize()
    await tg_app.process_update(update)
    await tg_app.shutdown()
    return {"ok": True}

app = fastapi_app
