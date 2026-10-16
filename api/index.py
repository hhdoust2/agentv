from fastapi import FastAPI, Request
import os, json
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

client = OpenAI(api_key=OPENAI_KEY, base_url=BASE_URL)
tavily = TavilyClient(api_key=TAVILY_KEY) if TAVILY_KEY else None

def load_json(f,d):
    if os.path.exists(f):
        try: return json.load(open(f,"r",encoding="utf-8"))
        except: return d
    return d
def save_json(f,d): 
    with open(f,"w",encoding="utf-8") as file:
        json.dump(d, file, ensure_ascii=False, indent=2)

def search_web(q):
    if not tavily: return "Tavily تنظیم نشده"
    try:
        r=tavily.search(query=q, max_results=3, include_answer=True)
        return r.get('answer','') + str(r.get('results',[])[:1])
    except Exception as e: return str(e)

def manage_task(action, task="", task_id=0):
    tasks=load_json("/tmp/tasks.json",[])
    if action=="add":
        tasks.append({"id":len(tasks)+1,"task":task,"status":"pending"})
        save_json("/tmp/tasks.json",tasks); return f"تسک اضافه شد: {task}"
    if action=="list":
        if not tasks: return "هیچ تسکی نیست"
        return "\n".join([f"{t['id']}. {t['task']} - {t['status']}" for t in tasks])
    if action=="done":
        for t in tasks:
            if t['id']==task_id: t['status']="done"
        save_json("/tmp/tasks.json",tasks); return f"تسک {task_id} انجام شد"
    return "نامعتبر"

def remember_info(key,value="",action="save"):
    m=load_json("/tmp/memory.json",{})
    if action=="save": m[key]=value; save_json("/tmp/memory.json",m); return f"ذخیره شد {key}={value}"
    if action=="get": return m.get(key,"یافت نشد")
    if action=="list": return json.dumps(m,ensure_ascii=False,indent=2)
    return "خطا"

tools=[
 {"type":"function","function":{"name":"search_web","description":"جستجو اینترنت","parameters":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}}},
 {"type":"function","function":{"name":"manage_task","description":"مدیریت تسک","parameters":{"type":"object","properties":{"action":{"type":"string","enum":["add","list","done","delete"]},"task":{"type":"string"},"task_id":{"type":"integer"}},"required":["action"]}}},
 {"type":"function","function":{"name":"remember_info","description":"حافظه","parameters":{"type":"object","properties":{"key":{"type":"string"},"value":{"type":"string"},"action":{"type":"string","enum":["save","get","list"]}},"required":["key","action"]}}}
]
SYSTEM_PROMPT="تو دستیار هوشمند فارسی هستی. از ابزارها استفاده کن و صمیمی جواب بده."

async def agent_brain(text):
    messages=[{"role":"system","content":SYSTEM_PROMPT},{"role":"user","content":text}]
    try:
        r=client.chat.completions.create(model=MODEL_NAME,messages=messages,tools=tools,tool_choice="auto")
        msg=r.choices[0].message
        if msg.tool_calls:
            messages.append(msg)
            for tc in msg.tool_calls:
                name=tc.function.name; args=json.loads(tc.function.arguments)
                if name=="search_web": res=search_web(args["query"])
                elif name=="manage_task": res=manage_task(args["action"],args.get("task",""),args.get("task_id",0))
                elif name=="remember_info": res=remember_info(args["key"],args.get("value",""),args["action"])
                else: res="ابزار یافت نشد"
                messages.append({"role":"tool","tool_call_id":tc.id,"content":res})
            final=client.chat.completions.create(model=MODEL_NAME,messages=messages)
            ans=final.choices[0].message.content
            return ans if ans and ans.strip() else "انجام شد ✅"
        return msg.content or "متوجه نشدم"
    except Exception as e: return f"خطا: {e}"

tg_app = Application.builder().token(BOT_TOKEN).build()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID: return
    await update.message.reply_text("سلام! ربات روی Vercel روشنه 🤖")

async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        await update.message.reply_text("⛔ دسترسی ندارید"); return
    txt=update.message.text
    if txt.startswith("/task "): await update.message.reply_text(manage_task("add",task=txt[6:])); return
    if txt.startswith("/tasks"): await update.message.reply_text(manage_task("list")); return
    if txt.startswith("/done "):
        try: await update.message.reply_text(manage_task("done",task_id=int(txt.split()[1])))
        except: await update.message.reply_text("فرمت: /done 1")
        return
    ans=await agent_brain(txt)
    await update.message.reply_text(ans)

tg_app.add_handler(CommandHandler("start", start))
tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
tg_app.add_handler(MessageHandler(filters.COMMAND, handle))

# --- FastAPI برای Vercel ---
fastapi_app = FastAPI()
@fastapi_app.get("/")
async def root(): return {"status":"Bot is running on Vercel"}

@fastapi_app.post("/api/index")
async def webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.initialize()
    await tg_app.process_update(update)
    await tg_app.shutdown()
    return {"ok": True}

app = fastapi_app
