# Housely Collections Bot

Bot for compact daily housing collections.

## What it does

- Watches new property posts directly in `@dublin_rent` and `@irelandrent`.
- Collects posts published manually or by any bot. The Publish Bot database is
  not used: a new post with a new `Ref` in a source channel is enough.
- Before every Preview, also reloads recent public posts from both source
  channels. This restores recent Refs missed during a Railway restart or
  redeploy.
- Reads only posts that contain a `Ref`.
- Extracts Ref, location, price, object type, audience and original post URL.
- Provides two collection modes:
  - `📅 Підбірка за сьогодні` — all current objects published today.
  - `🆕 Тільки нові об'єкти` — only objects that have not appeared in a
    successfully published collection yet.
- Marks an object as used only after at least one channel publication succeeds.
  Creating, editing, regenerating or cancelling a Preview does not mark it.
- Groups the collection by object type (rooms first, then bedspaces,
  apartments, studios, houses), and by location inside every type.
- Uses compact item titles: object type + audience + price + `Детальніше`.
  Marketing phrases from the original title are not copied into a collection.
- Preview buttons: Publish / Edit / Regenerate / Cancel.
- Can publish to the main channel, either object channel, or both object channels.
- Before every Preview/Regenerate, checks the public Telegram source links and
  removes posts that were deleted directly in the channel from SQLite.
- Keeps `Детальніше` links clickable after manual editing. If a collection is
  pasted back as plain text, the bot rebuilds the links and restores standard
  bold formatting for the title, type headings, locations, prices and footer.
- After publishing, shows `Скасувати й видалити публікацію`. The button deletes
  every copy created by that publish action. After a complete undo, those
  objects become available in `Тільки нові об'єкти` again.
- Automatically adds the mandatory official-channel footer to the end of every
  collection and replaces a copied/plain footer with the canonical formatting.

## Important limitation

Telegram Bot API does not provide arbitrary channel-history backfill. The bot
collects posts delivered to it while it is an administrator in the source
channels; polling also keeps pending updates enabled. The public-channel sync
recovers recent posts automatically. If a much older post is no longer present
on the public preview page and was missed entirely, repost or edit it after the
bot is running so Telegram sends a new update.

For the first test:
1. Deploy the bot.
2. Publish one **new** property post with a `Ref` in `@dublin_rent` or `@irelandrent`.
3. Send `/start` to the bot in private chat.
4. Press `📅 Підбірка за сьогодні` or `🆕 Тільки нові об'єкти`.

`Тільки нові об'єкти` is based only on successful publications. Deployment or
restart time never marks an object as used. A successful `Підбірка за сьогодні`
publication also marks its objects as used for the New mode.

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
- `SOURCE_SYNC_ENABLED=true`
- `SOURCE_SYNC_TIMEOUT=6`

The source channels must stay public for live deletion checks. A temporary
Telegram/network error is fail-safe: the item remains in the collection. Only a
definitive "post not found" response removes it from the local database.

The bot must be an administrator in both source channels so it receives manual
and automated channel posts. It also needs permission to post in every
destination channel. To use the undo button, it needs permission to delete
messages in those channels.

Multiple admins example:

`ADMIN_IDS=1231023850,987654321`

## Persistent database

The bot uses SQLite at `/app/data/collections.db`. Existing databases are
migrated automatically; do not delete the Railway Volume during deployment.

For persistent storage, add a Railway Volume mounted at:

`/app/data`

Without a Volume, the test database can disappear after redeploy/restart.

## How an employee finds their Telegram ID

Send `/id` to this bot from that Telegram account. The bot replies with that account's own numeric Telegram ID. Add it to `ADMIN_IDS` and redeploy.
