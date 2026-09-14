# Housely Collections Bot

## v0.7.2 — channel reliability fix

This version focuses on the exact failure seen after v0.7.1: the bot could show **“Сьогодні ... немає об'єктів”** even when the source channels contained listings.

### Main source logic

`📅 Підбірка за сьогодні` and `🆕 Тільки нові об'єкти` use the Telegram source channels:

- `@dublin_rent`
- `@irelandrent`

The public Telegram channel history is the primary source. The SQLite table is only a fallback mirror of real Telegram channel posts and publication/Undo history.

### Reliability changes in v0.7.2

- Telegram public history is retried up to 3 times when it returns an empty/anti-bot page or a temporary HTTP error.
- An unreadable Telegram page is no longer treated as proof that there are zero objects. If both the direct source and the channel mirror are unavailable, the bot shows a source error instead of a false empty collection.
- At startup the bot warms the fallback mirror from the last 72 hours of public channel history.
- Normal `channel_post` and `edited_channel_post` updates keep the mirror fresh after startup.
- Manual property posts without `Ref` are included when they have a recognizable property type, location and price.
- Posts with a real Housely `Ref` are accepted even if a future/new property wording is not yet recognized by the type parser.
- Repeated real `Ref` values are de-duplicated and only the newest Telegram post is kept.
- Manual posts without `Ref` are de-duplicated by normalized post content.
- Standard Housely footer text (`Переглядай актуальні пропозиції...`) no longer causes a normal listing to be rejected.
- Generated collection posts are ignored and cannot be parsed back as listings.
- Posts edited to `Не доступно`, `Недоступно`, `Неактуально`, `Здано/Сдано`, etc. are removed from the fallback mirror instead of remaining as stale listings.
- `📍 Lucan`, `📍 Malahide, Co. Dublin`, etc. are recognized even when the location line has no `Локація:` label.
- `Bed-space` / `bed space` / `ліжко-місце` variants are recognized.
- Telegram caption markup is supported in addition to the usual message-text markup.
- A partial collection is not produced if one source channel is unreadable and no safe fallback exists for that channel.

## Diagnostic command

Admin-only command:

`/source_status`

It reports, for each source channel:

- how many listings can be read directly from Telegram today;
- how many are available in the fallback channel-post mirror;
- whether Telegram reports the bot as available in that source channel.

If the result is `direct: ERROR` and `cache: 0`, the bot deliberately refuses to pretend that the channel is empty.

## Important for stable fallback

For the fallback mirror to receive future `channel_post` updates reliably, add the bot to both source channels (`@dublin_rent` and `@irelandrent`). In practice a Telegram bot is normally added to a channel as an administrator. It does not need to publish there just for the mirror logic, but it must be present so Telegram can deliver channel post updates.

The direct public-history reader still works independently when Telegram allows the Railway server to read the public `/s/` pages.

## Railway variables

Required:

- `BOT_TOKEN`
- `ADMIN_IDS`
- `MAIN_CHANNEL=@arpireland1`
- `FOOTER_CHANNEL_URL=https://t.me/arpireland1`
- `DUBLIN_CHANNEL=@dublin_rent`
- `IRELAND_CHANNEL=@irelandrent`
- `TIMEZONE=Europe/Dublin`
- `MAX_ITEMS=25`
- `DATA_DIR=/app/data`

Recommended source settings (defaults already match these values):

- `LIVE_SOURCE_MAX_PAGES=20`
- `LIVE_SOURCE_TIMEOUT=8`
- `LIVE_SOURCE_RETRIES=3`
- `LIVE_SOURCE_RETRY_DELAY=0.8`
- `SOURCE_SYNC_ENABLED=true`
- `SOURCE_SYNC_LOOKBACK_HOURS=72`

Legacy source-post verification settings can remain:

- `VERIFY_SOURCE_POSTS=true`
- `POST_CHECK_TIMEOUT=8`
- `POST_CHECK_CONCURRENCY=8`
- `SOURCE_SYNC_TIMEOUT=6`

## Persistent database

Keep the Railway Volume mounted at:

`/app/data`

Do **not** delete the existing volume or `collections.db` when deploying this update. Publication history and Undo depend on it, and valid channel-post mirror rows are useful as a fallback.

## Deployment test

1. Deploy this version without deleting the Railway Volume.
2. Send `/source_status` to the bot.
3. Press `📅 Підбірка за сьогодні`.
4. Confirm repeated `Ref` values appear once.
5. Confirm manual posts without `Ref` are included when they contain property type + location + price.
6. If a source cannot be read, the bot must show a source error rather than “0 objects”.
7. Publish a new listing after deployment and verify it appears in the next collection.

## Test suite

The release was checked with the existing regression suite plus new reliability tests for:

- public-page retry and recovery;
- anti-bot/empty-page handling;
- direct-source failure with and without cache;
- partial source failure;
- manual posts without Ref;
- duplicate Ref handling;
- standard Housely footer parsing;
- unavailable edited posts;
- pin-only locations;
- bed-space spelling;
- caption markup;
- current multi-room Housely listing format;
- startup mirror warm-up.
