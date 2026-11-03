import importlib
import io
import os
import pkgutil
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import tokenize
import unittest
from unittest.mock import patch

from aiohttp import web
import ownmax
from ownmax.infrastructure.proto_tcp import MobileProto
from ownmax.infrastructure.proto_web import WebProto
from ownmax.protocols.max import call_service, media_service


class ProtocolTests(unittest.TestCase):
    def test_mobile_roundtrip(self):
        protocol = MobileProto()
        for payload in ({}, {"text": "Привет", "items": [1, 2]}, {"text": "repeat" * 500}):
            packet = protocol.pack_packet(cmd=1, seq=65535, opcode=64, payload=payload)
            result = protocol.unpack_packet(packet)
            self.assertEqual(
                (result["seq"], result["opcode"], result["payload"]), (65535, 64, payload)
            )

    def test_mobile_truncated_packet(self):
        protocol = MobileProto()
        packet = protocol.pack_packet(payload={"text": "hello"})
        for size in (0, 9, len(packet) - 1):
            self.assertIsNone(protocol.unpack_packet(packet[:size]))

    def test_web_roundtrip_and_bad_input(self):
        protocol = WebProto()
        payload = {"text": "Привет", "id": 1}
        self.assertEqual(
            protocol.unpack_packet(protocol.pack_packet(payload=payload))["payload"], payload
        )
        for value in (None, "{", "x" * (protocol.MAX_PACKET_SIZE + 1)):
            self.assertEqual(protocol.unpack_packet(value), {})


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.keys = patch.object(
            call_service,
            "keys",
            return_value={"signing": "unit-test-secret", "turn": "unit-turn-secret"},
        )
        self.keys.start()
        self.addCleanup(self.keys.stop)

    def test_valid_token(self):
        value = call_service.token(42, "signal", "room")
        self.assertEqual(call_service.verify(value, "signal", "room"), 42)

    def test_wrong_scope_room_and_signature(self):
        value = call_service.token(42, "signal", "room")
        for credential, kind, room in (
            (value, "download", "room"),
            (value, "signal", "other"),
            (value + "0", "signal", "room"),
        ):
            with self.assertRaises(web.HTTPUnauthorized):
                call_service.verify(credential, kind, room)

    def test_expired_and_malformed_tokens(self):
        for value in (call_service.token(42, lifetime=-1), "invalid", "", None, {}):
            with self.assertRaises(web.HTTPUnauthorized):
                call_service.verify(value, "api")

    def test_binary_storage_compatibility(self):
        payload = b"\x00\xff"
        self.assertEqual(media_service.binary_default(payload), {"__ownmax_binary__": "AP8="})
        for marker in ("__ownmax_binary__", "__openmax_binary__"):
            self.assertEqual(media_service.binary_hook({marker: "AP8="}), payload)
        self.assertEqual(media_service.binary_hook({}), {})


class InstallationTests(unittest.TestCase):
    def test_import_every_module(self):
        for item in pkgutil.walk_packages(ownmax.__path__, ownmax.__name__ + "."):
            importlib.import_module(item.name)

    def test_portable_configuration(self):
        environment = {
            **os.environ,
            "OWNMAX_PUBLIC_URL": "https://ownmax.example",
            "OWNMAX_MEDIA_URL": "https://media.example",
            "OWNMAX_SIGNAL_URL": "wss://signal.example/ws2",
            "OWNMAX_TURN_URL": "turn:relay.example",
            "OWNMAX_CALL_PHONES": "1001,1002",
            "OWNMAX_TEST_PHONES": "",
            "OWNMAX_DATA_DIR": "/tmp/ownmax-config-test",
        }
        source = "from ownmax.settings import *;from ownmax.protocols.max.config import OnemeConfig;from ownmax.protocols.max import call_service as c,media_service as m;assert str(DATA_DIR)=='/tmp/ownmax-config-test';assert c.BASE=='https://ownmax.example/';assert c.WSS=='wss://signal.example/ws2';assert m.BASE=='https://media.example';assert CALL_PHONES==('1001','1002');assert OnemeConfig.SERVER_CONFIG['calls-endpoint']==c.BASE"
        subprocess.run([sys.executable, "-c", source], env=environment, check=True, timeout=15)

    def test_database_initialization_does_not_overwrite(self):
        with tempfile.TemporaryDirectory(prefix="ownmax-init-") as temporary:
            directory = Path(temporary)
            environment = {
                **os.environ,
                "db_type": "sqlite",
                "db_file": str(directory / "ownmax.db"),
                "OWNMAX_DATA_DIR": str(directory),
            }
            command = [sys.executable, "-m", "ownmax", "init-db"]
            subprocess.run(command, env=environment, check=True, capture_output=True, timeout=15)
            database = directory / "ownmax.db"
            with sqlite3.connect(database) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM users").fetchone()[0], 0)
                connection.execute("CREATE TABLE preservation (value TEXT)")
                connection.execute("INSERT INTO preservation VALUES ('keep')")
            keyfile = directory / "call-secrets.json"
            keys = keyfile.read_bytes()
            self.assertEqual(stat.S_IMODE(keyfile.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(database.stat().st_mode), 0o600)
            result = subprocess.run(command, env=environment, capture_output=True, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(keyfile.read_bytes(), keys)
            with sqlite3.connect(database) as connection:
                self.assertEqual(
                    connection.execute("SELECT value FROM preservation").fetchone()[0], "keep"
                )

    def test_python_sources_have_no_comments(self):
        root = Path(__file__).resolve().parents[2]
        for folder in ("src", "services", "examples", "tests"):
            for path in (root / folder).rglob("*.py"):
                tokens = tokenize.generate_tokens(io.StringIO(path.read_text()).readline)
                self.assertFalse(any(token.type == tokenize.COMMENT for token in tokens), str(path))


if __name__ == "__main__":
    unittest.main()
