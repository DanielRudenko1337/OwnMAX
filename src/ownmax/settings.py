import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
DATA_DIR = Path(os.getenv("OWNMAX_DATA_DIR", "data")).expanduser().resolve()
PUBLIC_URL = os.getenv("OWNMAX_PUBLIC_URL", "https://localhost").rstrip("/") + "/"
MEDIA_URL = os.getenv("OWNMAX_MEDIA_URL", PUBLIC_URL).rstrip("/")
SIGNAL_URL = os.getenv(
    "OWNMAX_SIGNAL_URL",
    MEDIA_URL.replace("https://", "wss://").replace("http://", "ws://") + "/ws2",
)
TURN_URL = os.getenv("OWNMAX_TURN_URL", "turn:localhost:3478?transport=tcp")
STUN_URL = os.getenv("OWNMAX_STUN_URL", "stun:localhost:3478")
CALL_PHONES = tuple(
    (value.strip() for value in os.getenv("OWNMAX_CALL_PHONES", "").split(",") if value.strip())
)
HTTP_HOST = os.getenv("OWNMAX_HTTP_HOST", "127.0.0.1")
HTTP_PORT = int(os.getenv("OWNMAX_HTTP_PORT", "9183"))
