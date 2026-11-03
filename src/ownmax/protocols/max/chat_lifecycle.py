import time


async def operation(p, payload, seq, writer, uid, op):
    try:
        chat = int(payload["chatId"])
        internal = -uid if chat == 0 else chat
        async with p.db_pool.acquire() as conn:
            async with conn.cursor() as c:
                await c.execute("SELECT * FROM chats WHERE id=%s", (internal,))
                meta = await c.fetchone()
        if not meta and chat:
            if op in (52, 58):
                result = {}
                if op == 58:
                    now = int(time.time() * 1000)
                    result = {
                        "message": {
                            "id": time.time_ns(),
                            "cid": now,
                            "chatId": chat,
                            "time": now,
                            "type": "SYSTEM",
                            "sender": uid,
                            "text": "",
                            "attaches": [{"_type": "CONTROL", "event": "remove", "userIds": [uid]}],
                            "elements": [],
                            "reactionInfo": {},
                        }
                    }
                await p._send(
                    writer, p.proto.pack_packet(cmd=1, seq=seq, opcode=op, payload=result)
                )
                return
            raise PermissionError()
        members = [uid] if not chat else await p.tools.get_chat_participants(internal, p.db_pool)
        if uid not in members:
            raise PermissionError()
        for_all = payload.get("forAll", False)
        if type(for_all) is not bool:
            raise ValueError()
        if (
            for_all
            and meta
            and (meta["type"] in ("CHAT", "CHANNEL"))
            and (int(meta["owner"]) != uid)
        ):
            raise PermissionError()
        if (
            op == 58
            and meta
            and (meta["type"] in ("CHAT", "CHANNEL"))
            and (int(meta["owner"]) == uid)
        ):
            raise ValueError()
        targets = members if for_all else [uid]
        async with p.db_pool.acquire() as conn:
            async with conn.cursor() as c:
                if op in (52, 58):
                    for target in targets:
                        await c.execute(
                            "INSERT INTO chat_visibility(chat_id,user_id,hidden) VALUES (%s,%s,1) ON CONFLICT(chat_id,user_id) DO UPDATE SET hidden=1",
                            (internal, target),
                        )
                    if for_all and meta and (meta["type"] in ("CHAT", "CHANNEL")):
                        await c.execute("UPDATE chats SET is_deleted=1 WHERE id=%s", (internal,))
                    elif op == 58:
                        await c.execute(
                            "DELETE FROM chat_participants WHERE chat_id=%s AND user_id=%s",
                            (internal, uid),
                        )
                        await c.execute(
                            "DELETE FROM chat_admins WHERE chat_id=%s AND user_id=%s",
                            (internal, uid),
                        )
                else:
                    await c.execute(
                        "SELECT MAX(CAST(time AS INTEGER)) AS t FROM messages WHERE chat_id=%s",
                        (internal,),
                    )
                    latest = int((await c.fetchone())["t"] or 0)
                    end = min(latest, max(0, int(payload.get("lastEventTime") or latest)))
                    for target in [0] if for_all else targets:
                        await c.execute(
                            "INSERT OR IGNORE INTO message_deletions(message_id,user_id,deleted_at) SELECT id,%s,%s FROM messages WHERE chat_id=%s AND CAST(time AS INTEGER)<=%s",
                            (target, time.time_ns() // 1000000, internal, end),
                        )
        result = {}
        if op == 58:
            mid, prev, when = await p.tools.insert_message(
                internal,
                uid,
                "",
                [{"_type": "CONTROL", "event": "remove", "userIds": [uid]}],
                [],
                time.time_ns() // 1000000,
                "SYSTEM",
                p.db_pool,
            )
            result = {
                "message": {
                    "id": mid,
                    "cid": when,
                    "time": when,
                    "type": "SYSTEM",
                    "sender": uid,
                    "text": "",
                    "attaches": [{"_type": "CONTROL", "event": "remove", "userIds": [uid]}],
                    "elements": [],
                    "reactionInfo": {},
                }
            }
        await p._send(writer, p.proto.pack_packet(cmd=1, seq=seq, opcode=op, payload=result))
        for target in targets:
            if op in (52, 58):
                await p.event(
                    target,
                    {
                        "eventType": "chat_updated",
                        "chat": {
                            "id": chat,
                            "type": meta["type"] if meta else "DIALOG",
                            "status": "LEFT" if op == 58 else "REMOVED",
                            "modified": int(time.time() * 1000),
                        },
                    },
                )
            else:
                chats = await p.tools.generate_chats(
                    [] if chat == 0 else [internal],
                    p.db_pool,
                    target,
                    include_favourites=chat == 0,
                    protocol_type=p.type,
                )
                if chats:
                    await p.event(target, {"eventType": "chat_updated", "chat": chats[0]})
        if op == 58:
            from ownmax.protocols.max.groups import notify

            await notify(p, internal, [m for m in members if m != uid])
    except PermissionError:
        await p._send_error(seq, op, p.error_types.CHAT_NOT_ACCESS, writer)
    except (ValueError, TypeError, KeyError):
        await p._send_error(seq, op, p.error_types.INVALID_PAYLOAD, writer)
