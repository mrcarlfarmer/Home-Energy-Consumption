import asyncio
import hashlib
import secrets
import time
from collections import OrderedDict, deque
from dataclasses import dataclass

from app.db.store import MAX_SESSIONS, Store
from app.models import now_us

COOKIE = "energy_session"
SESSION_SECONDS = 12 * 3600


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

    async def restore_sessions(self) -> None:
        self.sessions = OrderedDict(
            (digest, Session(digest, csrf, expires / 1_000_000))
            for digest, csrf, expires in await self.store.load_sessions()
        )

    async def refresh_password(self) -> None:
        encoded = await self.store.password_hash()
        if encoded is None:
            raise RuntimeError("Administrator hash disappeared")
        if self.password_hash != encoded:
            self.password_hash = encoded
            self.sessions.clear()

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
            await self.refresh_password()
            encoded = self.password_hash
            valid = await asyncio.to_thread(verify_password, password, encoded)
            if not valid or encoded != self.password_hash:
                return None
            token = secrets.token_urlsafe(32)
            digest = hashlib.sha256(token.encode()).hexdigest()
            expires = now_us() + SESSION_SECONDS * 1_000_000
            session = Session(digest, secrets.token_urlsafe(32), expires / 1_000_000)
            if not await self.store.save_session(digest, session.csrf, expires, encoded):
                return None
            if encoded != self.password_hash:
                return None
            for key, existing in list(self.sessions.items()):
                if existing.expires <= time.time():
                    self.sessions.pop(key, None)
            self.sessions[digest] = session
            while len(self.sessions) > MAX_SESSIONS:
                self.sessions.popitem(last=False)
            return token, session

    async def logout(self, session: Session) -> None:
        await self.store.delete_session(session.digest)
        self.sessions.pop(session.digest, None)

    def session(self, token: str | None) -> Session | None:
        if token is None or len(token) > 128:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        session = self.sessions.get(digest)
        if session and session.expires <= time.time():
            self.sessions.pop(digest, None)
            return None
        return session

    async def watch_password(self) -> None:
        while True:
            await self.refresh_password()
            await asyncio.sleep(15)
