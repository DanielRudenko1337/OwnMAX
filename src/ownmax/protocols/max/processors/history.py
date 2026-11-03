import pydantic
from ownmax.core.baseprocessor import BaseProcessor
from ownmax.protocols.max.models import ChatHistoryPayloadModel


class HistoryProcessors(BaseProcessor):

    async def chat_history(self, payload, seq, writer, senderId):
        try:
            ChatHistoryPayloadModel.model_validate(payload)
        except pydantic.ValidationError as error:
            self.logger.error(f"Возникли ошибки при валидации пакета: {error}")
            await self._send_error(
                seq, self.opcodes.CHAT_HISTORY, self.error_types.INVALID_PAYLOAD, writer
            )
            return
        chatId = payload.get("chatId")
        forward = payload.get("forward", 0)
        backward = payload.get("backward", 0)
        from_time = payload.get("from", 0)
        getMessages = payload.get("getMessages", True)
        getChat = payload.get("getChat", False)
        messages = []
        isFavourite = chatId == senderId ^ senderId
        if isFavourite:
            chatId = -senderId
        async with self.db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                if not isFavourite:
                    await cursor.execute("SELECT * FROM chats WHERE id = %s", (chatId,))
                    chat = await cursor.fetchone()
                    if not chat:
                        await self._send_error(
                            seq, self.opcodes.CHAT_HISTORY, self.error_types.CHAT_NOT_FOUND, writer
                        )
                        return
                    participants = await self.tools.get_chat_participants(chatId, self.db_pool)
                    if int(senderId) not in participants:
                        await self._send_error(
                            seq, self.opcodes.CHAT_HISTORY, self.error_types.CHAT_NOT_ACCESS, writer
                        )
                        return
                if getMessages:
                    if backward > 0:
                        await cursor.execute(
                            "SELECT * FROM messages WHERE chat_id = %s AND NOT EXISTS (SELECT 1 FROM message_deletions d WHERE d.message_id = messages.id AND d.user_id IN (0, %s)) AND time < %s ORDER BY time DESC LIMIT %s",
                            (chatId, senderId, from_time, backward),
                        )
                        result = await cursor.fetchall()
                        for row in result:
                            messages.append(self.tools.build_message_dict(row, self.type))
                        backward_count = len(result)
                    if forward > 0:
                        await cursor.execute(
                            "SELECT * FROM messages WHERE chat_id = %s AND NOT EXISTS (SELECT 1 FROM message_deletions d WHERE d.message_id = messages.id AND d.user_id IN (0, %s)) AND time > %s ORDER BY time ASC LIMIT %s",
                            (chatId, senderId, from_time, forward),
                        )
                        result = await cursor.fetchall()
                        for row in result:
                            messages.append(self.tools.build_message_dict(row, self.type))
                        forward_count = len(result)
        from ownmax.protocols.max.reactions import augment

        await augment(self, chatId, messages, senderId)
        messages.sort(key=lambda x: x["time"])
        payload = {"messages": messages}
        if getChat:
            chats = await self.tools.generate_chats(
                [] if isFavourite else [chatId],
                self.db_pool,
                senderId,
                include_favourites=isFavourite,
                protocol_type=self.type,
            )
            payload["chat"] = chats[0] if chats else {}
        packet = self.proto.pack_packet(
            cmd=self.proto.CMD_OK, seq=seq, opcode=self.opcodes.CHAT_HISTORY, payload=payload
        )
        await self._send(writer, packet)

    async def chat_media(self, payload, seq, writer, uid):
        try:
            chat = int(payload["chatId"])
            internal = -uid if chat == 0 else chat
            if chat and uid not in await self.tools.get_chat_participants(internal, self.db_pool):
                raise PermissionError()
            types = payload.get("attachTypes") or []
            if not isinstance(types, list) or len(types) > 20:
                raise ValueError()
            requested = {str(t).upper() for t in types}
            anchor = int(payload.get("messageId") or 0)
            forward = max(0, min(100, int(payload.get("forward") or 0)))
            backward = max(0, min(100, int(payload.get("backward") or 0)))
            async with self.db_pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT * FROM messages m WHERE chat_id=%s AND attaches IS NOT NULL AND attaches<>'[]' AND NOT EXISTS (SELECT 1 FROM message_deletions d WHERE d.message_id=m.id AND d.user_id IN (0,%s)) ORDER BY CAST(time AS INTEGER),id LIMIT 10000",
                        (internal, uid),
                    )
                    rows = await cur.fetchall()
            matches = []
            for row in rows:
                message = self.tools.build_message_dict(row, self.type)
                attaches = message["attaches"]
                if any(
                    (
                        a.get("_type") in requested
                        or (not requested and a.get("_type") in ("PHOTO", "VIDEO", "AUDIO", "FILE"))
                        for a in attaches
                    )
                ):
                    matches.append(message)
            pos = next(
                (i for i, m in enumerate(matches) if int(m["id"]) == anchor),
                len(matches) if anchor == 0 else 0,
            )
            start = max(0, pos - backward)
            end = min(len(matches), pos + forward + (1 if anchor else 0))
            if not forward and (not backward):
                start = max(0, len(matches) - 50)
                end = len(matches)
            selected = matches[start:end]
            from ownmax.protocols.max.reactions import augment

            await augment(self, internal, selected, uid)
            result = {
                "messages": selected,
                "total": len(matches),
                "pos": start,
                "forward": max(0, len(matches) - end),
                "backward": start,
            }
            await self._send(
                writer, self.proto.pack_packet(cmd=1, seq=seq, opcode=51, payload=result)
            )
        except PermissionError:
            await self._send_error(seq, 51, self.error_types.CHAT_NOT_ACCESS, writer)
        except (ValueError, TypeError, KeyError):
            await self._send_error(seq, 51, self.error_types.INVALID_PAYLOAD, writer)
