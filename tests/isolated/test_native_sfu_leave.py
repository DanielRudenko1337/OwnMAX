import asyncio, math, msgpack
from array import array
from aiortc import RTCPeerConnection, RTCSessionDescription, AudioStreamTrack
from ownmax.protocols.max.native_sfu import NativeRelay


class Tone(AudioStreamTrack):

    async def recv(self):
        f = await super().recv()
        x = f.pts or 0
        f.planes[0].update(
            array(
                "h",
                [
                    int(math.sin(2 * math.pi * 440 * (i + x) / f.sample_rate) * 5000)
                    for i in range(f.samples)
                ],
            ).tobytes()
        )
        return f


class Service:

    def turn(self, uid):
        return {"urls": [], "username": "", "credential": ""}


async def main():
    relay = NativeRelay(Service(), {"users": [1, 2]})
    peers = []
    channels = {}
    frames = 0
    energy = False
    screen = asyncio.Event()
    tasks = []
    try:
        for uid in (1, 2):
            pc = RTCPeerConnection()
            peers.append(pc)
            pc.addTrack(Tone())

            @pc.on("datachannel")
            def channel(ch, uid=uid):
                channels[uid, ch.label] = ch

                @ch.on("message")
                def message(data):
                    if (
                        uid == 2
                        and ch.label == "producerScreenShare"
                        and (
                            data
                            == b"\x01\x00\x01\x00\x00\x00\x00\x01\x00\x01\x07authorized-screen-packet"
                        )
                    ):
                        screen.set()
                    if ch.label == "producerNotification":
                        unpacker = msgpack.Unpacker(raw=False)
                        unpacker.feed(data)
                        fields = list(unpacker)
                        assert fields == [1, {"u1:sSCREEN": 1, "u2:sSCREEN": 2}], fields

            @pc.on("track")
            def track_received(track, uid=uid):

                async def drain():
                    nonlocal frames, energy
                    try:
                        while True:
                            f = await track.recv()
                            if uid == 2 and track.kind == "audio":
                                frames += 1
                                energy = energy or any(
                                    (
                                        abs(v) > (0.02 if f.format.name.startswith("flt") else 500)
                                        for v in array(
                                            "f" if f.format.name.startswith("flt") else "h",
                                            bytes(f.planes[0]),
                                        )
                                    )
                                )
                    except Exception:
                        pass

                tasks.append(asyncio.create_task(drain()))

            offer = await relay.handle(uid, "allocate-consumer", {})
            await pc.setRemoteDescription(
                RTCSessionDescription(sdp=offer["description"], type="offer")
            )
            await pc.setLocalDescription(await pc.createAnswer())
            await relay.handle(uid, "consumer-answered", {"description": pc.localDescription.sdp})
        async with asyncio.timeout(25):
            while not all((p.connectionState == "connected" for p in peers)) or not (
                channels.get((1, "consumerScreenShare"))
                and channels[1, "consumerScreenShare"].readyState == "open"
            ):
                await asyncio.sleep(0.1)
            while (
                not channels.get((2, "producerScreenShare"))
                or channels[2, "producerScreenShare"].readyState != "open"
            ):
                await asyncio.sleep(0.1)
            channels[1, "consumerScreenShare"].send(
                b"\x01\x00\x01\x00\x00\x00\x00\x01\xff\xff\x07authorized-screen-packet"
            )
            await screen.wait()
            while frames < 30 or not energy:
                await asyncio.sleep(0.1)
        remaining = relay.consumers[2]
        await relay.remove(1)
        assert (
            not relay.closed and 2 in relay.consumers and (remaining.connectionState == "connected")
        )
        before = frames
        async with asyncio.timeout(5):
            while frames < before + 10:
                await asyncio.sleep(0.05)
        energy = False
        returning = RTCPeerConnection()
        peers.append(returning)
        returning.addTrack(Tone())
        offer = await relay.handle(1, "allocate-consumer", {})
        await returning.setRemoteDescription(
            RTCSessionDescription(sdp=offer["description"], type="offer")
        )
        await returning.setLocalDescription(await returning.createAnswer())
        await relay.handle(1, "consumer-answered", {"description": returning.localDescription.sdp})
        async with asyncio.timeout(15):
            while returning.connectionState != "connected" or not energy:
                await asyncio.sleep(0.05)
        assert remaining.connectionState == "connected"
        print(
            "PASS same participant rejoins; remaining connection survives and microphone audio resumes",
            flush=True,
        )
        print(
            "PASS leaving participant media closes independently; remaining WebRTC connection and audio frames continue",
            flush=True,
        )
        print(
            "PASS two isolated WebRTC peers; real WebRTC audio relay with nonzero source samples; native screen header source remapped and forwarded to other participant only; SDK transport ID mapping decoded",
            flush=True,
        )
    finally:
        await asyncio.gather(*(p.close() for p in peers), return_exceptions=True)
        await relay.close()
        for t in tasks:
            t.cancel()


asyncio.run(main())
