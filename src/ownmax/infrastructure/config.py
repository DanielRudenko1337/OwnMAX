from ownmax.settings import DATA_DIR
import os
from dotenv import load_dotenv

load_dotenv()


class ServerConfig:
    host = os.getenv("host") or "0.0.0.0"
    oneme_tcp_port = int(os.getenv("oneme_tcp_port") or 443)
    oneme_ws_port = int(os.getenv("oneme_ws_port") or 81)
    log_level = os.getenv("log_level") or "debug"
    db_type = os.getenv("db_type") or "mysql"
    db_host = os.getenv("db_host") or "127.0.0.1"
    db_port = int(os.getenv("db_port") or 3306)
    db_user = os.getenv("db_user") or "root"
    db_password = os.getenv("db_password") or "qwerty"
    db_name = os.getenv("db_name") or "ownmax"
    db_file = os.getenv("db_file") or str(DATA_DIR / "ownmax.db")
    certfile = os.getenv("certfile") or "cert.pem"
    keyfile = os.getenv("keyfile") or "key.pem"
    avatar_base_url = os.getenv("avatar_base_url") or "http://127.0.0.1/avatar/"
    telegram_bot_token = os.getenv("telegram_bot_token") or "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    telegram_bot_enabled = bool(int(os.getenv("telegram_bot_enabled", 0)))
    telegram_whitelist_ids = [
        x.strip() for x in os.getenv("telegram_whitelist_ids", "").split(",") if x.strip()
    ]
    telegram_whitelist_enabled = bool(int(os.getenv("telegram_whitelist_enabled", 0)))
    origins = (
        [x.strip() for x in os.getenv("origins", "").split(",") if x.strip()]
        if os.getenv("origins")
        else None
    )
    sms_gateway_url = os.getenv("sms_gateway_url", "")
    firebase_credentials_path = os.getenv("firebase_credentials_path", "")
    geo_db_path = os.getenv("geo_db_path", "")
