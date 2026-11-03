from ownmax.protocols.max.media_service import binary_default, binary_hook
import json
import random
import secrets
import time


class Tools:

    def build_message_dict(self, row, protocol_type="mobile"):
        try:
            attaches = json.loads(row.get("attaches") or "[]", object_hook=binary_hook)
        except (TypeError, ValueError):
            attaches = []
        try:
            elements = json.loads(row.get("elements") or "[]")
        except (TypeError, ValueError):
            elements = []
        from ownmax.protocols.max.media_service import load, attachment
        from aiohttp import web

        for index, attach in enumerate(attaches):
            kind = attach.get("_type")
            key = {
                "VIDEO": "videoId",
                "AUDIO": "audioId",
                "PHOTO": "photoId",
                "FILE": "fileId",
            }.get(kind)
            if key and attach.get(key):
                try:
                    media = load(str(attach[key]))
                    if media.get("ready") and media.get("kind") == kind:
                        attaches[index] = attachment(media, attach)
                except (web.HTTPException, ValueError, TypeError):
                    pass
        message = {
            "id": row.get("id") if protocol_type == "mobile" else str(row.get("id")),
            "cid": int(row.get("cid") or 0),
            "chatId": int(row.get("chat_id") or 0),
            "time": int(row.get("time")),
            "type": row.get("type") or "USER",
            "sender": row.get("sender"),
            "text": row.get("text") or "",
            "attaches": attaches,
            "elements": elements,
            "reactionInfo": {},
        }
        return message

    @staticmethod
    def profile_avatar_url(photo_id, fallback_base=""):
        if not photo_id:
            return None
        from ownmax.protocols.max.media_service import load, avatar_download

        try:
            media = load(str(photo_id))
            if media.get("ready") and media.get("kind") == "PHOTO":
                return avatar_download(media)
        except Exception:
            pass
        return fallback_base + str(photo_id) if fallback_base else None

    def generate_profile(
        self,
        id=None,
        phone=None,
        avatarUrl=None,
        photoId=None,
        updateTime=None,
        firstName=None,
        lastName=None,
        options=None,
        description=None,
        accountStatus=None,
        profileOptions=None,
        includeProfileOptions=True,
        username=None,
        custom_firstname=None,
        custom_lastname=None,
        blocked=False,
    ):
        contact = {
            "id": id,
            "updateTime": updateTime,
            "phone": phone,
            "names": [
                {"name": firstName, "firstName": firstName, "lastName": lastName, "type": "ONEME"}
            ],
            "options": options,
            "accountStatus": accountStatus,
            "location": "RU",
            "registrationTime": int(time.time() * 1000),
        }
        flags = 0
        options = options or []
        if "BOT" in options:
            flags |= 2
            contact.pop("phone", None)
        if "VERIFIED" in options or "OFFICIAL" in options:
            flags |= 1
        if "SERVICE_ACCOUNT" in options:
            flags |= 8
        contact["flags"] = flags
        if avatarUrl:
            contact["photoId"] = photoId
            contact["baseUrl"] = avatarUrl
            contact["baseRawUrl"] = avatarUrl
        if description:
            contact["description"] = description
        if username:
            contact["link"] = "https://max.ru/" + username
        if custom_firstname:
            contact["names"].append(
                {
                    "name": custom_firstname,
                    "firstName": custom_firstname,
                    "lastName": custom_lastname,
                    "type": "CUSTOM",
                }
            )
        if blocked:
            contact["status"] = "BLOCKED"
        if includeProfileOptions:
            return {"contact": contact, "profileOptions": profileOptions}
        else:
            return contact

    def generate_chat(
        self, id, owner, type, participants, lastMessage, lastEventTime, prevMessageId=0
    ):
        if isinstance(participants, dict):
            result_participants = {
                str(int(k)): int(v) if v is not None else 0 for k, v in participants.items()
            }
        else:
            result_participants = {str(int(participant)): 0 for participant in participants}
        result = None
        if type in ("DIALOG", "CHAT", "CHANNEL"):
            result = {
                "id": id,
                "type": type,
                "status": "ACTIVE",
                "owner": owner,
                "participants": result_participants,
                "lastMessage": lastMessage,
                "lastEventTime": lastEventTime,
                "lastDelayedUpdateTime": 0,
                "lastFireDelayedErrorTime": 0,
                "created": 1,
                "cid": id,
                "prevMessageId": prevMessageId,
                "joinTime": 1,
                "modified": lastEventTime,
            }
        return result

    async def generate_chats(
        self, chatIds, db_pool, senderId, include_favourites=True, protocol_type="mobile"
    ):
        chats = []
        for chatId in chatIds:
            async with db_pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT hidden FROM chat_visibility WHERE chat_id=%s AND user_id=%s",
                        (chatId, senderId),
                    )
                    visibility = await cur.fetchone()
                    if visibility and visibility["hidden"]:
                        continue
                    await cur.execute("SELECT is_deleted FROM chats WHERE id=%s", (chatId,))
                    state = await cur.fetchone()
                    if state and state["is_deleted"]:
                        continue
            async with db_pool.acquire() as db_connection:
                async with db_connection.cursor() as cursor:
                    await cursor.execute("SELECT * FROM `chats` WHERE id = %s", (chatId,))
                    row = await cursor.fetchone()
                    if row:
                        message, messageTime = await self.get_last_message(
                            chatId, db_pool, protocol_type=protocol_type, viewer_id=senderId
                        )
                        participant_ids = await self.get_chat_participants(chatId, db_pool)
                        participants = await self.get_participant_last_activity(
                            chatId, participant_ids, db_pool
                        )
                        prevMessageId = await self.get_previous_message_id(
                            chatId, db_pool, protocol_type=protocol_type, viewer_id=senderId
                        )
                        chats.append(
                            self.generate_chat(
                                row.get("id"),
                                row.get("owner"),
                                row.get("type"),
                                participants,
                                message,
                                messageTime,
                                prevMessageId,
                            )
                        )
        for chat in chats:
            if chat and chat["type"] == "DIALOG":
                async with db_pool.acquire() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute("SELECT options FROM chats WHERE id=%s", (chat["id"],))
                        metadata = await cur.fetchone()
                chat["options"] = json.loads(metadata["options"] or "{}") if metadata else {}
                async with db_pool.acquire() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "SELECT suspended FROM bot_states WHERE user_id=%s AND bot_id=%s",
                            (senderId, int(chat["id"]) ^ int(senderId)),
                        )
                        bot_state = await cur.fetchone()
                if bot_state:
                    chat["isSuspended"] = bool(bot_state["suspended"])
                if chat["options"].get("SERVICE_CHAT"):
                    chat["restrictions"] = 64 | 128 | 256
            if chat and chat["type"] in ("CHAT", "CHANNEL"):
                async with db_pool.acquire() as conn:
                    async with conn.cursor() as cursor:
                        await cursor.execute("SELECT * FROM chats WHERE id=%s", (chat["id"],))
                        meta = await cursor.fetchone()
                chat.update(
                    title=meta["title"],
                    description=meta["description"],
                    created=int(meta["created"]),
                    options=json.loads(meta["options"]),
                    participantsCount=len(chat["participants"]),
                    admins=[int(meta["owner"])],
                    adminParticipants={str(meta["owner"]): {"permissions": 4095}},
                    access="PRIVATE",
                    restrictions=0,
                )
                async with db_pool.acquire() as conn:
                    async with conn.cursor() as cursor:
                        await cursor.execute(
                            "SELECT user_id,permissions FROM chat_admins WHERE chat_id=%s",
                            (chat["id"],),
                        )
                        admin_rows = await cursor.fetchall()
                        if meta.get("pinned_message_id"):
                            await cursor.execute(
                                "SELECT * FROM messages WHERE chat_id=%s AND id=%s AND NOT EXISTS (SELECT 1 FROM message_deletions WHERE message_id=%s AND user_id IN (0,%s))",
                                (
                                    chat["id"],
                                    meta["pinned_message_id"],
                                    meta["pinned_message_id"],
                                    senderId,
                                ),
                            )
                            pinned = await cursor.fetchone()
                            if pinned:
                                chat["pinnedMessage"] = self.build_message_dict(
                                    pinned, protocol_type
                                )
                admin_map = {
                    str(r["user_id"]): {"permissions": int(r["permissions"])} for r in admin_rows
                }
                admin_map[str(meta["owner"])] = {"permissions": 4095}
                chat["admins"] = [int(k) for k in admin_map]
                chat["adminParticipants"] = admin_map
                chat["access"] = meta.get("access") or "PRIVATE"
                if meta.get("public_slug") and chat["access"] != "PRIVATE":
                    chat["link"] = "https://max.ru/" + meta["public_slug"]
                icon = meta.get("avatar_id")
                url = self.profile_avatar_url(int(icon), "") if icon else None
                chat.update(
                    baseIconUrl=url,
                    baseRawIconUrl=url,
                    iconId=int(icon) if icon else None,
                    modified=max(chat.get("modified", 0), int(meta.get("updated") or 0)),
                )
        if include_favourites:
            favouriteChatId = -senderId
            message, messageTime = await self.get_last_message(
                favouriteChatId, db_pool, protocol_type=protocol_type, viewer_id=senderId
            )
            chatId = senderId ^ senderId
            participants = await self.get_participant_last_activity(
                favouriteChatId, [senderId], db_pool
            )
            prevMessageId = await self.get_previous_message_id(
                favouriteChatId, db_pool, protocol_type=protocol_type, viewer_id=senderId
            )
            chats.append(
                self.generate_chat(
                    chatId, senderId, "DIALOG", participants, message, messageTime, prevMessageId
                )
            )
        return chats

    async def generate_contacts(self, contacts, db_pool, avatar_base_url=""):
        contact_list = []
        for contact in contacts:
            contact_id = contact.get("id")
            firstname = contact.get("firstname")
            lastname = contact.get("lastname")
            blocked = contact.get("blocked", False)
            async with db_pool.acquire() as db_connection:
                async with db_connection.cursor() as cursor:
                    await cursor.execute("SELECT * FROM `users` WHERE id = %s", (contact_id,))
                    user = await cursor.fetchone()
                    if user:
                        photoId = None if not user.get("avatar_id") else int(user.get("avatar_id"))
                        avatar_url = (
                            None
                            if not photoId
                            else self.profile_avatar_url(photoId, avatar_base_url)
                        )
                        description = (
                            None if not user.get("description") else user.get("description")
                        )
                        contact = self.generate_profile(
                            id=user.get("id"),
                            phone=int(user.get("phone")),
                            avatarUrl=avatar_url,
                            photoId=photoId,
                            updateTime=int(user.get("updatetime")),
                            firstName=user.get("firstname"),
                            lastName=user.get("lastname"),
                            options=json.loads(user.get("options")),
                            description=description,
                            accountStatus=int(user.get("accountstatus")),
                            includeProfileOptions=False,
                            username=user.get("username"),
                            custom_firstname=firstname,
                            custom_lastname=lastname,
                            blocked=blocked,
                        )
                        contact_list.append(contact)
        return contact_list

    async def collect_user_contacts(self, owner_id, db_pool, avatar_base_url=""):
        contacts = []
        async with db_pool.acquire() as db_connection:
            async with db_connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT * FROM `contacts` WHERE owner_id = %s AND is_blocked = FALSE",
                    (owner_id,),
                )
                rows = await cursor.fetchall()
                for row in rows:
                    contacts.append(
                        {
                            "id": int(row.get("contact_id")),
                            "firstname": row.get("custom_firstname"),
                            "lastname": row.get("custom_lastname"),
                            "blocked": bool(row.get("is_blocked")),
                        }
                    )
        return await self.generate_contacts(contacts, db_pool, avatar_base_url=avatar_base_url)

    async def insert_message(self, chatId, senderId, text, attaches, elements, cid, type, db_pool):
        async with db_pool.acquire() as db_connection:
            async with db_connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT id FROM `messages` WHERE chat_id = %s ORDER BY time DESC LIMIT 1",
                    (chatId,),
                )
                row = await cursor.fetchone() or {}
                last_message_id = row.get("id") or 0
                message_time = int(time.time() * 1000)
                message_id = int(time.time() * 1000000) * 1000 + random.randint(100, 999)
                await cursor.execute(
                    "INSERT INTO `messages` (id, chat_id, sender, time, text, attaches, cid, elements, type) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        message_id,
                        chatId,
                        senderId,
                        message_time,
                        text,
                        json.dumps(attaches, default=binary_default),
                        cid,
                        json.dumps(elements),
                        type,
                    ),
                )
        return (int(message_id), int(last_message_id), message_time)

    async def get_last_message(self, chatId, db_pool, protocol_type="mobile", viewer_id=0):
        async with db_pool.acquire() as db_connection:
            async with db_connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT * FROM messages WHERE chat_id = %s AND NOT EXISTS (SELECT 1 FROM message_deletions d WHERE d.message_id = messages.id AND d.user_id IN (0,%s)) ORDER BY time DESC LIMIT 1",
                    (chatId, viewer_id),
                )
                row = await cursor.fetchone()
                if not row:
                    return (None, None)
                return (self.build_message_dict(row, protocol_type), int(row.get("time")))

    async def get_previous_message_id(self, chatId, db_pool, protocol_type="mobile", viewer_id=0):
        async with db_pool.acquire() as db_connection:
            async with db_connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT id FROM messages WHERE chat_id = %s AND NOT EXISTS (SELECT 1 FROM message_deletions d WHERE d.message_id = messages.id AND d.user_id IN (0,%s)) ORDER BY time DESC LIMIT 1 OFFSET 1",
                    (chatId, viewer_id),
                )
                row = await cursor.fetchone()
                if row:
                    return row.get("id") if protocol_type == "mobile" else str(row.get("id"))
                return 0 if protocol_type == "mobile" else "0"

    async def get_participant_last_activity(self, chatId, participant_ids, db_pool):
        if not participant_ids:
            return {}
        async with db_pool.acquire() as db_connection:
            async with db_connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT user_id,mark FROM chat_read_marks WHERE chat_id=%s", (chatId,)
                )
                rows = await cursor.fetchall()
                result = {int(pid): 0 for pid in participant_ids}
                for row in rows:
                    if int(row["user_id"]) in result:
                        result[int(row["user_id"])] = int(row["mark"])
                return result

    async def get_chat_participants(self, chatId, db_pool):
        async with db_pool.acquire() as db_connection:
            async with db_connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT cp.user_id FROM chat_participants cp JOIN chats c ON c.id=cp.chat_id WHERE cp.chat_id=%s AND c.is_deleted=0",
                    (chatId,),
                )
                rows = await cursor.fetchall()
                return [int(row["user_id"]) for row in rows]

    async def auth_required(self, userPhone, coro, *args):
        if userPhone:
            await coro(*args)

    async def update_user_config(self, cursor, phone, user_settings, default_settings):
        user_config = json.loads(user_settings)
        updated_config = {**default_settings, **user_config}
        if updated_config != user_config:
            await cursor.execute(
                "UPDATE user_data SET user_config = %s WHERE phone = %s",
                (json.dumps(updated_config), phone),
            )
        return updated_config

    @staticmethod
    def presence_is_online(client):
        return bool(
            client
            and any(
                (
                    c.get("device_type") == "DESKTOP" or c.get("interactive", True)
                    for c in client.get("clients", [])
                )
            )
        )

    async def collect_presence(self, contact_ids, clients, db_pool):
        now = int(time.time())
        presence = {}
        db_lookup_ids = []
        for contact_id in contact_ids:
            contact_id = int(contact_id)
            client = clients.get(contact_id)
            if self.presence_is_online(client):
                presence[str(contact_id)] = {"seen": now, "on": "ON", "status": 1}
            elif client and client.get("last_seen"):
                presence[str(contact_id)] = {
                    "seen": int(client.get("last_seen")),
                    "on": "OFF",
                    "status": 0,
                }
            else:
                db_lookup_ids.append(contact_id)
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                for contact_id in db_lookup_ids:
                    await cursor.execute("SELECT lastseen FROM users WHERE id = %s", (contact_id,))
                    row = await cursor.fetchone()
                    if row:
                        lastseen = row.get("lastseen")
                        presence[str(contact_id)] = {
                            "seen": int(lastseen),
                            "on": "OFF",
                            "status": 0,
                        }
        return presence

    def get_geo(self, ip, db_path):
        from ownmax.protocols.max.session_metadata import geolocation

        return geolocation(ip, db_path)

    async def generate_user_id(self, db_pool):
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                while True:
                    user_id = secrets.randbelow(2147383647) + 100000
                    await cursor.execute("SELECT id FROM users WHERE id = %s", (user_id,))
                    if not await cursor.fetchone():
                        return user_id

    async def contact_is_blocked(self, owner_id, contact_id, db_pool):
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT * FROM contacts WHERE owner_id = %s AND contact_id = %s AND is_blocked = %s",
                    (owner_id, contact_id, True),
                )
                row = await cursor.fetchone()
                if row:
                    return True
                else:
                    return False
