import asyncio, time
import ownmax.protocols.max.call_service as c
from aiohttp import web, ClientSession, WSMsgType
from aiohttp.test_utils import TestServer


async def main():
    c.keys = lambda: {"signing": "isolated-test-key", "turn": "isolated-turn-key"}
    svc = c.CallService({}, [1, 2])
    svc.turn = lambda uid: {"urls": [], "username": "isolated-test", "credential": "isolated-test"}
    room = {
        "id": "test-room",
        "caller": 1,
        "users": [1, 2],
        "video": False,
        "created": time.time() - 7200,
        "accepted": {1, 2},
        "peers": {},
        "backlog": {1: [], 2: []},
        "media": {},
        "ended": False,
    }
    svc.rooms[room["id"]] = room
    import json, base64, lz4.block

    length, encoded = svc.vcp(room, 1).split(":", 1)
    params = json.loads(
        lz4.block.decompress(base64.b64decode(encoded), uncompressed_size=int(length))
    )
    assert (
        params["srcp"] == "one_me"
        and params["iv"] is False
        and (0 < params["et"] - time.time() <= 600)
    )
    assert svc.room_is_idle(room, time.time())
    app = web.Application()
    app.router.add_get("/ws2", svc.signal)
    async with TestServer(app) as server, ClientSession() as session:

        def url(uid):
            return (
                str(server.make_url("/ws2"))
                + "?conversationId=test-room&userId="
                + str(uid)
                + "&peerId="
                + str(10000 + uid)
                + "&token="
                + c.token(uid, "signal", "test-room")
            )

        async def receive(ws, predicate):
            async with asyncio.timeout(12):
                while True:
                    m = await ws.receive()
                    assert m.type == WSMsgType.TEXT, m.type
                    if m.data == "ping":
                        await ws.send_str("pong")
                        continue
                    import json

                    d = json.loads(m.data)
                    if predicate(d):
                        return d

        async with session.ws_connect(url(1)) as a, session.ws_connect(url(2)) as b:
            ha = await receive(a, lambda d: d.get("notification") == "connection")
            hb = await receive(b, lambda d: d.get("notification") == "connection")
            assert ha["peerId"]["id"] == 10001 and hb["peerId"]["id"] == 10002
            assert not svc.room_is_idle(room, time.time() + 7200), "Connected room expired"
            await b.send_json(
                {
                    "command": "change-participant-state",
                    "sequence": 1,
                    "participantState": {"state": {"hand": "1"}},
                }
            )
            d = await receive(a, lambda d: d.get("notification") == "participant-state-changed")
            assert (
                d["peerId"]["id"] == 10002
                and d["handCount"] == 1
                and (d["participantState"]["state"]["hand"] == "1")
            )
            await b.send_json({"command": "put-hands-down", "sequence": 2, "participantIds": [1]})
            d = await receive(b, lambda d: d.get("sequence") == 2 and d.get("type") == "response")
            assert d.get("error") == "permission-denied"
            await b.send_json(
                {"command": "put-hands-down", "sequence": 3, "participantIds": ["bad"]}
            )
            d = await receive(b, lambda d: d.get("sequence") == 3 and d.get("type") == "response")
            assert d.get("error") == "permission-denied"
            await a.send_json({"command": "put-hands-down", "sequence": 4, "participantIds": [2]})
            d = await receive(
                b,
                lambda d: d.get("notification") == "participant-state-changed"
                and d["participantState"]["state"].get("hand") == "0",
            )
            assert d["handCount"] == 0
            async with session.ws_connect(url(1)) as again:
                d = await receive(
                    again, lambda d: d.get("notification") == "participant-state-changed"
                )
                assert d["participantState"]["state"]["hand"] == "0"
        await asyncio.sleep(0.05)
        assert not svc.room_is_idle(room, time.time()), "Freshly disconnected room expired"
        assert svc.room_is_idle(room, time.time() + 601), "Abandoned room not cleaned"
    print(
        "PASS distinct native transport identities; active call survives >2 hours; idle expiry; hand push/reconnect; moderator permissions; malformed target handling"
    )


asyncio.run(main())
