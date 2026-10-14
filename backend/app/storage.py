import os
from dataclasses import dataclass

DAY_US = 86_400_000_000


class StoragePressure(Exception):
    pass


@dataclass(frozen=True)
class StoragePolicy:
    budget_bytes: int = 2 * 1024**3
    reserve_bytes: int = 256 * 1024**2
    raw_days: int = 30
    five_minute_days: int = 396
    daily_days: int = 3653
    recovery_days: int = 1
    require_bounded_filesystem: bool = False

    def __post_init__(self) -> None:
        if self.budget_bytes < 8 * 1024**2 or self.reserve_bytes < 0:
            raise ValueError("Storage budget must be >=8 MiB and free-space reserve nonnegative")
        if not 1 <= self.recovery_days < self.raw_days < self.five_minute_days < self.daily_days:
            raise ValueError(
                "Require 1 <= recovery days < raw days < five-minute days < daily days"
            )

    @classmethod
    def from_env(cls) -> "StoragePolicy":
        bounded = os.getenv("APP_REQUIRE_BOUNDED_FILESYSTEM", "false")
        if bounded not in ("true", "false"):
            raise ValueError("APP_REQUIRE_BOUNDED_FILESYSTEM must be true or false")
        return cls(
            budget_bytes=int(os.getenv("APP_STORAGE_BUDGET_BYTES", str(2 * 1024**3))),
            reserve_bytes=int(os.getenv("APP_STORAGE_RESERVE_BYTES", str(256 * 1024**2))),
            raw_days=int(os.getenv("APP_RAW_RETENTION_DAYS", "30")),
            five_minute_days=int(os.getenv("APP_5M_RETENTION_DAYS", "396")),
            daily_days=int(os.getenv("APP_DAILY_RETENTION_DAYS", "3653")),
            recovery_days=int(os.getenv("APP_RECOVERY_LOOKBACK_DAYS", "1")),
            require_bounded_filesystem=bounded == "true",
        )
