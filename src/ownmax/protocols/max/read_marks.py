async def handle(p, payload, seq, writer, uid, opcode):
    try:
        chat = int(payload["chatId"])
        internal = -uid if chat == 0 else chat
        participants = (
            [uid] if chat == 0 else await p.tools.get_chat_participants(internal, p.db_pool)
        )
        if uid not in participants:
            raise PermissionError()
        async with p.db_pool.acquire() as conn:
            async with conn.cursor() as c:
                if opcode == 74:
                    ids = list(dict.fromkeys((int(x) for x in payload["messageIds"])))
                    if len(ids) > 100:
                        raise ValueError()
                    stats = {}
                    for mid in ids:
                        await c.execute(
                            "SELECT time FROM messages WHERE id=%s AND chat_id=%s AND NOT EXISTS (SELECT 1 FROM message_deletions WHERE message_id=%s AND user_id IN (0,%s))",
                            (mid, internal, mid, uid),
                        )
                        row = await c.fetchone()
                        if not row:
                            raise PermissionError()
                        await c.execute(
                            "SELECT COUNT(*) AS n FROM chat_read_marks r JOIN chat_participants cp ON cp.chat_id=r.chat_id AND cp.user_id=r.user_id WHERE r.chat_id=%s AND r.mark>=%s",
                            (internal, int(row["time"])),
                        )
                        stats[mid] = {"views": int((await c.fetchone())["n"]), "forwards": 0}
                    result = {"stats": stats}
                else:
                    kind = str(payload.get("type", "READ")).upper()
                    mark = max(0, int(payload.get("mark", 0)))
                    await c.execute(
                        "SELECT MAX(CAST(time AS INTEGER)) AS last_time FROM messages WHERE chat_id=%s",
                        (internal,),
                    )
                    last = int((await c.fetchone())["last_time"] or 0)
                    mid = payload.get("messageId")
                    if mid:
                        await c.execute(
                            "SELECT time FROM messages WHERE id=%s AND chat_id=%s",
                            (int(mid), internal),
                        )
                        row = await c.fetchone()
                        if not row:
                            raise ValueError()
                        mark = int(row["time"])
                    mark = min(mark, last)
                    unread = kind in ("UNREAD", "MARK_AS_UNREAD")
                    await c.execute(
                        "INSERT INTO chat_read_marks(chat_id,user_id,mark,marked_unread) VALUES (%s,%s,%s,%s) ON CONFLICT(chat_id,user_id) DO UPDATE SET mark=MAX(chat_read_marks.mark,excluded.mark),marked_unread=excluded.marked_unread",
                        (internal, uid, mark, int(unread)),
                    )
                    await c.execute(
                        "SELECT mark FROM chat_read_marks WHERE chat_id=%s AND user_id=%s",
                        (internal, uid),
                    )
                    mark = int((await c.fetchone())["mark"])
                    await c.execute(
                        "SELECT COUNT(*) AS n FROM messages m WHERE chat_id=%s AND CAST(time AS INTEGER)>%s AND sender<>%s AND NOT EXISTS (SELECT 1 FROM message_deletions WHERE message_id=m.id AND user_id IN (0,%s))",
                        (internal, mark, uid, uid),
                    )
                    count = int((await c.fetchone())["n"])
                    result = {"mark": mark, "unread": count}
        await p._send(writer, p.proto.pack_packet(cmd=1, seq=seq, opcode=opcode, payload=result))
        if opcode == 50:
            for recipient in participants:
                await p.event(
                    recipient,
                    {
                        "eventType": "raw",
                        "opcode": 130,
                        "payload": {
                            "chatId": chat,
                            "userId": uid,
                            "mark": mark,
                            "unread": count if recipient == uid else 0,
                            "setAsUnread": unread,
                        },
                    },
                )
    except PermissionError:
        await p._send_error(seq, opcode, p.error_types.CHAT_NOT_ACCESS, writer)
    except (ValueError, TypeError, KeyError):
        await p._send_error(seq, opcode, p.error_types.INVALID_PAYLOAD, writer)
