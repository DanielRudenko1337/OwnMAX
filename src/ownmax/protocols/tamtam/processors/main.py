import json
import pydantic
from ownmax.core.baseprocessor import BaseProcessor
from ownmax.protocols.tamtam.models import HelloPayloadModel, PingPayloadModel
from ownmax.protocols.tamtam.models import UpdateProfilePayloadModel


class MainProcessors(BaseProcessor):

    async def session_init(self, payload, seq, writer):
        try:
            HelloPayloadModel.model_validate(payload)
        except pydantic.ValidationError as error:
            await self._send_error(
                seq, self.opcodes.SESSION_INIT, self.error_types.INVALID_PAYLOAD, writer
            )
            return (None, None)
        device_type = payload.get("userAgent").get("deviceType")
        device_name = payload.get("userAgent").get("deviceName")
        payload = {
            "proxy": "",
            "logs-enabled": False,
            "proxy-domains": [],
            "location": "RU",
            "libh-enabled": False,
            "phone-auto-complete-enabled": False,
        }
        packet = self.proto.pack_packet(
            cmd=self.proto.CMD_OK, seq=seq, opcode=self.opcodes.SESSION_INIT, payload=payload
        )
        await self._send(writer, packet)
        return (device_type, device_name)

    async def profile(self, payload, seq, writer, userId):
        try:
            UpdateProfilePayloadModel.model_validate(payload)
        except pydantic.ValidationError as error:
            self.logger.error(f"Возникли ошибки при валидации пакета: {error}")
            await self._send_error(
                seq, self.opcodes.PROFILE, self.error_types.INVALID_PAYLOAD, writer
            )
            return
        async with self.db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT * FROM users WHERE id = %s", (userId,))
                user = await cursor.fetchone()
                if not user:
                    await self._send_error(
                        seq, self.opcodes.PROFILE, self.error_types.USER_NOT_FOUND, writer
                    )
                    return
                photo_id = int(user["avatar_id"]) if user.get("avatar_id") else None
                avatar_url = f"{self.config.avatar_base_url}{photo_id}" if photo_id else None
                description = user.get("description")
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
        payload = {"profile": profile}
        response = self.proto.pack_packet(
            cmd=self.proto.CMD_OK, seq=seq, opcode=self.opcodes.PROFILE, payload=payload
        )
        await self._send(writer, response)

    async def update_config(self, payload, seq, writer, userPhone, hashedToken=None):
        result_payload = None
        if payload.get("pushToken"):
            push_token = payload.get("pushToken")
            async with self.db_pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        "UPDATE tokens SET push_token = %s WHERE phone = %s AND token_hash = %s",
                        (push_token, str(userPhone), hashedToken),
                    )
        elif payload.get("settings") and payload.get("settings").get("user"):
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

    async def ping(self, payload, seq, writer):
        try:
            PingPayloadModel.model_validate(payload)
        except pydantic.ValidationError as error:
            self.logger.error(f"Возникли ошибки при валидации пакета: {error}")
            await self._send_error(seq, self.opcodes.PING, self.error_types.INVALID_PAYLOAD, writer)
            return
        packet = self.proto.pack_packet(
            cmd=self.proto.CMD_OK, seq=seq, opcode=self.opcodes.PING, payload=None
        )
        await self._send(writer, packet)

    async def log(self, payload, seq, writer):
        packet = self.proto.pack_packet(
            cmd=self.proto.CMD_OK, seq=seq, opcode=self.opcodes.LOG, payload=None
        )
        await self._send(writer, packet)
