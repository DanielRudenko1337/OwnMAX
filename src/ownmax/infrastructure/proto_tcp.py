import logging
import lz4.block
import msgpack


class MobileProto:

    def __init__(self) -> None:
        self.logger = logging.getLogger(__name__)

    MAX_PAYLOAD_SIZE = 1048576
    MAX_DECOMPRESSED_SIZE = 1048576
    HEADER_SIZE = 10

    def unpack_packet(self, data: bytes) -> dict | None:
        if len(data) < self.HEADER_SIZE:
            self.logger.warning(f"Пакет слишком маленький: {len(data)} байт")
            return None
        ver = int.from_bytes(data[0:1], "big", signed=False)
        cmd = int.from_bytes(data[1:2], "big", signed=False)
        seq = int.from_bytes(data[2:4], "big", signed=False)
        opcode = int.from_bytes(data[4:6], "big", signed=False)
        packed_len = int.from_bytes(data[6:10], "big", signed=False)
        comp_flag = packed_len >> 24
        payload_length = packed_len & 16777215
        if payload_length > self.MAX_PAYLOAD_SIZE:
            self.logger.warning(
                f"Payload слишком большой: {payload_length} B (лимит {self.MAX_PAYLOAD_SIZE})"
            )
            return None
        if len(data) < self.HEADER_SIZE + payload_length:
            self.logger.warning(
                f"Пакет неполный: требуется {self.HEADER_SIZE + payload_length} B, получено {len(data)}"
            )
            return None
        payload_bytes = data[10 : 10 + payload_length]
        payload = None
        if payload_bytes:
            if comp_flag != 0:
                compressed_data = payload_bytes
                try:
                    payload_bytes = lz4.block.decompress(
                        compressed_data, uncompressed_size=self.MAX_DECOMPRESSED_SIZE
                    )
                except lz4.block.LZ4BlockError:
                    self.logger.warning("Ошибка декомпрессии LZ4")
                    return None
            payload = msgpack.unpackb(payload_bytes, raw=False, strict_map_key=False)
        self.logger.debug(
            f"Распаковал - ver={ver} cmd={cmd} seq={seq} opcode={opcode} payload={payload} comp_flag={comp_flag}"
        )
        return {"ver": ver, "cmd": cmd, "seq": seq, "opcode": opcode, "payload": payload}

    def pack_packet(
        self, ver: int = 10, cmd: int = 1, seq: int = 1, opcode: int = 6, payload: dict = {}
    ) -> bytes:
        ver_b = ver.to_bytes(1, "big")
        cmd_b = cmd.to_bytes(1, "big")
        seq_b = seq.to_bytes(2, "big")
        opcode_b = opcode.to_bytes(2, "big")
        payload_bytes: bytes | None = msgpack.packb(payload)
        if payload_bytes is None:
            payload_bytes = b""
        comp_flag = 0
        try:
            payload_comp = lz4.block.compress(
                payload_bytes, mode="high_compression", store_size=False
            )
            if len(payload_bytes) > len(payload_comp):
                final_payload = payload_comp
                comp_flag = 2
            else:
                final_payload = payload_bytes
        except Exception as e:
            self.logger.warning(f"Ошибка сжатия LZ4: {e}")
            final_payload = payload_bytes
        payload_len = len(final_payload) & 16777215
        packed_len = comp_flag << 24 | payload_len
        payload_len_b = packed_len.to_bytes(4, "big")
        self.logger.debug(
            f"Упаковал - ver={ver} cmd={cmd} seq={seq} opcode={opcode} payload={payload} comp_flag={comp_flag}"
        )
        return ver_b + cmd_b + seq_b + opcode_b + payload_len_b + final_payload

    CMD_OK = 1
    CMD_NOF = 2
    CMD_ERR = 3
    PROTO_VER = 10
