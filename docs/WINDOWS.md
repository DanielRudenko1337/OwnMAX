# OwnMAX for Windows

**English** | [Русский](WINDOWS.ru.md)

The Windows x64 build is based on MAX 26.38.0.82021 and is distributed separately from the server sources. This is a **preview**: the configurator and file structure have been checked, but the native application UI has not been launched on a real Windows machine for this release.

Download [OwnMAX-Windows-x64.zip](https://github.com/DanielRudenko1337/OwnMAX/releases/download/v0.1.0/OwnMAX-Windows-x64.zip). Its checksum is included in `SHA256SUMS.txt` on the [release page](https://github.com/DanielRudenko1337/OwnMAX/releases/tag/v0.1.0).

## Where to change the server

1. Extract the **entire** ZIP into a separate writable folder in your user profile. Keep all DLLs and subfolders beside `max.exe`.
2. Close this build, its tray icon, and its `max-service` process.
3. Open **`server.json` beside `START_OwnMAX.cmd`** in Notepad.
4. Replace the placeholder with your own host and MAX TCP port:

```json
{
  "host": "m.example.ru",
  "port": 8443
}
```

`m.example.ru` is an example: replace it with a domain you control. An IPv4 address is also accepted. Do not include `https://`, a path, or `:port` in `host`.

**This binary layout supports at most 12 ASCII characters in `host`.** If your IP or domain is longer, create a short DNS alias pointing to your server. For example, `m.example.ru` is 12 characters long. IPv6 is not supported by this configurator. Valid ports range from 1 to 65535.

5. Save `server.json` and run **`START_OwnMAX.cmd`**.

The launcher runs `Configure-OwnMAX.ps1`, verifies the binary hashes, updates the endpoint in both `config.dll` copies, `max.exe`, and `max-service.exe`, synchronizes the local configuration, and starts the client. To switch servers, edit `server.json` and run the launcher again. Manual EXE editing is unnecessary.

The unconfigured build contains the nonfunctional `test.invalid` placeholder. Launching `max.exe` before applying settings does not connect it to your server.

## Server requirements

| Setting | Requirement |
| --- | --- |
| DNS | `host` must resolve to your server |
| TCP port | `port` must match `oneme_tcp_port` in the server `.env` and be reachable through its firewall |
| TLS | The certificate must be trusted by Windows and valid for the chosen DNS name or IP SAN |
| Media and calls | Configure `OWNMAX_PUBLIC_URL`, `OWNMAX_MEDIA_URL`, `OWNMAX_SIGNAL_URL`, and TURN/STUN on the server separately |
| Call access | List allowed numbers in `OWNMAX_CALL_PHONES`; calls are experimental |

The configurator does not disable TLS validation or install certificates. If you use a short DNS alias, the certificate must cover that alias.

The native configuration is written to **`%LOCALAPPDATA%\OwnMAX\config.json`**. The launcher regenerates it from `server.json` and `config.template.json`, so edit `server.json` for ordinary server changes.

## Sign-in

Enter a phone number in the client. Read the sign-in or registration code from **your OwnMAX server console**. Codes contain six digits and expire after five minutes. SMS and Telegram are not used to deliver the test code.

## Verification and limitations

Checks cover ZIP CRC, neutral binary hashes, preserved PE sizes and structure, configuration application, repeated server changes, ports 1 and 65535, and rejection of damaged binaries or invalid settings. The PowerShell configurator was executed on test copies under Linux; this does not replace testing the native client on Windows.

The private server address has been removed from the release ZIP contents, including EXEs, DLLs, and instructions. User databases, tokens, and connection settings are not included. Do not mix client builds: the configurator rejects unknown binary versions.

Modified EXEs/DLLs do not have valid publisher signatures; Windows may warn about an unknown publisher. Official updates replace this build's files and settings. Third-party files and copyright notices are retained. The server's MIT license does not relicense the MAX client or its dependencies. See `THIRD_PARTY_NOTICE.txt` inside the ZIP.

For questions or Windows test results, contact [@danielrudenko](https://t.me/danielrudenko).
