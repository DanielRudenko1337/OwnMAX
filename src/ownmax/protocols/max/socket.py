import asyncio
import logging
import time
import traceback
import msgpack
import struct
from ownmax.infrastructure.opcodes import Opcodes
from ownmax.infrastructure.proto_tcp import MobileProto
from ownmax.infrastructure.rate_limiter import RateLimiter
from ownmax.infrastructure.tools import Tools
from ownmax.protocols.max.processors import Processors


class ProtocolVersionWriter:

    def __init__(self, writer):
        self.writer = writer
        self.version = 10

    def write(self, data):
        if len(data) >= 10 and data[0] in (10, 11):
            if self.version == 11:
                packet = MobileProto().unpack_packet(data)
                if packet is not None:
                    packer = msgpack.Packer(use_single_float=True)

                    def encode(value, key=None):
                        if isinstance(value, dict):
                            if key in ("participants", "presence", "adminParticipants"):
                                value = {
                                    (
                                        int(k)
                                        if isinstance(k, str) and k.lstrip("-").isdigit()
                                        else k
                                    ): v
                                    for k, v in value.items()
                                }
                            return packer.pack_map_header(len(value)) + b"".join(
                                (encode(k) + encode(v, k) for k, v in value.items())
                            )
                        if isinstance(value, (list, tuple)):
                            if packet["opcode"] in range(272, 279) and key in (
                                "filters",
                                "options",
                            ):
                                return packer.pack_array_header(len(value)) + b"".join(
                                    (b"\xd0" + struct.pack(">b", v) for v in value)
                                )
                            return packer.pack_array_header(len(value)) + b"".join(
                                (encode(v) for v in value)
                            )
                        if type(value) is int and (
                            key in ("typeId", "status", "uploaderType")
                            or (key == "type" and packet["opcode"] == 89)
                        ):
                            return b"\xd0" + struct.pack(">b", value)
                        if type(value) is int and key == "seen":
                            return b"\xd3" + struct.pack(">q", value)
                        if type(value) is int and key in ("accountStatus", "videoType"):
                            return b"\xd1" + struct.pack(">h", value)
                        if type(value) is int and 0 <= value <= 9223372036854775807:
                            return (
                                b"\xd2" + struct.pack(">i", value)
                                if value <= 2147483647
                                else b"\xd3" + struct.pack(">q", value)
                            )
                        return packer.pack(value)

                    payload = encode(packet["payload"])
                    data = (
                        bytes((self.version,))
                        + data[1:6]
                        + len(payload).to_bytes(4, "big")
                        + payload
                    )
            else:
                data = bytes((self.version,)) + data[1:]
        return self.writer.write(data)

    def __getattr__(self, name):
        return getattr(self.writer, name)


class OnemeMobile:

    def __init__(self, host, port, ssl_context, db_pool, clients, send_event, telegram_bot):
        self.host = host
        self.port = port
        self.ssl_context = ssl_context
        self.server = None
        self.logger = logging.getLogger(__name__)
        self.db_pool = db_pool
        self.clients = clients
        self.proto = MobileProto()
        self.auth_required = Tools().auth_required
        self.processors = Processors(
            db_pool=db_pool, clients=clients, send_event=send_event, telegram_bot=telegram_bot
        )
        self.opcodes = Opcodes()
        self.auth_rate_limiter = RateLimiter(max_attempts=15, window_seconds=60)
        self.read_timeout = 300
        self.max_read_size = 65536

    async def handle_client(self, reader, writer):
        address = writer.get_extra_info("peername")
        self.logger.info(f"Работаю с клиентом {address[0]}:{address[1]}")
        writer = ProtocolVersionWriter(writer)
        deviceType = None
        deviceName = None
        appVersion = None
        userPhone = None
        userId = None
        hashedToken = None
        try:
            while True:
                try:
                    header = await asyncio.wait_for(
                        reader.readexactly(10), timeout=self.read_timeout
                    )
                    payload_size = int.from_bytes(header[6:10], "big") & 16777215
                    if payload_size > self.proto.MAX_PAYLOAD_SIZE:
                        break
                    payload_bytes = await asyncio.wait_for(
                        reader.readexactly(payload_size), timeout=self.read_timeout
                    )
                    data = header + payload_bytes
                except asyncio.IncompleteReadError:
                    break
                except asyncio.TimeoutError:
                    self.logger.info(f"Таймаут соединения для {address[0]}:{address[1]}")
                    break
                if not data:
                    break
                if len(data) > self.proto.MAX_PAYLOAD_SIZE + 10:
                    self.logger.warning(
                        f"Пакет от {address[0]}:{address[1]} превышает лимит ({len(data)} байт)"
                    )
                    break
                packet = self.proto.unpack_packet(data)
                if packet is None:
                    self.logger.warning(f"Невалидный пакет от {address[0]}:{address[1]}")
                    continue
                if packet.get("ver") in (10, 11):
                    writer.version = packet["ver"]
                opcode = packet.get("opcode")
                seq = packet.get("seq")
                payload = packet.get("payload")
                if opcode == self.opcodes.SESSION_INIT or deviceType == "DESKTOP":
                    self.logger.info(
                        "Protocol metadata platform=%s ver=%s cmd=%s seq=%s opcode=%s",
                        deviceType,
                        packet.get("ver"),
                        packet.get("cmd"),
                        seq,
                        opcode,
                    )
                match opcode:
                    case 39 | 43:
                        await self.auth_required(
                            userPhone,
                            self.processors.private_profile_photos,
                            payload,
                            seq,
                            writer,
                            userId,
                            opcode,
                        )
                    case 288 | 289 | 291:
                        if opcode != 288 or self.auth_rate_limiter.is_allowed(address[0]):
                            await self.processors.qr_auth_operation(
                                payload,
                                seq,
                                writer,
                                opcode,
                                userId,
                                deviceType,
                                deviceName,
                                appVersion,
                                address[0],
                            )
                        else:
                            await self.processors._send_error(
                                seq, opcode, self.processors.error_types.RATE_LIMITED, writer
                            )
                    case 290:
                        if userPhone:
                            await self.processors.qr_auth_operation(
                                payload,
                                seq,
                                writer,
                                opcode,
                                userId,
                                deviceType,
                                deviceName,
                                appVersion,
                                address[0],
                            )
                        else:
                            await self.processors._send_error(
                                seq, opcode, self.processors.error_types.INVALID_TOKEN, writer
                            )
                    case 104 | 107 | 108 | 111 | 112 | 113 | 115:
                        await self.processors.private_account_security(
                            payload, seq, writer, userId, opcode
                        )
                    case self.opcodes.SESSION_INIT:
                        deviceType, deviceName, appVersion = await self.processors.session_init(
                            payload, seq, writer
                        )
                        writer.device_type = deviceType
                        self.logger.info(
                            "Client session platform=%s version=%s", deviceType, appVersion
                        )
                    case self.opcodes.AUTH_REQUEST:
                        if not self.auth_rate_limiter.is_allowed(address[0]):
                            await self.processors._send_error(
                                seq,
                                self.opcodes.AUTH_REQUEST,
                                self.processors.error_types.RATE_LIMITED,
                                writer,
                            )
                        else:
                            await self.processors.auth_request(payload, seq, writer)
                    case self.opcodes.AUTH:
                        if not self.auth_rate_limiter.is_allowed(address[0]):
                            await self.processors._send_error(
                                seq,
                                self.opcodes.AUTH,
                                self.processors.error_types.RATE_LIMITED,
                                writer,
                            )
                        else:
                            await self.processors.auth(
                                payload, seq, writer, deviceType, deviceName, appVersion, address[0]
                            )
                    case self.opcodes.AUTH_CONFIRM:
                        if not self.auth_rate_limiter.is_allowed(address[0]):
                            await self.processors._send_error(
                                seq,
                                self.opcodes.AUTH_CONFIRM,
                                self.processors.error_types.RATE_LIMITED,
                                writer,
                            )
                        elif payload and payload.get("tokenType") == "REGISTER":
                            await self.processors.auth_confirm(
                                payload, seq, writer, deviceType, deviceName, appVersion, address[0]
                            )
                        else:
                            self.logger.warning(f"AUTH_CONFIRM с неизвестным tokenType: {payload}")
                    case self.opcodes.LOGIN:
                        if not self.auth_rate_limiter.is_allowed(address[0]):
                            await self.processors._send_error(
                                seq,
                                self.opcodes.LOGIN,
                                self.processors.error_types.RATE_LIMITED,
                                writer,
                            )
                        else:
                            userPhone, userId, hashedToken = await self.processors.login(
                                payload, seq, writer, appVersion
                            )
                            if userPhone:
                                await self._finish_auth(writer, address, userPhone, userId)
                                from ownmax.protocols.max.session_metadata import store

                                await store(
                                    self.processors,
                                    hashedToken,
                                    address[0],
                                    writer,
                                    deviceType,
                                    deviceName,
                                    appVersion,
                                )
                    case self.opcodes.SYNC:
                        await self.auth_required(
                            userPhone, self.processors.sync, payload, seq, writer, userId
                        )
                    case self.opcodes.LOGOUT:
                        await self.processors.logout(seq, writer, hashedToken=hashedToken)
                        break
                    case self.opcodes.PING:
                        await self.processors.ping(payload, seq, writer, userId)
                    case self.opcodes.LOG:
                        await self.processors.log(payload, seq, writer)
                    case self.opcodes.ASSETS_UPDATE:
                        await self.auth_required(
                            userPhone, self.processors.assets_update, payload, seq, writer
                        )
                    case self.opcodes.ASSETS_GET:
                        await self.auth_required(
                            userPhone, self.processors.assets_get, payload, seq, writer
                        )
                    case self.opcodes.ASSETS_GET_BY_IDS:
                        await self.auth_required(
                            userPhone, self.processors.assets_get_by_ids, payload, seq, writer
                        )
                    case self.opcodes.ASSETS_ADD:
                        await self.auth_required(
                            userPhone, self.processors.assets_add, payload, seq, writer
                        )
                    case self.opcodes.ASSETS_REMOVE:
                        await self.auth_required(
                            userPhone, self.processors.assets_remove, payload, seq, writer
                        )
                    case self.opcodes.ASSETS_MOVE:
                        await self.auth_required(
                            userPhone, self.processors.assets_move, payload, seq, writer
                        )
                    case self.opcodes.ASSETS_LIST_MODIFY:
                        await self.auth_required(
                            userPhone, self.processors.assets_list_modify, payload, seq, writer
                        )
                    case self.opcodes.VIDEO_CHAT_HISTORY:
                        await self.auth_required(
                            userPhone, self.processors.video_chat_history, payload, seq, writer
                        )
                    case 119:
                        await self.auth_required(
                            userPhone,
                            self.processors.native_bot_suspend,
                            payload,
                            seq,
                            writer,
                            userId,
                        )
                    case 144 | 145:
                        await self.auth_required(
                            userPhone,
                            self.processors.native_bot_info,
                            payload,
                            seq,
                            writer,
                            userId,
                            opcode,
                        )
                    case 56 | 57 | 221 | 222 | 223 | 300:
                        await self.auth_required(
                            userPhone,
                            self.processors.chat_link_operation,
                            payload,
                            seq,
                            writer,
                            userId,
                            opcode,
                        )
                    case 52 | 54 | 58:
                        await self.auth_required(
                            userPhone,
                            self.processors.chat_lifecycle_operation,
                            payload,
                            seq,
                            writer,
                            userId,
                            opcode,
                        )
                    case 51:
                        await self.auth_required(
                            userPhone, self.processors.chat_media, payload, seq, writer, userId
                        )
                    case 50 | 74:
                        await self.auth_required(
                            userPhone,
                            self.processors.chat_read_operation,
                            payload,
                            seq,
                            writer,
                            userId,
                            opcode,
                        )
                    case 55 | 59 | 77:
                        await self.auth_required(
                            userPhone,
                            self.processors.private_group_operation,
                            payload,
                            seq,
                            writer,
                            userId,
                            opcode,
                        )
                    case 76 | 84 | 89 | 166 | 195:
                        await self.auth_required(
                            userPhone,
                            self.processors.private_group_call,
                            payload,
                            seq,
                            writer,
                            userId,
                            opcode,
                        )
                    case self.opcodes.MSG_DELETE:
                        await self.auth_required(
                            userPhone, self.processors.msg_delete, payload, seq, writer, userId
                        )
                    case self.opcodes.MSG_SEND:
                        await self.auth_required(
                            userPhone,
                            self.processors.msg_send,
                            payload,
                            seq,
                            writer,
                            userId,
                            self.db_pool,
                        )
                    case 273 | 275 | 276:
                        await self.auth_required(
                            userPhone,
                            self.processors.folder_operation,
                            payload,
                            seq,
                            writer,
                            userPhone,
                            opcode,
                        )
                    case self.opcodes.FOLDERS_GET:
                        await self.auth_required(
                            userPhone, self.processors.folders_get, payload, seq, writer, userPhone
                        )
                    case self.opcodes.FOLDERS_UPDATE:
                        await self.auth_required(
                            userPhone,
                            self.processors.folders_update,
                            payload,
                            seq,
                            writer,
                            userPhone,
                        )
                    case self.opcodes.SESSIONS_CLOSE:
                        if userPhone:
                            hashedToken = await self.processors.sessions_close(
                                payload, seq, writer, userPhone, hashedToken
                            )
                        else:
                            await self.processors._send_error(
                                seq, 97, self.processors.error_types.INVALID_TOKEN, writer
                            )
                    case self.opcodes.SESSIONS_INFO:
                        await self.auth_required(
                            userPhone,
                            self.processors.sessions_info,
                            payload,
                            seq,
                            writer,
                            userPhone,
                            hashedToken,
                        )
                    case self.opcodes.CHAT_INFO:
                        await self.auth_required(
                            userPhone, self.processors.chat_info, payload, seq, writer, userId
                        )
                    case self.opcodes.CHAT_HISTORY:
                        await self.auth_required(
                            userPhone, self.processors.chat_history, payload, seq, writer, userId
                        )
                    case self.opcodes.CONTACT_INFO_BY_PHONE:
                        await self.auth_required(
                            userPhone,
                            self.processors.contact_info_by_phone,
                            payload,
                            seq,
                            writer,
                            userId,
                        )
                    case 80 | 82 | 87:
                        await self.auth_required(
                            userPhone,
                            self.processors.private_upload,
                            payload,
                            seq,
                            writer,
                            userId,
                            opcode,
                        )
                    case 301:
                        await self.auth_required(
                            userPhone,
                            self.processors.private_audio_url,
                            payload,
                            seq,
                            writer,
                            userId,
                        )
                    case 178 | 179 | 180 | 181:
                        await self.auth_required(
                            userPhone,
                            self.processors.message_reaction,
                            payload,
                            seq,
                            writer,
                            userId,
                            opcode,
                        )
                    case 83:
                        await self.auth_required(
                            userPhone,
                            self.processors.private_media_url,
                            payload,
                            seq,
                            writer,
                            userId,
                        )
                    case 78:
                        await self.auth_required(
                            userPhone,
                            self.processors.start_private_call,
                            payload,
                            seq,
                            writer,
                            userId,
                        )
                    case self.opcodes.OK_TOKEN:
                        await self.auth_required(
                            userPhone, self.processors.ok_token, payload, seq, writer, userId
                        )
                    case self.opcodes.MSG_TYPING:
                        await self.auth_required(
                            userPhone, self.processors.msg_typing, payload, seq, writer, userId
                        )
                    case self.opcodes.CONTACT_INFO:
                        await self.auth_required(
                            userPhone, self.processors.contact_info, payload, seq, writer, userId
                        )
                    case self.opcodes.CONTACT_LIST:
                        await self.auth_required(
                            userPhone, self.processors.contact_list, payload, seq, writer, userId
                        )
                    case self.opcodes.COMPLAIN_REASONS_GET:
                        await self.auth_required(
                            userPhone, self.processors.complain_reasons_get, payload, seq, writer
                        )
                    case self.opcodes.PROFILE:
                        await self.processors.profile(payload, seq, writer, userId=userId)
                    case self.opcodes.CHAT_SUBSCRIBE:
                        await self.auth_required(
                            userPhone, self.processors.chat_subscribe, payload, seq, writer
                        )
                    case self.opcodes.CONFIG:
                        await self.auth_required(
                            userPhone,
                            self.processors.update_config,
                            payload,
                            seq,
                            writer,
                            userPhone,
                            hashedToken,
                        )
                    case self.opcodes.CONTACT_UPDATE:
                        await self.auth_required(
                            userPhone, self.processors.contact_update, payload, seq, writer, userId
                        )
                    case self.opcodes.CONTACT_ADD_BY_PHONE:
                        await self.auth_required(
                            userPhone,
                            self.processors.contact_add_by_phone,
                            payload,
                            seq,
                            writer,
                            userId,
                        )
                    case self.opcodes.CONTACT_PRESENCE:
                        await self.auth_required(
                            userPhone, self.processors.contact_presence, payload, seq, writer
                        )
                    case _:
                        self.logger.warning(f"Неизвестный опкод {opcode}")
        except Exception as e:
            self.logger.error(
                f"Произошла ошибка при работе с клиентом {address[0]}:{address[1]}: {e}"
            )
            traceback.print_exc()
        if userPhone:
            await self._end_session(userId, address[0], address[1])
        writer.close()
        self.logger.info(f"Прекратил работать работать с клиентом {address[0]}:{address[1]}")

    async def _finish_auth(self, writer, addr, phone, id):
        user = self.clients.get(id)
        if user:
            user["clients"].append(
                {
                    "writer": writer,
                    "ip": addr[0],
                    "port": addr[1],
                    "protocol": "oneme",
                    "device_type": getattr(writer, "device_type", None),
                }
            )
        else:
            self.clients[id] = {
                "phone": phone,
                "id": id,
                "status": 2,
                "last_seen": 0,
                "clients": [
                    {
                        "writer": writer,
                        "ip": addr[0],
                        "port": addr[1],
                        "protocol": "oneme",
                        "device_type": getattr(writer, "device_type", None),
                    }
                ],
            }
        self.clients[id]["status"] = 2
        self.clients[id]["last_seen"] = int(time.time())
        await self._broadcast_presence(id, True)

    async def _broadcast_presence(self, userId, online):
        now = int(time.time())
        now_ms = int(time.time() * 1000)
        if online:
            presence_data = {"on": "ON", "seen": now, "status": 1}
        else:
            presence_data = {"seen": now, "on": "OFF", "status": 0}
        async with self.db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT owner_id FROM contacts WHERE contact_id = %s", (userId,)
                )
                contact_owners = await cursor.fetchall()
        async with self.db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT DISTINCT user_id AS owner_id FROM chat_participants WHERE chat_id IN (SELECT chat_id FROM chat_participants WHERE user_id = %s) AND user_id != %s",
                    (userId, userId),
                )
                dialog_owners = await cursor.fetchall()
        for owner_id in {int(row["owner_id"]) for row in contact_owners + dialog_owners}:
            if owner_id in self.clients:
                await self.processors.event(
                    owner_id,
                    {
                        "eventType": "presence",
                        "userId": userId,
                        "presence": presence_data,
                        "time": now_ms,
                    },
                )

    async def _end_session(self, id, ip, port):
        user = self.clients.get(id)
        if not user:
            return
        clients = user.get("clients", [])
        for i, client in enumerate(clients):
            if (client.get("ip"), client.get("port")) == (ip, port):
                clients.pop(i)
        if clients:
            online = Tools.presence_is_online(user)
            user["status"] = 2 if online else 0
            await self._broadcast_presence(id, online)
        if not clients:
            now = int(time.time())
            user["status"] = 0
            user["last_seen"] = now
            async with self.db_pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        "UPDATE users SET lastseen = %s WHERE id = %s", (str(now), id)
                    )
            await self._broadcast_presence(id, False)

    async def start(self):
        self.server = await asyncio.start_server(
            self.handle_client, self.host, self.port, ssl=self.ssl_context
        )
        self.logger.info(f"Сокет запущен на порту {self.port}")
        try:
            async with self.server:
                await self.server.serve_forever()
        except asyncio.CancelledError:
            self.server.close()
            await self.server.wait_closed()
