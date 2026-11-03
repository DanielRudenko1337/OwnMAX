import pydantic
from ownmax.core.baseprocessor import BaseProcessor
from ownmax.protocols.tamtam.models import TypingPayloadModel, SendMessagePayloadModel


class MessagesProcessors(BaseProcessor):

    async def msg_typing(self, payload, seq, writer, senderId):
        try:
            TypingPayloadModel.model_validate(payload)
        except pydantic.ValidationError as error:
            self.logger.error(f"Возникли ошибки при валидации пакета: {error}")
            await self._send_error(
                seq, self.opcodes.MSG_TYPING, self.error_types.INVALID_PAYLOAD, writer
            )
            return
        chatId = payload.get("chatId")
        type = payload.get("type") or "TYPING"
        async with self.db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT * FROM chats WHERE id = %s", (chatId,))
                chat = await cursor.fetchone()
        if not chat:
            await self._send_error(
                seq, self.opcodes.MSG_TYPING, self.error_types.CHAT_NOT_FOUND, writer
            )
            return
        participants = await self.tools.get_chat_participants(chatId, self.db_pool)
        if int(senderId) not in participants:
            await self._send_error(
                seq, self.opcodes.MSG_TYPING, self.error_types.CHAT_NOT_ACCESS, writer
            )
            return
        for participant in participants:
            if participant != senderId:
                await self.event(
                    participant,
                    {
                        "eventType": "typing",
                        "chatId": chatId,
                        "type": type,
                        "userId": senderId,
                        "writer": writer,
                    },
                )
        packet = self.proto.pack_packet(seq=seq, opcode=self.opcodes.MSG_TYPING)
        await self._send(writer, packet)

    async def msg_send(self, payload, seq, writer, senderId, db_pool):
        try:
            SendMessagePayloadModel.model_validate(payload)
        except pydantic.ValidationError as error:
            self.logger.error(f"Возникли ошибки при валидации пакета: {error}")
            await self._send_error(
                seq, self.opcodes.MSG_SEND, self.error_types.INVALID_PAYLOAD, writer
            )
            return
        userId = payload.get("userId")
        chatId = payload.get("chatId")
        message = payload.get("message")
        elements = message.get("elements") or []
        attaches = message.get("attaches") or []
        cid = message.get("cid") or 0
        text = message.get("text") or ""
        if chatId is None:
            chatId = userId ^ senderId
        async with db_pool.acquire() as db_connection:
            async with db_connection.cursor() as cursor:
                await cursor.execute("SELECT * FROM chats WHERE id = %s", (chatId,))
                chat = await cursor.fetchone()
                if not chat:
                    await self._send_error(
                        seq, self.opcodes.MSG_SEND, self.error_types.CHAT_NOT_FOUND, writer
                    )
                    return
                participants = await self.tools.get_chat_participants(chatId, db_pool)
                if int(senderId) not in participants:
                    await self._send_error(
                        seq, self.opcodes.MSG_SEND, self.error_types.CHAT_NOT_ACCESS, writer
                    )
                    return
                if chat.get("type") == "DIALOG":
                    contactid = [p for p in participants if p != int(senderId)][0]
                    if await self.tools.contact_is_blocked(contactid, senderId, db_pool):
                        await self._send_error(
                            seq, self.opcodes.MSG_SEND, self.error_types.CONTACT_BLOCKED, writer
                        )
                        return
        messageId, lastMessageId, messageTime = await self.tools.insert_message(
            chatId=chatId,
            senderId=senderId,
            text=text,
            attaches=attaches,
            elements=elements,
            cid=cid,
            type="USER",
            db_pool=self.db_pool,
        )
        bodyMessage = {
            "id": messageId,
            "time": messageTime,
            "type": "USER",
            "sender": senderId,
            "cid": cid,
            "text": text,
            "attaches": attaches,
            "elements": elements,
        }
        for participant in participants:
            await self.event(
                participant,
                {
                    "eventType": "new_msg",
                    "chatId": chatId,
                    "message": bodyMessage,
                    "prevMessageId": lastMessageId,
                    "time": messageTime,
                    "writer": writer,
                },
            )
        payload = {"chatId": chatId, "message": bodyMessage, "unread": 0, "mark": messageTime}
        packet = self.proto.pack_packet(
            cmd=self.proto.CMD_OK, seq=seq, opcode=self.opcodes.MSG_SEND, payload=payload
        )
        await self._send(writer, packet)
