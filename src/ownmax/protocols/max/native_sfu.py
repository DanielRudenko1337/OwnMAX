import asyncio, logging, time, struct, msgpack, av
from fractions import Fraction
from collections import deque
from aiortc.mediastreams import MediaStreamError
from aiortc import (
    RTCPeerConnection,
    RTCConfiguration,
    RTCBundlePolicy,
    RTCIceServer,
    RTCSessionDescription,
    AudioStreamTrack,
    VideoStreamTrack,
)
from aiortc.contrib.media import MediaRelay
from aiortc.sdp import candidate_from_sdp

log = logging.getLogger("ownmax.sfu")
logging.getLogger("aiortc.rtcdtlstransport").setLevel(logging.DEBUG)
COMMANDS = {
    "allocate-consumer",
    "consumer-answered",
    "accept-producer",
    "producer-updated",
    "change-streams-priorities",
}


class StableRelayAudio(AudioStreamTrack):

    def __init__(self, source=None):
        super().__init__()
        self.source = source
        self.pending = deque()
        self.resampler = None
        self.input_format = None
        self.samples_sent = 0
        self.started = None
        self.source_task = None

    def set_source(self, source):
        if self.source_task and (not self.source_task.done()):
            self.source_task.cancel()
        self.source_task = None
        self.source = source
        self.pending.clear()
        self.resampler = None
        self.input_format = None

    async def recv(self):
        if self.readyState != "live":
            raise MediaStreamError
        if self.started is None:
            self.started = time.monotonic()
        while self.source is not None and (not self.pending):
            source = self.source
            task = asyncio.create_task(source.recv())
            self.source_task = task
            try:
                frame = await task
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
                continue
            except MediaStreamError:
                if source is self.source:
                    self.set_source(None)
                break
            finally:
                if self.source_task is task:
                    self.source_task = None
            if source is not self.source:
                continue
            signature = (frame.format.name, frame.layout.name, frame.sample_rate)
            if signature != self.input_format:
                self.resampler = av.AudioResampler(
                    format="s16", layout="stereo", rate=48000, frame_size=960
                )
                self.input_format = signature
            self.pending.extend(self.resampler.resample(frame))
        if self.pending:
            raw = self.pending.popleft()
            frame = av.AudioFrame(format="s16", layout="stereo", samples=raw.samples)
            frame.planes[0].update(bytes(raw.planes[0])[: raw.samples * 4])
        else:
            await asyncio.sleep(max(0, self.started + self.samples_sent / 48000 - time.monotonic()))
            frame = av.AudioFrame(format="s16", layout="stereo", samples=960)
            frame.planes[0].update(bytes(frame.samples * 4))
        frame.sample_rate = 48000
        frame.time_base = Fraction(1, 48000)
        frame.pts = self.samples_sent
        self.samples_sent += frame.samples
        return frame


class NativeRelay:

    def __init__(self, service, room):
        self.service, self.room = (service, room)
        self.consumers = {}
        self.producers = {}
        self.channels = {}
        self.tracks = {}
        self.senders = {}
        self.audio_outputs = {}
        self.relay = MediaRelay()
        self.closed = False
        self.forwarded = 0
        self.pending = {}
        self.ice_pending = {}
        self.screen_cache = {}
        self.screen_cache_bytes = {}

    def connection(self, uid, role):
        t = self.service.turn(uid)
        pc = RTCPeerConnection(
            RTCConfiguration(
                bundlePolicy=RTCBundlePolicy.MAX_BUNDLE,
                iceServers=[
                    RTCIceServer(urls=t["urls"], username=t["username"], credential=t["credential"])
                ],
            )
        )

        @pc.on("connectionstatechange")
        async def changed():
            log.info("Native relay uid=%s role=%s state=%s", uid, role, pc.connectionState)
            if (
                not self.closed
                and role == "consumer"
                and (pc.connectionState in ("connected", "closed", "failed"))
            ):
                connected = pc.connectionState == "connected"
                event = lambda user, on: {
                    "stamp": time.time_ns(),
                    "type": "notification",
                    "notification": "session-state",
                    "participantId": user,
                    "participantType": "USER",
                    "deviceIdx": 0,
                    "connected": on,
                    "markers": [],
                }
                if hasattr(self.service, "broadcast"):
                    await self.service.broadcast(self.room, event(uid, connected))
                if connected:
                    self.publish_mappings()
                    for other, peer in self.consumers.items():
                        if (
                            other != uid
                            and peer.connectionState == "connected"
                            and hasattr(self.service, "broadcast")
                        ):
                            await self.service.broadcast(self.room, event(other, True), target=uid)

        @pc.on("track")
        def track_received(track):
            self.tracks[uid, track.kind] = track
            log.info("Native relay input uid=%s kind=%s", uid, track.kind)
            for (other, kind), sender in list(self.senders.items()):
                if other != uid and kind == track.kind:
                    source = self.relay.subscribe(track, buffered=False)
                    if kind == "audio" and other in self.audio_outputs:
                        self.audio_outputs[other].set_source(source)
                    else:
                        sender.replaceTrack(source)

        @pc.on("datachannel")
        def channel_received(channel):
            self.bind_channel(uid, role, channel)

        return pc

    def replay_screens(self, uid):
        out = self.channels.get((uid, "consumer", "producerScreenShare"))
        if out and out.readyState == "open":
            for source, packets in self.screen_cache.items():
                if source != uid:
                    for packet in packets:
                        if out.bufferedAmount < 2097152:
                            out.send(packet)

    def publish_mappings(self):
        packet = msgpack.packb(1) + msgpack.packb(
            {"u" + str(user) + ":sSCREEN": i + 1 for i, user in enumerate(self.room["users"])},
            use_bin_type=True,
        )
        for (user, role, label), channel in list(self.channels.items()):
            if label == "producerNotification" and channel.readyState == "open":
                channel.send(packet)

    def bind_channel(self, uid, role, channel):
        key = (uid, role, channel.label)
        self.channels[key] = channel

        @channel.on("open")
        def opened():
            log.info("Native relay channel uid=%s role=%s label=%s open", uid, role, channel.label)
            if channel.label == "producerNotification":
                self.publish_mappings()
                asyncio.get_running_loop().call_later(0.15, self.replay_screens, uid)
                log.info("Native participant media mapping sent uid=%s", uid)

        @channel.on("message")
        def message(data):
            if not isinstance(data, (bytes, str)) or len(data) > 1048576:
                return
            target_label = {
                "consumerScreenShare": "producerScreenShare",
                "producerScreenShare": "consumerScreenShare",
            }.get(channel.label)
            if (
                channel.label == "consumerScreenShare"
                and isinstance(data, bytes)
                and (len(data) >= 11)
                and (data[0] == 1)
            ):
                compact = self.room["users"].index(uid) + 1
                data = data[:8] + struct.pack(">H", compact) + data[10:]
                flags = data[10]
                if flags & 4 and flags & 1:
                    self.screen_cache[uid] = []
                    self.screen_cache_bytes[uid] = 0
                if uid in self.screen_cache:
                    size = self.screen_cache_bytes.get(uid, 0) + len(data)
                    if size <= 2097152:
                        self.screen_cache[uid].append(data)
                        self.screen_cache_bytes[uid] = size
                    else:
                        self.screen_cache.pop(uid, None)
                        self.screen_cache_bytes.pop(uid, None)
            if target_label:
                for (other, other_role, label), out in list(self.channels.items()):
                    if (
                        other != uid
                        and label == target_label
                        and (other_role == "consumer")
                        and (out.readyState == "open")
                        and (out.bufferedAmount < 2097152)
                    ):
                        packet = data
                        if (
                            channel.label == "consumerScreenShare"
                            and isinstance(data, bytes)
                            and (len(data) >= 11)
                            and (data[0] == 1)
                        ):
                            compact = self.room["users"].index(uid) + 1
                            packet = data[:8] + struct.pack(">H", compact) + data[10:]
                        out.send(packet)
                        self.forwarded += 1
                        if self.forwarded in (1, 30, 300):
                            log.info(
                                "Native screen packets forwarded count=%s bytes=%s",
                                self.forwarded,
                                len(data),
                            )
            elif channel.label in ("producerCommand", "consumerCommand"):
                log.debug("Native relay command channel uid=%s bytes=%s", uid, len(data))

    def queue_sdp(self, uid, role, description):
        packet = {"stamp": time.time_ns(), "type": "notification", "description": description.sdp}
        if role == "consumer" and description.type == "offer":
            packet.update(notification="producer-updated", sessionId=str(uid))
        elif description.type == "answer":
            packet.update(notification="consumer-answered")
        else:
            raise ValueError("Unsupported native media description direction")
        self.pending.setdefault(uid, []).append(packet)

    async def flush(self, uid, ws):
        for packet in self.pending.pop(uid, []):
            if not ws.closed:
                await ws.send_json(packet)

    async def transport_signal(self, uid, data):
        peer = data.get("peerId") or {}
        role = str(data.get("participantType") or peer.get("type", "")).lower()
        if role not in ("producer", "consumer"):
            raise ValueError("Unknown media peer role")
        body = data.get("data") or {}
        description = body.get("sdp")
        pc = (self.producers if role == "producer" else self.consumers).get(uid)
        if description:
            if (
                not isinstance(description, dict)
                or description.get("type") not in ("offer", "answer")
                or (not isinstance(description.get("sdp"), str))
            ):
                raise ValueError("Invalid SDP packet")
            if not pc:
                if role != "producer":
                    raise ValueError("Consumer not allocated")
                pc = self.connection(uid, "producer")
                self.producers[uid] = pc
                for label in (
                    "consumerScreenShare",
                    "producerScreenShare",
                    "producerCommand",
                    "producerNotification",
                ):
                    self.bind_channel(uid, "producer", pc.createDataChannel(label))
            await pc.setRemoteDescription(
                RTCSessionDescription(sdp=description["sdp"], type=description["type"])
            )
            for candidate in self.ice_pending.pop((uid, role), []):
                await pc.addIceCandidate(candidate)
            if description["type"] == "offer":
                await pc.setLocalDescription(await pc.createAnswer())
                self.queue_sdp(uid, role, pc.localDescription)
            log.info(
                "Native relay SDP applied uid=%s role=%s type=%s", uid, role, description["type"]
            )
        candidate = body.get("candidate")
        if candidate and candidate.get("candidate"):
            value = candidate_from_sdp(candidate["candidate"].removeprefix("candidate:"))
            value.sdpMid = candidate.get("sdpMid")
            value.sdpMLineIndex = candidate.get("sdpMLineIndex")
            if pc and pc.remoteDescription:
                await pc.addIceCandidate(value)
            else:
                queue = self.ice_pending.setdefault((uid, role), [])
                if len(queue) < 128:
                    queue.append(value)
        return {}

    def capabilities(self):
        return {
            "audioMix": False,
            "transparentAudio": True,
            "unifiedPlan": True,
            "onDemandTracks": False,
            "singleSession": True,
            "csrcAccessible": True,
            "videoTracksCount": 1,
            "producerNotificationDataChannelVersion": 7,
            "producerCommandDataChannelVersion": 2,
            "producerScreenDataChannelVersion": 1,
            "consumerScreenDataChannelVersion": 1,
            "asrDataChannelVersion": 0,
            "red": False,
            "fastScreenShare": True,
            "audioShare": False,
        }

    @staticmethod
    def description(data):
        value = data.get("description") or data.get("sdp")
        if isinstance(value, dict):
            value = value.get("sdp") or value.get("description")
        if not isinstance(value, str) or not value.startswith("v=0") or len(value) > 262144:
            raise ValueError("Missing SDP description")
        return value

    async def allocate(self, uid):
        if uid in self.consumers:
            await self.consumers.pop(uid).close()
        pc = self.connection(uid, "consumer")
        self.consumers[uid] = pc
        other = next((u for u in self.room["users"] if u != uid))
        for kind, base in [("audio", AudioStreamTrack), ("video", VideoStreamTrack)]:
            source = self.tracks.get((other, kind))
            subscribed = self.relay.subscribe(source, buffered=False) if source else None
            if kind == "audio":
                track = StableRelayAudio(subscribed)
                self.audio_outputs[uid] = track
            else:
                track = subscribed if subscribed else base()
            track._id = kind + "-u" + str(other)
            sender = pc.addTrack(track)
            self.senders[uid, kind] = sender
        for label in (
            "producerScreenShare",
            "consumerScreenShare",
            "producerCommand",
            "producerNotification",
        ):
            self.bind_channel(uid, "consumer", pc.createDataChannel(label))
        await pc.setLocalDescription(await pc.createOffer())
        offer = pc.localDescription
        self.queue_sdp(uid, "consumer", offer)
        log.info("Allocated native relay consumer uid=%s description_bytes=%s", uid, len(offer.sdp))
        return {
            "description": offer.sdp,
            "sdp": {"type": offer.type, "sdp": offer.sdp},
            "capabilities": self.capabilities(),
            "sessionId": str(uid),
            "consumerId": uid,
            "id": uid,
            "version": 1,
        }

    async def handle(self, uid, command, data):
        if self.closed or uid not in self.room["users"]:
            raise ValueError("Relay unavailable")
        if command == "allocate-consumer":
            if data.get("description"):
                return await self.handle(uid, "accept-producer", data)
            return await self.allocate(uid)
        if command == "change-streams-priorities":
            return {}
        if command == "transmit-data":
            return await self.transport_signal(uid, data)
        if not (data.get("description") or data.get("sdp")):
            return {}
        description = self.description(data)
        consumer = self.consumers.get(uid)
        if command == "consumer-answered" or (
            consumer
            and consumer.signalingState == "have-local-offer"
            and ("a=setup:actpass" not in description)
        ):
            if not consumer:
                raise ValueError("Consumer not allocated")
            await consumer.setRemoteDescription(
                RTCSessionDescription(sdp=description, type="answer")
            )
            log.info("Native relay consumer answer applied uid=%s", uid)
            return {"sessionId": str(uid), "description": consumer.localDescription.sdp}
        if command in ("accept-producer", "producer-updated"):
            pc = self.producers.get(uid)
            if not pc:
                pc = self.connection(uid, "producer")
                self.producers[uid] = pc
                for label in ("consumerScreenShare", "producerScreenShare"):
                    self.bind_channel(uid, "producer", pc.createDataChannel(label))
            await pc.setRemoteDescription(RTCSessionDescription(sdp=description, type="offer"))
            await pc.setLocalDescription(await pc.createAnswer())
            log.info("Native relay producer answer created uid=%s", uid)
            self.queue_sdp(uid, "producer", pc.localDescription)
            return {
                "description": pc.localDescription.sdp,
                "sdp": {"type": "answer", "sdp": pc.localDescription.sdp},
                "sessionId": str(uid),
                "capabilities": self.capabilities(),
            }
        raise ValueError("Unsupported relay command")

    async def remove(self, uid):
        peers = [pool.pop(uid, None) for pool in (self.consumers, self.producers)]
        own_audio = self.audio_outputs.pop(uid, None)
        if own_audio:
            own_audio.stop()
        for other, output in self.audio_outputs.items():
            previous = output.source
            output.set_source(None)
            if previous:
                previous.stop()
        for key, sender in list(self.senders.items()):
            if key[0] == uid:
                self.senders.pop(key, None)
            elif key[1] == "video":
                previous = sender.track
                sender.replaceTrack(VideoStreamTrack())
                if previous:
                    previous.stop()
        for key, track in list(self.tracks.items()):
            if key[0] == uid:
                track.stop()
                self.tracks.pop(key, None)
        for key, ch in list(self.channels.items()):
            if key[0] == uid:
                ch.close()
                self.channels.pop(key, None)
        for key in list(self.ice_pending):
            if key[0] == uid:
                self.ice_pending.pop(key, None)
        self.pending.pop(uid, None)
        self.screen_cache.pop(uid, None)
        self.screen_cache_bytes.pop(uid, None)
        await asyncio.gather(*(pc.close() for pc in peers if pc), return_exceptions=True)
        log.info("Native relay released participant uid=%s; other transports preserved", uid)

    async def close(self):
        self.closed = True
        await asyncio.gather(
            *(pc.close() for pc in set(self.consumers.values()) | set(self.producers.values())),
            return_exceptions=True,
        )
        self.consumers.clear()
        self.producers.clear()
        self.channels.clear()
        self.senders.clear()
