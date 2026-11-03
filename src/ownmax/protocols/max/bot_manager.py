from ownmax.settings import MEDIA_URL
import secrets, hashlib, json, re, time, logging

MANAGER = 2000000012
COMMANDS = [
    ("setavatar", "Изменить аватар бота"),
    ("newbot", "Создать бота"),
    ("mybots", "Мои боты"),
    ("token", "Перевыпустить токен"),
    ("setname", "Изменить название"),
    ("setdescription", "Изменить описание"),
    ("deletebot", "Удалить бота"),
    ("cancel", "Отменить создание"),
]


async def ensure(p):
    async with p.db_pool.acquire() as conn:
        async with conn.cursor() as c:
            await c.execute("SELECT username FROM users WHERE id=%s", (MANAGER,))
            row = await c.fetchone()
            if row:
                if row["username"] != "botmanager":
                    raise ValueError("Reserved bot id collision")
                return
            await c.execute(
                "INSERT INTO users(id,phone,firstname,lastname,username,profileoptions,options,accountstatus,updatetime,lastseen,description) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    MANAGER,
                    "02000000012",
                    "Управление ботами",
                    "",
                    "botmanager",
                    "[]",
                    json.dumps(["BOT", "ONEME"]),
                    0,
                    str(int(time.time() * 1000)),
                    "0",
                    "Создание и управление ботами вашего частного сервера без верификации",
                ),
            )


async def reply(p, uid, text):
    from ownmax.protocols.max.security_bot import send

    await send(p, uid, text, bot_id=MANAGER)


async def state(p, uid, step, draft):
    async with p.db_pool.acquire() as conn:
        async with conn.cursor() as c:
            if step is None:
                await c.execute("DELETE FROM bot_wizard WHERE user_id=%s", (uid,))
            else:
                await c.execute(
                    "INSERT INTO bot_wizard(user_id,step,draft) VALUES (%s,%s,%s) ON CONFLICT(user_id) DO UPDATE SET step=excluded.step,draft=excluded.draft",
                    (uid, step, json.dumps(draft)),
                )


async def create(p, uid, draft, avatar=None):
    name = draft["name"].strip()
    username = draft["username"].lower()
    description = draft.get("description", "")
    if (
        not 1 <= len(name) <= 100
        or not re.fullmatch("[a-z][a-z0-9_]{4,63}", username)
        or (not username.endswith("bot"))
        or (len(description) > 1024)
    ):
        raise ValueError("Некорректные данные бота.")
    if username in ("securitybot", "botmanager"):
        raise ValueError("Это имя зарезервировано.")
    login = secrets.token_urlsafe(48)
    digest = hashlib.sha256(login.encode()).hexdigest()
    now = int(time.time() * 1000)
    async with p.db_pool.acquire() as conn:
        async with conn.cursor() as c:
            await c.execute(
                "SELECT COUNT(*) AS n FROM bot_registry WHERE owner_id=%s AND active=1", (uid,)
            )
            if int((await c.fetchone())["n"]) >= 20:
                raise ValueError("Можно создать не более 20 ботов.")
            await c.execute("SELECT id FROM users WHERE LOWER(username)=%s", (username,))
            if await c.fetchone():
                raise ValueError("Это имя пользователя уже занято.")
            await c.execute("SELECT id FROM chats WHERE LOWER(public_slug)=%s", (username,))
            if await c.fetchone():
                raise ValueError("Это имя уже используется каналом.")
            for _ in range(20):
                bid = 1000000000 + secrets.randbelow(900000000)
                await c.execute("SELECT id FROM users WHERE id=%s", (bid,))
                if not await c.fetchone():
                    break
            else:
                raise ValueError("Не удалось выделить идентификатор.")
            await c.execute(
                "INSERT INTO users(id,phone,firstname,lastname,username,description,avatar_id,profileoptions,options,accountstatus,updatetime,lastseen) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    bid,
                    "0" + str(bid),
                    name,
                    "",
                    username,
                    description,
                    avatar,
                    "[]",
                    json.dumps(["BOT", "ONEME"]),
                    0,
                    str(now),
                    "0",
                ),
            )
            await c.execute(
                "INSERT INTO bot_registry(bot_id,owner_id,token_hash,created) VALUES (%s,%s,%s,%s)",
                (bid, uid, digest, now),
            )
    return (bid, login)


async def handle_incoming(p, uid, participants, text, attaches):
    if MANAGER not in participants or uid == MANAGER:
        return
    try:
        await ensure(p)
        async with p.db_pool.acquire() as conn:
            async with conn.cursor() as c:
                await c.execute("SELECT options FROM users WHERE id=%s", (uid,))
                actor = await c.fetchone()
                if not actor or "BOT" in json.loads(actor["options"] or "[]"):
                    return
                await c.execute("SELECT * FROM bot_wizard WHERE user_id=%s", (uid,))
                wizard = await c.fetchone()
        text = str(text or "").strip()
        if any((a.get("_type") == "CONTROL" and a.get("event") == "botStarted" for a in attaches)):
            text = "/start"
        if text == "/cancel":
            await state(p, uid, None, {})
            await reply(p, uid, "Создание отменено.")
            return
        if text in ("/start", "/help"):
            await reply(
                p,
                uid,
                "Создайте бота командой /newbot. Верификация не требуется.\n/mybots — ваши боты\n/token имя_bot — перевыпустить токен\n/setname имя_bot Новое название\n/setavatar имя_bot — изменить аватар\n/setdescription имя_bot Новое описание\n/deletebot имя_bot CONFIRM — удалить\n/cancel — отменить создание",
            )
            return
        if text.startswith("/setavatar "):
            name = text.split(maxsplit=1)[1].removeprefix("@").strip().lower()
            async with p.db_pool.acquire() as conn:
                async with conn.cursor() as c:
                    await c.execute(
                        "SELECT b.bot_id FROM bot_registry b JOIN users u ON u.id=b.bot_id WHERE b.owner_id=%s AND LOWER(u.username)=%s AND b.active=1",
                        (uid, name),
                    )
                    bot = await c.fetchone()
            if not bot:
                raise ValueError("Бот не найден среди ваших ботов.")
            await state(p, uid, "edit_avatar", {"bot_id": int(bot["bot_id"])})
            await reply(
                p,
                uid,
                "Отправьте новое фото для аватара бота. /remove — убрать аватар, /cancel — отменить.",
            )
            return
        if wizard and wizard["step"] == "edit_avatar":
            draft = json.loads(wizard["draft"])
            bid = int(draft["bot_id"])
            photo = next((a for a in attaches if a.get("_type") == "PHOTO"), None)
            if not photo and text != "/remove":
                raise ValueError("Пришлите фото, /remove или /cancel.")
            async with p.db_pool.acquire() as conn:
                async with conn.cursor() as c:
                    await c.execute(
                        "SELECT bot_id FROM bot_registry WHERE bot_id=%s AND owner_id=%s AND active=1",
                        (bid, uid),
                    )
                    if not await c.fetchone():
                        raise ValueError("Нет доступа к боту.")
                    await c.execute(
                        "UPDATE users SET avatar_id=%s,updatetime=%s WHERE id=%s",
                        (
                            int(photo["photoId"]) if photo else None,
                            str(int(time.time() * 1000)),
                            bid,
                        ),
                    )
            contacts = await p.tools.generate_contacts(
                [{"id": bid}], p.db_pool, avatar_base_url=p.config.avatar_base_url
            )
            if not photo:
                contacts[0].update(photoId=0, baseUrl="", baseRawUrl="")
            for target in list(p.clients):
                await p.event(target, {"eventType": "contact_updated", "contact": contacts[0]})
            await state(p, uid, None, {})
            await reply(p, uid, "Аватар бота обновлён.")
            return
        if text == "/newbot":
            await state(p, uid, "name", {})
            await reply(p, uid, "Введите название бота.")
            return
        if wizard and (not text.startswith("/") or text == "/skip"):
            draft = json.loads(wizard["draft"])
            step = wizard["step"]
            if step == "name":
                if not 1 <= len(text) <= 100:
                    raise ValueError("Название должно содержать от 1 до 100 символов.")
                draft["name"] = text
                await state(p, uid, "username", draft)
                await reply(
                    p,
                    uid,
                    "Введите уникальное имя латиницей, заканчивающееся на bot, например family_test_bot.",
                )
                return
            if step == "username":
                name = text.removeprefix("@").lower()
                if not re.fullmatch("[a-z][a-z0-9_]{4,63}", name) or not name.endswith("bot"):
                    raise ValueError(
                        "Нужно имя из 5–64 латинских букв, цифр и подчёркиваний, заканчивающееся на bot."
                    )
                async with p.db_pool.acquire() as conn:
                    async with conn.cursor() as c:
                        await c.execute("SELECT id FROM users WHERE LOWER(username)=%s", (name,))
                        if await c.fetchone():
                            raise ValueError("Это имя занято. Введите другое.")
                draft["username"] = name
                await state(p, uid, "description", draft)
                await reply(p, uid, "Введите описание бота или /skip.")
                return
            if step == "description":
                if len(text) > 1024:
                    raise ValueError("Описание не должно превышать 1024 символа.")
                draft["description"] = "" if text == "/skip" else text
                await state(p, uid, "avatar", draft)
                await reply(p, uid, "Отправьте аватар бота как фото или /skip.")
                return
            if step == "avatar":
                photo = next((a for a in attaches if a.get("_type") == "PHOTO"), None)
                if not photo and text != "/skip":
                    raise ValueError("Отправьте фото или /skip.")
                bid, token = await create(p, uid, draft, int(photo["photoId"]) if photo else None)
                await state(p, uid, None, {})
                await reply(
                    p,
                    uid,
                    "Бот создан: https://max.ru/"
                    + draft["username"]
                    + "\n\nТокен API:\n"
                    + token
                    + "\n\nХраните токен в секрете. API: "
                    + MEDIA_URL
                    + "/botapi/",
                )
                return
        if text == "/mybots":
            async with p.db_pool.acquire() as conn:
                async with conn.cursor() as c:
                    await c.execute(
                        "SELECT u.firstname,u.username FROM bot_registry b JOIN users u ON u.id=b.bot_id WHERE b.owner_id=%s AND b.active=1 ORDER BY b.created",
                        (uid,),
                    )
                    rows = await c.fetchall()
            await reply(
                p,
                uid,
                "\n".join((r["firstname"] + " — https://max.ru/" + r["username"] for r in rows))
                or "У вас пока нет ботов. Создайте первого: /newbot",
            )
            return
        parts = text.split(maxsplit=2)
        if parts and parts[0] in ("/token", "/deletebot", "/setname", "/setdescription"):
            if len(parts) < 2:
                raise ValueError("Укажите имя пользователя бота.")
            name = parts[1].removeprefix("@").lower()
            command = parts[0]
            argument = parts[2] if len(parts) > 2 else ""
            async with p.db_pool.acquire() as conn:
                async with conn.cursor() as c:
                    await c.execute(
                        "SELECT b.bot_id FROM bot_registry b JOIN users u ON u.id=b.bot_id WHERE b.owner_id=%s AND LOWER(u.username)=%s AND b.active=1",
                        (uid, name),
                    )
                    row = await c.fetchone()
                    if not row:
                        raise ValueError("Бот не найден среди ваших ботов.")
                    bid = int(row["bot_id"])
                    if command == "/token":
                        token = secrets.token_urlsafe(48)
                        await c.execute(
                            "UPDATE bot_registry SET token_hash=%s WHERE bot_id=%s",
                            (hashlib.sha256(token.encode()).hexdigest(), bid),
                        )
                        answer = "Новый токен API:\n" + token + "\nПредыдущий токен отозван."
                    elif command == "/deletebot":
                        if argument != "CONFIRM":
                            raise ValueError(
                                "Для удаления отправьте /deletebot " + name + " CONFIRM"
                            )
                        await c.execute("UPDATE bot_registry SET active=0 WHERE bot_id=%s", (bid,))
                        answer = "Бот отключён. Его токен больше не работает."
                    else:
                        if not argument or len(argument) > (100 if command == "/setname" else 1024):
                            raise ValueError(
                                "Укажите новое название или описание допустимой длины."
                            )
                        field = "firstname" if command == "/setname" else "description"
                        await c.execute(
                            "UPDATE users SET " + field + "=%s,updatetime=%s WHERE id=%s",
                            (argument, str(int(time.time() * 1000)), bid),
                        )
                        answer = "Изменения сохранены."
            await reply(p, uid, answer)
            return
        await reply(p, uid, "Используйте /newbot или /mybots. Все команды: /help")
    except ValueError as error:
        await reply(p, uid, str(error))
    except Exception:
        logging.getLogger("ownmax.bots").exception("Bot manager operation failed")
        await reply(p, uid, "Не удалось выполнить действие. Повторите позже.")


async def native_info(p, payload, seq, writer, uid, op):
    try:
        await ensure(p)
        if op == 145:
            bid = int(payload["botId"])
        else:
            members = await p.tools.get_chat_participants(int(payload["chatId"]), p.db_pool)
            if uid not in members:
                raise ValueError()
            bid = MANAGER if MANAGER in members else next((u for u in members if u != uid), 0)
        async with p.db_pool.acquire() as conn:
            async with conn.cursor() as c:
                await c.execute("SELECT options FROM users WHERE id=%s", (bid,))
                row = await c.fetchone()
        if not row or "BOT" not in json.loads(row["options"] or "[]"):
            raise ValueError()
        commands = (
            [{"botId": MANAGER, "name": name, "description": desc} for name, desc in COMMANDS]
            if bid == MANAGER
            else []
        )
        result = {"commands": commands}
        if op == 145:
            contacts = await p.tools.generate_contacts(
                [{"id": bid}], p.db_pool, avatar_base_url=p.config.avatar_base_url
            )
            result["contact"] = contacts[0]
            if bid == MANAGER:
                result["startMessage"] = {
                    "text": "Создавайте и управляйте ботами вашего частного сервера. Нажмите «Старт», чтобы начать."
                }
        await p._send(writer, p.proto.pack_packet(cmd=1, seq=seq, opcode=op, payload=result))
    except (ValueError, TypeError, KeyError):
        await p._send_error(seq, op, p.error_types.INVALID_PAYLOAD, writer)


async def ensure_dialog(p, uid, bid):
    if uid == bid:
        raise ValueError()
    async with p.db_pool.acquire() as conn:
        async with conn.cursor() as c:
            await c.execute("SELECT id,options FROM users WHERE id IN (%s,%s)", (uid, bid))
            users = await c.fetchall()
            if len(users) != 2:
                return False
            bot = next((u for u in users if "BOT" in json.loads(u["options"] or "[]")), None)
            if not bot:
                return False
            actual = int(bot["id"])
            if actual not in (MANAGER, 2000000011):
                await c.execute("SELECT active FROM bot_registry WHERE bot_id=%s", (actual,))
                reg = await c.fetchone()
                if not reg or not reg["active"]:
                    raise ValueError("Bot disabled")
            chat = uid ^ bid
            await c.execute("SELECT id FROM chats WHERE id=%s", (chat,))
            if not await c.fetchone():
                await c.execute(
                    "INSERT INTO chats(id,owner,type,title,description,created,options) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (
                        chat,
                        uid,
                        "DIALOG",
                        "",
                        "",
                        int(time.time() * 1000),
                        (
                            json.dumps({"SERVICE_CHAT": True, "NO_INPUT": True})
                            if actual == 2000000011
                            else "{}"
                        ),
                    ),
                )
                for member in (uid, bid):
                    await c.execute(
                        "INSERT INTO chat_participants(chat_id,user_id) VALUES (%s,%s)",
                        (chat, member),
                    )
    return True


async def suspend(p, payload, seq, writer, uid):
    try:
        bid = int(payload["botId"])
        value = payload.get("value", True)
        if type(value) is not bool or bid == 2000000011:
            raise ValueError()
        async with p.db_pool.acquire() as conn:
            async with conn.cursor() as c:
                await c.execute("SELECT options FROM users WHERE id=%s", (bid,))
                bot = await c.fetchone()
                if not bot or "BOT" not in json.loads(bot["options"] or "[]"):
                    raise ValueError()
                await c.execute(
                    "INSERT INTO bot_states(bot_id,user_id,suspended) VALUES (%s,%s,%s) ON CONFLICT(bot_id,user_id) DO UPDATE SET suspended=excluded.suspended",
                    (bid, uid, int(value)),
                )
        await p._send(writer, p.proto.pack_packet(cmd=1, seq=seq, opcode=119, payload={}))
        chats = await p.tools.generate_chats(
            [uid ^ bid], p.db_pool, uid, include_favourites=False, protocol_type=p.type
        )
        if chats:
            await p.event(uid, {"eventType": "chat_updated", "chat": chats[0]})
    except (ValueError, KeyError, TypeError):
        await p._send_error(seq, 119, p.error_types.INVALID_PAYLOAD, writer)
