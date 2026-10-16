import asyncio
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import main

REAL_IS_USER_MEMBER = main.is_user_member


class FakeReference:
    def __init__(self, value=None):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value

    def transaction(self, updater):
        self.value = updater(self.value)
        return self.value


class FakeTreeReference:
    def __init__(self, tree, path):
        self.tree = tree
        self.path = tuple(path.split("/"))

    def _node(self, path=None, create=False):
        node = self.tree
        for segment in self.path if path is None else path:
            if not isinstance(node, dict):
                return None
            if segment not in node:
                if not create:
                    return None
                node[segment] = {}
            node = node[segment]
        return node

    def get(self):
        return self._node()

    def set(self, value):
        parent = self._node(self.path[:-1], create=True)
        parent[self.path[-1]] = value

    def update(self, value):
        node = self._node(create=True)
        if not isinstance(node, dict):
            raise TypeError("target is not an object")
        node.update(value)

    def transaction(self, updater):
        updated = updater(self.get())
        self.set(updated)
        return updated


class SearchAndMediaTests(unittest.TestCase):
    def setUp(self):
        membership = patch.object(main, "is_user_member", new=AsyncMock(return_value=True))
        membership.start()
        self.addCleanup(membership.stop)

    def test_search_normalization_is_case_insensitive_and_unicode_safe(self):
        self.assertEqual(main.normalize_search_text("The.Matrix_1999.MKV"), "the matrix 1999 mkv")
        self.assertEqual(main.normalize_search_text("Cafe\u0301 2025"), "cafe\u0301 2025")

    def test_rate_limiter_blocks_burst_and_allows_after_window(self):
        old_values = (main.RATE_LIMIT_MAX_MESSAGES, main.RATE_LIMIT_WINDOW_SECONDS, dict(main.USER_UPDATE_TIMES))

        async def run():
            try:
                main.RATE_LIMIT_MAX_MESSAGES = 2
                main.RATE_LIMIT_WINDOW_SECONDS = 10
                main.USER_UPDATE_TIMES.clear()
                self.assertTrue(await main.allow_user_update(7001))
                self.assertTrue(await main.allow_user_update(7001))
                self.assertFalse(await main.allow_user_update(7001))
                self.assertTrue(await main.allow_user_update(main.ADMIN_ID or 7002))
            finally:
                main.RATE_LIMIT_MAX_MESSAGES, main.RATE_LIMIT_WINDOW_SECONDS, previous = old_values
                main.USER_UPDATE_TIMES.clear()
                main.USER_UPDATE_TIMES.update(previous)

        asyncio.run(run())

    def test_telegram_startup_operation_retries_transient_errors(self):
        attempts = 0

        async def operation():
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise main.TelegramError("temporary outage")
            return "ready"

        async def run():
            with patch.object(main.asyncio, "sleep", new=AsyncMock()):
                return await main.retry_telegram_operation(operation, "test operation")

        self.assertEqual(asyncio.run(run()), "ready")
        self.assertEqual(attempts, 3)

    def test_media_details_preserve_filename_and_size(self):
        message = SimpleNamespace(
            document=SimpleNamespace(file_name="Movie.Name.mkv", file_size=987654321),
            video=None, audio=None, animation=None, video_note=None,
            voice=None, photo=None, caption="Movie Name", message_id=12,
        )
        details = main.extract_media_details(message)
        self.assertEqual(details["file_name"], "Movie.Name.mkv")
        self.assertEqual(details["file_size"], 987654321)
        self.assertEqual(details["caption"], "Movie Name")
        self.assertEqual(details["caption_entities"], [])
        self.assertIn("movie name", details["search_text"])

    def test_photo_uses_largest_size(self):
        message = SimpleNamespace(
            document=None, video=None, audio=None, animation=None, video_note=None,
            voice=None, photo=[SimpleNamespace(file_size=10), SimpleNamespace(file_size=100)],
            caption="Film poster", message_id=3,
        )
        details = main.extract_media_details(message)
        self.assertEqual(details["file_size"], 100)
        self.assertEqual(details["file_name"], "Film poster")

    def test_start_welcome_shows_required_join_link_and_instagram(self):
        message = SimpleNamespace(
            chat=SimpleNamespace(type="private"),
            reply_text=AsyncMock(),
        )
        update = SimpleNamespace(effective_message=message, effective_user=None)

        asyncio.run(main.start_command(update, SimpleNamespace(bot=None)))

        welcome = message.reply_text.await_args.args[0]
        self.assertIn(main.MAIN_CHANNEL_URL, welcome)
        self.assertIn("to unlock the library", welcome)
        self.assertIn(f'<a href="{main.INSTAGRAM_URL}">Instagram</a>', welcome)
        self.assertIn(main.ADMIN_CONTACT_URL, welcome)

    def test_force_join_helper_checks_telegram_and_fails_closed(self):
        bot = SimpleNamespace(get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")))

        async def run():
            with patch.object(main, "FORCE_JOIN_CHANNEL_ID", "-1004352245988"):
                self.assertTrue(await REAL_IS_USER_MEMBER(bot, 77))
                bot.get_chat_member.assert_awaited_once_with(chat_id=-1004352245988, user_id=77)
                bot.get_chat_member.return_value = SimpleNamespace(status="left")
                self.assertFalse(await REAL_IS_USER_MEMBER(bot, 77))
                with patch.object(main, "FORCE_JOIN_CHANNEL_ID", ""):
                    self.assertFalse(await REAL_IS_USER_MEMBER(bot, 77))

        asyncio.run(run())

    def test_force_join_can_be_disabled_without_calling_telegram(self):
        bot = SimpleNamespace(get_chat_member=AsyncMock())

        async def run():
            with patch.object(main, "FORCE_JOIN_ENABLED", False):
                self.assertTrue(await REAL_IS_USER_MEMBER(bot, 77))
            bot.get_chat_member.assert_not_awaited()

        asyncio.run(run())

    def test_force_join_channel_edit_updates_id_username_url(self):
        self.assertEqual(main.parse_force_join_channel_input("-1001234567890 @newchannel"), ("-1001234567890", "@newchannel"))
        self.assertIsNone(main.parse_force_join_channel_input("@newchannel"))
        old_values = (main.FORCE_JOIN_CHANNEL_ID, main.FORCE_JOIN_CHANNEL_USERNAME, main.FORCE_JOIN_CHANNEL_URL)

        async def run():
            try:
                with patch.object(main, "firebase_ready", return_value=False):
                    await main.save_force_join_channel("-1001234567890", "@newchannel")
                self.assertEqual(main.FORCE_JOIN_CHANNEL_ID, "-1001234567890")
                self.assertEqual(main.FORCE_JOIN_CHANNEL_USERNAME, "@newchannel")
                self.assertEqual(main.FORCE_JOIN_CHANNEL_URL, "https://t.me/newchannel")
            finally:
                main.FORCE_JOIN_CHANNEL_ID, main.FORCE_JOIN_CHANNEL_USERNAME, main.FORCE_JOIN_CHANNEL_URL = old_values

        asyncio.run(run())

    def test_cancel_clears_pending_channel_updates_without_saving(self):
        old_admin = main.ADMIN_ID
        old_force = set(main.PENDING_FORCE_JOIN_EDITS)
        old_channels = dict(main.PENDING_CHANNEL_ADD_ADMINS)
        message = SimpleNamespace(reply_text=AsyncMock())

        async def run():
            try:
                main.ADMIN_ID = 42
                main.PENDING_FORCE_JOIN_EDITS.clear()
                main.PENDING_FORCE_JOIN_EDITS.add(42)
                main.PENDING_CHANNEL_ADD_ADMINS.clear()
                main.PENDING_CHANNEL_ADD_ADMINS[42] = {"url": "https://t.me/example"}
                await main.cancel_pending_command(
                    SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=42)),
                    SimpleNamespace(),
                )
            finally:
                main.ADMIN_ID = old_admin
                main.PENDING_FORCE_JOIN_EDITS.clear()
                main.PENDING_FORCE_JOIN_EDITS.update(old_force)
                main.PENDING_CHANNEL_ADD_ADMINS.clear()
                main.PENDING_CHANNEL_ADD_ADMINS.update(old_channels)

        asyncio.run(run())
        self.assertFalse(main.PENDING_FORCE_JOIN_EDITS)
        self.assertFalse(main.PENDING_CHANNEL_ADD_ADMINS)
        self.assertIn("No changes were saved", message.reply_text.await_args.args[0])

    def test_force_join_command_is_admin_only_and_toggles_state(self):
        old_admin = main.ADMIN_ID
        old_enabled = main.FORCE_JOIN_ENABLED

        async def run():
            try:
                main.ADMIN_ID = 42
                main.FORCE_JOIN_ENABLED = True
                non_admin_message = SimpleNamespace(reply_text=AsyncMock())
                await main.force_join_command(
                    SimpleNamespace(effective_message=non_admin_message, effective_user=SimpleNamespace(id=7)),
                    SimpleNamespace(),
                    "off",
                )
                non_admin_message.reply_text.assert_awaited_once_with("Admin only.")

                admin_message = SimpleNamespace(reply_text=AsyncMock())
                with patch.object(main, "set_force_join_enabled", new=AsyncMock()) as toggle:
                    await main.force_join_command(
                        SimpleNamespace(effective_message=admin_message, effective_user=SimpleNamespace(id=42)),
                        SimpleNamespace(),
                        "off",
                    )
                toggle.assert_awaited_once_with(False)
                self.assertIn("INACTIVE", admin_message.reply_text.await_args.args[0])
            finally:
                main.ADMIN_ID = old_admin
                main.FORCE_JOIN_ENABLED = old_enabled

        asyncio.run(run())

    def test_movie_channel_link_validation_and_join_message(self):
        self.assertTrue(main.valid_movie_channel_link("https://t.me/mf_movies"))
        self.assertFalse(main.valid_movie_channel_link("https://example.com/movies"))
        bot = SimpleNamespace(send_message=AsyncMock())
        old_links = list(main.MOVIE_CHANNEL_LINKS)

        async def run():
            try:
                main.MOVIE_CHANNEL_LINKS[:] = [
                    {"url": "https://t.me/mf_movies", "name": "MF Movies"},
                    {"url": "https://t.me/mf_updates", "name": "MF Updates"},
                ]
                await main.send_join_and_enjoy_message(bot, 77)
            finally:
                main.MOVIE_CHANNEL_LINKS[:] = old_links

        asyncio.run(run())
        bot.send_message.assert_awaited_once()
        kwargs = bot.send_message.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], 77)
        self.assertNotIn("JOIN AND ENJOY", kwargs["text"])
        self.assertEqual(
            [(button.text, button.url) for row in kwargs["reply_markup"].inline_keyboard for button in row],
            [("MF Movies", "https://t.me/mf_movies"), ("MF Updates", "https://t.me/mf_updates")],
        )

    def test_nonmembers_are_blocked_from_every_catalog_and_delivery_path(self):
        user = SimpleNamespace(id=77)
        bot = SimpleNamespace(send_chat_action=AsyncMock())
        context = SimpleNamespace(bot=bot)

        async def run():
            with patch.object(main, "is_user_member", new=AsyncMock(return_value=False)):
                search_message = SimpleNamespace(
                    chat=SimpleNamespace(type="private"), chat_id=77, reply_text=AsyncMock()
                )
                await main.search_command(
                    SimpleNamespace(effective_message=search_message, effective_user=user),
                    context,
                    "Fractured",
                )
                self.assertIn("MEMBERSHIP REQUIRED", search_message.reply_text.await_args.args[0])
                self.assertEqual(
                    search_message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0].url,
                    main.MAIN_CHANNEL_URL,
                )

                latest_message = SimpleNamespace(
                    chat=SimpleNamespace(type="private"), chat_id=77, reply_text=AsyncMock()
                )
                await main.latest_command(
                    SimpleNamespace(effective_message=latest_message, effective_user=user), context
                )
                self.assertIn("MEMBERSHIP REQUIRED", latest_message.reply_text.await_args.args[0])

                file_message = SimpleNamespace(
                    chat=SimpleNamespace(type="private"), reply_text=AsyncMock()
                )
                file_query = SimpleNamespace(
                    message=file_message, from_user=user, data="file:-1001_9", answer=AsyncMock()
                )
                await main.file_button_callback(SimpleNamespace(callback_query=file_query), context)
                self.assertIn("MEMBERSHIP REQUIRED", file_message.reply_text.await_args.args[0])

                page_message = SimpleNamespace(
                    chat=SimpleNamespace(type="private"), chat_id=77, reply_text=AsyncMock()
                )
                page_query = SimpleNamespace(
                    message=page_message, from_user=user, data="page:missing:10", answer=AsyncMock()
                )
                await main.search_page_callback(SimpleNamespace(callback_query=page_query), context)
                self.assertIn("MEMBERSHIP REQUIRED", page_message.reply_text.await_args.args[0])

                token = main.make_movie_request_session(user.id, "Moonlight")
                status, title, keyboard = await main.submit_movie_request(bot, user, token)
                self.assertEqual((status, title), ("membership", "Moonlight"))
                self.assertEqual(keyboard.inline_keyboard[0][0].url, main.MAIN_CHANNEL_URL)
                main.MOVIE_REQUEST_SESSIONS.pop(token, None)

        asyncio.run(run())

    def test_start_search_deep_link_runs_private_search_from_title_slug(self):
        message = SimpleNamespace(chat=SimpleNamespace(type="private"), reply_text=AsyncMock())
        user = SimpleNamespace(id=77)
        update = SimpleNamespace(effective_message=message, effective_user=user)
        context = SimpleNamespace(bot=SimpleNamespace())

        async def run():
            with patch.object(main, "register_user", new=AsyncMock()) as register, patch.object(
                main, "search_command", new=AsyncMock()
            ) as search:
                await main.start_command(update, context, "search_Fractured_2019")
                register.assert_not_awaited()
                search.assert_awaited_once_with(update, context, "Fractured 2019")

        asyncio.run(run())

    def test_file_size_formatting(self):
        self.assertEqual(main.format_file_size(0), "Size unknown")
        self.assertEqual(main.format_file_size(1024 * 1024), "1.0 MB")

    def test_delivered_file_caption_attaches_source_and_requested_handles(self):
        caption, entities = main.delivered_file_caption(
            {
                "file_name": "Movie.mkv",
                "caption": "Original movie caption",
                "caption_entities": [{"type": "bold", "offset": 0, "length": 8}],
            }
        )
        self.assertTrue(caption.startswith("Original movie caption\n\n"))
        self.assertIn("Main channel: @mfmainchannel", caption)
        self.assertIn("Bot: @mf_movie_bot", caption)
        self.assertIn("Auto-deletes in 10 minutes.", caption)
        self.assertEqual(entities[0].type, "bold")
        self.assertEqual((entities[0].offset, entities[0].length), (0, 8))
        self.assertEqual(entities[-1].type, "text_link")
        self.assertEqual(entities[-1].url, main.INSTAGRAM_URL)
        self.assertEqual(
            entities[-1].offset,
            main._utf16_units(caption[: caption.index("Instagram")]),
        )

    def test_delivered_file_caption_removes_source_usernames_and_clickable_entities(self):
        caption, entities = main.delivered_file_caption(
            {
                "file_name": "Movie.mkv",
                "caption": "Movie title\nJoin @source_channel for more",
                "caption_entities": [{"type": "mention", "offset": 23, "length": 15}],
            }
        )
        self.assertNotIn("@source_channel", caption)
        self.assertIn("Movie title", caption)
        self.assertEqual([entity.type for entity in entities], ["text_link"])
        self.assertEqual(entities[0].url, main.INSTAGRAM_URL)

    def test_delivered_file_caption_preserves_footer_when_source_caption_is_too_long(self):
        caption, entities = main.delivered_file_caption(
            {"caption": "🎬" * 1000, "file_name": "Movie.mkv", "caption_entities": []}
        )
        self.assertLessEqual(main._utf16_units(caption), 1024)
        self.assertIn("Main channel: @mfmainchannel", caption)
        self.assertIn("Bot: @mf_movie_bot", caption)
        self.assertEqual(len(entities), 1)
        self.assertEqual(entities[0].type, "text_link")
        self.assertEqual(entities[0].url, main.INSTAGRAM_URL)

    def test_backup_source_is_added_without_replacing_existing_sources(self):
        self.assertEqual(
            main.configured_source_chat_refs("@archive_one, -100123", "-1004443404016"),
            ["@archive_one", "-100123", "-1004443404016"],
        )
        self.assertEqual(
            main.configured_source_chat_refs("-1004443404016", "-1004443404016"),
            ["-1004443404016"],
        )

    def test_search_catalog_returns_all_matches_for_pagination(self):
        async def run():
            records = {
                f"-1001_{i}": {
                    "file_name": f"The Matrix {i}.mkv",
                    "search_text": f"the matrix {i} mkv",
                    "indexed_at": f"2026-01-{i:02d}",
                }
                for i in range(1, 13)
            }
            with patch.object(main, "get_catalog", new=AsyncMock(return_value=records)):
                matches = await main.search_catalog("matrix")
            self.assertEqual(len(matches), 12)
        asyncio.run(run())

    def test_search_catalog_ranks_exact_title_ahead_of_partial_and_caption_matches(self):
        records = {
            "exact": {
                "file_name": "Matrix.mkv",
                "search_text": "matrix",
                "indexed_at": "2024-01-01",
            },
            "partial": {
                "file_name": "Matrix Reloaded.mkv",
                "search_text": "matrix reloaded",
                "indexed_at": "2026-01-01",
            },
            "caption": {
                "file_name": "Cinema Collection.mkv",
                "search_text": "cinema collection matrix reference",
                "indexed_at": "2027-01-01",
            },
        }

        async def run():
            with patch.object(main, "get_catalog", new=AsyncMock(return_value=records)):
                results = await main.search_catalog("matrix")
            self.assertEqual([key for key, _ in results], ["exact", "partial", "caption"])

        asyncio.run(run())

    def test_latest_catalog_orders_by_upload_timestamp_newest_first(self):
        records = {
            "old": {"file_name": "Old.mkv", "indexed_at": "2025-01-01T00:00:00Z"},
            "new": {"file_name": "New.mkv", "indexed_at": "2026-03-01T00:00:00Z"},
        }

        async def run():
            with patch.object(main, "get_catalog", new=AsyncMock(return_value=records)):
                results = await main.latest_catalog()
            self.assertEqual([key for key, _ in results], ["new", "old"])

        asyncio.run(run())

    def test_search_results_keep_blue_file_buttons_and_green_navigation_without_channel_buttons(self):
        matches = [
            (f"-1001_{i}", {"file_name": f"Movie {i}.mkv", "file_size": i * 1024})
            for i in range(1, 23)
        ]
        keyboard = main.search_page_keyboard(matches, "session12345678", 0)
        self.assertEqual(len(keyboard.inline_keyboard), 11)
        file_buttons = [row[0] for row in keyboard.inline_keyboard[:-1]]
        self.assertTrue(all(button.style == "primary" for button in file_buttons))
        self.assertTrue(file_buttons[0].text.startswith("1.0 KB · Movie 1.mkv"))
        self.assertTrue(all(len(row) == 1 for row in keyboard.inline_keyboard[:-1]))
        self.assertFalse(any(button.text == "Join Main Channel" for row in keyboard.inline_keyboard for button in row))
        navigation = keyboard.inline_keyboard[-1]
        self.assertEqual(navigation[0].callback_data, "page:session12345678:10")
        self.assertEqual(navigation[0].style, "success")

        second_page = main.search_page_keyboard(matches, "session12345678", 10)
        self.assertEqual(len(second_page.inline_keyboard), 11)
        self.assertEqual(second_page.inline_keyboard[0][0].style, "primary")
        self.assertEqual(second_page.inline_keyboard[-1][0].text, "‹ Back")
        self.assertEqual(second_page.inline_keyboard[-1][1].text, "Next ›")

    def test_group_result_buttons_open_bot_with_signed_file_payload(self):
        with patch.object(main, "BOT_TOKEN", "123456:unit-test-token"), patch.object(
            main, "BOT_USERNAME", "mf_movie_library_bot"
        ):
            key = "-1004352245988_12345"
            payload = main.make_file_start_payload(key)
            self.assertLessEqual(len(payload), 64)
            self.assertEqual(main.file_key_from_start_payload(payload), key)
            self.assertIsNone(main.file_key_from_start_payload(payload + "x"))

            keyboard = main.group_search_page_keyboard(
                [(key, {"file_name": "Example.Movie.mkv", "file_size": 1024})],
                "group-session",
                0,
            )
            file_button = keyboard.inline_keyboard[0][0]
            self.assertEqual(file_button.url, f"https://t.me/mf_movie_library_bot?start={payload}")
            self.assertTrue(file_button.text.startswith("1.0 KB · Example.Movie.mkv"))
            self.assertEqual(file_button.style, "primary")
            self.assertEqual(len(keyboard.inline_keyboard[0]), 1)

    def test_delivered_file_keyboard_places_red_main_and_green_request_group_links(self):
        keyboard = main.delivered_file_keyboard()
        main_button, requests_button = keyboard.inline_keyboard[0]
        self.assertEqual(main_button.url, "https://t.me/mfmainchannel")
        self.assertEqual(main_button.style, "danger")
        self.assertEqual(requests_button.url, "https://t.me/MFMovieandSeriesRequestGroup")
        self.assertEqual(requests_button.style, "success")

    def test_new_file_announcement_has_filename_size_and_get_file_deep_link(self):
        record = {"file_name": "Movie <HD>.mkv", "file_size": 1024 * 1024}
        with patch.object(main, "BOT_TOKEN", "123456:unit-test-token"), patch.object(
            main, "BOT_USERNAME", "mf_movie_bot"
        ):
            text = main.new_file_announcement_text(record)
            keyboard = main.new_file_announcement_keyboard("-1004443404016_88", record["file_name"])
            button = keyboard.inline_keyboard[0][0]
            payload = button.url.split("?start=", 1)[1]
            self.assertIn("Movie &lt;HD&gt;.mkv", text)
            self.assertIn("1.0 MB", text)
            self.assertEqual(button.text, "Get File")
            self.assertEqual(payload, "search_Movie_HD")
            self.assertEqual(button.url, "https://t.me/mf_movie_bot?start=search_Movie_HD")
            self.assertEqual(main.make_movie_search_start_payload("Fractured 2019.mkv"), "search_Fractured_2019")

    def test_new_upload_is_announced_once_to_all_configured_channels(self):
        chat = SimpleNamespace(id=-1004443404016, title="DB archive", username=None)
        post = SimpleNamespace(chat=chat, message_id=88)
        update = SimpleNamespace(channel_post=post)
        bot = SimpleNamespace(send_message=AsyncMock())
        application = SimpleNamespace(bot=bot)
        record = {"file_name": "Movie.mkv", "file_size": 1024}

        async def run():
            with patch.object(main, "SOURCE_CHANNEL_IDS", {-1004443404016, -1004443404020}), patch.object(
                main, "MOVIE_ANNOUNCEMENT_CHANNELS", ["-1003391211397"]
            ), patch.object(
                main, "MAIN_CHANNEL_ID", "-1004352245988"
            ), patch.object(main, "BOT_APP", application), patch.object(
                main, "BOT_USERNAME", "mf_movie_library_bot"
            ), patch.object(main, "BOT_TOKEN", "123456:unit-test-token"), patch.object(
                main, "firebase_ready", return_value=True
            ), patch.object(main, "extract_media_details", return_value=record), patch.object(
                main, "save_new_media_post", new=AsyncMock(return_value=True)
            ) as save:
                await main.index_channel_post(update)
                save.assert_awaited_once()
            self.assertEqual(
                [call.kwargs["chat_id"] for call in bot.send_message.await_args_list],
                [-1004443404020, -1004443404016, -1003391211397, -1004352245988],
            )
            self.assertIn("Movie.mkv", bot.send_message.await_args_list[0].kwargs["text"])
            self.assertEqual(
                bot.send_message.await_args_list[0].kwargs["reply_markup"].inline_keyboard[0][0].text,
                "Get File",
            )
            self.assertEqual(
                bot.send_message.await_args_list[0].kwargs["reply_markup"].inline_keyboard[0][0].url,
                "https://t.me/mf_movie_library_bot?start=search_Movie",
            )
            bot.send_message.reset_mock()
            with patch.object(main, "SOURCE_CHANNEL_IDS", {-1004443404016, -1004443404020}), patch.object(
                main, "MOVIE_ANNOUNCEMENT_CHANNELS", ["-1003391211397"]
            ), patch.object(
                main, "MAIN_CHANNEL_ID", "-1004352245988"
            ), patch.object(main, "BOT_APP", application), patch.object(
                main, "BOT_USERNAME", "mf_movie_library_bot"
            ), patch.object(main, "BOT_TOKEN", "123456:unit-test-token"), patch.object(
                main, "firebase_ready", return_value=True
            ), patch.object(main, "extract_media_details", return_value=record), patch.object(
                main, "save_new_media_post", new=AsyncMock(return_value=False)
            ):
                await main.index_channel_post(update)
            bot.send_message.assert_not_awaited()

        asyncio.run(run())

    def test_group_search_returns_bot_deep_link_results_without_registering_group_user(self):
        message = SimpleNamespace(
            chat=SimpleNamespace(type="supergroup"),
            chat_id=-100123,
            reply_text=AsyncMock(),
        )
        update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=77))
        context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))

        async def run():
            with patch.object(main, "BOT_TOKEN", "123456:unit-test-token"), patch.object(
                main, "BOT_USERNAME", "mf_movie_library_bot"
            ), patch.object(
                main, "MAIN_CHANNEL_ID", ""
            ), patch.object(main, "send_chat_action", new=AsyncMock()) as chat_action, patch.object(
                main, "schedule_group_result_keyboard_removal", new=Mock()
            ) as cleanup, patch.object(
                main, "register_user", new=AsyncMock()
            ) as register, patch.object(
                main, "firebase_ready", return_value=True
            ), patch.object(main, "record_active_user", new=AsyncMock()) as active_user, patch.object(
                main, "record_search", new=AsyncMock()
            ), patch.object(
                main,
                "search_catalog",
                new=AsyncMock(return_value=[("-1001_9", {"file_name": "Movie.mkv", "file_size": 2048})]),
            ):
                await main.search_command(update, context, "Movie")
                register.assert_not_awaited()
                active_user.assert_awaited_once_with(update.effective_user)
                chat_action.assert_awaited_once_with(context.bot, message.chat_id, "typing")
                cleanup.assert_called_once_with(
                    context.bot,
                    message.chat_id,
                    message.reply_text.return_value.message_id,
                )
            markup = message.reply_text.await_args.kwargs["reply_markup"]
            self.assertTrue(markup.inline_keyboard[0][0].url.startswith("https://t.me/mf_movie_library_bot?start="))
        asyncio.run(run())

    def test_latest_command_and_pagination_work_in_groups_with_group_typing(self):
        user = SimpleNamespace(id=79)
        message = SimpleNamespace(
            chat=SimpleNamespace(type="supergroup"),
            chat_id=-100987,
            text="/latest",
            reply_text=AsyncMock(),
        )
        update = SimpleNamespace(
            channel_post=None,
            callback_query=None,
            effective_message=message,
            effective_user=user,
        )
        context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))
        movies = [
            (f"-1001_{i}", {"file_name": f"Movie {i}.mkv", "file_size": i * 1024})
            for i in range(1, 13)
        ]

        async def run():
            with patch.object(main, "BOT_TOKEN", "123456:unit-test-token"), patch.object(
                main, "BOT_USERNAME", "mf_movie_library_bot"
            ), patch.object(main, "MAIN_CHANNEL_ID", ""), patch.object(
                main, "send_chat_action", new=AsyncMock()
            ) as typing, patch.object(
                main, "firebase_ready", return_value=True
            ), patch.object(main, "record_active_user", new=AsyncMock()), patch.object(
                main, "register_user", new=AsyncMock()
            ) as register, patch.object(main, "latest_catalog", new=AsyncMock(return_value=movies)), patch.object(main, "schedule_group_result_keyboard_removal", new=Mock()) as cleanup:
                await main.dispatch_update(update, context)
                typing.assert_awaited_once_with(context.bot, -100987, "typing")
                register.assert_not_awaited()
                cleanup.assert_called_once_with(
                    context.bot, -100987, message.reply_text.return_value.message_id
                )

            first_markup = message.reply_text.await_args.kwargs["reply_markup"]
            self.assertIn("JUST ADDED", message.reply_text.await_args.args[0])
            next_button = first_markup.inline_keyboard[-1][0]
            self.assertEqual(next_button.text, "Next ›")
            self.assertEqual(next_button.style, "success")
            _, token, offset = next_button.callback_data.split(":")
            self.assertEqual(offset, "10")
            self.assertEqual(main.SEARCH_SESSIONS[token]["mode"], "latest")

            query = SimpleNamespace(
                message=SimpleNamespace(chat=SimpleNamespace(type="supergroup"), chat_id=-100987, message_id=888),
                from_user=user,
                data=next_button.callback_data,
                answer=AsyncMock(),
                edit_message_text=AsyncMock(),
            )
            page_update = SimpleNamespace(callback_query=query)
            with patch.object(main, "BOT_TOKEN", "123456:unit-test-token"), patch.object(
                main, "BOT_USERNAME", "mf_movie_library_bot"
            ), patch.object(main, "MAIN_CHANNEL_ID", ""), patch.object(
                main, "send_chat_action", new=AsyncMock()
            ) as page_typing, patch.object(main, "latest_catalog", new=AsyncMock(return_value=movies)), patch.object(main, "schedule_group_result_keyboard_removal", new=Mock()) as page_cleanup:
                await main.search_page_callback(page_update, context)
                page_typing.assert_awaited_once_with(context.bot, -100987, "typing")
                page_cleanup.assert_called_once_with(context.bot, -100987, 888)
            self.assertIn("Page 2 of 2", query.edit_message_text.await_args.args[0])

        asyncio.run(run())

    def test_group_result_keyboard_is_removed_after_five_minutes(self):
        bot = SimpleNamespace(edit_message_reply_markup=AsyncMock())

        async def run():
            with patch.object(main, "GROUP_KEYBOARD_TASKS", {}), patch.object(
                main.asyncio, "sleep", new=AsyncMock()
            ) as wait:
                main.schedule_group_result_keyboard_removal(bot, -100123, 456)
                task = main.GROUP_KEYBOARD_TASKS["-100123_456"]
                await task
                wait.assert_awaited_once_with(300)
            bot.edit_message_reply_markup.assert_awaited_once_with(
                chat_id=-100123,
                message_id=456,
                reply_markup=None,
            )

        asyncio.run(run())

    def test_group_no_results_offers_signed_private_request_link(self):
        message = SimpleNamespace(
            chat=SimpleNamespace(type="supergroup"),
            chat_id=-100123,
            reply_text=AsyncMock(),
        )
        user = SimpleNamespace(id=77)
        update = SimpleNamespace(effective_message=message, effective_user=user)
        context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))

        async def run():
            with patch.object(main, "BOT_TOKEN", "123456:unit-test-token"), patch.object(
                main, "BOT_USERNAME", "mf_movie_library_bot"
            ), patch.object(main, "MAIN_CHANNEL_ID", ""), patch.object(
                main, "send_chat_action", new=AsyncMock()
            ), patch.object(main, "schedule_group_result_keyboard_removal", new=Mock()) as cleanup, patch.object(
                main, "firebase_ready", return_value=True
            ), patch.object(main, "record_active_user", new=AsyncMock()), patch.object(
                main, "record_search", new=AsyncMock()
            ), patch.object(main, "search_catalog", new=AsyncMock(return_value=[])):
                await main.search_command(update, context, "Moonlight 2025")
                cleanup.assert_called_once_with(
                    context.bot, message.chat_id, message.reply_text.return_value.message_id
                )
            button = message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
            self.assertEqual(button.text, "Request this movie")
            self.assertTrue(button.url.startswith("https://t.me/mf_movie_library_bot?start=r"))
            payload = button.url.split("?start=", 1)[1]
            with patch.object(main, "BOT_TOKEN", "123456:unit-test-token"):
                token = main.movie_request_token_from_start_payload(payload, 77)
                wrong_user_token = main.movie_request_token_from_start_payload(payload, 78)
            self.assertIsNotNone(token)
            self.assertEqual(main.MOVIE_REQUEST_SESSIONS[token]["title"], "Moonlight 2025")
            self.assertIsNone(wrong_user_token)

        asyncio.run(run())

    def test_movie_request_link_parser_accepts_underscores_inside_signature(self):
        payload = "r25_abc123_abcde_fghij"
        with patch.object(main, "BOT_TOKEN", "123456:unit-test-token"), patch.object(
            main,
            "MOVIE_REQUEST_SESSIONS",
            {"abc123": {"user_id": 77, "created_at": time.time()}},
        ), patch.object(main, "make_file_payload_signature", return_value="abcde_fghij"):
            self.assertEqual(main.movie_request_token_from_start_payload(payload, 77), "abc123")

    def test_group_request_start_persists_request_for_the_button_user(self):
        tree = {}
        user = SimpleNamespace(id=77)
        message = SimpleNamespace(chat=SimpleNamespace(type="private"), reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message, effective_user=user)
        context = SimpleNamespace(bot=SimpleNamespace())

        async def run():
            with patch.object(main, "BOT_TOKEN", "123456:unit-test-token"), patch.object(
                main, "BOT_USERNAME", "mf_movie_library_bot"
            ), patch.object(main, "ADMIN_ID", 0
            ), patch.object(main, "register_user", new=AsyncMock()), patch.object(
                main, "MAIN_CHANNEL_ID", ""
            ), patch.object(
                main, "firebase_ready", return_value=True
            ), patch.object(
                main,
                "firebase_reference",
                side_effect=lambda path: FakeTreeReference(tree, path),
            ):
                button = main.no_results_keyboard(77, "Moonlight 2025", group=True).inline_keyboard[0][0]
                payload = button.url.split("?start=", 1)[1]
                await main.start_command(update, context, payload)

            self.assertIn("REQUEST RECEIVED", message.reply_text.await_args.args[0])
            request = next(iter(tree["movie_requests"].values()))
            self.assertEqual(request["title"], "Moonlight 2025")
            self.assertEqual(request["requester_count"], 1)
            self.assertIn("77", request["requesters"])

        asyncio.run(run())

    def test_signed_group_start_delivers_file_with_attached_caption_and_expires_file(self):
        bot = SimpleNamespace(
            send_chat_action=AsyncMock(),
            copy_message=AsyncMock(return_value=SimpleNamespace(message_id=501)),
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=502)),
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(type="private"),
            reply_text=AsyncMock(),
        )
        update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=77))
        context = SimpleNamespace(bot=bot)

        async def run():
            with patch.object(main, "BOT_TOKEN", "123456:unit-test-token"), patch.object(
                main, "register_user", new=AsyncMock()
            ), patch.object(main, "MAIN_CHANNEL_ID", ""), patch.object(
                main, "firebase_ready", return_value=True
            ), patch.object(
                main,
                "get_catalog_entry",
                new=AsyncMock(return_value={
                    "media_type": "video",
                    "source_chat_id": -1001,
                    "source_message_id": 9,
                    "file_name": "Movie.mkv",
                }),
            ), patch.object(main, "persist_pending_delete", new=AsyncMock()) as persist, patch.object(
                main, "schedule_file_deletion"
            ) as schedule:
                payload = main.make_file_start_payload("-1001_9")
                await main.start_command(update, context, payload)
                self.assertEqual(persist.await_count, 1)
                self.assertEqual(schedule.call_count, 1)

        asyncio.run(run())
        bot.copy_message.assert_awaited_once()
        copy_args = bot.copy_message.await_args.kwargs
        self.assertEqual(
            {key: copy_args[key] for key in ("chat_id", "from_chat_id", "message_id")},
            {"chat_id": 77, "from_chat_id": -1001, "message_id": 9},
        )
        self.assertEqual(
            [button.url for button in copy_args["reply_markup"].inline_keyboard[0]],
            ["https://t.me/mfmainchannel", "https://t.me/MFMovieandSeriesRequestGroup"],
        )
        self.assertIn("Main channel: @mfmainchannel", copy_args["caption"])
        self.assertIn("Bot: @mf_movie_bot", copy_args["caption"])
        self.assertIn('Use "VLC MEDIA PLAYER" app to avoid issues while opening files!', copy_args["caption"])
        self.assertIn("Auto-deletes in 10 minutes.", copy_args["caption"])
        bot.send_message.assert_not_awaited()

    def test_request_keyboard_has_callback_and_green_style(self):
        previous = dict(main.MOVIE_REQUEST_SESSIONS)
        try:
            keyboard = main.no_results_keyboard(44, "Interstellar 2024")
            button = keyboard.inline_keyboard[0][0]
            self.assertEqual(button.text, "Request this movie")
            self.assertTrue(button.callback_data.startswith("movie_request:"))
            self.assertEqual(button.style, "success")
        finally:
            main.MOVIE_REQUEST_SESSIONS.clear()
            main.MOVIE_REQUEST_SESSIONS.update(previous)

    def test_admin_request_page_has_an_individual_green_button_per_movie(self):
        pending = [
            (f"{i:024x}", {"title": f"Requested movie {i}", "requester_count": i})
            for i in range(1, 22)
        ]
        text, keyboard = main.requests_page_content(pending, 0)
        self.assertIn("21 open", text)
        self.assertEqual(len(keyboard.inline_keyboard), 21)
        self.assertEqual(keyboard.inline_keyboard[0][0].style, "success")
        self.assertTrue(keyboard.inline_keyboard[0][0].callback_data.startswith("request_done:"))
        self.assertEqual(keyboard.inline_keyboard[-1][0].callback_data, "request_page:20")

    def test_send_action_matches_media_type(self):
        self.assertEqual(main.upload_action_for_media("video"), "upload_video")
        self.assertEqual(main.upload_action_for_media("photo"), "upload_photo")
        self.assertEqual(main.upload_action_for_media("document"), "upload_document")

    def test_permanently_missing_expiry_message_clears_pending_retry_record(self):
        bot = SimpleNamespace(delete_message=AsyncMock(side_effect=main.BadRequest("message to delete not found")))

        async def run():
            with patch.object(main, "BOT_APP", SimpleNamespace(bot=bot)), patch.object(
                main, "clear_pending_delete", new=AsyncMock()
            ) as clear, patch.object(main.asyncio, "sleep", new=AsyncMock()):
                await main.delete_delivered_file("77_501", 77, 501, int(time.time()) - 1)
                bot.delete_message.assert_awaited_once_with(chat_id=77, message_id=501)
                clear.assert_awaited_once_with("77_501")

        asyncio.run(run())

    def test_successful_file_selection_sends_then_removes_search_results_and_blocks_duplicate(self):
        events = []

        async def copy_file(**kwargs):
            events.append("file")
            return SimpleNamespace(message_id=88)

        async def delete_results(**kwargs):
            events.append("results")
            return True

        bot = SimpleNamespace(
            send_chat_action=AsyncMock(),
            copy_message=AsyncMock(side_effect=copy_file),
            delete_message=AsyncMock(side_effect=delete_results),
            edit_message_reply_markup=AsyncMock(),
            send_message=AsyncMock(),
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(type="private"),
            chat_id=77,
            message_id=33,
        )
        query = SimpleNamespace(
            message=message,
            from_user=SimpleNamespace(id=77),
            data="file:-1001_7",
            answer=AsyncMock(),
        )
        update = SimpleNamespace(callback_query=query)
        context = SimpleNamespace(bot=bot)

        async def run():
            with patch.object(main, "ACTIVE_FILE_MESSAGES", set()), patch.object(
                main, "CONSUMED_FILE_MESSAGES", {}
            ), patch.object(main, "register_user", new=AsyncMock()), patch.object(
                main, "MAIN_CHANNEL_ID", ""
            ), patch.object(main, "firebase_ready", return_value=True), patch.object(
                main,
                "get_catalog_entry",
                new=AsyncMock(
                    return_value={
                        "media_type": "video",
                        "source_chat_id": -1001,
                        "source_message_id": 7,
                        "file_name": "Movie.mkv",
                        "caption": "Original caption",
                        "caption_entities": [],
                    }
                ),
            ), patch.object(main, "persist_pending_delete", new=AsyncMock()) as persist, patch.object(
                main, "schedule_file_deletion"
            ) as schedule:
                await main.file_button_callback(update, context)
                await main.file_button_callback(update, context)
                self.assertEqual(persist.await_count, 1)
                self.assertEqual(schedule.call_count, 1)

        asyncio.run(run())
        self.assertEqual(events, ["file", "results"])
        bot.copy_message.assert_awaited_once()
        copy_args = bot.copy_message.await_args.kwargs
        self.assertEqual(
            {key: copy_args[key] for key in ("chat_id", "from_chat_id", "message_id")},
            {"chat_id": 77, "from_chat_id": -1001, "message_id": 7},
        )
        self.assertTrue(copy_args["caption"].startswith("Original caption\n\n"))
        self.assertIn("Main channel: @mfmainchannel", copy_args["caption"])
        self.assertIn("Bot: @mf_movie_bot", copy_args["caption"])
        self.assertIn('Use "VLC MEDIA PLAYER" app to avoid issues while opening files!', copy_args["caption"])
        self.assertIn("Auto-deletes in 10 minutes.", copy_args["caption"])
        self.assertEqual(copy_args["reply_markup"].inline_keyboard[0][0].style, "danger")
        self.assertEqual(copy_args["reply_markup"].inline_keyboard[0][1].style, "success")
        bot.send_message.assert_not_awaited()
        bot.delete_message.assert_awaited_once_with(chat_id=77, message_id=33)
        self.assertEqual(query.answer.await_count, 2)
        self.assertIn("already used", query.answer.await_args.args[0])

    def test_search_counts_and_top_searches_are_persisted_in_firebase_paths(self):
        refs = {}

        def get_ref(path):
            refs.setdefault(path, FakeReference())
            return refs[path]

        async def run():
            with patch.object(main, "firebase_reference", side_effect=get_ref):
                await main.record_search("The Matrix 1999")
                await main.record_search("the matrix 1999")

        asyncio.run(run())
        self.assertEqual(refs["stats/total_searches"].value, 2)
        for path in ("analytics/top_searches/", "stats/top_searches/"):
            top = next(value.value for key, value in refs.items() if key.startswith(path))
            self.assertEqual(top["count"], 2)
            self.assertEqual(top["query"], "the matrix 1999")

    def test_file_received_counter_is_incremented(self):
        reference = FakeReference(4)

        async def run():
            with patch.object(main, "firebase_reference", return_value=reference):
                await main.record_file_received()

        asyncio.run(run())
        self.assertEqual(reference.value, 5)

    def test_active_user_tracking_deduplicates_group_users_without_private_registration(self):
        refs = {}

        def get_ref(path):
            refs.setdefault(path, FakeReference())
            return refs[path]

        async def run():
            with patch.object(main, "firebase_ready", return_value=True), patch.object(
                main, "firebase_reference", side_effect=get_ref
            ):
                user = SimpleNamespace(id=77)
                await main.record_active_user(user)
                first_seen = refs["analytics/active_users/77"].value["first_seen_at"]
                await main.record_active_user(user)
                self.assertEqual(refs["analytics/active_users/77"].value["first_seen_at"], first_seen)

        asyncio.run(run())
        self.assertNotIn("users/77", refs)

    def test_user_registration_counts_unique_users_and_stores_minimal_fields(self):
        refs = {"stats/total_users": FakeReference(0)}

        def get_ref(path):
            refs.setdefault(path, FakeReference())
            return refs[path]

        async def run():
            with patch.object(main, "firebase_ready", return_value=True), patch.object(
                main, "firebase_reference", side_effect=get_ref
            ):
                user = SimpleNamespace(id=77, username="not-needed", first_name="Not needed")
                await main.register_user(user)
                await main.register_user(user)

        asyncio.run(run())
        self.assertEqual(refs["stats/total_users"].value, 1)
        self.assertEqual(set(refs["users/77"].value), {"user_id", "chat_id", "first_seen_at", "last_seen_at"})

    def test_status_repairs_stale_totals_from_firebase_records(self):
        tree = {
            "files": {"file-a": {}, "file-b": {}, "file-c": {}},
            "users": {"1": {}, "2": {}},
            "stats": {
                "total_users": 0,
                "total_files": 0,
                "total_searches": 0,
                "total_file_received": 12,
                "top_searches": {
                    "one": {"query": "Alpha", "count": 4},
                    "two": {"query": "Beta", "count": 2},
                },
            },
            "analytics": {
                "active_users": {"2": {}, "3": {}},
                "top_searches": {
                    "matrix": {"query": "The Matrix", "normalized_query": "the matrix", "count": 5}
                },
            },
        }

        async def run():
            with patch.object(main, "firebase_reference", side_effect=lambda path: FakeTreeReference(tree, path)):
                return await main.get_status_stats()

        stats = asyncio.run(run())
        self.assertEqual(stats["total_users"], 3)
        self.assertEqual(stats["total_files"], 3)
        self.assertEqual(stats["total_file_received"], 12)
        self.assertEqual(stats["top_searches"][0]["query"], "The Matrix")
        self.assertEqual(stats["total_searches"], 11)
        self.assertEqual(tree["stats"]["total_users"], 3)
        self.assertEqual(tree["stats"]["total_files"], 3)
        self.assertEqual(tree["stats"]["total_file_received"], 12)
        self.assertEqual(tree["stats"]["total_searches"], 11)

    def test_health_endpoint_reports_unready_as_503_and_ready_as_200_payload(self):
        async def run():
            with patch.object(main, "BOT_TOKEN", ""), patch.object(main, "BOT_APP", None), patch.object(
                main, "FIREBASE_DATABASE_URL", ""
            ), patch.object(main, "FIREBASE_SERVICE_ACCOUNT_JSON", ""), patch.object(
                main, "TELEGRAM_SOURCE_CHATS", ""
            ), patch.object(main, "SOURCE_CHANNEL_IDS", set()), patch.object(
                main, "MAIN_CHANNEL_ID", ""
            ), patch.object(main, "FORCE_JOIN_CHANNEL_ID", ""), patch.object(
                main, "firebase_ready", return_value=False
            ):
                with self.assertRaises(main.HTTPException) as error:
                    await main.healthz()
                self.assertEqual(error.exception.status_code, 503)

            with patch.object(main, "BOT_TOKEN", "test-token"), patch.object(main, "BOT_APP", object()), patch.object(
                main, "FIREBASE_DATABASE_URL", "https://example.invalid"
            ), patch.object(main, "FIREBASE_SERVICE_ACCOUNT_JSON", "{}"), patch.object(
                main, "TELEGRAM_SOURCE_CHATS", "-1001"
            ), patch.object(main, "SOURCE_CHANNEL_IDS", {-1001}), patch.object(
                main, "MAIN_CHANNEL_ID", ""
            ), patch.object(main, "FORCE_JOIN_CHANNEL_ID", "-1004352245988"), patch.object(
                main, "firebase_ready", return_value=True
            ):
                status = await main.healthz()
                self.assertTrue(status["ok"])
                self.assertFalse(status["main_channel_configured"])
                self.assertTrue(status["force_join_configured"])
        asyncio.run(run())

    def test_confirmed_broadcast_persists_job_and_schedules_background_worker(self):
        async def run():
            old_admin = main.ADMIN_ID
            old_pending = dict(main.PENDING_BROADCASTS)
            token = "broadcasttest123"
            try:
                main.ADMIN_ID = 999
                main.PENDING_BROADCASTS[token] = {
                    "admin_id": 999,
                    "payload": {"kind": "text", "text": "Hello archive users"},
                    "created_at": time.time(),
                }
                job_ref = FakeReference()
                query = SimpleNamespace(
                    message=SimpleNamespace(message_id=123),
                    from_user=SimpleNamespace(id=999),
                    answer=AsyncMock(),
                    edit_message_text=AsyncMock(),
                )
                update = SimpleNamespace(callback_query=query)
                app_context = object()
                context = SimpleNamespace(application=app_context)

                def get_ref(path):
                    if path == "users":
                        return FakeReference({"100": {"chat_id": 100}})
                    if path == f"broadcast_jobs/{token}":
                        return job_ref
                    raise AssertionError(f"Unexpected Firebase path: {path}")

                with patch.object(main, "firebase_ready", return_value=True), patch.object(
                    main, "firebase_reference", side_effect=get_ref
                ), patch.object(main, "start_background_broadcast", return_value=True) as schedule:
                    await main.send_broadcast(update, context, token, cancel=False)
                schedule.assert_called_once_with(token, app_context)
                self.assertEqual(job_ref.value["status"], "running")
                self.assertEqual(job_ref.value["recipient_ids"], {"100": True})
                self.assertEqual(job_ref.value["admin_message_id"], 123)
                self.assertNotIn(token, main.PENDING_BROADCASTS)
            finally:
                main.ADMIN_ID = old_admin
                main.PENDING_BROADCASTS.clear()
                main.PENDING_BROADCASTS.update(old_pending)
        asyncio.run(run())

    def test_broadcast_worker_skips_persisted_success_and_finishes_job(self):
        tree = {
            "broadcast_jobs": {
                "job123": {
                    "admin_id": 999,
                    "admin_message_id": 50,
                    "payload": {"kind": "text", "text": "Hello"},
                    "recipient_ids": {"100": True, "101": True},
                    "sent_to": {"100": True},
                    "failed_to": {},
                    "status": "running",
                }
            }
        }
        bot = SimpleNamespace(
            send_message=AsyncMock(),
            copy_message=AsyncMock(),
            edit_message_text=AsyncMock(),
        )

        async def run():
            with patch.object(main, "BOT_APP", SimpleNamespace(bot=bot)), patch.object(
                main, "firebase_ready", return_value=True
            ), patch.object(main, "firebase_reference", side_effect=lambda path: FakeTreeReference(tree, path)):
                await main.run_broadcast_job("job123")

        asyncio.run(run())
        job = tree["broadcast_jobs"]["job123"]
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["sent_count"], 2)
        self.assertEqual(bot.send_message.await_count, 1)
        bot.send_message.assert_awaited_once_with(chat_id=101, text="Hello")
        bot.edit_message_text.assert_awaited_once()

    def test_broadcast_preview_shows_exact_text_and_excludes_admin(self):
        async def run():
            old_admin = main.ADMIN_ID
            old_pending = dict(main.PENDING_BROADCASTS)
            try:
                main.ADMIN_ID = 999
                message = SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    reply_to_message=None,
                    reply_text=AsyncMock(),
                )
                update = SimpleNamespace(
                    effective_message=message,
                    effective_user=SimpleNamespace(id=999),
                )
                users = {
                    "100": {"chat_id": 100},
                    "999": {"chat_id": 999},
                }
                with patch.object(main, "firebase_ready", return_value=True), patch.object(
                    main, "firebase_reference", return_value=FakeReference(users)
                ):
                    await main.prepare_broadcast(update, SimpleNamespace(), "Hello & <everyone>")
                preview = message.reply_text.await_args.args[0]
                self.assertIn("Recipients: 1 registered users", preview)
                self.assertIn("Hello & <everyone>", preview)
                buttons = message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard[0]
                self.assertEqual([button.style for button in buttons], ["success", "success"])
            finally:
                main.ADMIN_ID = old_admin
                main.PENDING_BROADCASTS.clear()
                main.PENDING_BROADCASTS.update(old_pending)
        asyncio.run(run())

    def test_broadcast_recipient_ids_are_unique_and_exclude_admin(self):
        old_admin = main.ADMIN_ID
        try:
            main.ADMIN_ID = 2
            users = {
                "1": {"chat_id": 1},
                "2": {"chat_id": 2},
                "duplicate": {"chat_id": 1},
                "bad": {"chat_id": "not-an-id"},
            }
            self.assertEqual(main.registered_chat_ids(users), [1])
        finally:
            main.ADMIN_ID = old_admin

    def test_plain_movie_message_routes_to_search(self):
        async def run():
            message = SimpleNamespace(text="Interstellar 2014")
            update = SimpleNamespace(
                channel_post=None,
                callback_query=None,
                effective_message=message,
                effective_user=SimpleNamespace(id=42),
            )
            with patch.object(main, "search_command", new=AsyncMock()) as search:
                await main.dispatch_update(update, SimpleNamespace())
            search.assert_awaited_once_with(update, unittest.mock.ANY, "Interstellar 2014")
        asyncio.run(run())

    def test_stuts_alias_routes_to_admin_status(self):
        async def run():
            message = SimpleNamespace(text="/stuts", caption=None)
            update = SimpleNamespace(
                channel_post=None,
                callback_query=None,
                effective_message=message,
                effective_user=SimpleNamespace(id=42),
            )
            old_admin = main.ADMIN_ID
            try:
                main.ADMIN_ID = 42
                with patch.object(main, "status_command", new=AsyncMock()) as status:
                    await main.dispatch_update(update, SimpleNamespace())
                status.assert_awaited_once()
            finally:
                main.ADMIN_ID = old_admin
        asyncio.run(run())

    def test_forwarding_is_scoped_to_the_addchannel_workflow(self):
        source = Path(main.__file__).read_text(encoding="utf-8").lower()
        self.assertIn("forward_message", source)
        self.assertIn('command == "/addchannel"', source)
        self.assertNotIn("/shareall", source)
        self.assertNotIn("approved_destination", source)


if __name__ == "__main__":
    unittest.main()
