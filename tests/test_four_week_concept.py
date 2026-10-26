import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from itertools import count
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="collections-four-weeks-"))
import main


NOW = datetime(2026, 10, 26, 10, tzinfo=timezone.utc)


class Clock(datetime):
    current = NOW

    @classmethod
    def now(cls, tz=None):
        return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)


def prop(number, *, days=1, date=None, channel="irelandrent", contacts=True):
    date = date or NOW - timedelta(days=days)
    text = f"Кімната\n📍 Dublin 1\nВартість: €900\nRef {number:07d}"
    if contacts:
        text += "\nДля перегляду пишіть @team_housely"
    return {"id": None, "ref": f"{number:07d}", "real_ref": f"{number:07d}",
            "message_id": number, "channel_username": channel, "post_url": f"https://t.me/{channel}/{number}",
            "raw_text": text, "raw_html": "", "description": "Кімната", "property_type": "Кімната",
            "location": "Dublin 1", "price": "€900", "audience": None,
            "created_at_utc": date.isoformat(), "local_date": date.astimezone(main.TZ).date().isoformat()}


def update(data=""):
    return SimpleNamespace(effective_user=SimpleNamespace(id=123), callback_query=SimpleNamespace(
        data=data, answer=AsyncMock(), edit_message_text=AsyncMock(), message=SimpleNamespace(reply_text=AsyncMock())))


class TestFourWeekConcept(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Clock.current = NOW
        self.temp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(main, "DB_PATH", Path(self.temp.name) / "collections.db"),
                        patch.object(main, "SOURCE_CHANNELS", {"irelandrent", "dublin_rent"}),
                        patch.object(main, "MAIN_CHANNEL", "@main_test"), patch.object(main, "ADMIN_IDS", {123}),
                        patch.object(main, "datetime", Clock),
                        patch.object(main, "ensure_publication_history", new=AsyncMock()),
                        patch.object(main, "check_source_post", new=AsyncMock(return_value=True)),
                        patch.object(main.httpx, "AsyncClient", return_value=AsyncMock())]
        for p in self.patches:
            p.start()
        main.init_db()
        main.PREVIEWS.clear()
        main.EDIT_WAITING.clear()
        self.ids = count(500)
        self.bot = SimpleNamespace(send_message=AsyncMock(side_effect=self.sent_message), delete_message=AsyncMock())
        self.context = SimpleNamespace(bot=self.bot)

    async def sent_message(self, **kwargs):
        return SimpleNamespace(chat=SimpleNamespace(id=-1009), message_id=next(self.ids))

    def tearDown(self):
        main.PREVIEWS.clear()
        main.EDIT_WAITING.clear()
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def record(self, properties, mode="all"):
        return main.record_publication(123, mode, "collection", properties,
                                       [{"destination": "@main_test", "chat_id": -1009, "message_id": next(self.ids)}])

    async def select(self, mode, properties):
        async def fetch(channel, threshold):
            return [p for p in properties if p["channel_username"] == channel]
        with patch.object(main, "fetch_live_channel_objects", side_effect=fetch):
            return (await main.get_live_properties(mode))[0]

    async def test_all_includes_four_week_boundary_from_both_channels_but_only_available_posts(self):
        room, house = prop(1, days=28), prop(2, days=25, channel="dublin_rent")
        house["property_type"] = "Будинок"
        closed, no_contact, expired = prop(3), prop(4, contacts=False), prop(5, days=29)
        closed["raw_text"] += "\n🔴 БІЛЬШЕ НЕ ДОСТУПНО 🔴"
        self.record([room])
        rows = await self.select("all", [room, house, closed, no_contact, expired])
        self.assertEqual({p["post_url"] for p in rows}, {room["post_url"], house["post_url"]})

    async def test_after_all_only_posts_published_later_and_not_previously_used_are_fresh(self):
        old, omitted = prop(1), prop(2)
        self.record([old])
        Clock.current = NOW + timedelta(hours=2)
        new = prop(3, date=NOW + timedelta(hours=1))
        repost = prop(4, date=NOW + timedelta(hours=1))
        repost["ref"] = old["ref"]
        rows = await self.select("new", [old, omitted, new, repost])
        self.assertEqual([p["post_url"] for p in rows], [new["post_url"]])
        self.assertEqual(len(await self.select("all", [old, omitted, new])), 3)
        self.record([new], mode="new")
        Clock.current += timedelta(hours=2)
        newest = prop(5, date=NOW + timedelta(hours=3))
        self.assertEqual([p["post_url"] for p in await self.select("new", [new, newest])], [newest["post_url"]])

    async def test_creating_preview_does_not_move_fresh_cutoff(self):
        source = [prop(1), prop(2)]
        with patch.object(main, "get_verified_properties", new=AsyncMock(return_value=(source, 0, []))):
            await main.create_collection_preview(update(), "all")
        self.assertIsNone(main.get_last_successful_publication_utc())
        self.assertEqual(len(await self.select("new", source)), 2)

    def test_contacts_require_actual_details_not_channel_footers_prices_or_refs(self):
        for text in ("Контакти: @team_housely", "Пишіть @other_manager", "+353 89 448 4275", "087 123 4567", "hello@example.ie", "https://wa.me/353894484275"):
            with self.subTest(text=text):
                self.assertTrue(main.has_listing_contacts(text))
        for text in ("Ref 00001000", "Вартість: €1000", "Контакти:", "https://t.me/irelandrent", "@dublin_rent", "https://t.me/main_test"):
            with self.subTest(text=text):
                self.assertFalse(main.has_listing_contacts(text))
        self.assertTrue(main.has_listing_contacts("Написати менеджеру", '<a href="https://t.me/team_housely">Написати менеджеру</a>'))
        self.assertTrue(main.has_listing_contacts("Контакти", '<a href="tg://user?id=123456">Контакти</a>'))

    def test_contact_removed_or_unavailable_marker_rejects_current_embed(self):
        no_contact = '<div class="tgme_widget_message_text">Кімната<br>€900<br>Ref 00001000<br>https://t.me/irelandrent</div>'
        inactive = '<div class="tgme_widget_message_text">Контакти: @team_housely<br>🔴 БІЛЬШЕ НЕ ДОСТУПНО 🔴</div>'
        self.assertIs(main.classify_post_check_response(200, no_contact, require_text=True), False)
        self.assertIs(main.classify_post_check_response(200, inactive, require_text=True), False)

    async def test_no_listing_is_dropped_when_collection_exceeds_the_part_limit(self):
        source = [prop(n, channel="irelandrent" if n < 51 else "dublin_rent") for n in range(1, 74)]
        with patch.object(main, "MAX_ITEMS", 10):
            rows = await self.select("all", source)
            preview = main.new_collection_preview(rows, "all")
        self.assertEqual(len(rows), 73)
        parts = main.preview_parts(preview)
        self.assertGreater(len(parts), 1)
        self.assertEqual(sum(len(p["properties"]) for p in parts), 73)
        self.assertEqual({p["post_url"] for p in preview["properties"]}, {p["post_url"] for p in source})
        for p in parts:
            self.assertLessEqual(len(p["text"]), 4096)
            self.assertNotIn("не показано", p["text"])

    async def test_publish_all_parts_records_every_listing_and_undo_deletes_every_message(self):
        source = [prop(1), prop(2), prop(3)]
        with patch.object(main, "MAX_ITEMS", 1):
            preview = main.new_collection_preview(source, "all")
        main.PREVIEWS[123] = preview
        await main.publish_to(update(f"dest_main:{preview['token']}"), self.context, ["@main_test"])
        self.assertEqual(self.bot.send_message.await_count, 3)
        self.assertEqual(main.publication_history_stats(), {"publications": 1, "posts": 3})
        self.assertEqual(main.get_last_successful_publication_utc(), NOW)
        with main.db() as conn:
            publication_id = conn.execute("SELECT id FROM collection_publications").fetchone()[0]
        await main.undo_publication(update(f"undo_publish:{publication_id}"), self.context)
        self.assertEqual(self.bot.delete_message.await_count, 3)
        self.assertIsNone(main.get_last_successful_publication_utc())
        self.assertEqual(len(await self.select("new", source)), 3)

    async def test_failed_part_is_rolled_back_and_does_not_move_cutoff(self):
        source = [prop(1), prop(2), prop(3)]
        with patch.object(main, "MAX_ITEMS", 1):
            preview = main.new_collection_preview(source, "all")
        main.PREVIEWS[123] = preview
        self.bot.send_message.side_effect = [await self.sent_message(), RuntimeError("failed second part")]
        await main.publish_to(update(f"dest_main:{preview['token']}"), self.context, ["@main_test"])
        self.bot.delete_message.assert_awaited_once()
        self.assertIsNone(main.get_last_successful_publication_utc())
        self.assertEqual(main.publication_history_stats()["publications"], 0)
        self.assertEqual(len(await self.select("new", source)), 3)

    async def test_failed_rollback_tracks_used_part_without_moving_cutoff(self):
        source = [prop(1), prop(2), prop(3)]
        with patch.object(main, "MAX_ITEMS", 1):
            preview = main.new_collection_preview(source, "all")
        main.PREVIEWS[123] = preview
        self.bot.send_message.side_effect = [await self.sent_message(), RuntimeError("failed second part")]
        self.bot.delete_message.side_effect = RuntimeError("cannot remove sent part")
        await main.publish_to(update(f"dest_main:{preview['token']}"), self.context, ["@main_test"])
        self.assertIsNone(main.get_last_successful_publication_utc())
        self.assertEqual(main.publication_history_stats()["posts"], 1)
        self.assertEqual({p["post_url"] for p in await self.select("new", source)}, {p["post_url"] for p in source[1:]})

    def test_recovered_multipart_publication_moves_cutoff_only_when_last_part_exists(self):
        with patch.object(main, "MAX_ITEMS", 1):
            parts = main.build_collection_parts([prop(1), prop(2)], "all")
        for index, part in enumerate(parts):
            Clock.current = NOW + timedelta(seconds=index)
            main.save_published_collection("main_test", 600 + index, main.visible_html_text(part["text"]), part["text"], Clock.current)
            self.assertEqual(main.get_last_successful_publication_utc(), None if index == 0 else Clock.current)

    async def test_editing_one_part_keeps_other_parts_and_does_not_reuse_removed_links(self):
        with patch.object(main, "MAX_ITEMS", 1):
            preview = main.new_collection_preview([prop(1), prop(2), prop(3)], "all")
        main.PREVIEWS[123] = preview
        await main.edit_preview(update(f"edit_preview:{preview['token']}:1"), self.context)
        message = SimpleNamespace(text="Оновлена частина без об'єкта", text_html="Оновлена частина без об'єкта", reply_text=AsyncMock())
        await main.receive_edit(SimpleNamespace(effective_user=SimpleNamespace(id=123), effective_message=message), self.context)
        self.assertEqual({p["post_url"] for p in preview["properties"]}, {prop(1)["post_url"], prop(3)["post_url"]})
        self.assertEqual(len(preview["parts"]), 3)

    async def test_fresh_preview_becomes_stale_after_another_collection_even_if_its_post_was_omitted(self):
        preview = main.new_collection_preview([prop(1)], "new")
        main.PREVIEWS[123] = preview
        self.record([prop(2)])
        await main.publish_to(update(f"dest_main:{preview['token']}"), self.context, ["@main_test"])
        self.bot.send_message.assert_not_awaited()

    async def test_hidden_contact_links_survive_cache_storage_and_restart(self):
        p = prop(1, contacts=False)
        main.upsert_property_post("irelandrent", -1001, 1, p["raw_text"], NOW - timedelta(days=1),
                                  text_html='<a href="https://t.me/team_housely">Написати менеджеру</a>')
        main.init_db()
        cached = main.get_cached_channel_objects("irelandrent")
        self.assertTrue(main.is_available_property(cached[0]))

    async def test_rate_limit_retry_does_not_duplicate_history_or_skip_part(self):
        with patch.object(main, "MAX_ITEMS", 1):
            preview = main.new_collection_preview([prop(1), prop(2)], "all")
        main.PREVIEWS[123] = preview
        with patch.dict(os.environ, {"PTB_TIMEDELTA": "1"}), patch.object(main.asyncio, "sleep", new=AsyncMock()):
            self.bot.send_message.side_effect = [main.RetryAfter(1), await self.sent_message(), await self.sent_message()]
            await main.publish_to(update(f"dest_main:{preview['token']}"), self.context, ["@main_test"])
        self.assertEqual(self.bot.send_message.await_count, 3)
        self.assertEqual(main.publication_history_stats(), {"publications": 1, "posts": 2})

    async def test_one_complete_destination_moves_cutoff_even_if_another_has_an_orphan(self):
        with patch.object(main, "MAX_ITEMS", 1):
            preview = main.new_collection_preview([prop(1), prop(2)], "all")
        main.PREVIEWS[123] = preview
        self.bot.send_message.side_effect = [
            SimpleNamespace(chat=SimpleNamespace(id=-1009), message_id=500),
            SimpleNamespace(chat=SimpleNamespace(id=-1009), message_id=501),
            SimpleNamespace(chat=SimpleNamespace(id=-1001), message_id=600), RuntimeError("part failed"),
        ]
        self.bot.delete_message.side_effect = RuntimeError("cleanup failed")
        await main.publish_to(update(f"dest_main:{preview['token']}"), self.context, ["@main_test", "@dublin_rent"])
        self.assertEqual(main.get_last_successful_publication_utc(), NOW)
        self.assertEqual(main.publication_history_stats(), {"publications": 1, "posts": 2})
        with main.db() as conn:
            publication_id = conn.execute("SELECT id FROM collection_publications WHERE deleted_at_utc IS NULL").fetchone()[0]
        self.assertEqual(len(main.get_active_publication_messages(publication_id)), 3)

    def test_upgrade_preserves_recovered_history_from_old_schema(self):
        old_path = Path(self.temp.name) / "old-schema.db"
        with main.sqlite3.connect(old_path) as conn:
            conn.execute("""CREATE TABLE published_collection_posts (
                channel_username TEXT NOT NULL, collection_message_id INTEGER NOT NULL, post_url TEXT NOT NULL,
                ref TEXT, created_at_utc TEXT NOT NULL, imported_at_utc TEXT NOT NULL, deleted_at_utc TEXT,
                PRIMARY KEY(channel_username, collection_message_id, post_url))""")
            conn.execute("INSERT INTO published_collection_posts VALUES (?, ?, ?, ?, ?, ?, NULL)",
                         ("main_test", 500, prop(1)["post_url"], prop(1)["ref"], NOW.isoformat(), NOW.isoformat()))
        with patch.object(main, "DB_PATH", old_path):
            main.init_db()
            self.assertEqual(main.recovered_history_stats(), {"publications": 1, "posts": 1})
            self.assertEqual(main.get_last_successful_publication_utc(), NOW)
