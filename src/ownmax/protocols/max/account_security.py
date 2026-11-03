import asyncio, hashlib, hmac, json, secrets, time
from collections import defaultdict, deque
from contextvars import ContextVar

TRACKS = {}
FAILURES = defaultdict(deque)
AUTH_GRANTED = ContextVar("ownmax_password_authorization", default=None)
MIGRATION = "CREATE TABLE IF NOT EXISTS account_security (user_id BIGINT PRIMARY KEY,password_hash TEXT NOT NULL,hint TEXT NOT NULL,updated BIGINT NOT NULL)"


def digest(password, salt=None):
    salt = salt or secrets.token_hex(16)
    return (
        salt
        + ":"
        + hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 310000).hex()
    )


def matches(password, value):
    try:
        return hmac.compare_digest(digest(password, value.split(":")[0]), value)
    except (ValueError, AttributeError):
        return False


async def record(p, uid):
    async with p.db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT * FROM account_security WHERE user_id=%s", (uid,))
            return await cur.fetchone()


def track(uid, kind, **extra):
    now = time.time()
    for key in list(TRACKS):
        if TRACKS[key]["expires"] < now:
            TRACKS.pop(key, None)
    if len(TRACKS) >= 256:
        raise ValueError()
    key = secrets.token_urlsafe(32)
    TRACKS[key] = {
        "uid": int(uid),
        "kind": kind,
        "expires": now + 600,
        "verified": False,
        "attempts": 0,
        **extra,
    }
    return key


async def challenge(p, account, payload, seq, writer, context):
    key = hashlib.sha256(payload["token"].encode()).hexdigest()
    if AUTH_GRANTED.get() == (key, int(account["id"])):
        return False
    row = await record(p, account["id"])
    if not row:
        return False
    tid = track(account["id"], "login", auth=dict(payload), context=context, grant=key)
    await p._send(
        writer,
        p.proto.pack_packet(
            cmd=1,
            seq=seq,
            opcode=18,
            payload={
                "tokenAttrs": {},
                "passwordChallenge": {"trackId": tid, "hint": row["hint"] or ""},
            },
        ),
    )
    return True


async def reply(p, op, seq, writer, value=None):
    await p._send(writer, p.proto.pack_packet(cmd=1, seq=seq, opcode=op, payload=value))


async def operation(p, op, payload, seq, writer, uid=None):
    try:
        payload = payload or {}
        if op == 104:
            if not uid:
                raise ValueError()
            row = await record(p, uid)
            await reply(
                p,
                op,
                seq,
                writer,
                {
                    "passwordEnabled": bool(row),
                    "hasPassword": bool(row),
                    "hasEmail": False,
                    "hasHint": bool(row and row["hint"]),
                    "hint": row["hint"] if row else "",
                },
            )
            return
        if op == 112:
            if not uid:
                raise ValueError()
            tid = track(uid, "settings", writer=writer)
            row = await record(p, uid)
            await reply(
                p,
                op,
                seq,
                writer,
                {"trackId": tid, "capabilities": [0, 1, 3, 5], "passwordEnabled": bool(row)},
            )
            return
        tid = payload.get("trackId")
        t = TRACKS.get(tid)
        if not t or t["expires"] < time.time():
            raise ValueError()
        if op != 115 and (
            not uid or t["uid"] != uid or t["kind"] != "settings" or (t["writer"] is not writer)
        ):
            raise ValueError()
        if op == 115 and t["kind"] != "login":
            raise ValueError()
        password = payload.get("password", "")
        if op in (113, 115):
            now = time.time()
            q = FAILURES[t["uid"]]
            while q and q[0] < now - 60:
                q.popleft()
            if len(q) >= 10 or t["attempts"] >= 5:
                raise ValueError()
            t["attempts"] += 1
            row = await record(p, t["uid"])
            if (
                not row
                or not isinstance(password, str)
                or len(password) > 256
                or (not await asyncio.to_thread(matches, password, row["password_hash"]))
            ):
                q.append(now)
                raise ValueError()
            t["verified"] = True
            if op == 115:
                TRACKS.pop(tid, None)

                class Capture:

                    def __init__(self):
                        self.data = []

                    def write(self, v):
                        self.data.append(v)

                    async def drain(self):
                        pass

                cap = Capture()
                cap._session_agent = getattr(writer, "_session_agent", {})
                grant = AUTH_GRANTED.set((t["grant"], t["uid"]))
                try:
                    await p.auth(t["auth"], seq, cap, *t["context"])
                finally:
                    AUTH_GRANTED.reset(grant)
                for raw in cap.data:
                    v = p.proto.unpack_packet(raw)
                    await p._send(
                        writer,
                        p.proto.pack_packet(
                            cmd=v["cmd"], seq=seq, opcode=115, payload=v["payload"]
                        ),
                    )
                return
            await reply(p, op, seq, writer, {"valid": True, "capabilities": [1, 3, 5]})
            return
        if op == 107:
            if not isinstance(password, str) or not 6 <= len(password) <= 256:
                raise ValueError()
            t["pending"] = await asyncio.to_thread(digest, password)
            await reply(p, op, seq, writer, {"valid": True})
            return
        if op == 108:
            hint = payload.get("hint", "")
            if not isinstance(hint, str) or len(hint) > 200:
                raise ValueError()
            t["hint"] = hint
            await reply(p, op, seq, writer, {"valid": True})
            return
        if op == 111:
            row = await record(p, uid)
            if row and (not t["verified"]):
                raise ValueError()
            remove = payload.get("remove2fa") is True
            if not remove:
                if not isinstance(password, str) or not 6 <= len(password) <= 256:
                    raise ValueError()
                value = t.get("pending") or await asyncio.to_thread(digest, password)
                if not await asyncio.to_thread(matches, password, value):
                    raise ValueError()
                hint = payload.get("hint", t.get("hint", "")) or ""
                if len(hint) > 200:
                    raise ValueError()
            async with p.db_pool.acquire() as conn:
                async with conn.cursor() as cur:
                    if remove:
                        await cur.execute("DELETE FROM account_security WHERE user_id=%s", (uid,))
                    else:
                        await cur.execute(
                            "INSERT OR REPLACE INTO account_security (user_id,password_hash,hint,updated) VALUES (%s,%s,%s,%s)",
                            (uid, value, hint, int(time.time() * 1000)),
                        )
                    await cur.execute("SELECT profileoptions FROM users WHERE id=%s", (uid,))
                    u = await cur.fetchone()
                    opts = [v for v in json.loads(u["profileoptions"]) if v not in (2, 3, 4)]
                    if not remove:
                        opts += [2] + ([4] if hint else [])
                    await cur.execute(
                        "UPDATE users SET profileoptions=%s,updatetime=%s WHERE id=%s",
                        (json.dumps(opts), int(time.time() * 1000), uid),
                    )
            TRACKS.pop(tid, None)
            await reply(p, op, seq, writer, {"passwordEnabled": not remove, "profileOptions": opts})
            return
        raise ValueError()
    except (KeyError, TypeError, ValueError):
        await p._send_error(seq, op, p.error_types.INVALID_PAYLOAD, writer)
