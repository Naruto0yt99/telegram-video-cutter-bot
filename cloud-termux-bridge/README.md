# Cloud Termux Bridge

Isolated queue service for the Termux remote-control bot. This directory is on the `cloud-termux-bridge` branch and does not change `main`.

## Render environment variables

Set these in the Render service dashboard (never commit secrets):

- `DATABASE_URL`: Internal connection URL for the `termux-bridge-queue` Render Postgres database.
- `BRIDGE_SECRET`: A long random secret shared only with the Termux agent.
- `OWNER_ID`: Your numeric Telegram user ID.
- `TELEGRAM_BOT_TOKEN`: Token for the Telegram bot that will accept `/run` and `/status` commands.

The cloud service must become the only polling process for that bot token. Stop the local polling process before enabling the same token in Render, otherwise Telegram polling conflicts. Do not stop the existing bot until the cloud service and local agent are configured and tested.

The agent endpoints require `Authorization: Bearer <BRIDGE_SECRET>`. Do not expose them without authentication. `/health` checks database connectivity.

## Limitations

A command waits in Postgres while Termux is offline and runs only after the phone is on, online, and the local agent is running. Render's free resources can sleep or expire; this is not a guaranteed 24/7 service.
