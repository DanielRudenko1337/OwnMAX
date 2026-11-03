# OwnMAX

**English** | [Русский](README.ru.md)

**A self-hosted messaging server for chats, groups, media, and bots, with MAX and TamTam protocol support.**

OwnMAX is an independent project. It is not the official MAX, is not affiliated with its developers, and is not supported or endorsed by them. The names MAX and TamTam describe protocol compatibility, not a partnership or endorsement.

OwnMAX aims to run core messaging workflows on your own server, with your own database and configuration. This repository contains a Python server, a separate SMS service, and a sample bot. Client applications, APKs, IPAs, and Windows builds are not included. You need a client configured to connect to your server; installing an official client alone does not configure that connection.

If you find OwnMAX useful, **give the repository a Star on GitHub**. It is a simple way to support the project and help others discover it.

## Features

The server implements core messaging workflows. Coverage depends on the protocol, client version, and server configuration. MAX and TamTam do not have identical feature sets.

| Area | Implementation | Status |
| --- | --- | --- |
| Authentication | Code requests and verification, sign-in, registration, user sessions | Test sign-in and registration codes appear in the server console; corresponding requests are tested for both protocols |
| Account | Profile, description, avatar, session details; MAX password and QR authentication handlers | Implemented; verification coverage varies by feature |
| Contacts | Contact search and addition, user details, presence updates | Implemented |
| Messages | Sending, history retrieval, deletion, reactions, read marks, and typing events | Implemented; activity refresh and expiry are tested in isolation |
| Chats and groups | Group creation, participants, permissions, chat management, and MAX invitation links | Implemented; not every operation has been tested with real clients |
| Folders | Folder retrieval and modification; extended MAX operations | Implemented; TamTam creation, retrieval, user isolation, and invalid requests are tested |
| Media | Photo, file, audio, and video upload and retrieval; signed URLs and ownership checks | Implemented; avatar lifecycle and ownership checks are tested |
| MAX bots | Bot creation and management, profiles and avatars, HTTP API for messages, updates, and uploads | Implemented; first bot start and avatar management are tested |
| MAX calls | Signaling, participant state, personal and group workflows, WebRTC relay, and screen data forwarding | Experimental; local audio, screen, leave, and rejoin scenarios pass isolated checks |
| Storage | SQLite, empty database schemas, and a MySQL adapter | SQLite is the primary verified option; MySQL schema coverage for all features is not confirmed |
| Integrations | Telegram, Firebase push, session geolocation, and a separate SMS gateway | Require your own configuration; external services are outside the isolated test suite |

This is not a promise of complete compatibility with every official application feature. Protocol changes or a particular client version may require adjustments. Experimental calls should not be treated as a production communications platform.

## Test sign-in and registration

For any valid phone number, the server generates a random six-digit code and prints it to the **server process console**. The current runtime message is in Russian:

```text
OwnMAX: код входа или регистрации для <phone number>: <6 digits>
```

This applies both to existing-account sign-in and new-account registration. Codes expire after five minutes. Enter the code from the console in the connected client.

**SMS and Telegram are not called to deliver these codes.** This is a test authentication workflow: anyone with access to the server console or logs can see sign-in codes. Do not publish logs containing active codes.

For a container deployment:

```sh
docker compose logs -f ownmax
```

## Quick start

You need **Python 3.12 or later**. Run the following commands from the repository root:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
cp .env.example .env
```

Edit `.env` to set your server addresses and the paths to your TLS certificate and private key. The default paths are `certs/cert.pem` and `certs/key.pem`.

Initialize an empty database and start the server:

```sh
python -m ownmax init-db
python -m ownmax serve
```

`init-db` creates a SQLite database and call secrets. It does not overwrite an existing database: attempting to initialize the same file again fails. The repository contains schemas only, not a populated database with users or conversations.

After installation, `ownmax serve` is equivalent to `python -m ownmax serve`.

## Configuration

Set the main configuration in `.env`. See [.env.example](.env.example) for the full example.

| Variable | Purpose |
| --- | --- |
| `host` | TCP and WebSocket listener address |
| `oneme_tcp_port`, `oneme_ws_port` | MAX protocol ports |
| `tamtam_tcp_port`, `tamtam_ws_port` | TamTam protocol ports |
| `db_type` | Database type; the example uses `sqlite` |
| `db_file` | Optional SQLite database path; defaults to `ownmax.db` inside the state directory |
| `OWNMAX_DATA_DIR` | Directory for database files, secrets, and other server state |
| `certfile`, `keyfile` | TLS certificate and private key paths |
| `OWNMAX_PUBLIC_URL` | Public base HTTPS URL of the server |
| `OWNMAX_MEDIA_URL` | Public media service URL |
| `OWNMAX_SIGNAL_URL` | Public WebSocket URL for call signaling |
| `OWNMAX_TURN_URL`, `OWNMAX_STUN_URL` | Your TURN/STUN infrastructure addresses |
| `OWNMAX_HTTP_HOST`, `OWNMAX_HTTP_PORT` | Internal HTTP listener; defaults to `127.0.0.1:9183` |
| `OWNMAX_CALL_PHONES` | Comma-separated phone numbers allowed to use experimental calls; an empty list allows nobody |
| `origins` | Allowed WebSocket Origin values |
| `log_level` | Logging level |

Replace the example `localhost` addresses with your public addresses if clients connect from another device. `127.0.0.1` refers to the local machine; `0.0.0.0` is a listener address for network interfaces.

The internal HTTP service handles media, the Bot API, and signaling. For public access, configure a reverse proxy with TLS and WebSocket support. Changing a URL in `.env` alone is not enough: the domain, certificate, proxy, and routing must agree.

## Calls and screen sharing

Calls are experimental and are available only to phone numbers listed in `OWNMAX_CALL_PHONES`.

Connections across machines require suitable public signaling addresses and your own TURN/STUN setup. The shared TURN secret must match the `turn` value in the private `call-secrets.json` file inside the state directory. Do not commit that file to the repository.

Isolated checks cover audio forwarding with nonzero samples, stable Opus encoding when audio sources change, screen data forwarding, independent participant departure, and rejoining. They do not establish call quality across all networks or support for every real-client mode.

## Bots

MAX support includes a bot manager and HTTP API routes at `/botapi/me`, `/botapi/messages`, `/botapi/updates`, and `/botapi/uploads`. The implemented method set is defined by the server handler; it is not claimed to be a complete copy of an external Bot API.

The example in [examples/hello_bot.py](examples/hello_bot.py) responds to greetings and the `/start`, `/help`, and `/ping` commands.

Set your bot token as an environment variable, then run:

```sh
python examples/hello_bot.py
```

`OWNMAX_BOT_TOKEN` is required. Optional variables are `OWNMAX_BOT_API` for the API URL and `OWNMAX_BOT_STATE` for the update-position state file. Do not hard-code or publish your token.

## Docker

Docker and Docker Compose are required. Prepare `.env` and your certificate files as described in the regular setup instructions.

```sh
docker compose build
docker compose run --rm ownmax python -m ownmax init-db
docker compose up -d
```

The ports in [compose.yaml](compose.yaml) match [.env.example](.env.example). If you change the ports, update both configurations. Database files and secrets use a separate container volume; certificates are mounted read-only. The internal HTTP port is published only on the host loopback interface.

The Docker image has been checked by building it and initializing an empty database. The full Compose stack was not launched during this verification because the test VDS did not have the Compose plugin installed.

## Verification

After installing the project:

```sh
python tests/run.py
```

The current suite contains **19 unit tests and 8 isolated scenarios**. They use temporary state, local connections, and synthetic accounts. They do not require a live database, production tokens, or real conversations.

Checks cover packet encoding, token scope and validation, installation and database initialization without overwriting existing data, portable configuration, absence of comments in Python sources, task shutdown before database closure, sign-in and registration code requests, WebSocket dispatch, TamTam folders, the sample bot, chat activity, avatars, bot startup, and selected call scenarios.

Additional checks covered Python package building, dependencies, startup of five local listeners with a temporary TLS certificate, and clean process termination. Isolated checks do not replace testing your deployment with real clients.

GitHub Actions checks are prepared in [.github/workflows/checks.yml](.github/workflows/checks.yml).

## Repository layout

| Directory | Contents |
| --- | --- |
| `src/ownmax/core` | Base handlers |
| `src/ownmax/infrastructure` | Transport, configuration, storage access, and supporting services |
| `src/ownmax/protocols/max` | MAX handlers, chats, bots, media, and experimental calls |
| `src/ownmax/protocols/tamtam` | TamTam handlers |
| `src/ownmax/integrations` | Telegram integration |
| `src/ownmax/storage` | SQL schemas for empty databases |
| `services/sms` | Separate SMS gateway; not used for console-based test sign-in codes |
| `examples` | Sample bot |
| `tests` | Unit tests and isolated scenarios |

Live databases, `.env`, private keys, certificates, and client builds are excluded from the source archive. Create your own deployment state.

## Feedback

For questions, suggestions, and feedback, contact the project author on Telegram: **[@danielrudenko](https://t.me/danielrudenko)**.

When reporting a bug, describe the action, expected result, client version, and actual behavior. Include logs without tokens, sign-in codes, or personal data. You can also open a GitHub issue to track the problem.


## License and attribution

OwnMAX is distributed under the [MIT License](LICENSE).

You may use, modify, and distribute the code, including your own versions and commercial uses. Copies and substantial portions of the code must retain the `Copyright (c) 2026 OwnMAX` notice and the license text. This condition also applies to derivative versions. The software is provided without warranties; see `LICENSE` for the exact terms.

If you publish a project based on OwnMAX, please also credit OwnMAX in your README and link to the original repository. Dependencies retain their own licenses; the OwnMAX license does not replace their terms.
