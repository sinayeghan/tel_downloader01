# Trendify Media Downloader (نسخه بدون نیاز به کوکی)

این نسخه بر پایه مخزن اصلی بازنویسی شده است تا با استفاده از **Deno** و سرور محلی **bgutil-ytdlp-pot-provider** به‌صورت خودکار توکن اعتبارسنجی یوتیوب (**PO Token**) تولید کند و دیگر نیازی به وارد کردن و تمدید مداوم `youtube_cookies.txt` نباشد.

## تنظیمات استقرار روی Render.com (بدون Docker)

1. فایل‌های `main.py` و `requirements.txt` را در یک مخزن **Public** گیت‌هاب آپلود کنید.
2. در پنل [Render](https://render.com) گزینه **New -> Web Service** را بزنید و مخزن گیت‌هاب خود را متصل کنید.
3. تنظیمات سرویس را به شکل زیر وارد کنید:
   - **Runtime:** `Python`
   - **Build Command:**
     ```bash
     pip install -r requirements.txt && pip install --pre -U "yt-dlp[default]" && export DENO_INSTALL="$PWD/.deno" && curl -fsSL https://deno.land/install.sh | sh && rm -rf bgutil-server && git clone --depth 1 --single-branch --branch 2.0.2 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git bgutil-server && (cd bgutil-server/server && "$DENO_INSTALL/bin/deno" install --allow-scripts=npm:canvas --frozen)
     ```
   - **Start Command:**
     ```bash
     python main.py
     ```
4. در بخش **Environment Variables** متغیرهای زیر را تعریف کنید:
   - `BOT_TOKEN`: توکن ربات از `@BotFather`
   - `WEBHOOK_SECRET`: یک رشته تصادفی ۳۰ کاراکتری
   - `WEBHOOK_PATH`: مقدار `telegram`
   - `PROXY_URL` *(اختیاری)*: فقط در صورتی که IP سرور Render به‌کلی توسط یوتیوب بلاک شد، آدرس یک پروکسی مسکونی (Residential) را وارد کنید (این پروکسی هم به `yt-dlp` و هم به سرور `bgutil` اعمال می‌شود).
5. نیازی به ساخت **Secret File** برای کوکی‌ها نیست. روی **Deploy Web Service** کلیک کنید.
