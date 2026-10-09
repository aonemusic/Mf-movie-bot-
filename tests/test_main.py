import logging
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import main


class ForwarderHelperTests(unittest.TestCase):
    def test_parse_destination_ids_accepts_only_negative_numeric_ids(self):
        ids, rejected = main.parse_destination_channel_ids("-1001234567890, -1009876543210, @channel, 123, nope")
        self.assertEqual(ids, [-1001234567890, -1009876543210])
        self.assertEqual(rejected, 3)

    def test_parse_destination_ids_handles_empty_values(self):
        self.assertEqual(main.parse_destination_channel_ids(" , , "), ([], 0))

    def test_chat_reference_resolves_ids_and_usernames(self):
        self.assertEqual(main.chat_reference("-100123"), -100123)
        self.assertEqual(main.chat_reference("@archive"), "@archive")

    def test_filename_caption_is_preserved(self):
        self.assertIn("Movie.Name.mkv", main.media_caption("Movie.Name.mkv", "video"))

    def test_photo_caption_uses_source_caption_or_fallback(self):
        self.assertIn("Official poster", main.media_caption("photo.jpg", "photo", "Official poster"))
        self.assertIn("Movie poster", main.media_caption("photo.jpg", "photo"))

    def test_document_filename_is_extracted(self):
        class DocumentAttributeFilename:
            file_name = "Movie.Name.mkv"

        media = SimpleNamespace(document=SimpleNamespace(attributes=[DocumentAttributeFilename()], mime_type="video/x-matroska"), photo=None)
        self.assertEqual(main.telethon_file_details(SimpleNamespace(media=media)), [{"name": "Movie.Name.mkv", "kind": "document"}])

    def test_main_channel_button_url(self):
        keyboard = main.main_channel_keyboard()
        self.assertEqual(keyboard.inline_keyboard[0][0].text, "MF Main Channel")
        self.assertEqual(keyboard.inline_keyboard[0][0].url, "https://t.me/mfmainchannel")

    def test_log_filter_redacts_bot_api_token_in_message_and_traceback(self):
        filt = main.TelegramTokenRedactionFilter()
        record = logging.LogRecord("test", logging.ERROR, __file__, 1, "request failed", (), None)
        try:
            raise RuntimeError("https://api.telegram.org/bot123456:abc_DEF-123/sendMessage")
        except RuntimeError:
            import sys
            record.exc_info = sys.exc_info()
        self.assertTrue(filt.filter(record))
        rendered = record.getMessage()
        self.assertNotIn("123456:abc_DEF-123", rendered)
        self.assertIsNone(record.exc_info)
        self.assertIn("[REDACTED]", rendered)


if __name__ == "__main__":
    unittest.main()
