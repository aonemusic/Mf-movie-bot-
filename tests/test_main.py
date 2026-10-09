import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import main


class SearchAndMediaTests(unittest.TestCase):
    def test_search_normalization_is_case_insensitive_and_unicode_safe(self):
        self.assertEqual(main.normalize_search_text("The.Matrix_1999.MKV"), "the matrix 1999 mkv")
        self.assertEqual(main.normalize_search_text("Cafe\u0301 2025"), "cafe\u0301 2025")

    def test_media_details_preserve_document_filename_and_size(self):
        document = SimpleNamespace(file_name="Movie.Name.mkv", file_size=987654321)
        message = SimpleNamespace(
            document=document, video=None, audio=None, animation=None,
            video_note=None, voice=None, photo=None, caption="Movie Name",
            message_id=12,
        )
        details = main.extract_media_details(message)
        self.assertEqual(details["file_name"], "Movie.Name.mkv")
        self.assertEqual(details["file_size"], 987654321)
        self.assertIn("movie name", details["search_text"])

    def test_video_without_filename_gets_deterministic_name(self):
        message = SimpleNamespace(
            document=None, video=SimpleNamespace(file_size=3000), audio=None,
            animation=None, video_note=None, voice=None, photo=None,
            caption=None, message_id=44,
        )
        self.assertEqual(main.extract_media_details(message)["file_name"], "video_44.mp4")

    def test_photo_uses_largest_size(self):
        small = SimpleNamespace(file_size=10, file_id="small")
        large = SimpleNamespace(file_size=100, file_id="large")
        message = SimpleNamespace(
            document=None, video=None, audio=None, animation=None,
            video_note=None, voice=None, photo=[small, large], caption="Film poster",
            message_id=3,
        )
        details = main.extract_media_details(message)
        self.assertEqual(details["file_size"], 100)
        self.assertEqual(details["file_name"], "Film poster")

    def test_file_size_formatting(self):
        self.assertEqual(main.format_file_size(0), "Size unknown")
        self.assertEqual(main.format_file_size(1024 * 1024), "1.0 MB")

    def test_public_and_private_telegram_post_links(self):
        public = SimpleNamespace(username="archive", id=-1001234567890)
        private = SimpleNamespace(username=None, id=-1001234567890)
        self.assertEqual(main.message_link(public, 55), "https://t.me/archive/55")
        self.assertEqual(main.message_link(private, 55), "https://t.me/c/1234567890/55")

    def test_search_catalog_matches_all_terms_as_substrings(self):
        async def run():
            records = {
                "-1001_1": {"file_name": "The Dark Knight.mkv", "search_text": "the dark knight mkv", "indexed_at": "2024"},
                "-1001_2": {"file_name": "Dark City.mkv", "search_text": "dark city mkv", "indexed_at": "2025"},
            }
            with patch.object(main, "get_catalog", new=AsyncMock(return_value=records)):
                matches = await main.search_catalog("DARK KN")
            self.assertEqual([item[0] for item in matches], ["-1001_1"])
        asyncio.run(run())

    def test_callback_file_key_pattern_is_telegram_safe(self):
        import re
        valid = re.fullmatch(r"-?\d{1,19}_\d{1,20}", "-1001234567890_42")
        invalid = re.fullmatch(r"-?\d{1,19}_\d{1,20}", "../../private")
        self.assertIsNotNone(valid)
        self.assertIsNone(invalid)

    def test_forward_channel_button_has_expected_label_and_url(self):
        keyboard = main.main_channel_keyboard()
        button = keyboard.inline_keyboard[0][0]
        self.assertEqual(button.text, "MF Main Channel")
        self.assertEqual(button.url, "https://t.me/mfmainchannel")

    def test_forward_caption_escapes_dynamic_filename(self):
        caption = main.media_caption({"file_name": "Movie & <title>.mkv"})
        self.assertIn("Movie &amp; &lt;title&gt;.mkv", caption)

    def test_plain_movie_message_routes_to_search_without_search_command(self):
        async def run():
            message = SimpleNamespace(text="Interstellar 2014")
            update = SimpleNamespace(
                channel_post=None,
                my_chat_member=None,
                callback_query=None,
                effective_message=message,
                effective_user=SimpleNamespace(id=42),
            )
            context = SimpleNamespace()
            with patch.object(main, "search_command", new=AsyncMock()) as search:
                await main.dispatch_update(update, context)
            search.assert_awaited_once_with(update, context, "Interstellar 2014")
        asyncio.run(run())

    def test_destination_prompt_requires_yes_no_and_mentions_future_posts(self):
        async def run():
            bot = SimpleNamespace(send_message=AsyncMock())
            previous_app = main.BOT_APP
            previous_admin = main.ADMIN_ID
            try:
                main.BOT_APP = SimpleNamespace(bot=bot)
                main.ADMIN_ID = 100
                await main.prompt_destination(-100123, "Archive destination")
                kwargs = bot.send_message.await_args.kwargs
                self.assertIn("new media", kwargs["text"])
                buttons = kwargs["reply_markup"].inline_keyboard
                self.assertEqual(len(buttons), 2)
                self.assertTrue(buttons[0][0].callback_data.startswith("destination:yes:"))
                self.assertTrue(buttons[1][0].callback_data.startswith("destination:no:"))
            finally:
                main.BOT_APP = previous_app
                main.ADMIN_ID = previous_admin
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
