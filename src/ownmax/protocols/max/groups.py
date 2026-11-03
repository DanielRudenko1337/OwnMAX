import json, secrets, time
from ownmax.protocols.max.permissions import mask, require, ALL


async def snapshot(p, chatid, uid):
    v = await p.tools.generate_chats(
        [chatid], p.db_pool, uid, include_favourites=False, protocol_type=p.type
    )
    return v[0] if v else None


async def notify(p, chatid, participants):
    for uid in participants:
        chat = await snapshot(p, chatid, uid)
        await p.event(uid, {"eventType": "chat_updated", "chat": chat})


async def create(p, payload, seq, writer, uid):
    a = payload["message"]["attaches"][0]
    kind = a.get("chatType", "CHAT")
    title = str(a.get("title", "")).strip()
    ids = list(dict.fromkeys([uid] + [int(x) for x in a.get("userIds", [])]))
    if kind not in ("CHAT", "CHANNEL") or not title or len(title) > 200 or (len(ids) > 20):
        raise ValueError()
    async with p.db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            for member in ids:
                await cur.execute("SELECT id FROM users WHERE id = %s", (member,))
                if not await cur.fetchone():
                    raise ValueError()
            chatid = 9000000000000 + secrets.randbelow(1000000000)
            now = int(time.time() * 1000)
            await cur.execute(
                "INSERT INTO chats (id,owner,type,title,description,created,options) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    chatid,
                    uid,
                    kind,
                    title,
                    "",
                    now,
                    json.dumps(
                        {
                            "ONLY_OWNER_CAN_CHANGE_ICON_TITLE": True,
                            "ONLY_ADMIN_CAN_ADD_MEMBER": True,
                            "ONLY_ADMIN_CAN_CALL": False,
                            "MEMBERS_CAN_SEE_PRIVATE_LINK": False,
                        }
                    ),
                ),
            )
            for member in ids:
                await cur.execute(
                    "INSERT INTO chat_participants (chat_id,user_id) VALUES (%s,%s)",
                    (chatid, member),
                )
    mid, prev, when = await p.tools.insert_message(
        chatid,
        uid,
        "",
        [{"_type": "CONTROL", "event": "new", "chatType": kind, "title": title, "userIds": ids}],
        [],
        payload["message"].get("cid", now),
        "SYSTEM",
        p.db_pool,
    )
    chat = await snapshot(p, chatid, uid)
    message = chat["lastMessage"]
    await p._send(
        writer,
        p.proto.pack_packet(
            cmd=1,
            seq=seq,
            opcode=64,
            payload={"chatId": chatid, "chat": chat, "message": message, "unread": 0, "mark": when},
        ),
    )
    await notify(p, chatid, ids)


async def operation(p, op, payload, seq, writer, uid):
    try:
        chatid = int(payload["chatId"])
        members = await p.tools.get_chat_participants(chatid, p.db_pool)
        if uid not in members:
            raise PermissionError()
        async with p.db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT * FROM chats WHERE id = %s", (chatid,))
                row = await cur.fetchone()
                if not row or row["type"] not in ("CHAT", "CHANNEL"):
                    raise ValueError()
                if op == 59:
                    await cur.execute(
                        "SELECT user_id,permissions FROM chat_admins WHERE chat_id=%s", (chatid,)
                    )
                    admins = {
                        int(r["user_id"]): int(r["permissions"]) for r in await cur.fetchall()
                    }
                    admins[int(row["owner"])] = ALL
                    kind = str(payload.get("type", "MEMBER")).upper()
                    selected = (
                        [int(row["owner"])]
                        if kind == "OWNER"
                        else list(admins) if kind == "ADMIN" else members
                    )
                    marker = max(0, int(payload.get("marker", 0)))
                    count = max(1, min(100, int(payload.get("count", 50))))
                    part = selected[marker : marker + count]
                    contacts = await p.tools.generate_contacts(
                        [{"id": x} for x in part],
                        p.db_pool,
                        avatar_base_url=p.config.avatar_base_url,
                    )
                    result = {
                        "members": [
                            {
                                "userId": x,
                                "id": x,
                                "role": (
                                    "OWNER"
                                    if x == int(row["owner"])
                                    else "ADMIN" if x in admins else "MEMBER"
                                ),
                                "permissions": admins.get(x, 0),
                                "joinTime": int(row["created"]),
                            }
                            for x in part
                        ],
                        "contacts": contacts,
                        "marker": marker + len(part) if marker + len(part) < len(selected) else 0,
                        "hasMore": marker + len(part) < len(selected),
                    }
                else:
                    rights = await mask(cur, row, uid)
                    owner = uid == int(row["owner"])
                    if op == 77:
                        raw_ids = payload.get("userIds") or [
                            m.get("userId", m.get("id"))
                            for m in payload.get("members", [])
                            if isinstance(m, dict)
                        ]
                        ids = list(dict.fromkeys((int(x) for x in raw_ids)))
                        action = str(payload.get("operation", "")).lower()
                        admin_change = str(payload.get("type", "")).upper() == "ADMIN"
                        permissions = int(payload.get("permissions", ALL))
                        require(rights, 4 if admin_change else 2)
                        if admin_change and (not owner) and permissions & ~rights:
                            raise PermissionError()
                        if admin_change and (not 0 <= permissions <= ALL):
                            raise ValueError()
                        if len(ids) > 20:
                            raise ValueError()
                        for member in ids:
                            if member in (uid, int(row["owner"])):
                                raise ValueError()
                            if not owner:
                                await cur.execute(
                                    "SELECT user_id FROM chat_admins WHERE chat_id=%s AND user_id=%s",
                                    (chatid, member),
                                )
                                if await cur.fetchone():
                                    raise PermissionError()
                            if admin_change:
                                if member not in members:
                                    raise ValueError()
                                if action in ("add", "update"):
                                    await cur.execute(
                                        "INSERT INTO chat_admins(chat_id,user_id,permissions) VALUES (%s,%s,%s) ON CONFLICT(chat_id,user_id) DO UPDATE SET permissions=excluded.permissions",
                                        (chatid, member, permissions),
                                    )
                                elif action == "remove":
                                    await cur.execute(
                                        "DELETE FROM chat_admins WHERE chat_id=%s AND user_id=%s",
                                        (chatid, member),
                                    )
                                else:
                                    raise ValueError()
                            elif action == "add":
                                await cur.execute("SELECT id FROM users WHERE id = %s", (member,))
                                if not await cur.fetchone():
                                    raise ValueError()
                                await cur.execute(
                                    "INSERT OR IGNORE INTO chat_participants (chat_id,user_id) VALUES (%s,%s)",
                                    (chatid, member),
                                )
                            elif action == "remove":
                                await cur.execute(
                                    "DELETE FROM chat_admins WHERE chat_id=%s AND user_id=%s",
                                    (chatid, member),
                                )
                                await cur.execute(
                                    "DELETE FROM chat_participants WHERE chat_id=%s AND user_id=%s",
                                    (chatid, member),
                                )
                            else:
                                raise ValueError()
                    elif op == 55:
                        if "changeOwnerId" in payload and (not owner):
                            raise PermissionError()
                        if "pinMessageId" in payload:
                            require(rights, 16)
                        if any(
                            (
                                k in payload
                                for k in ("access", "link", "removeLink", "revokePrivateLink")
                            )
                        ):
                            require(rights, 128)
                        if any(
                            (
                                k in payload
                                for k in ("theme", "title", "description", "photoToken", "options")
                            )
                        ):
                            require(rights, 8)
                        if "access" in payload:
                            access = str(payload["access"]).upper()
                            if access not in ("PUBLIC", "PRIVATE", "PUBLIC_BUSINESS"):
                                raise ValueError()
                            if access != "PRIVATE" and (
                                not (payload.get("link") or row.get("public_slug"))
                            ):
                                raise ValueError()
                        if payload.get("link"):
                            from ownmax.protocols.max.chat_links import slug

                            public = slug(payload["link"])
                            await cur.execute(
                                "SELECT id FROM chats WHERE public_slug=%s AND id<>%s",
                                (public, chatid),
                            )
                            if await cur.fetchone():
                                raise ValueError()
                            await cur.execute(
                                "SELECT id FROM users WHERE LOWER(username)=%s", (public,)
                            )
                            if await cur.fetchone():
                                raise ValueError()
                            await cur.execute(
                                "UPDATE chats SET public_slug=%s WHERE id=%s", (public, chatid)
                            )
                        if "access" in payload:
                            await cur.execute(
                                "UPDATE chats SET access=%s WHERE id=%s", (access, chatid)
                            )
                        if payload.get("removeLink"):
                            await cur.execute(
                                "UPDATE chats SET public_slug=NULL,access='PRIVATE' WHERE id=%s",
                                (chatid,),
                            )
                        if payload.get("revokePrivateLink"):
                            await cur.execute(
                                "UPDATE chat_invite_links SET revoked=1 WHERE chat_id=%s", (chatid,)
                            )
                        if "changeOwnerId" in payload:
                            target = int(payload["changeOwnerId"])
                            if target not in members:
                                raise ValueError()
                            await cur.execute(
                                "UPDATE chats SET owner=%s WHERE id=%s", (target, chatid)
                            )
                            await cur.execute(
                                "DELETE FROM chat_admins WHERE chat_id=%s AND user_id=%s",
                                (chatid, target),
                            )
                            await cur.execute(
                                "INSERT INTO chat_admins(chat_id,user_id,permissions) VALUES (%s,%s,%s) ON CONFLICT(chat_id,user_id) DO UPDATE SET permissions=excluded.permissions",
                                (chatid, uid, ALL),
                            )
                        if "pinMessageId" in payload:
                            mid = int(payload["pinMessageId"] or 0)
                            if mid:
                                await cur.execute(
                                    "SELECT id FROM messages WHERE chat_id=%s AND id=%s AND NOT EXISTS (SELECT 1 FROM message_deletions WHERE message_id=%s AND user_id=0)",
                                    (chatid, mid, mid),
                                )
                                if not await cur.fetchone():
                                    raise ValueError()
                            await cur.execute(
                                "UPDATE chats SET pinned_message_id=%s WHERE id=%s",
                                (mid or None, chatid),
                            )
                        title = payload.get("theme", payload.get("title"))
                        desc = payload.get("description")
                        if title is not None:
                            title = str(title).strip()
                            if not title or len(title) > 200:
                                raise ValueError()
                            await cur.execute(
                                "UPDATE chats SET title=%s WHERE id=%s", (title, chatid)
                            )
                        if desc is not None:
                            if len(str(desc)) > 1024:
                                raise ValueError()
                            await cur.execute(
                                "UPDATE chats SET description=%s WHERE id=%s", (str(desc), chatid)
                            )
                        if "photoToken" in payload and payload["photoToken"] is not None:
                            photo = None
                            if payload["photoToken"] != "":
                                from ownmax.protocols.max.media_service import enrich, load

                                media = enrich(
                                    [{"_type": "PHOTO", "photoToken": payload["photoToken"]}], uid
                                )[0]
                                if media["_type"] != "PHOTO" or load(str(media["photoId"])).get(
                                    "mime"
                                ) not in ("image/png", "image/jpeg"):
                                    raise ValueError()
                                photo = int(media["photoId"])
                            await cur.execute(
                                "UPDATE chats SET avatar_id=%s WHERE id=%s", (photo, chatid)
                            )
                        await cur.execute(
                            "UPDATE chats SET updated=%s WHERE id=%s",
                            (int(time.time() * 1000), chatid),
                        )
                        if isinstance(payload.get("options"), dict):
                            opts = json.loads(row["options"])
                            opts.update(
                                {
                                    k: v
                                    for k, v in payload["options"].items()
                                    if k in opts and type(v) is bool
                                }
                            )
                            await cur.execute(
                                "UPDATE chats SET options=%s WHERE id=%s",
                                (json.dumps(opts), chatid),
                            )
                    result = {"chat": await snapshot(p, chatid, uid)}
        await p._send(writer, p.proto.pack_packet(cmd=1, seq=seq, opcode=op, payload=result))
        if op != 59:
            await notify(p, chatid, await p.tools.get_chat_participants(chatid, p.db_pool))
    except (ValueError, TypeError, KeyError, PermissionError):
        await p._send_error(seq, op, p.error_types.CHAT_NOT_ACCESS, writer)
