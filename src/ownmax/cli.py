import argparse
import asyncio
import json
import os
import secrets
import sqlite3
from importlib.resources import files


def initialize():
    from ownmax.settings import DATA_DIR
    from ownmax.infrastructure.config import ServerConfig

    if ServerConfig.db_type != "sqlite":
        raise ValueError("init-db requires db_type=sqlite")
    from pathlib import Path

    database = Path(ServerConfig.db_file)
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    try:
        with sqlite3.connect(database) as connection:
            connection.executescript(
                files("ownmax.storage").joinpath("schema.sqlite.sql").read_text()
            )
    except Exception:
        database.unlink()
        raise
    keyfile = DATA_DIR / "call-secrets.json"
    try:
        with open(keyfile, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
            json.dump(
                {"signing": secrets.token_urlsafe(48), "turn": secrets.token_urlsafe(48)}, stream
            )
    except FileExistsError:
        pass
    print("OwnMAX database initialized")


def main():
    parser = argparse.ArgumentParser(prog="ownmax")
    parser.add_argument("command", choices=("serve", "init-db"), nargs="?", default="serve")
    args = parser.parse_args()
    if args.command == "init-db":
        initialize()
    else:
        from ownmax.app import main as serve

        asyncio.run(serve())
