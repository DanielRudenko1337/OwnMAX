OwnMAX — Windows x64 preview

Client base: MAX 26.38.0.82021. This is a modified third-party client build,
not an OwnMAX client built from its own source code. The OwnMAX server is
independent of the developers of the original MAX application.

SERVER CONFIGURATION

1. Extract the ENTIRE ZIP into a separate writable folder in your user profile.
   Keep all DLLs and subfolders. Do not run the application inside the ZIP.
2. Close this build, including its tray icon and max-service background process.
3. Open server.json beside START_OwnMAX.cmd in Notepad.
4. Set host to YOUR server address and port to its MAX TCP port:

   { "host": "m.example.ru", "port": 8443 }

   m.example.ru is an example: replace it with your own domain. Use only an
   ASCII DNS name or IPv4 address, without https://, a path, or :port.
   This binary layout supports AT MOST 12 CHARACTERS in host. If your IP or
   domain is longer, create a short DNS alias for your server. m.example.ru
   is 12 characters long. IPv6 is not supported by this configurator.
   Valid ports: 1–65535.
5. Save the file and run START_OwnMAX.cmd. It applies server.json to both
   config DLLs, the EXE and service via Configure-OwnMAX.ps1, then starts
   the client. Running max.exe before setup uses the test.invalid placeholder.

The native configuration file is also written to:
   %LOCALAPPDATA%\OwnMAX\config.json
For each server change, edit server.json and run START_OwnMAX.cmd again.
This updates the local configuration and embedded endpoints together.
Do not mix EXEs/DLLs from different builds: hash validation will reject them.

SERVER REQUIREMENTS

The server must listen on the TCP port configured in server.json
(oneme_tcp_port in the server .env). Its TLS certificate must be trusted by
Windows and valid for the selected DNS name or IPv4 address. The configurator
does NOT disable TLS validation or install certificates. Point the domain
at your server and allow the port through its firewall. Configure your own
public media and signaling URLs in the server .env.

SIGN-IN

Enter a phone number. Read the six-digit sign-in or registration code from
YOUR OwnMAX server console. It expires after five minutes. SMS and Telegram
are not used to deliver the test code.

VERIFICATION AND LIMITATIONS

The private server address has been removed from this release. test.invalid
is a nonfunctional placeholder. ZIP CRC, PE structure and sizes, patch ranges,
the configurator, and server/port changes are checked on test copies. The
application UI has not been launched on a real Windows machine for this
release: this is a PREVIEW, not a guarantee of complete client compatibility.
Server calls remain experimental.

Modified EXEs/DLLs are unsigned; Windows may warn about an unknown publisher.
Official updates replace the files and settings of this build. Do not install
a different updater over it. Third-party DLLs and notices are preserved.
See THIRD_PARTY_NOTICE.txt.

Feedback: https://t.me/danielrudenko
Server and documentation: https://github.com/DanielRudenko1337/OwnMAX
