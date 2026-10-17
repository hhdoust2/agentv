from fastapi import FastAPI, Request
import os,json,httpx,traceback,io,contextlib,asyncio
from openai import OpenAI
from tavily import TavilyClient
from bs4 import BeautifulSoup

BOT_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_KEY=os.getenv("OPENAI_API_KEY")
BASE_URL=os.getenv("OPENAI_BASE_URL","https://api.openai.com/v1")
MODEL_NAME=os.getenv("MODEL_NAME","gpt-4o")
TAVILY_KEY=os.getenv("TAVILY_API_KEY")
ALLOWED_USER_ID=int(os.getenv("ALLOWED_USER_ID","0"))
REDIS_URL=os.getenv("REDIS_URL") or os.getenv("KV_URL")
KV_URL=os.getenv("KV_REST_API_URL") or os.getenv("UPSTASH_REDIS_REST_URL")
KV_TOKEN=os.getenv("KV_REST_API_TOKEN") or os.getenv("UPSTASH_REDIS_REST_TOKEN")

def db_get(k,d):
    if REDIS_URL and REDIS_URL.startswith("redis"):
        try:
            import redis
            r=redis.from_url(REDIS_URL,decode_responses=True,socket_timeout=5)
            v=r.get(k)
            return json.loads(v) if v else d
        except Exception as e: print(f"DB GET err:{e}")
    if KV_URL and KV_TOKEN and KV_URL.startswith("http"):
        try:
            resp=httpx.post(KV_URL,headers={"Authorization":f"Bearer {KV_TOKEN}"},json=["GET",k],timeout=10)
            res=resp.json().get("result")
            return json.loads(res) if res else d
        except Exception as e: print(f"DB KV GET err:{e}")
    return d

def db_set(k,v):
    s=json.dumps(v,ensure_ascii=False)
    if REDIS_URL and REDIS_URL.startswith("redis"):
        try:
            import redis
            r=redis.from_url(REDIS_URL,decode_responses=True,socket_timeout=5)
            r.set(k,s); print(f"DB SET {k}:ok"); return
        except Exception as e: print(f"DB SET err:{e}")
    if KV_URL and KV_TOKEN and KV_URL.startswith("http"):
        try: httpx.post(KV_URL,headers={"Authorization":f"Bearer {KV_TOKEN}"},json=["SET",k,s],timeout=10)
        except Exception as e: print(f"DB KV SET err:{e}")

client=OpenAI(api_key=OPENAI_KEY,base_url=BASE_URL)
tavily=TavilyClient(api_key=TAVILY_KEY) if TAVILY_KEY else None

def search_web(query:str):
    if not tavily: return "Tavily تنظیم نشده"
    try:
        res=tavily.search(query=query,max_results=5,include_answer=True,search_depth="advanced")
        out=f"Answer:{res.get('answer','')}\n"
        for r in res.get('results',[]): out+=f"- {r['title']}:{r['content'][:500]}\nURL:{r['url']}\n"
        return out[:5000]
    except Exception as e: return f"خطا جستجو:{e}"

def browse_url(url:str):
    try:
        r=httpx.get(url,timeout=15,follow_redirects=True,headers={"User-Agent":"Mozilla/5.0"})
        soup=BeautifulSoup(r.text,'html.parser')
        for s in soup(["script","style","nav","footer"]): s.decompose()
        text=soup.get_text(separator="\n")
        lines=[l.strip() for l in text.splitlines() if l.strip()]
        return "\n".join(lines)[:6000]
    except Exception as e: return f"خطا URL:{e}"

def execute_python(code:str):
    try:
        out=io.StringIO()
        with contextlib.redirect_stdout(out): exec(code,{})
        v=out.getvalue(); return v[:4000] if v else "کد بدون خروجی اجرا شد ✅"
    except Exception as e: return f"خطا اجرا:\n{traceback.format_exc()[:3000]}"

def manage_task(action:str,task:str="",task_id:int=0):
    tasks=db_get("tasks",[])
    if action=="add": tasks.append({"id":len(tasks)+1,"task":task,"status":"pending"}); db_set("tasks",tasks); return f"تسک اضافه شد:{task}"
    if action=="list": return "\n".join([f"{t['id']}. {t['task']} [{t['status']}]" for t in tasks]) if tasks else "هیچ تسکی نیست"
    if action=="done":
        for t in tasks:
            if t['id']==task_id: t['status']="done"
        db_set("tasks",tasks); return f"تسک {task_id} انجام شد"
    if action=="delete": tasks=[t for t in tasks if t['id']!=task_id]; db_set("tasks",tasks); return f"تسک {task_id} حذف شد"
    return "نامعتبر"

def remember_info(key:str,value:str="",action:str="save"):
    m=db_get("memory",{})
    if action=="save": m[key]=value; db_set("memory",m); return f"ذخیره شد {key}={value}"
    if action=="get": return m.get(key,"یافت نشد")
    if action=="list": return json.dumps(m,ensure_ascii=False,indent=2) if m else "حافظه خالی است"
    return "خطا"

def file_store(action:str,filename:str="",content:str=""):
    files=db_get("files",{})
    if action=="write": files[filename]=content; db_set("files",files); return f"فایل {filename} ذخیره شد"
    if action=="read": return files.get(filename,"فایل یافت نشد")
    if action=="list": return ", ".join(files.keys()) if files else "فایلی نیست"
    return "نامعتبر"

tools=[
 {"type":"function","function":{"name":"search_web","description":"جستجوی وب","parameters":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}}},
 {"type":"function","function":{"name":"browse_url","description":"خواندن URL","parameters":{"type":"object","properties":{"url":{"type":"string"}},"required":["url"]}}},
 {"type":"function","function":{"name":"execute_python","description":"اجرای کد پایتون","parameters":{"type":"object","properties":{"code":{"type":"string"}},"required":["code"]}}},
 {"type":"function","function":{"name":"manage_task","description":"مدیریت تسک فقط وقتی کاربر گفت تسک","parameters":{"type":"object","properties":{"action":{"type":"string","enum":["add","list","done","delete"]},"task":{"type":"string"},"task_id":{"type":"integer"}},"required":["action"]}}},
 {"type":"function","function":{"name":"remember_info","description":"حافظه بلندمدت","parameters":{"type":"object","properties":{"key":{"type":"string"},"value":{"type":"string"},"action":{"type":"string","enum":["save","get","list"]}},"required":["key","action"]}}},
 {"type":"function","function":{"name":"file_store","description":"ذخیره فایل","parameters":{"type":"object","properties":{"action":{"type":"string","enum":["write","read","list"]},"filename":{"type":"string"},"content":{"type":"string"}},"required":["action","filename"]}}},
]
SYSTEM_PROMPT="""تو یک ایجنت هوشمند و خودمختار هستی.
- برای کار پیچیده چند ابزار پشت سر هم بزن
- فقط وقتی کاربر گفت تسک از manage_task استفاده کن، برای تحقیق فقط search_web و browse_url
- اگر تحقیق خواست اول search_web بعد browse_url بعد تحلیل
- اگر کد خواست با execute_python بنویس و اجرا کن
- همیشه فارسی دقیق و کاربردی جواب بده"""

async def agent_brain(user_text:str):
    messages=[{"role":"system","content":SYSTEM_PROMPT},{"role":"user","content":user_text}]
    for step in range(4):
        try:
            r=client.chat.completions.create(model=MODEL_NAME,messages=messages,tools=tools,tool_choice="auto",timeout=30)
            msg=r.choices[0].message
            if not msg.tool_calls: return msg.content or "انجام شد ✅"
            messages.append(msg)
            for tc in msg.tool_calls:
                name=tc.function.name; args=json.loads(tc.function.arguments)
                print(f"AGENT step {step}:{name} {args}")
                if name=="search_web": res=search_web(args["query"])
                elif name=="browse_url": res=browse_url(args["url"])
                elif name=="execute_python": res=execute_python(args["code"])
                elif name=="manage_task": res=manage_task(args["action"],args.get("task",""),args.get("task_id",0))
                elif name=="remember_info": res=remember_info(args["key"],args.get("value",""),args["action"])
                elif name=="file_store": res=file_store(args["action"],args["filename"],args.get("content",""))
                else: res="ابزار ناشناخته"
                messages.append({"role":"tool","tool_call_id":tc.id,"content":str(res)[:3500]})
        except Exception as e:
            if "429" in str(e): print("429 wait 5s"); await asyncio.sleep(5); continue
            return f"خطا:{e}"
    try:
        final=client.chat.completions.create(model=MODEL_NAME,messages=messages,timeout=30)
        return final.choices[0].message.content or "تمام شد ✅"
    except Exception as e: return f"خطا جمع بندی:{e}"

async def send_chat_action(chat_id):
    async with httpx.AsyncClient() as c:
        try: await c.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendChatAction",json={"chat_id":chat_id,"action":"typing"},timeout=5)
        except: pass

async def typing_loop(chat_id,stop_event):
    while not stop_event.is_set():
        await send_chat_action(chat_id)
        try: await asyncio.wait_for(stop_event.wait(),timeout=4)
        except asyncio.TimeoutError: continue

async def send_message(chat_id,text):
    async with httpx.AsyncClient() as c:
        for i in range(0,len(text),4000):
            await c.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",json={"chat_id":chat_id,"text":text[i:i+4000]})
            print(f"Sent to {chat_id}")

async def delete_message(chat_id,msg_id):
    if not msg_id: return
    async with httpx.AsyncClient() as c:
        try: await c.post(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage",json={"chat_id":chat_id,"message_id":msg_id},timeout=5)
        except: pass

async def send_status(chat_id):
    async with httpx.AsyncClient() as c:
        try:
            r=await c.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",json={"chat_id":chat_id,"text":"⏳ دریافت شد، در حال انجام..."},"timeout":5)
            return r.json().get("result",{}).get("message_id")
        except: return None
app=FastAPI()

@app.get("/")
async def root():
    return {"status":"Agent running","redis":bool(REDIS_URL),"kv":bool(KV_URL)}

@app.post("/api/index")
async def webhook(req:Request):
    status_id=None
    stop=None
    task=None
    try:
        data=await req.json()
        msg=data.get("message") or data.get("edited_message")
        if not msg: return {"ok":True}
        chat_id=msg["chat"]["id"]
        user_id=msg["from"]["id"]
        text=(msg.get("text") or msg.get("caption") or "").strip()
        print(f"From {user_id}:{text[:200]}")
        if user_id!=ALLOWED_USER_ID:
            await send_message(chat_id,"⛔ دسترسی ندارید")
            return {"ok":True}
        if text.startswith("/start"):
            await send_message(chat_id,"سلام! من ایجنت هوشمند تو هستم 🤖\n• تحقیق: بگو تحقیق کن ...\n• کد: بگو کد بنویس و اجرا کن\n• لینک بفرست تا خلاصه کنم\n• حافظه + تسک + فایل")
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
            else: res=remember_info("یادداشت",content,"save")
            await send_message(chat_id,f"✅ {res}")
            return {"ok":True}
        if "اسم من چیه" in text or "نام من چیه" in text:
            res=remember_info("اسم","","get")
            await send_message(chat_id,f"اسم شما {res} هست 😊" if res!="یافت نشد" else "هنوز اسمت رو نمیدونم")
            return {"ok":True}
        if text.startswith("/tasks"):
            await send_message(chat_id,manage_task("list")); return {"ok":True}
        if text.startswith("/task "):
            await send_message(chat_id,manage_task("add",task=text[6:])); return {"ok":True}
        if text.startswith("/memory"):
            await send_message(chat_id,remember_info("all","","list")); return {"ok":True}

        # --- حالت در حال انجام ---
        status_id=await send_status(chat_id)
        stop=asyncio.Event()
        task=asyncio.create_task(typing_loop(chat_id,stop))

        ans=await agent_brain(text)

        stop.set()
        if task: task.cancel()
        await delete_message(chat_id,status_id)
        await send_message(chat_id, ans or "انجام شد ✅")
        return {"ok":True}
    except Exception as e:
        print(f"Webhook err:{e} {traceback.format_exc()}")
        try:
            if stop: stop.set()
            if task: task.cancel()
            if status_id: await delete_message(chat_id,status_id)
        except: pass
        try: await send_message(chat_id,f"خطا: {e}")
        except: pass
        return {"ok":False}
