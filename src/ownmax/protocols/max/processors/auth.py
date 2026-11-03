from ownmax.infrastructure.auth_delivery import issue_code
import hashlib
import json
import secrets
import time
import pydantic
from ownmax.core.baseprocessor import BaseProcessor
from ownmax.protocols.max.config import OnemeConfig
from ownmax.protocols.max.models import (
    AuthConfirmRegisterPayloadModel,
    LoginPayloadModel,
    RequestCodePayloadModel,
    VerifyCodePayloadModel,
)


class AuthProcessors(BaseProcessor):

    def __init__(
        self, db_pool=None, clients=None, send_event=None, telegram_bot=None, type="socket"
    ):
        super().__init__(db_pool, clients, send_event, type)
        self.server_config = OnemeConfig().SERVER_CONFIG
        self.telegram_bot = telegram_bot

    def _check_legacy_version(self, app_version):
        return tuple((int(v) for v in app_version.split("."))) < (25, 8, 0)

    async def _send_banners(self, writer):
        banners = []
        async with self.db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT * FROM banners WHERE enabled = TRUE")
                rows = await cursor.fetchall()
                for row in rows:
                    banner = {
                        "description": row.get("description"),
                        "title": row.get("title"),
                        "priority": row.get("priority"),
                        "type": row.get("type"),
                        "hideCloseButton": bool(row.get("hide_close_button")),
                        "rerun": row.get("rerun"),
                        "url": row.get("url"),
                        "animojiId": row.get("animoji_id"),
                        "repeat": row.get("repeat"),
                        "hideOnClick": bool(row.get("hide_on_click")),
                        "id": row.get("id"),
                        "isTitleAnimated": bool(row.get("is_title_animated")),
                    }
                    banners.append(banner)
        payload = {"showTime": 86400000, "updateTime": int(time.time() * 1000), "banners": banners}
        packet = self.proto.pack_packet(cmd=0, opcode=self.opcodes.NOTIF_BANNERS, payload=payload)
        await self._send(writer, packet)

    async def auth_request(self, payload, seq, writer):
        try:
            RequestCodePayloadModel.model_validate(payload)
        except pydantic.ValidationError as error:
            self.logger.error(f"Возникли ошибки при валидации пакета: {error}")
            await self._send_error(
                seq, self.opcodes.AUTH_REQUEST, self.error_types.INVALID_PAYLOAD, writer
            )
            return
        phone = payload.get("phone").replace("+", "").replace(" ", "").replace("-", "")
        if not phone.isdigit() or not 8 <= len(phone) <= 15 or phone.startswith("0"):
            await self._send_error(seq, 17, self.error_types.INVALID_PAYLOAD, writer)
            return
        from ownmax.protocols.max.security_bot import rate_allowed

        if not rate_allowed(phone):
            await self._send_error(seq, 17, self.error_types.RATE_LIMITED, writer)
            return
        token = secrets.token_urlsafe(102)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        expires = int(time.time()) + 300
        async with self.db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT * FROM users WHERE phone = %s", (phone,))
                user = await cursor.fetchone()
        code = issue_code(phone)
        code_hash = hashlib.sha256(code.encode()).hexdigest()
        async with self.db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                if user:
                    await cursor.execute(
                        "INSERT INTO auth_tokens (phone, token_hash, code_hash, expires) VALUES (%s, %s, %s, %s)",
                        (phone, token_hash, code_hash, expires),
                    )
                else:
                    await cursor.execute(
                        "INSERT INTO auth_tokens (phone, token_hash, code_hash, expires, state) VALUES (%s, %s, %s, %s, %s)",
                        (phone, token_hash, code_hash, expires, "register"),
                    )
        payload = {
            "token": token,
            "codeLength": len(code),
            "requestMaxDuration": 60000,
            "requestCountLeft": 10,
            "altActionDuration": 60000,
        }
        packet = self.proto.pack_packet(
            cmd=self.proto.CMD_OK, seq=seq, opcode=self.opcodes.AUTH_REQUEST, payload=payload
        )
        await self._send(writer, packet)
        if user:
            from ownmax.protocols.max.security_bot import send

            await send(
                self,
                int(user["id"]),
                "Код для входа: "
                + code
                + "\n\nНикому не сообщайте этот код. Он действует 5 минут.",
            )

    async def auth(self, payload, seq, writer, deviceType, deviceName, appVersion, ip):
        try:
            VerifyCodePayloadModel.model_validate(payload)
        except pydantic.ValidationError as error:
            self.logger.error(f"Возникли ошибки при валидации пакета: {error}")
            await self._send_error(seq, self.opcodes.AUTH, self.error_types.INVALID_PAYLOAD, writer)
            return
        code = payload.get("verifyCode")
        token = payload.get("token")
        hashed_code = hashlib.sha256(code.encode()).hexdigest()
        hashed_token = hashlib.sha256(token.encode()).hexdigest()
        login = secrets.token_urlsafe(128)
        hashed_login = hashlib.sha256(login.encode()).hexdigest()
        async with self.db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT * FROM auth_tokens WHERE token_hash = %s AND expires > UNIX_TIMESTAMP()",
                    (hashed_token,),
                )
                stored_token = await cursor.fetchone()
                if stored_token is None:
                    await self._send_error(
                        seq, self.opcodes.AUTH, self.error_types.CODE_EXPIRED, writer
                    )
                    return
                if int(stored_token.get("attempts") or 0) >= 5:
                    await cursor.execute(
                        "DELETE FROM auth_tokens WHERE token_hash=%s", (hashed_token,)
                    )
                    await self._send_error(seq, 18, self.error_types.CODE_EXPIRED, writer)
                    return
                if not secrets.compare_digest(stored_token.get("code_hash") or "", hashed_code):
                    await cursor.execute(
                        "UPDATE auth_tokens SET attempts=attempts+1 WHERE token_hash=%s",
                        (hashed_token,),
                    )
                    await self._send_error(
                        seq, self.opcodes.AUTH, self.error_types.INVALID_CODE, writer
                    )
                    return
                if stored_token.get("state") in ("register", "verified"):
                    await cursor.execute(
                        "UPDATE auth_tokens SET state = %s WHERE token_hash = %s",
                        ("verified", hashed_token),
                    )
                    packet = self.proto.pack_packet(
                        cmd=self.proto.CMD_OK,
                        seq=seq,
                        opcode=self.opcodes.AUTH,
                        payload={"tokenAttrs": {"REGISTER": {"token": token}}, "presetAvatars": []},
                    )
                    await self._send(writer, packet)
                    return
                await cursor.execute(
                    "SELECT * FROM users WHERE phone = %s", (stored_token.get("phone"),)
                )
                account = await cursor.fetchone()
                from ownmax.protocols.max.account_security import challenge

                if await challenge(
                    self, account, payload, seq, writer, (deviceType, deviceName, appVersion, ip)
                ):
                    return
                await cursor.execute(
                    "DELETE FROM auth_tokens WHERE token_hash = %s", (hashed_token,)
                )
                await cursor.execute(
                    "INSERT INTO tokens (phone, token_hash, device_type, device_name, location, time) VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        stored_token.get("phone"),
                        hashed_login,
                        deviceType,
                        deviceName,
                        self.tools.get_geo(ip=ip, db_path=self.config.geo_db_path),
                        int(time.time() * 1000),
                    ),
                )
        from ownmax.protocols.max.session_metadata import store

        await store(self, hashed_login, ip, writer, deviceType, deviceName, appVersion)
        from ownmax.protocols.max.security_bot import new_login

        await new_login(self, int(account["id"]))
        photoId = None if not account.get("avatar_id") else int(account.get("avatar_id"))
        avatar_url = (
            None
            if not photoId
            else self.tools.profile_avatar_url(photoId, self.config.avatar_base_url)
        )
        description = None if not account.get("description") else account.get("description")
        if self._check_legacy_version(appVersion):
            include_profile_options = False
        else:
            include_profile_options = True
        payload = {
            "tokenAttrs": {"LOGIN": {"token": login}},
            "profile": self.tools.generate_profile(
                id=account.get("id"),
                phone=int(account.get("phone")),
                avatarUrl=avatar_url,
                photoId=photoId,
                updateTime=int(account.get("updatetime")),
                firstName=account.get("firstname"),
                lastName=account.get("lastname"),
                options=json.loads(account.get("options")),
                description=description,
                accountStatus=int(account.get("accountstatus")),
                profileOptions=json.loads(account.get("profileoptions")),
                includeProfileOptions=include_profile_options,
                username=account.get("username"),
            ),
        }
        packet = self.proto.pack_packet(
            cmd=self.proto.CMD_OK, seq=seq, opcode=self.opcodes.AUTH, payload=payload
        )
        await self._send(writer, packet)

    async def auth_confirm(self, payload, seq, writer, deviceType, deviceName, appVersion, ip):
        try:
            AuthConfirmRegisterPayloadModel.model_validate(payload)
        except pydantic.ValidationError as error:
            self.logger.error(f"Возникли ошибки при валидации пакета: {error}")
            await self._send_error(
                seq, self.opcodes.AUTH_CONFIRM, self.error_types.INVALID_PAYLOAD, writer
            )
            return
        token = payload.get("token")
        first_name = payload.get("firstName").strip()
        last_name = (payload.get("lastName") or "").strip()
        if not first_name or len(first_name) > 100 or len(last_name) > 100:
            await self._send_error(seq, 23, self.error_types.INVALID_PAYLOAD, writer)
            return
        hashed_token = hashlib.sha256(token.encode()).hexdigest()
        login = secrets.token_urlsafe(128)
        hashed_login = hashlib.sha256(login.encode()).hexdigest()
        async with self.db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT * FROM auth_tokens WHERE token_hash = %s AND expires > UNIX_TIMESTAMP() AND state = %s",
                    (hashed_token, "verified"),
                )
                stored_token = await cursor.fetchone()
                if stored_token is None:
                    await self._send_error(
                        seq, self.opcodes.AUTH_CONFIRM, self.error_types.CODE_EXPIRED, writer
                    )
                    return
                phone = stored_token.get("phone")
                await cursor.execute("SELECT id FROM users WHERE phone = %s", (phone,))
                if await cursor.fetchone():
                    await self._send_error(
                        seq, self.opcodes.AUTH_CONFIRM, self.error_types.INVALID_PAYLOAD, writer
                    )
                    return
                now_ms = int(time.time() * 1000)
                now_s = int(time.time())
                user_id = await self.tools.generate_user_id(self.db_pool)
                await cursor.execute(
                    "\n                    INSERT INTO users\n                        (id, phone, telegram_id, firstname, lastname, username,\n                        profileoptions, options, accountstatus, updatetime, lastseen)\n                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)\n                    ",
                    (
                        user_id,
                        phone,
                        None,
                        first_name,
                        last_name,
                        None,
                        json.dumps([]),
                        json.dumps(["TT", "ONEME"]),
                        0,
                        str(now_ms),
                        str(now_s),
                    ),
                )
                await cursor.execute(
                    "\n                    INSERT INTO user_data\n                        (phone, user_config, chat_config)\n                    VALUES (%s, %s, %s)\n                    ",
                    (phone, json.dumps(self.static.USER_SETTINGS), json.dumps({})),
                )
                await cursor.execute(
                    "\n                    INSERT INTO user_folders \n                        (id, phone, title, sort_order)\n                    VALUES ('all.chat.folder', %s, 'Все', 0)\n                    ",
                    (phone,),
                )
                await cursor.execute(
                    "DELETE FROM auth_tokens WHERE token_hash = %s", (hashed_token,)
                )
                await cursor.execute(
                    "INSERT INTO tokens (phone, token_hash, device_type, device_name, location, time) VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        phone,
                        hashed_login,
                        deviceType or "ANDROID",
                        deviceName or "Unknown",
                        self.tools.get_geo(ip=ip, db_path=self.config.geo_db_path),
                        now_ms,
                    ),
                )
        from ownmax.protocols.max.session_metadata import store

        await store(self, hashed_login, ip, writer, deviceType, deviceName, appVersion)
        from ownmax.protocols.max.security_bot import new_login

        await new_login(self, user_id)
        if self._check_legacy_version(appVersion):
            include_profile_options = False
        else:
            include_profile_options = True
        profile = self.tools.generate_profile(
            id=user_id,
            phone=int(phone),
            avatarUrl=None,
            photoId=None,
            updateTime=now_ms,
            firstName=first_name,
            lastName=last_name,
            options=["ONEME"],
            description=None,
            accountStatus=0,
            profileOptions=[],
            includeProfileOptions=include_profile_options,
            username=None,
        )
        payload = {"userToken": "0", "profile": profile, "tokenType": "LOGIN", "token": login}
        packet = self.proto.pack_packet(
            cmd=self.proto.CMD_OK, seq=seq, opcode=self.opcodes.AUTH_CONFIRM, payload=payload
        )
        await self._send(writer, packet)
        self.logger.info(
            f"Новый пользователь зарегистрирован: phone={phone} id={user_id} name={first_name} {last_name}"
        )

    async def login(self, payload, seq, writer, appVersion):
        try:
            LoginPayloadModel.model_validate(payload)
        except pydantic.ValidationError as error:
            self.logger.error(f"Возникли ошибки при валидации пакета: {error}")
            await self._send_error(
                seq, self.opcodes.LOGIN, self.error_types.INVALID_PAYLOAD, writer
            )
            return (None, None, None)
        chats = []
        token = payload.get("token")
        hashed_token = hashlib.sha256(token.encode()).hexdigest()
        async with self.db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT * FROM tokens WHERE token_hash = %s", (hashed_token,))
                token_data = await cursor.fetchone()
                if token_data is None:
                    await self._send_error(
                        seq, self.opcodes.LOGIN, self.error_types.INVALID_TOKEN, writer
                    )
                    return (None, None, None)
                await cursor.execute(
                    "SELECT * FROM users WHERE phone = %s", (token_data.get("phone"),)
                )
                user = await cursor.fetchone()
                await cursor.execute(
                    "SELECT * FROM user_data WHERE phone = %s", (token_data.get("phone"),)
                )
                user_data = await cursor.fetchone()
                await cursor.execute(
                    "SELECT * FROM chat_participants WHERE user_id = %s", (user.get("id"),)
                )
                user_chats = await cursor.fetchall()
                for chat in user_chats:
                    chats.append(chat.get("chat_id"))
                updated_user_config = await self.tools.update_user_config(
                    cursor,
                    token_data.get("phone"),
                    user_data.get("user_config"),
                    self.static.USER_SETTINGS,
                )
        photoId = None if not user.get("avatar_id") else int(user.get("avatar_id"))
        avatar_url = (
            None
            if not photoId
            else self.tools.profile_avatar_url(photoId, self.config.avatar_base_url)
        )
        description = None if not user.get("description") else user.get("description")
        if self._check_legacy_version(appVersion):
            include_profile_options = False
        else:
            include_profile_options = True
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
            includeProfileOptions=include_profile_options,
            username=user.get("username"),
        )
        chats = await self.tools.generate_chats(
            chats, self.db_pool, user.get("id"), protocol_type=self.type
        )
        contacts = await self.tools.collect_user_contacts(
            user.get("id"), self.db_pool, self.config.avatar_base_url
        )
        contact_ids = [c.get("id") for c in contacts if c.get("id") is not None]
        presence = await self.tools.collect_presence(contact_ids, self.clients, self.db_pool)
        payload = {
            "profile": profile,
            "chats": chats,
            "chatMarker": 0,
            "messages": {},
            "contacts": contacts,
            "presence": presence,
            "config": {
                "hash": "0",
                "server": self.server_config,
                "user": updated_user_config,
                "chats": json.loads(user_data.get("chat_config") or "{}"),
                "experiments": {},
            },
            "token": token,
            "videoChatHistory": False,
            "calls": [],
            "time": int(time.time() * 1000),
        }
        packet = self.proto.pack_packet(
            cmd=self.proto.CMD_OK, seq=seq, opcode=self.opcodes.LOGIN, payload=payload
        )
        await self._send(writer, packet)
        await self._send_banners(writer)
        return (int(user.get("phone")), int(user.get("id")), hashed_token)

    async def logout(self, seq, writer, hashedToken):
        async with self.db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("DELETE FROM tokens WHERE token_hash = %s", (hashedToken,))
        response = self.proto.pack_packet(
            cmd=self.proto.CMD_OK, seq=seq, opcode=self.opcodes.LOGOUT, payload=None
        )
        await self._send(writer, response)

    async def sync(self, payload, seq, writer, userId):
        async with self.db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT chat_id FROM chat_participants WHERE user_id = %s", (userId,)
                )
                rows = await cursor.fetchall()
        chats = await self.tools.generate_chats(
            [row["chat_id"] for row in rows], self.db_pool, userId, protocol_type=self.type
        )
        now = int(time.time() * 1000)
        packet = self.proto.pack_packet(
            cmd=self.proto.CMD_OK,
            seq=seq,
            opcode=self.opcodes.SYNC,
            payload={"chats": chats, "messages": {}, "marker": now, "time": now},
        )
        await self._send(writer, packet)

    async def qr_auth_operation(
        self, payload, seq, writer, opcode, userId, deviceType, deviceName, appVersion, ip
    ):
        from ownmax.protocols.max.qr_auth import operation

        await operation(
            self, payload, seq, writer, opcode, userId, deviceType, deviceName, appVersion, ip
        )
