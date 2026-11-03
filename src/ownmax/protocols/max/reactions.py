import time
from collections import Counter


async def info(processor, chat, mid, uid):
    async with processor.db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT user_id,reaction_type,reaction_id FROM message_reactions WHERE chat_id=%s AND message_id=%s ORDER BY updated,user_id",
                (chat, mid),
            )
            rows = await cur.fetchall()
    counts = Counter(((r["reaction_type"], r["reaction_id"]) for r in rows))
    result = {
        "totalCount": len(rows),
        "counters": [{"reaction": i, "count": n} for (t, i), n in counts.items()],
    }
    own = next((r for r in rows if int(r["user_id"]) == uid), None)
    if own:
        result["yourReaction"] = own["reaction_id"]
    return (result, rows)


async def augment(processor, chat, messages, uid):
    for message in messages:
        message["reactionInfo"] = (await info(processor, chat, int(message["id"]), uid))[0]


async def handle(processor, payload, seq, writer, uid, opcode):
    try:
        chat = int(payload["chatId"])
        internal = -uid if chat == 0 else chat
        participants = (
            [uid]
            if chat == 0
            else await processor.tools.get_chat_participants(internal, processor.db_pool)
        )
        if uid not in participants:
            raise PermissionError()
        ids = (
            list(dict.fromkeys((int(i) for i in payload["messageIds"])))
            if opcode == 180
            else [int(payload["messageId"])]
        )
        if not ids or len(ids) > 100:
            raise ValueError()
        async with processor.db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                for mid in ids:
                    await cur.execute(
                        "SELECT id FROM messages WHERE chat_id=%s AND id=%s AND NOT EXISTS (SELECT 1 FROM message_deletions WHERE message_id=%s AND user_id IN (0,%s))",
                        (internal, mid, mid, uid),
                    )
                    if not await cur.fetchone():
                        raise PermissionError()
                if opcode == 178:
                    reaction = payload["reaction"]
                    kind = reaction["reactionType"]
                    rid = reaction["id"]
                    if (
                        not isinstance(kind, str)
                        or not 1 <= len(kind) <= 32
                        or (not isinstance(rid, str))
                        or (not 1 <= len(rid) <= 128)
                    ):
                        raise ValueError()
                    await cur.execute(
                        "INSERT INTO message_reactions(chat_id,message_id,user_id,reaction_type,reaction_id,updated) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(message_id,user_id) DO UPDATE SET reaction_type=excluded.reaction_type,reaction_id=excluded.reaction_id,updated=excluded.updated",
                        (internal, ids[0], uid, kind, rid, time.time_ns()),
                    )
                elif opcode == 179:
                    await cur.execute(
                        "DELETE FROM message_reactions WHERE chat_id=%s AND message_id=%s AND user_id=%s",
                        (internal, ids[0], uid),
                    )
        if opcode == 180:
            result = {
                "messagesReactions": {
                    mid: (await info(processor, internal, mid, uid))[0] for mid in ids
                }
            }
        else:
            summary, rows = await info(processor, internal, ids[0], uid)
            result = {"reactionInfo": summary}
            if opcode == 181:
                own = next((r for r in rows if int(r["user_id"]) == uid), None)
                marker = max(0, int(payload.get("marker", 0)))
                count = max(1, min(100, int(payload.get("count", 50))))
                entries = [
                    {"userId": int(r["user_id"]), "reaction": r["reaction_id"]} for r in rows
                ]
                selected = payload.get("reaction")
                if selected:
                    entries = [
                        e
                        for e in entries
                        if e["reaction"]
                        == (selected.get("id") if isinstance(selected, dict) else selected)
                    ]
                result["reactions"] = entries[marker : marker + count]
                if marker + count < len(entries):
                    result["marker"] = marker + count
                if own:
                    result["yourReaction"] = {"userId": uid, "reaction": summary["yourReaction"]}
        await processor._send(
            writer, processor.proto.pack_packet(cmd=1, seq=seq, opcode=opcode, payload=result)
        )
        if opcode in (178, 179):
            notification = {
                "chatId": chat,
                "messageId": ids[0],
                "totalCount": summary["totalCount"],
                "counters": summary["counters"],
            }
            for recipient in participants:
                await processor.event(
                    recipient, {"eventType": "raw", "opcode": 155, "payload": notification}
                )
    except PermissionError:
        await processor._send_error(seq, opcode, processor.error_types.CHAT_NOT_ACCESS, writer)
    except (ValueError, TypeError, KeyError):
        await processor._send_error(seq, opcode, processor.error_types.INVALID_PAYLOAD, writer)
