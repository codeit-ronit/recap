"""A minimal, synchronous Redis client: the RESP2 protocol over one TCP connection.

RECAP's job queue needs about twenty Redis commands, so this small client replaces a
dependency. It speaks the wire protocol any Redis 6.2+ server speaks (BLMOVE needs 6.2):

    command  ->  *<n>\\r\\n  $<len>\\r\\n<arg>\\r\\n ...        (an array of bulk strings)
    reply    <-  +simple  -error  :integer  $bulk  *array      ($-1 and *-1 are null)

One connection per client, guarded by a lock, so a client can be shared by threads (the API
calls it from its worker threads). A blocking command (BLMOVE) holds its connection while it
waits, so a worker gives its claim loop a client of its own. A broken connection is closed and
opened again on the next command; a server that cannot be reached raises RedisUnavailable, so
callers can tell "Redis is down" from "Redis refused this command" (RedisError).

Bulk replies are decoded as UTF-8 text: every value RECAP stores is JSON or an identifier.
"""

from __future__ import annotations

import builtins
import socket
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlparse

Reply = Any  # str | int | None | list[Reply]


class RedisError(Exception):
    """The server answered with an error (a wrong command, a wrong type, a failed EXEC)."""


class RedisUnavailable(RedisError):
    """The server could not be reached, or the connection broke mid-command."""


@dataclass(frozen=True)
class RedisAddress:
    host: str
    port: int
    db: int
    password: str | None

    @classmethod
    def parse(cls, url: str) -> RedisAddress:
        """redis://[:password@]host[:port][/db]; anything else is refused."""
        parsed = urlparse(url)
        if parsed.scheme != "redis":
            raise ValueError(f"not a redis:// URL: {url!r}")
        db = parsed.path.lstrip("/")
        return cls(
            host=parsed.hostname or "127.0.0.1",
            port=parsed.port or 6379,
            db=int(db) if db else 0,
            password=unquote(parsed.password) if parsed.password else None,
        )

    def shown(self) -> str:
        """The address without its password (for logs and the API)."""
        return f"redis://{self.host}:{self.port}/{self.db}"


class Redis:
    """One Redis connection, opened on first use and again after it breaks."""

    def __init__(self, url: str, *, timeout: float = 5.0) -> None:
        self.address = RedisAddress.parse(url)
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._buffer = b""
        self._lock = threading.Lock()

    # --- commands ---------------------------------------------------------------------------

    def execute(self, *args: Any, timeout: float | None = None) -> Reply:
        """Send one command and return its reply; a refusal raises RedisError. `timeout`
        overrides the socket timeout (a blocking command waits longer than the server's own
        timeout)."""
        reply = self.pipeline([args], timeout=timeout)[0]
        if isinstance(reply, RedisError):
            raise reply
        return reply

    def pipeline(
        self, commands: Sequence[Sequence[Any]], *, timeout: float | None = None
    ) -> list[Reply]:
        """Send several commands in one write and read their replies in order. A command the
        server refuses comes back as a RedisError instance in the list, not raised."""
        payload = b"".join(_encode(command) for command in commands)
        with self._lock:
            sock = self._connected()
            try:
                sock.settimeout(timeout if timeout is not None else self.timeout)
                sock.sendall(payload)
                return [self._read_reply(sock, raise_errors=False) for _ in commands]
            except (OSError, ConnectionError) as exc:
                self._close()
                raise RedisUnavailable(f"{self.address.shown()}: {exc}") from None
            finally:
                if self._sock is not None:
                    self._sock.settimeout(self.timeout)

    def transaction(self, commands: Sequence[Sequence[Any]]) -> list[Reply]:
        """Run commands atomically (MULTI ... EXEC): no other client's command runs between
        them. A command refused while queued aborts the whole transaction; one that fails as
        it runs (a wrong type) does not undo the others, as in Redis itself. Either raises."""
        replies = self.pipeline([("MULTI",), *commands, ("EXEC",)])
        for reply in replies[: len(commands) + 1]:
            if isinstance(reply, RedisError):
                raise reply
        result = replies[-1]
        if isinstance(result, RedisError):
            raise result
        if result is None:
            raise RedisError("the transaction was aborted")
        for reply in result:
            if isinstance(reply, RedisError):
                raise reply
        return list(result)

    def close(self) -> None:
        with self._lock:
            self._close()

    # --- the commands RECAP uses, typed -----------------------------------------------------

    def ping(self) -> bool:
        return self.execute("PING") == "PONG"

    def get(self, key: str) -> str | None:
        return _text(self.execute("GET", key))

    def set(
        self, key: str, value: str, *, nx: bool = False, xx: bool = False, px: int | None = None
    ) -> bool:
        args: list[Any] = ["SET", key, value]
        if px is not None:
            args += ["PX", px]
        if nx:
            args.append("NX")
        if xx:
            args.append("XX")
        return self.execute(*args) == "OK"

    def delete(self, *keys: str) -> int:
        return int(self.execute("DEL", *keys)) if keys else 0

    def hset(self, key: str, mapping: dict[str, Any]) -> int:
        flat = [item for pair in mapping.items() for item in (pair[0], _arg(pair[1]))]
        return int(self.execute("HSET", key, *flat))

    def hget(self, key: str, field: str) -> str | None:
        return _text(self.execute("HGET", key, field))

    def hgetall(self, key: str) -> dict[str, str]:
        flat = self.execute("HGETALL", key) or []
        return {str(flat[i]): str(flat[i + 1]) for i in range(0, len(flat), 2)}

    def hincrby(self, key: str, field: str, amount: int = 1) -> int:
        return int(self.execute("HINCRBY", key, field, amount))

    def lpush(self, key: str, *values: str) -> int:
        return int(self.execute("LPUSH", key, *values))

    def rpush(self, key: str, *values: str) -> int:
        return int(self.execute("RPUSH", key, *values))

    def llen(self, key: str) -> int:
        return int(self.execute("LLEN", key))

    def lrange(self, key: str, start: int, stop: int) -> list[str]:
        return [str(item) for item in (self.execute("LRANGE", key, start, stop) or [])]

    def lrem(self, key: str, count: int, value: str) -> int:
        return int(self.execute("LREM", key, count, value))

    def blmove(self, source: str, destination: str, timeout: float) -> str | None:
        """Atomically move the oldest item of `source` (its right end) to `destination`,
        waiting up to `timeout` seconds; None when nothing came."""
        reply = self.execute(
            "BLMOVE",
            source,
            destination,
            "RIGHT",
            "LEFT",
            f"{timeout:g}",
            timeout=timeout + self.timeout,
        )
        return _text(reply)

    def sadd(self, key: str, *members: str) -> int:
        return int(self.execute("SADD", key, *members))

    def srem(self, key: str, *members: str) -> int:
        return int(self.execute("SREM", key, *members))

    def smembers(self, key: str) -> builtins.set[str]:  # `set` is a method of this class
        return {str(item) for item in (self.execute("SMEMBERS", key) or [])}

    def exists(self, key: str) -> bool:
        return int(self.execute("EXISTS", key)) > 0

    # --- the connection ---------------------------------------------------------------------

    def _connected(self) -> socket.socket:
        if self._sock is not None:
            return self._sock
        try:
            sock = socket.create_connection(
                (self.address.host, self.address.port), timeout=self.timeout
            )
        except OSError as exc:
            raise RedisUnavailable(f"{self.address.shown()}: {exc}") from None
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock, self._buffer = sock, b""
        try:
            if self.address.password is not None:
                sock.sendall(_encode(("AUTH", self.address.password)))
                self._read_reply(sock, raise_errors=True)
            if self.address.db:
                sock.sendall(_encode(("SELECT", self.address.db)))
                self._read_reply(sock, raise_errors=True)
        except (OSError, ConnectionError) as exc:
            self._close()
            raise RedisUnavailable(f"{self.address.shown()}: {exc}") from None
        except RedisError:
            self._close()
            raise
        return sock

    def _close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock, self._buffer = None, b""

    def _line(self, sock: socket.socket) -> bytes:
        while b"\r\n" not in self._buffer:
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError("the server closed the connection")
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\r\n", 1)
        return line

    def _exactly(self, sock: socket.socket, size: int) -> bytes:
        while len(self._buffer) < size + 2:
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError("the server closed the connection")
            self._buffer += chunk
        data, self._buffer = self._buffer[:size], self._buffer[size + 2 :]
        return data

    def _read_reply(self, sock: socket.socket, *, raise_errors: bool) -> Reply:
        line = self._line(sock)
        kind, rest = line[:1], line[1:]
        if kind == b"+":
            return rest.decode()
        if kind == b"-":
            error = RedisError(rest.decode())
            if raise_errors:
                raise error
            return error
        if kind == b":":
            return int(rest)
        if kind == b"$":
            size = int(rest)
            return None if size < 0 else self._exactly(sock, size).decode("utf-8")
        if kind == b"*":
            count = int(rest)
            if count < 0:
                return None
            return [self._read_reply(sock, raise_errors=False) for _ in range(count)]
        raise ConnectionError(f"unexpected reply type {kind!r}")


def _arg(value: Any) -> str:
    return value if isinstance(value, str) else str(value)


def _encode(command: Sequence[Any]) -> bytes:
    parts = [f"*{len(command)}\r\n".encode()]
    for item in command:
        data = item if isinstance(item, bytes) else _arg(item).encode("utf-8")
        parts.append(b"$%d\r\n%s\r\n" % (len(data), data))
    return b"".join(parts)


def _text(reply: Reply) -> str | None:
    return None if reply is None else str(reply)
