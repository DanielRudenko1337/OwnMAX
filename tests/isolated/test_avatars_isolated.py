import asyncio, json, time, tempfile
from pathlib import Path
from unittest.mock import patch, AsyncMock
import aiosqlite
from ownmax.infrastructure.sqlite import SQLitePoolCompat
from ownmax.protocols.max.processors import Processors
from ownmax.protocols.max import media_service as media, bot_manager


class Capture:

    def __init__(self):
        self.packets = []

    def write(self, v):
        self.packets.append(v)

    async def drain(self):
        pass


async def main():
    schema = (
        (Path(__file__).resolve().parents[2] / "src/ownmax/storage/schema.sqlite.sql")
        .read_text()
        .split(";")
    )
    raw = await aiosqlite.connect(":memory:", isolation_level=None)
    raw.row_factory = aiosqlite.Row
    try:
        for sql in schema:
            await raw.execute(sql)
        uid, other, bid = (1000001, 1000002, 1000003)
        now = time.time_ns() // 1000000
        for ident, name, opts in [
            (uid, "Owner", []),
            (other, "Other", []),
            (bid, "Bot", ["BOT", "ONEME"]),
        ]:
            await raw.execute(
                "insert into users(id,phone,firstname,lastname,username,description,profileoptions,options,accountstatus,updatetime,lastseen) values (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ident,
                    str(ident),
                    name,
                    "",
                    "qa_avatar_bot" if ident == bid else "qa_" + str(ident),
                    "",
                    "[]",
                    json.dumps(opts),
                    0,
                    str(now),
                    "0",
                ),
            )
        await raw.execute(
            "insert into bot_registry(bot_id,owner_id,token_hash,created) values (?,?,?,?)",
            (bid, uid, "unused", now),
        )
        events = []

        async def event(target, data):
            events.append((target, data))

        p = Processors(db_pool=SQLitePoolCompat(raw), send_event=event)
        p.clients = {uid: {}, other: {}}
        with (
            tempfile.TemporaryDirectory(prefix="ownmax-avatar-qa-") as tmp,
            patch.object(media, "ROOT", Path(tmp)),
        ):
            mid = "100000000099"
            m = {"id": mid, "owner": uid, "kind": "PHOTO", "ready": True, "mime": "image/png"}
            media.save(m)
            cap = media.attachment(m)["token"]

            async def profile(actor, payload, ok=True):
                w = Capture()
                await p.profile(payload, 1, w, actor)
                v = p.proto.unpack_packet(w.packets[-1])
                assert (v["cmd"] == 1) == ok
                return v["payload"]

            await profile(other, {"photoToken": cap}, False)
            r = await profile(uid, {"photoToken": cap})
            assert r["profile"]["contact"]["photoId"] == int(mid)
            updates = [
                e["contact"]
                for target, e in events
                if target == other and e.get("eventType") == "contact_updated"
            ]
            assert updates[-1]["photoId"] == int(mid)
            a = (await p.tools.generate_contacts([{"id": uid}], p.db_pool))[0]
            b = (await p.tools.generate_contacts([{"id": uid}], p.db_pool))[0]
            assert a["baseUrl"] == b["baseUrl"] and a["photoId"] == int(mid)
            await profile(uid, {"photoToken": ""})
            assert (
                events[-1][1]["contact"]["photoId"] == 0
                and events[-1][1]["contact"]["baseUrl"] == ""
            )
            print(
                "PASS user avatar ownership, persistence, other-user live contact update, stable URL and clear event"
            )
            with patch.object(bot_manager, "reply", new=AsyncMock()):
                await bot_manager.handle_incoming(
                    p, other, [other, bot_manager.MANAGER], "/setavatar qa_avatar_bot", []
                )
                assert not await (
                    await raw.execute("select * from bot_wizard where user_id=?", (other,))
                ).fetchone()
                await bot_manager.handle_incoming(
                    p, uid, [uid, bot_manager.MANAGER], "/setavatar qa_avatar_bot", []
                )
                await bot_manager.handle_incoming(
                    p,
                    uid,
                    [uid, bot_manager.MANAGER],
                    "",
                    [{"_type": "PHOTO", "photoId": int(mid)}],
                )
                row = await (
                    await raw.execute("select avatar_id from users where id=?", (bid,))
                ).fetchone()
                assert int(row[0]) == int(mid)
                assert any(
                    (
                        target == other
                        and e.get("contact", {}).get("id") == bid
                        and (e["contact"].get("photoId") == int(mid))
                        for target, e in events
                    )
                )
                await bot_manager.handle_incoming(
                    p, uid, [uid, bot_manager.MANAGER], "/setavatar qa_avatar_bot", []
                )
                await bot_manager.handle_incoming(p, uid, [uid, bot_manager.MANAGER], "/remove", [])
                row = await (
                    await raw.execute("select avatar_id from users where id=?", (bid,))
                ).fetchone()
                assert row[0] is None
                print("PASS bot owner-only avatar wizard, other-user update and removal")
    finally:
        await raw.close()


asyncio.run(main())
