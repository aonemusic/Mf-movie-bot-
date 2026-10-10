import asyncio
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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

    def test_search_pagination_shows_ten_blue_file_buttons_and_green_navigation(self):
        matches = [
            (f"-1001_{i}", {"file_name": f"Movie {i}.mkv", "file_size": i * 1024})
            for i in range(1, 23)
        ]
        keyboard = main.search_page_keyboard(matches, "session12345678", 0)
        self.assertEqual(len(keyboard.inline_keyboard), 11)
        self.assertEqual(keyboard.inline_keyboard[0][0].style, "primary")
        navigation = keyboard.inline_keyboard[-1]
        self.assertEqual(navigation[0].callback_data, "page:session12345678:10")
        self.assertEqual(navigation[0].style, "success")

        second_page = main.search_page_keyboard(matches, "session12345678", 10)
        self.assertEqual(len(second_page.inline_keyboard), 11)
        self.assertEqual(second_page.inline_keyboard[-1][0].text, "‹ Back")
        self.assertEqual(second_page.inline_keyboard[-1][1].text, "Next ›")

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
        top = next(value.value for path, value in refs.items() if path.startswith("stats/top_searches/"))
        self.assertEqual(top["count"], 2)
        self.assertEqual(top["query"], "the matrix 1999")

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
        }

        async def run():
            with patch.object(main, "firebase_reference", side_effect=lambda path: FakeTreeReference(tree, path)):
                return await main.get_status_stats()

        stats = asyncio.run(run())
        self.assertEqual(stats["total_users"], 2)
        self.assertEqual(stats["total_files"], 3)
        self.assertEqual(stats["total_searches"], 6)
        self.assertEqual(tree["stats"]["total_users"], 2)
        self.assertEqual(tree["stats"]["total_files"], 3)
        self.assertEqual(tree["stats"]["total_searches"], 6)

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
