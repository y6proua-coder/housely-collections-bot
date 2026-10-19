# Housely Collections Bot

## v0.8.1 - fresh posts or all available listings from the last 3 weeks

Both modes use a rolling **21-day window** from the time a collection is generated. A post published exactly 21 days ago is included; older posts are excluded. The original Telegram publication date is used, so editing a post does not renew its age. The window is checked again before publishing a saved preview.

The main menu now has exactly two options:

- `🆕 Тільки свіжі пости`: available source posts from the last 21 days that have not appeared in a successfully published, non-cancelled collection. A post can be older than today or the last collection, within that window. Existing publication history is reused, including posts published through the old Today mode or the new All mode. Identity is checked by Ref and Telegram post URL; a repost with an already used Ref is not fresh.
- `🏡 Усі вільні`: available posts from the last 21 days, including posts used in earlier collections. This replaces the Today button. Older Today buttons also open All.

Both modes scan source history back to the 21-day cutoff, ignore duplicate listings and reject posts marked unavailable even when the original advert remains in the channel. The team's exact marker `🔴 БІЛЬШЕ НЕ ДОСТУПНО 🔴` is recognized, including bold formatting, different letter case and repeated spaces. It may appear at the top, in the middle or at the end of the post or photo caption. Other supported status lines include `Більше недоступно`, `Больше не доступно`, `Об'єкт більше недоступний`, `Не доступно`, `Здано`, `Сдано` and `No longer available`. An amenity detail such as `Інтернет недоступний` does not close the listing.

Freshness is recorded only after Telegram accepts publication to at least one destination. Preview, Edit, Regenerate, Cancel, hidden items and lines removed from a preview do not consume listings. Undo restores eligibility only when the post is no longer used by another active collection. Once a collection is published and recorded, its preview is cleared to prevent accidental repeated publication.

Before publication the bot checks the current source-post text again. If a listing has become unavailable, been deleted, or been used in another collection while a Fresh preview was open, publication stops and the admin is asked to regenerate. If current availability cannot be read, publication stops for a retry.

Direct public channel history remains the primary source. Cached fallback listings are checked against their current Telegram captions before use in either mode, even if the legacy `VERIFY_SOURCE_POSTS` switch is disabled. Unreadable current captions cause a source error instead of being assumed available. The cache may cover only posts the bot previously received or synced; if a public-history read fails, it cannot recover older posts absent from that cache.

Pagination counts media-only posts, detects a stalled scan and refuses a silently truncated direct scan. `LIVE_SOURCE_MAX_PAGES` defaults to `200`. Update an explicitly configured value of `20` to `200` if the 21-day source history needs more pages. The default startup mirror lookback is now `SOURCE_SYNC_LOOKBACK_HOURS=504` (21 days); update an explicit old value of `72` to `504` for the same fallback coverage.

The existing `MAX_ITEMS=25` and Telegram message-size limit still apply to each collection. Additional matches are shown as a hidden count. Hidden posts remain eligible for the next Fresh collection; All always considers the full available set again.

Keep the existing `/app/data/collections.db` and persistent volume. No reset, new token or new database is required. This archive updates code only; deploying it is a separate step.

Validation: `python -m pytest -q` (install requirements and pytest first). Tests cover both modes, the exact 21-day boundary, exclusion before item limits, original publication dates after edits, old unused posts within the window, existing publication history, hidden items, Undo, status edits, cache checks, preview expiry, publication-time checks and history pagination.

## v0.7.3 — updated collection message templates

Both collection modes now use the approved Ukrainian intro, with different headlines:

- **Today:** `🏡 Всім привіт! Зібрали для вас підбірку новинок за сьогодні!`
- **Only new:** `🆕 Нові пропозиції житла вже тут!`

The introduction above the generated property list is identical in both modes: viewing prompt, invitation to contact Housely about a viewing, and clickable links to `https://t.me/team_housely` and WhatsApp `+353 89 448 4275` (`https://wa.me/353894484275`). The existing end-of-post channel footer remains unchanged. Property selection, collection history, Undo and database settings are unchanged.


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

- `LIVE_SOURCE_MAX_PAGES=200`
- `LIVE_SOURCE_TIMEOUT=8`
- `LIVE_SOURCE_RETRIES=3`
- `LIVE_SOURCE_RETRY_DELAY=0.8`
- `SOURCE_SYNC_ENABLED=true`
- `SOURCE_SYNC_LOOKBACK_HOURS=504`

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
