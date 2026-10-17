from fastapi import FastAPI, Request
import os, json, httpx
from openai import OpenAI
from tavily import TavilyClient

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

def db_get(key, default):
    if REDIS_URL and REDIS_URL.startswith("redis"):
        try:
            import redis
            r = redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=5)
            val = r.get(key)
            print(f"DB GET REDIS {key}: {bool(val)}")
            if val: return json.loads(val)
            return default
        except Exception as e: print(f"DB GET REDIS err: {e}")
    if KV_URL and KV_TOKEN:
        try:
            r = httpx.post(KV_URL, headers={"Authorization": f"Bearer {KV_TOKEN}"}, json=["GET", key], timeout=10)
            res = r.json().get("result")
            if res: return json.loads(res)
            return default
        except Exception as e: print(f"DB GET KV err: {e}")
    path = f"/tmp/{key}.json"
    if os.path.exists(path):
        try: return json.load(open(path,"r",encoding="utf-8"))
        except: return default
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
    if KV_URL and KV_TOKEN:
        try:
            r = httpx.post(KV_URL, headers={"Authorization": f"Bearer {KV_TOKEN}"}, json=["SET", key, val_str], timeout=10)
            print(f"DB SET KV {key}: {r.status_code}")
            return
        except Exception as e: print(f"DB SET KV err: {e}")
    with open(f"/tmp/{key}.json","w",encoding="utf-8") as f: json.dump(value,f,ensure_ascii=False,indent=2)

client = OpenAI(api_key=OPENAI_KEY, base_url=BASE_URL)
tavily = TavilyClient(api_key=TAVILY_KEY) if TAVILY_KEY else None

def search_web(q):
    if not tavily: return "Tavily تنظیم نشده"
    try:
        r=tavily.search(query=q, max_results=3, include_answer=True)
        return r.get('answer','')+str(r.get('results',[])[:1])
    except Exception as e: return str(e)

def manage_task(action, task="", task_id=0):
    tasks=db_get("tasks",[])
    if action=="add":
        tasks.append({"id":len(tasks)+1,"task":task,"status":"pending"})
        db_set("tasks",tasks); return f"تسک اضافه شد: {task}"
    if action=="list":
        if not tasks: return "هیچ تسکی نیست"
        return "\n".join([f"{t['id']}. {t['task']} - {t['status']}" for t in tasks])
    if action=="done":
        for t in tasks:
            if t['id']==task_id: t['status']="done"
        db_set("tasks",tasks); return f"تسک {task_id} انجام شد"
    return "نامعتبر"

def remember_info(key,value="",action="save"):
    m=db_get("memory",{})
    if action=="save": m[key]=value; db_set("memory",m); return f"ذخیره شد {key}={value}"
    if action=="get": return m.get(key,"یافت نشد")
    if action=="list": return json.dumps(m,ensure_ascii=False,indent=2)
    return "خطا"

tools=[
 {"type":"function","function":{"name":"search_web","description":"جستجو","parameters":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}}},
 {"type":"function","function":{"name":"manage_task","description":"تسک","parameters":{"type":"object","properties":{"action":{"type":"string","enum":["add","list","done"]},"task":{"type":"string"},"task_id":{"type":"integer"}},"required":["action"]}}},
 {"type":"function","function":{"name":"remember_info","description":"حافظه","parameters":{"type":"object","properties":{"key":{"type":"string"},"value":{"type":"string"},"action":{"type":"string","enum":["save","get","list"]}},"required":["key","action"]}}}
]
SYSTEM_PROMPT="تو دستیار فارسی هستی. ابزارها را استفاده کن."

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
                else: res="یافت نشد"
                messages.append({"role":"tool","tool_call_id":tc.id,"content":res})
            final=client.chat.completions.create(model=MODEL_NAME,messages=messages)
            return final.choices[0].message.content or "انجام شد ✅"
        return msg.content or "متوجه نشدم"
    except Exception as e: return f"خطا: {e}"

async def send_message(chat_id, text):
    async with httpx.AsyncClient() as c:
        await c.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={"chat_id":chat_id,"text":text})
        print(f"Sent to {chat_id}")

app=FastAPI()
@app.get("/")
async def root(): return {"status":"Bot Vercel DB","redis":bool(REDIS_URL),"kv":bool(KV_URL)}
@app.post("/api/index")
async def webhook(req: Request):
    try:
        data=await req.json()
        print(f"Incoming: {str(data)[:400]}")
        msg=data.get("message") or data.get("edited_message")
        if not msg: return {"ok":True}
        chat_id=msg["chat"]["id"]; user_id=msg["from"]["id"]; text=msg.get("text","")
        print(f"From {user_id}: {text}")
        if user_id != ALLOWED_USER_ID:
            await send_message(chat_id,"⛔ دسترسی ندارید"); return {"ok":True}
        if text.startswith("/start"):
            await send_message(chat_id,"سلام! ربات Vercel + Redis Cloud روشنه 🤖"); return {"ok":True}
        if text.startswith("یاد بگیر") or text.startswith("ذخیره کن"):
            content=text.replace("یاد بگیر","").replace("ذخیره کن","").strip()
            if "اسم من" in content:
                val=content.split("اسم من")[-1].replace("هست","").replace("است","").strip(" :")
                res=remember_info("اسم",val,"save")
                await send_message(chat_id,f"✅ {res} سلام {val}!"); return
            if "=" in content:
                k,v=content.split("=",1); res=remember_info(k.strip(),v.strip(),"save")
            else: res=remember_info("یادداشت",content,"save")
            await send_message(chat_id,f"✅ {res}"); return
        if "اسم من چیه" in text:
            res=remember_info("اسم","","get")
            await send_message(chat_id,f"اسم شما {res} هست" if res!="یافت نشد" else "هنوز نمیدونم"); return
        if text.startswith("/tasks"): await send_message(chat_id, manage_task("list")); return
        if text.startswith("/task "): await send_message(chat_id, manage_task("add",task=text[6:])); return
        ans=await agent_brain(text)
        await send_message(chat_id, ans)
        return {"ok":True}
    except Exception as e:
        print(f"Webhook err: {e}"); return {"ok":False}
