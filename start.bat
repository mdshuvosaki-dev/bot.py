@echo off
rem Windows — installs what is needed, then keeps bot.py running (restarts if it ever stops)
cd /d "%~dp0"
py -m pip install -q -r requirements.txt
:loop
py bot.py
echo bot.py stopped - restarting in 5 seconds (close this window to quit)
timeout /t 5 /nobreak >nul
goto loop
