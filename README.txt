JO BINGO - HOW TO START (3 steps)
1) Install Python from python.org (tick "Add Python to PATH").
2) Open config.env in Notepad. Fill ONLY:
     BOT_TOKEN=  (from Telegram @BotFather -> /newbot)
     ADMIN_ID=   (your number from Telegram @userinfobot)
   Save it.
3) Double-click start.bat. Wait for "=== RUNNING ===" - it prints your Game address and Admin address.
   Open your bot in Telegram -> /start -> share phone -> 🎮 Play.
Keep the black window open while people play.

FOLDER STRUCTURE
jobingo/
  config.env        <- the only file you edit
  start.bat         <- double-click to run everything
  cloudflared.exe   <- makes the free public https address automatically
  requirements.txt
  server/
    server.py       bot + menu + game engine + admin API
    cards.json      432 different 5x5 bingo cards (B1-15 I16-30 N31-45 G46-60 O61-75, free centre)
  webapp/
    index.html      the game screen (Telegram Mini App)
  admin/            React admin website, already built (opens at <Game address>/admin)
  admin-src/        React source code (only needed if you want to change the admin: npm install, npm run build)
  bingo.db          created automatically (users and balances)

Admin website: <Game address>/admin  - username: anything, password: ADMIN_PASS from config.env.
Deposit/withdraw requests arrive in your Telegram with Approve/Reject buttons.
Note: the address changes every time you restart (free tunnel), so players must reopen via the bot's Play button.
