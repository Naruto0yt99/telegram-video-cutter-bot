# Anime Edit Assistant — requirements

Primary channel: AnimeEmot.07

## Editing workflow
- Analyze one user-supplied episode and estimate viable edits.
- List timestamped scene candidates before rendering; flag uncertain timestamps for review.
- Support emotional, aura/sigma, fight/action, funny, sad, motivation, character-focused, skull, transition, and Twixtor-ending styles.
- User may choose ALL candidates or one candidate. Each edit is a separate clip/file; never combine candidates into a montage unless explicitly requested.
- Preserve audio/video sync and the original source file.
- Default output: vertical 9:16; configurable duration, quality, effects, and overlay.
- Minimize phone storage with temporary files, cleanup, progress reporting, retry/resume, and logs.

## Delivery format for each edit
1. Send the video as a separate message, including source timestamps, scene, style/mood, music/audio suggestions, and effects.
2. Send overlay text and main YouTube title in a separate message.
3. Send an SEO-aware description in a separate message.
4. Send one configurable sticker before moving to the next edit.

## Description template
Must be editable and support channel name/@username/socials, a 50+ word summary, anime/episode/arc, 10 alternative titles, music credits, channel goal, tags, and a copyright note. Never promise virality or claim music is copyright-free without verification.

## Provider and safety
- AI provider is configured by environment variables; never hardcode or print secrets.
- Do not pretend AI analysis occurred when no provider is configured.
- Do not automatically download from untrusted sources or overwrite the original episode.
- Do not claim timestamps are verified until checked against the actual source.
- Keep generated changes isolated until tests pass; do not run arbitrary shell commands returned by an AI model.

## Verification gates
1. Test the factory/worker integration.
2. Test the live bot.
3. Process one real episode and verify timestamps, rendering, A/V sync, and delivery.
4. Only then enable batch mode.
5. Test reboot/autostart separately.
6. Do not call the system production-ready before a real end-to-end pass.
