import asyncio
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import main


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
    def test_search_normalization_is_case_insensitive_and_unicode_safe(self):
        self.assertEqual(main.normalize_search_text("The.Matrix_1999.MKV"), "the matrix 1999 mkv")
        self.assertEqual(main.normalize_search_text("Cafe\u0301 2025"), "cafe\u0301 2025")

    def test_media_details_preserve_filename_and_size(self):
        message = SimpleNamespace(
            document=SimpleNamespace(file_name="Movie.Name.mkv", file_size=987654321),
            video=None, audio=None, animation=None, video_note=None,
            voice=None, photo=None, caption="Movie Name", message_id=12,
        )
        details = main.extract_media_details(message)
        self.assertEqual(details["file_name"], "Movie.Name.mkv")
        self.assertEqual(details["file_size"], 987654321)
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

    def test_file_size_formatting(self):
        self.assertEqual(main.format_file_size(0), "Size unknown")
        self.assertEqual(main.format_file_size(1024 * 1024), "1.0 MB")

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
            main, "BOT_USERNAME", "mf_movie_library_bot"
        ):
            text = main.new_file_announcement_text(record)
            keyboard = main.new_file_announcement_keyboard("-1004443404016_88")
            button = keyboard.inline_keyboard[0][0]
            payload = button.url.split("?start=", 1)[1]
            self.assertIn("Movie &lt;HD&gt;.mkv", text)
            self.assertIn("1.0 MB", text)
            self.assertEqual(button.text, "Get File")
            self.assertEqual(main.file_key_from_start_payload(payload), "-1004443404016_88")

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
                main, "FORCE_JOIN_CHANNEL_ID", "-1004352245988"
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
            bot.send_message.reset_mock()
            with patch.object(main, "SOURCE_CHANNEL_IDS", {-1004443404016, -1004443404020}), patch.object(
                main, "MOVIE_ANNOUNCEMENT_CHANNELS", ["-1003391211397"]
            ), patch.object(
                main, "FORCE_JOIN_CHANNEL_ID", "-1004352245988"
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

    def test_archive_mirror_candidates_only_include_unmirrored_files_from_db_source(self):
        catalog = {
            "-1004443404016_20": {
                "source_chat_id": -1004443404016,
                "source_message_id": 20,
                "file_name": "Later.mkv",
            },
            "-1004443404016_10": {
                "source_chat_id": -1004443404016,
                "source_message_id": 10,
                "file_name": "Earlier.mkv",
            },
            "-1004443404016_5": {
                "source_chat_id": -1004443404016,
                "source_message_id": 5,
                "archive_mirror_target_chat_id": "-1003942606485",
                "archive_mirror_target_message_id": 300,
            },
            "-1004443404020_8": {
                "source_chat_id": -1004443404020,
                "source_message_id": 8,
                "file_name": "Other channel.mkv",
            },
        }

        async def run():
            with patch.object(main, "ARCHIVE_MIRROR_SOURCE_CHANNEL_ID", "-1004443404016"), patch.object(
                main, "ARCHIVE_MIRROR_CHANNEL_ID", "-1003942606485"
            ), patch.object(main, "get_catalog", new=AsyncMock(return_value=catalog)):
                self.assertEqual(
                    await main.archive_mirror_candidate_keys(),
                    ["-1004443404016_10", "-1004443404016_20"],
                )

        asyncio.run(run())

    def test_new_db_upload_is_auto_mirrored_once_and_persists_receipt(self):
        chat = SimpleNamespace(id=-1004443404016, title="DB archive", username=None)
        post = SimpleNamespace(chat=chat, message_id=88)
        update = SimpleNamespace(channel_post=post)
        copied_message = SimpleNamespace(message_id=900)
        bot = SimpleNamespace(copy_message=AsyncMock(return_value=copied_message))
        application = SimpleNamespace(bot=bot)
        details = {"file_name": "Movie.mkv", "file_size": 1024, "media_type": "document"}
        record_store = {
            **details,
            "source_chat_id": -1004443404016,
            "source_message_id": 88,
        }

        async def get_entry(key):
            return dict(record_store)

        async def save_entry(key, record):
            record_store.update(record)

        async def run():
            with patch.object(main, "SOURCE_CHANNEL_IDS", {-1004443404016}), patch.object(
                main, "ARCHIVE_MIRROR_SOURCE_CHANNEL_ID", "-1004443404016"
            ), patch.object(main, "ARCHIVE_MIRROR_CHANNEL_ID", "-1003942606485"), patch.object(
                main, "ARCHIVE_MIRROR_LOCKS", {}
            ), patch.object(main, "ARCHIVE_MIRROR_SEND_LOCK", asyncio.Lock()), patch.object(
                main, "ARCHIVE_MIRROR_LAST_SENT_AT", 0.0
            ), patch.object(main, "BOT_APP", application), patch.object(
                main, "BOT_USERNAME", "mf_movie_library_bot"
            ), patch.object(main, "firebase_ready", return_value=True), patch.object(
                main, "extract_media_details", return_value=details
            ), patch.object(main, "save_new_media_post", new=AsyncMock(side_effect=[True, False])), patch.object(
                main, "get_catalog_entry", new=AsyncMock(side_effect=get_entry)
            ), patch.object(main, "save_catalog_entry", new=AsyncMock(side_effect=save_entry)), patch.object(
                main, "movie_announcement_destinations", return_value=[]
            ):
                await main.index_channel_post(update)
                await main.index_channel_post(update)

        asyncio.run(run())
        bot.copy_message.assert_awaited_once_with(
            chat_id=-1003942606485,
            from_chat_id=-1004443404016,
            message_id=88,
        )
        self.assertEqual(record_store["archive_mirror_target_chat_id"], "-1003942606485")
        self.assertEqual(record_store["archive_mirror_target_message_id"], 900)

    def test_archive_mirror_waits_for_three_seconds_between_sends(self):
        record = {"source_chat_id": -1004443404016, "source_message_id": 88}
        bot = SimpleNamespace(copy_message=AsyncMock(return_value=SimpleNamespace(message_id=900)))

        async def run():
            with patch.object(main, "ARCHIVE_MIRROR_SOURCE_CHANNEL_ID", "-1004443404016"), patch.object(
                main, "ARCHIVE_MIRROR_CHANNEL_ID", "-1003942606485"
            ), patch.object(main, "ARCHIVE_MIRROR_LOCKS", {}), patch.object(
                main, "ARCHIVE_MIRROR_SEND_LOCK", asyncio.Lock()
            ), patch.object(main, "ARCHIVE_MIRROR_DELAY_SECONDS", 3.0), patch.object(
                main, "ARCHIVE_MIRROR_LAST_SENT_AT", time.monotonic() - 1.0
            ), patch.object(main, "get_catalog_entry", new=AsyncMock(return_value=record)), patch.object(
                main, "save_catalog_entry", new=AsyncMock()
            ), patch.object(main.asyncio, "sleep", new=AsyncMock()) as sleep:
                result = await main.archive_mirror_catalog_item("-1004443404016_88", record, bot=bot)
                self.assertEqual(result, "copied")
                delay = sleep.await_args.args[0]
                self.assertGreater(delay, 1.9)
                self.assertLessEqual(delay, 2.0)

        asyncio.run(run())

    def test_admin_archive_mirror_preview_requires_confirmation_and_persists_job(self):
        tree = {}
        user = SimpleNamespace(id=77)
        message = SimpleNamespace(chat=SimpleNamespace(type="private"), reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message, effective_user=user)
        context = SimpleNamespace(application=SimpleNamespace())

        async def run():
            with patch.object(main, "ADMIN_ID", 77), patch.object(
                main, "ARCHIVE_MIRROR_SOURCE_CHANNEL_ID", "-1004443404016"
            ), patch.object(main, "ARCHIVE_MIRROR_CHANNEL_ID", "-1003942606485"), patch.object(
                main, "PENDING_ARCHIVE_MIRRORS", {}
            ), patch.object(main, "new_session_token", return_value="mirror-token"), patch.object(
                main, "archive_mirror_candidate_keys", new=AsyncMock(return_value=["-1004443404016_10", "-1004443404016_20"])
            ), patch.object(main, "firebase_ready", return_value=True):
                await main.prepare_archive_mirror_history(update, context)
                preview = message.reply_text.await_args.args[0]
                self.assertIn("Files to copy: <b>2</b>", preview)
                self.assertIn("3 seconds per file", preview)
                button = message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
                self.assertEqual(button.text, "Confirm mirror")
                self.assertTrue(button.callback_data.startswith("archive_mirror_confirm:"))

                query = SimpleNamespace(
                    from_user=user,
                    message=SimpleNamespace(chat=SimpleNamespace(type="private"), message_id=456),
                    answer=AsyncMock(),
                    edit_message_text=AsyncMock(),
                )
                callback_update = SimpleNamespace(callback_query=query)
                app_context = SimpleNamespace(application=SimpleNamespace())
                with patch.object(main, "firebase_ready", return_value=True), patch.object(
                    main, "firebase_reference", side_effect=lambda path: FakeTreeReference(tree, path)
                ), patch.object(main, "start_archive_mirror_job", return_value=True) as start_job:
                    await main.handle_archive_mirror_confirmation(
                        callback_update, app_context, "mirror-token", cancel=False
                    )
                    start_job.assert_called_once_with("mirror-token", app_context.application)

            job = tree["archive_mirror_jobs"]["mirror-token"]
            self.assertEqual(job["status"], "running")
            self.assertEqual(len(job["item_keys"]), 2)
            self.assertEqual(job["target_channel_id"], "-1003942606485")

        asyncio.run(run())

    def test_archive_mirror_status_shows_failed_item_reason(self):
        tree = {
            "archive_mirror_jobs": {
                "mirror-job": {
                    "status": "completed_with_errors",
                    "created_at": "2026-10-12T02:00:00+00:00",
                    "item_keys": {"-1004443404016_2150": True},
                    "completed_items": {},
                    "failed_items": {
                        "-1004443404016_2150": "BadRequest: Message to copy not found",
                    },
                    "copied_count": 0,
                    "completed_count": 0,
                    "failed_count": 1,
                }
            }
        }
        message = SimpleNamespace(chat=SimpleNamespace(type="private"), reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(id=77),
        )
        context = SimpleNamespace()

        async def run():
            with patch.object(main, "ADMIN_ID", 77), patch.object(
                main, "firebase_reference", side_effect=lambda path: FakeTreeReference(tree, path)
            ):
                await main.archive_mirror_status_command(update, context)

        asyncio.run(run())
        status_text = message.reply_text.await_args.args[0]
        self.assertIn("Failure details", status_text)
        self.assertIn("-1004443404016_2150", status_text)
        self.assertIn("BadRequest: Message to copy not found", status_text)

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
                main, "FORCE_JOIN_CHANNEL_ID", "-1004352245988"
            ), patch.object(main, "send_chat_action", new=AsyncMock()) as chat_action, patch.object(
                main, "schedule_group_result_keyboard_removal", new=Mock()
            ) as cleanup, patch.object(
                main, "register_user", new=AsyncMock()
            ) as register, patch.object(main, "is_user_member", new=AsyncMock(return_value=True)), patch.object(
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
            ), patch.object(main, "FORCE_JOIN_CHANNEL_ID", "-1002"), patch.object(
                main, "send_chat_action", new=AsyncMock()
            ) as typing, patch.object(main, "is_user_member", new=AsyncMock(return_value=True)), patch.object(
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
            ), patch.object(main, "is_user_member", new=AsyncMock(return_value=True)), patch.object(
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
            ), patch.object(main, "FORCE_JOIN_CHANNEL_ID", "-1004352245988"), patch.object(
                main, "send_chat_action", new=AsyncMock()
            ), patch.object(main, "schedule_group_result_keyboard_removal", new=Mock()) as cleanup, patch.object(
                main, "is_user_member", new=AsyncMock(return_value=True)), patch.object(
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
                main, "FORCE_JOIN_CHANNEL_ID", "-1004352245988"
            ), patch.object(main, "is_user_member", new=AsyncMock(return_value=True)), patch.object(
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

    def test_signed_group_start_delivers_file_and_expires_file_and_notice(self):
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
            ), patch.object(main, "FORCE_JOIN_CHANNEL_ID", "-1002"), patch.object(
                main, "is_user_member", new=AsyncMock(return_value=True)
            ), patch.object(main, "firebase_ready", return_value=True), patch.object(
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
                self.assertEqual(persist.await_count, 2)
                self.assertEqual(schedule.call_count, 2)
                self.assertEqual(schedule.call_args_list[0].args[-1], schedule.call_args_list[1].args[-1])

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
        self.assertIn("removed automatically", bot.send_message.await_args.kwargs["text"])

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

        async def send_notice(**kwargs):
            events.append("notice")
            return SimpleNamespace(message_id=89)

        bot = SimpleNamespace(
            send_chat_action=AsyncMock(),
            copy_message=AsyncMock(side_effect=copy_file),
            delete_message=AsyncMock(side_effect=delete_results),
            edit_message_reply_markup=AsyncMock(),
            send_message=AsyncMock(side_effect=send_notice),
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
                main, "FORCE_JOIN_CHANNEL_ID", "-1002"
            ), patch.object(main, "is_user_member", new=AsyncMock(return_value=True)
            ), patch.object(main, "firebase_ready", return_value=True), patch.object(
                main,
                "get_catalog_entry",
                new=AsyncMock(
                    return_value={
                        "media_type": "video",
                        "source_chat_id": -1001,
                        "source_message_id": 7,
                        "file_name": "Movie.mkv",
                    }
                ),
            ), patch.object(main, "persist_pending_delete", new=AsyncMock()) as persist, patch.object(
                main, "schedule_file_deletion"
            ) as schedule:
                await main.file_button_callback(update, context)
                await main.file_button_callback(update, context)
                self.assertEqual(persist.await_count, 2)
                self.assertEqual(schedule.call_count, 2)
                self.assertEqual(schedule.call_args_list[0].args[-1], schedule.call_args_list[1].args[-1])

        asyncio.run(run())
        self.assertEqual(events, ["file", "results", "notice"])
        bot.copy_message.assert_awaited_once()
        copy_args = bot.copy_message.await_args.kwargs
        self.assertEqual(
            {key: copy_args[key] for key in ("chat_id", "from_chat_id", "message_id")},
            {"chat_id": 77, "from_chat_id": -1001, "message_id": 7},
        )
        self.assertEqual(copy_args["reply_markup"].inline_keyboard[0][0].style, "danger")
        self.assertEqual(copy_args["reply_markup"].inline_keyboard[0][1].style, "success")
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
        self.assertEqual(stats["top_searches"][0]["query"], "The Matrix")
        self.assertEqual(stats["total_searches"], 11)
        self.assertEqual(tree["stats"]["total_users"], 3)
        self.assertEqual(tree["stats"]["total_files"], 3)
        self.assertEqual(tree["stats"]["total_searches"], 11)

    def test_health_endpoint_reports_unready_as_503_and_ready_as_200_payload(self):
        async def run():
            with patch.object(main, "BOT_TOKEN", ""), patch.object(main, "BOT_APP", None), patch.object(
                main, "FIREBASE_DATABASE_URL", ""
            ), patch.object(main, "FIREBASE_SERVICE_ACCOUNT_JSON", ""), patch.object(
                main, "TELEGRAM_SOURCE_CHATS", ""
            ), patch.object(main, "SOURCE_CHANNEL_IDS", set()), patch.object(
                main, "FORCE_JOIN_CHANNEL_ID", ""
            ), patch.object(main, "firebase_ready", return_value=False):
                with self.assertRaises(main.HTTPException) as error:
                    await main.healthz()
                self.assertEqual(error.exception.status_code, 503)

            with patch.object(main, "BOT_TOKEN", "test-token"), patch.object(main, "BOT_APP", object()), patch.object(
                main, "FIREBASE_DATABASE_URL", "https://example.invalid"
            ), patch.object(main, "FIREBASE_SERVICE_ACCOUNT_JSON", "{}"), patch.object(
                main, "TELEGRAM_SOURCE_CHATS", "-1001"
            ), patch.object(main, "SOURCE_CHANNEL_IDS", {-1001}), patch.object(
                main, "FORCE_JOIN_CHANNEL_ID", "-1002"
            ), patch.object(main, "firebase_ready", return_value=True):
                status = await main.healthz()
                self.assertTrue(status["ok"])
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

    def test_forwarding_code_and_command_are_removed(self):
        source = Path(main.__file__).read_text(encoding="utf-8").lower()
        self.assertNotIn("forward_message", source)
        self.assertNotIn("/shareall", source)
        self.assertNotIn("approved_destination", source)


if __name__ == "__main__":
    unittest.main()
