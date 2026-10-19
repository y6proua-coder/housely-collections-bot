import os
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="housely-mode-tests-"))

import main


LISTING = "🏠 Кімната\n📍 Dublin 8\n💶 €900\nRef 1234"
TEST_NOW = datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc)


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return TEST_NOW.astimezone(tz) if tz else TEST_NOW.replace(tzinfo=None)


def item(number, raw_text=LISTING):
    posted_at = TEST_NOW - timedelta(days=14) + timedelta(minutes=number)
    return {
        "id": None,
        "channel_username": "irelandrent",
        "message_id": number,
        "ref": str(number).zfill(7),
        "real_ref": str(number).zfill(7),
        "location": "Dublin 8",
        "price": "€900",
        "description": "Кімната",
        "property_type": "Кімната",
        "audience": None,
        "post_url": f"https://t.me/irelandrent/{number}",
        "raw_text": raw_text,
        "local_date": posted_at.astimezone(main.TZ).date().isoformat(),
        "created_at_utc": posted_at.isoformat(),
    }


def query_update():
    query = SimpleNamespace(
        answer=AsyncMock(),
        message=SimpleNamespace(reply_text=AsyncMock()),
    )
    return SimpleNamespace(effective_user=SimpleNamespace(id=123), callback_query=query)


class TestCollectionModes(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(main, "DB_PATH", Path(self.temp.name) / "collections.db"),
            patch.object(main, "SOURCE_CHANNELS", {"irelandrent"}),
            patch.object(main, "ADMIN_IDS", {123}),
            patch.object(main.httpx, "AsyncClient", return_value=AsyncMock()),
            patch.object(main, "datetime", FixedDateTime),
        ]
        for p in self.patches:
            p.start()
        main.init_db()
        main.PREVIEWS.clear()

    def tearDown(self):
        main.PREVIEWS.clear()
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def publish_record(self, properties, mode="new", message_id=100):
        return main.record_publication(
            123, mode, "collection", properties,
            [{"destination": "@main", "chat_id": -100, "message_id": message_id}],
        )

    async def test_new_uses_actual_items_and_keeps_old_omitted_posts(self):
        used, omitted = item(2), item(3)
        self.publish_record([used])
        with patch.object(main, "fetch_live_channel_objects", new=AsyncMock(return_value=[used, omitted])) as fetch:
            rows, _, _ = await main.get_live_properties("new")
        self.assertEqual([row["ref"] for row in rows], [omitted["ref"]])
        fetch.assert_awaited_once_with("irelandrent", TEST_NOW - timedelta(days=21))

    async def test_first_new_collection_has_no_midnight_cutoff(self):
        with patch.object(main, "fetch_live_channel_objects", new=AsyncMock(return_value=[item(2)])):
            rows, _, _ = await main.get_live_properties("new")
        self.assertEqual(len(rows), 1)

    async def test_all_includes_old_previously_used_posts(self):
        old = item(2)
        self.publish_record([old])
        with patch.object(main, "fetch_live_channel_objects", new=AsyncMock(return_value=[old])) as fetch:
            rows, _, _ = await main.get_live_properties("all")
        self.assertEqual(len(rows), 1)
        fetch.assert_awaited_once_with("irelandrent", TEST_NOW - timedelta(days=21))

    async def test_publication_in_all_mode_consumes_posts_for_fresh_mode(self):
        self.publish_record([item(2)], mode="all")
        with patch.object(main, "fetch_live_channel_objects", new=AsyncMock(return_value=[item(2)])):
            rows, _, _ = await main.get_live_properties("new")
        self.assertEqual(rows, [])

    async def test_hidden_items_remain_fresh_for_next_collection(self):
        with patch.object(main, "MAX_ITEMS", 1), patch.object(
            main, "fetch_live_channel_objects", new=AsyncMock(return_value=[item(2), item(3), item(4)])
        ):
            first, hidden, _ = await main.get_live_properties("new")
            self.assertEqual(hidden, 2)
            self.publish_record(first)
            following, hidden, _ = await main.get_live_properties("new")
            self.assertEqual(hidden, 1)
            self.assertNotEqual(first[0]["post_url"], following[0]["post_url"])

    def test_repost_with_previously_used_ref_is_not_fresh(self):
        self.publish_record([item(2)])
        repost = item(3)
        repost["ref"] = item(2)["ref"]
        self.assertEqual(main.exclude_collected_properties([repost]), [])

    def test_manual_post_without_ref_is_tracked_by_url(self):
        manual = item(2)
        manual["ref"] = "post:irelandrent:2"
        self.publish_record([manual])
        self.assertEqual(main.exclude_collected_properties([manual]), [])

    def test_publication_without_sent_messages_does_not_consume_posts(self):
        main.record_publication(123, "new", "draft", [item(2)], [])
        self.assertEqual(len(main.exclude_collected_properties([item(2)])), 1)

    def test_undo_one_collection_does_not_restore_post_used_in_another(self):
        first = self.publish_record([item(2)])
        self.publish_record([item(2)], mode="all", message_id=101)
        for message in main.get_active_publication_messages(first):
            main.mark_publication_message_deleted(message["id"])
        main.finish_publication_undo(first)
        self.assertEqual(main.exclude_collected_properties([item(2)]), [])

    async def test_both_modes_reject_closed_listings(self):
        closed = item(3, LISTING + "\n❌ Більше недоступно")
        with patch.object(main, "fetch_live_channel_objects", new=AsyncMock(return_value=[item(2), closed])):
            for mode in ("new", "all"):
                with self.subTest(mode=mode):
                    rows, _, _ = await main.get_live_properties(mode)
                    self.assertEqual([row["message_id"] for row in rows], [2])

    async def test_fallback_checks_current_caption_even_if_verification_disabled(self):
        main.upsert_property_post("irelandrent", -1001, 2, LISTING, TEST_NOW)
        with patch.object(main, "fetch_live_channel_objects", new=AsyncMock(side_effect=main.LiveChannelReadError("blocked"))), \
             patch.object(main, "check_source_post", new=AsyncMock(return_value=False)) as check, \
             patch.object(main, "VERIFY_SOURCE_POSTS", False):
            rows, _, _ = await main.get_live_properties("all")
        self.assertEqual(rows, [])
        self.assertTrue(check.await_args.kwargs["require_text"])
        self.assertEqual(main.get_cached_channel_objects("irelandrent"), [])

    async def test_unknown_fallback_availability_is_error(self):
        main.upsert_property_post("irelandrent", -1001, 2, LISTING, TEST_NOW)
        with patch.object(main, "fetch_live_channel_objects", new=AsyncMock(side_effect=main.LiveChannelReadError("blocked"))), \
             patch.object(main, "check_source_post", new=AsyncMock(return_value=None)):
            with self.assertRaises(main.LiveChannelReadError):
                await main.get_live_properties("all")

    async def test_publish_rechecks_closed_listing_and_sends_nothing(self):
        main.PREVIEWS[123] = {"mode": "all", "properties": [item(2)], "text": "preview"}
        update = query_update()
        context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        with patch.object(main, "check_source_post", new=AsyncMock(return_value=False)):
            await main.publish_to(update, context, ["@main"])
        context.bot.send_message.assert_not_awaited()
        self.assertIn("Regenerate", update.callback_query.message.reply_text.await_args.args[0])

    async def test_stale_fresh_preview_cannot_publish_used_posts(self):
        main.PREVIEWS[123] = {"mode": "new", "properties": [item(2)], "text": "preview"}
        self.publish_record([item(2)])
        context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        await main.publish_to(query_update(), context, ["@main"])
        context.bot.send_message.assert_not_awaited()

    async def test_successful_publish_records_history_and_clears_preview(self):
        main.PREVIEWS[123] = {"mode": "new", "properties": [item(2)], "text": "preview"}
        sent = SimpleNamespace(chat=SimpleNamespace(id=-100), message_id=100)
        context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(return_value=sent)))
        with patch.object(main, "check_source_post", new=AsyncMock(return_value=True)):
            await main.publish_to(query_update(), context, ["@main"])
        self.assertNotIn(123, main.PREVIEWS)
        self.assertEqual(main.exclude_collected_properties([item(2)]), [])

    def test_home_has_exactly_two_modes(self):
        buttons = [button for row in main.home_keyboard().inline_keyboard for button in row]
        self.assertEqual([button.callback_data for button in buttons], ["create_new", "create_all"])

    async def test_both_modes_include_21_day_boundary_and_exclude_older_posts(self):
        edge, expired, recent = item(2), item(3), item(4)
        edge["created_at_utc"] = (TEST_NOW - timedelta(days=21)).isoformat()
        expired["created_at_utc"] = (TEST_NOW - timedelta(days=21, seconds=1)).isoformat()
        with patch.object(main, "fetch_live_channel_objects", new=AsyncMock(return_value=[edge, expired, recent])):
            for mode in ("all", "new"):
                with self.subTest(mode=mode):
                    rows, _, _ = await main.get_live_properties(mode)
                    self.assertEqual({row["message_id"] for row in rows}, {2, 4})

    async def test_old_posts_are_excluded_before_item_limit(self):
        expired, recent = item(2), item(3)
        expired["created_at_utc"] = (TEST_NOW - timedelta(days=22)).isoformat()
        with patch.object(main, "MAX_ITEMS", 1), patch.object(
            main, "fetch_live_channel_objects", new=AsyncMock(return_value=[expired, recent])
        ):
            rows, hidden, _ = await main.get_live_properties("all")
        self.assertEqual([row["message_id"] for row in rows], [3])
        self.assertEqual(hidden, 0)

    async def test_old_fallback_posts_are_not_verified_or_included(self):
        main.upsert_property_post("irelandrent", -1001, 2, LISTING, TEST_NOW - timedelta(days=22))
        main.upsert_property_post("irelandrent", -1001, 3, LISTING.replace("1234", "1235"), TEST_NOW - timedelta(days=3))
        with patch.object(main, "fetch_live_channel_objects", new=AsyncMock(side_effect=main.LiveChannelReadError("blocked"))), \
             patch.object(main, "check_source_post", new=AsyncMock(return_value=True)) as check:
            rows, _, _ = await main.get_live_properties("all")
        self.assertEqual([row["message_id"] for row in rows], [3])
        self.assertEqual(check.await_count, 1)

    def test_source_edit_does_not_renew_original_post_date(self):
        original_date = TEST_NOW - timedelta(days=22)
        main.upsert_property_post("irelandrent", -1001, 2, LISTING, original_date)
        main.upsert_property_post("irelandrent", -1001, 2, LISTING + "\nНові деталі", original_date)
        rows, _ = main.get_uncollected_properties(apply_limit=False)
        self.assertEqual(rows, [])
        with main.db() as conn:
            row = conn.execute("SELECT created_at_utc, updated_at_utc FROM property_posts").fetchone()
        self.assertEqual(row["created_at_utc"], original_date.isoformat())
        self.assertEqual(row["updated_at_utc"], TEST_NOW.isoformat())

    async def test_publish_rejects_preview_that_has_aged_past_three_weeks(self):
        expired = item(2)
        expired["created_at_utc"] = (TEST_NOW - timedelta(days=21, seconds=1)).isoformat()
        main.PREVIEWS[123] = {"mode": "all", "properties": [expired], "text": "preview"}
        update = query_update()
        context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        await main.publish_to(update, context, ["@main"])
        context.bot.send_message.assert_not_awaited()
        self.assertIn("3 тижні", update.callback_query.message.reply_text.await_args.args[0])

    def test_recent_filter_compares_utc_offsets_and_rejects_invalid_dates(self):
        boundary, expired, invalid = item(2), item(3), item(4)
        boundary["created_at_utc"] = "2026-09-28T11:00:00+01:00"
        expired["created_at_utc"] = "2026-09-28T10:59:59+01:00"
        invalid["created_at_utc"] = "bad date"
        rows = main.filter_recent_properties([boundary, expired, invalid], now_utc=TEST_NOW)
        self.assertEqual([row["message_id"] for row in rows], [2])


class TestAvailabilityAndPagination(unittest.IsolatedAsyncioTestCase):
    def test_exact_housely_marker_is_rejected_anywhere_in_listing(self):
        for marker in (
            "🔴 **БІЛЬШЕ НЕ ДОСТУПНО** 🔴",
            "🔴 БІЛЬШЕ НЕ ДОСТУПНО 🔴",
            "🔴 більше не доступно 🔴",
            "🔴 БІЛЬШЕ\u00a0 НЕ  ДОСТУПНО 🔴",
        ):
            for text in (marker + "\n" + LISTING, LISTING + "\n" + marker,
                         LISTING.replace("\n", "\n" + marker + "\n", 1)):
                with self.subTest(marker=marker, text=text):
                    self.assertTrue(main.is_inactive_property_post(text))
                    self.assertIsNone(main.parse_live_property(text, "irelandrent", 2))

    def test_exact_housely_marker_in_bold_telegram_caption_is_rejected(self):
        body = ('<div class="tgme_widget_message_caption">'
                '🔴 <b>БІЛЬШЕ НЕ ДОСТУПНО</b> 🔴<br>'
                + LISTING.replace("\n", "<br>") + '</div>')
        self.assertIs(main.classify_post_check_response(200, body, require_text=True), False)

    def test_closed_status_anywhere_and_with_icons(self):
        for status in (
            "❌ Більше недоступно", "🚫 Больше не доступно", "⛔ Об'єкт більше недоступний",
            "❌ Цей об’єкт вже недоступний", "⚠️ Недоступно", "ЗДАНО", "СДАНО", "No longer available",
        ):
            with self.subTest(status=status):
                self.assertIsNone(main.parse_live_property(LISTING + "\n\n" + status, "irelandrent", 2))

    def test_amenity_unavailability_does_not_close_listing(self):
        for detail in ("Інтернет недоступний", "Паркінг недоступний", "Без тварин", "Доступно з листопада"):
            with self.subTest(detail=detail):
                self.assertIsNotNone(main.parse_live_property(LISTING + "\n" + detail, "irelandrent", 2))

    def test_existing_embed_with_closed_caption_is_excluded(self):
        body = '<div class="tgme_widget_message_caption">' + LISTING.replace("\n", "<br>") + '<br>❌ Більше недоступно</div>'
        self.assertIs(main.classify_post_check_response(200, body, require_text=True), False)

    def test_strict_check_needs_readable_caption(self):
        self.assertIsNone(main.classify_post_check_response(200, 'data-post="x/2"', require_text=True))
        body = '<div class="tgme_widget_message_text">' + LISTING + '</div>'
        self.assertIs(main.classify_post_check_response(200, body, require_text=True), True)

    def test_all_collection_is_not_reimported_as_a_property(self):
        text = main.build_collection([item(2)], mode="all")
        self.assertIsNone(main.parse_live_property(text, "irelandrent", 9))

    @staticmethod
    def response(number, *, more=True):
        older = '<a class="tme_messages_more" data-before="2"></a>' if more else ""
        return SimpleNamespace(
            text=f'<section class="tgme_channel_history">{older}<div data-post="irelandrent/{number}">'
                 f'<div class="tgme_widget_message_text">{LISTING.replace(chr(10), "<br>")}</div>'
                 '<time datetime="2025-01-01T10:00:00+00:00"></time></div></section>',
            raise_for_status=lambda: None,
        )

    async def test_all_reads_older_pages_until_actual_history_end(self):
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.get.side_effect = [self.response(20), self.response(10, more=False)]
        with patch.object(main.httpx, "AsyncClient", return_value=client):
            rows = await main.fetch_live_channel_objects("irelandrent")
        self.assertEqual({row["message_id"] for row in rows}, {20, 10})
        self.assertEqual(client.get.await_count, 2)

    async def test_page_limit_is_error_instead_of_silent_partial_all(self):
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.get.return_value = self.response(20)
        with patch.object(main.httpx, "AsyncClient", return_value=client), patch.object(main, "LIVE_SOURCE_MAX_PAGES", 1):
            with self.assertRaises(main.LiveChannelReadError):
                await main.fetch_live_channel_objects("irelandrent")

    def test_media_only_posts_still_advance_pagination(self):
        body = '<div data-post="irelandrent/20"><time datetime="2025-01-01T10:00:00+00:00"></time></div>'
        rows = main._parse_channel_page_messages("irelandrent", body)
        self.assertEqual(rows[0]["message_id"], 20)
        self.assertEqual(rows[0]["text"], "")
