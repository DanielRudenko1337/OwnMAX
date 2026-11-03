from pathlib import Path

"Real processors with isolated in-memory accounts: no live chats or login notices."
import asyncio, json, time
import aiosqlite
from ownmax.infrastructure.sqlite import SQLitePoolCompat
from ownmax.protocols.max.processors import Processors


class Capture:

    def __init__(self):
        self.packets = []

    def write(self, data):
        self.packets.append(data)

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
        uid, bid = (1000001, 1000002)
        now = time.time_ns() // 1000000
        for ident, phone, name, options in [
            (uid, "79990009999", "QA user", []),
            (bid, "01000002", "QA bot", ["BOT", "ONEME"]),
        ]:
            await raw.execute(
                "insert into users(id,phone,firstname,lastname,username,description,profileoptions,options,accountstatus,updatetime,lastseen) values (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ident,
                    phone,
                    name,
                    "",
                    "qa_" + str(ident),
                    "",
                    "[]",
                    json.dumps(options),
                    0,
                    str(now),
                    "0",
                ),
            )
        await raw.execute(
            "insert into bot_registry(bot_id,owner_id,token_hash,created) values (?,?,?,?)",
            (bid, uid, "unused-isolated-test-key", now),
        )
        events = []

        async def event(target, data):
            events.append((target, data))

        p = Processors(db_pool=SQLitePoolCompat(raw), send_event=event)
        w = Capture()
        await p.msg_send(
            {
                "chatId": uid ^ bid,
                "message": {"cid": now, "attaches": [{"_type": "CONTROL", "event": "botStarted"}]},
            },
            1,
            w,
            uid,
            p.db_pool,
        )
        v = p.proto.unpack_packet(w.packets[-1])
        assert v["cmd"] == 1, v["payload"]
        chat = v["payload"]["chat"]
        assert chat["lastEventTime"] > 0 and chat["isSuspended"] is False
        assert any(
            (
                t == uid
                and e.get("eventType") == "chat_updated"
                and (e["chat"]["isSuspended"] is False)
                for t, e in events
            )
        )
        w = Capture()
        await p.msg_send(
            {"chatId": uid ^ bid, "message": {"cid": now + 1, "text": "привет"}},
            2,
            w,
            uid,
            p.db_pool,
        )
        assert p.proto.unpack_packet(w.packets[-1])["cmd"] == 1
        print(
            "PASS first bot START creates real chat and removes suspended state; ACK and live chat snapshot precede text; isolated accounts only"
        )
    finally:
        await raw.close()


asyncio.run(main())
