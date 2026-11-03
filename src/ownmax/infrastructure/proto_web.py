import json


class WebProto:

    def pack_packet(self, ver=10, cmd=1, seq=0, opcode=1, payload=None):
        return json.dumps(
            {"ver": ver, "cmd": cmd, "seq": seq, "opcode": opcode, "payload": payload}
        )

    MAX_PACKET_SIZE = 65536

    def unpack_packet(self, packet):
        if isinstance(packet, (str, bytes)) and len(packet) > self.MAX_PACKET_SIZE:
            return {}
        try:
            parsed_packet = json.loads(packet)
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}
        return parsed_packet

    CMD_OK = 1
    CMD_NOF = 2
    CMD_ERR = 3
    PROTO_VER = 10
