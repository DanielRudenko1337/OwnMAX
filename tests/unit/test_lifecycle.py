import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import aiosqlite
from ownmax import app
from ownmax.infrastructure.sqlite import SQLitePoolCompat
from ownmax.protocols.tamtam.processors import Processors


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_database_type(self):
        with patch.object(app.server_config, "db_type", "unknown"):
            with self.assertRaises(ValueError):
                await app.init_db()

    async def test_failed_tls_does_not_open_database(self):
        with (
            patch.object(app, "init_ssl", side_effect=OSError("missing certificate")),
            patch.object(app, "init_db", new=AsyncMock()) as database,
        ):
            with self.assertRaises(OSError):
                await app.main()
            database.assert_not_awaited()

    async def test_tasks_finish_before_database_closes(self):
        finished = []

        async def worker(name, fail=False):
            try:
                if fail:
                    await asyncio.sleep(0)
                    raise RuntimeError("isolated controller failure")
                await asyncio.Event().wait()
            finally:
                finished.append(name)

        class Database:
            def close(self):
                if len(finished) != 4:
                    raise AssertionError(f"Tasks still running: {finished}")
                self.closed = True

            async def wait_closed(self):
                pass

        database = Database()
        controllers = [
            SimpleNamespace(launch=lambda api, i=i: worker(str(i), i == 0)) for i in range(3)
        ]
        with (
            patch.object(app, "init_ssl", return_value=object()),
            patch.object(app, "init_db", new=AsyncMock(return_value=database)),
            patch.object(app, "PushService"),
            patch.object(app, "OnemeController", return_value=controllers[0]),
            patch.object(app, "TTController", return_value=controllers[1]),
            patch.object(app, "TelegramBotController", return_value=controllers[2]),
            patch.object(app, "launch_calls", new=lambda *args: worker("calls")),
            patch.object(app.sys, "platform", "win32"),
            patch.object(app.logging, "exception") as error,
        ):
            await app.main()
        self.assertTrue(database.closed)
        error.assert_called_once()


class Capture:
    def __init__(self):
        self.packets = []

    def write(self, data):
        self.packets.append(data)

    async def drain(self):
        pass


class TamTamFolderTests(unittest.IsolatedAsyncioTestCase):
    async def test_folder_create_read_and_invalid_payload(self):
        raw = await aiosqlite.connect(":memory:", isolation_level=None)
        raw.row_factory = aiosqlite.Row
        try:
            from importlib.resources import files

            await raw.executescript(
                files("ownmax.storage").joinpath("schema.sqlite.sql").read_text()
            )
            processor = Processors(db_pool=SQLitePoolCompat(raw))
            writer = Capture()
            await processor.folders_update({"id": "work", "title": "Work"}, 1, writer, "1001")
            result = processor.proto.unpack_packet(writer.packets[0])
            self.assertEqual(result["cmd"], 1)
            self.assertEqual(result["payload"]["folder"]["filters"], [])
            writer = Capture()
            await processor.folders_get({"folderSync": 0}, 2, writer, "1001")
            self.assertEqual(
                processor.proto.unpack_packet(writer.packets[-1])["payload"]["folders"][0]["title"],
                "Work",
            )
            writer = Capture()
            await processor.folders_get({"folderSync": 0}, 3, writer, "1002")
            self.assertEqual(
                processor.proto.unpack_packet(writer.packets[-1])["payload"]["folders"], []
            )
            writer = Capture()
            await processor.folders_update({"id": "bad"}, 4, writer, "1001")
            self.assertEqual(processor.proto.unpack_packet(writer.packets[-1])["cmd"], 3)
        finally:
            await raw.close()


class ExampleTests(unittest.TestCase):
    def test_bot_commands(self):
        path = Path(__file__).resolve().parents[2] / "examples/hello_bot.py"
        spec = importlib.util.spec_from_file_location("ownmax_hello_example", path)
        example = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(example)
        self.assertEqual(example.answer_for({"text": " HI "}), "Привет!")
        self.assertEqual(example.answer_for({"text": "/ping"}), "pong")
        self.assertIn(
            "/help",
            example.answer_for({"attachments": [{"_type": "CONTROL", "event": "botStarted"}]}),
        )
        self.assertIn("/start", example.answer_for({"text": "/help"}))
        self.assertIsNone(example.answer_for({"text": "unknown"}))
