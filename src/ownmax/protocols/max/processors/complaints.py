import pydantic
import time
from ownmax.core.baseprocessor import BaseProcessor
from ownmax.protocols.max.models import ComplainReasonsGetPayloadModel


class ComplaintsProcessors(BaseProcessor):

    async def complain_reasons_get(self, payload, seq, writer):
        try:
            ComplainReasonsGetPayloadModel.model_validate(payload)
        except pydantic.ValidationError as error:
            self.logger.error(f"Возникли ошибки при валидации пакета: {error}")
            await self._send_error(
                seq, self.opcodes.COMPLAIN_REASONS_GET, self.error_types.INVALID_PAYLOAD, writer
            )
            return
        payload = {"complains": self.static.COMPLAIN_REASONS, "complainSync": int(time.time())}
        packet = self.proto.pack_packet(
            seq=seq, opcode=self.opcodes.COMPLAIN_REASONS_GET, payload=payload
        )
        await self._send(writer, packet)
