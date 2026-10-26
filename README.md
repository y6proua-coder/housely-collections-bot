# Housely Collections Bot

## v0.9.0

The bot reads property listings from `@irelandrent` and `@dublin_rent`.
It offers two collection modes:

| Mode | Selection |
| --- | --- |
| 🏡 Усі вільні | Every available listing originally published in either source channel during the last 28 days, including listings used in earlier collections. |
| 🆕 Тільки свіжі пости | Available listings from that same 28-day window, originally published after the latest successfully published collection and not previously used in an active collection. |

The period is a rolling four weeks, not the current calendar month. Exactly 28 days old is included; older listings are excluded. The original source-post date is used. Editing an advert does not make it fresh.

Before the first successful collection, Fresh can include all eligible unused listings within the four-week window. Creating, editing, regenerating or cancelling a preview does not move the publication cutoff. Both All and Fresh publications move it when publication succeeds.

## Availability

An available advert must contain an actionable contact: a Telegram manager handle or link, a telephone number, WhatsApp link, or email address. Links advertising only the configured property channels or the main channel do not count as contact details. Contact links hidden behind formatted text are preserved in the public-page reader and SQLite cache.

The marker `🔴 БІЛЬШЕ НЕ ДОСТУПНО 🔴` excludes a post, even if the previous advert and some old contact details remain below it. Bold formatting, different letter case, repeated spaces, and the marker appearing anywhere in the caption are supported. Existing Ukrainian, Russian and English closed-status variants remain supported.

Ordinary adverts are identified by property type, location, price and/or Ref. Collection posts and service advertisements are not treated as property listings.

Availability is checked again against the current source caption immediately before publication. A removed contact, a closed-status marker, or a deleted source post stops publication and asks the admin to regenerate. An unreadable source is not assumed available.

## Full collections and editing

Every eligible listing is included in the preview. A large collection is split into several Telegram messages with a short `Частина 1/N` label. `MAX_ITEMS` controls the maximum number of listings per message, not the number of listings in the whole collection. Message-length limits can create smaller parts.

Each part has its own Edit button. Publish, Regenerate and Cancel are shown on the final part. Publish sends the whole collection; Edit changes only the selected part. Existing previews are protected by their own token so an older button cannot act on a newer collection.

The line `➕ Ще … пропозицій не показано в цій підбірці` is never generated. Edit also removes it if it is pasted from an older collection.

Telegram rate-limit responses are retried after their specified delay. If a later part fails, the bot attempts to remove the incomplete messages in that destination. Any remaining partial messages are recorded as used, but an incomplete collection does not move the cutoff for Fresh. A collection published completely in at least one selected destination counts as successful.

Undo removes every recorded Telegram message in the collection. Listings are eligible again only if they remain within the current window, are available, and are not covered by another active collection or its date cutoff.

## Publication history

Publication history is stored in SQLite at `DATA_DIR/collections.db`, normally `/app/data/collections.db`. Keep the existing persistent volume mounted at `/app/data`. An ordinary code update must not delete or replace that database.

The bot also recovers source-post links from recognizable, actually published collections in `MAIN_CHANNEL`, `DUBLIN_CHANNEL` and `IRELAND_CHANNEL` for the last 28 days. This recovers history lost before a persistent disk was attached and supports collections copied into a channel manually with their links intact. Private previews are never imported.

Public-history checks are reused for at most 60 seconds. Failed or incomplete history reads stop Fresh with a specific error; All can still be used independently. Recovered multipart collections move the cutoff on their final part. Locally recorded publications are authoritative for completion and cancellation.

Database additions are applied automatically at startup while preserving existing records. No database reset is required.

## Configuration

Required:

- `BOT_TOKEN`
- `ADMIN_IDS` as comma-separated Telegram user IDs, or the older `ADMIN_ID`

Default channels and storage:

- `MAIN_CHANNEL=@arpireland1`
- `DUBLIN_CHANNEL=@dublin_rent`
- `IRELAND_CHANNEL=@irelandrent`
- `FOOTER_CHANNEL_URL=https://t.me/arpireland1`
- `DATA_DIR=/app/data`
- `TIMEZONE=Europe/Dublin`
- `MAX_ITEMS=25` per message

Source reading:

- `LIVE_SOURCE_MAX_PAGES=200`
- `LIVE_SOURCE_TIMEOUT=8`
- `LIVE_SOURCE_RETRIES=3`
- `LIVE_SOURCE_RETRY_DELAY=0.8`
- `SOURCE_SYNC_ENABLED=true`
- `SOURCE_SYNC_LOOKBACK_HOURS=672` (28 days)
- `POST_CHECK_TIMEOUT=8`
- `POST_CHECK_CONCURRENCY=8`

If an older deployment explicitly sets the mirror lookback to 72 or 504 hours, set it to 672 for the full four-week fallback coverage. The public source reader always uses the fixed 28-day selection window.

Add the bot to the property source channels so Telegram delivers new and edited channel posts. Public channel history remains the primary read source; the SQLite mirror is a fallback when public pages cannot be read.

## Diagnostics

Admin commands:

- `/start`: menu and running version.
- `/id`: the caller's Telegram ID.
- `/source_status`: running version, saved and recovered publication counts, Fresh cutoff, and direct/cache listing counts for the last 28 days.

Runtime selection logs show the mode, lookback, available count and number of posts excluded from Fresh.

## Validation

Install application requirements and pytest, then run:

```bash
python -m pytest -q
```

The suite covers four-week boundaries, both source channels, actionable contacts, closed statuses, original dates after edits, publication cutoffs, recovered history, multipart previews and edits, publishing and Undo, failed-part cleanup, retries, database migration and persistence. Tests send no live Telegram messages.
