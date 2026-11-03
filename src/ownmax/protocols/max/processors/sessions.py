from ownmax.core.baseprocessor import BaseProcessor


class SessionsProcessors(BaseProcessor):

    async def sessions_info(self, payload, seq, writer, senderPhone, hashedToken):
        from ownmax.protocols.max.session_metadata import store

        for user in self.clients.values():
            if str(user.get("phone")) != str(senderPhone):
                continue
            for client in user.get("clients", []):
                if client.get("writer") is writer and client.get("ip"):
                    await store(self, hashedToken, client["ip"], writer)
                    break
        sessions = []
        async with self.db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT * FROM tokens WHERE phone = %s", (str(senderPhone),))
                user_sessions = await cursor.fetchall()
        for session in sorted(
            user_sessions,
            key=lambda v: (v.get("token_hash") == hashedToken, int(v.get("time") or 0)),
            reverse=True,
        ):
            sessions.append(
                {
                    "time": int(session.get("time")),
                    "client": {
                        "DESKTOP": "MAX для ПК",
                        "IOS": "MAX для iOS",
                        "IPHONE": "MAX для iOS",
                        "ANDROID": "MAX для Android",
                        "WEB": "WEB",
                    }.get(str(session.get("device_type") or "").upper(), "MAX"),
                    "info": session.get("client_info")
                    or session.get("device_name")
                    or "Неизвестное устройство",
                    "location": ", ".join(
                        (
                            v
                            for v in (
                                session.get("location"),
                                (
                                    "IP " + session["ip_address"]
                                    if session.get("ip_address")
                                    else None
                                ),
                                (
                                    "DB-IP.com"
                                    if session.get("ip_address")
                                    and session.get("location")
                                    not in ("Локальная сеть", "Местоположение не определено", None)
                                    else None
                                ),
                            )
                            if v
                        )
                    ),
                    "current": True if session.get("token_hash") == hashedToken else False,
                }
            )
        payload = {"sessions": sessions}
        response = self.proto.pack_packet(
            cmd=self.proto.CMD_OK, seq=seq, opcode=self.opcodes.SESSIONS_INFO, payload=payload
        )
        await self._send(writer, response)

    async def sessions_close(self, payload, seq, writer, senderPhone, hashedToken):
        import secrets, hashlib, inspect

        login = secrets.token_urlsafe(128)
        new_hash = hashlib.sha256(login.encode()).hexdigest()
        async with self.db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT * FROM tokens WHERE phone=%s AND token_hash=%s",
                    (str(senderPhone), hashedToken),
                )
                if not await cur.fetchone():
                    await self._send_error(seq, 97, self.error_types.INVALID_TOKEN, writer)
                    return hashedToken
                await cur.execute(
                    "DELETE FROM tokens WHERE phone=%s AND token_hash<>%s",
                    (str(senderPhone), hashedToken),
                )
                await cur.execute(
                    "UPDATE tokens SET token_hash=%s WHERE phone=%s AND token_hash=%s",
                    (new_hash, str(senderPhone), hashedToken),
                )
                await cur.execute("SELECT id FROM users WHERE phone=%s", (str(senderPhone),))
                uid = int((await cur.fetchone())["id"])
        await self._send(
            writer, self.proto.pack_packet(cmd=1, seq=seq, opcode=97, payload={"token": login})
        )
        for client in list(self.clients.get(uid, {}).get("clients", [])):
            other = client.get("writer")
            if other is writer:
                continue
            try:
                value = other.close()
                if inspect.isawaitable(value):
                    await value
            except (ConnectionError, RuntimeError):
                pass
        return new_hash
