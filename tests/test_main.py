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

    def test_russian_nine_rooms_from_screenshot_is_a_room_post(self):
        parsed = main.parse_property(
            "🏡 Сдается 9 комнат в Dublin 24 для одного\n"
            "📍 Локація: Dublin 24\n"
            "💶 Вартість: 850€/міс з людини\n"
            "⭐ Наразі доступно 9 single кімнат\n"
            "🏠 В будинку без власників, всього 12 кімнат та 5 санвузлів\n"
            "📝 Для запису на перегляд пишіть @team_housely\n"
            "Ref 936"
        )
        self.assertEqual(parsed["property_type"], "Кімната")
        self.assertEqual(parsed["location"], "Dublin 24")

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

    def test_unknown_type_is_included_without_invented_type_heading(self):
        item = property_item(
            1,
            description="Нова пропозиція",
            location="Dublin 24",
            property_type="Житло",
        )
        text = main.build_collection([item], mode="new")

        self.assertIn("📍 <b>Dublin 24</b>", text)
        self.assertIn("• 🏠 <b>€901</b>", text)
        self.assertIn(item["post_url"], text)
        self.assertNotIn("Інше житло", text)

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

    def test_duplicate_ref_does_not_hide_a_distinct_post(self):
        first, second = property_item(1), property_item(2)
        second["ref"] = first["ref"]
        self.insert_item(first)
        self.insert_item(second)
        rows, _ = main.get_today_properties(apply_limit=False)
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["post_url"] for row in rows}, {
            first["post_url"], second["post_url"],
        })

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

    def test_legacy_deploy_timestamp_does_not_hide_unpublished_refs(self):
        item = property_item(1)
        self.insert_item(item)
        with main.db() as conn:
            conn.execute(
                """
                INSERT INTO bot_settings (key, value)
                VALUES ('new_collections_tracking_started_at_utc', ?)
                """,
                ("2099-01-01T00:00:00+00:00",),
            )

        rows, _ = main.get_uncollected_properties(apply_limit=False)

        self.assertEqual([row["ref"] for row in rows], [item["ref"]])

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

    def test_five_refs_after_last_publication_are_all_new(self):
        previously_published = property_item(1)
        self.insert_item(previously_published)
        rows, _ = main.get_uncollected_properties(apply_limit=False)
        main.record_publication(
            123,
            "new",
            "yesterday collection",
            rows,
            [{"destination": "@main", "chat_id": -1009, "message_id": 55}],
        )

        new_items = [property_item(index) for index in range(2, 7)]
        for item in new_items:
            self.insert_item(item)

        remaining, _ = main.get_uncollected_properties(apply_limit=False)
        self.assertEqual(
            {row["ref"] for row in remaining},
            {item["ref"] for item in new_items},
        )

    def test_two_new_posts_with_same_ref_are_both_included(self):
        baseline = property_item(1)
        self.insert_item(baseline)
        main.record_publication(
            123,
            "new",
            "last collection",
            [baseline],
            [{"destination": "@main", "chat_id": -1009, "message_id": 55}],
        )

        first, second = property_item(2), property_item(3)
        second["ref"] = first["ref"]
        self.insert_item(first)
        self.insert_item(second)

        remaining, _ = main.get_uncollected_properties(apply_limit=False)
        self.assertEqual(
            {row["post_url"] for row in remaining},
            {first["post_url"], second["post_url"]},
        )

    def test_post_before_last_publication_is_not_new_even_if_omitted(self):
        omitted = property_item(1)
        published = property_item(2)
        self.insert_item(omitted)
        self.insert_item(published)
        main.record_publication(
            123,
            "new",
            "last collection",
            [published],
            [{"destination": "@main", "chat_id": -1009, "message_id": 55}],
        )

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


class TestPublicChannelRecovery(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = main.DB_PATH
        self.old_sync_enabled = main.SOURCE_SYNC_ENABLED
        main.DB_PATH = Path(self.temp_dir.name) / "collections.db"
        main.SOURCE_SYNC_ENABLED = True
        main.init_db()

    def tearDown(self):
        main.DB_PATH = self.old_db_path
        main.SOURCE_SYNC_ENABLED = self.old_sync_enabled
        self.temp_dir.cleanup()

    @staticmethod
    def channel_page_html():
        return """
        <div class="tgme_widget_message" data-post="irelandrent/2020">
          <div class="tgme_widget_message_text">
            Здається кімната в Dublin 24<br>
            Для однієї особи з роботою<br>
            Вартість: €850<br>
            Ref 0000937
          </div>
          <time datetime="2026-09-03T18:25:00+00:00"></time>
        </div>
        """

    def test_public_channel_html_yields_ref_post(self):
        posts = main.parse_public_channel_page(
            "irelandrent",
            self.channel_page_html(),
        )

        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["message_id"], 2020)
        self.assertIn("Ref 0000937", posts[0]["text"])

    async def test_source_sync_recovers_post_after_empty_redeploy_database(self):
        response = SimpleNamespace(
            text=self.channel_page_html(),
            raise_for_status=lambda: None,
        )
        fake_client = AsyncMock()
        fake_client.get.return_value = response
        fake_client.__aenter__.return_value = fake_client
        fake_client.__aexit__.return_value = None

        with patch.object(main.httpx, "AsyncClient", return_value=fake_client):
            saved = await main.sync_recent_source_posts()

        rows, _ = main.get_uncollected_properties(apply_limit=False)
        self.assertEqual(saved, 1)
        self.assertEqual([row["ref"] for row in rows], ["0000937"])


class TestLiveChannelSource(unittest.TestCase):
    def test_live_parser_accepts_manual_post_without_ref(self):
        html = """
        <div class="tgme_widget_message" data-post="dublin_rent/3001">
          <div class="tgme_widget_message_text">
            🛏 Ліжко-місце для одного чоловіка<br>
            📍 Локація: Dublin 3<br>
            💶 Вартість: €620
          </div>
          <time datetime="2026-09-14T12:00:00+00:00"></time>
        </div>
        """
        rows = main.parse_live_channel_page("dublin_rent", html)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["real_ref"])
        self.assertEqual(rows[0]["ref"], "post:dublin_rent:3001")
        self.assertEqual(rows[0]["post_url"], "https://t.me/dublin_rent/3001")

    def test_live_dedup_uses_ref_and_keeps_newest_post(self):
        first = property_item(1)
        first["real_ref"] = "0000999"
        first["ref"] = "0000999"
        first["created_at_utc"] = "2026-09-14T10:00:00+00:00"
        second = property_item(2)
        second["real_ref"] = "0000999"
        second["ref"] = "0000999"
        second["created_at_utc"] = "2026-09-14T11:00:00+00:00"

        rows = main.deduplicate_live_objects([first, second])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["post_url"], second["post_url"])

    def test_collection_post_is_not_parsed_as_property(self):
        html = """
        <div class="tgme_widget_message" data-post="dublin_rent/3002">
          <div class="tgme_widget_message_text">
            🆕 Нові актуальні пропозиції<br>
            📍 Dublin 3<br>
            • 🛏 Ліжко-місце — €620 → Детальніше<br>
            • 🏠 Кімната — €900 → Детальніше
          </div>
          <time datetime="2026-09-14T13:00:00+00:00"></time>
        </div>
        """
        self.assertEqual(main.parse_live_channel_page("dublin_rent", html), [])


class TestPreviewPublicationSeparation(unittest.IsolatedAsyncioTestCase):
    async def test_preview_does_not_create_publication_record(self):
        temp_dir = tempfile.TemporaryDirectory()
        old_db_path = main.DB_PATH
        old_admin_ids = main.ADMIN_IDS
        main.DB_PATH = Path(temp_dir.name) / "collections.db"
        main.ADMIN_IDS = {123}
        main.init_db()
        try:
            query = SimpleNamespace(
                answer=AsyncMock(),
                message=SimpleNamespace(reply_text=AsyncMock()),
            )
            update = SimpleNamespace(
                callback_query=query,
                effective_user=SimpleNamespace(id=123),
            )
            with patch.object(
                main,
                "get_verified_properties",
                new=AsyncMock(return_value=([property_item(1)], 0, [])),
            ):
                await main.create_collection_preview(update, "new")

            with main.db() as conn:
                publication_count = conn.execute(
                    "SELECT COUNT(*) AS count FROM collection_publications"
                ).fetchone()["count"]
            self.assertEqual(publication_count, 0)
            self.assertIn(123, main.PREVIEWS)
        finally:
            main.PREVIEWS.pop(123, None)
            main.DB_PATH = old_db_path
            main.ADMIN_IDS = old_admin_ids
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


def test_live_property_with_standard_footer_is_not_rejected():
    text = """🏡 Здається кімната в Dublin 15
📍 Локація: Dublin 15
💶 Оренда: 1250€
👤 Для однієї особи з роботою/студент(ка)
Ref 0000920
⸻
Переглядай актуальні пропозиції житла в нашому офіційному телеграм каналі:
🔗 https://t.me/irelandrent
⸻"""
    parsed = main.parse_live_property(text, "irelandrent", 12345)
    assert parsed is not None
    assert parsed["real_ref"] == "0000920"


def test_generated_collection_is_still_rejected():
    text = """🆕 Нові актуальні пропозиції
🏠 Кімнати
📍 Dublin 15
• 🏠 Кімната — €1,250 → Детальніше
📍 Dublin 24
• 🏠 Кімната — €1,000 → Детальніше"""
    assert main.parse_live_property(text, "irelandrent", 99999) is None

class TestV072Reliability(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = main.DB_PATH
        self.old_sources = main.SOURCE_CHANNELS
        self.old_retries = main.LIVE_SOURCE_RETRIES
        self.old_retry_delay = main.LIVE_SOURCE_RETRY_DELAY
        self.old_sync_lookback = main.SOURCE_SYNC_LOOKBACK_HOURS
        main.DB_PATH = Path(self.temp_dir.name) / "collections.db"
        main.SOURCE_CHANNELS = {"irelandrent"}
        main.LIVE_SOURCE_RETRIES = 3
        main.LIVE_SOURCE_RETRY_DELAY = 0
        main.SOURCE_SYNC_LOOKBACK_HOURS = 72
        main.init_db()

    def tearDown(self):
        main.DB_PATH = self.old_db_path
        main.SOURCE_CHANNELS = self.old_sources
        main.LIVE_SOURCE_RETRIES = self.old_retries
        main.LIVE_SOURCE_RETRY_DELAY = self.old_retry_delay
        main.SOURCE_SYNC_LOOKBACK_HOURS = self.old_sync_lookback
        self.temp_dir.cleanup()

    def test_pin_only_location_is_parsed(self):
        text = "🏠 Кімната для пари\n📍 Lucan, Co. Dublin\n💶 €750"
        parsed = main.parse_live_property(text, "irelandrent", 1)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["location"], "Lucan, Co. Dublin")

    def test_bed_space_hyphen_is_recognized(self):
        self.assertEqual(
            main.extract_property_type("Bed-space available in Dublin 24"),
            "Ліжко-місце",
        )

    def test_ref_listing_with_new_type_word_is_not_lost(self):
        text = (
            "Нова житлова пропозиція\n"
            "📍 Локація: Dublin 3\n"
            "💶 Оренда: €1,500\n"
            "Ref 1234"
        )
        parsed = main.parse_live_property(text, "irelandrent", 77)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["real_ref"], "0001234")

    def test_inactive_listing_is_rejected_even_if_old_body_remains(self):
        text = (
            "Не доступно\n"
            "🏡 Здається кімната в Dublin 8\n"
            "📍 Локація: Dublin 8\n"
            "💶 Оренда: €1,100\n"
            "Ref 1234"
        )
        self.assertIsNone(main.parse_live_property(text, "irelandrent", 77))

    def test_edit_to_unavailable_removes_stale_cache_row(self):
        now = datetime.now(timezone.utc)
        live_text = (
            "🏡 Здається кімната в Dublin 8\n"
            "📍 Локація: Dublin 8\n"
            "💶 Оренда: €1,100\n"
            "Ref 1234"
        )
        self.assertTrue(main.upsert_property_post("irelandrent", -1001, 77, live_text, now))
        self.assertEqual(len(main.get_cached_channel_objects("irelandrent")), 1)

        unavailable = "Не доступно\n" + live_text
        self.assertFalse(main.upsert_property_post("irelandrent", -1001, 77, unavailable, now))
        self.assertEqual(main.get_cached_channel_objects("irelandrent"), [])

    async def test_first_public_page_empty_retries_then_raises(self):
        response = SimpleNamespace(text="<html>Telegram</html>", raise_for_status=lambda: None)
        client = AsyncMock()
        client.get.return_value = response

        with self.assertRaises(main.LiveChannelReadError):
            await main._get_public_history_page(
                client,
                "irelandrent",
                "https://t.me/s/irelandrent",
                first_page=True,
            )
        self.assertEqual(client.get.await_count, 3)

    async def test_first_public_page_recovers_on_second_try(self):
        empty = SimpleNamespace(text="<html>Telegram</html>", raise_for_status=lambda: None)
        valid = SimpleNamespace(
            text="""
            <div class=\"tgme_widget_message\" data-post=\"irelandrent/10\">
              <div class=\"tgme_widget_message_text\">🏠 Кімната<br>📍 Dublin 8<br>💶 €900</div>
              <time datetime=\"2026-09-14T12:00:00+00:00\"></time>
            </div>
            """,
            raise_for_status=lambda: None,
        )
        client = AsyncMock()
        client.get.side_effect = [empty, valid]
        response, messages = await main._get_public_history_page(
            client,
            "irelandrent",
            "https://t.me/s/irelandrent",
            first_page=True,
        )
        self.assertIs(response, valid)
        self.assertEqual(len(messages), 1)
        self.assertEqual(client.get.await_count, 2)

    async def test_direct_failure_with_empty_cache_is_error_not_false_zero(self):
        with patch.object(
            main,
            "fetch_live_channel_objects",
            new=AsyncMock(side_effect=main.LiveChannelReadError("blocked")),
        ):
            with self.assertRaises(main.LiveChannelReadError):
                await main.get_live_properties("today")

    async def test_direct_failure_uses_real_channel_cache(self):
        now = datetime.now(timezone.utc)
        text = (
            "🏡 Здається кімната в Dublin 8\n"
            "📍 Локація: Dublin 8\n"
            "💶 Оренда: €1,100\n"
            "Ref 1234"
        )
        main.upsert_property_post("irelandrent", -1001, 77, text, now)
        with patch.object(
            main,
            "fetch_live_channel_objects",
            new=AsyncMock(side_effect=main.LiveChannelReadError("blocked")),
        ):
            rows, hidden, missing = await main.get_live_properties("today")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["real_ref"], "0001234")
        self.assertEqual(hidden, 0)
        self.assertEqual(missing, [])

    async def test_startup_sync_caches_manual_post_without_ref(self):
        now = datetime.now(timezone.utc)
        item = {
            "id": None,
            "channel_username": "irelandrent",
            "channel_id": None,
            "message_id": 222,
            "ref": "post:irelandrent:222",
            "real_ref": None,
            "location": "Lucan",
            "price": "€750",
            "description": "Кімната",
            "property_type": "Кімната",
            "audience": None,
            "post_url": "https://t.me/irelandrent/222",
            "raw_text": "🏠 Кімната\n📍 Lucan\n💶 €750",
            "local_date": now.astimezone(main.TZ).date().isoformat(),
            "created_at_utc": now.isoformat(),
            "updated_at_utc": now.isoformat(),
        }
        with patch.object(
            main,
            "fetch_live_channel_objects",
            new=AsyncMock(return_value=[item]),
        ):
            saved = await main.sync_recent_source_posts()
        self.assertEqual(saved, 1)
        cached = main.get_cached_channel_objects("irelandrent")
        self.assertEqual(len(cached), 1)
        self.assertEqual(cached[0]["ref"], "post:irelandrent:222")

    def test_caption_markup_is_supported(self):
        html = """
        <div class=\"tgme_widget_message\" data-post=\"irelandrent/333\">
          <div class=\"tgme_widget_message_caption\">🏠 Кімната<br>📍 Lucan<br>💶 €750</div>
          <time datetime=\"2026-09-14T12:00:00+00:00\"></time>
        </div>
        """
        rows = main.parse_live_channel_page("irelandrent", html)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["message_id"], 333)

class TestV072RealFormats(unittest.TestCase):
    def test_current_multi_room_format_with_footer_parses(self):
        text = """🏡 Здаються кімнати в Dublin 15
📍 Локація: Dublin 15
💶 1 кімната: велика кімната з двоспальним ліжком -1300€
💶 2 кімната: двоспальне ліжко - 1250€
👤 Для однієї особи/пари або двох друзів з роботою або студенти
🏠 В будинку 3 кімнати, без власників
📝 Для запису на перегляд пишіть @team_housely
Ref 0000920
⸻
Переглядай актуальні пропозиції житла в нашому офіційному телеграм каналі:
🔗 https://t.me/irelandrent
⸻"""
        parsed = main.parse_live_property(text, "irelandrent", 920)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["real_ref"], "0000920")
        self.assertEqual(parsed["property_type"], "Кімната")
        self.assertEqual(parsed["location"], "Dublin 15")
        self.assertEqual(parsed["price"], "€1,300")

    def test_blank_rent_line_uses_first_actual_price(self):
        text = """🏡 Здається 2-кімнатний мобільний будинок в Co. Kildare
📍 Локація: Co. Kildare
💶 Оренда:
👤 Для однієї особи з роботою/студент(ка) - 800€
👥 Для пари з роботою/студенти - 1000€
Ref 0000609"""
        parsed = main.parse_live_property(text, "dublin_rent", 609)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["price"], "€800")
        self.assertEqual(parsed["property_type"], "Будинок")


class TestV072PartialFailure(unittest.IsolatedAsyncioTestCase):
    async def test_one_unreadable_source_prevents_partial_collection(self):
        old_sources = main.SOURCE_CHANNELS
        main.SOURCE_CHANNELS = {"dublin_rent", "irelandrent"}
        try:
            now = datetime.now(timezone.utc)
            good = {
                "id": None,
                "channel_username": "irelandrent",
                "channel_id": None,
                "message_id": 1,
                "ref": "0000001",
                "real_ref": "0000001",
                "location": "Dublin 1",
                "price": "€900",
                "description": "Кімната",
                "property_type": "Кімната",
                "audience": None,
                "post_url": "https://t.me/irelandrent/1",
                "raw_text": "Кімната",
                "local_date": now.astimezone(main.TZ).date().isoformat(),
                "created_at_utc": now.isoformat(),
                "updated_at_utc": now.isoformat(),
            }

            async def fake_fetch(username, threshold):
                if username == "dublin_rent":
                    raise main.LiveChannelReadError("blocked")
                return [good]

            with patch.object(main, "fetch_live_channel_objects", side_effect=fake_fetch), \
                 patch.object(main, "get_cached_channel_objects", return_value=[]):
                with self.assertRaises(main.LiveChannelReadError):
                    await main.get_live_properties("today")
        finally:
            main.SOURCE_CHANNELS = old_sources

class TestV072SourceTruth(unittest.IsolatedAsyncioTestCase):
    async def test_successful_direct_empty_does_not_resurrect_cache(self):
        temp_dir = tempfile.TemporaryDirectory()
        old_db_path = main.DB_PATH
        old_sources = main.SOURCE_CHANNELS
        main.DB_PATH = Path(temp_dir.name) / "collections.db"
        main.SOURCE_CHANNELS = {"irelandrent"}
        main.init_db()
        try:
            now = datetime.now(timezone.utc)
            main.upsert_property_post(
                "irelandrent",
                -1001,
                55,
                "🏠 Кімната\n📍 Dublin 8\n💶 €900\nRef 555",
                now,
            )
            with patch.object(
                main,
                "fetch_live_channel_objects",
                new=AsyncMock(return_value=[]),
            ):
                rows, hidden, missing = await main.get_live_properties("today")
            self.assertEqual(rows, [])
            self.assertEqual(hidden, 0)
            self.assertEqual(missing, [])
        finally:
            main.DB_PATH = old_db_path
            main.SOURCE_CHANNELS = old_sources
            temp_dir.cleanup()

    async def test_pagination_stopping_before_threshold_is_error(self):
        old_retries = main.LIVE_SOURCE_RETRIES
        old_delay = main.LIVE_SOURCE_RETRY_DELAY
        old_pages = main.LIVE_SOURCE_MAX_PAGES
        main.LIVE_SOURCE_RETRIES = 1
        main.LIVE_SOURCE_RETRY_DELAY = 0
        main.LIVE_SOURCE_MAX_PAGES = 3
        try:
            first = SimpleNamespace(
                text="""
                <div class=\"tgme_widget_message\" data-post=\"irelandrent/500\">
                  <div class=\"tgme_widget_message_text\">🏠 Кімната<br>📍 Dublin 8<br>💶 €900<br>Ref 500</div>
                  <time datetime=\"2026-09-14T18:00:00+00:00\"></time>
                </div>
                """,
                raise_for_status=lambda: None,
            )
            empty = SimpleNamespace(text="<html>Telegram</html>", raise_for_status=lambda: None)
            fake_client = AsyncMock()
            fake_client.get.side_effect = [first, empty]
            fake_client.__aenter__.return_value = fake_client
            fake_client.__aexit__.return_value = None

            threshold = datetime(2026, 9, 14, 0, 0, tzinfo=timezone.utc)
            with patch.object(main.httpx, "AsyncClient", return_value=fake_client):
                with self.assertRaises(main.LiveChannelReadError):
                    await main.fetch_live_channel_objects("irelandrent", threshold)
        finally:
            main.LIVE_SOURCE_RETRIES = old_retries
            main.LIVE_SOURCE_RETRY_DELAY = old_delay
            main.LIVE_SOURCE_MAX_PAGES = old_pages
