import pydantic
import json
import time
from ownmax.core.baseprocessor import BaseProcessor
from ownmax.protocols.tamtam.models import (
    ContactListPayloadModel,
    ContactPresencePayloadModel,
    ContactUpdatePayloadModel,
)


class ContactsProcessors(BaseProcessor):

    async def contact_list(self, payload, seq, writer, userId):
        try:
            ContactListPayloadModel.model_validate(payload)
        except pydantic.ValidationError as error:
            self.logger.error(f"Возникли ошибки при валидации пакета: {error}")
            await self._send_error(
                seq, self.opcodes.CONTACT_LIST, self.error_types.INVALID_PAYLOAD, writer
            )
            return
        status = payload.get("status")
        count = payload.get("count")
        contact_list = []
        if status == "BLOCKED":
            blocked = []
            async with self.db_pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    if count:
                        await cursor.execute(
                            "SELECT * FROM contacts WHERE owner_id = %s AND is_blocked = TRUE LIMIT %s",
                            (userId, count),
                        )
                    else:
                        await cursor.execute(
                            "SELECT * FROM contacts WHERE owner_id = %s AND is_blocked = TRUE",
                            (userId,),
                        )
                    rows = await cursor.fetchall()
                    for row in rows:
                        blocked.append(
                            {
                                "id": int(row.get("contact_id")),
                                "firstname": row.get("custom_firstname"),
                                "lastname": row.get("custom_lastname"),
                                "blocked": True,
                            }
                        )
            contact_list = await self.tools.generate_contacts(
                blocked, self.db_pool, avatar_base_url=self.config.avatar_base_url
            )
        response_payload = {"contacts": contact_list}
        packet = self.proto.pack_packet(
            seq=seq, opcode=self.opcodes.CONTACT_LIST, payload=response_payload
        )
        await self._send(writer, packet)

    async def contact_update(self, payload, seq, writer, userId):
        try:
            ContactUpdatePayloadModel.model_validate(payload)
        except pydantic.ValidationError as error:
            self.logger.error(f"Возникли ошибки при валидации пакета: {error}")
            await self._send_error(
                seq, self.opcodes.CONTACT_UPDATE, self.error_types.INVALID_PAYLOAD, writer
            )
            return
        action = payload.get("action")
        contactId = payload.get("contactId")
        firstName = payload.get("firstName")
        lastName = payload.get("lastName", "")
        if action == "ADD":
            async with self.db_pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute("SELECT * FROM users WHERE id = %s", (contactId,))
                    user = await cursor.fetchone()
                    if not user:
                        await self._send_error(
                            seq,
                            self.opcodes.CONTACT_UPDATE,
                            self.error_types.USER_NOT_FOUND,
                            writer,
                        )
                        return
                    await cursor.execute(
                        "SELECT * FROM contacts WHERE owner_id = %s AND contact_id = %s",
                        (userId, contactId),
                    )
                    row = await cursor.fetchone()
                    if not row:
                        await cursor.execute(
                            "INSERT INTO contacts (owner_id, contact_id, custom_firstname, custom_lastname, is_blocked) VALUES (%s, %s, %s, %s, FALSE)",
                            (userId, contactId, firstName, lastName),
                        )
                    else:
                        await self._send_error(
                            seq,
                            self.opcodes.CONTACT_UPDATE,
                            self.error_types.CONTACT_ALREADY_ADDED,
                            writer,
                        )
                        return
            photoId = None if not user.get("avatar_id") else int(user.get("avatar_id"))
            avatar_url = None if not photoId else self.config.avatar_base_url + str(photoId)
            contact = self.tools.generate_profile(
                id=user.get("id"),
                phone=int(user.get("phone")),
                avatarUrl=avatar_url,
                photoId=photoId,
                updateTime=int(user.get("updatetime")),
                firstName=user.get("firstname"),
                lastName=user.get("lastname"),
                options=json.loads(user.get("options")),
                accountStatus=int(user.get("accountstatus")),
                includeProfileOptions=False,
                custom_firstname=firstName,
                custom_lastname=lastName,
            )
            response_payload = {"contact": contact}
            packet = self.proto.pack_packet(
                cmd=self.proto.CMD_OK,
                seq=seq,
                opcode=self.opcodes.CONTACT_UPDATE,
                payload=response_payload,
            )
            await self._send(writer, packet)
        elif action == "REMOVE":
            async with self.db_pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        "DELETE FROM contacts WHERE owner_id = %s AND contact_id = %s",
                        (userId, contactId),
                    )
            packet = self.proto.pack_packet(
                cmd=self.proto.CMD_OK, seq=seq, opcode=self.opcodes.CONTACT_UPDATE, payload=None
            )
            await self._send(writer, packet)
        elif action == "BLOCK":
            async with self.db_pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        "SELECT * FROM contacts WHERE owner_id = %s AND contact_id = %s",
                        (userId, contactId),
                    )
                    row = await cursor.fetchone()
                    if row:
                        await cursor.execute(
                            "UPDATE contacts SET is_blocked = TRUE WHERE owner_id = %s AND contact_id = %s",
                            (userId, contactId),
                        )
                    else:
                        await cursor.execute("SELECT * FROM users WHERE id = %s", (contactId,))
                        user = await cursor.fetchone()
                        if not user:
                            await self._send_error(
                                seq,
                                self.opcodes.CONTACT_UPDATE,
                                self.error_types.USER_NOT_FOUND,
                                writer,
                            )
                            return
                        await cursor.execute(
                            "INSERT INTO contacts (owner_id, contact_id, custom_firstname, custom_lastname, is_blocked) VALUES (%s, %s, %s, %s, TRUE)",
                            (userId, contactId, firstName, lastName),
                        )
            packet = self.proto.pack_packet(
                cmd=self.proto.CMD_OK, seq=seq, opcode=self.opcodes.CONTACT_UPDATE, payload=None
            )
            await self._send(writer, packet)
        elif action == "UNBLOCK":
            async with self.db_pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        "SELECT * FROM contacts WHERE owner_id = %s AND contact_id = %s",
                        (userId, contactId),
                    )
                    row = await cursor.fetchone()
                    if row:
                        await cursor.execute(
                            "UPDATE contacts SET is_blocked = FALSE WHERE owner_id = %s AND contact_id = %s",
                            (userId, contactId),
                        )
                    else:
                        await self._send_error(
                            seq,
                            self.opcodes.CONTACT_UPDATE,
                            self.error_types.CONTACT_NOT_FOUND,
                            writer,
                        )
                        return
            packet = self.proto.pack_packet(
                cmd=self.proto.CMD_OK, seq=seq, opcode=self.opcodes.CONTACT_UPDATE, payload=None
            )
            await self._send(writer, packet)
        elif action == "UPDATE":
            async with self.db_pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        "SELECT * FROM contacts WHERE owner_id = %s AND contact_id = %s",
                        (userId, contactId),
                    )
                    row = await cursor.fetchone()
                    if not row:
                        await self._send_error(
                            seq,
                            self.opcodes.CONTACT_UPDATE,
                            self.error_types.CONTACT_NOT_FOUND,
                            writer,
                        )
                        return
                    await cursor.execute(
                        "UPDATE contacts SET custom_firstname = %s, custom_lastname = %s WHERE owner_id = %s AND contact_id = %s",
                        (firstName, lastName, userId, contactId),
                    )
                    await cursor.execute("SELECT * FROM users WHERE id = %s", (contactId,))
                    user = await cursor.fetchone()
            photo_id = None if not user.get("avatar_id") else int(user.get("avatar_id"))
            avatar_url = None if not photo_id else self.config.avatar_base_url + str(photo_id)
            contact = (
                self.tools.generate_profile_tt(
                    id=user.get("id"),
                    phone=int(user.get("phone")),
                    avatarUrl=avatar_url,
                    photoId=photo_id,
                    updateTime=int(user.get("updatetime")),
                    firstName=user.get("firstname"),
                    lastName=user.get("lastname"),
                    options=json.loads(user.get("options")),
                    description=user.get("description"),
                    username=user.get("username"),
                ),
            )
            response_payload = {"contact": contact}
            packet = self.proto.pack_packet(
                cmd=self.proto.CMD_OK,
                seq=seq,
                opcode=self.opcodes.CONTACT_UPDATE,
                payload=response_payload,
            )
            await self._send(writer, packet)

    async def contact_presence(self, payload, seq, writer):
        try:
            ContactPresencePayloadModel.model_validate(payload)
        except pydantic.ValidationError as error:
            self.logger.error(f"Возникли ошибки при валидации пакета: {error}")
            await self._send_error(
                seq, self.opcodes.CONTACT_PRESENCE, self.error_types.INVALID_PAYLOAD, writer
            )
            return
        contact_ids = payload.get("contactIds", [])
        now_ms = int(time.time() * 1000)
        presence = await self.tools.collect_presence(contact_ids, self.clients, self.db_pool)
        response_payload = {"presence": presence, "time": now_ms}
        packet = self.proto.pack_packet(
            cmd=self.proto.CMD_OK,
            seq=seq,
            opcode=self.opcodes.CONTACT_PRESENCE,
            payload=response_payload,
        )
        await self._send(writer, packet)
