import re
import sqlite3
import time
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

random.seed(42)

WRITE_PREFIXES = frozenset({
    "INSERT", "UPDATE", "DELETE", "MERGE",
    "CREATE", "DROP", "ALTER", "TRUNCATE",
    "GRANT", "REVOKE", "BEGIN", "START", "COMMIT", "ROLLBACK",
})

READ_PREFIXES = frozenset({
    "SELECT", "WITH", "EXPLAIN", "SHOW",
})

LOCKING_PATTERN = re.compile(r"\bFOR\s+(UPDATE|SHARE|NO\s+KEY\s+UPDATE|KEY\s+SHARE)\b", re.IGNORECASE)


@dataclass
class ReplicationLag:
    lag_seconds: float
    lag_bytes: int


@dataclass
class NodeStats:
    name: str
    role: str
    query_count: int = 0
    last_lag_seconds: float = 0.0


class FakePostgres:
    def __init__(self, name: str, role: str = "standby"):
        self.name = name
        self.role = role
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, name TEXT, email TEXT)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, user_id INTEGER, amount REAL)")
        self.lag_seconds = 0.0
        self.lag_bytes = 0
        self.stats = NodeStats(name=name, role=role)

    def execute(self, sql: str, params: tuple = ()) -> list[tuple]:
        self.stats.query_count += 1
        cur = self.conn.cursor()
        exec_sql = re.sub(r"\s+FOR\s+(UPDATE|SHARE|NO\s+KEY\s+UPDATE|KEY\s+SHARE)(\s+OF\s+\w+)?(\s+NOWAIT)?",
                          "", sql, flags=re.IGNORECASE)
        cur.execute(exec_sql, params)
        try:
            return cur.fetchall()
        except sqlite3.ProgrammingError:
            return []

    def commit(self) -> None:
        self.conn.commit()

    def set_lag(self, seconds: float, bytes_: int) -> None:
        self.lag_seconds = seconds
        self.lag_bytes = bytes_
        self.stats.last_lag_seconds = seconds

    def sync_from(self, primary: "FakePostgres") -> None:
        for table in ("users", "orders"):
            rows = primary.conn.execute(f"SELECT * FROM {table}").fetchall()
            self.conn.execute(f"DELETE FROM {table}")
            if rows:
                placeholders = ",".join("?" * len(rows[0]))
                self.conn.executemany(
                    f"INSERT INTO {table} VALUES ({placeholders})", rows
                )
        self.conn.commit()
        self.set_lag(0.0, 0)


class ReadWriteRouter:
    def __init__(
        self,
        primary: FakePostgres,
        standbys: list[FakePostgres],
        max_lag_seconds: float = 1.0,
        max_lag_bytes: int = 1024 * 1024,
        session_sticky_seconds: float = 0.0,
    ):
        self.primary = primary
        self.standbys = standbys
        self.max_lag_seconds = max_lag_seconds
        self.max_lag_bytes = max_lag_bytes
        self.session_sticky_seconds = session_sticky_seconds
        self._standby_idx = 0
        self._last_write_time: Optional[float] = None
        self._in_explicit_txn = False

    def _classify(self, sql: str) -> str:
        stripped = sql.strip()
        if not stripped:
            return "primary"
        first_word = stripped.split()[0].upper().lstrip("(")

        if first_word in ("BEGIN", "START"):
            self._in_explicit_txn = True
            return "primary"
        if first_word in ("COMMIT", "ROLLBACK"):
            self._in_explicit_txn = False
            return "primary"
        if self._in_explicit_txn:
            return "primary"
        if LOCKING_PATTERN.search(stripped):
            return "primary"
        if first_word in WRITE_PREFIXES:
            return "primary"
        if first_word in READ_PREFIXES:
            return "standby"
        return "primary"

    def _is_sticky(self) -> bool:
        if self._last_write_time is None:
            return False
        return (time.time() - self._last_write_time) < self.session_sticky_seconds

    def _pick_standby(self) -> FakePostgres:
        candidates = [
            s for s in self.standbys
            if s.lag_seconds <= self.max_lag_seconds
            and s.lag_bytes <= self.max_lag_bytes
        ]
        if not candidates:
            print(f"  [警告] 所有备库延迟超阈值，回退到主库")
            return self.primary
        standby = candidates[self._standby_idx % len(candidates)]
        self._standby_idx += 1
        return standby

    def route(self, sql: str) -> FakePostgres:
        target = self._classify(sql)
        if target == "primary":
            self._last_write_time = time.time()
            return self.primary
        if self._is_sticky():
            return self.primary
        return self._pick_standby()

    def execute(self, sql: str, params: tuple = ()) -> list[tuple]:
        node = self.route(sql)
        result = node.execute(sql, params)
        if node.role == "primary" and sql.strip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            node.commit()
        tag = "主库" if node.role == "primary" else f"备库({node.name})"
        print(f"  [{tag}] {sql.strip()[:60]}")
        return result

    def stats(self) -> None:
        print("\n路由统计：")
        print(f"  主库查询数: {self.primary.stats.query_count}")
        for s in self.standbys:
            print(f"  备库 {s.name} 查询数: {s.stats.query_count}")


def demo_basic_routing() -> None:
    print("=" * 60)
    print("演示 1：基础读写分离路由")
    print("=" * 60)

    primary = FakePostgres("primary", "primary")
    standby1 = FakePostgres("standby1", "standby")
    standby2 = FakePostgres("standby2", "standby")

    router = ReadWriteRouter(primary, [standby1, standby2])

    router.execute("INSERT INTO users (id, name, email) VALUES (1, 'Alice', 'a@x.com')")
    router.execute("INSERT INTO users (id, name, email) VALUES (2, 'Bob', 'b@x.com')")
    router.execute("UPDATE users SET email = 'alice@x.com' WHERE id = 1")

    for s in (standby1, standby2):
        s.sync_from(primary)

    router.execute("SELECT * FROM users WHERE id = 1")
    router.execute("SELECT count(*) FROM users")
    router.execute("SELECT * FROM users WHERE id = 2")

    router.execute("DELETE FROM users WHERE id = 2")
    primary.commit()
    for s in (standby1, standby2):
        s.sync_from(primary)

    router.execute("SELECT count(*) FROM users")
    router.stats()


def demo_lag_fallback() -> None:
    print("\n" + "=" * 60)
    print("演示 2：复制延迟超阈值回退主库")
    print("=" * 60)

    primary = FakePostgres("primary", "primary")
    standby = FakePostgres("standby1", "standby")
    standby.sync_from(primary)

    router = ReadWriteRouter(primary, [standby], max_lag_seconds=0.5)

    router.execute("INSERT INTO users (id, name, email) VALUES (1, 'Alice', 'a@x.com')")

    print("\n模拟备库延迟 2 秒（超过阈值 0.5s）：")
    standby.set_lag(2.0, 5 * 1024 * 1024)
    router.execute("SELECT * FROM users WHERE id = 1")

    print("\n模拟备库恢复正常：")
    standby.sync_from(primary)
    router.execute("SELECT * FROM users WHERE id = 1")

    router.stats()


def demo_session_sticky() -> None:
    print("\n" + "=" * 60)
    print("演示 3：会话粘滞（写后 5 秒内读走主库）")
    print("=" * 60)

    primary = FakePostgres("primary", "primary")
    standby = FakePostgres("standby1", "standby")
    standby.sync_from(primary)

    router = ReadWriteRouter(
        primary, [standby],
        session_sticky_seconds=5.0,
    )

    router.execute("INSERT INTO users (id, name, email) VALUES (1, 'Alice', 'a@x.com')")

    print("\n写后立即读（应走主库）：")
    router.execute("SELECT * FROM users WHERE id = 1")

    print("\n等待 6 秒后读（应走备库）：")
    time.sleep(6)
    standby.sync_from(primary)
    router.execute("SELECT * FROM users WHERE id = 1")

    router.stats()


def demo_locking_query() -> None:
    print("\n" + "=" * 60)
    print("演示 4：SELECT FOR UPDATE 走主库")
    print("=" * 60)

    primary = FakePostgres("primary", "primary")
    standby = FakePostgres("standby1", "standby")
    standby.sync_from(primary)

    router = ReadWriteRouter(primary, [standby])

    router.execute("INSERT INTO users (id, name, email) VALUES (1, 'Alice', 'a@x.com')")
    primary.commit()
    standby.sync_from(primary)

    router.execute("SELECT * FROM users WHERE id = 1 FOR UPDATE")
    router.execute("SELECT * FROM users WHERE id = 1")

    router.stats()


def demo_load_balance() -> None:
    print("\n" + "=" * 60)
    print("演示 5：多备库负载均衡")
    print("=" * 60)

    primary = FakePostgres("primary", "primary")
    standbys = [FakePostgres(f"standby{i}", "standby") for i in range(1, 4)]
    for s in standbys:
        s.sync_from(primary)

    router = ReadWriteRouter(primary, standbys)

    router.execute("INSERT INTO users (id, name, email) VALUES (1, 'Alice', 'a@x.com')")
    primary.commit()
    for s in standbys:
        s.sync_from(primary)

    for _ in range(9):
        router.execute("SELECT count(*) FROM users")

    router.stats()


def main() -> None:
    demo_basic_routing()
    demo_lag_fallback()
    demo_session_sticky()
    demo_locking_query()
    demo_load_balance()
    print("\n所有演示完成。")


if __name__ == "__main__":
    main()