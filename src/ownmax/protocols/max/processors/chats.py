from ownmax.core.baseprocessor import BaseProcessor
from ownmax.protocols.max.models import ChatSubscribePayloadModel


class ChatsProcessors(BaseProcessor):

    async def chat_subscribe(self, payload, seq, writer):
        try:
            ChatSubscribePayloadModel.model_validate(payload)
        except Exception as e:
            await self._send_error(
                seq, self.opcodes.CHAT_SUBSCRIBE, self.error_types.INVALID_PAYLOAD, writer
            )
            return
        packet = self.proto.pack_packet(
            cmd=self.proto.CMD_OK, seq=seq, opcode=self.opcodes.CHAT_SUBSCRIBE, payload=None
        )
        await self._send(writer, packet)

    async def private_group_operation(self, payload, seq, writer, userId, opcode):
        from ownmax.protocols.max.groups import operation

        await operation(self, opcode, payload, seq, writer, userId)

    async def chat_read_operation(self, payload, seq, writer, userId, opcode):
        from ownmax.protocols.max.read_marks import handle

        await handle(self, payload or {}, seq, writer, userId, opcode)

    async def chat_lifecycle_operation(self, payload, seq, writer, userId, opcode):
        from ownmax.protocols.max.chat_lifecycle import operation

        await operation(self, payload or {}, seq, writer, userId, opcode)

    async def chat_link_operation(self, payload, seq, writer, userId, opcode):
        from ownmax.protocols.max.chat_links import operation

        await operation(self, opcode, payload or {}, seq, writer, userId)

    async def native_bot_info(self, payload, seq, writer, userId, opcode):
        from ownmax.protocols.max.bot_manager import native_info

        await native_info(self, payload or {}, seq, writer, userId, opcode)

    async def native_bot_suspend(self, payload, seq, writer, userId):
        from ownmax.protocols.max.bot_manager import suspend

        await suspend(self, payload or {}, seq, writer, userId)
