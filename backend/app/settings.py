import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database: Path
    password_file: Path | None = None
    certificate: Path | None = None
    private_key: Path | None = None
    allowed_hosts: tuple[str, ...] = ("localhost",)
    allowed_origins: tuple[str, ...] = ("https://localhost:8443",)
    static: Path = Path(__file__).parent / "static"
    secure: bool = True
    start_workers: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        def path(name: str) -> Path | None:
            value = os.getenv(name)
            return Path(value) if value else None

        hosts = tuple(x.strip() for x in os.getenv("APP_ALLOWED_HOSTS", "").split(",") if x.strip())
        origins = tuple(
            x.strip().rstrip("/")
            for x in os.getenv("APP_ALLOWED_ORIGINS", "").split(",")
            if x.strip()
        )
        if not hosts or not origins or "*" in hosts:
            raise ValueError("Set explicit APP_ALLOWED_HOSTS and APP_ALLOWED_ORIGINS")
        if any(not origin.startswith("https://") for origin in origins):
            raise ValueError("Allowed origins must use HTTPS")
        cert, key = path("APP_TLS_CERT_FILE"), path("APP_TLS_KEY_FILE")
        if cert is None or key is None or not cert.is_file() or not key.is_file():
            raise ValueError("Readable APP_TLS_CERT_FILE and APP_TLS_KEY_FILE are required")
        return cls(
            database=Path(os.getenv("APP_DATABASE_PATH", "/data/energy.sqlite3")),
            password_file=path("APP_ADMIN_PASSWORD_FILE"),
            certificate=cert,
            private_key=key,
            allowed_hosts=hosts,
            allowed_origins=origins,
        )
