import av, os
from pathlib import Path
from fractions import Fraction


def normalize(path):
    path = Path(path)
    with av.open(str(path)) as source:
        video = next((s for s in source.streams if s.type == "video"), None)
        audio = next((s for s in source.streams if s.type == "audio"), None)
        if not video:
            return False
        if video.codec_context.name == "h264" and (not audio or audio.codec_context.name == "aac"):
            return False
        target = path.with_suffix(".compat.tmp")
        try:
            with av.open(
                str(target), "w", format="mp4", options={"movflags": "+faststart"}
            ) as output:
                outv = output.add_stream("libx264", rate=video.average_rate or Fraction(30, 1))
                outv.width = video.width - video.width % 2
                outv.height = video.height - video.height % 2
                outv.pix_fmt = "yuv420p"
                outv.options = {"preset": "veryfast", "crf": "24", "threads": "1"}
                outv.time_base = video.time_base
                outv.codec_context.time_base = video.time_base
                outa = output.add_stream("aac", rate=48000) if audio else None
                if outa:
                    outa.layout = "stereo"
                resampler = (
                    av.AudioResampler(format="fltp", layout="stereo", rate=48000) if audio else None
                )
                for packet in source.demux([s for s in (video, audio) if s]):
                    for frame in packet.decode():
                        if packet.stream.type == "video":
                            frame = frame.reformat(outv.width, outv.height, "yuv420p")
                            for encoded in outv.encode(frame):
                                output.mux(encoded)
                        elif outa:
                            for converted in resampler.resample(frame):
                                for encoded in outa.encode(converted):
                                    output.mux(encoded)
                for encoded in outv.encode(None):
                    output.mux(encoded)
                if outa:
                    for converted in resampler.resample(None):
                        for encoded in outa.encode(converted):
                            output.mux(encoded)
                    for encoded in outa.encode(None):
                        output.mux(encoded)
            if target.stat().st_size > 32 * 1024 * 1024:
                raise ValueError("Normalized video exceeds upload limit")
            with av.open(str(target)) as check:
                assert check.streams.video[0].codec_context.name == "h264"
                assert next(check.decode(video=0), None) is not None
            os.replace(target, path)
            return True
        finally:
            target.unlink(missing_ok=True)
