import asyncio
from ownmax.protocols.tamtam.socket import TamTamMobile
from ownmax.protocols.tamtam.websocket import TamTamWS
from ownmax.core.controllerbase import ControllerBase
from ownmax.infrastructure.config import ServerConfig
from ownmax.infrastructure.proto_tcp import MobileProto
from ownmax.infrastructure.proto_web import WebProto
from ownmax.infrastructure.opcodes import Opcodes


class TTController(ControllerBase):

    def __init__(self):
        self.config = ServerConfig()
        self.proto_tcp = MobileProto()
        self.proto_web = WebProto()
        self.opcodes = Opcodes()

    async def event(self, target, client, eventData):
        eventType = eventData.get("eventType")
        writer = client.get("writer")
        is_web = client.get("type") == "web"
        proto = self.proto_web if is_web else self.proto_tcp
        packet = None
        if writer == eventData.get("writer"):
            return
        if eventType == "new_msg":
            chatId = eventData.get("chatId")
            message = eventData.get("message")
            prevMessageId = eventData.get("prevMessageId")
            time = eventData.get("time")
            payload = {
                "chatId": chatId,
                "message": message,
                "prevMessageId": prevMessageId,
                "ttl": False,
                "unread": 0,
                "mark": time,
            }
            packet = proto.pack_packet(
                cmd=0, seq=1, opcode=self.opcodes.NOTIF_MESSAGE, payload=payload
            )
        elif eventType == "typing":
            chatId = eventData.get("chatId")
            userId = eventData.get("userId")
            type = eventData.get("type")
            payload = {"chatId": chatId, "userId": userId, "type": type}
            packet = proto.pack_packet(
                cmd=0, seq=1, opcode=self.opcodes.NOTIF_TYPING, payload=payload
            )
        elif eventType == "profile_updated":
            profile = eventData.get("profile")
            payload = {"profile": profile}
            packet = proto.pack_packet(
                cmd=0, seq=1, opcode=self.opcodes.NOTIF_PROFILE, payload=payload
            )
        elif eventType == "presence":
            userId = eventData.get("userId")
            presence = eventData.get("presence")
            event_time = eventData.get("time")
            payload = {"userId": userId, "presence": presence, "time": event_time}
            packet = proto.pack_packet(
                cmd=0, seq=1, opcode=self.opcodes.NOTIF_PRESENCE, payload=payload
            )
        if not packet:
            return
        if is_web:
            await writer.send(packet)
        else:
            writer.write(packet)
            await writer.drain()

    def launch(self, api):

        async def _start_all():
            await asyncio.gather(
                TamTamMobile(
                    host=self.config.host,
                    port=self.config.tamtam_tcp_port,
                    ssl_context=api["ssl"],
                    db_pool=api["db"],
                    clients=api["clients"],
                    send_event=api["event"],
                ).start(),
                TamTamWS(
                    host=self.config.host,
                    port=self.config.tamtam_ws_port,
                    ssl_context=api["ssl"],
                    db_pool=api["db"],
                    clients=api["clients"],
                    send_event=api["event"],
                ).start(),
            )

        return _start_all()
