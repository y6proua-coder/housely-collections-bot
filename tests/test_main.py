import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="housely-collections-tests-")
os.environ["VERIFY_SOURCE_POSTS"] = "true"

import main


def property_item(
    index,
    description=None,
    price=None,
    location="Dublin 1",
    audience=None,
    property_type=None,
):
    return {
        "id": index,
        "channel_id": -1001,
        "message_id": 100 + index,
        "ref": str(index).zfill(7),
        "location": location,
        "price": price or f"€{900 + index:,}",
        "description": description or f"Кімната номер {index}",
        "property_type": property_type,
        "audience": audience,
        "post_url": f"https://t.me/dublin_rent/{100 + index}",
        "created_at_utc": f"2026-09-01T10:{index:02d}:00+00:00",
    }


class TestPostCheckClassification(unittest.TestCase):
    def test_404_is_missing(self):
        self.assertIs(main.classify_post_check_response(404, ""), False)

    def test_410_is_missing(self):
        self.assertIs(main.classify_post_check_response(410, ""), False)

    def test_500_is_unknown(self):
        self.assertIsNone(main.classify_post_check_response(500, "post not found"))

    def test_403_is_unknown(self):
        self.assertIsNone(main.classify_post_check_response(403, ""))

    def test_widget_error_is_missing(self):
        body = '<div class="tgme_widget_message_error">Post not found</div>'
        self.assertIs(main.classify_post_check_response(200, body), False)

    def test_post_not_found_is_missing(self):
        self.assertIs(main.classify_post_check_response(200, "Post not found"), False)

    def test_message_not_found_is_missing(self):
        self.assertIs(main.classify_post_check_response(200, "Message not found"), False)

    def test_api_style_missing_marker_is_missing(self):
        self.assertIs(main.classify_post_check_response(200, "MESSAGE_NOT_FOUND"), False)

    def test_bubble_is_existing(self):
        self.assertIs(
            main.classify_post_check_response(200, "tgme_widget_message_bubble"),
            True,
        )

    def test_data_post_is_existing(self):
        self.assertIs(main.classify_post_check_response(200, 'data-post="x/1"'), True)

    def test_text_widget_is_existing(self):
        self.assertIs(
            main.classify_post_check_response(200, "tgme_widget_message_text"),
            True,
        )

    def test_generic_page_is_unknown(self):
        self.assertIsNone(main.classify_post_check_response(200, "<html>Telegram</html>"))


class TestParsing(unittest.TestCase):
    def test_extract_ref_zero_pads(self):
        self.assertEqual(main.extract_ref("Ref 858"), "0000858")

    def test_extract_ref_keeps_seven_digits(self):
        self.assertEqual(main.extract_ref("Ref: 0001234"), "0001234")

    def test_extract_ref_missing(self):
        self.assertIsNone(main.extract_ref("No reference"))

    def test_extract_ukrainian_location(self):
        self.assertEqual(main.extract_location("📍 Локація: Dublin 15"), "Dublin 15")

    def test_extract_fallback_dublin_location(self):
        self.assertEqual(main.extract_location("Room available in Dublin 8"), "Dublin 8")

    def test_price_euro_prefix(self):
        self.assertEqual(main.extract_price("Оренда: €1100/міс"), "€1,100")

    def test_price_euro_suffix(self):
        self.assertEqual(main.extract_price("Оренда: 1250€"), "€1,250")

    def test_price_uses_cost_field_instead_of_earlier_arp_amount(self):
        text = "АРП: €600\n💶 Вартість: €1,450/місяць"
        self.assertEqual(main.extract_price(text), "€1,450")

    def test_parse_requires_ref(self):
        self.assertIsNone(main.parse_property("Кімната в Dublin 1 — €900"))

    def test_marketing_title_becomes_only_room_type(self):
        parsed = main.parse_property(
            "🏠 Кімната з класним власним санвузлом\n"
            "📍 Локація: Dublin 15\n"
            "👤 Для кого: пари\n"
            "💶 Оренда: €1200\n"
            "Ref 858"
        )
        self.assertEqual(parsed["property_type"], "Кімната")
        self.assertEqual(parsed["audience"], "пари")

    def test_one_bedroom_apartment_is_not_a_room(self):
        parsed = main.parse_property(
            "🏡 Здається 1-кімнатна квартира в Dublin 14\n"
            "📍 Локація: Dublin 14\n"
            "💶 Вартість: 1400€/міс\n"
            "👤 ЛИШЕ для 1 особи з роботою !\n"
            "📝 Для запису на перегляд пишіть @team_housely\n"
            "Ref 2021"
        )
        self.assertEqual(parsed["property_type"], "Квартира")
        self.assertEqual(parsed["audience"], "1 особи з роботою")
        self.assertEqual(parsed["location"], "Dublin 14")

    def test_contact_cta_is_never_used_as_audience(self):
        parsed = main.parse_property(
            "Здається квартира в Dublin 14\n"
            "Вартість: €1,400\n"
            "Для запису на перегляд пишіть @team_housely\n"
            "Ref 2022"
        )
        self.assertIsNone(parsed["audience"])

    def test_family_line_is_used_as_audience(self):
        parsed = main.parse_property(
            "🏡 Здається 1-кімнатна квартира в Dublin 18\n"
            "📍 Локація: Dublin 18\n"
            "💶 Вартість: €2200/міс\n"
            "👤 Для сім’ї з роботою/студенти\n"
            "Ref 2023"
        )
        self.assertEqual(parsed["property_type"], "Квартира")
        self.assertEqual(parsed["audience"], "сім’ї з роботою/студенти")

    def test_plural_rooms_beat_later_house_context(self):
        parsed = main.parse_property(
            "🏡 Здається 9 кімнат в Dublin 24 для одного\n"
            "📍 Локація: Dublin 24\n"
            "💶 Вартість: 850€/міс з людини\n"
            "🏠 В будинку без власників, всього 12 кімнат та 5 санвузлів\n"
            "📝 Для запису на перегляд пишіть @team_housely\n"
            "Ref 2020"
        )
        self.assertEqual(parsed["property_type"], "Кімната")
        self.assertEqual(parsed["audience"], "одного")

    def test_apartment_is_not_misread_from_bedroom(self):
        self.assertEqual(
            main.extract_property_type("2-bedroom apartment in Dublin 2"),
            "Квартира",
        )

    def test_display_title_has_type_then_audience(self):
        item = property_item(
            1,
            "Кімната з власним санвузлом",
            audience="Для кого: однієї людини з роботою",
        )
        self.assertEqual(
            main.property_display_title(item),
            "Кімната для однієї людини з роботою",
        )


class TestLinkRestoration(unittest.TestCase):
    def setUp(self):
        self.items = [
            property_item(1, "Кімната для однієї особи", "€1,100", "Dublin 1"),
            property_item(2, "Кімната з власним санвузлом", "€1,200", "Dublin 11"),
            property_item(3, "2-кімнатний будинок", "€2,950", "Dublin 3"),
        ]

    def test_existing_anchor_is_preserved(self):
        original = main.build_collection(self.items)
        restored = main.restore_missing_detail_links(original, self.items)
        self.assertEqual(restored, original)

    def test_plain_details_becomes_clickable(self):
        line = "• 🏠 Кімната для однієї особи — €1,100 → Детальніше"
        restored = main.restore_missing_detail_links(line, self.items)
        self.assertIn(self.items[0]["post_url"], restored)

    def test_bold_plain_details_becomes_clickable(self):
        line = "• 🏠 Кімната для однієї особи — <b>€1,100</b> → <b>Детальніше</b>"
        restored = main.restore_missing_detail_links(line, self.items)
        self.assertIn('<a href="https://t.me/dublin_rent/101">', restored)

    def test_deleted_middle_item_does_not_shift_links(self):
        edited = "\n".join([
            "📍 Dublin 1",
            "• 🏠 Кімната для однієї особи — €1,100 → Детальніше",
            "📍 Dublin 3",
            "• 🏡 2-кімнатний будинок — €2,950 → Детальніше",
        ])
        restored = main.restore_missing_detail_links(edited, self.items)
        self.assertIn(self.items[0]["post_url"], restored)
        self.assertIn(self.items[2]["post_url"], restored)
        self.assertNotIn(self.items[1]["post_url"], restored)

    def test_unmatched_rewrite_does_not_get_random_link(self):
        edited = "Зовсім інший текст → Детальніше"
        restored = main.restore_missing_detail_links(edited, self.items)
        self.assertNotIn("https://t.me/", restored)

    def test_line_without_details_is_unchanged(self):
        edited = "🏡 Актуальне житло на сьогодні"
        self.assertEqual(main.restore_missing_detail_links(edited, self.items), edited)

    def test_duplicate_descriptions_use_distinct_urls(self):
        duplicates = [
            property_item(4, "Кімната для пари", "€1,000"),
            property_item(5, "Кімната для пари", "€1,000"),
        ]
        line = "• 🏠 Кімната для пари — €1,000 → Детальніше"
        restored = main.restore_missing_detail_links(f"{line}\n{line}", duplicates)
        self.assertIn(duplicates[0]["post_url"], restored)
        self.assertIn(duplicates[1]["post_url"], restored)

    def test_html_ampersand_in_url_is_escaped(self):
        item = property_item(6)
        item["post_url"] = "https://t.me/x/6?a=1&b=2"
        line = "• 🏠 Кімната номер 6 — €906 → Детальніше"
        restored = main.restore_missing_detail_links(line, [item])
        self.assertIn("a=1&amp;b=2", restored)

    def test_user_bold_formatting_is_kept(self):
        line = "• 🏠 <b>Кімната для однієї особи</b> — €1,100 → Детальніше"
        restored = main.restore_missing_detail_links(line, self.items)
        self.assertIn("<b>Кімната для однієї особи</b>", restored)

    def test_visible_html_text_decodes_entities(self):
        self.assertEqual(main.visible_html_text("A &amp; B <b>bold</b>"), "A & B bold")


class TestCollectionBuilder(unittest.TestCase):
    def test_collection_contains_clickable_anchor(self):
        text = main.build_collection([property_item(1)])
        self.assertIn('<a href="https://t.me/dublin_rent/101">', text)

    def test_collection_groups_location(self):
        text = main.build_collection([property_item(1, location="Dublin 15")])
        self.assertIn("📍 <b>Dublin 15</b>", text)

    def test_collection_escapes_description(self):
        text = main.build_collection([property_item(1, "Room & ensuite <new>")])
        self.assertNotIn("ensuite", text)
        self.assertIn("Кімната", text)

    def test_collection_uses_compact_type_and_audience(self):
        item = property_item(
            1,
            "Кімната з класним санвузлом",
            audience="пари",
        )
        text = main.build_collection([item])
        self.assertIn("Кімната для пари — <b>€901</b>", text)
        self.assertNotIn("класним санвузлом", text)

    def test_collection_groups_types_with_rooms_before_houses(self):
        house = property_item(
            1,
            "Будинок",
            location="Dublin 1",
            property_type="Будинок",
        )
        room = property_item(
            2,
            "Кімната",
            location="Dublin 24",
            property_type="Кімната",
        )
        text = main.build_collection([house, room])
        self.assertLess(text.index("<b>Кімнати</b>"), text.index("<b>Будинки</b>"))
        self.assertLess(text.index("Dublin 24"), text.index("Dublin 1"))

    def test_new_collection_has_distinct_title(self):
        text = main.build_collection([property_item(1)], mode="new")
        self.assertIn("Нові актуальні пропозиції", text)

    def test_collection_shows_hidden_count(self):
        text = main.build_collection([property_item(1)], hidden_count=4)
        self.assertIn("Ще 4 пропозицій", text)

    def test_properties_rendered_excludes_trimmed(self):
        items = [property_item(1), property_item(2)]
        text = main.build_collection([items[0]])
        rendered = main.properties_rendered_in(text, items)
        self.assertEqual([item["id"] for item in rendered], [1])

    def test_collection_contains_mandatory_footer_text(self):
        text = main.build_collection([property_item(1)])
        self.assertIn("Переглядай актуальні пропозиції", text)
        self.assertIn("телеграм каналі:", text)

    def test_footer_channel_url_is_clickable(self):
        text = main.build_collection([property_item(1)])
        self.assertIn(
            '<a href="https://t.me/arpireland1"><b>https://t.me/arpireland1</b></a>',
            text,
        )

    def test_footer_is_at_the_very_end(self):
        text = main.build_collection([property_item(1)])
        self.assertTrue(text.endswith("⸻"))
        self.assertGreater(text.rfind("https://t.me/arpireland1"), text.rfind("Детальніше"))

    def test_manual_edit_restores_removed_footer(self):
        edited = "🏡 Підбірка\n\n• 🏠 Об'єкт — €900 → Детальніше"
        restored = main.ensure_collection_footer(edited)
        self.assertTrue(restored.endswith("⸻"))
        self.assertIn("https://t.me/arpireland1", restored)

    def test_existing_footer_is_not_duplicated(self):
        text = main.build_collection([property_item(1)])
        restored = main.ensure_collection_footer(text)
        self.assertEqual(restored.count("Переглядай актуальні пропозиції"), 1)


class TestDatabaseCleanup(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = main.DB_PATH
        main.DB_PATH = Path(self.temp_dir.name) / "collections.db"
        main.init_db()

    def tearDown(self):
        main.DB_PATH = self.old_db_path
        self.temp_dir.cleanup()

    def insert_item(self, item):
        now = datetime.now(timezone.utc).isoformat()
        local_date = datetime.now(main.TZ).date().isoformat()
        with main.db() as conn:
            conn.execute(
                """
                INSERT INTO property_posts (
                    channel_username, channel_id, message_id, ref, location,
                    price, description, audience, post_url, raw_text,
                    local_date, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "dublin_rent", item["channel_id"], item["message_id"],
                    item["ref"], item["location"], item["price"],
                    item["description"], item.get("audience"), item["post_url"],
                    item.get("description", "raw"),
                    local_date, now, now,
                ),
            )

    def test_delete_property_posts_removes_exact_row(self):
        first, second = property_item(1), property_item(2)
        self.insert_item(first)
        self.insert_item(second)
        rows, _ = main.get_today_properties(apply_limit=False)
        main.delete_property_posts([rows[0]])
        remaining, _ = main.get_today_properties(apply_limit=False)
        self.assertEqual(len(remaining), 1)

    def test_duplicate_ref_keeps_latest_post(self):
        first, second = property_item(1), property_item(2)
        second["ref"] = first["ref"]
        self.insert_item(first)
        self.insert_item(second)
        rows, _ = main.get_today_properties(apply_limit=False)
        self.assertEqual(len(rows), 1)

    def test_manual_channel_post_with_new_ref_is_saved_directly(self):
        now = datetime.now(timezone.utc)
        message = SimpleNamespace(
            chat=SimpleNamespace(username="irelandrent", id=-100777),
            text=(
                "Здається кімната в Dublin 24\n"
                "Для однієї особи з роботою\n"
                "Вартість: €850\n"
                "Ref 2999"
            ),
            caption=None,
            message_id=2020,
            date=now,
        )
        update = SimpleNamespace(channel_post=message, edited_channel_post=None)

        self.assertTrue(main.save_channel_post(update))
        rows, _ = main.get_today_properties(apply_limit=False)

        self.assertEqual([row["ref"] for row in rows], ["0002999"])
        self.assertEqual(rows[0]["property_type"], "Кімната")
        self.assertEqual(rows[0]["audience"], "однієї особи з роботою")
        self.assertEqual(rows[0]["post_url"], "https://t.me/irelandrent/2020")

    def test_published_items_are_excluded_from_new_collection(self):
        item = property_item(1)
        self.insert_item(item)
        rows, _ = main.get_uncollected_properties(apply_limit=False)
        publication_id = main.record_publication(
            123,
            "new",
            "text",
            rows,
            [{"destination": "@main", "chat_id": -1009, "message_id": 55}],
        )
        self.assertIsInstance(publication_id, int)
        remaining, _ = main.get_uncollected_properties(apply_limit=False)
        self.assertEqual(remaining, [])

    def test_undo_makes_items_new_again(self):
        item = property_item(1)
        self.insert_item(item)
        rows, _ = main.get_uncollected_properties(apply_limit=False)
        publication_id = main.record_publication(
            123,
            "today",
            "text",
            rows,
            [{"destination": "@main", "chat_id": -1009, "message_id": 56}],
        )
        message = main.get_active_publication_messages(publication_id)[0]
        main.mark_publication_message_deleted(message["id"])
        self.assertTrue(main.finish_publication_undo(publication_id))
        remaining, _ = main.get_uncollected_properties(apply_limit=False)
        self.assertEqual([row["ref"] for row in remaining], [item["ref"]])


class TestDatabaseMigration(unittest.TestCase):
    def test_existing_database_is_migrated_without_losing_posts(self):
        temp_dir = tempfile.TemporaryDirectory()
        old_db_path = main.DB_PATH
        main.DB_PATH = Path(temp_dir.name) / "collections.db"
        try:
            conn = sqlite3.connect(main.DB_PATH)
            conn.execute(
                """
                CREATE TABLE property_posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_username TEXT NOT NULL,
                    channel_id INTEGER,
                    message_id INTEGER NOT NULL,
                    ref TEXT,
                    location TEXT,
                    price TEXT,
                    description TEXT,
                    audience TEXT,
                    post_url TEXT NOT NULL,
                    raw_text TEXT NOT NULL,
                    local_date TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    UNIQUE(channel_id, message_id)
                )
                """
            )
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                INSERT INTO property_posts (
                    channel_username, channel_id, message_id, ref, location,
                    price, description, audience, post_url, raw_text,
                    local_date, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "irelandrent", -1001, 5, "0000005", "Dublin 5", "€1,000",
                    "Кімната з балконом", "пари", "https://t.me/irelandrent/5",
                    "Кімната з балконом\nRef 5", "2026-09-03", now, now,
                ),
            )
            conn.commit()
            conn.close()

            main.init_db()
            with main.db() as migrated:
                row = migrated.execute(
                    "SELECT property_type FROM property_posts WHERE ref = '0000005'"
                ).fetchone()
                publication_table = migrated.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'table' AND name = 'collection_publications'
                    """
                ).fetchone()
            self.assertEqual(row["property_type"], "Кімната")
            self.assertIsNotNone(publication_table)
        finally:
            main.DB_PATH = old_db_path
            temp_dir.cleanup()


class TestFormattingRestoration(unittest.TestCase):
    def test_plain_copied_collection_restores_links_and_all_standard_bold(self):
        item = property_item(1, audience="пари")
        plain = "\n".join([
            "🏡 Актуальне житло на сьогодні",
            "🏠 Кімнати",
            "📍 Dublin 1",
            "• 🏠 Кімната для пари — €901 → Детальніше",
            "⸻",
            "Переглядай актуальні пропозиції житла в нашому офіційному телеграм каналі:",
            "https://t.me/arpireland1",
            "⸻",
        ])
        restored = main.restore_missing_detail_links(plain, [item])
        restored = main.restore_standard_collection_formatting(restored, [item])
        restored = main.ensure_collection_footer(restored)
        self.assertIn("🏡 <b>Актуальне житло на сьогодні</b>", restored)
        self.assertIn("🏠 <b>Кімнати</b>", restored)
        self.assertIn("📍 <b>Dublin 1</b>", restored)
        self.assertIn("<b>€901</b>", restored)
        self.assertIn(
            '<a href="https://t.me/dublin_rent/101"><b>Детальніше</b></a>',
            restored,
        )
        self.assertEqual(restored.count("Переглядай актуальні пропозиції"), 1)

    def test_existing_manual_bold_is_preserved(self):
        item = property_item(1)
        line = (
            "• 🏠 <b>Кімната</b> — <b>€901</b> → "
            '<a href="https://t.me/dublin_rent/101"><b>Детальніше</b></a>'
        )
        restored = main.restore_standard_collection_formatting(line, [item])
        self.assertEqual(restored, line)


class TestUndoPublicationHandler(unittest.IsolatedAsyncioTestCase):
    async def test_undo_deletes_every_published_message(self):
        temp_dir = tempfile.TemporaryDirectory()
        old_db_path = main.DB_PATH
        main.DB_PATH = Path(temp_dir.name) / "collections.db"
        main.init_db()
        try:
            publication_id = main.record_publication(
                123,
                "new",
                "text",
                [property_item(1)],
                [
                    {"destination": "@one", "chat_id": -1001, "message_id": 10},
                    {"destination": "@two", "chat_id": -1002, "message_id": 20},
                ],
            )
            query = SimpleNamespace(
                data=f"undo_publish:{publication_id}",
                answer=AsyncMock(),
                edit_message_text=AsyncMock(),
                message=SimpleNamespace(reply_text=AsyncMock()),
            )
            update = SimpleNamespace(
                callback_query=query,
                effective_user=SimpleNamespace(id=123),
            )
            context = SimpleNamespace(
                bot=SimpleNamespace(delete_message=AsyncMock()),
            )
            old_admin_ids = main.ADMIN_IDS
            main.ADMIN_IDS = {123}
            try:
                await main.undo_publication(update, context)
            finally:
                main.ADMIN_IDS = old_admin_ids

            self.assertEqual(context.bot.delete_message.await_count, 2)
            self.assertEqual(main.get_active_publication_messages(publication_id), [])
            query.edit_message_text.assert_awaited_once()
        finally:
            main.DB_PATH = old_db_path
            temp_dir.cleanup()


class TestAsyncSourceChecks(unittest.IsolatedAsyncioTestCase):
    async def test_check_source_post_uses_embed_url(self):
        response = type("Response", (), {
            "status_code": 200,
            "text": 'data-post="dublin_rent/101"',
        })()
        client = type("Client", (), {"get": AsyncMock(return_value=response)})()
        state = await main.check_source_post(
            property_item(1), client, main.asyncio.Semaphore(1)
        )
        self.assertIs(state, True)
        requested_url = client.get.await_args.args[0]
        self.assertTrue(requested_url.endswith("?embed=1&mode=tme"))

    async def test_network_error_is_unknown(self):
        client = type("Client", (), {
            "get": AsyncMock(side_effect=main.httpx.ConnectError("offline"))
        })()
        state = await main.check_source_post(
            property_item(1), client, main.asyncio.Semaphore(1)
        )
        self.assertIsNone(state)

    async def test_remove_missing_keeps_unknown_and_deletes_only_missing(self):
        items = [property_item(1), property_item(2), property_item(3)]

        async def fake_check(item, client, semaphore):
            return {1: True, 2: False, 3: None}[item["id"]]

        fake_client = AsyncMock()
        fake_client.__aenter__.return_value = fake_client
        fake_client.__aexit__.return_value = None

        with patch.object(main.httpx, "AsyncClient", return_value=fake_client), \
             patch.object(main, "check_source_post", side_effect=fake_check), \
             patch.object(main, "delete_property_posts") as delete_mock:
            remaining, missing = await main.remove_missing_source_posts(items)

        self.assertEqual([item["id"] for item in remaining], [1, 3])
        self.assertEqual([item["id"] for item in missing], [2])
        delete_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
