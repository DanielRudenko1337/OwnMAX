import time, json, logging
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

BOT_ID = 2000000011
_rate = {}


def rate_allowed(phone):
    now = time.time()
    for key in list(_rate):
        if not _rate[key] or now - _rate[key][-1] > 3600:
            _rate.pop(key, None)
    if len(_rate) > 10000:
        return False
    recent = _rate.setdefault(phone, deque())
    while recent and recent[0] < now - 3600:
        recent.popleft()
    if len(recent) >= 20 or sum((v > now - 60 for v in recent)) >= 5:
        return False
    recent.append(now)
    return True


async def ensure(p):
    now = int(time.time() * 1000)
    async with p.db_pool.acquire() as conn:
        async with conn.cursor() as c:
            await c.execute("SELECT id,username FROM users WHERE id=%s", (BOT_ID,))
            existing = await c.fetchone()
            if existing:
                if existing["username"] != "securitybot":
                    raise ValueError("Reserved service id collision")
                return
            await c.execute("SELECT id FROM users WHERE username=%s", ("securitybot",))
            if await c.fetchone():
                raise ValueError("Reserved service username collision")
            await c.execute(
                "INSERT INTO users(id,phone,firstname,lastname,username,profileoptions,options,accountstatus,updatetime,lastseen,description) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    BOT_ID,
                    "0",
                    "Безопасность",
                    "",
                    "securitybot",
                    "[]",
                    json.dumps(["BOT", "ONEME", "VERIFIED", "SERVICE_ACCOUNT"]),
                    0,
                    str(now),
                    "0",
                    "Сервисные уведомления вашего частного сервера OwnMAX",
                ),
            )


async def send(p, uid, text, bot_id=BOT_ID, attaches=None):
    try:
        if bot_id == BOT_ID:
            await ensure(p)
        chat = uid ^ bot_id
        now = int(time.time() * 1000)
        async with p.db_pool.acquire() as conn:
            async with conn.cursor() as c:
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
                            now,
                            (
                                json.dumps({"SERVICE_CHAT": True, "NO_INPUT": True})
                                if bot_id == BOT_ID
                                else "{}"
                            ),
                        ),
                    )
                    for member in (uid, bot_id):
                        await c.execute(
                            "INSERT INTO chat_participants(chat_id,user_id) VALUES (%s,%s)",
                            (chat, member),
                        )
        mid, prev, when = await p.tools.insert_message(
            chat, bot_id, text, attaches or [], [], time.time_ns() // 1000000, "USER", p.db_pool
        )
        snap = await p.tools.generate_chats(
            [chat], p.db_pool, uid, include_favourites=False, protocol_type=p.type
        )
        if snap:
            await p.event(uid, {"eventType": "chat_updated", "chat": snap[0]})
        await p.event(
            uid,
            {
                "eventType": "new_msg",
                "chatId": chat,
                "message": {
                    "id": mid,
                    "cid": when,
                    "time": when,
                    "type": "USER",
                    "sender": bot_id,
                    "text": text,
                    "attaches": attaches or [],
                    "elements": [],
                    "reactionInfo": {},
                },
                "prevMessageId": prev,
                "time": when,
            },
        )
    except Exception:
        logging.getLogger("ownmax.security").exception("Failed to store local security notice")


async def new_login(p, uid):
    date = datetime.now(ZoneInfo("Europe/Moscow"))
    months = (
        "января",
        "февраля",
        "марта",
        "апреля",
        "мая",
        "июня",
        "июля",
        "августа",
        "сентября",
        "октября",
        "ноября",
        "декабря",
    )
    text = (
        "Новый вход\n\nОбнаружили вход в профиль с вашим номером телефона "
        + f"{date.day} {months[date.month - 1]} {date.year} {date:%H:%M} МСК"
        + "\n\nЕсли это были не вы, перейдите в настройки → Устройства → Выйти на всех устройствах, кроме этого, чтобы предотвратить нежелательный доступ к вашим данным, перепискам и звонкам"
    )
    await send(p, uid, text, attaches=devices_keyboard())


def devices_keyboard():
    return [
        {
            "_type": "INLINE_KEYBOARD",
            "callbackId": "security-devices",
            "keyboard": {
                "buttons": [
                    [
                        {
                            "type": "LINK",
                            "text": "Выйти на всех устройствах",
                            "url": "https://max.ru/:settings/devices",
                        }
                    ]
                ]
            },
        }
    ]
