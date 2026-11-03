import hashlib, json, time
from aiohttp import web
from ownmax.protocols.max.processors.messages import MessagesProcessors
from ownmax.protocols.max.bot_manager import ensure, ensure_dialog


class Capture:

    def __init__(self):
        self.data = []

    def write(self, data):
        self.data.append(data)

    async def drain(self):
        pass


class BotAPI:

    def __init__(self, service):
        self.service = service
        self.p = MessagesProcessors(
            db_pool=service.db, clients=service.clients, send_event=self.event
        )

    async def event(self, uid, event):
        kind = event.get("eventType")
        if kind == "new_msg":
            await self.service.notify(
                uid,
                128,
                {
                    "chatId": event["chatId"],
                    "message": event["message"],
                    "prevMessageId": event.get("prevMessageId", 0),
                    "unread": 0,
                    "mark": event["time"],
                },
            )
        elif kind == "chat_updated":
            await self.service.notify(uid, 135, {"chat": event["chat"]})
        elif kind == "message_deleted":
            await self.service.notify(
                uid,
                142,
                {
                    "chatId": event["chatId"],
                    "messageIds": event["messageIds"],
                    "forMe": event["forMe"],
                },
            )
        elif kind == "raw":
            await self.service.notify(uid, event["opcode"], event["payload"])

    async def profile_changed(self, bid, cleared=False):
        contacts = await self.p.tools.generate_contacts(
            [{"id": bid}], self.service.db, avatar_base_url=self.p.config.avatar_base_url
        )
        if not contacts:
            return
        contact = contacts[0]
        if cleared:
            contact.update(photoId=0, baseUrl="", baseRawUrl="")
        for uid in list(self.service.clients):
            await self.service.notify(uid, 131, {"contact": contact})

    async def auth(self, request):
        value = request.headers.get("Authorization", "")
        if value.startswith("Bearer "):
            value = value[7:]
        if not 32 <= len(value) <= 256:
            raise web.HTTPUnauthorized()
        digest = hashlib.sha256(value.encode()).hexdigest()
        async with self.service.db.acquire() as conn:
            async with conn.cursor() as c:
                await c.execute(
                    "SELECT b.*,u.username,u.firstname,u.description,u.avatar_id FROM bot_registry b JOIN users u ON u.id=b.bot_id JOIN users owner ON owner.id=b.owner_id WHERE b.token_hash=%s AND b.active=1",
                    (digest,),
                )
                row = await c.fetchone()
        if not row:
            raise web.HTTPUnauthorized()
        return row

    async def native_result(self, capture):
        if not capture.data:
            raise web.HTTPInternalServerError()
        packet = self.p.proto.unpack_packet(capture.data[-1])
        if packet["cmd"] == 3:
            raise web.HTTPBadRequest(
                text=json.dumps({"error": packet["payload"].get("error", "bad_request")}),
                content_type="application/json",
            )
        return packet["payload"]

    async def body(self, request):
        data = bytearray()
        async for chunk in request.content.iter_chunked(16384):
            data.extend(chunk)
            if len(data) > 65536:
                raise web.HTTPRequestEntityTooLarge(max_size=65536, actual_size=len(data))
        try:
            return json.loads(data)
        except (ValueError, UnicodeError):
            raise web.HTTPBadRequest()

    async def handler(self, request):
        try:
            return await self.dispatch(request)
        except (ValueError, TypeError, KeyError):
            raise web.HTTPBadRequest()

    async def dispatch(self, request):
        bot = await self.auth(request)
        bid = int(bot["bot_id"])
        path = request.match_info["method"]
        if path == "me" and request.method == "GET":
            return web.json_response(
                {
                    "user_id": bid,
                    "name": bot["firstname"],
                    "username": bot["username"],
                    "is_bot": True,
                    "description": bot.get("description") or "",
                    "avatar_url": self.p.tools.profile_avatar_url(bot.get("avatar_id"), ""),
                }
            )
        if path == "uploads" and request.method == "POST":
            if request.query.get("type", "PHOTO").upper() not in ("PHOTO", "IMAGE"):
                raise web.HTTPBadRequest()
            from ownmax.protocols.max.media_service import allocate

            return web.json_response(allocate(bid, 80, {"count": 1, "profile": True}))
        if path == "me" and request.method == "PATCH":
            body = await self.body(request)
            if not isinstance(body, dict):
                raise web.HTTPBadRequest()
            allowed = {"name": "firstname", "description": "description"}
            avatar = None
            if "photoToken" in body and body["photoToken"] != "":
                from ownmax.protocols.max.media_service import enrich, load

                media = enrich([{"_type": "PHOTO", "photoToken": body["photoToken"]}], bid)[0]
                if media["_type"] != "PHOTO" or load(str(media["photoId"])).get("mime") not in (
                    "image/png",
                    "image/jpeg",
                ):
                    raise web.HTTPBadRequest()
                avatar = int(media["photoId"])
            async with self.service.db.acquire() as conn:
                async with conn.cursor() as c:
                    for key, field in allowed.items():
                        if key in body:
                            value = body[key]
                            if (
                                not isinstance(value, str)
                                or len(value) > (100 if key == "name" else 1024)
                                or (key == "name" and (not value.strip()))
                            ):
                                raise web.HTTPBadRequest()
                            await c.execute(
                                "UPDATE users SET " + field + "=%s,updatetime=%s WHERE id=%s",
                                (value, str(int(time.time() * 1000)), bid),
                            )
                    if "photoToken" in body:
                        await c.execute(
                            "UPDATE users SET avatar_id=%s,updatetime=%s WHERE id=%s",
                            (avatar, str(int(time.time() * 1000)), bid),
                        )
            await self.profile_changed(bid, cleared="photoToken" in body and avatar is None)
            return web.json_response({"success": True})
        if path == "updates" and request.method == "GET":
            try:
                marker = max(0, int(request.query.get("marker", 0)))
                limit = max(1, min(100, int(request.query.get("limit", 50))))
            except ValueError:
                raise web.HTTPBadRequest()
            async with self.service.db.acquire() as conn:
                async with conn.cursor() as c:
                    await c.execute(
                        "SELECT m.* FROM messages m JOIN chat_participants cp ON cp.chat_id=m.chat_id AND cp.user_id=%s WHERE m.id>%s AND m.sender<>%s AND NOT EXISTS (SELECT 1 FROM message_deletions d WHERE d.message_id=m.id AND d.user_id IN (0,%s)) ORDER BY m.id LIMIT %s",
                        (bid, marker, bid, bid, limit),
                    )
                    rows = await c.fetchall()
            updates = [
                {
                    "update_type": "message_created",
                    "timestamp": int(r["time"]),
                    "message": {
                        "sender": {"user_id": int(r["sender"])},
                        "recipient": {"chat_id": int(r["chat_id"])},
                        "body": {
                            "mid": str(r["id"]),
                            "text": r["text"],
                            "attachments": self.p.tools.build_message_dict(r)["attaches"],
                        },
                    },
                }
                for r in rows
            ]
            return web.json_response(
                {"updates": updates, "marker": int(rows[-1]["id"]) if rows else marker}
            )
        if path == "messages" and request.method == "POST":
            body = await self.body(request)
            if (
                not isinstance(body, dict)
                or not isinstance(body.get("text", ""), str)
                or len(body.get("text", "")) > 16000
            ):
                raise web.HTTPBadRequest()
            payload = {"message": {"text": body.get("text", ""), "cid": time.time_ns() // 1000000}}
            if "chat_id" in request.query:
                payload["chatId"] = int(request.query["chat_id"])
            elif "user_id" in request.query:
                target = int(request.query["user_id"])
                chat = bid ^ target
                members = await self.p.tools.get_chat_participants(chat, self.service.db)
                if target != int(bot["owner_id"]) and (target not in members or bid not in members):
                    raise web.HTTPForbidden()
                await ensure_dialog(self.p, bid, target)
                payload["userId"] = target
            else:
                raise web.HTTPBadRequest()
            capture = Capture()
            await self.p.msg_send(payload, 1, capture, bid, self.service.db)
            result = await self.native_result(capture)
            return web.json_response({"message": result["message"]})
        if path == "messages" and request.method == "DELETE":
            try:
                mid = int(request.query["message_id"])
            except (ValueError, KeyError):
                raise web.HTTPBadRequest()
            async with self.service.db.acquire() as conn:
                async with conn.cursor() as c:
                    await c.execute(
                        "SELECT chat_id FROM messages WHERE id=%s AND sender=%s", (mid, bid)
                    )
                    message = await c.fetchone()
            if not message:
                raise web.HTTPForbidden()
            capture = Capture()
            await self.p.msg_delete(
                {"chatId": int(message["chat_id"]), "messageIds": [mid], "forMe": False},
                1,
                capture,
                bid,
            )
            await self.native_result(capture)
            return web.json_response({"success": True})
        raise web.HTTPNotFound()


async def routes(app, service):
    api = BotAPI(service)
    await ensure(api.p)
    app.router.add_route("*", "/botapi/{method:me|messages|updates|uploads}", api.handler)
