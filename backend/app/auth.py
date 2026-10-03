import asyncio
import hashlib
import secrets
import time
from collections import OrderedDict, deque
from dataclasses import dataclass

from app.db.store import Store

COOKIE = "energy_session"


def hash_password(password: str) -> str:
    if not 12 <= len(password) <= 1024:
        raise ValueError("Administrator password must contain 12-1024 characters")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600_000)
    return f"pbkdf2_sha256$600000${salt}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    algorithm, count, salt, expected = encoded.split("$")
    if algorithm != "pbkdf2_sha256" or int(count) < 600_000:
        raise ValueError("Unsupported stored password hash")
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(count))
    return secrets.compare_digest(actual.hex(), expected)


@dataclass
class Session:
    digest: str
    csrf: str
    expires: float


class Auth:
    def __init__(self, store: Store, password_hash: str) -> None:
        self.store, self.password_hash = store, password_hash
        self.sessions: OrderedDict[str, Session] = OrderedDict()
        self.attempts: OrderedDict[str, deque[float]] = OrderedDict()
        self.global_attempts: deque[float] = deque()
        self.verifying = asyncio.Lock()

    def allowed(self, address: str) -> bool:
        now = time.monotonic()
        attempts = self.attempts.setdefault(address, deque())
        self.attempts.move_to_end(address)
        while len(self.attempts) > 256:
            self.attempts.popitem(last=False)
        for queue in (attempts, self.global_attempts):
            while queue and queue[0] < now - 60:
                queue.popleft()
        if len(attempts) >= 5 or len(self.global_attempts) >= 20:
            return False
        attempts.append(now)
        self.global_attempts.append(now)
        return True

    async def login(self, password: str) -> tuple[str, Session] | None:
        async with self.verifying:
            encoded = self.password_hash
            valid = await asyncio.to_thread(verify_password, password, encoded)
        if not valid or encoded != self.password_hash:
            return None
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        session = Session(digest, secrets.token_urlsafe(32), time.monotonic() + 12 * 3600)
        self.sessions[digest] = session
        while len(self.sessions) > 32:
            self.sessions.popitem(last=False)
        return token, session

    def session(self, token: str | None) -> Session | None:
        if token is None or len(token) > 128:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        session = self.sessions.get(digest)
        if session and session.expires <= time.monotonic():
            self.sessions.pop(digest, None)
            return None
        return session

    async def watch_password(self) -> None:
        while True:
            encoded = await self.store.password_hash()
            if encoded is None:
                raise RuntimeError("Administrator hash disappeared")
            if self.password_hash != encoded:
                self.password_hash = encoded
                self.sessions.clear()
            await asyncio.sleep(15)
