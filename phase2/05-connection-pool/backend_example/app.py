"""后端示例：连接池 + 路由 + 同步/异步 + 事务边界

用 Python 标准库（http.server + sqlite3 + asyncio）实现，零外部依赖。
代码结构模拟 Flask/FastAPI 的路由风格，便于迁移。
"""
import sqlite3
import json
import time
import queue
import threading
import traceback
from contextlib import contextmanager
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

DB_PATH = "backend_example.db"


class SQLiteConnectionPool:
    def __init__(self, path, pool_size=10, init_sql=None):
        self.path = path
        self.pool_size = pool_size
        self._pool = queue.Queue(maxsize=pool_size)
        self._all_conns = []
        self._checked_out = {}
        self._lock = threading.Lock()
        for _ in range(pool_size):
            conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            if init_sql:
                conn.executescript(init_sql)
            self._pool.put(conn)
            self._all_conns.append(conn)

    def getconn(self, timeout=5):
        conn = self._pool.get(timeout=timeout)
        with self._lock:
            self._checked_out[id(conn)] = (time.time(), traceback.format_stack())
        return conn

    def putconn(self, conn):
        with self._lock:
            self._checked_out.pop(id(conn), None)
        self._pool.put(conn)

    @contextmanager
    def connection(self):
        conn = self.getconn()
        try:
            yield conn

        finally:
            self.putconn(conn)

    @contextmanager
    def transaction(self):
        conn = self.getconn()
        try:
            conn.execute("BEGIN")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            self.putconn(conn)

    def stats(self):
        with self._lock:
            checked_out = len(self._checked_out)
        available = self._pool.qsize()
        return {
            "pool_size": self.pool_size,
            "available": available,
            "checked_out": checked_out,
            "leaked": self.leak_report(silent=True),
        }

    def leak_report(self, threshold=10, silent=False):
        now = time.time()
        leaks = []
        with self._lock:
            for conn_id, (checkout_time, stack) in self._checked_out.items():
                age = now - checkout_time
                if age > threshold:
                    leaks.append({"conn_id": conn_id, "age": age, "stack": stack})
        if not silent and leaks:
            print(f"\n=== 检测到 {len(leaks)} 个疑似泄漏连接 ===")
            for leak in leaks:
                print(f"连接 {leak['conn_id']} 借出 {leak['age']:.1f}s 未还")
                print("借出栈:", "".join(leak["stack"][-5:]))
        return leaks

    def close(self):
        for conn in self._all_conns:
            conn.close()


def init_db(pool):
    with pool.transaction() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
            title TEXT NOT NULL, body TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY, owner TEXT NOT NULL, balance REAL NOT NULL)""")
        if conn.execute("SELECT count(*) FROM users").fetchone()[0] == 0:
            for i in range(1, 21):
                conn.execute("INSERT INTO users(id, name, email) VALUES(?, ?, ?)",
                             (i, f"user_{i}", f"user_{i}@example.com"))
            for i in range(1, 21):
                for j in range(1, 6):
                    conn.execute(
                        "INSERT INTO posts(id, user_id, title, body) VALUES(?, ?, ?, ?)",
                        ((i - 1) * 5 + j, i, f"Post {j} by user_{i}", f"Body {j}"))
            for i in range(1, 11):
                conn.execute("INSERT INTO accounts(id, owner, balance) VALUES(?, ?, ?)",
                             (i, f"account_{i}", 1000.0))


pool = SQLiteConnectionPool(DB_PATH, pool_size=10)
init_db(pool)


def get_users_n_plus_1():
    users = []
    with pool.connection() as conn:
        rows = conn.execute("SELECT id, name FROM users").fetchall()
    for row in rows:
        with pool.connection() as conn:
            count = conn.execute(
                "SELECT count(*) FROM posts WHERE user_id=?", (row["id"],)).fetchone()[0]
        users.append({"id": row["id"], "name": row["name"], "post_count": count})
    return users


def get_users_join():
    with pool.connection() as conn:
        rows = conn.execute("""
            SELECT u.id, u.name, COUNT(p.id) AS post_count
            FROM users u
            LEFT JOIN posts p ON p.user_id = u.id
            GROUP BY u.id, u.name
            ORDER BY u.id
        """).fetchall()
    return [{"id": r["id"], "name": r["name"], "post_count": r["post_count"]} for r in rows]


def get_users_two_queries():
    with pool.connection() as conn:
        users = conn.execute("SELECT id, name FROM users").fetchall()
        counts = conn.execute("""
            SELECT user_id, count(*) AS cnt FROM posts GROUP BY user_id
        """).fetchall()
    count_map = {r["user_id"]: r["cnt"] for r in counts}
    return [{"id": u["id"], "name": u["name"],
             "post_count": count_map.get(u["id"], 0)} for u in users]


def transfer(from_id, to_id, amount):
    with pool.transaction() as conn:
        from_balance = conn.execute(
            "SELECT balance FROM accounts WHERE id=?", (from_id,)).fetchone()[0]
        if from_balance < amount:
            raise ValueError(f"余额不足: {from_balance} < {amount}")
        conn.execute("UPDATE accounts SET balance=balance-? WHERE id=?", (amount, from_id))
        conn.execute("UPDATE accounts SET balance=balance+? WHERE id=?", (amount, to_id))
        return {"from": from_id, "to": to_id, "amount": amount,
                "from_balance": from_balance - amount}


def transfer_wrong(from_id, to_id, amount):
    with pool.connection() as conn:
        conn.execute("UPDATE accounts SET balance=balance-? WHERE id=?", (amount, from_id))
    raise RuntimeError("模拟中途失败：扣款已执行，但加款未执行，且不在事务中！")
    with pool.connection() as conn:
        conn.execute("UPDATE accounts SET balance=balance+? WHERE id=?", (amount, to_id))


class SyncHandler(BaseHTTPRequestHandler):
    routes = {}

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        handler = self.routes.get(path)
        if handler is None:
            self._send(404, {"error": "not found"})
            return
        try:
            result = handler(parse_qs(parsed.query))
            self._send(200, result)
        except Exception as e:
            self._send(500, {"error": str(e)})

    def _send(self, code, body):
        data = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass


SyncHandler.routes = {
    "/users/n+1": lambda q: get_users_n_plus_1(),
    "/users/join": lambda q: get_users_join(),
    "/users/two-query": lambda q: get_users_two_queries(),
    "/pool/stats": lambda q: pool.stats(),
    "/pool/leak-check": lambda q: {"leaks": pool.leak_report(silent=True)},
}


def run_sync_server(port=8000):
    print(f"同步服务启动: http://localhost:{port}")
    print("路由:")
    print("  GET /users/n+1        — N+1 查询（反面教材）")
    print("  GET /users/join       — JOIN 解决 N+1")
    print("  GET /users/two-query  — 两次查询解决 N+1")
    print("  GET /pool/stats       — 连接池状态")
    print("  GET /pool/leak-check  — 泄漏检测")
    server = HTTPServer(("localhost", port), SyncHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n关闭服务...")
        pool.close()


async def async_get_users_join():
    def _query():
        with pool.connection() as conn:
            rows = conn.execute("""
                SELECT u.id, u.name, COUNT(p.id) AS post_count
                FROM users u LEFT JOIN posts p ON p.user_id = u.id
                GROUP BY u.id, u.name ORDER BY u.id
            """).fetchall()
        return [{"id": r["id"], "name": r["name"], "post_count": r["post_count"]}
                for r in rows]
    import asyncio
    return await asyncio.to_thread(_query)


async def async_transfer(from_id, to_id, amount):
    def _transfer():
        return transfer(from_id, to_id, amount)
    import asyncio
    return await asyncio.to_thread(_transfer)


async def run_async_demo():
    import asyncio
    print("\n=== 异步演示 ===")
    tasks = [async_get_users_join() for _ in range(5)]
    results = await asyncio.gather(*tasks)
    print(f"并发 5 个查询，每个返回 {len(results[0])} 个用户")

    write_sem = asyncio.Semaphore(1)
    async def guarded_transfer(from_id, to_id, amount):
        async with write_sem:
            return await async_transfer(from_id, to_id, amount)

    tasks = [guarded_transfer(1, 2, 10) for _ in range(3)]
    results = await asyncio.gather(*tasks)
    print(f"串行 3 笔转账（Semaphore 限制写并发=1），结果: {results[-1]}")


def demo_transaction_boundary():
    print("\n=== 事务边界演示 ===")
    print("\n--- 正确：事务内转账 ---")
    before = transfer(1, 2, 0)
    print(f"转账前 account_1 余额: {before['from_balance'] + 100}")
    result = transfer(1, 2, 100)
    print(f"转账 100: {result}")

    print("\n--- 错误：非事务跨连接转账 ---")
    try:
        transfer_wrong(3, 4, 50)
    except RuntimeError as e:
        print(f"失败: {e}")
        with pool.connection() as conn:
            bal3 = conn.execute("SELECT balance FROM accounts WHERE id=3").fetchone()[0]
            bal4 = conn.execute("SELECT balance FROM accounts WHERE id=4").fetchone()[0]
        print(f"account_3 余额: {bal3}（被扣了 50 但没回滚！）")
        print(f"account_4 余额: {bal4}（没收到钱）")
        print("→ 数据不一致，这就是不用事务的后果")
        with pool.transaction() as conn:
            conn.execute("UPDATE accounts SET balance=1000.0 WHERE id=3")
            conn.execute("UPDATE accounts SET balance=1000.0 WHERE id=4")


def main():
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "server":
        run_sync_server()
        return

    print("=== 连接池后端示例 ===")
    print(f"连接池状态: {pool.stats()}")

    print("\n--- 查询对比 ---")
    t0 = time.time()
    r1 = get_users_n_plus_1()
    t1 = time.time()
    r2 = get_users_join()
    t2 = time.time()
    r3 = get_users_two_queries()
    t3 = time.time()
    print(f"N+1 查询:     {(t1-t0)*1000:.2f}ms, 返回 {len(r1)} 用户")
    print(f"JOIN 查询:    {(t2-t1)*1000:.2f}ms, 返回 {len(r2)} 用户")
    print(f"两次查询:     {(t3-t2)*1000:.2f}ms, 返回 {len(r3)} 用户")

    demo_transaction_boundary()

    import asyncio
    asyncio.run(run_async_demo())

    print(f"\n最终连接池状态: {pool.stats()}")
    pool.close()
    print("\n要启动 HTTP 服务: python app.py server")


if __name__ == "__main__":
    main()