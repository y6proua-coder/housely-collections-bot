import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="housely-collections-tests-")
os.environ["VERIFY_SOURCE_POSTS"] = "true"

import main


def property_item(index, description=None, price=None, location="Dublin 1"):
    return {
        "id": index,
        "channel_id": -1001,
        "message_id": 100 + index,
        "ref": str(index).zfill(7),
        "location": location,
        "price": price or f"€{900 + index:,}",
        "description": description or f"Кімната номер {index}",
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

    def test_parse_requires_ref(self):
        self.assertIsNone(main.parse_property("Кімната в Dublin 1 — €900"))


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
        self.assertIn("Room &amp; ensuite &lt;new&gt;", text)

    def test_collection_shows_hidden_count(self):
        text = main.build_collection([property_item(1)], hidden_count=4)
        self.assertIn("Ще 4 пропозицій", text)

    def test_properties_rendered_excludes_trimmed(self):
        items = [property_item(1), property_item(2)]
        text = main.build_collection([items[0]])
        rendered = main.properties_rendered_in(text, items)
        self.assertEqual([item["id"] for item in rendered], [1])


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
                    item["description"], None, item["post_url"], "raw",
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
