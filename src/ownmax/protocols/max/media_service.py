from ownmax.settings import DATA_DIR, MEDIA_URL
import asyncio, base64, json, logging, secrets, time
from pathlib import Path
from urllib.parse import urlencode, quote
import av
from aiohttp import web
from ownmax.protocols.max.call_service import token, verify

log = logging.getLogger("ownmax.media")
ROOT = DATA_DIR / "media"
BASE = MEDIA_URL
LIMIT = 32 * 1024 * 1024
QUOTA = 256 * 1024 * 1024


def binary_default(v):
    if isinstance(v, bytes):
        return {"__ownmax_binary__": base64.b64encode(v).decode()}
    raise TypeError(type(v).__name__)


def binary_hook(v):
    if len(v) == 1:
        key = next(iter(v))
        if key in ("__ownmax_binary__", "__openmax_binary__"):
            return base64.b64decode(v[key])
    return v


def load(mid):
    if not str(mid).isdigit():
        raise web.HTTPNotFound()
    try:
        return json.loads((ROOT / (str(mid) + ".json")).read_text())
    except FileNotFoundError:
        raise web.HTTPNotFound()


def save(m):
    ROOT.mkdir(parents=True, exist_ok=True)
    p = ROOT / (m["id"] + ".json")
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(m))
    tmp.replace(p)


def download(m):
    return (
        BASE
        + "/media/"
        + m["id"]
        + "/"
        + quote(token(m["owner"], "download", m["id"], 7 * 86400), safe="")
        + ("?v=" + str(m["format_revision"]) if m.get("format_revision") else "")
    )


def avatar_download(m):
    expiry = int(m.get("avatar_cap_expiry") or 0)
    if expiry < int(time.time()) + 86400:
        expiry = int(time.time()) + 30 * 86400
        m["avatar_cap_expiry"] = expiry
        save(m)
    import hashlib, hmac
    from ownmax.protocols.max.call_service import keys

    raw = json.dumps(
        {"uid": int(m["owner"]), "kind": "download", "room": m["id"], "exp": expiry},
        separators=(",", ":"),
    ).encode()
    data = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    sig = hmac.new(keys()["signing"].encode(), data.encode(), hashlib.sha256).hexdigest()
    return BASE + "/media/" + m["id"] + "/" + quote("$" + data + "." + sig, safe="") + "?avatar=1"


def attachment(m, original=None):
    a = dict(original or {})
    kind = m["kind"]
    mid = int(m["id"])
    url = download(m)
    a.update(_type=kind, token=token(m["owner"], "attach", m["id"], 7 * 86400))
    if kind == "PHOTO":
        a.update(
            photoId=mid, baseUrl=url, url=url, width=m.get("width", 1), height=m.get("height", 1)
        )
    elif kind == "AUDIO":
        a.update(audioId=mid, url=url, duration=a.get("duration", m.get("duration", 1000)))
    elif kind == "VIDEO":
        a.update(
            videoType=m.get("videoType", a.get("videoType", 0)),
            videoId=mid,
            url=url,
            duration=a.get("duration", m.get("duration", 1000)),
            width=m.get("width", 480),
            height=m.get("height", 480),
        )
    else:
        a.update(
            fileId=m["id"],
            fileName=m.get("name", "file"),
            name=m.get("name", "file"),
            fileSize=m.get("size", 0),
            size=m.get("size", 0),
            fileUrl=url,
            url=url,
        )
    return a


def allocate(uid, opcode, payload):
    ROOT.mkdir(parents=True, exist_ok=True)
    count = max(1, min(int(payload.get("count", 1)), 8))
    kind = (
        "PHOTO"
        if opcode == 80
        else "FILE" if opcode == 87 else "AUDIO" if payload.get("type") == 2 else "VIDEO"
    )
    items = []
    for _ in range(count):
        mid = str(secrets.randbelow(1 << 46) + (1 << 46))
        m = {
            "id": mid,
            "owner": uid,
            "kind": kind,
            "videoType": 1 if payload.get("type") == 1 else 0,
            "created": time.time(),
            "ready": False,
            "name": payload.get("name", ""),
        }
        save(m)
        items.append(m)
    first = items[0]
    ids = ",".join((m["id"] for m in items))
    url = (
        BASE
        + "/media/upload/"
        + first["id"]
        + "?"
        + urlencode({"token": token(uid, "upload", first["id"], 3600), "ids": ids})
    )
    if opcode == 80:
        return {"url": url}
    return {
        "info": [
            {
                "url": BASE
                + "/media/upload/"
                + m["id"]
                + "?"
                + urlencode({"token": token(uid, "upload", m["id"], 3600)}),
                "videoId": int(m["id"]),
                "fileId": m["id"],
                "token": token(uid, "attach", m["id"], 7 * 86400),
                "name": m["name"],
            }
            for m in items
        ],
        "uploaderType": payload.get("uploaderType", 1),
    }


def enrich(attaches, uid):
    out = []
    for a in attaches:
        if a.get("_type") not in ("PHOTO", "AUDIO", "VIDEO", "FILE"):
            out.append(a)
            continue
        cap = a.get("token") or a.get("photoToken")
        if not cap:
            raise ValueError("Uploaded media token required")
        try:
            raw = cap.removeprefix("$").rsplit(".", 1)[0]
            body = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
            mid = body["room"]
            owner = verify(cap, "attach", mid)
            m = load(mid)
            if owner != uid or m["owner"] != uid or (not m["ready"]):
                raise ValueError("Media not ready or not owned")
        except Exception as e:
            raise ValueError("Invalid attachment") from e
        out.append(attachment(m, a))
    return out


async def upload(request):
    from ownmax.protocols.max import call_service

    mid = request.match_info["mid"]
    m = load(mid)
    uid = verify(request.query.get("token", ""), "upload", mid)
    if m["owner"] != uid:
        raise web.HTTPForbidden()
    ids = request.query.get("ids", mid).split(",")
    if len(ids) > 8:
        raise web.HTTPBadRequest()
    used = sum((p.stat().st_size for p in ROOT.glob("*.bin")))
    if used >= QUOTA:
        raise web.HTTPServiceUnavailable()
    files = []
    if request.content_type.startswith("multipart/"):
        reader = await request.multipart()
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.filename:
                data = await part.read(decode=False)
                files.append(
                    (
                        part.filename,
                        bytes(data),
                        part.headers.get("Content-Type", "application/octet-stream"),
                    )
                )
    else:
        files = [(m.get("name") or "media", await request.read(), request.content_type)]
    if len(files) != len(ids):
        raise web.HTTPBadRequest(text="File count mismatch")
    results = {}
    for item_id, (name, data, mime) in zip(ids, files):
        mm = load(item_id)
        if mm["owner"] != uid or mm["kind"] != m["kind"]:
            raise web.HTTPForbidden()
        if mm["ready"] and (ROOT / (item_id + ".bin")).read_bytes() != data:
            raise web.HTTPConflict(text="Upload already finalized")
        if not data or len(data) > LIMIT or used + len(data) > QUOTA:
            raise web.HTTPRequestEntityTooLarge(max_size=LIMIT, actual_size=len(data))
        used += len(data)
        path = ROOT / (item_id + ".bin")
        path.write_bytes(data)
        if mm["kind"] == "VIDEO":
            from ownmax.protocols.max.media_compat import normalize

            if await asyncio.to_thread(normalize, path):
                mm["format_revision"] = int(time.time())
                data = path.read_bytes()
        mm.update(name=Path(name).name, size=len(data), mime="application/octet-stream", ready=True)
        try:
            with av.open(str(path)) as c:
                if c.duration:
                    mm["duration"] = int(c.duration / 1000)
                for st in c.streams:
                    if st.type == "video":
                        mm.update(width=st.width, height=st.height)
                        break
        except Exception:
            pass
        if data.startswith(b"\x89PNG"):
            mm["mime"] = "image/png"
        elif data.startswith(b"\xff\xd8"):
            mm["mime"] = "image/jpeg"
        elif data.startswith(b"OggS"):
            mm["mime"] = "audio/ogg"
        elif b"ftyp" in data[:32]:
            mm["mime"] = "video/mp4"
        save(mm)
        a = attachment(mm)
        results[item_id] = {
            "token": a["token"],
            "photoId": int(item_id),
            "width": mm.get("width", 1),
            "height": mm.get("height", 1),
        }
        if call_service.SERVICE is not None:
            await call_service.SERVICE.notify(
                uid,
                136,
                {
                    "attach": a,
                    "videoId": int(item_id),
                    "fileId": int(item_id),
                    "token": a["token"],
                    "type": mm["kind"],
                },
            )
        log.info("Media upload kind=%s bytes=%s", mm["kind"], len(data))
    if m["kind"] == "PHOTO":
        return web.json_response({"photos": results})
    return web.json_response(
        {"token": a["token"], "videoId": int(mid), "fileId": int(mid), "retval": 1}
    )


async def get_media(request):
    mid = request.match_info["mid"]
    m = load(mid)
    import re

    capability = request.match_info.get("token") or request.query.get("token", "")
    capability = re.sub("&fn=sqr_[1-9][0-9]{0,3}$", "", capability)
    uid = verify(capability, "download", mid)
    if uid != m["owner"] or not m["ready"]:
        raise web.HTTPForbidden()
    return web.FileResponse(
        ROOT / (mid + ".bin"),
        headers={
            "Content-Type": m.get("mime", "application/octet-stream"),
            "Cache-Control": "private, max-age=600",
            "X-Content-Type-Options": "nosniff",
        },
    )


def audio_urls(payload):
    mid = str(payload.get("audioId") or "")
    m = load(mid)
    owner = verify(payload.get("token", ""), "attach", mid)
    if owner != m["owner"] or not m["ready"] or m["kind"] != "AUDIO":
        raise ValueError("Invalid audio")
    with av.open(str(ROOT / (mid + ".bin"))) as source:
        codec = source.streams.audio[0].codec_context.name
    key = {"opus": "opus", "aac": "m4a", "mp3": "mp3"}.get(codec)
    if not key:
        raise ValueError("Unsupported audio format")
    return {key: download(m)}


def urls(payload):
    mid = str(payload.get("videoId") or payload.get("audioId") or payload.get("fileId") or "")
    m = load(mid)
    owner = verify(payload.get("token", ""), "attach", mid)
    if owner != m["owner"]:
        raise ValueError("Invalid media owner")
    if not m["ready"]:
        raise ValueError("Media not ready")
    url = download(m)
    return {
        "url": url,
        "EXTERNAL": url,
        "MP4_720": url,
        "MP4_480": url,
        "MP4_360": url,
        "MP4_240": url,
        "AUDIO": url,
        "cache": True,
    }


def routes(app):
    app.router.add_route("POST", "/media/upload/{mid:\\d+}", upload)
    app.router.add_route("PUT", "/media/upload/{mid:\\d+}", upload)
    app.router.add_route("GET", "/media/{mid:\\d+}", get_media)
    app.router.add_route("GET", "/media/{mid:\\d+}/{token}", get_media)
    app.router.add_route("HEAD", "/media/{mid:\\d+}/{token}", get_media)
