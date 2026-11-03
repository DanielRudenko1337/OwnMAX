import json
import pydantic
from ownmax.core.baseprocessor import BaseProcessor
from ownmax.protocols.max.models import (
    SearchUsersPayloadModel,
    SearchChatsPayloadModel,
    SearchByPhonePayloadModel,
)


class SearchProcessors(BaseProcessor):

    async def contact_info(self, payload, seq, writer, senderId):
        try:
            SearchUsersPayloadModel.model_validate(payload)
        except pydantic.ValidationError as error:
            self.logger.error(f"Возникли ошибки при валидации пакета: {error}")
            await self._send_error(
                seq, self.opcodes.CONTACT_INFO, self.error_types.INVALID_PAYLOAD, writer
            )
            return
        users = []
        contactIds = payload.get("contactIds")
        async with self.db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                for contactId in contactIds:
                    await cursor.execute("SELECT * FROM users WHERE id = %s", (contactId,))
                    user = await cursor.fetchone()
                    if user:
                        photoId = None if not user.get("avatar_id") else int(user.get("avatar_id"))
                        avatar_url = (
                            None
                            if not photoId
                            else self.tools.profile_avatar_url(photoId, self.config.avatar_base_url)
                        )
                        description = (
                            None if not user.get("description") else user.get("description")
                        )
                        await cursor.execute(
                            "SELECT * FROM contacts WHERE owner_id = %s AND contact_id = %s",
                            (senderId, contactId),
                        )
                        contact_row = await cursor.fetchone()
                        custom_firstname = (
                            contact_row.get("custom_firstname") if contact_row else None
                        )
                        custom_lastname = (
                            contact_row.get("custom_lastname") if contact_row else None
                        )
                        blocked = bool(contact_row.get("is_blocked")) if contact_row else False
                        users.append(
                            self.tools.generate_profile(
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
                                includeProfileOptions=False,
                                username=user.get("username"),
                                custom_firstname=custom_firstname,
                                custom_lastname=custom_lastname,
                                blocked=blocked,
                            )
                        )
        payload = {"contacts": users}
        response = self.proto.pack_packet(
            cmd=self.proto.CMD_OK, seq=seq, opcode=self.opcodes.CONTACT_INFO, payload=payload
        )
        await self._send(writer, response)

    async def contact_info_by_phone(self, payload, seq, writer, senderId):
        try:
            SearchByPhonePayloadModel.model_validate(payload)
        except Exception as e:
            await self._send_error(
                seq, self.opcodes.CONTACT_INFO_BY_PHONE, self.error_types.INVALID_PAYLOAD, writer
            )
            return
        async with self.db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT * FROM users WHERE phone = %s", (int(payload.get("phone")),)
                )
                user = await cursor.fetchone()
                if not user:
                    await self._send_error(
                        seq,
                        self.opcodes.CONTACT_INFO_BY_PHONE,
                        self.error_types.USER_NOT_FOUND,
                        writer,
                    )
                    return
                chatId = senderId ^ user.get("id")
                await cursor.execute("SELECT * FROM chats WHERE id = %s", (chatId,))
                chat = await cursor.fetchone()
                if not chat:
                    await cursor.execute(
                        "INSERT INTO chats (id, owner, type) VALUES (%s, %s, %s)",
                        (chatId, senderId, "DIALOG"),
                    )
                    participants = [int(senderId), int(user.get("id"))]
                    for user_id in participants:
                        await cursor.execute(
                            "INSERT INTO chat_participants (chat_id, user_id) VALUES (%s, %s)",
                            (chatId, user_id),
                        )
        photoId = None if not user.get("avatar_id") else int(user.get("avatar_id"))
        avatar_url = (
            None
            if not photoId
            else self.tools.profile_avatar_url(photoId, self.config.avatar_base_url)
        )
        description = None if not user.get("description") else user.get("description")
        async with self.db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT * FROM contacts WHERE owner_id = %s AND contact_id = %s",
                    (senderId, user.get("id")),
                )
                contact_row = await cursor.fetchone()
        custom_firstname = contact_row.get("custom_firstname") if contact_row else None
        custom_lastname = contact_row.get("custom_lastname") if contact_row else None
        blocked = bool(contact_row.get("is_blocked")) if contact_row else False
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
            includeProfileOptions=False,
            username=user.get("username"),
            custom_firstname=custom_firstname,
            custom_lastname=custom_lastname,
            blocked=blocked,
        )
        payload = {"contact": profile}
        response = self.proto.pack_packet(
            cmd=self.proto.CMD_OK,
            seq=seq,
            opcode=self.opcodes.CONTACT_INFO_BY_PHONE,
            payload=payload,
        )
        await self._send(writer, response)

    async def chat_info(self, payload, seq, writer, senderId):
        try:
            SearchChatsPayloadModel.model_validate(payload)
        except pydantic.ValidationError as error:
            self.logger.error(f"Возникли ошибки при валидации пакета: {error}")
            await self._send_error(
                seq, self.opcodes.CHAT_INFO, self.error_types.INVALID_PAYLOAD, writer
            )
            return
        requested = payload.get("chatIds") or []
        visible = []
        for chatid in requested:
            if int(chatid) == 0:
                continue
            members = await self.tools.get_chat_participants(int(chatid), self.db_pool)
            if int(senderId) in members:
                visible.append(int(chatid))
        chats = await self.tools.generate_chats(
            visible,
            self.db_pool,
            senderId,
            include_favourites=0 in requested,
            protocol_type=self.type,
        )
        payload = {"chats": chats}
        response = self.proto.pack_packet(
            cmd=self.proto.CMD_OK, seq=seq, opcode=self.opcodes.CHAT_INFO, payload=payload
        )
        await self._send(writer, response)
