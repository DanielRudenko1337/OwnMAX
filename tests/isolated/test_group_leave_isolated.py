import asyncio, time
from ownmax.protocols.max.call_service import CallService


class Socket:

    def __init__(self):
        self.closed = False
        self.events = []

    async def send_json(self, event):
        self.events.append(event)

    async def close(self):
        self.closed = True


class Relay:

    def __init__(self):
        self.removed = []
        self.closed = False

    async def remove(self, uid):
        self.removed.append(uid)

    async def close(self):
        self.closed = True


class Request:
    method = "POST"
    path = "/fb.do"
    query = {}
    match_info = {}
    remote = "127.0.0.1"

    async def post(self):
        return {
            "method": "vchat.hangupConversation",
            "session_key": "isolated",
            "conversationId": "isolated-room",
        }


def room(group=True):
    return {
        "id": "isolated-room",
        "caller": 1,
        "users": [1, 2],
        "video": False,
        "accepted": {1, 2},
        "peers": {1: {Socket()}, 2: {Socket()}},
        "backlog": {1: [], 2: []},
        "media": {},
        "ended": False,
        "created": time.time(),
        "chat_id": 99 if group else 0,
        "sfu": Relay(),
    }


async def main():
    service = CallService({}, [1, 2])
    r = room()
    service.rooms[r["id"]] = r
    service.sessions["isolated"] = {"uid": 1, "exp": time.time() + 60}
    one = next(iter(r["peers"][1]))
    two = next(iter(r["peers"][2]))
    sfu = r["sfu"]
    await service.http(Request())
    assert one.closed and (not two.closed) and (not r["ended"]) and (r["accepted"] == {2})
    assert service.rooms[r["id"]] is r and sfu.removed == [1] and (not sfu.closed)
    assert any(
        (e["notification"] == "participant-removed" and e["participantId"] == 1 for e in two.events)
    )
    assert not any((e["notification"] in ("closed-conversation", "hungup") for e in two.events))
    await service.leave(r, 2, "HUNGUP")
    assert two.closed and r["ended"] and (r["id"] not in service.rooms)
    p = room(False)
    service.rooms[p["id"]] = p
    peers = [next(iter(v)) for v in p["peers"].values()]
    await service.leave(p, 1, "HUNGUP")
    assert p["ended"] and all((s.closed for s in peers))
    print(
        "PASS real HTTP group leave keeps other participant and relay alive; owner can leave; empty group cleanup; personal hangup still ends both; no live account messages"
    )


asyncio.run(main())
