import argparse
import base64
import copy
import hashlib
import json
from pathlib import Path
import re
import struct
import zipfile

PLACEHOLDER = "test.invalid"
BINARY_PATHS = ("config.dll", "max-service/config.dll", "max.exe", "max-service/max-service.exe")


def pe_info(data):
    pe = struct.unpack_from("<I", data, 60)[0]
    if data[pe : pe + 4] != b"PE\0\0":
        raise ValueError("Invalid PE signature")
    opt = pe + 24
    if struct.unpack_from("<H", data, opt)[0] != 0x20B:
        raise ValueError("Expected a Windows x64 binary")
    sections = []
    table = opt + struct.unpack_from("<H", data, pe + 20)[0]
    for i in range(struct.unpack_from("<H", data, pe + 6)[0]):
        start = table + i * 40
        virtual_size, rva, size, offset = struct.unpack_from("<IIII", data, start + 8)
        sections.append((rva, max(virtual_size, size), offset))
    return opt, sections


def file_offset(sections, rva):
    for start, size, offset in sections:
        if start <= rva < start + size:
            return offset + rva - start
    raise ValueError("RVA outside PE sections")


def embedded_config(data):
    start = data.rfind(b"{", 0, data.index(b'"ssl_url2"'))
    end = data.index(b"\0", start)
    return json.loads(data[start:end]), start, end


def overwrite_field(data, name, value):
    pattern = rb'"' + name.encode() + rb'"\s*:\s*(?:"[^"\r\n]*"|true|false|[0-9]+)'
    matches = list(re.finditer(pattern, data))
    if len(matches) != 1:
        raise ValueError("Unexpected JSON field: " + name)
    match = matches[0]
    replacement = json.dumps(name).encode() + b":" + json.dumps(value).encode()
    if len(replacement) > len(match.group()):
        raise ValueError("JSON field does not fit: " + name)
    data[match.start() : match.end()] = replacement.ljust(len(match.group()), b" ")
    return match.start(), len(match.group())


def patch_binary(path, original, old_host):
    data = bytearray(original)
    opt, sections = pe_info(data)
    struct.pack_into("<I", data, opt + 64, 0)
    struct.pack_into("<II", data, opt + 112 + 4 * 8, 0, 0)
    ranges = []
    if path.endswith("config.dll"):
        config, _, _ = embedded_config(data)
        if config["ssl_url2"] != old_host:
            raise ValueError("Config DLL endpoint mismatch")
        offset, length = overwrite_field(data, "ssl_url2", PLACEHOLDER)
        ranges.append((offset, length, "host_json"))
        offset, length = overwrite_field(data, "ssl_port", 8443)
        ranges.append((offset, length, "port_json"))
        overwrite_field(data, "app_folder_name", "OwnMAX")
        overwrite_field(data, "single_instance_id", "ownmax_instance")
        overwrite_field(data, "local_server_pipe_name", "ownmax_local_pipe")
        overwrite_field(data, "api_fallback_url", "")
        overwrite_field(data, "rooms_auto_update_enabled", False)
        old_wide = "ONEME".encode("utf-16le")
        if data.count(old_wide) != 1:
            raise ValueError("Unexpected native config directory")
        offset = data.index(old_wide)
        if data[offset + 10 : offset + 16] != bytes(6):
            raise ValueError("Native path slot has no padding")
        data[offset : offset + 16] = "OwnMAX".encode("utf-16le").ljust(16, b"\0")
        instruction = file_offset(sections, 0x3B034)
        if data[instruction : instruction + 8] != b"\x48\xc7\x45\xbf\x05\x00\x00\x00":
            raise ValueError("Unsupported native config path code")
        struct.pack_into("<I", data, instruction + 4, 6)
        old = old_host.encode()
        positions = [m.start() for m in re.finditer(re.escape(old), data)]
        if len(positions) != 1:
            raise ValueError("Unexpected native host string")
        offset = positions[0]
        if data[offset + len(old)] != 0:
            raise ValueError("Native host is not terminated")
        data[offset : offset + len(old)] = PLACEHOLDER.encode()
        ranges.append((offset, len(old) + 1, "cstring"))
    elif path == "max.exe":
        old = old_host.encode("utf-16le")
        if data.count(old) != 1:
            raise ValueError("Unexpected Qt endpoint")
        offset = data.index(old)
        if struct.unpack_from("<I", data, offset - 4)[0] != len(old_host):
            raise ValueError("Unexpected Qt string length")
        data[offset : offset + len(old)] = PLACEHOLDER.encode("utf-16le")
        ranges.append((offset - 4, 4 + len(old) + 2, "qstring"))
    else:
        old = old_host.encode()
        if data.count(old) != 1:
            raise ValueError("Unexpected service endpoint")
        offset = data.index(old)
        if data[offset + len(old)] != 0:
            raise ValueError("Service endpoint is not terminated")
        data[offset : offset + len(old)] = PLACEHOLDER.encode()
        ranges.append((offset, len(old) + 1, "cstring"))
    if len(data) != len(original):
        raise ValueError("Binary length changed")
    patches = [
        {
            "offset": offset,
            "length": length,
            "kind": kind,
            "template": base64.b64encode(data[offset : offset + length]).decode(),
        }
        for offset, length, kind in sorted(ranges)
    ]
    return bytes(data), {
        "path": path,
        "sha256": hashlib.sha256(data).hexdigest(),
        "patches": patches,
    }


def build(source, output):
    manifest = {"version": 1, "host_limit": 12, "files": []}
    script_dir = Path(__file__).resolve().parent
    with zipfile.ZipFile(source) as source_zip:
        candidates = [
            n for n in source_zip.namelist() if n.endswith("/max.exe") and n.count("/") == 1
        ]
        if len(candidates) != 1:
            raise ValueError("Expected one archive root containing max.exe")
        prefix = candidates[0].rsplit("/", 1)[0] + "/"
        config, _, _ = embedded_config(source_zip.read(prefix + "config.dll"))
        old_host = config["ssl_url2"]
        if len(old_host) != len(PLACEHOLDER):
            raise ValueError("This builder supports the supplied 26.38.0 private-client layout")
        private_markers = [old_host.encode(), old_host.encode("utf-16le")]
        output.parent.mkdir(parents=True, exist_ok=True)
        templates = {}
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as target:
            for entry in source_zip.infolist():
                if entry.is_dir():
                    continue
                if not entry.filename.startswith(prefix):
                    raise ValueError("Unexpected archive entry")
                relative = entry.filename[len(prefix) :]
                if relative.startswith("/") or ".." in Path(relative).parts:
                    raise ValueError("Unsafe archive entry")
                if Path(relative).name.upper().startswith("README") or relative.endswith(".cmd"):
                    continue
                data = source_zip.read(entry)
                if relative in BINARY_PATHS:
                    data, record = patch_binary(relative, data, old_host)
                    manifest["files"].append(record)
                    if relative == "config.dll":
                        clean_config, _, _ = embedded_config(data)
                        templates["config.template.json"] = json.dumps(
                            clean_config, ensure_ascii=False, indent=2
                        ).encode()
                if any(marker in data for marker in private_markers):
                    raise ValueError("Private endpoint remains in " + relative)
                info = copy.copy(entry)
                info.filename = "OwnMAX/" + relative
                target.writestr(info, data)
            if len(manifest["files"]) != len(BINARY_PATHS):
                raise ValueError("Missing expected binary")
            templates["binary-manifest.json"] = json.dumps(manifest, indent=2).encode()
            templates["server.json"] = (
                json.dumps({"host": PLACEHOLDER, "port": 8443}, indent=2) + "\n"
            ).encode()
            templates["Configure-OwnMAX.ps1"] = (script_dir / "Configure-OwnMAX.ps1").read_bytes()
            templates["START_OwnMAX.cmd"] = (
                b'@echo off\r\ncd /d "%~dp0"\r\npowershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Configure-OwnMAX.ps1"\r\nif errorlevel 1 (\r\n  pause\r\n  exit /b 1\r\n)\r\nstart "" "%~dp0max.exe"\r\n'
            )
            for name in [
                "README_WINDOWS_RU.txt",
                "README_WINDOWS_EN.txt",
                "THIRD_PARTY_NOTICE.txt",
            ]:
                templates[name] = (script_dir / name).read_bytes()
            for name, data in templates.items():
                if any(marker in data for marker in private_markers):
                    raise ValueError("Private endpoint remains in generated file")
                target.writestr("OwnMAX/" + name, data)
    with zipfile.ZipFile(output) as archive:
        damaged = archive.testzip()
        if damaged:
            raise ValueError("CRC failure: " + damaged)
    print(
        json.dumps(
            {
                "archive": str(output),
                "bytes": output.stat().st_size,
                "sha256": hashlib.file_digest(output.open("rb"), "sha256").hexdigest(),
                "private_endpoint_removed": True,
                "binary_files": len(manifest["files"]),
            }
        ),
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.source, args.output)


if __name__ == "__main__":
    main()
