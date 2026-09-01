# Housely Collections Bot — MVP

Bot for compact daily housing collections.

## What it does

- Watches new property posts in `@dublin_rent` and `@irelandrent`.
- Reads only posts that contain a `Ref`.
- Extracts Ref, location, price, short description and original post URL.
- Lets admins create today's collection in private chat.
- Preview buttons: Publish / Edit / Regenerate / Cancel.
- Can publish to the main channel, either object channel, or both object channels.
- Before every Preview/Regenerate, checks the public Telegram source links and
  removes posts that were deleted directly in the channel from SQLite.
- Keeps `Детальніше` links clickable after manual editing. Telegram link
  entities are preserved; missing entities are rebuilt from the Preview data.
- Automatically adds the mandatory official-channel footer to the end of every
  collection and restores it if it was removed during manual editing.

## Important limitation

Telegram Bot API does not backfill old channel history. The bot will only collect posts it receives after it is running in Railway and present in the source channels.

For the first test:
1. Deploy the bot.
2. Publish one **new** property post with a `Ref` in `@dublin_rent` or `@irelandrent`.
3. Send `/start` to the bot in private chat.
4. Press `📋 Створити підбірку`.

## Railway Variables

Add:

- `BOT_TOKEN`
- `ADMIN_IDS`
- `MAIN_CHANNEL=@arpireland1`
- `FOOTER_CHANNEL_URL=https://t.me/arpireland1`
- `DUBLIN_CHANNEL=@dublin_rent`
- `IRELAND_CHANNEL=@irelandrent`
- `TIMEZONE=Europe/Dublin`
- `MAX_ITEMS=25`
- `DATA_DIR=/app/data`
- `VERIFY_SOURCE_POSTS=true`
- `POST_CHECK_TIMEOUT=8`
- `POST_CHECK_CONCURRENCY=8`

The source channels must stay public for live deletion checks. A temporary
Telegram/network error is fail-safe: the item remains in the collection. Only a
definitive "post not found" response removes it from the local database.

Multiple admins example:

`ADMIN_IDS=1231023850,987654321`

## Persistent database

The bot uses SQLite at `/app/data/collections.db`.

For persistent storage, add a Railway Volume mounted at:

`/app/data`

Without a Volume, the test database can disappear after redeploy/restart.

## How an employee finds their Telegram ID

Send `/id` to this bot from that Telegram account. The bot replies with that account's own numeric Telegram ID. Add it to `ADMIN_IDS` and redeploy.
