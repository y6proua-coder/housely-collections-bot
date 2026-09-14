# Housely Collections Bot

Bot for compact housing collections built directly from the public Telegram source channels.

## What changed in this version

The source of truth for `📅 Підбірка за сьогодні` and `🆕 Тільки нові об'єкти` is now Telegram itself, not the `property_posts` SQLite table.

- Before every Preview/Regenerate, the bot opens the public history of `@dublin_rent` and `@irelandrent`.
- It paginates backwards far enough to cover the requested period, instead of reading only one recent page.
- `Підбірка за сьогодні` takes posts whose Telegram publication date is today in `Europe/Dublin`.
- `Тільки нові об'єкти` takes posts published after the last successfully published collection. If there has never been a successful collection, it starts from today's midnight.
- Duplicate objects are removed before rendering. If posts have a real `Ref`, one `Ref` appears only once and the newest channel post is kept, even if it exists in both channels or was reposted.
- Manual property posts without `Ref` can also be included. They receive an internal post identity only for publication tracking; the identity is never shown to users.
- Generated collection posts are ignored so the bot does not parse its own collections back as housing objects.
- If one of the source channels cannot be read, the bot does **not** silently fall back to stale database data. It stops that Preview and asks you to retry, preventing an incomplete collection.

The old `property_posts` table and channel update handler remain in the project as a legacy cache, but they are no longer used to build Today/New collections. SQLite is still required for publication history, undo, and the "last successful collection" boundary.

## Collection format

The bot extracts location, price, object type, audience and original Telegram post URL, then groups objects by type and location. It keeps `Детальніше` links clickable and restores standard formatting after manual edits.

Modes:

- `📅 Підбірка за сьогодні` — live objects published today in the two source channels.
- `🆕 Тільки нові об'єкти` — live objects published after the last successful collection.

Preview controls remain: Publish / Edit / Regenerate / Cancel. Publishing can go to the main channel, either object channel, or both object channels. The undo button deletes all copies created by that publication and restores the previous New-mode boundary behavior.

## Railway Variables

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

Live channel reading:

- `LIVE_SOURCE_MAX_PAGES=20` — maximum public-history pages read per source channel for one collection.
- `LIVE_SOURCE_TIMEOUT=8` — timeout in seconds for each Telegram public-page request.

Legacy cache/check settings can remain unchanged; they are not the source for Today/New collections:

- `VERIFY_SOURCE_POSTS=true`
- `POST_CHECK_TIMEOUT=8`
- `POST_CHECK_CONCURRENCY=8`
- `SOURCE_SYNC_ENABLED=true`
- `SOURCE_SYNC_TIMEOUT=6`

The source channels must be public because the collection builder reads `https://t.me/s/<channel>` history directly. The bot should still be an administrator in destination channels so it can publish and, if required, delete messages with Undo.

Multiple admins example:

`ADMIN_IDS=1231023850,987654321`

## Persistent database

SQLite is stored at `/app/data/collections.db`. Keep the Railway Volume mounted at `/app/data`. The database is now used mainly for successful-publication history and Undo; it is no longer the primary source of property objects for collection creation.

## Test after deployment

1. Deploy this version.
2. Ensure `@dublin_rent` and `@irelandrent` are public.
3. Publish or manually add several property posts, including a repeated Ref if you want to test de-duplication.
4. Send `/start` to the bot.
5. Press `📅 Підбірка за сьогодні`.
6. Confirm that each Ref appears once and that a manual property post without Ref is also included if it contains a recognizable property type, location and price.
7. Publish the collection, add another object, then press `🆕 Тільки нові об'єкти` and confirm only posts after the successful publication are included.

## Telegram ID helper

Send `/id` to the bot from the employee account. The bot replies with that account's numeric Telegram ID; add it to `ADMIN_IDS` and redeploy.
