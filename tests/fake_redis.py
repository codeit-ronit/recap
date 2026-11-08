"""A small in-memory Redis stand-in for tests: the RESP2 protocol over TCP, on a free port.

It implements exactly the commands RECAP's job queue uses (recap/workers/queue.py), with
Redis's semantics for each: strings with PX expiry and NX/XX, hashes, lists (BLMOVE blocks
until an item arrives or its timeout ends), sets, MULTI/EXEC applied atomically, and errors as
RESP errors. Because it is a real TCP server, worker processes started by a test share it with
the test process and with each other, so a crash test can SIGKILL a worker and start another.

It is no Redis replacement: no persistence, no Lua, one database. The real Redis integration
test (tests/integration/test_workers_redis.py) runs only when RECAP_TEST_REDIS_URL points at a
real server.
"""

from __future__ import annotations

import fnmatch
import socket
import socketserver
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

WRONGTYPE = "WRONGTYPE Operation against a key holding the wrong kind of value"


class _Store:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.expires: dict[str, float] = {}
        self.lock = threading.Condition()

    def alive(self, key: str) -> bool:
        deadline = self.expires.get(key)
        if deadline is not None and time.monotonic() >= deadline:
            self.data.pop(key, None)
            self.expires.pop(key, None)
        return key in self.data

    def get(self, key: str, kind: type) -> Any:
        if not self.alive(key):
            return None
        value = self.data[key]
        if not isinstance(value, kind):
            raise _Error(WRONGTYPE)
        return value


class _Error(Exception):
    pass


def _int(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise _Error("ERR value is not an integer or out of range") from None


class _Handler(socketserver.StreamRequestHandler):
    server: FakeRedisServer

    def setup(self) -> None:
        super().setup()
        with self.server.store.lock:
            self.server.connections.add(self.connection)

    def finish(self) -> None:
        with self.server.store.lock:
            self.server.connections.discard(self.connection)
        try:
            super().finish()
        except OSError:
            pass  # the connection was cut by stop()

    def handle(self) -> None:
        queued: list[list[str]] | None = None
        while True:
            try:
                command = self._read_command()
            except (ConnectionError, OSError, ValueError):
                return
            if command is None:
                return
            name = command[0].upper()
            if name == "MULTI":
                queued = []
                self._write("+OK")
                continue
            if name == "DISCARD":
                queued = None
                self._write("+OK")
                continue
            if name == "EXEC":
                if queued is None:
                    self._write_error("ERR EXEC without MULTI")
                    continue
                with self.server.store.lock:
                    replies = []
                    for item in queued:
                        try:
                            replies.append(self.server.run(item, blocking=False))
                        except _Error as exc:
                            replies.append(_Error(str(exc)))
                    self.server.store.lock.notify_all()
                queued = None
                self._write_reply(replies)
                continue
            if queued is not None:
                queued.append(command)
                self._write("+QUEUED")
                continue
            try:
                reply = self.server.run(command, blocking=True)
            except _Error as exc:
                self._write_error(str(exc))
                continue
            self._write_reply(reply)

    # --- RESP ---------------------------------------------------------------------------

    def _read_command(self) -> list[str] | None:
        line = self.rfile.readline()
        if not line:
            return None
        if not line.startswith(b"*"):
            raise ValueError("inline commands are not supported")
        count = int(line[1:].strip())
        args = []
        for _ in range(count):
            header = self.rfile.readline()
            size = int(header[1:].strip())
            data = self.rfile.read(size + 2)[:size]
            args.append(data.decode("utf-8"))
        return args

    def _write(self, text: str) -> None:
        self.wfile.write(text.encode() + b"\r\n")

    def _write_error(self, message: str) -> None:
        self._write(f"-{message}")

    def _write_reply(self, reply: Any) -> None:
        self.wfile.write(_encode(reply))


def _encode(reply: Any) -> bytes:
    if reply is None:
        return b"$-1\r\n"
    if isinstance(reply, _Error):
        return f"-{reply}\r\n".encode()
    if isinstance(reply, _Simple):
        return f"+{reply.text}\r\n".encode()
    if isinstance(reply, bool):
        return f":{int(reply)}\r\n".encode()
    if isinstance(reply, int):
        return f":{reply}\r\n".encode()
    if isinstance(reply, str):
        data = reply.encode("utf-8")
        return b"$%d\r\n%s\r\n" % (len(data), data)
    if isinstance(reply, list):
        return b"*%d\r\n" % len(reply) + b"".join(_encode(item) for item in reply)
    raise TypeError(f"cannot encode {reply!r}")


class _Simple:
    def __init__(self, text: str) -> None:
        self.text = text


OK = _Simple("OK")


class FakeRedisServer(socketserver.ThreadingTCPServer):
    """Start with `with FakeRedisServer.running() as url: ...`, or start()/stop().

    An outage: stop() cuts every connection (clients see a broken connection, as when Redis
    goes away); restarted() serves the same data on the same port again (Redis restarting from
    its append-only file, or a network partition healing).
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port: int = 0, store: _Store | None = None) -> None:
        super().__init__(("127.0.0.1", port), _Handler)
        self.store = store or _Store()
        self.connections: set[socket.socket] = set()
        self.commands: list[str] = []  # every command name received, in order (tests read it)
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"redis://127.0.0.1:{self.server_address[1]}/0"

    def start(self) -> FakeRedisServer:
        self._thread = threading.Thread(target=self.serve_forever, name="fake-redis", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
        with self.store.lock:
            cut = list(self.connections)
            self.connections.clear()
            self.store.lock.notify_all()
        for connection in cut:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def restarted(self) -> FakeRedisServer:
        """A new server on this one's port, with this one's data (call stop() first)."""
        return FakeRedisServer(self.server_address[1], self.store).start()

    @classmethod
    @contextmanager
    def running(cls) -> Iterator[FakeRedisServer]:
        server = cls().start()
        try:
            yield server
        finally:
            server.stop()

    # --- commands --------------------------------------------------------------------------

    def run(self, command: list[str], *, blocking: bool) -> Any:
        name, args = command[0].upper(), command[1:]
        self.commands.append(name)
        store = self.store
        if name == "BLMOVE" and blocking:
            return self._blmove(args)
        with store.lock:
            reply = self._apply(name, args)
            store.lock.notify_all()
            return reply

    def _blmove(self, args: list[str]) -> Any:
        source, timeout = args[0], float(args[4])
        deadline = time.monotonic() + timeout
        store = self.store
        with store.lock:
            while True:
                items = store.get(source, list)
                if items:
                    reply = self._apply("LMOVE", args[:4])
                    store.lock.notify_all()
                    return reply
                remaining = deadline - time.monotonic()
                if timeout and remaining <= 0:
                    return None
                store.lock.wait(timeout=min(remaining, 0.05) if timeout else 0.05)

    def _apply(self, name: str, args: list[str]) -> Any:  # one branch per command
        store = self.store
        if name == "PING":
            return _Simple("PONG")
        if name in ("AUTH", "SELECT"):
            return OK
        if name == "FLUSHALL":
            store.data.clear()
            store.expires.clear()
            return OK
        if name == "GET":
            return store.get(args[0], str)
        if name == "SET":
            key, value, options = args[0], args[1], [a.upper() for a in args[2:]]
            exists = store.alive(key)
            if "NX" in options and exists:
                return None
            if "XX" in options and not exists:
                return None
            store.data[key] = value
            store.expires.pop(key, None)
            if "PX" in options:
                store.expires[key] = (
                    time.monotonic() + _int(args[2 + options.index("PX") + 1]) / 1000
                )
            if "EX" in options:
                store.expires[key] = time.monotonic() + _int(args[2 + options.index("EX") + 1])
            return OK
        if name == "DEL":
            removed = 0
            for key in args:
                if store.alive(key):
                    del store.data[key]
                    store.expires.pop(key, None)
                    removed += 1
            return removed
        if name == "EXISTS":
            return sum(1 for key in args if store.alive(key))
        if name in ("INCR", "INCRBY"):
            current = store.get(args[0], str) or "0"
            number = _int(current) + (_int(args[1]) if name == "INCRBY" else 1)
            store.data[args[0]] = str(number)
            return number
        if name in ("PEXPIRE", "EXPIRE"):
            if not store.alive(args[0]):
                return 0
            scale = 1000 if name == "PEXPIRE" else 1
            store.expires[args[0]] = time.monotonic() + _int(args[1]) / scale
            return 1
        if name == "PTTL":
            if not store.alive(args[0]):
                return -2
            deadline = store.expires.get(args[0])
            return -1 if deadline is None else int((deadline - time.monotonic()) * 1000)
        if name == "HSET":
            table = store.get(args[0], dict)
            if table is None:
                table = store.data[args[0]] = {}
            added = 0
            for i in range(1, len(args), 2):
                added += args[i] not in table
                table[args[i]] = args[i + 1]
            return added
        if name == "HGET":
            table = store.get(args[0], dict) or {}
            return table.get(args[1])
        if name == "HGETALL":
            table = store.get(args[0], dict) or {}
            return [item for pair in table.items() for item in pair]
        if name == "HDEL":
            table = store.get(args[0], dict) or {}
            return sum(1 for field in args[1:] if table.pop(field, None) is not None)
        if name == "HINCRBY":
            table = store.get(args[0], dict)
            if table is None:
                table = store.data[args[0]] = {}
            number = _int(table.get(args[1], "0")) + _int(args[2])
            table[args[1]] = str(number)
            return number
        if name in ("LPUSH", "RPUSH"):
            items = store.get(args[0], list)
            if items is None:
                items = store.data[args[0]] = []
            for value in args[1:]:
                if name == "LPUSH":
                    items.insert(0, value)
                else:
                    items.append(value)
            return len(items)
        if name == "LLEN":
            return len(store.get(args[0], list) or [])
        if name == "LRANGE":
            items = store.get(args[0], list) or []
            start, stop = _int(args[1]), _int(args[2])
            stop = len(items) - 1 if stop == -1 else stop
            return items[start : stop + 1]
        if name == "LTRIM":
            items = store.get(args[0], list)
            if items is not None:
                start, stop = _int(args[1]), _int(args[2])
                stop = len(items) - 1 if stop == -1 else stop
                store.data[args[0]] = items[start : stop + 1]
            return OK
        if name == "LREM":
            items = store.get(args[0], list) or []
            count, value = _int(args[1]), args[2]
            removed = 0
            for item in list(items):
                if item == value and (count == 0 or removed < abs(count)):
                    items.remove(item)
                    removed += 1
            return removed
        if name == "LMOVE":
            source, destination = args[0], args[1]
            items = store.get(source, list) or []
            if not items:
                return None
            value = items.pop() if args[2].upper() == "RIGHT" else items.pop(0)
            target = store.get(destination, list)
            if target is None:
                target = store.data[destination] = []
            if args[3].upper() == "LEFT":
                target.insert(0, value)
            else:
                target.append(value)
            return value
        if name == "SADD":
            members = store.get(args[0], set)
            if members is None:
                members = store.data[args[0]] = set()
            added = len(set(args[1:]) - members)
            members.update(args[1:])
            return added
        if name == "SREM":
            members = store.get(args[0], set) or set()
            removed = len(members & set(args[1:]))
            members.difference_update(args[1:])
            return removed
        if name == "SMEMBERS":
            return sorted(store.get(args[0], set) or set())
        if name == "KEYS":
            return sorted(
                key
                for key in list(store.data)
                if store.alive(key) and fnmatch.fnmatch(key, args[0])
            )
        raise _Error(f"ERR unknown command '{name}'")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
