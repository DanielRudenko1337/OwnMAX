import asyncio
from fractions import Fraction
import av
from aiortc import AudioStreamTrack
from aiortc.codecs.opus import OpusEncoder
from ownmax.protocols.max.native_sfu import StableRelayAudio


class Source(AudioStreamTrack):

    def __init__(self, rate, layout):
        super().__init__()
        self.rate = rate
        self.layout = layout
        self.offset = 0

    async def recv(self):
        frame = av.AudioFrame(format="s16", layout=self.layout, samples=self.rate // 50)
        for plane in frame.planes:
            plane.update(bytes(plane.buffer_size))
        frame.sample_rate = self.rate
        frame.time_base = Fraction(1, self.rate)
        frame.pts = self.offset
        self.offset += frame.samples
        return frame


async def main():
    output = StableRelayAudio()
    encoder = OpusEncoder()
    last = -1
    for source in (
        None,
        Source(48000, "stereo"),
        Source(8000, "mono"),
        Source(48000, "mono"),
        None,
    ):
        output.set_source(source)
        for _ in range(8):
            frame = await output.recv()
            assert (
                frame.format.name == "s16"
                and frame.layout.name == "stereo"
                and (frame.sample_rate == 48000)
                and (frame.pts > last)
            )
            last = frame.pts
            encoder.encode(frame)
    output.stop()
    print(
        "PASS stable Opus encoder across silence, late stereo microphone, mono/rate changes and disconnect"
    )


asyncio.run(main())
