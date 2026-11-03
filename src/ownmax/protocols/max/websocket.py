import logging
import time
import traceback
import websockets
import asyncio
from ownmax.infrastructure.proto_web import WebProto
from ownmax.protocols.max.processors import Processors
from ownmax.infrastructure.rate_limiter import RateLimiter
from ownmax.infrastructure.opcodes import Opcodes
from ownmax.infrastructure.tools import Tools


class OnemeWS:

    def __init__(self, host, port, ssl_context, db_pool, clients, send_event, telegram_bot):
        self.host = host
        self.port = port
        self.ssl_context = ssl_context
        self.server = None
        self.logger = logging.getLogger(__name__)
        self.db_pool = db_pool
        self.clients = clients
        self.opcodes = Opcodes()
        self.proto = WebProto()
        self.processors = Processors(
            db_pool=db_pool,
            clients=clients,
            send_event=send_event,
            telegram_bot=telegram_bot,
            type="web",
        )
        self.auth_required = Tools().auth_required
        self.auth_rate_limiter = RateLimiter(max_attempts=5, window_seconds=60)
        self.read_timeout = 300
        self.max_read_size = 65536

    async def handle_client(self, websocket):
        address = websocket.remote_address
        self.logger.info(f"Работаю с клиентом {address[0]}:{address[1]}")
        deviceType = None
        deviceName = None
        appVersion = None
        userPhone = None
        userId = None
        hashedToken = None
        try:
            async for message in websocket:
                if len(message) > self.max_read_size:
                    self.logger.warning(
                        f"Пакет от {address[0]}:{address[1]} превышает лимит ({len(message)} байт)"
                    )
                    break
                packet = self.proto.unpack_packet(message)
                if not packet:
                    self.logger.warning(f"Невалидный пакет от {address[0]}:{address[1]}")
                    continue
                opcode = packet.get("opcode")
                seq = packet.get("seq")
                payload = packet.get("payload")
                match opcode:
                    case 39 | 43:
                        await self.auth_required(
                            userPhone,
                            self.processors.private_profile_photos,
                            payload,
                            seq,
                            websocket,
                            userId,
                            opcode,
                        )
                    case 288 | 289 | 291:
                        if opcode != 288 or self.auth_rate_limiter.is_allowed(address[0]):
                            await self.processors.qr_auth_operation(
                                payload,
                                seq,
                                websocket,
                                opcode,
                                userId,
                                deviceType,
                                deviceName,
                                appVersion,
                                address[0],
                            )
                        else:
                            await self.processors._send_error(
                                seq, opcode, self.processors.error_types.RATE_LIMITED, websocket
                            )
                    case 290:
                        if userPhone:
                            await self.processors.qr_auth_operation(
                                payload,
                                seq,
                                websocket,
                                opcode,
                                userId,
                                deviceType,
                                deviceName,
                                appVersion,
                                address[0],
                            )
                        else:
                            await self.processors._send_error(
                                seq, opcode, self.processors.error_types.INVALID_TOKEN, websocket
                            )
                    case 104 | 107 | 108 | 111 | 112 | 113 | 115:
                        await self.processors.private_account_security(
                            payload, seq, websocket, userId, opcode
                        )
                    case self.opcodes.SESSION_INIT:
                        deviceType, deviceName, appVersion = await self.processors.session_init(
                            payload, seq, websocket
                        )
                    case self.opcodes.AUTH_REQUEST:
                        if not self.auth_rate_limiter.is_allowed(address[0]):
                            await self.processors._send_error(
                                seq,
                                self.opcodes.AUTH_REQUEST,
                                self.processors.error_types.RATE_LIMITED,
                                websocket,
                            )
                        else:
                            await self.processors.auth_request(payload, seq, websocket)
                    case self.opcodes.AUTH:
                        if not self.auth_rate_limiter.is_allowed(address[0]):
                            await self.processors._send_error(
                                seq,
                                self.opcodes.AUTH,
                                self.processors.error_types.RATE_LIMITED,
                                websocket,
                            )
                        else:
                            await self.processors.auth(
                                payload,
                                seq,
                                websocket,
                                deviceType,
                                deviceName,
                                appVersion,
                                address[0],
                            )
                    case self.opcodes.AUTH_CONFIRM:
                        if not self.auth_rate_limiter.is_allowed(address[0]):
                            await self.processors._send_error(
                                seq,
                                self.opcodes.AUTH_CONFIRM,
                                self.processors.error_types.RATE_LIMITED,
                                websocket,
                            )
                        elif payload and payload.get("tokenType") == "REGISTER":
                            await self.processors.auth_confirm(
                                payload,
                                seq,
                                websocket,
                                deviceType,
                                deviceName,
                                appVersion,
                                address[0],
                            )
                        else:
                            self.logger.warning(f"AUTH_CONFIRM с неизвестным tokenType: {payload}")
                    case self.opcodes.LOGIN:
                        if not self.auth_rate_limiter.is_allowed(address[0]):
                            await self.processors._send_error(
                                seq,
                                self.opcodes.LOGIN,
                                self.processors.error_types.RATE_LIMITED,
                                websocket,
                            )
                        else:
                            userPhone, userId, hashedToken = await self.processors.login(
                                payload, seq, websocket, appVersion
                            )
                            if userPhone:
                                await self._finish_auth(websocket, address, userPhone, userId)
                                from ownmax.protocols.max.session_metadata import store

                                await store(
                                    self.processors,
                                    hashedToken,
                                    address[0],
                                    websocket,
                                    deviceType,
                                    deviceName,
                                    appVersion,
                                )
                    case self.opcodes.LOGOUT:
                        await self.processors.logout(seq, websocket, hashedToken=hashedToken)
                        break
                    case self.opcodes.PING:
                        await self.processors.ping(payload, seq, websocket, userId)
                    case self.opcodes.LOG:
                        await self.processors.log(payload, seq, websocket)
                    case self.opcodes.ASSETS_UPDATE:
                        await self.auth_required(
                            userPhone, self.processors.assets_update, payload, seq, websocket
                        )
                    case self.opcodes.ASSETS_GET:
                        await self.auth_required(
                            userPhone, self.processors.assets_get, payload, seq, websocket
                        )
                    case self.opcodes.ASSETS_GET_BY_IDS:
                        await self.auth_required(
                            userPhone, self.processors.assets_get_by_ids, payload, seq, websocket
                        )
                    case self.opcodes.ASSETS_ADD:
                        await self.auth_required(
                            userPhone, self.processors.assets_add, payload, seq, websocket
                        )
                    case self.opcodes.ASSETS_REMOVE:
                        await self.auth_required(
                            userPhone, self.processors.assets_remove, payload, seq, websocket
                        )
                    case self.opcodes.ASSETS_MOVE:
                        await self.auth_required(
                            userPhone, self.processors.assets_move, payload, seq, websocket
                        )
                    case self.opcodes.ASSETS_LIST_MODIFY:
                        await self.auth_required(
                            userPhone, self.processors.assets_list_modify, payload, seq, websocket
                        )
                    case self.opcodes.VIDEO_CHAT_HISTORY:
                        await self.auth_required(
                            userPhone, self.processors.video_chat_history, payload, seq, websocket
                        )
                    case 119:
                        await self.auth_required(
                            userPhone,
                            self.processors.native_bot_suspend,
                            payload,
                            seq,
                            websocket,
                            userId,
                        )
                    case 144 | 145:
                        await self.auth_required(
                            userPhone,
                            self.processors.native_bot_info,
                            payload,
                            seq,
                            websocket,
                            userId,
                            opcode,
                        )
                    case 56 | 57 | 221 | 222 | 223 | 300:
                        await self.auth_required(
                            userPhone,
                            self.processors.chat_link_operation,
                            payload,
                            seq,
                            websocket,
                            userId,
                            opcode,
                        )
                    case 52 | 54 | 58:
                        await self.auth_required(
                            userPhone,
                            self.processors.chat_lifecycle_operation,
                            payload,
                            seq,
                            websocket,
                            userId,
                            opcode,
                        )
                    case 51:
                        await self.auth_required(
                            userPhone, self.processors.chat_media, payload, seq, websocket, userId
                        )
                    case 50 | 74:
                        await self.auth_required(
                            userPhone,
                            self.processors.chat_read_operation,
                            payload,
                            seq,
                            websocket,
                            userId,
                            opcode,
                        )
                    case 55 | 59 | 77:
                        await self.auth_required(
                            userPhone,
                            self.processors.private_group_operation,
                            payload,
                            seq,
                            websocket,
                            userId,
                            opcode,
                        )
                    case 76 | 84 | 89 | 166 | 195:
                        await self.auth_required(
                            userPhone,
                            self.processors.private_group_call,
                            payload,
                            seq,
                            websocket,
                            userId,
                            opcode,
                        )
                    case self.opcodes.MSG_DELETE:
                        await self.auth_required(
                            userPhone, self.processors.msg_delete, payload, seq, websocket, userId
                        )
                    case self.opcodes.MSG_SEND:
                        await self.auth_required(
                            userPhone,
                            self.processors.msg_send,
                            payload,
                            seq,
                            websocket,
                            userId,
                            self.db_pool,
                        )
                    case 273 | 275 | 276:
                        await self.auth_required(
                            userPhone,
                            self.processors.folder_operation,
                            payload,
                            seq,
                            websocket,
                            userPhone,
                            opcode,
                        )
                    case self.opcodes.FOLDERS_GET:
                        await self.auth_required(
                            userPhone,
                            self.processors.folders_get,
                            payload,
                            seq,
                            websocket,
                            userPhone,
                        )
                    case self.opcodes.FOLDERS_UPDATE:
                        await self.auth_required(
                            userPhone,
                            self.processors.folders_update,
                            payload,
                            seq,
                            websocket,
                            userPhone,
                        )
                    case self.opcodes.SESSIONS_CLOSE:
                        if userPhone:
                            hashedToken = await self.processors.sessions_close(
                                payload, seq, websocket, userPhone, hashedToken
                            )
                        else:
                            await self.processors._send_error(
                                seq, 97, self.processors.error_types.INVALID_TOKEN, websocket
                            )
                    case self.opcodes.SESSIONS_INFO:
                        await self.auth_required(
                            userPhone,
                            self.processors.sessions_info,
                            payload,
                            seq,
                            websocket,
                            userPhone,
                            hashedToken,
                        )
                    case self.opcodes.CHAT_INFO:
                        await self.auth_required(
                            userPhone, self.processors.chat_info, payload, seq, websocket, userId
                        )
                    case self.opcodes.CHAT_HISTORY:
                        await self.auth_required(
                            userPhone, self.processors.chat_history, payload, seq, websocket, userId
                        )
                    case self.opcodes.CONTACT_INFO_BY_PHONE:
                        await self.auth_required(
                            userPhone,
                            self.processors.contact_info_by_phone,
                            payload,
                            seq,
                            websocket,
                            userId,
                        )
                    case 80 | 82 | 87:
                        await self.auth_required(
                            userPhone,
                            self.processors.private_upload,
                            payload,
                            seq,
                            websocket,
                            userId,
                            opcode,
                        )
                    case 301:
                        await self.auth_required(
                            userPhone,
                            self.processors.private_audio_url,
                            payload,
                            seq,
                            websocket,
                            userId,
                        )
                    case 178 | 179 | 180 | 181:
                        await self.auth_required(
                            userPhone,
                            self.processors.message_reaction,
                            payload,
                            seq,
                            websocket,
                            userId,
                            opcode,
                        )
                    case 83:
                        await self.auth_required(
                            userPhone,
                            self.processors.private_media_url,
                            payload,
                            seq,
                            websocket,
                            userId,
                        )
                    case 78:
                        await self.auth_required(
                            userPhone,
                            self.processors.start_private_call,
                            payload,
                            seq,
                            websocket,
                            userId,
                        )
                    case self.opcodes.OK_TOKEN:
                        await self.auth_required(
                            userPhone, self.processors.ok_token, payload, seq, websocket, userId
                        )
                    case self.opcodes.MSG_TYPING:
                        await self.auth_required(
                            userPhone, self.processors.msg_typing, payload, seq, websocket, userId
                        )
                    case self.opcodes.CONTACT_INFO:
                        await self.auth_required(
                            userPhone, self.processors.contact_info, payload, seq, websocket, userId
                        )
                    case self.opcodes.CONTACT_LIST:
                        await self.auth_required(
                            userPhone, self.processors.contact_list, payload, seq, websocket, userId
                        )
                    case self.opcodes.COMPLAIN_REASONS_GET:
                        await self.auth_required(
                            userPhone, self.processors.complain_reasons_get, payload, seq, websocket
                        )
                    case self.opcodes.PROFILE:
                        await self.processors.profile(payload, seq, websocket, userId=userId)
                    case self.opcodes.CHAT_SUBSCRIBE:
                        await self.auth_required(
                            userPhone, self.processors.chat_subscribe, payload, seq, websocket
                        )
                    case self.opcodes.CONFIG:
                        await self.auth_required(
                            userPhone,
                            self.processors.update_config,
                            payload,
                            seq,
                            websocket,
                            userPhone,
                            hashedToken,
                        )
                    case self.opcodes.CONTACT_UPDATE:
                        await self.auth_required(
                            userPhone,
                            self.processors.contact_update,
                            payload,
                            seq,
                            websocket,
                            userId,
                        )
                    case self.opcodes.CONTACT_ADD_BY_PHONE:
                        await self.auth_required(
                            userPhone,
                            self.processors.contact_add_by_phone,
                            payload,
                            seq,
                            websocket,
                            userId,
                        )
                    case self.opcodes.CONTACT_PRESENCE:
                        await self.auth_required(
                            userPhone, self.processors.contact_presence, payload, seq, websocket
                        )
                    case _:
                        self.logger.warning(f"Неизвестный опкод {opcode}")
        except websockets.exceptions.ConnectionClosed:
            self.logger.info(f"Прекратил работать с клиентом {address[0]}:{address[1]}")
        except Exception as e:
            self.logger.error(
                f" Произошла ошибка при работе с клиентом {address[0]}:{address[1]}: {e}"
            )
            traceback.print_exc()
        if userId:
            await self._end_session(userId, address[0], address[1])
        self.logger.info(f"Прекратил работать с клиентом {address[0]}:{address[1]}")

    async def _finish_auth(self, websocket, addr, phone, id):
        user = self.clients.get(id)
        if user:
            user["clients"].append(
                {
                    "writer": websocket,
                    "ip": addr[0],
                    "port": addr[1],
                    "protocol": "oneme",
                    "type": "web",
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
                        "writer": websocket,
                        "ip": addr[0],
                        "port": addr[1],
                        "protocol": "oneme",
                        "type": "web",
                    }
                ],
            }
        await self._broadcast_presence(id, True)

    async def _broadcast_presence(self, userId, online):
        now = int(time.time())
        now_ms = int(time.time() * 1000)
        if online:
            presence_data = {"on": "ON", "seen": now, "status": 1}
        else:
            presence_data = {"seen": now}
        async with self.db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT owner_id FROM contacts WHERE contact_id = %s", (userId,)
                )
                contact_owners = await cursor.fetchall()
        for row in contact_owners:
            owner_id = int(row.get("owner_id"))
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
        self.server = await websockets.serve(
            self.handle_client, self.host, self.port, ssl=self.ssl_context
        )
        self.logger.info(f"WebSocket запущен на порту {self.port}")
        try:
            await self.server.wait_closed()
        except asyncio.CancelledError:
            self.server.close()
            await self.server.wait_closed()
            raise
