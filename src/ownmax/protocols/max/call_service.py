from ownmax.settings import (
    DATA_DIR,
    PUBLIC_URL,
    SIGNAL_URL,
    TURN_URL,
    STUN_URL,
    CALL_PHONES,
    HTTP_HOST,
    HTTP_PORT,
)
import asyncio, base64, hashlib, hmac, json, logging, secrets, time, uuid
from pathlib import Path
from urllib.parse import urlencode, urlparse
import lz4.block
from aiohttp import web, WSMsgType
from ownmax.infrastructure.proto_tcp import MobileProto
from ownmax.infrastructure.proto_web import WebProto

log = logging.getLogger("ownmax.calls")
BASE = PUBLIC_URL
WSS = SIGNAL_URL
KEYFILE = DATA_DIR / "call-secrets.json"
SERVICE = None


def keys():
    return json.loads(KEYFILE.read_text())


def token(uid, kind="api", room="", lifetime=3600):
    raw = json.dumps(
        {"uid": int(uid), "kind": kind, "room": room, "exp": int(time.time()) + lifetime},
        separators=(",", ":"),
    ).encode()
    data = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    sig = hmac.new(keys()["signing"].encode(), data.encode(), hashlib.sha256).hexdigest()
    return "$" + data + "." + sig


def verify(value, kind, room=None):
    if not isinstance(value, str):
        raise web.HTTPUnauthorized(text="Invalid call credential")
    try:
        data, sig = value.removeprefix("$").rsplit(".", 1)
        expect = hmac.new(keys()["signing"].encode(), data.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expect):
            raise ValueError()
        body = json.loads(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)))
        if (
            body["exp"] < time.time()
            or body["kind"] != kind
            or (room is not None and body["room"] != room)
        ):
            raise ValueError()
        return int(body["uid"])
    except (ValueError, TypeError, KeyError):
        raise web.HTTPUnauthorized(text="Invalid or expired call credential")


class CallService:

    def __init__(self, clients, allowed, state_path=None):
        self.clients, self.allowed = (clients, set(allowed))
        self.sessions, self.rooms, self.rate = ({}, {}, {})
        self.db = None
        self.state_path = Path(state_path) if state_path else None
        if self.state_path and self.state_path.exists():
            try:
                for item in json.loads(self.state_path.read_text()):
                    rid = str(uuid.UUID(item["id"]))
                    users = [int(u) for u in item["users"]]
                    if (
                        len(users) != 2
                        or not set(users) <= self.allowed
                        or time.time() - item["created"] > 86400
                    ):
                        continue
                    self.rooms[rid] = {
                        **item,
                        "id": rid,
                        "users": users,
                        "accepted": set(),
                        "peers": {},
                        "backlog": {u: [] for u in users},
                        "media": {},
                        "ended": False,
                        "posted": True,
                        "last_activity": time.time(),
                    }
            except (ValueError, TypeError, KeyError, OSError):
                log.exception("Could not restore call link metadata")

    def persist_links(self, remove=None):
        if not self.state_path:
            return
        fields = ("id", "caller", "users", "video", "created", "chat_id", "join_link")
        records = [
            {k: r[k] for k in fields}
            for r in self.rooms.values()
            if r["id"] != remove and (not r.get("ended"))
        ]
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(records))
        tmp.chmod(384)
        tmp.replace(self.state_path)

    def allow(self, uid):
        if uid not in self.allowed:
            raise web.HTTPForbidden(text="Account is not enabled for calls")

    def turn(self, uid):
        username = f"{int(time.time()) + 3600}:{uid}"
        password = base64.b64encode(
            hmac.new(keys()["turn"].encode(), username.encode(), hashlib.sha1).digest()
        ).decode()
        return {"urls": [TURN_URL], "username": username, "credential": password}

    @staticmethod
    def topology(room):
        marker = DATA_DIR / "native-sfu-test-enabled"
        try:
            targets = {int(v.strip()) for v in marker.read_text().split(",")}
        except (FileNotFoundError, ValueError, OSError):
            return "DIRECT"
        return "SERVER" if int(room.get("chat_id") or 0) in targets else "DIRECT"

    def info(self, uid, room):
        t = token(uid, "signal", room["id"], 600)
        return {
            "id": room["id"],
            "token": t,
            "endpoint": WSS
            + "?"
            + urlencode(
                {
                    "userId": uid,
                    "entityType": "USER",
                    "deviceIdx": 0,
                    "conversationId": room["id"],
                    "token": t,
                }
            ),
            "turn_server": self.turn(uid),
            "stun_server": {"urls": [STUN_URL]},
            "client_type": "ONE_ME",
            "device_idx": 0,
            "is_concurrent": False,
            "p2p_forbidden": self.topology(room) == "SERVER",
            "join_link": room.get("join_link", ""),
            "chat_id": room.get("chat_id", 0),
        }

    async def notify(self, uid, opcode, payload):
        count = 0
        for c in list(self.clients.get(uid, {}).get("clients", [])):
            if c.get("protocol") != "oneme":
                continue
            try:
                proto = WebProto() if c.get("type") == "web" else MobileProto()
                data = proto.pack_packet(cmd=0, seq=1, opcode=opcode, payload=payload)
                if c.get("type") == "web":
                    await c["writer"].send(data)
                else:
                    c["writer"].write(data)
                    await c["writer"].drain()
                count += 1
            except (ConnectionError, RuntimeError):
                log.warning("Call notification connection closed")
        return count

    def vcp(self, room, uid):
        info = self.info(uid, room)
        vcp = {
            "srcp": "one_me",
            "iv": bool(room["video"]),
            "et": int(time.time()) + 600,
            "tkn": info["token"],
            "wse": WSS,
            "stne": STUN_URL,
            "trne": ",".join(info["turn_server"]["urls"]),
            "trnu": info["turn_server"]["username"],
            "trnp": info["turn_server"]["credential"],
        }
        raw = json.dumps(vcp, separators=(",", ":")).encode()
        packed = (
            str(len(raw))
            + ":"
            + base64.b64encode(lz4.block.compress(raw, store_size=False)).decode()
        )
        return packed

    async def incoming(self, room, uid):
        await self.notify(
            uid,
            137,
            {
                "vcp": self.vcp(room, uid),
                "callerId": room["caller"],
                "conversationId": room["id"],
                "chatId": room.get("chat_id", 0),
                "type": "VIDEO" if room["video"] else "AUDIO",
                "isContact": True,
                "isVideo": room["video"],
                "is_video": room["video"],
                "time": int(time.time() * 1000),
            },
        )

    async def start_native(self, uid, payload):
        self.allow(uid)
        rid = str(uuid.UUID(payload["conversationId"]))
        targets = payload.get("calleeIds", [])
        if len(targets) != 1:
            raise ValueError("One callee required")
        target = int(targets[0])
        self.allow(target)
        if target == uid:
            raise ValueError("Use two separate test accounts")
        return {"conversationId": rid}

    async def http(self, request):
        if request.method == "GET" and request.path == "/fb.do" and (not request.query):
            return web.json_response({"service": "OwnMAX private calls", "experimental": True})
        form = dict(request.query)
        if request.method == "POST":
            form.update(await request.post())
        method = str(form.get("method", ""))
        if request.match_info.get("group"):
            method = request.match_info["group"] + "." + request.match_info["action"]
        peer = request.remote
        now = time.monotonic()
        hits = [t for t in self.rate.get(peer, []) if now - t < 60]
        if len(hits) >= 180:
            raise web.HTTPTooManyRequests()
        self.rate[peer] = hits + [now]
        if len(self.rate) > 512:
            self.rate = {peer: hits}
        log.info("Calls HTTP method=%s keys=%s", method, ",".join(sorted(form.keys())))
        if method == "auth.anonymLogin":
            try:
                sd = json.loads(form.get("session_data", "{}"))
            except (ValueError, TypeError):
                raise web.HTTPBadRequest()
            uid = verify(sd.get("auth_token", ""), "api")
            self.allow(uid)
            self.sessions = {k: v for k, v in self.sessions.items() if v["exp"] > time.time()}
            if len(self.sessions) > 128:
                raise web.HTTPServiceUnavailable()
            sk = secrets.token_urlsafe(48)
            self.sessions[sk] = {"uid": uid, "exp": time.time() + 3600}
            return web.json_response(
                {
                    "uid": str(uid),
                    "session_key": sk,
                    "session_secret_key": secrets.token_urlsafe(24),
                    "api_server": BASE,
                    "external_user_id": str(uid),
                }
            )
        session = self.sessions.get(form.get("session_key"))
        if not session or session["exp"] < time.time():
            raise web.HTTPUnauthorized()
        uid = session["uid"]
        self.allow(uid)
        if method == "system.getInfo":
            return web.json_response(
                {
                    "uid": uid,
                    "time": int(time.time() * 1000),
                    "server_time": int(time.time() * 1000),
                    "serverTime": int(time.time() * 1000),
                    "scopes": ["calls", "history", "audio", "video"],
                }
            )
        if method == "vchat.startConversation":
            try:
                payload = json.loads(form.get("payload") or "{}")
                chat_id = int(payload.get("chat_id") or payload.get("chatId") or 0)
                rid = str(uuid.UUID(form.get("conversationId", "")))
                if chat_id:
                    previews = [
                        r
                        for r in self.rooms.values()
                        if r.get("chat_id") == chat_id
                        and (not r["ended"])
                        and (not r["accepted"])
                        and (not any(r["peers"].values()))
                    ]
                    for preview in previews:
                        self.rooms.pop(preview["id"], None)
                    existing = next(
                        (
                            r
                            for r in self.rooms.values()
                            if r.get("chat_id") == chat_id and (not r["ended"])
                        ),
                        None,
                    )
                    users = await self.group_members(uid, chat_id, starting=existing is None)
                    if existing:
                        await self.accept_group_join(existing, uid)
                        return web.json_response(self.info(uid, existing))
                else:
                    text = str(form.get("externalIds", "")).strip()
                    recipients = json.loads(text) if text.startswith("[") else text.split(",")
                    target = int(recipients[0])
                    if len(recipients) != 1 or target == uid:
                        raise ValueError()
                    self.allow(target)
                    users = [uid, target]
            except (ValueError, TypeError, IndexError):
                raise web.HTTPBadRequest(
                    text="A private group or one recipient and a UUID are required"
                )
            if rid in self.rooms:
                room = self.rooms[rid]
                if uid not in room["users"]:
                    raise web.HTTPForbidden()
                return web.json_response(self.info(uid, room))
            if len(self.rooms) >= 8:
                raise web.HTTPTooManyRequests()
            room = {
                "id": rid,
                "caller": uid,
                "users": users,
                "video": str(form.get("isVideo")).lower() == "true",
                "created": time.time(),
                "accepted": {uid},
                "peers": {},
                "backlog": {u: [] for u in users},
                "media": {},
                "ended": False,
                "chat_id": chat_id,
                "join_link": "https://max.ru/joincall/" + rid if chat_id else "",
                "posted": False,
            }
            self.rooms[rid] = room
            self.persist_links()
            if chat_id:
                room["post_task"] = asyncio.create_task(self.post_group_link(room))
            if not chat_id:
                for target in users:
                    if target != uid:
                        await self.incoming(room, target)
            log.info("Call created users=%s group=%s", room["users"], bool(chat_id))
            return web.json_response(self.info(uid, room))
        if method in (
            "vchat.joinConversation",
            "vchat.joinConversationByLink",
            "vchat.getConversation",
            "vchat.getConversationParams",
        ):
            raw = form.get("conversationId") or form.get("joinLink") or form.get("join_link") or ""
            rid = self.link_id(raw)
            room = self.rooms.get(rid)
            if room is None:
                room = await self.resolve_published_link(uid, rid)
            if not room or uid not in room["users"]:
                raise web.HTTPForbidden()
            if room.get("chat_id"):
                await self.group_members(uid, room["chat_id"])
                if method in ("vchat.joinConversation", "vchat.joinConversationByLink"):
                    await self.accept_group_join(room, uid)
            return web.json_response(self.info(uid, room))
        if method in ("vchat.hangupConversation", "vchat.leaveConversation"):
            room = self.rooms.get(form.get("conversationId"))
            if room:
                if uid not in room["users"]:
                    raise web.HTTPForbidden()
                await self.leave(room, uid, str(form.get("reason", "HUNGUP")))
            return web.json_response({})
        if method == "vchat.getHistory":
            return web.json_response({"history": [], "has_more": False})
        if method in (
            "vchat.clientStats",
            "vchat.logClientStats",
            "vchat.clientSupportedCodecs",
            "auth.logout",
        ):
            return web.json_response({})
        return web.json_response(
            {"error_code": 3, "error_msg": "Unsupported private call method"}, status=400
        )

    @staticmethod
    def link_id(value):
        text = str(value).strip()
        if "://" in text:
            url = urlparse(text)
            if url.hostname != "max.ru" or url.scheme not in ("https", "max"):
                raise ValueError()
            text = url.path
        text = text.strip("/")
        if text.startswith("joincall/"):
            text = text[len("joincall/") :]
        return str(uuid.UUID(text))

    async def group_members(self, uid, chat_id, starting=False):
        if self.db is None:
            raise web.HTTPServiceUnavailable()
        async with self.db.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT * FROM chats WHERE id=%s", (chat_id,))
                chat = await cur.fetchone()
                await cur.execute(
                    "SELECT user_id FROM chat_participants WHERE chat_id=%s", (chat_id,)
                )
                users = [int(r["user_id"]) for r in await cur.fetchall()]
        if (
            not chat
            or chat.get("is_deleted")
            or chat["type"] not in ("CHAT", "CHANNEL")
            or (uid not in users)
        ):
            raise web.HTTPForbidden()
        opts = json.loads(chat.get("options") or "{}")
        if starting and (chat["type"] == "CHANNEL" or opts.get("ONLY_ADMIN_CAN_CALL")):
            from ownmax.protocols.max.permissions import mask

            async with self.db.acquire() as conn:
                async with conn.cursor() as cur:
                    rights = await mask(cur, chat, uid)
            if not rights & 64:
                raise web.HTTPForbidden()
        if len(users) > 2:
            raise web.HTTPBadRequest(
                text="This private native media transport currently supports two participants"
            )
        for user in users:
            self.allow(user)
        return [uid] + [u for u in users if u != uid]

    async def post_group_link(self, room):
        if room["ended"] or room.get("posted") or (not room.get("chat_id")):
            return
        room["posted"] = True
        try:
            from ownmax.infrastructure.tools import Tools

            tools = Tools()
            rid = room["id"]
            chat = room["chat_id"]
            async with self.db.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT id FROM messages WHERE chat_id=%s AND text LIKE %s LIMIT 1",
                        (chat, "%" + room["join_link"] + "%"),
                    )
                    if await cur.fetchone():
                        return
            text = "Групповой звонок\n" + room["join_link"]
            mid, prev, when = await tools.insert_message(
                chat, room["caller"], text, [], [], time.time_ns() // 1000000, "USER", self.db
            )
            message = {
                "id": mid,
                "cid": when,
                "chatId": chat,
                "time": when,
                "type": "USER",
                "sender": room["caller"],
                "text": text,
                "attaches": [],
                "elements": [],
                "reactionInfo": {},
            }
            for user in room["users"]:
                await self.notify(
                    user,
                    128,
                    {
                        "chatId": chat,
                        "message": message,
                        "prevMessageId": prev,
                        "ttl": False,
                        "unread": 0,
                        "mark": when,
                    },
                )
            log.info("Group call link posted once chatId=%s", chat)
        except Exception:
            room["posted"] = False
            log.exception("Could not post private group call link")

    async def resolve_published_link(self, uid, value):
        rid = self.link_id(value)
        room = self.rooms.get(rid)
        if room and (not room.get("ended")):
            await self.group_members(uid, room["chat_id"])
            return room
        if self.db is None:
            raise web.HTTPNotFound()
        link = "https://max.ru/joincall/" + rid
        async with self.db.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT m.chat_id,m.sender,m.text FROM messages m WHERE m.text LIKE %s AND NOT EXISTS (SELECT 1 FROM message_deletions d WHERE d.message_id=m.id AND d.user_id=0) ORDER BY m.time DESC LIMIT 20",
                    ("%" + link + "%",),
                )
                candidates = await cur.fetchall()
        message = next((m for m in candidates if link in m["text"].split()), None)
        if message is None:
            raise web.HTTPNotFound()
        chat = int(message["chat_id"])
        users = await self.group_members(uid, chat)
        if len(self.rooms) >= 8:
            raise web.HTTPTooManyRequests()
        room = {
            "id": rid,
            "caller": int(message["sender"]),
            "users": users,
            "video": False,
            "created": time.time(),
            "accepted": set(),
            "peers": {},
            "backlog": {u: [] for u in users},
            "media": {},
            "ended": False,
            "posted": True,
            "chat_id": chat,
            "join_link": link,
            "last_activity": time.time(),
        }
        self.rooms[rid] = room
        self.persist_links()
        return room

    async def group_wire(self, uid, opcode, payload):
        raw = (
            payload.get("conversationId")
            or payload.get("callId")
            or payload.get("joinLink")
            or payload.get("link")
        )
        room = (
            await self.resolve_published_link(uid, raw)
            if raw
            else next(
                (
                    r
                    for r in self.rooms.values()
                    if r.get("chat_id") == int(payload.get("chatId") or 0) and (not r["ended"])
                ),
                None,
            )
        )
        if not room or not room.get("chat_id"):
            raise web.HTTPNotFound()
        await self.group_members(uid, room["chat_id"])
        log.info("Private call lookup opcode=%s chat=%s", opcode, room["chat_id"])
        if opcode == 89:
            from ownmax.infrastructure.tools import Tools

            async with self.db.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT * FROM users WHERE id=%s", (room["caller"],))
                    owner = await cur.fetchone()
                    await cur.execute("SELECT title FROM chats WHERE id=%s", (room["chat_id"],))
                    chat = await cur.fetchone()
            contact = Tools().generate_profile(
                id=int(owner["id"]),
                phone=int(owner["phone"]),
                updateTime=int(owner["updatetime"]),
                firstName=owner["firstname"],
                lastName=owner["lastname"],
                options=json.loads(owner["options"]),
                accountStatus=int(owner["accountstatus"]),
                includeProfileOptions=False,
            )
            return {
                "videoConference": {
                    "owner": contact,
                    "conferenceId": room["id"],
                    "joinLink": room["join_link"],
                    "callType": "VIDEO" if room["video"] else "AUDIO",
                    "callName": chat["title"],
                    "participantsCount": len(room["accepted"]),
                    "startAt": int(room["created"] * 1000),
                    "previewParticipantIds": list(room["accepted"]),
                    "chatId": room["chat_id"],
                }
            }
        if opcode == 166:
            log.info("Group join preflight uid=%s keys=%s", uid, ",".join(sorted(payload)))
            return {"conversationId": room["id"], "internalParams": self.vcp(room, uid)}
        if opcode == 195:
            return {
                "members": [{"userId": u, "id": u} for u in room["accepted"]],
                "participants": self.participants(room),
                "count": len(room["accepted"]),
            }
        return {
            "chatId": room["chat_id"],
            "conversationId": room["id"],
            "callId": room["id"],
            "joinLink": room["join_link"],
            "link": room["join_link"],
            "vcp": self.vcp(room, uid),
            "url": room["join_link"],
        }

    async def accept_group_join(self, room, uid):
        if uid in room["accepted"]:
            return
        room["accepted"].add(uid)
        log.info("Group participant accepted explicit join uid=%s", uid)
        if not any((not ws.closed for ws in room["peers"].get(uid, []))):
            return
        if any((not ws.closed for ws in room["peers"].get(uid, []))):
            await self.broadcast(
                room,
                {
                    "stamp": time.time_ns(),
                    "type": "notification",
                    "notification": "registered-peer",
                    "peerId": self.peer_id(room, uid),
                    "participantId": uid,
                    "participantType": "USER",
                    "clientType": "ONE_ME",
                },
                exclude=uid,
            )
        await self.broadcast(room, self.accepted_event(room, uid), exclude=uid)

    def participant_event(self, room, uid, notification):
        participant = dict(next((p for p in self.participants(room) if p["id"] == uid)))
        if notification == "participant-added":
            participant["state"] = "CALLED"
        return {
            "stamp": time.time_ns(),
            "type": "notification",
            "notification": notification,
            "callerId": room["caller"],
            "participant": participant,
            "participantId": uid,
            "participantType": "USER",
            "peerId": self.peer_id(room, uid),
            "mediaSettings": participant["mediaSettings"],
            "externalId": participant["externalId"],
            "roles": participant["roles"],
            "permissions": participant["permissions"],
        }

    def accepted_event(self, room, uid):
        return {
            "stamp": time.time_ns(),
            "type": "notification",
            "notification": "accepted-call",
            "participantId": uid,
            "participantType": "USER",
            "peerId": self.peer_id(room, uid),
            "mediaSettings": room["media"].get(
                uid, {"isAudioEnabled": True, "isVideoEnabled": room["video"]}
            ),
            "deviceIdx": 0,
            "deviceCount": 1,
            "conversation": {"id": room["id"], "participants": self.participants(room)},
        }

    @staticmethod
    def peer_id(room, uid):
        return {"id": room.get("peer_ids", {}).get(uid, uid), "type": "WEB_TRANSPORT"}

    def participants(self, room, include_uid=None, defer_remote_accept=False):
        return [
            {
                "id": u,
                "externalId": {"type": "ONE_ME", "id": str(u)},
                "state": (
                    "ACCEPTED"
                    if u in room["accepted"] and (not defer_remote_accept or u == include_uid)
                    else "CALLED"
                ),
                "roles": ["CREATOR"] if u == room["caller"] else [],
                "mediaSettings": room["media"].get(
                    u, {"isAudioEnabled": True, "isVideoEnabled": room["video"]}
                ),
                "permissions": [],
            }
            for u in room["users"]
            if not room.get("chat_id") or u in room["accepted"] or u == include_uid
        ]

    async def broadcast(self, room, data, exclude=None, queue=False, target=None):
        for uid in room["users"]:
            if uid == exclude or (target is not None and uid != target):
                continue
            sockets = list(room["peers"].get(uid, []))
            if not sockets and queue:
                room["backlog"][uid].append(data)
                room["backlog"][uid] = room["backlog"][uid][-128:]
            for ws in sockets:
                if not ws.closed:
                    await ws.send_json(data)

    async def leave(self, room, uid, reason):
        if not room.get("chat_id"):
            await self.end(room, uid, reason)
            return
        if room["ended"]:
            return
        if uid not in room["accepted"] and (not room["peers"].get(uid)):
            return
        was_present = uid in room["accepted"]
        removed = self.participant_event(room, uid, "participant-removed") if was_present else None
        room["accepted"].discard(uid)
        sockets = list(room["peers"].pop(uid, set()))
        room["media"].pop(uid, None)
        room.setdefault("hands", {}).pop(uid, None)
        room.setdefault("participant_states", {}).pop(uid, None)
        room["backlog"].setdefault(uid, []).clear()
        room["last_activity"] = time.time()
        if was_present:
            removed.update(reason=reason, deviceCount=0)
            await self.broadcast(room, removed, exclude=uid)
            await self.broadcast(
                room,
                {
                    "stamp": time.time_ns(),
                    "type": "notification",
                    "notification": "session-state",
                    "participantId": uid,
                    "participantType": "USER",
                    "deviceIdx": 0,
                    "connected": False,
                    "markers": [],
                },
                exclude=uid,
            )
            await self.broadcast(
                room,
                {
                    "stamp": time.time_ns(),
                    "type": "notification",
                    "notification": "unregistered-peer",
                    "participantId": uid,
                    "participantType": "USER",
                    "peerId": self.peer_id(room, uid),
                },
                exclude=uid,
            )
        if room.get("sfu"):
            await room["sfu"].remove(uid)
        for socket in sockets:
            if not socket.closed:
                await socket.close()
        log.info("Group participant left uid=%s remaining=%s", uid, len(room["accepted"]))
        if not room["accepted"] and (not any(room["peers"].values())):
            await self.end(room, uid, "EMPTY")

    async def end(self, room, uid, reason):
        if room["ended"]:
            return
        if reason != "SERVER_STOP":
            self.persist_links(remove=room["id"])
        room["ended"] = True
        for task in room.get("disconnect_tasks", {}).values():
            if task is not asyncio.current_task():
                task.cancel()
        room.get("disconnect_tasks", {}).clear()
        await self.broadcast(
            room,
            {
                "stamp": time.time_ns(),
                "type": "notification",
                "notification": "hungup",
                "participantId": uid,
                "participantType": "USER",
                "reason": reason,
                "peerId": self.peer_id(room, uid),
                "deviceCount": 0,
            },
        )
        await self.broadcast(
            room,
            {
                "stamp": time.time_ns(),
                "type": "notification",
                "notification": "closed-conversation",
                "reason": reason,
            },
        )
        for sockets in room["peers"].values():
            for ws in list(sockets):
                await ws.close()
        if room.get("sfu"):
            await room["sfu"].close()
        self.rooms.pop(room["id"], None)
        log.info("Call ended reason=%s", reason)

    async def signal(self, request):
        rid = request.query.get("conversationId", "")
        uid = verify(request.query.get("token", ""), "signal", rid)
        self.allow(uid)
        if str(uid) != request.query.get("userId"):
            raise web.HTTPForbidden()
        room = self.rooms.get(rid)
        if not room or uid not in room["users"] or room["ended"]:
            raise web.HTTPForbidden()
        if room.get("chat_id"):
            await self.group_members(uid, room["chat_id"])
        if len(room["peers"].get(uid, [])) >= 2:
            raise web.HTTPTooManyRequests()
        try:
            peer_id = int(request.query.get("peerId", uid))
            if peer_id == 0 or not -9223372036854775808 <= peer_id <= 9223372036854775807:
                raise ValueError()
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(text="Invalid peer identity")
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=262144)
        await ws.prepare(request)
        room["peers"].setdefault(uid, set()).add(ws)
        room.setdefault("peer_ids", {})[uid] = peer_id
        room["last_activity"] = time.time()
        disconnect = room.setdefault("disconnect_tasks", {}).pop(uid, None)
        if disconnect:
            disconnect.cancel()
        if uid == room["caller"] and room.get("chat_id") and (not room.get("post_task")):
            room["post_task"] = asyncio.create_task(self.post_group_link(room))
        log.info("Call signaling connected uid=%s", uid)
        await ws.send_json(
            {
                "stamp": time.time_ns(),
                "type": "notification",
                "notification": "connection",
                "peerId": self.peer_id(room, uid),
                "endpoint": self.info(uid, room)["endpoint"],
                "conversationParams": {
                    "turn": self.turn(uid),
                    "stun": {"urls": [STUN_URL]},
                    "serverTime": int(time.time() * 1000),
                    "activityTimeout": 120000,
                },
                "conversation": {
                    "id": rid,
                    "state": "ACTIVE",
                    "topology": self.topology(room),
                    "participants": self.participants(
                        room,
                        include_uid=uid,
                        defer_remote_accept=bool(room.get("chat_id"))
                        and self.topology(room) == "DIRECT",
                    ),
                    "participantsLimit": 2,
                    "features": ["HAND_UP"],
                    "featuresPerRole": {},
                    "options": [],
                    "clientType": "ONE_ME",
                    "handCount": len(room.get("hands", {})),
                },
                "isConcurrent": False,
                "mediaModifiers": {"denoise": True, "denoiseAnn": True},
            }
        )
        await ws.send_json(
            {
                "stamp": time.time_ns(),
                "type": "notification",
                "notification": "settings-update",
                "camera": {"maxDimension": 1280, "maxBitrateK": 1500, "maxFramerate": 30},
                "screenSharing": {"maxDimension": 1920, "maxBitrateK": 2000, "maxFramerate": 15},
                "settings": {
                    "badNet": {"rtt": 1000, "loss": 7},
                    "goodNet": {"rtt": 600, "loss": 0.5},
                },
            }
        )
        await ws.send_json(
            {
                "stamp": time.time_ns(),
                "type": "notification",
                "notification": "topology-changed",
                "topology": self.topology(room),
            }
        )
        await asyncio.sleep(0.5)
        if ws.closed:
            return ws
        if room.get("chat_id") and uid in room["accepted"]:
            await self.broadcast(
                room, self.participant_event(room, uid, "participant-added"), exclude=uid
            )
        if not room.get("chat_id") or uid in room["accepted"]:
            await self.broadcast(
                room,
                {
                    "stamp": time.time_ns(),
                    "type": "notification",
                    "notification": "registered-peer",
                    "peerId": self.peer_id(room, uid),
                    "participantType": "USER",
                    "participantId": uid,
                    "platform": request.query.get("platform", ""),
                    "clientType": "ONE_ME",
                },
                exclude=uid,
            )
        if room.get("chat_id") and uid in room["accepted"]:
            await self.broadcast(room, self.accepted_event(room, uid), exclude=uid)
        for other, sockets in room["peers"].items():
            if (
                other != uid
                and other in room["accepted"]
                and any((not peer.closed for peer in sockets))
            ):
                await ws.send_json(
                    {
                        "stamp": time.time_ns(),
                        "type": "notification",
                        "notification": "registered-peer",
                        "peerId": self.peer_id(room, other),
                        "participantId": other,
                        "participantType": "USER",
                        "platform": "DESKTOP",
                        "clientType": "ONE_ME",
                    }
                )
        for data in room["backlog"][uid]:
            await ws.send_json(data)
        room["backlog"][uid].clear()
        for participant, state in room.get("participant_states", {}).items():
            if not isinstance(state, dict) or not state.get("state"):
                continue
            await ws.send_json(
                {
                    "stamp": time.time_ns(),
                    "type": "notification",
                    "notification": "participant-state-changed",
                    "peerId": self.peer_id(room, participant),
                    "participantId": participant,
                    "participantType": "USER",
                    "participantState": state,
                    "handCount": len(room.get("hands", {})),
                }
            )

        async def application_heartbeat():
            try:
                while not ws.closed:
                    await asyncio.sleep(3)
                    if not ws.closed:
                        await ws.send_str("ping")
            except (ConnectionError, RuntimeError):
                pass

        ping_task = asyncio.create_task(application_heartbeat())
        pong_count = 0
        try:
            async for msg in ws:
                room["last_activity"] = time.time()
                if msg.type != WSMsgType.TEXT:
                    if msg.type == WSMsgType.BINARY and msg.data == b"pong":
                        continue
                    if msg.type == WSMsgType.BINARY and msg.data == b"ping":
                        await ws.send_bytes(b"pong")
                        continue
                    if msg.type == WSMsgType.BINARY:
                        log.warning("Unsupported binary call signaling length=%s", len(msg.data))
                    continue
                if msg.data == "pong":
                    pong_count += 1
                    if pong_count == 1:
                        log.info("Native application heartbeat acknowledged uid=%s", uid)
                    continue
                if msg.data == "ping":
                    await ws.send_str("pong")
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                if not isinstance(data, dict):
                    continue
                command = data.get("command", "")
                log.info(
                    "Call signaling command=%s keys=%s", command, ",".join(sorted(data.keys()))
                )
                reply = {
                    "stamp": 0,
                    "type": "response",
                    "response": command,
                    "sequence": data.get("sequence", 0),
                }
                if command == "accept-call":
                    room["accepted"].add(uid)
                    room["media"][uid] = data.get("mediaSettings", {})
                    reply.update(
                        participantIds=[uid], participantTypes=["USER"], participantDeviceIdxs=[0]
                    )
                    await self.broadcast(room, self.accepted_event(room, uid), exclude=uid)
                    sfu = room.get("sfu")
                    consumer = sfu.consumers.get(uid) if sfu else None
                    if (
                        consumer
                        and consumer.signalingState == "have-local-offer"
                        and consumer.localDescription
                    ):
                        sfu.queue_sdp(uid, "consumer", consumer.localDescription)
                        log.info("Replaying pending media offer after explicit accept uid=%s", uid)
                elif (
                    command
                    in (
                        "allocate-consumer",
                        "consumer-answered",
                        "accept-producer",
                        "producer-updated",
                        "change-streams-priorities",
                    )
                    or (
                        command == "transmit-data"
                        and (
                            data.get("participantType") in ("CONSUMER", "PRODUCER")
                            or (data.get("peerId") or {}).get("type") in ("CONSUMER", "PRODUCER")
                        )
                    )
                ) and self.topology(room) == "SERVER":
                    from ownmax.protocols.max.native_sfu import NativeRelay

                    if "sfu" not in room:
                        room["sfu"] = NativeRelay(self, room)
                    try:
                        await room["sfu"].handle(uid, command, data)
                    except (ValueError, RuntimeError, TypeError, KeyError):
                        log.exception(
                            "Native relay signaling failure command=%s uid=%s", command, uid
                        )
                        reply.update(error="media-negotiation-failed")
                    if not ws.closed:
                        await ws.send_json(reply)
                    await room["sfu"].flush(uid, ws)
                    continue
                elif command == "get-hand-queue":
                    hands = room.setdefault("hands", {})
                    reply.update(
                        participants=[
                            {"id": u, "participantId": u, "participantType": "USER", "addedTs": ts}
                            for u, ts in hands.items()
                        ],
                        participantIds=list(hands),
                        handCount=len(hands),
                        totalCount=len(hands),
                        firstParticipants=[
                            {
                                "id": u,
                                "participantId": u,
                                "participantType": "USER",
                                "addedTs": ts,
                                "participantState": {"state": {"hand": "1"}},
                            }
                            for u, ts in hands.items()
                        ],
                    )
                elif command in ("get-participants", "get-participant-list-chunk"):
                    reply.update(
                        participants=self.participants(room),
                        totalCount=len(room["users"]),
                        marker="",
                    )
                elif command == "change-participant-state":
                    state = data.get("participantState", {})
                    if not isinstance(state, (dict, str)):
                        reply.update(error="invalid-participant-state")
                        await ws.send_json(reply)
                        continue
                    flags = state.get("state", {}) if isinstance(state, dict) else {}
                    if not isinstance(flags, dict):
                        flags = {}
                    if flags:
                        room.setdefault("participant_states", {})[uid] = state
                    if state == "ACCEPTED":
                        room["accepted"].add(uid)
                    hands = room.setdefault("hands", {})
                    if flags.get("hand") in ("1", 1, True) or flags.get("HAND_UP") is True:
                        hands.setdefault(uid, int(time.time() * 1000))
                    else:
                        hands.pop(uid, None)
                    log.info(
                        "Participant state update uid=%s flags=%s handCount=%s",
                        uid,
                        sorted(flags),
                        len(hands),
                    )
                    reply.update(participantState=state, handCount=len(hands))
                    if flags:
                        await self.broadcast(
                            room,
                            {
                                "stamp": time.time_ns(),
                                "type": "notification",
                                "notification": "participant-state-changed",
                                "peerId": self.peer_id(room, uid),
                                "participantId": uid,
                                "participantType": "USER",
                                "participantState": state,
                                "handCount": len(hands),
                            },
                        )
                elif command == "put-hands-down":
                    targets = data.get("participantIds") or [uid]
                    try:
                        if not isinstance(targets, list) or len(targets) > len(room["users"]):
                            raise ValueError()
                        targets = [int(x) for x in targets]
                        if any((x not in room["users"] for x in targets)):
                            raise ValueError()
                        if uid != room["caller"] and any((x != uid for x in targets)):
                            raise ValueError()
                    except (ValueError, TypeError):
                        reply.update(error="permission-denied")
                        await ws.send_json(reply)
                        continue
                    hands = room.setdefault("hands", {})
                    for target in targets:
                        target = int(target)
                        hands.pop(target, None)
                        state = room.setdefault("participant_states", {}).get(target, {"state": {}})
                        if not isinstance(state, dict):
                            state = {"state": {}}
                        state = {**state, "state": {**state.get("state", {}), "hand": "0"}}
                        room["participant_states"][target] = state
                        await self.broadcast(
                            room,
                            {
                                "stamp": time.time_ns(),
                                "type": "notification",
                                "notification": "participant-state-changed",
                                "peerId": self.peer_id(room, target),
                                "participantId": target,
                                "participantType": "USER",
                                "participantState": state,
                                "handCount": len(hands),
                            },
                        )
                    reply.update(handCount=len(hands))
                elif command == "switch-topology":
                    requested = data.get("topology")
                    if requested == self.topology(room):
                        reply.update(topology=self.topology(room))
                        await self.broadcast(
                            room,
                            {
                                "stamp": time.time_ns(),
                                "type": "notification",
                                "notification": "topology-changed",
                                "topology": self.topology(room),
                            },
                        )
                    else:
                        reply.update(error="unsupported-topology")
                elif command == "update-media-modifiers":
                    reply["mediaModifiers"] = {
                        **data.get("mediaModifiers", {}),
                        "denoise": True,
                        "denoiseAnn": True,
                    }
                    await ws.send_json(
                        {
                            "stamp": time.time_ns(),
                            "type": "notification",
                            "notification": "media-modifiers-changed",
                            "mediaModifiers": reply["mediaModifiers"],
                        }
                    )
                elif command == "transmit-data":
                    target = data.get("participantId")
                    if target is not None and int(target) not in room["users"]:
                        raise web.HTTPForbidden()
                    await self.broadcast(
                        room,
                        {
                            "stamp": time.time_ns(),
                            "type": "notification",
                            "notification": "transmitted-data",
                            "participantId": uid,
                            "participantType": "USER",
                            "peerId": self.peer_id(room, uid),
                            "data": data.get("data"),
                        },
                        exclude=uid,
                        queue=True,
                        target=int(target) if target is not None else None,
                    )
                elif command == "change-media-settings":
                    room["media"][uid] = data.get("mediaSettings", {})
                    await self.broadcast(
                        room,
                        {
                            "stamp": time.time_ns(),
                            "type": "notification",
                            "notification": "media-settings-changed",
                            "participantId": uid,
                            "participantType": "USER",
                            "peerId": self.peer_id(room, uid),
                            "mediaSettings": room["media"][uid],
                        },
                        exclude=uid,
                    )
                elif command in ("hangup", "hangup-call", "leave"):
                    await self.leave(room, uid, data.get("reason", "HUNGUP"))
                    break
                elif command not in ("get-rooms", "custom-data", "update-display-layout", "ping"):
                    reply.update(error="unsupported-command")
                if not ws.closed:
                    await ws.send_json(reply)
                if command == "accept-call" and room.get("sfu"):
                    await room["sfu"].flush(uid, ws)
        finally:
            ping_task.cancel()
            await asyncio.gather(ping_task, return_exceptions=True)
            room["peers"].get(uid, set()).discard(ws)
            log.info("Call signaling disconnected uid=%s", uid)
            if (
                room.get("chat_id")
                and (not room["ended"])
                and (uid in room["accepted"])
                and (not any((not peer.closed for peer in room["peers"].get(uid, ()))))
            ):

                async def disconnect_grace():
                    await asyncio.sleep(5)
                    if not room["ended"] and (
                        not any((not peer.closed for peer in room["peers"].get(uid, ())))
                    ):
                        await self.leave(room, uid, "DISCONNECTED")

                previous = room.setdefault("disconnect_tasks", {}).pop(uid, None)
                if previous:
                    previous.cancel()
                room["disconnect_tasks"][uid] = asyncio.create_task(disconnect_grace())
        return ws

    @staticmethod
    def room_is_idle(room, now):
        connected = any((not ws.closed for peers in room["peers"].values() for ws in peers))
        return not connected and now - room.get("last_activity", room["created"]) > 600

    async def cleanup(self):
        while True:
            await asyncio.sleep(15)
            for room in list(self.rooms.values()):
                if self.room_is_idle(room, time.time()):
                    await self.end(room, room["caller"], "TIMEOUT")


async def launch(clients, db):
    global SERVICE
    allowed = []
    if CALL_PHONES:
        async with db.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id FROM users WHERE phone IN ("
                    + ",".join(("%s" for _ in CALL_PHONES))
                    + ")",
                    CALL_PHONES,
                )
                allowed = [int(r["id"]) for r in await cur.fetchall()]
    SERVICE = CallService(clients, allowed, DATA_DIR / "call-link-rooms.json")
    SERVICE.db = db
    app = web.Application(client_max_size=64 * 1024 * 1024)
    from ownmax.protocols.max.media_service import routes

    routes(app)
    from ownmax.protocols.max.bot_api import routes as bot_routes

    await bot_routes(app, SERVICE)
    app.router.add_route("*", "/fb.do", SERVICE.http)
    app.router.add_route("GET", "/ws2", SERVICE.signal)
    app.router.add_route("*", "/api/{group:auth|vchat|system}/{action}", SERVICE.http)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, HTTP_HOST, HTTP_PORT).start()
    log.info("OwnMAX calls listening on %s:%s", HTTP_HOST, HTTP_PORT)
    try:
        await SERVICE.cleanup()
    finally:
        for room in list(SERVICE.rooms.values()):
            await SERVICE.end(room, room["caller"], "SERVER_STOP")
        await runner.cleanup()
