import pydantic
import json
import time
from ownmax.core.baseprocessor import BaseProcessor
from ownmax.protocols.max.models import (
    HelloPayloadModel,
    PingPayloadModel,
    UpdateProfilePayloadModel,
)


class MainProcessors(BaseProcessor):

    def __init__(self, db_pool=None, clients=None, send_event=None, type="socket"):
        super().__init__(db_pool, clients, send_event, type)

    async def session_init(self, payload, seq, writer):
        try:
            HelloPayloadModel.model_validate(payload)
        except pydantic.ValidationError as error:
            self.logger.error(f"Возникли ошибки при валидации пакета: {error}")
            await self._send_error(
                seq, self.opcodes.SESSION_INIT, self.error_types.INVALID_PAYLOAD, writer
            )
            return (None, None, None)
        userAgent = payload.get("userAgent")
        deviceType = userAgent.get("deviceType")
        deviceName = userAgent.get("deviceName")
        appVersion = userAgent.get("appVersion")
        try:
            writer._session_agent = {
                str(k): v
                for k, v in userAgent.items()
                if isinstance(v, (str, int, bool)) and len(str(v)) <= 300
            }
        except (AttributeError, TypeError):
            pass
        payload = {
            "callsSeed": int(time.time() * 1000),
            "location": "RU",
            "app-update-type": 0,
            "reg-country-code": self.static.REG_COUNTRY_CODES,
            "phone-auto-complete-enabled": False,
            "qr-auth-enabled": True,
            "lang": True,
        }
        packet = self.proto.pack_packet(
            cmd=self.proto.CMD_OK, seq=seq, opcode=self.opcodes.SESSION_INIT, payload=payload
        )
        await self._send(writer, packet)
        return (deviceType, deviceName, appVersion)

    async def ping(self, payload, seq, writer, userId=None):
        try:
            PingPayloadModel.model_validate(payload)
        except pydantic.ValidationError as error:
            self.logger.error(f"Возникли ошибки при валидации пакета: {error}")
            await self._send_error(seq, self.opcodes.PING, self.error_types.INVALID_PAYLOAD, writer)
            return
        interactive = payload.get("interactive") if payload else None
        if userId and interactive is not None:
            now = int(time.time())
            user = self.clients.get(userId)
            if user:
                for connection in user.get("clients", []):
                    if connection.get("writer") is writer:
                        connection["interactive"] = bool(interactive)
                interactive = self.tools.presence_is_online(user)
                user["status"] = 2 if interactive else 0
                user["last_seen"] = now
                async with self.db_pool.acquire() as conn:
                    async with conn.cursor() as cursor:
                        await cursor.execute(
                            "UPDATE users SET lastseen = %s WHERE id = %s", (str(now), userId)
                        )
                now_ms = int(time.time() * 1000)
                if interactive:
                    presence_data = {"on": "ON", "seen": now, "status": 1}
                else:
                    presence_data = {"seen": now, "on": "OFF", "status": 0}
                async with self.db_pool.acquire() as conn2:
                    async with conn2.cursor() as cursor2:
                        await cursor2.execute(
                            "SELECT owner_id FROM contacts WHERE contact_id = %s", (userId,)
                        )
                        contact_owners = await cursor2.fetchall()
                async with self.db_pool.acquire() as conn2:
                    async with conn2.cursor() as cursor2:
                        await cursor2.execute(
                            "SELECT DISTINCT user_id AS owner_id FROM chat_participants WHERE chat_id IN (SELECT chat_id FROM chat_participants WHERE user_id = %s) AND user_id != %s",
                            (userId, userId),
                        )
                        dialog_owners = await cursor2.fetchall()
                for row in {int(r["owner_id"]): r for r in contact_owners + dialog_owners}.values():
                    owner_id = int(row.get("owner_id"))
                    if owner_id in self.clients:
                        await self.event(
                            owner_id,
                            {
                                "eventType": "presence",
                                "userId": userId,
                                "presence": presence_data,
                                "time": now_ms,
                            },
                        )
        response = self.proto.pack_packet(
            cmd=self.proto.CMD_OK, seq=seq, opcode=self.opcodes.PING, payload=None
        )
        await self._send(writer, response)

    async def log(self, payload, seq, writer):
        response = self.proto.pack_packet(
            cmd=self.proto.CMD_OK, seq=seq, opcode=self.opcodes.LOG, payload=None
        )
        await self._send(writer, response)

    async def profile(self, payload, seq, writer, userId):
        try:
            UpdateProfilePayloadModel.model_validate(payload)
        except pydantic.ValidationError as error:
            self.logger.error(f"Возникли ошибки при валидации пакета: {error}")
            await self._send_error(
                seq, self.opcodes.PROFILE, self.error_types.INVALID_PAYLOAD, writer
            )
            return
        if not userId:
            await self._send_error(
                seq, self.opcodes.PROFILE, self.error_types.INVALID_PAYLOAD, writer
            )
            return
        changes = {}
        for field, column, limit in (
            ("firstName", "firstname", 64),
            ("lastName", "lastname", 64),
            ("description", "description", 1024),
        ):
            if field in payload and payload[field] is not None:
                value = payload[field].strip()
                if len(value) > limit or (field == "firstName" and (not value)):
                    await self._send_error(
                        seq, self.opcodes.PROFILE, self.error_types.INVALID_PAYLOAD, writer
                    )
                    return
                changes[column] = value
        if "photoToken" in payload and payload["photoToken"] is not None:
            try:
                if payload["photoToken"] == "":
                    changes["avatar_id"] = None
                else:
                    from ownmax.protocols.max.media_service import enrich, load

                    avatar = enrich(
                        [{"_type": "PHOTO", "photoToken": payload["photoToken"]}], userId
                    )[0]
                    if avatar["_type"] != "PHOTO":
                        raise ValueError()
                    media = load(str(avatar["photoId"]))
                    if media.get("mime") not in ("image/png", "image/jpeg"):
                        raise ValueError()
                    changes["avatar_id"] = avatar["photoId"]
            except (ValueError, TypeError, KeyError):
                await self._send_error(
                    seq, self.opcodes.PROFILE, self.error_types.INVALID_PAYLOAD, writer
                )
                return
        if changes:
            changes["updatetime"] = int(time.time() * 1000)
            async with self.db_pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    fields = ", ".join((column + " = %s" for column in changes))
                    await cursor.execute(
                        "UPDATE users SET " + fields + " WHERE id = %s",
                        tuple(changes.values()) + (userId,),
                    )
        async with self.db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT * FROM users WHERE id = %s", (userId,))
                user = await cursor.fetchone()
                if not user:
                    await self._send_error(
                        seq, self.opcodes.PROFILE, self.error_types.USER_NOT_FOUND, writer
                    )
                    return
                photoId = None if not user.get("avatar_id") else int(user.get("avatar_id"))
                avatar_url = (
                    None
                    if not photoId
                    else self.tools.profile_avatar_url(photoId, self.config.avatar_base_url)
                )
                description = None if not user.get("description") else user.get("description")
                profile = self.tools.generate_profile(
                    id=user.get("id"),
                    phone=int(user.get("phone")),
                    avatarUrl=avatar_url,
                    photoId=photoId,
                    updateTime=int(user.get("updatetime")),
                    firstName=user.get("firstname"),
                    lastName=user.get("lastname"),
                    options=json.loads(user.get("options")),
                    description=description,
                    accountStatus=int(user.get("accountstatus")),
                    profileOptions=json.loads(user.get("profileoptions")),
                    includeProfileOptions=True,
                    username=user.get("username"),
                )
        if "photoToken" in payload and payload["photoToken"] == "":
            profile["contact"].update(photoId=0, baseUrl="", baseRawUrl="")
        if changes:
            await self.event(
                userId, {"eventType": "profile_updated", "profile": profile, "writer": writer}
            )
            for recipient in list(self.clients):
                if recipient != userId:
                    await self.event(
                        recipient, {"eventType": "contact_updated", "contact": profile["contact"]}
                    )
        payload = {"profile": profile}
        response = self.proto.pack_packet(
            cmd=self.proto.CMD_OK, seq=seq, opcode=self.opcodes.PROFILE, payload=payload
        )
        await self._send(writer, response)

    async def update_config(self, payload, seq, writer, userPhone, hashedToken=None):
        result_payload = None
        chat_settings = (payload.get("settings") or {}).get("chats")
        if isinstance(chat_settings, dict):
            try:
                if len(chat_settings) > 100 or len(json.dumps(chat_settings)) > 65536:
                    raise ValueError()
                async with self.db_pool.acquire() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute("SELECT id FROM users WHERE phone=%s", (str(userPhone),))
                        uid = int((await cur.fetchone())["id"])
                        for key, value in chat_settings.items():
                            cid = int(key)
                            if not isinstance(value, dict):
                                raise ValueError()
                            if cid not in (
                                0,
                                -uid,
                            ) and uid not in await self.tools.get_chat_participants(
                                cid, self.db_pool
                            ):
                                raise ValueError()
                        await cur.execute(
                            "SELECT chat_config FROM user_data WHERE phone=%s", (str(userPhone),)
                        )
                        row = await cur.fetchone()
                        config = json.loads(row["chat_config"] or "{}")
                        for key, value in chat_settings.items():
                            config.setdefault(str(key), {}).update(value)
                        await cur.execute(
                            "UPDATE user_data SET chat_config=%s WHERE phone=%s",
                            (json.dumps(config), str(userPhone)),
                        )
                result_payload = {
                    "chats": config,
                    "hash": str(__import__("time").time_ns() // 1000000),
                }
                await self.event(
                    uid, {"eventType": "raw", "opcode": 134, "payload": result_payload}
                )
            except (ValueError, TypeError, KeyError):
                await self._send_error(seq, 22, self.error_types.INVALID_PAYLOAD, writer)
                return
        if payload.get("pushToken"):
            push_token = payload.get("pushToken")
            async with self.db_pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        "UPDATE tokens SET push_token = %s WHERE phone = %s AND token_hash = %s",
                        (push_token, str(userPhone), hashedToken),
                    )
        elif payload.get("settings") and payload.get("settings").get("user"):
            "Обновление настроек приватности"
            new_settings = payload.get("settings").get("user")
            async with self.db_pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        "SELECT user_config FROM user_data WHERE phone = %s", (userPhone,)
                    )
                    row = await cursor.fetchone()
                    if row:
                        current_config = json.loads(row.get("user_config"))
                        for key, value in new_settings.items():
                            if key in current_config:
                                current_config[key] = value
                        await cursor.execute(
                            "UPDATE user_data SET user_config = %s WHERE phone = %s",
                            (json.dumps(current_config), userPhone),
                        )
                        result_payload = {"user": current_config, "hash": "0"}
        response = self.proto.pack_packet(
            cmd=self.proto.CMD_OK, seq=seq, opcode=self.opcodes.CONFIG, payload=result_payload
        )
        await self._send(writer, response)

    async def private_account_security(self, payload, seq, writer, userId, opcode):
        from ownmax.protocols.max.account_security import operation

        await operation(self, opcode, payload, seq, writer, userId)

    async def private_profile_photos(self, payload, seq, writer, userId, opcode):
        try:
            target = int(
                (payload or {}).get("contactId") or (payload or {}).get("userId") or userId
            )
            if opcode == 43:
                async with self.db_pool.acquire() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute("SELECT avatar_id FROM users WHERE id=%s", (userId,))
                        row = await cur.fetchone()
                requested = (payload or {}).get("photoId")
                if requested is not None and str(requested) != str(row.get("avatar_id")):
                    raise ValueError()

                class Capture:

                    def __init__(self):
                        self.data = []

                    def write(self, v):
                        self.data.append(v)

                    async def drain(self):
                        pass

                cap = Capture()
                await self.profile({"photoToken": ""}, seq, cap, userId)
                for raw in cap.data:
                    v = self.proto.unpack_packet(raw)
                    await self._send(
                        writer,
                        self.proto.pack_packet(
                            cmd=v["cmd"], seq=seq, opcode=43, payload=v["payload"]
                        ),
                    )
                return
            async with self.db_pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT avatar_id FROM users WHERE id=%s", (target,))
                    row = await cur.fetchone()
            photos = []
            if row and row.get("avatar_id"):
                mid = int(row["avatar_id"])
                url = self.tools.profile_avatar_url(mid, self.config.avatar_base_url)
                photos = [{"id": mid, "photoId": mid, "baseUrl": url, "baseRawUrl": url}]
            await self._send(
                writer,
                self.proto.pack_packet(
                    cmd=1,
                    seq=seq,
                    opcode=opcode,
                    payload={"photos": photos, "marker": 0, "hasMore": False},
                ),
            )
        except (ValueError, TypeError, KeyError):
            await self._send_error(seq, opcode, self.error_types.INVALID_PAYLOAD, writer)
