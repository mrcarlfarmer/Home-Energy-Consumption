import argparse
import asyncio
import getpass
import os
import sqlite3
from pathlib import Path

from app.auth import hash_password
from app.db.store import Store


async def execute(command: str, database: Path, output: Path | None) -> None:
    if not await asyncio.to_thread(database.is_file):
        raise ValueError("Database does not exist; start the application first")
    store = Store(database)
    await store.open()
    try:
        if command == "password-reset":
            password = getpass.getpass("New dashboard password (12+ characters): ")
            if password != getpass.getpass("Confirm password: "):
                raise ValueError("Passwords do not match")
            await store.set_password(await asyncio.to_thread(hash_password, password))
            print("Password updated. Running sessions are revoked within 15 seconds.")
        elif command == "backup":
            if output is None or await asyncio.to_thread(output.exists):
                raise ValueError("Specify --output pointing to a new backup file")
            output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            target = sqlite3.connect(output)
            try:
                await store.writer.backup(target, pages=128, sleep=0.05)
                result = target.execute("PRAGMA quick_check").fetchone()
                if result is None or result[0] != "ok":
                    raise RuntimeError("Backup integrity check failed")
            finally:
                target.close()
            print(f"Consistent backup written to {output}; it contains secrets.")
        elif command == "rebuild":
            async with store.write() as conn:
                await conn.execute(
                    "INSERT OR IGNORE INTO dirty_rollup_buckets "
                    "SELECT device_id,read_at_us/300000000*300000000 FROM readings GROUP BY 1,2"
                )
            total = 0
            while count := await store.rebuild(24):
                total += count
            print(f"Rebuilt {total} five-minute buckets.")
    finally:
        await store.close()


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Private energy-monitor administration")
    parser.add_argument("command", choices=["backup", "password-reset", "rebuild"])
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.getenv("APP_DATABASE_PATH", "/data/energy.sqlite3")),
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    asyncio.run(execute(args.command, args.database, args.output))


if __name__ == "__main__":
    main()
