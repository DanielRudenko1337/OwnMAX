from ownmax.infrastructure.auth_delivery import issue_code
import hashlib
import secrets
import time
import json
import re
from ownmax.core.baseprocessor import BaseProcessor
from ownmax.protocols.tamtam.models import (
    RequestCodePayloadModel,
    VerifyCodePayloadModel,
    FinalAuthPayloadModel,
    AuthConfirmRegisterPayloadModel,
    LoginPayloadModel,
)
from ownmax.protocols.tamtam.config import TTConfig


class AuthProcessors(BaseProcessor):

    def __init__(self, db_pool=None, clients=None, send_event=None, type="socket"):
        super().__init__(db_pool, clients, send_event, type)
        self.server_config = TTConfig().SERVER_CONFIG

    async def _finish_auth(
        self,
        payload,
        seq,
        writer,
        cursor,
        phone,
        hashed_token,
        hashed_login,
        account,
        deviceType,
        deviceName,
        ip,
        login,
    ):
        try:
            FinalAuthPayloadModel.model_validate(payload)
        except Exception as e:
            await self._send_error(
                seq, self.opcodes.AUTH_CONFIRM, self.error_types.INVALID_PAYLOAD, writer
            )
            return None
        await cursor.execute("DELETE FROM auth_tokens WHERE token_hash = %s", (hashed_token,))
        await cursor.execute(
            "INSERT INTO tokens (phone, token_hash, device_type, device_name, location, time) VALUES (%s, %s, %s, %s, %s, %s)",
            (
                phone,
                hashed_login,
                deviceType,
                deviceName,
                self.tools.get_geo(ip=ip, db_path=self.config.geo_db_path),
                int(time.time() * 1000),
            ),
        )
        photo_id = None if not account.get("avatar_id") else int(account.get("avatar_id"))
        avatar_url = None if not photo_id else self.config.avatar_base_url + str(photo_id)
        description = None if not account.get("description") else account.get("description")
        return {
            "userToken": str(account.get("id")),
            "profile": self.tools.generate_profile_tt(
                id=account.get("id"),
                phone=int(account.get("phone")),
                avatarUrl=avatar_url,
                photoId=photo_id,
                updateTime=int(account.get("updatetime")),
                firstName=account.get("firstname"),
                lastName=account.get("lastname"),
                options=json.loads(account.get("options")),
                description=description,
                username=account.get("username"),
            ),
            "tokenType": "LOGIN",
            "token": login,
        }

    async def _finish_reg(
        self,
        payload,
        seq,
        writer,
        cursor,
        phone,
        hashed_token,
        hashed_login,
        deviceType,
        deviceName,
        ip,
        login,
    ):
        try:
            AuthConfirmRegisterPayloadModel.model_validate(payload)
        except Exception as e:
            await self._send_error(
                seq, self.opcodes.AUTH_CONFIRM, self.error_types.INVALID_PAYLOAD, writer
            )
            return None
        name = payload.get("name", "").strip()
        now_ms = int(time.time() * 1000)
        now_s = int(time.time())
        user_id = await self.tools.generate_user_id(self.db_pool)
        await cursor.execute(
            "\n            INSERT INTO users\n                (id, phone, telegram_id, firstname, lastname, username,\n                profileoptions, options, accountstatus, updatetime, lastseen)\n            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)\n            ",
            (
                user_id,
                phone,
                None,
                name,
                None,
                None,
                json.dumps([]),
                json.dumps(["TT", "ONEME"]),
                0,
                str(now_ms),
                str(now_s),
            ),
        )
        await cursor.execute(
            "\n            INSERT INTO user_data\n                (phone, user_config, chat_config)\n            VALUES (%s, %s, %s)\n            ",
            (phone, json.dumps(self.static.USER_SETTINGS), json.dumps({})),
        )
        await cursor.execute(
            "\n            INSERT INTO user_folders\n                (id, phone, title, sort_order)\n            VALUES ('all.chat.folder', %s, 'Все', 0)\n            ",
            (phone,),
        )
        await cursor.execute("DELETE FROM auth_tokens WHERE token_hash = %s", (hashed_token,))
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
        profile = self.tools.generate_profile_tt(
            id=user_id,
            phone=int(phone),
            avatarUrl=None,
            photoId=None,
            updateTime=now_ms,
            firstName=name,
            lastName="",
            options=["TT", "ONEME"],
            description=None,
            username=None,
        )
        self.logger.info(
            f"Новый пользователь зарегистрирован: phone={phone} id={user_id} name={name}"
        )
        return {"userToken": "0", "profile": profile, "tokenType": "LOGIN", "token": login}

    async def auth_request(self, payload, seq, writer):
        try:
            RequestCodePayloadModel.model_validate(payload)
        except Exception as e:
            await self._send_error(
                seq, self.opcodes.AUTH_REQUEST, self.error_types.INVALID_PAYLOAD, writer
            )
            return
        phone = re.sub("\\D", "", payload.get("phone", ""))
        if not phone.isdigit() or not 8 <= len(phone) <= 15 or phone.startswith("0"):
            await self._send_error(
                seq, self.opcodes.AUTH_REQUEST, self.error_types.INVALID_PAYLOAD, writer
            )
            return
        token = secrets.token_urlsafe(128)
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
                        "INSERT INTO auth_tokens (phone, token_hash, code_hash, expires, state) VALUES (%s, %s, %s, %s, %s)",
                        (phone, token_hash, code_hash, expires, "started"),
                    )
                else:
                    await cursor.execute(
                        "INSERT INTO auth_tokens (phone, token_hash, code_hash, expires, state) VALUES (%s, %s, %s, %s, %s)",
                        (phone, token_hash, code_hash, expires, "register"),
                    )
        payload = {
            "verifyToken": token,
            "retries": 5,
            "codeDelay": 60,
            "codeLength": len(code),
            "callDelay": 0,
            "requestType": "SMS",
        }
        packet = self.proto.pack_packet(
            cmd=self.proto.CMD_OK, seq=seq, opcode=self.opcodes.AUTH_REQUEST, payload=payload
        )
        await self._send(writer, packet)

    async def auth(self, payload, seq, writer):
        try:
            VerifyCodePayloadModel.model_validate(payload)
        except Exception as e:
            await self._send_error(seq, self.opcodes.AUTH, self.error_types.INVALID_PAYLOAD, writer)
            return
        code = payload.get("verifyCode")
        token = payload.get("token")
        hashed_code = hashlib.sha256(code.encode()).hexdigest()
        hashed_token = hashlib.sha256(token.encode()).hexdigest()
        async with self.db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT * FROM auth_tokens WHERE token_hash = %s AND expires > UNIX_TIMESTAMP()",
                    (hashed_token,),
                )
                stored_token = await cursor.fetchone()
                if not stored_token:
                    await self._send_error(
                        seq, self.opcodes.AUTH, self.error_types.CODE_EXPIRED, writer
                    )
                    return
                if stored_token.get("code_hash") != hashed_code:
                    await self._send_error(
                        seq, self.opcodes.AUTH, self.error_types.INVALID_CODE, writer
                    )
                    return
                if stored_token.get("state") == "register":
                    await cursor.execute(
                        "UPDATE auth_tokens SET state = %s WHERE token_hash = %s",
                        ("verified", hashed_token),
                    )
                    packet = self.proto.pack_packet(
                        cmd=self.proto.CMD_OK,
                        seq=seq,
                        opcode=self.opcodes.AUTH,
                        payload={
                            "tokenAttrs": {"NEW": {"token": token}},
                            "tokenTypes": {"NEW": token},
                        },
                    )
                    await self._send(writer, packet)
                    return
                await cursor.execute(
                    "SELECT * FROM users WHERE phone = %s", (stored_token.get("phone"),)
                )
                account = await cursor.fetchone()
                await cursor.execute(
                    "UPDATE auth_tokens SET state = %s WHERE token_hash = %s",
                    ("verified", hashed_token),
                )
        photo_id = int(account["avatar_id"]) if account.get("avatar_id") else None
        avatar_url = f"{self.config.avatar_base_url}{photo_id}" if photo_id else None
        description = account.get("description")
        payload = {
            "profile": self.tools.generate_profile_tt(
                id=account.get("id"),
                phone=int(account.get("phone")),
                avatarUrl=avatar_url,
                photoId=photo_id,
                updateTime=int(account.get("updatetime")),
                firstName=account.get("firstname"),
                lastName=account.get("lastname"),
                options=json.loads(account.get("options")),
                description=description,
                username=account.get("username"),
            ),
            "tokenAttrs": {"AUTH": {"token": token}},
            "tokenTypes": {"AUTH": token},
        }
        packet = self.proto.pack_packet(
            cmd=self.proto.CMD_OK, seq=seq, opcode=self.opcodes.AUTH, payload=payload
        )
        await self._send(writer, packet)

    async def auth_confirm(self, payload, seq, writer, deviceType, deviceName, ip):
        token = payload.get("token")
        if not deviceType:
            deviceType = payload.get("deviceType")
        if not deviceName:
            deviceName = "Unknown device"
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
                        seq, self.opcodes.AUTH_CONFIRM, self.error_types.INVALID_TOKEN, writer
                    )
                    return
                if (
                    stored_token.get("state") == "started"
                    or stored_token.get("state") == "register"
                ):
                    await self._send_error(
                        seq, self.opcodes.AUTH_CONFIRM, self.error_types.INVALID_TOKEN, writer
                    )
                    return
                phone = stored_token.get("phone")
                await cursor.execute("SELECT * FROM users WHERE phone = %s", (phone,))
                account = await cursor.fetchone()
                if account:
                    resp_payload = await self._finish_auth(
                        payload,
                        seq,
                        writer,
                        cursor,
                        phone,
                        hashed_token,
                        hashed_login,
                        account,
                        deviceType,
                        deviceName,
                        ip,
                        login,
                    )
                else:
                    resp_payload = await self._finish_reg(
                        payload,
                        seq,
                        writer,
                        cursor,
                        phone,
                        hashed_token,
                        hashed_login,
                        deviceType,
                        deviceName,
                        ip,
                        login,
                    )
                if resp_payload is None:
                    return
        packet = self.proto.pack_packet(
            cmd=self.proto.CMD_OK, seq=seq, opcode=self.opcodes.AUTH_CONFIRM, payload=resp_payload
        )
        await self._send(writer, packet)

    async def login(self, payload, seq, writer):
        try:
            LoginPayloadModel.model_validate(payload)
        except Exception as e:
            self.logger.error(f"Возникли ошибки при валидации пакета: {e}")
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
        photo_id = None if not user.get("avatar_id") else int(user.get("avatar_id"))
        avatar_url = None if not photo_id else self.config.avatar_base_url + str(photo_id)
        description = None if not user.get("description") else user.get("description")
        profile = self.tools.generate_profile_tt(
            id=user.get("id"),
            phone=int(user.get("phone")),
            avatarUrl=avatar_url,
            photoId=photo_id,
            updateTime=int(user.get("updatetime")),
            firstName=user.get("firstname"),
            lastName=user.get("lastname"),
            options=json.loads(user.get("options")),
            description=description,
            username=user.get("username"),
        )
        chats = await self.tools.generate_chats(
            chats, self.db_pool, user.get("id"), include_favourites=False
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
                "chatFolders": {"FOLDERS": [], "ALL_FILTER_EXCLUDE": []},
            },
            "token": token,
            "calls": [],
            "videoChatHistory": False,
            "drafts": {
                "chats": {"discarded": {}, "saved": {}},
                "users": {"discarded": {}, "saved": {}},
            },
            "time": int(time.time() * 1000),
        }
        packet = self.proto.pack_packet(
            cmd=self.proto.CMD_OK, seq=seq, opcode=self.opcodes.LOGIN, payload=payload
        )
        await self._send(writer, packet)
        return (int(user.get("phone")), int(user.get("id")), hashed_token)

    async def logout(self, seq, writer, hashedToken):
        async with self.db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("DELETE FROM tokens WHERE token_hash = %s", (hashedToken,))
        response = self.proto.pack_packet(
            cmd=self.proto.CMD_OK, seq=seq, opcode=self.opcodes.LOGOUT, payload=None
        )
        await self._send(writer, response)
