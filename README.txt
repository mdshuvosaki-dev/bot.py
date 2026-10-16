TECH BD INCOME — TELEGRAM BOT (bot.py)           সব ফাইল তৈরি, শুধু চালালেই হবে
================================================================================

এই ফোল্ডারে যা আছে
  bot.py                   বট (Withdraw নোটিফিকেশন, Refer, Broadcast, DM নোটিফিকেশন)
  serviceAccountKey.json   Firebase এর চাবি (গোপন)
  bot_config.json          Bot Token + Channel ID (গোপন)
  requirements.txt         দরকারি লাইব্রেরি
  start.bat / start.sh     এক ক্লিকে চালানোর ফাইল (বন্ধ হয়ে গেলে নিজে আবার চালু করে)

চালানোর নিয়ম
  Windows :  Python 3.8+ ইনস্টল থাকলে start.bat এ ডাবল ক্লিক করুন
  Linux / Mac / Termux :  bash start.sh

  প্রথমবার চালালে বট নিজে অ্যাডমিন প্যানেলের Token, Channel ID ভরে দেয় এবং "bot.py Mode" চালু করে।
  অ্যাডমিন প্যানেল পেজ একবার রিলোড করলে সবুজ "🐍 bot.py ২৪/৭" ব্যাজ দেখবেন।

২৪ ঘন্টা চালু রাখতে (যেকোনো একটা)
  • সার্ভার/VPS:   nohup bash start.sh > bot.log 2>&1 &
  • pm2:           pm2 start bot.py --interpreter python3 --name techbd-bot  &&  pm2 save
  • systemd:       /etc/systemd/system/techbd-bot.service
                     [Unit]
                     Description=Tech BD Bot
                     After=network-online.target
                     [Service]
                     WorkingDirectory=/path/to/tech-bd-bot
                     ExecStart=/usr/bin/python3 bot.py
                     Restart=always
                     RestartSec=5
                     [Install]
                     WantedBy=multi-user.target
                   তারপর:  systemctl enable --now techbd-bot
  • Termux:        termux-wake-lock  চালিয়ে তারপর bash start.sh

  কম্পিউটার/ফোন বন্ধ থাকলে বটও বন্ধ থাকবে — তাই সবসময় চালু থাকে এমন জায়গায় চালান।

গুরুত্বপূর্ণ
  1) বটকে আপনার চ্যানেলের Admin বানাতে হবে (মেসেজ পোস্ট ও এডিট করার অনুমতিসহ)।
  2) admin.html আর usre.html আগের মতো আপনার ওয়েবসাইটে আপলোড করুন। এই ফোল্ডারের ফাইলগুলো
     (বিশেষ করে serviceAccountKey.json ও bot_config.json) কখনো ওয়েবসাইটে আপলোড করবেন না।
  3) বট চালু থাকলে অ্যাডমিন পেজের ভেতরের বট বন্ধ থাকে (দুটো একসাথে চললে Telegram এ 409 error আসে)।
     bot.py বন্ধ করতে চাইলে আগে অ্যাডমিন প্যানেলে "bot.py Mode" বন্ধ করুন।
  4) Leaderboard আপডেট আগের মতোই (প্যানেল খোলা থাকলে হয়)।

সমস্যা হলে
  • "serviceAccountKey.json পাওয়া যায়নি"  → ফাইলটা bot.py এর পাশেই আছে কিনা দেখুন।
  • "Token ভুল"                            → BotFather থেকে নতুন Token নিয়ে অ্যাডমিন প্যানেলে সেভ করুন।
  • "অন্য কোথাও একই বট চলছে" (409)        → আরেকটা bot.py বা অ্যাডমিন পেজের বট চলছে; একটাই রাখুন।
  • চ্যানেলে মেসেজ যায় না                 → বট চ্যানেলের Admin কিনা এবং Channel ID (-100…) ঠিক কিনা দেখুন।
  • স্ট্যাটাস লাল                          → bot.py চলছে কিনা এবং ইন্টারনেট আছে কিনা দেখুন।

নিরাপত্তা (খুব জরুরি)
  Service-account চাবি আর Bot Token চ্যাটে/কোথাও শেয়ার হয়ে গেলে যে কেউ আপনার Firebase ডেটা ও বট নিয়ন্ত্রণ করতে পারে।
  সবকিছু ঠিকমতো চলা নিশ্চিত হলে নতুন চাবি ও নতুন Token বানিয়ে পুরনোগুলো বাতিল করুন:
    • Firebase Console → Project settings → Service accounts → নতুন private key বানান, পুরনোটা মুছুন
      (Google Cloud Console → IAM → Service Accounts → Keys থেকে পুরনো key delete)
    • Telegram @BotFather → /revoke → বট বেছে নতুন Token নিন
  নতুনগুলো serviceAccountKey.json ও bot_config.json এ বসিয়ে অ্যাডমিন প্যানেলে Token আবার সেভ করুন।
