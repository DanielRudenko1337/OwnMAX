import asyncio
from ownmax.protocols.max import activity


class Processor:

    def __init__(self):
        self.events = []

    async def event(self, uid, payload):
        self.events.append((uid, payload.copy()))


async def main():
    p = Processor()
    activity.TTL = 0.15
    await activity.update(p, 10, 1, "TYPING", [1, 2])
    assert p.events[-1][1]["type"] == "TEXT"
    await asyncio.sleep(0.1)
    await activity.update(p, 10, 1, "STICKER", [1, 2])
    await asyncio.sleep(0.08)
    assert p.events[-1][1]["type"] == "STICKER", "Old timer cleared newer activity"
    await activity.update(p, 11, 1, "AUDIO", [1, 3])
    await asyncio.sleep(0.09)
    assert any((uid == 2 and e["chatId"] == 10 and (e["type"] is None) for uid, e in p.events))
    assert not any(
        (e["chatId"] == 11 and e["type"] is None for _, e in p.events)
    ), "Activity leaked between chats"
    await activity.stop(p, 11, 1)
    assert p.events[-1][0] == 3 and p.events[-1][1]["type"] is None
    await activity.update(p, 10, 1, None, [1, 2])
    assert p.events[-1][1]["type"] is None
    assert not activity.ACTIVE
    print(
        "PASS activity refresh does not expire early; independent chat expiry; explicit cancel; message completion clears activity; legacy TEXT normalization"
    )


asyncio.run(main())
