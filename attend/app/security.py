import hashlib
import hmac
import secrets
import time
from collections import defaultdict

_ITER = 200_000


def hash_password(pw: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), _ITER)
    return f"pbkdf2${_ITER}${salt}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, it, salt, hexdk = stored.split("$")
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), int(it))
    return hmac.compare_digest(dk.hex(), hexdk)


class LoginLimiter:
    """5 failed attempts per phone in 15 minutes locks that phone out for the window."""

    def __init__(self, max_fail: int = 5, window: int = 900):
        self.max_fail, self.window = max_fail, window
        self.fails: dict[str, list[float]] = defaultdict(list)

    def blocked(self, key: str) -> bool:
        now = time.time()
        self.fails[key] = [t for t in self.fails[key] if now - t < self.window]
        return len(self.fails[key]) >= self.max_fail

    def fail(self, key: str) -> None:
        self.fails[key].append(time.time())

    def reset(self, key: str) -> None:
        self.fails.pop(key, None)


limiter = LoginLimiter()
