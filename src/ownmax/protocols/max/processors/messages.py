import pydantic
from ownmax.core.baseprocessor import BaseProcessor
from ownmax.protocols.max.models import TypingPayloadModel, SendMessagePayloadModel


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
        type = payload.get("type", "TEXT")
        if type in (None, "", "NONE", "STOP", "CANCEL"):
            type = None
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
        from ownmax.protocols.max.activity import update

        await update(self, chatId, senderId, type, participants)
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
        if userId is not None:
            from ownmax.protocols.max.bot_manager import ensure_dialog

            try:
                await ensure_dialog(self, int(senderId), int(userId))
            except ValueError:
                await self._send_error(seq, 64, self.error_types.CHAT_NOT_ACCESS, writer)
                return
        if userId is None and chatId is not None and (int(chatId) > 0):
            from ownmax.protocols.max.bot_manager import ensure_dialog

            try:
                await ensure_dialog(self, int(senderId), int(chatId) ^ int(senderId))
            except ValueError:
                await self._send_error(seq, 64, self.error_types.CHAT_NOT_ACCESS, writer)
                return
        elements = message.get("elements") or []
        attaches = message.get("attaches") or []
        if (
            attaches
            and attaches[0].get("_type") == "CONTROL"
            and (str(attaches[0].get("event", "")).lower() == "new")
            and (chatId is None)
            and (userId is None)
        ):
            from ownmax.protocols.max.groups import create

            try:
                await create(self, payload, seq, writer, senderId)
            except (ValueError, TypeError, KeyError):
                await self._send_error(seq, 64, self.error_types.INVALID_PAYLOAD, writer)
            return
        from ownmax.protocols.max.media_service import enrich

        try:
            attaches = enrich(attaches, senderId)
        except ValueError:
            await self._send_error(
                seq, self.opcodes.MSG_SEND, self.error_types.INVALID_PAYLOAD, writer
            )
            return
        cid = message.get("cid") or 0
        text = message.get("text") or ""
        if chatId is None:
            chatId = userId ^ senderId
        if chatId == senderId ^ senderId:
            chatId = -senderId
            participants = [senderId]
        else:
            async with db_pool.acquire() as db_connection:
                async with db_connection.cursor() as cursor:
                    await cursor.execute("SELECT * FROM chats WHERE id = %s", (chatId,))
                    chat = await cursor.fetchone()
                    if not chat:
                        await self._send_error(
                            seq, self.opcodes.MSG_SEND, self.error_types.CHAT_NOT_FOUND, writer
                        )
                        return
                    if chat.get("type") == "CHANNEL":
                        from ownmax.protocols.max.permissions import mask

                        if not await mask(cursor, chat, senderId) & (1 | 256):
                            await self._send_error(
                                seq, 64, self.error_types.CHAT_NOT_ACCESS, writer
                            )
                            return
                    if chat.get("type") == "DIALOG":
                        import json

                        if json.loads(chat.get("options") or "{}").get("NO_INPUT"):
                            await self._send_error(
                                seq, 64, self.error_types.CHAT_NOT_ACCESS, writer
                            )
                            return
                        await cursor.execute("SELECT options FROM users WHERE id=%s", (senderId,))
                        actor = await cursor.fetchone()
                        if actor and "BOT" in json.loads(actor["options"] or "[]"):
                            await cursor.execute(
                                "SELECT suspended FROM bot_states WHERE bot_id=%s AND user_id=%s",
                                (senderId, chatId ^ senderId),
                            )
                            status = await cursor.fetchone()
                            if status and status["suspended"]:
                                await self._send_error(
                                    seq, 64, self.error_types.CHAT_NOT_ACCESS, writer
                                )
                                return
                        else:
                            await cursor.execute(
                                "SELECT options FROM users WHERE id=%s", (chatId ^ senderId,)
                            )
                            target = await cursor.fetchone()
                            if target and "BOT" in json.loads(target["options"] or "[]"):
                                await cursor.execute(
                                    "INSERT INTO bot_states(bot_id,user_id,suspended) VALUES (%s,%s,0) ON CONFLICT(bot_id,user_id) DO UPDATE SET suspended=0",
                                    (chatId ^ senderId, senderId),
                                )
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
        async with self.db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("UPDATE chat_visibility SET hidden=0 WHERE chat_id=%s", (chatId,))
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
        bot_chat = None
        if chatId > 0:
            async with self.db_pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT options FROM users WHERE id IN (%s,%s)",
                        (senderId, chatId ^ senderId),
                    )
                    bot_dialog = any(
                        (
                            "BOT" in __import__("json").loads(r["options"] or "[]")
                            for r in await cur.fetchall()
                        )
                    )
            if bot_dialog:
                for recipient in participants:
                    snaps = await self.tools.generate_chats(
                        [chatId],
                        self.db_pool,
                        recipient,
                        include_favourites=False,
                        protocol_type=self.type,
                    )
                    if snaps:
                        await self.event(recipient, {"eventType": "chat_updated", "chat": snaps[0]})
                        if recipient == senderId:
                            bot_chat = snaps[0]
        from ownmax.protocols.max.activity import stop

        await stop(self, chatId, senderId)
        bodyMessage = {
            "id": messageId if self.type == "mobile" else str(messageId),
            "cid": int(cid or 0),
            "time": messageTime,
            "type": "USER",
            "sender": senderId,
            "text": text,
            "attaches": attaches if isinstance(attaches, list) else [],
            "elements": elements if isinstance(elements, list) else [],
            "reactionInfo": {},
        }
        for participant in participants:
            await self.event(
                participant,
                {
                    "eventType": "new_msg",
                    "chatId": 0 if chatId == -senderId else chatId,
                    "message": bodyMessage,
                    "prevMessageId": lastMessageId,
                    "time": messageTime,
                    "writer": writer,
                },
            )
        payload = {
            "chatId": 0 if chatId == -senderId else chatId,
            "message": bodyMessage,
            "unread": 0,
            "mark": messageTime,
        }
        if bot_chat is not None:
            payload["chat"] = bot_chat
        packet = self.proto.pack_packet(
            cmd=self.proto.CMD_OK, seq=seq, opcode=self.opcodes.MSG_SEND, payload=payload
        )
        await self._send(writer, packet)
        from ownmax.protocols.max.bot_manager import handle_incoming

        await handle_incoming(self, senderId, participants, text, attaches)

    async def msg_delete(self, payload, seq, writer, senderId):
        try:
            chatId = int(payload["chatId"])
            ids = list(dict.fromkeys((int(v) for v in payload["messageIds"])))
            if not ids or len(ids) > 100 or type(payload.get("forMe", True)) is not bool:
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            await self._send_error(seq, 66, self.error_types.INVALID_PAYLOAD, writer)
            return
        internal = -senderId if chatId == 0 else chatId
        participants = (
            [senderId]
            if chatId == 0
            else await self.tools.get_chat_participants(internal, self.db_pool)
        )
        if senderId not in participants:
            await self._send_error(seq, 66, self.error_types.CHAT_NOT_ACCESS, writer)
            return
        forMe = payload.get("forMe", True)
        placeholders = ",".join(["%s"] * len(ids))
        async with self.db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, sender FROM messages WHERE chat_id = %s AND id IN ("
                    + placeholders
                    + ")",
                    (internal, *ids),
                )
                rows = await cur.fetchall()
                if not forMe and any((int(row["sender"]) != senderId for row in rows)):
                    await cur.execute("SELECT * FROM chats WHERE id=%s", (internal,))
                    chat = await cur.fetchone()
                    from ownmax.protocols.max.permissions import mask

                    rights = (
                        await mask(cur, chat, senderId)
                        if chat and chat["type"] in ("CHAT", "CHANNEL")
                        else 0
                    )
                    if not rights & (1 | 1024):
                        await self._send_error(seq, 66, self.error_types.CHAT_NOT_ACCESS, writer)
                        return
                owner = senderId if forMe else 0
                for row in rows:
                    await cur.execute(
                        "INSERT OR IGNORE INTO message_deletions (message_id,user_id,deleted_at) VALUES (%s,%s,%s)",
                        (row["id"], owner, __import__("time").time_ns() // 1000000),
                    )
        result = {"chatId": chatId, "messageIds": ids}
        await self._send(writer, self.proto.pack_packet(cmd=1, seq=seq, opcode=66, payload=result))
        for recipient in [senderId] if forMe else participants:
            chats = await self.tools.generate_chats(
                [internal] if chatId else [],
                self.db_pool,
                recipient,
                include_favourites=not bool(chatId),
                protocol_type=self.type,
            )
            await self.event(
                recipient,
                {
                    "eventType": "message_deleted",
                    "chatId": chatId,
                    "chat": chats[0] if chats else {},
                    "messageIds": ids,
                    "forMe": forMe,
                    "writer": writer,
                },
            )

    async def message_reaction(self, payload, seq, writer, userId, opcode):
        from ownmax.protocols.max.reactions import handle

        await handle(self, payload or {}, seq, writer, userId, opcode)
