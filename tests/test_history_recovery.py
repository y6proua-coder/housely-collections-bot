import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="housely-history-tests-"))
import main


def listing(number, days=1):
    date = datetime.now(timezone.utc) - timedelta(days=days)
    return {
        "id": None, "ref": str(number).zfill(7), "real_ref": str(number).zfill(7),
        "post_url": f"https://t.me/irelandrent/{number}", "message_id": number,
        "channel_username": "irelandrent", "location": "Dublin 1", "price": "€900",
        "description": "Кімната", "property_type": "Кімната", "audience": None,
        "raw_text": f"Кімната\n📍 Dublin 1\n💶 €900\nRef {number}\nКонтакти: @team_housely",
        "local_date": date.astimezone(main.TZ).date().isoformat(), "created_at_utc": date.isoformat(),
    }


def collection_post(properties, message_id=500, days=0, username="main_test"):
    text_html = main.build_collection(properties, mode="all")
    return {
        "username": username, "message_id": message_id, "text": main.visible_html_text(text_html),
        "text_html": text_html, "message_date": datetime.now(timezone.utc) - timedelta(days=days),
    }


class TestRecoveredPublicationHistory(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(main, "DB_PATH", Path(self.temp.name) / "collections.db"),
                        patch.object(main, "MAIN_CHANNEL", "@main_test"),
                        patch.object(main, "SOURCE_CHANNELS", {"irelandrent", "dublin_rent"})]
        for p in self.patches:
            p.start()
        main.init_db()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def save(self, post, properties=()):
        return main.save_published_collection(
            post["username"], post["message_id"], post["text"], post["text_html"], post["message_date"],
            {item["post_url"]: item["ref"] for item in properties},
        )

    async def test_empty_database_recovers_old_collections_and_modes_have_different_sets(self):
        used, unused, expired, closed = listing(2417, days=3), listing(2428), listing(2320, days=29), listing(2458)
        closed["raw_text"] += "\n🔴 БІЛЬШЕ НЕ ДОСТУПНО 🔴"
        published = collection_post([used], days=2)

        async def fetch_history(username, threshold):
            return [published] if username == "main_test" else []

        async def fetch_properties(username, threshold):
            return [used, unused, expired, closed] if username == "irelandrent" else []

        with patch.object(main, "fetch_channel_messages", side_effect=fetch_history) as history, \
             patch.object(main, "fetch_live_channel_objects", side_effect=fetch_properties):
            fresh, _, _ = await main.get_live_properties("new")
            available, _, _ = await main.get_live_properties("all")
        self.assertEqual([item["ref"] for item in fresh], [unused["ref"]])
        self.assertEqual({item["ref"] for item in available}, {used["ref"], unused["ref"]})
        self.assertEqual(history.await_count, 3)
        self.assertEqual(main.recovered_history_stats(), {"publications": 1, "posts": 1})
        main.init_db()
        self.assertEqual(main.exclude_collected_properties([used, unused]), [unused])

    async def test_history_failure_does_not_silently_treat_everything_as_fresh(self):
        with patch.object(main, "fetch_channel_messages", new=AsyncMock(side_effect=main.LiveChannelReadError("blocked"))):
            with self.assertRaises(main.PublicationHistoryReadError):
                await main.ensure_publication_history([listing(2417)])
        with main.db() as conn:
            self.assertIsNone(conn.execute("SELECT value FROM bot_settings WHERE key='collection_history_checked_at'").fetchone())

    async def test_old_collections_outside_recovery_window_are_not_imported(self):
        old = collection_post([listing(2417)], days=29)
        with patch.object(main, "fetch_channel_messages", new=AsyncMock(return_value=[old])):
            await main.ensure_publication_history()
        self.assertEqual(main.recovered_history_stats()["posts"], 0)

    async def test_recent_successful_history_sync_is_reused(self):
        with patch.object(main, "fetch_channel_messages", new=AsyncMock(return_value=[])) as fetch:
            await main.ensure_publication_history()
            await main.ensure_publication_history()
        self.assertEqual(fetch.await_count, 3)

    def test_links_in_bold_html_are_kept_while_parsing_public_channel(self):
        post = collection_post([listing(2417)])
        page = '<div data-post="main_test/500"><div class="tgme_widget_message_text">' + post["text_html"] + '</div><time datetime="' + post["message_date"].isoformat() + '"></time></div>'
        parsed = main._parse_channel_page_messages("main_test", page)[0]
        self.assertTrue(self.save(parsed))
        self.assertEqual(main.exclude_collected_properties([listing(2417)]), [])

    def test_normal_advert_and_unrelated_channel_are_not_publication_history(self):
        p = listing(2417)
        self.assertFalse(main.save_published_collection("main_test", 500, p["raw_text"], '<a href="' + p["post_url"] + '">Детальніше</a>', datetime.now(timezone.utc)))
        post = collection_post([p], username="unrelated_channel")
        self.assertFalse(self.save(post))

    def test_forward_channel_update_observes_a_published_collection(self):
        post = collection_post([listing(2417)])
        message = SimpleNamespace(chat=SimpleNamespace(username="main_test", id=-1009), message_id=500,
                                  text=post["text"], caption=None, text_html=post["text_html"], date=post["message_date"])
        self.assertTrue(main.save_channel_post(SimpleNamespace(channel_post=message, edited_channel_post=None)))
        self.assertEqual(main.recovered_history_stats()["posts"], 1)

    def test_reimport_is_idempotent_and_known_ref_excludes_a_repost(self):
        old, repost = listing(2417), listing(2488)
        repost["ref"] = old["ref"]
        post = collection_post([old])
        self.save(post, [old])
        self.save(post, [old])
        self.assertEqual(main.recovered_history_stats(), {"publications": 1, "posts": 1})
        self.assertEqual(main.exclude_collected_properties([repost]), [])

    def test_undo_is_not_reversed_by_reimport_of_the_same_public_page(self):
        p = listing(2417)
        post = collection_post([p])
        publication_id = main.record_publication(123, "all", post["text_html"], [p],
                                               [{"destination": "@main_test", "chat_id": -1009, "message_id": 500}])
        self.save(post, [p])
        message = main.get_active_publication_messages(publication_id)[0]
        main.mark_publication_message_deleted(message["id"])
        main.finish_publication_undo(publication_id)
        self.save(post, [p])
        self.assertEqual(main.exclude_collected_properties([p]), [p])

    def test_history_from_another_collection_still_excludes_undone_posts(self):
        p = listing(2417)
        self.save(collection_post([p], message_id=501), [p])
        self.save(collection_post([p], message_id=502), [p])
        with main.db() as conn:
            conn.execute("UPDATE published_collection_posts SET deleted_at_utc='cancelled' WHERE collection_message_id=501")
        self.assertEqual(main.exclude_collected_properties([p]), [])

    def test_no_overflow_notice_is_generated_in_either_mode(self):
        for mode in ("new", "all"):
            text = main.build_collection([listing(2417)], hidden_count=47, mode=mode)
            self.assertNotIn("➕", text)
            self.assertNotIn("не показано", text)
            self.assertNotIn("Ще 47", text)

    def test_normalizes_only_supported_source_post_urls(self):
        urls = main.listing_urls_from_html('<a href="https://t.me/IRELANDRENT/2417?single">Детальніше</a>'
                                          '<a href="https://t.me/team_housely">Менеджер</a>'
                                          '<a href="https://example.com/irelandrent/2418">Other</a>')
        self.assertEqual(urls, {"https://t.me/irelandrent/2417"})

    def test_long_collection_has_no_overflow_notice_after_automatic_trimming(self):
        items = [listing(number) for number in range(2400, 2475)]
        text = main.build_collection(items, hidden_count=47, mode="all")
        self.assertLessEqual(len(text), 3900)
        self.assertNotIn("не показано", text)
        self.assertNotIn("➕ Ще", text)

    def test_edit_removes_old_overflow_line_but_keeps_property_lines(self):
        text = '🏠 Кімната для пари - €900\n➕ <b>Ще 47 пропозицій</b> не показано в цій підбірці.\nКонтакти'
        self.assertEqual(main.remove_overflow_notice(text), '🏠 Кімната для пари - €900\nКонтакти')
