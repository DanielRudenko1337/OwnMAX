from ownmax.core.baseprocessor import BaseProcessor
from ownmax.protocols.tamtam.models import ChatSubscribePayloadModel


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
