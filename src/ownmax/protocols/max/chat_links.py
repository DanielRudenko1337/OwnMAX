import secrets, time, re, json
from urllib.parse import urlparse


def slug(value):
    text = str(value).strip()
    if "://" in text:
        url = urlparse(text)
        if url.hostname != "max.ru":
            raise ValueError()
        text = url.path.strip("/")
    if not re.fullmatch("[A-Za-z][A-Za-z0-9_]{3,63}", text):
        raise ValueError()
    if text.lower() in ("securitybot", "botmanager", "join", "joincall", "auth"):
        raise ValueError()
    return text.lower()


def wire_link(row):
    return {
        "id": int(row["id"]),
        "title": row["title"],
        "link": "https://max.ru/join/" + row["token"],
        "joinCount": int(row["join_count"]),
    }


async def preview(p, meta, uid):
    members = await p.tools.get_chat_participants(int(meta["id"]), p.db_pool)
    if uid in members:
        chats = await p.tools.generate_chats(
            [int(meta["id"])], p.db_pool, uid, include_favourites=False, protocol_type=p.type
        )
        if chats:
            return chats[0]
    return {
        "id": int(meta["id"]),
        "owner": int(meta["owner"]),
        "type": meta["type"],
        "title": meta["title"],
        "description": meta["description"] or "",
        "status": "ACTIVE",
        "participants": {},
        "participantsCount": len(members),
        "access": meta.get("access") or "PRIVATE",
        "link": "https://max.ru/" + meta["public_slug"] if meta.get("public_slug") else "",
        "options": json.loads(meta["options"] or "{}"),
        "created": int(meta["created"]),
        "modified": int(meta.get("updated") or meta["created"]),
        "lastEventTime": int(meta.get("updated") or meta["created"]),
        "lastDelayedUpdateTime": 0,
        "lastFireDelayedErrorTime": 0,
        "joinTime": 0,
        "cid": int(meta["id"]),
        "prevMessageId": 0,
    }


async def resolve(p, uid, value):
    value = str(value or "").strip()
    if "://" in value:
        url = urlparse(value)
        if url.hostname != "max.ru":
            raise ValueError()
        path = url.path.strip("/")
    else:
        path = value.strip("/")
    async with p.db_pool.acquire() as conn:
        async with conn.cursor() as c:
            if path.startswith("join/"):
                cap = path.split("/", 1)[1]
                await c.execute(
                    "SELECT l.id AS invite_id,c.* FROM chat_invite_links l JOIN chats c ON c.id=l.chat_id WHERE l.token=%s AND l.revoked=0 AND c.is_deleted=0",
                    (cap,),
                )
                meta = await c.fetchone()
            else:
                await c.execute(
                    "SELECT * FROM chats WHERE public_slug=%s AND access IN ('PUBLIC','PUBLIC_BUSINESS') AND is_deleted=0",
                    (path.lower(),),
                )
                meta = await c.fetchone()
            if meta:
                return ({"chat": await preview(p, meta, uid)}, meta)
            if path.startswith("join/"):
                raise PermissionError()
            await c.execute(
                "SELECT id FROM users WHERE username=%s AND NOT EXISTS (SELECT 1 FROM bot_registry b WHERE b.bot_id=users.id AND b.active=0)",
                (path.lower(),),
            )
            user = await c.fetchone()
    if user:
        contacts = await p.tools.generate_contacts(
            [{"id": int(user["id"])}], p.db_pool, avatar_base_url=p.config.avatar_base_url
        )
        if contacts:
            return (
                {"user": {"contact": contacts[0], "summary": contacts[0].get("description") or ""}},
                None,
            )
    raise ValueError()


async def operation(p, op, payload, seq, writer, uid):
    try:
        if op in (89, 56, 57):
            result, meta = await resolve(p, uid, payload.get("link") or payload.get("joinLink"))
            if op == 57:
                if not meta:
                    raise ValueError()
                chat = int(meta["id"])
                before = await p.tools.get_chat_participants(chat, p.db_pool)
                async with p.db_pool.acquire() as conn:
                    async with conn.cursor() as c:
                        await c.execute(
                            "INSERT OR IGNORE INTO chat_participants(chat_id,user_id) VALUES (%s,%s)",
                            (chat, uid),
                        )
                        await c.execute(
                            "UPDATE chat_visibility SET hidden=0 WHERE chat_id=%s AND user_id=%s",
                            (chat, uid),
                        )
                        if uid not in before and meta.get("invite_id"):
                            await c.execute(
                                "UPDATE chat_invite_links SET join_count=join_count+1 WHERE id=%s",
                                (meta["invite_id"],),
                            )
                from ownmax.protocols.max.groups import notify

                await notify(p, chat, await p.tools.get_chat_participants(chat, p.db_pool))
                result = {"chat": await preview(p, meta, uid)}
        elif op == 300:
            async with p.db_pool.acquire() as conn:
                async with conn.cursor() as c:
                    await c.execute(
                        "SELECT c.* FROM chats c WHERE c.type='CHANNEL' AND c.access IN ('PUBLIC','PUBLIC_BUSINESS') AND c.is_deleted=0 AND NOT EXISTS (SELECT 1 FROM chat_participants cp WHERE cp.chat_id=c.id AND cp.user_id=%s) ORDER BY (SELECT COUNT(*) FROM chat_participants cp WHERE cp.chat_id=c.id) DESC,c.updated DESC,c.id LIMIT 20",
                        (uid,),
                    )
                    rows = await c.fetchall()
            result = {
                "chats": [await preview(p, r, uid) for r in rows],
                "recomId": "public-local-v1",
            }
        else:
            chat = int(payload["chatId"])
            async with p.db_pool.acquire() as conn:
                async with conn.cursor() as c:
                    await c.execute("SELECT * FROM chats WHERE id=%s AND is_deleted=0", (chat,))
                    meta = await c.fetchone()
                    if not meta or uid not in await p.tools.get_chat_participants(chat, p.db_pool):
                        raise PermissionError()
                    from ownmax.protocols.max.permissions import mask, require

                    require(await mask(c, meta, uid), 128)
                    if op == 222:
                        title = str(payload.get("title") or "").strip()
                        if len(title) > 128:
                            raise ValueError()
                        await c.execute(
                            "SELECT COUNT(*) AS n FROM chat_invite_links WHERE chat_id=%s AND revoked=0",
                            (chat,),
                        )
                        if int((await c.fetchone())["n"]) >= 20:
                            raise ValueError()
                        row = {
                            "id": str(secrets.randbelow(1 << 48) + (1 << 48)),
                            "token": secrets.token_urlsafe(24),
                            "title": title,
                            "join_count": 0,
                        }
                        await c.execute(
                            "INSERT INTO chat_invite_links(id,chat_id,token,title,created) VALUES (%s,%s,%s,%s,%s)",
                            (row["id"], chat, row["token"], title, int(time.time() * 1000)),
                        )
                        result = {"link": wire_link(row)}
                    elif op == 223:
                        ids = payload["linkIds"]
                        if not isinstance(ids, list) or len(ids) > 20:
                            raise ValueError()
                        for linkid in ids:
                            await c.execute(
                                "UPDATE chat_invite_links SET revoked=1 WHERE chat_id=%s AND id=%s",
                                (chat, str(linkid)),
                            )
                        result = {}
                    elif op == 221:
                        await c.execute(
                            "SELECT * FROM chat_invite_links WHERE chat_id=%s AND revoked=0 ORDER BY created",
                            (chat,),
                        )
                        result = {"links": [wire_link(r) for r in await c.fetchall()]}
                    else:
                        raise ValueError()
        await p._send(writer, p.proto.pack_packet(cmd=1, seq=seq, opcode=op, payload=result))
    except PermissionError:
        await p._send_error(seq, op, p.error_types.CHAT_NOT_ACCESS, writer)
    except (ValueError, TypeError, KeyError):
        await p._send_error(seq, op, p.error_types.INVALID_PAYLOAD, writer)
