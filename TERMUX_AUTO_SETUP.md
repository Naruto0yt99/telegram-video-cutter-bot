# Termux automatic operation

## What start_bot.sh does

- fetches the latest `main` from GitHub at startup
- fast-forwards the local checkout when there are no local edits
- installs Python requirements only when `requirements.txt` changes
- removes stale `bot.py` processes
- starts `bot.py`
- restarts it automatically after an unexpected crash
- writes supervisor/bot output to `logs/bot.log`

Run it manually with:

    bash ~/telegram-video-cutter-bot/start_bot.sh

Do not run a second copy while the first supervisor is active.

## Start after Android reboot

Install/open Termux:Boot once, then copy the repository boot script:

    mkdir -p ~/.termux/boot
    cp ~/telegram-video-cutter-bot/termux_boot/01-anime-bot ~/.termux/boot/01-anime-bot
    chmod +x ~/telegram-video-cutter-bot/start_bot.sh
    chmod +x ~/.termux/boot/01-anime-bot

Android may still stop background Termux processes. Disable battery optimization for Termux and Termux:Boot if the phone allows it. A boot script improves convenience but is not a guarantee against Android process killing.

## Logs

    tail -f ~/telegram-video-cutter-bot/logs/bot.log

## Test URLs

The four acceptance-test URLs supplied for the current FIND test are stored in `TEST_URLS.txt`.
