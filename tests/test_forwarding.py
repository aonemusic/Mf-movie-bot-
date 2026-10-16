import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import main


class FakeTreeReference:
    def __init__(self, tree, path):
        self.tree = tree
        self.path = tuple(part for part in path.split("/") if part)

    def _node(self, parts=None, create=False):
        node = self.tree
        for segment in self.path if parts is None else parts:
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


class ForwardingTests(unittest.TestCase):
    def test_channel_reference_parser(self):
        self.assertEqual(main.parse_forward_channel_reference("-1001234567890"), -1001234567890)
        self.assertEqual(main.parse_forward_channel_reference("@my_channel"), "@my_channel")
        self.assertEqual(main.parse_forward_channel_reference("https://t.me/my_channel"), "@my_channel")
        self.assertIsNone(main.parse_forward_channel_reference("https://example.com/channel"))
        self.assertIsNone(main.parse_forward_channel_reference("12345"))

    def test_destination_registration_requires_both_admins_and_persists_to_firebase(self):
        tree = {}
        chat = SimpleNamespace(id=-1001234567890, type="channel", title="Target", username="target")
        bot = SimpleNamespace(
            id=999,
            get_chat=AsyncMock(return_value=chat),
            get_chat_member=AsyncMock(side_effect=[
                SimpleNamespace(status="administrator", can_post_messages=True),
                SimpleNamespace(status="creator"),
            ]),
        )

        async def run():
            with (
                patch.object(main, "firebase_ready", return_value=True),
                patch.object(main, "firebase_reference", side_effect=lambda path: FakeTreeReference(tree, path)),
            ):
                result = await main.register_forward_destination(bot, 42, "-1001234567890")
                self.assertTrue(result[0])
                self.assertEqual(result[2], -1001234567890)
                self.assertEqual(tree["settings"]["forward_channels"]["-1001234567890"]["added_by"], 42)
                channels = await main.get_forward_destinations()
                self.assertEqual(channels[0]["title"], "Target")

        asyncio.run(run())

    def test_forwarding_markers_prevent_duplicate_delivery(self):
        tree = {}
        bot = SimpleNamespace(forward_message=AsyncMock())
        record = {"source_chat_id": -100111, "source_message_id": 15}

        async def run():
            with (
                patch.object(main, "firebase_ready", return_value=True),
                patch.object(main, "firebase_reference", side_effect=lambda path: FakeTreeReference(tree, path)),
            ):
                first = await main.forward_catalog_record(bot, record, -100222, "-100111_-100222")
                second = await main.forward_catalog_record(bot, record, -100222, "-100111_-100222")
                self.assertEqual(first, "sent")
                self.assertEqual(second, "already")
                bot.forward_message.assert_awaited_once_with(
                    chat_id=-100222,
                    from_chat_id=-100111,
                    message_id=15,
                )

        asyncio.run(run())

    def test_new_media_is_auto_forwarded_only_for_matching_enabled_route(self):
        tree = {
            "settings": {
                "forward_routes": {
                    "-100111_-100222": {
                        "enabled": True,
                        "source_chat_id": -100111,
                        "destination_chat_id": -100222,
                    },
                    "-100333_-100444": {
                        "enabled": True,
                        "source_chat_id": -100333,
                        "destination_chat_id": -100444,
                    },
                }
            }
        }
        bot = SimpleNamespace(forward_message=AsyncMock())
        record = {"source_chat_id": -100111, "source_message_id": 16}

        async def run():
            with (
                patch.object(main, "firebase_ready", return_value=True),
                patch.object(main, "BOT_APP", SimpleNamespace(bot=bot)),
                patch.object(main, "firebase_reference", side_effect=lambda path: FakeTreeReference(tree, path)),
            ):
                await main.auto_forward_new_media(record)
                bot.forward_message.assert_awaited_once_with(
                    chat_id=-100222,
                    from_chat_id=-100111,
                    message_id=16,
                )

        asyncio.run(run())

    def test_backfill_forwards_all_catalogued_files_from_only_the_selected_source(self):
        route_key = "-100111_-100222"
        tree = {
            "forward_jobs": {
                route_key: {
                    "status": "running",
                    "source_chat_id": -100111,
                    "destination_chat_id": -100222,
                    "admin_id": 42,
                }
            }
        }
        bot = SimpleNamespace(forward_message=AsyncMock())
        app = SimpleNamespace(bot=bot)
        catalog = {
            "-100111_2": {"source_chat_id": -100111, "source_message_id": 2},
            "-100111_1": {"source_chat_id": -100111, "source_message_id": 1},
            "-100333_1": {"source_chat_id": -100333, "source_message_id": 1},
        }

        async def run():
            with (
                patch.object(main, "firebase_ready", return_value=True),
                patch.object(main, "BOT_APP", app),
                patch.object(main, "get_catalog", new=AsyncMock(return_value=catalog)),
                patch.object(main, "notify_admin", new=AsyncMock()),
                patch.object(main, "firebase_reference", side_effect=lambda path: FakeTreeReference(tree, path)),
            ):
                await main.run_forward_backfill(route_key)
                job = tree["forward_jobs"][route_key]
                self.assertEqual(job["status"], "completed")
                self.assertEqual(job["total"], 2)
                self.assertEqual(job["sent_count"], 2)
                self.assertEqual(
                    [call.kwargs["message_id"] for call in bot.forward_message.await_args_list],
                    [1, 2],
                )

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
