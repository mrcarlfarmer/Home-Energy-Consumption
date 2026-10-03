import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


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
    auth_required: bool = True
    start_workers: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        def path(name: str) -> Path | None:
            value = os.getenv(name)
            return Path(value) if value else None

        transport = os.getenv("APP_TRANSPORT", "https")
        if transport not in ("http", "https"):
            raise ValueError("APP_TRANSPORT must be http or https")
        secure = transport == "https"
        auth_required = os.getenv("APP_AUTH_REQUIRED", "true")
        if auth_required not in ("true", "false"):
            raise ValueError("APP_AUTH_REQUIRED must be true or false")
        hosts = tuple(x.strip() for x in os.getenv("APP_ALLOWED_HOSTS", "").split(",") if x.strip())
        origins: list[str] = []
        for value in os.getenv("APP_ALLOWED_ORIGINS", "").split(","):
            if not value.strip():
                continue
            origin = urlsplit(value.strip())
            if origin.scheme != transport:
                raise ValueError(f"Allowed origins must use {transport.upper()}")
            if (
                not origin.hostname
                or origin.username is not None
                or origin.password is not None
                or origin.path not in ("", "/")
                or origin.query
                or origin.fragment
            ):
                raise ValueError("Allowed origins must not contain credentials, paths or queries")
            host = f"[{origin.hostname}]" if ":" in origin.hostname else origin.hostname
            if origin.port is not None and origin.port != (443 if secure else 80):
                host += f":{origin.port}"
            origins.append(f"{transport}://{host}")
        if not hosts or not origins or "*" in hosts:
            raise ValueError("Set explicit APP_ALLOWED_HOSTS and APP_ALLOWED_ORIGINS")
        cert = key = None
        if secure:
            cert, key = path("APP_TLS_CERT_FILE"), path("APP_TLS_KEY_FILE")
            if cert is None or key is None or not cert.is_file() or not key.is_file():
                raise ValueError("Readable APP_TLS_CERT_FILE and APP_TLS_KEY_FILE are required")
        return cls(
            database=Path(os.getenv("APP_DATABASE_PATH", "/data/energy.sqlite3")),
            password_file=path("APP_ADMIN_PASSWORD_FILE"),
            certificate=cert,
            private_key=key,
            allowed_hosts=hosts,
            allowed_origins=tuple(origins),
            secure=secure,
            auth_required=auth_required == "true",
        )
