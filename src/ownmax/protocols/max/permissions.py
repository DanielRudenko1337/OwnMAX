ALL = 4095


async def mask(cur, chat, uid):
    if int(chat["owner"]) == int(uid):
        return ALL
    await cur.execute(
        "SELECT permissions FROM chat_admins WHERE chat_id=%s AND user_id=%s", (chat["id"], uid)
    )
    row = await cur.fetchone()
    return int(row["permissions"]) if row else 0


def require(value, bits):
    if not value & bits:
        raise PermissionError()
