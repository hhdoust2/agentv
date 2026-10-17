# agentv — ایجنت هوشمند تلگرام روی Vercel

## متغیرهای محیطی (Vercel → Settings → Environment Variables)
| نام | توضیح |
|---|---|
| TELEGRAM_BOT_TOKEN | توکن ربات از BotFather |
| OPENAI_API_KEY | کلید API مدل |
| OPENAI_BASE_URL | (اختیاری) آدرس سرویس سازگار با OpenAI |
| MODEL_NAME | (اختیاری) پیش‌فرض gpt-4o |
| TAVILY_API_KEY | (اختیاری) برای جستجوی وب |
| ALLOWED_USER_ID | آیدی عددی تلگرام خودت (الزامی) |
| WEBHOOK_SECRET | یک رمز دلخواه (حروف/عدد/_ و -) — **حتماً بگذار** |
| REDIS_URL یا KV_REST_API_URL + KV_REST_API_TOKEN | دیتابیس برای حافظه/تسک/تاریخچه |
| ENABLE_PYTHON | (اختیاری) `1` = اجرای کد پایتون فعال. پیش‌فرض خاموش |
| HISTORY_LEN | (اختیاری) تعداد پیام‌های قبلی در حافظه مکالمه، پیش‌فرض 6 (۰ = خاموش) |

## ست کردن وبهوک
این آدرس را در مرورگر باز کن (سه مقدار را عوض کن):

https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<DOMAIN>/api/index&secret_token=<WEBHOOK_SECRET>&drop_pending_updates=true

## دستورات
/start ، /reset ، /tasks ، /task متن ، /memory
