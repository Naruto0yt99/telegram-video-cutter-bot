# Autonomous Project Worker (isolated lab)

This folder is an isolated experiment on the `autonomous-worker-lab` branch. It does not alter the default branch or the running Telegram bot.

## Current state
- Durable SQLite task queue and checkpoint records: implemented in the starter.
- Provider-backed code generation: requires a configured OpenAI-compatible API endpoint and API key.
- No API credentials are stored in this repository.
- This is not yet a deployed, always-on service. GitHub Actions is event-driven and free-tier quotas/limits apply.

## Safety
- Keep this branch separate from `main`.
- The worker must never print credentials.
- Generated changes must remain in the isolated working tree and be reviewed before merging.
- Never run generated shell commands from an AI response. Only run explicitly configured test commands.
- Do not connect this worker to existing bot repositories automatically.

## Provider configuration
Set these as environment variables in the environment that runs the worker:
- `AI_BASE_URL`: OpenAI-compatible chat completions base URL
- `AI_API_KEY`: provider API key
- `AI_MODEL`: model identifier

A free model/provider may have usage limits and may change availability. Without credentials, the worker should remain in queue/demo mode rather than pretending AI work is happening.

## Next milestone
Connect a real free-tier provider, then test on a disposable sample project before pointing it at any existing project.
