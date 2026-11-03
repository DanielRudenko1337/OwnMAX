import pydantic
import time
from ownmax.core.baseprocessor import BaseProcessor
from ownmax.protocols.max.models import GetCallTokenPayloadModel, GetCallHistoryPayloadModel


class CallsProcessors(BaseProcessor):

    async def ok_token(self, payload, seq, writer, userId):
        try:
            GetCallTokenPayloadModel.model_validate(payload)
        except pydantic.ValidationError as error:
            self.logger.error(f"Возникли ошибки при валидации пакета: {error}")
            await self._send_error(
                seq, self.opcodes.OK_TOKEN, self.error_types.INVALID_PAYLOAD, writer
            )
            return
        payload = {
            "token": __import__("ownmax.protocols.max.call_service", fromlist=["token"]).token(
                userId
            ),
            "token_lifetime_ts": int((time.time() + 3600) * 1000),
            "token_refresh_ts": int((time.time() + 3300) * 1000),
        }
        packet = self.proto.pack_packet(
            cmd=self.proto.CMD_OK, seq=seq, opcode=self.opcodes.OK_TOKEN, payload=payload
        )
        await self._send(writer, packet)

    async def video_chat_history(self, payload, seq, writer):
        try:
            GetCallHistoryPayloadModel.model_validate(payload)
        except pydantic.ValidationError as error:
            self.logger.error(f"Возникли ошибки при валидации пакета: {error}")
            await self._send_error(
                seq, self.opcodes.VIDEO_CHAT_HISTORY, self.error_types.INVALID_PAYLOAD, writer
            )
            return
        payload = {"hasMore": False, "history": [], "backwardMarker": 0, "forwardMarker": 0}
        packet = self.proto.pack_packet(
            cmd=self.proto.CMD_OK, seq=seq, opcode=self.opcodes.VIDEO_CHAT_HISTORY, payload=payload
        )
        await self._send(writer, packet)

    async def start_private_call(self, payload, seq, writer, userId):
        from ownmax.protocols.max import call_service
        from aiohttp import web

        try:
            result = await call_service.SERVICE.start_native(userId, payload)
        except (ValueError, TypeError, KeyError, web.HTTPException):
            await self._send_error(seq, 78, self.error_types.INVALID_PAYLOAD, writer)
            return
        await self._send(writer, self.proto.pack_packet(cmd=1, seq=seq, opcode=78, payload=result))

    async def private_upload(self, payload, seq, writer, userId, opcode):
        from ownmax.protocols.max.media_service import allocate

        try:
            result = allocate(userId, opcode, payload or {})
        except (ValueError, TypeError):
            await self._send_error(seq, opcode, self.error_types.INVALID_PAYLOAD, writer)
            return
        await self._send(
            writer, self.proto.pack_packet(cmd=1, seq=seq, opcode=opcode, payload=result)
        )

    async def private_media_url(self, payload, seq, writer, userId):
        from ownmax.protocols.max.media_service import urls
        from aiohttp import web

        self.logger.info(
            "Media URL request uid=%s keys=%s", userId, ",".join(sorted((payload or {}).keys()))
        )
        try:
            result = urls(payload or {})
        except (ValueError, TypeError, web.HTTPException) as error:
            self.logger.warning("Media URL failed class=%s", type(error).__name__)
            await self._send_error(seq, 83, self.error_types.INVALID_PAYLOAD, writer)
            return
        await self._send(writer, self.proto.pack_packet(cmd=1, seq=seq, opcode=83, payload=result))

    async def private_audio_url(self, payload, seq, writer, userId):
        from ownmax.protocols.max.media_service import audio_urls
        from aiohttp import web

        try:
            result = audio_urls(payload or {})
        except (ValueError, TypeError, KeyError, IndexError, web.HTTPException):
            await self._send_error(seq, 301, self.error_types.INVALID_PAYLOAD, writer)
            return
        await self._send(writer, self.proto.pack_packet(cmd=1, seq=seq, opcode=301, payload=result))

    async def private_group_call(self, payload, seq, writer, userId, opcode):
        if opcode == 89:
            raw = str((payload or {}).get("link") or "")
            import uuid

            try:
                uuid.UUID(raw)
                is_call = True
            except ValueError:
                is_call = "/joincall/" in raw or raw.lstrip("/").startswith("joincall/")
            if not is_call:
                from ownmax.protocols.max.chat_links import operation

                await operation(self, 89, payload or {}, seq, writer, userId)
                return
        from ownmax.protocols.max import call_service
        from aiohttp import web

        try:
            result = await call_service.SERVICE.group_wire(userId, opcode, payload or {})
        except (ValueError, TypeError, KeyError, web.HTTPException):
            await self._send_error(seq, opcode, self.error_types.INVALID_PAYLOAD, writer)
            return
        await self._send(
            writer, self.proto.pack_packet(cmd=1, seq=seq, opcode=opcode, payload=result)
        )
