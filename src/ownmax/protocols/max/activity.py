import asyncio

ACTIVE = {}
TTL = 5.0


async def emit(p, chat, uid, kind, members):
    for other in members:
        if int(other) != int(uid):
            await p.event(
                int(other),
                {"eventType": "typing", "chatId": int(chat), "userId": int(uid), "type": kind},
            )


async def stop(p, chat, uid):
    key = (int(chat), int(uid))
    entry = ACTIVE.pop(key, None)
    if entry:
        task = entry["task"]
        if task is not asyncio.current_task():
            task.cancel()
        await emit(p, chat, uid, None, entry["members"])


async def update(p, chat, uid, kind, members):
    key = (int(chat), int(uid))
    old = ACTIVE.pop(key, None)
    if old:
        old["task"].cancel()
    if kind is None:
        await emit(p, chat, uid, None, members)
        return
    if kind == "TYPING":
        kind = "TEXT"
    entry = {"members": tuple(members), "task": None}

    async def expire():
        try:
            await asyncio.sleep(TTL)
            if ACTIVE.get(key) is entry:
                await stop(p, chat, uid)
        except asyncio.CancelledError:
            pass

    entry["task"] = asyncio.create_task(expire())
    ACTIVE[key] = entry
    await emit(p, chat, uid, kind, members)
