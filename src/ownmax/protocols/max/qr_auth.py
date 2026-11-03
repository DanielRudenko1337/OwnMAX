import secrets, time, hashlib, json
from urllib.parse import urlparse, parse_qs

_tracks = {}


def cleanup():
    now = time.time()
    for key in list(_tracks):
        if _tracks[key]["expires"] <= now:
            _tracks.pop(key, None)


async def operation(p, payload, seq, writer, op, uid, device_type, device_name, app_version, ip):
    try:
        cleanup()
        payload = payload or {}
        if op == 288:
            if len(_tracks) >= 128:
                raise ValueError()
            for key in list(_tracks):
                if _tracks[key]["writer"] is writer:
                    _tracks.pop(key, None)
            track = secrets.token_urlsafe(32)
            approval = secrets.token_urlsafe(32)
            _tracks[track] = {
                "writer": writer,
                "approval": hashlib.sha256(approval.encode()).digest(),
                "expires": time.time() + 300,
                "uid": None,
            }
            result = {
                "trackId": track,
                "pollingInterval": 1000,
                "ttl": 300000,
                "qrLink": "https://max.ru/auth/qr?token=" + approval,
            }
        elif op == 290:
            if not uid:
                raise PermissionError()
            link = urlparse(str(payload.get("qrLink") or payload.get("link") or ""))
            if link.scheme != "https" or link.hostname != "max.ru" or link.path != "/auth/qr":
                raise ValueError()
            approval = parse_qs(link.query).get("token", [""])[0]
            if len(approval) > 128:
                raise ValueError()
            digest = hashlib.sha256(approval.encode()).digest()
            item = next(
                (v for v in _tracks.values() if secrets.compare_digest(v["approval"], digest)), None
            )
            if not item or item["uid"] is not None:
                raise ValueError()
            item["uid"] = int(uid)
            result = {}
        else:
            track = str(payload.get("trackId") or "")
            item = _tracks.get(track)
            if not item or item["writer"] is not writer:
                raise PermissionError()
            if op == 289:
                result = {
                    "status": {
                        "loginAvailable": item["uid"] is not None,
                        "expiresAt": int(item["expires"] * 1000),
                        "ttl": max(0, int((item["expires"] - time.time()) * 1000)),
                    }
                }
            elif op == 291:
                if item["uid"] is None:
                    raise PermissionError()
                _tracks.pop(track, None)
                async with p.db_pool.acquire() as conn:
                    async with conn.cursor() as c:
                        await c.execute("SELECT * FROM users WHERE id=%s", (item["uid"],))
                        user = await c.fetchone()
                        if not user or user["phone"] == "0":
                            raise PermissionError()
                        login = secrets.token_urlsafe(128)
                        hashed = hashlib.sha256(login.encode()).hexdigest()
                        await c.execute(
                            "INSERT INTO tokens(phone,token_hash,device_type,device_name,location,time) VALUES (%s,%s,%s,%s,%s,%s)",
                            (
                                str(user["phone"]),
                                hashed,
                                device_type,
                                device_name,
                                p.tools.get_geo(ip=ip, db_path=p.config.geo_db_path),
                                int(time.time() * 1000),
                            ),
                        )
                photo = int(user["avatar_id"]) if user.get("avatar_id") else None
                profile = p.tools.generate_profile(
                    id=int(user["id"]),
                    phone=int(user["phone"]),
                    firstName=user["firstname"],
                    lastName=user["lastname"],
                    username=user.get("username"),
                    updateTime=int(user["updatetime"]),
                    options=json.loads(user["options"] or "[]"),
                    profileOptions=json.loads(user["profileoptions"] or "[]"),
                    accountStatus=int(user["accountstatus"]),
                    photoId=photo,
                    avatarUrl=p.tools.profile_avatar_url(photo, p.config.avatar_base_url),
                    description=user.get("description"),
                )
                from ownmax.protocols.max.session_metadata import store

                await store(p, hashed, ip, writer, device_type, device_name, app_version)
                from ownmax.protocols.max.security_bot import new_login

                await new_login(p, int(user["id"]))
                result = {
                    "tokenAttrs": {"LOGIN": {"token": login}},
                    "profile": profile,
                    "presetAvatars": [],
                }
            else:
                raise ValueError()
        await p._send(writer, p.proto.pack_packet(cmd=1, seq=seq, opcode=op, payload=result))
    except PermissionError:
        await p._send_error(seq, op, p.error_types.INVALID_TOKEN, writer)
    except (ValueError, TypeError, KeyError):
        await p._send_error(seq, op, p.error_types.INVALID_PAYLOAD, writer)
