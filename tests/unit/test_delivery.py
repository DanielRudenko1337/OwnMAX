import hashlib
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import aiosqlite
from importlib.resources import files
from ownmax.infrastructure import auth_delivery
from ownmax.infrastructure.sqlite import SQLitePoolCompat
from ownmax.protocols.max.processors import auth as max_auth
from ownmax.protocols.max.websocket import OnemeWS


class Capture:

    def __init__(self):
        self.packets = []

    def write(self, data):
        self.packets.append(data)

    async def drain(self):
        pass


class ConsoleCodeTests(unittest.IsolatedAsyncioTestCase):

    async def test_console_contains_phone_and_six_digit_code(self):
        with (
            patch.object(auth_delivery.secrets, "randbelow", return_value=42),
            patch("builtins.print") as console,
        ):
            self.assertEqual(auth_delivery.issue_code("79990009991"), "000042")
        console.assert_called_once_with(
            "OwnMAX: код входа или регистрации для 79990009991: 000042", flush=True
        )

    async def test_login_and_registration_use_console_for_max(self):
        raw = await aiosqlite.connect(":memory:", isolation_level=None)
        raw.row_factory = aiosqlite.Row
        try:
            await raw.executescript(
                files("ownmax.storage").joinpath("schema.sqlite.sql").read_text()
            )
            await raw.execute(
                "INSERT INTO users(id,phone,firstname,lastname,username,description,profileoptions,options,accountstatus,updatetime,lastseen) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (1001, "79990009991", "Fixture", "", "fixture", "", "[]", "[]", 0, "0", "0"),
            )
            processor = max_auth.AuthProcessors(
                db_pool=SQLitePoolCompat(raw), send_event=AsyncMock()
            )
            for phone in ("79990009991", "79990009992"):
                writer = Capture()
                with (
                    patch.object(auth_delivery.secrets, "randbelow", return_value=123456),
                    patch("builtins.print") as console,
                ):
                    await processor.auth_request({"phone": phone, "type": "START_AUTH"}, 1, writer)
                response = processor.proto.unpack_packet(writer.packets[-1])
                self.assertEqual(response["cmd"], 1)
                self.assertEqual(response["payload"]["codeLength"], 6)
                self.assertIn(phone, console.call_args.args[0])
                self.assertIn("123456", console.call_args.args[0])
                row = await (
                    await raw.execute("SELECT code_hash FROM auth_tokens WHERE phone=?", (phone,))
                ).fetchone()
                self.assertEqual(row[0], hashlib.sha256(b"123456").hexdigest())
        finally:
            await raw.close()


class WebSocketDispatchTests(unittest.IsolatedAsyncioTestCase):

    async def test_operations_receive_current_websocket(self):
        server = OnemeWS("127.0.0.1", 0, None, None, {}, None, None)
        server.auth_required = AsyncMock()
        server.logger = MagicMock()
        server.processors.private_account_security = AsyncMock()
        operations = [104, 55, 76, server.opcodes.MSG_DELETE, 273]

        class Socket:
            remote_address = ("127.0.0.1", 12345)

            def __aiter__(self):

                async def messages():
                    for opcode in operations:
                        yield json.dumps({"opcode": opcode, "seq": 1, "payload": {}})

                return messages()

        socket = Socket()
        await server.handle_client(socket)
        server.processors.private_account_security.assert_awaited_once_with(
            {}, 1, socket, None, 104
        )
        self.assertEqual(server.auth_required.await_count, 4)
        for call in server.auth_required.await_args_list:
            self.assertIs(call.args[4], socket)
        server.logger.error.assert_not_called()
