import json, time
from ownmax.core.baseprocessor import BaseProcessor

FILTER_NAMES = {
    name: n
    for n, name in enumerate(
        (
            "UNREAD",
            "READ",
            "CHANNEL",
            "CHAT",
            "DIALOG",
            "OWNER",
            "ADMIN",
            "MUTED",
            "CONTACT",
            "NOT_CONTACT",
            "BOT",
            "NOT_MUTED",
            "MARKED_UNREAD",
            "ORG",
            "MENTION",
        )
    )
}


def filters_wire(values):
    if not isinstance(values, list):
        raise ValueError()
    result = []
    for value in values:
        if isinstance(value, str):
            value = FILTER_NAMES.get(value.upper(), -1)
        if type(value) is not int or not 0 <= value <= 14:
            raise ValueError()
        if value not in result:
            result.append(value)
    return result


class FoldersProcessors(BaseProcessor):

    async def _folder_state(self, phone):
        async with self.db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT * FROM user_folders WHERE phone=%s ORDER BY sort_order,id",
                    (str(phone),),
                )
                rows = await cur.fetchall()
        folders = [
            {
                "id": r["id"],
                "title": r["title"],
                "filters": filters_wire(json.loads(r["filters"] or "[]")),
                "include": json.loads(r["include"] or "[]"),
                "exclude": json.loads(r.get("exclude_ids") or "[]"),
                "favorites": json.loads(r.get("favorites") or "[]"),
                "emoji": r.get("emoji") or "",
                "options": json.loads(r["options"] or "[]"),
                "updateTime": int(r["update_time"] or 0),
                "sourceId": int(r["source_id"] or 1),
            }
            for r in rows
        ]
        return {
            "folderSync": max([int(time.time() * 1000)] + [f["updateTime"] for f in folders]),
            "folders": folders,
            "foldersOrder": [f["id"] for f in folders],
            "allFilterExcludeFolders": [],
        }

    async def folders_get(self, payload, seq, writer, senderPhone):
        await self.folder_operation(payload, seq, writer, senderPhone, 272)

    async def folders_update(self, payload, seq, writer, senderPhone):
        await self.folder_operation(payload, seq, writer, senderPhone, 274)

    async def folder_operation(self, payload, seq, writer, phone, opcode):
        try:
            payload = payload or {}
            state = await self._folder_state(phone)
            async with self.db_pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT id FROM users WHERE phone=%s", (str(phone),))
                    user = await cur.fetchone()
                    uid = int(user["id"])
                    ids = [f["id"] for f in state["folders"]]
                    if opcode == 274:
                        data = payload.get("folder", payload)
                        fid = str(data["id"])
                        title = str(data["title"]).strip()
                        if (
                            not fid
                            or len(fid) > 64
                            or (not title)
                            or (len(title) > 128)
                            or (fid not in ids and len(ids) >= 50)
                        ):
                            raise ValueError()
                        include = list(dict.fromkeys((int(v) for v in data.get("include", []))))
                        exclude = list(dict.fromkeys((int(v) for v in data.get("exclude", []))))
                        favorites = list(dict.fromkeys((int(v) for v in data.get("favorites", []))))
                        emoji = str(data.get("emoji") or "")
                        if len(emoji) > 32:
                            raise ValueError()
                        filters = filters_wire(data.get("filters", []))
                        options = data.get("options", [])
                        if (
                            len(include) + len(exclude) > 1000
                            or not isinstance(filters, list)
                            or (
                                not all(
                                    (
                                        type(v) is int
                                        and 0 <= v <= 14
                                        or (isinstance(v, str) and len(v) < 64)
                                        for v in filters
                                    )
                                )
                            )
                            or (not isinstance(options, list))
                            or (not all((type(v) is int and 0 <= v <= 4 for v in options)))
                        ):
                            raise ValueError()
                        for chatid in include + exclude + favorites:
                            if chatid in (0, -uid):
                                continue
                            await cur.execute(
                                "SELECT user_id FROM chat_participants WHERE chat_id=%s AND user_id=%s",
                                (chatid, uid),
                            )
                            if not await cur.fetchone():
                                raise PermissionError()
                        now = max(int(time.time() * 1000), state["folderSync"] + 1)
                        position = ids.index(fid) if fid in ids else len(ids)
                        await cur.execute(
                            "INSERT INTO user_folders(id,phone,title,filters,`include`,options,source_id,update_time,sort_order,exclude_ids) VALUES(%s,%s,%s,%s,%s,%s,1,%s,%s,%s) ON CONFLICT(id,phone) DO UPDATE SET title=excluded.title,filters=excluded.filters,`include`=excluded.`include`,options=excluded.options,update_time=excluded.update_time,exclude_ids=excluded.exclude_ids",
                            (
                                fid,
                                str(phone),
                                title,
                                json.dumps(filters),
                                json.dumps(include),
                                json.dumps(options),
                                now,
                                position,
                                json.dumps(exclude),
                            ),
                        )
                        await cur.execute(
                            "UPDATE user_folders SET favorites=%s,emoji=%s WHERE phone=%s AND id=%s",
                            (json.dumps(favorites), emoji, str(phone), fid),
                        )
                    elif opcode == 275:
                        order = payload.get(
                            "foldersOrder", payload.get("folderIds", payload.get("ids", []))
                        )
                        if (
                            not isinstance(order, list)
                            or len(order) != len(set(order))
                            or any((v not in ids for v in order))
                        ):
                            raise ValueError()
                        order = (
                            ["all.chat.folder"]
                            + [v for v in order if v != "all.chat.folder"]
                            + [v for v in ids if v not in order and v != "all.chat.folder"]
                        )
                        for i, fid in enumerate(order):
                            await cur.execute(
                                "UPDATE user_folders SET sort_order=%s,update_time=%s WHERE phone=%s AND id=%s",
                                (i, int(time.time() * 1000), str(phone), fid),
                            )
                    elif opcode == 276:
                        selected = payload.get("folderIds", payload.get("ids", [payload.get("id")]))
                        if not isinstance(selected, list) or any(
                            (v not in ids or v == "all.chat.folder" for v in selected)
                        ):
                            raise ValueError()
                        for fid in selected:
                            await cur.execute(
                                "DELETE FROM user_folders WHERE phone=%s AND id=%s",
                                (str(phone), fid),
                            )
                    elif opcode not in (272, 273):
                        raise ValueError()
                    await conn.commit()
            state = await self._folder_state(phone)
            result = state
            if opcode == 273:
                selected = payload.get("folderIds", payload.get("ids", []))
                result = {**state, "folders": [f for f in state["folders"] if f["id"] in selected]}
            if opcode == 274:
                result = {**state, "folder": next((f for f in state["folders"] if f["id"] == fid))}
            await self._send(
                writer, self.proto.pack_packet(cmd=1, seq=seq, opcode=opcode, payload=result)
            )
            if opcode in (274, 275, 276):
                await self.event(uid, {"eventType": "raw", "opcode": 277, "payload": state})
        except (ValueError, TypeError, KeyError, PermissionError):
            await self._send_error(seq, opcode, self.error_types.INVALID_PAYLOAD, writer)
