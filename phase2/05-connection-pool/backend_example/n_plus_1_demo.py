"""N+1 查询问题演示：创建博客数据，对比 N+1 / JOIN / eager loading

用 SQLite 演示，零外部依赖。
模拟 ORM 的 lazy loading 行为，量化查询次数和耗时。
"""
import sqlite3
import time
import os
from collections import defaultdict

DB_PATH = "n_plus_1_demo.db"
NUM_USERS = 50
POSTS_PER_USER = 10


def setup_db():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, email TEXT);
        CREATE TABLE posts (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            body TEXT,
            created_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE INDEX idx_posts_user ON posts(user_id);
    """)
    for i in range(1, NUM_USERS + 1):
        conn.execute("INSERT INTO users(id, name, email) VALUES(?, ?, ?)",
                     (i, f"user_{i:04d}", f"user_{i}@example.com"))
    pid = 1
    for uid in range(1, NUM_USERS + 1):
        for j in range(1, POSTS_PER_USER + 1):
            conn.execute(
                "INSERT INTO posts(id, user_id, title, body, created_at) VALUES(?, ?, ?, ?, ?)",
                (pid, uid, f"Post {j} by user_{uid:04d}",
                 f"Body content {j} " * 5, f"2024-01-{j:02d}"))
            pid += 1
    conn.commit()
    conn.close()
    print(f"已创建 {NUM_USERS} 用户 × {POSTS_PER_USER} 帖子 = {NUM_USERS * POSTS_PER_USER} 帖子\n")


class QueryCounter:
    def __init__(self, conn):
        self.conn = conn
        self.count = 0

    def execute(self, sql, params=()):
        self.count += 1
        return self.conn.execute(sql, params)

    def fetchall(self, sql, params=()):
        self.count += 1
        return self.conn.execute(sql, params).fetchall()

    def fetchone(self, sql, params=()):
        self.count += 1
        return self.conn.execute(sql, params).fetchone()


def approach_n_plus_1():
    """模拟 ORM lazy loading：先查用户，再循环查每个用户的帖子"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    counter = QueryCounter(conn)

    t0 = time.time()
    users = counter.fetchall("SELECT id, name FROM users")
    result = []
    for u in users:
        posts = counter.fetchall("SELECT id, title FROM posts WHERE user_id=?", (u["id"],))
        result.append({"user": dict(u), "posts": [dict(p) for p in posts]})
    elapsed = time.time() - t0

    conn.close()
    return {"queries": counter.count, "ms": elapsed * 1000, "users": len(result),
            "expected_queries": 1 + NUM_USERS}


def approach_join():
    """用 LEFT JOIN 一次查出，应用层摊平"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    counter = QueryCounter(conn)

    t0 = time.time()
    rows = counter.fetchall("""
        SELECT u.id AS user_id, u.name AS user_name, u.email,
               p.id AS post_id, p.title AS post_title
        FROM users u
        LEFT JOIN posts p ON p.user_id = u.id
        ORDER BY u.id, p.id
    """)
    users_map = {}
    for r in rows:
        uid = r["user_id"]
        if uid not in users_map:
            users_map[uid] = {"user": {"id": uid, "name": r["user_name"]}, "posts": []}
        if r["post_id"] is not None:
            users_map[uid]["posts"].append({"id": r["post_id"], "title": r["post_title"]})
    result = list(users_map.values())
    elapsed = time.time() - t0

    conn.close()
    return {"queries": counter.count, "ms": elapsed * 1000, "users": len(result),
            "expected_queries": 1}


def approach_two_queries():
    """两次查询 + 应用层组装（模拟 selectinload / prefetch_related）"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    counter = QueryCounter(conn)

    t0 = time.time()
    users = counter.fetchall("SELECT id, name FROM users")
    posts = counter.fetchall("SELECT id, user_id, title FROM posts")
    posts_by_user = defaultdict(list)
    for p in posts:
        posts_by_user[p["user_id"]].append({"id": p["id"], "title": p["title"]})
    result = []
    for u in users:
        result.append({"user": dict(u), "posts": posts_by_user[u["id"]]})
    elapsed = time.time() - t0

    conn.close()
    return {"queries": counter.count, "ms": elapsed * 1000, "users": len(result),
            "expected_queries": 2}


def approach_batch_in():
    """分批 IN 查询（适合 user_id 列表很长时分批）"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    counter = QueryCounter(conn)
    BATCH = 20

    t0 = time.time()
    users = counter.fetchall("SELECT id, name FROM users")
    posts_by_user = defaultdict(list)
    for i in range(0, len(users), BATCH):
        batch = users[i:i + BATCH]
        ids = [u["id"] for u in batch]
        placeholders = ",".join("?" * len(ids))
        posts = counter.fetchall(
            f"SELECT id, user_id, title FROM posts WHERE user_id IN ({placeholders})", ids)
        for p in posts:
            posts_by_user[p["user_id"]].append({"id": p["id"], "title": p["title"]})
    result = [{"user": dict(u), "posts": posts_by_user[u["id"]]} for u in users]
    elapsed = time.time() - t0

    conn.close()
    batches = (len(users) + BATCH - 1) // BATCH
    return {"queries": counter.count, "ms": elapsed * 1000, "users": len(result),
            "expected_queries": 1 + batches}


def run_comparison():
    print("=" * 70)
    print(f"{'方案':<25} {'查询数':>8} {'耗时(ms)':>10} {'理论查询数':>12}")
    print("=" * 70)

    results = []
    for name, fn in [
        ("N+1（lazy loading）", approach_n_plus_1),
        ("JOIN（手动摊平）", approach_join),
        ("两次查询（selectinload）", approach_two_queries),
        ("分批 IN（batch=20）", approach_batch_in),
    ]:
        r = fn()
        results.append((name, r))
        print(f"{name:<25} {r['queries']:>8} {r['ms']:>10.2f} {r['expected_queries']:>12}")

    print("=" * 70)
    n1 = results[0][1]
    join = results[1][1]
    print(f"\nN+1 查询数 = {n1['queries']}，是 JOIN 的 {n1['queries'] / join['queries']:.0f} 倍")
    print(f"随用户数增长：N+1 是 O(N)，JOIN/两次查询是 O(1)")
    return results


def sweep_user_count():
    """扫描用户数，展示 N+1 的线性增长"""
    print("\n=== N+1 随用户数线性增长 ===")
    print(f"{'用户数':>8} {'N+1 查询数':>12} {'JOIN 查询数':>12} {'N+1 耗时(ms)':>14} {'JOIN 耗时(ms)':>14}")
    print("-" * 70)

    global NUM_USERS, POSTS_PER_USER
    original = (NUM_USERS, POSTS_PER_USER)
    for n in [10, 50, 100, 200, 500]:
        NUM_USERS = n
        setup_db_silent()
        r1 = approach_n_plus_1()
        r2 = approach_join()
        print(f"{n:>8} {r1['queries']:>12} {r2['queries']:>12} "
              f"{r1['ms']:>14.2f} {r2['ms']:>14.2f}")

    NUM_USERS, POSTS_PER_USER = original
    setup_db_silent()


def setup_db_silent():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, email TEXT);
        CREATE TABLE posts (
            id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
            title TEXT NOT NULL, body TEXT, created_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE INDEX idx_posts_user ON posts(user_id);
    """)
    for i in range(1, NUM_USERS + 1):
        conn.execute("INSERT INTO users(id, name, email) VALUES(?, ?, ?)",
                     (i, f"user_{i:04d}", f"user_{i}@example.com"))
    pid = 1
    for uid in range(1, NUM_USERS + 1):
        for j in range(1, POSTS_PER_USER + 1):
            conn.execute(
                "INSERT INTO posts(id, user_id, title, body, created_at) VALUES(?, ?, ?, ?, ?)",
                (pid, uid, f"Post {j}", f"Body {j}", f"2024-01-{j:02d}"))
            pid += 1
    conn.commit()
    conn.close()


def main():
    print("=== N+1 查询问题演示 ===\n")
    setup_db()
    run_comparison()
    sweep_user_count()

    print("\n=== 结论 ===")
    print("1. N+1：1 + N 次查询，随 N 线性增长 → 生产环境必须避免")
    print("2. JOIN：1 次查询，最快，但需要应用层摊平结果")
    print("3. 两次查询（selectinload）：2 次查询，与 N 无关，ORM 推荐方案")
    print("4. 分批 IN：1 + ceil(N/batch) 次查询，适合 N 很大时分批")

    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)


if __name__ == "__main__":
    main()