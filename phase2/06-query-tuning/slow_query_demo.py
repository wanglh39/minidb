"""慢查询案例：5 个真实场景的调优全过程（慢→EXPLAIN→优化→快→对比）"""
import sqlite3
import time
import os
import random

DB_PATH = "slow_query_demo.db"


def setup_database():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode = WAL")

    conn.execute("""
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            region TEXT NOT NULL,
            vip INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            amount REAL NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE order_items (
            id INTEGER PRIMARY KEY,
            order_id INTEGER NOT NULL,
            product_name TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            price REAL NOT NULL
        )
    """)

    random.seed(42)
    regions = ['CN', 'US', 'EU', 'JP', 'KR']
    statuses = ['pending', 'paid', 'shipped', 'cancelled']

    customers = [(i, f"customer_{i}", random.choice(regions), random.randint(0, 1)) for i in range(1, 5001)]
    conn.executemany("INSERT INTO customers VALUES (?,?,?,?)", customers)

    orders = []
    for i in range(1, 200001):
        cid = random.randint(1, 5000)
        status = random.choice(statuses)
        amount = round(random.uniform(10, 1000), 2)
        created = f"2024-{random.randint(1,12):02d}-{random.randint(1,28):02d}"
        orders.append((i, cid, status, amount, created))
    conn.executemany("INSERT INTO orders VALUES (?,?,?,?,?)", orders)

    items = []
    for oid in range(1, 200001):
        n_items = random.randint(1, 5)
        for j in range(n_items):
            items.append((
                len(items) + 1, oid,
                f"product_{random.randint(1, 1000)}",
                random.randint(1, 10),
                round(random.uniform(5, 200), 2)
            ))
    conn.executemany("INSERT INTO order_items VALUES (?,?,?,?,?)", items)
    conn.commit()
    conn.close()
    print(f"测试数据：5000 客户, 20万 订单, ~60万 订单明细\n")


def show_plan(conn, sql, label=""):
    if label:
        print(f"  [{label}]")
    rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()
    for r in rows:
        print(f"    {r[3]}")
    print()


def benchmark(conn, sql, params=(), runs=3):
    times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        result = conn.execute(sql, params).fetchall()
        t1 = time.perf_counter()
        times.append((t1 - t0) * 1000)
    avg = sum(times) / len(times)
    return avg, len(result)


def case1_full_table_scan():
    print("=" * 70)
    print("案例1：全表扫描 → 加索引")
    print("=" * 70)
    print("场景：按 customer_id 查询订单，无索引时全表扫描\n")

    conn = sqlite3.connect(DB_PATH)

    print("--- 优化前：无索引 ---")
    show_plan(conn, "SELECT * FROM orders WHERE customer_id = 2500", "优化前计划")
    avg, n = benchmark(conn, "SELECT * FROM orders WHERE customer_id = 2500")
    print(f"  耗时: {avg:.2f}ms, 结果行数: {n}\n")

    print("--- 加索引 ---")
    conn.execute("CREATE INDEX idx_orders_customer ON orders(customer_id)")
    conn.commit()

    print("--- 优化后：有索引 ---")
    show_plan(conn, "SELECT * FROM orders WHERE customer_id = 2500", "优化后计划")
    avg2, n = benchmark(conn, "SELECT * FROM orders WHERE customer_id = 2500")
    print(f"  耗时: {avg2:.3f}ms, 结果行数: {n}\n")

    print(f"  >>> 性能提升: {avg/avg2:.0f} 倍 ({avg:.2f}ms → {avg2:.3f}ms)\n")
    conn.close()


def case2_n_plus_one():
    print("=" * 70)
    print("案例2：N+1 子查询 → 改为 JOIN")
    print("=" * 70)
    print("场景：查每个客户的订单数，N+1 方式 vs JOIN 方式\n")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id)")
    conn.commit()

    print("--- 反模式：N+1 查询（应用层循环）---")
    t0 = time.perf_counter()
    customers = conn.execute("SELECT id, name FROM customers WHERE region = 'CN'").fetchall()
    results_n1 = []
    for cid, cname in customers:
        cnt = conn.execute("SELECT count(*) FROM orders WHERE customer_id = ?", (cid,)).fetchone()[0]
        results_n1.append((cname, cnt))
    t1 = time.perf_counter()
    avg_n1 = (t1 - t0) * 1000
    print(f"  查询次数: {len(customers) + 1} (1 + {len(customers)})")
    print(f"  耗时: {avg_n1:.2f}ms\n")

    print("--- 优化：JOIN + GROUP BY（1 次查询）---")
    sql_join = """
        SELECT c.name, count(o.id) AS cnt
        FROM customers c
        LEFT JOIN orders o ON o.customer_id = c.id
        WHERE c.region = 'CN'
        GROUP BY c.id, c.name
    """
    show_plan(conn, sql_join, "优化后计划")
    avg_join, n = benchmark(conn, sql_join, runs=3)
    print(f"  查询次数: 1")
    print(f"  耗时: {avg_join:.2f}ms, 结果行数: {n}\n")

    print(f"  >>> 性能提升: {avg_n1/avg_join:.0f} 倍 ({avg_n1:.2f}ms → {avg_join:.2f}ms)\n")
    conn.close()


def case3_or_condition():
    print("=" * 70)
    print("案例3：不当的 OR 条件 → 改为 UNION")
    print("=" * 70)
    print("场景：查 customer_id=2500 或 status='pending' 的订单\n")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
    conn.commit()

    print("--- 反模式：OR 条件（可能无法同时利用两个索引）---")
    sql_or = "SELECT * FROM orders WHERE customer_id = 2500 OR status = 'pending'"
    show_plan(conn, sql_or, "OR 计划")
    avg_or, n_or = benchmark(conn, sql_or, runs=3)
    print(f"  耗时: {avg_or:.2f}ms, 结果行数: {n_or}\n")

    print("--- 优化1：UNION（各自用索引，去重）---")
    sql_union = """
        SELECT * FROM orders WHERE customer_id = 2500
        UNION
        SELECT * FROM orders WHERE status = 'pending' AND customer_id != 2500
    """
    show_plan(conn, sql_union, "UNION 计划")
    avg_union, n_union = benchmark(conn, sql_union, runs=3)
    print(f"  耗时: {avg_union:.2f}ms, 结果行数: {n_union}\n")

    print("--- 优化2：UNION ALL（不去重，更快，但需确保无重复）---")
    sql_union_all = """
        SELECT * FROM orders WHERE customer_id = 2500 AND status != 'pending'
        UNION ALL
        SELECT * FROM orders WHERE status = 'pending'
    """
    show_plan(conn, sql_union_all, "UNION ALL 计划")
    avg_ua, n_ua = benchmark(conn, sql_union_all, runs=3)
    print(f"  耗时: {avg_ua:.2f}ms, 结果行数: {n_ua}\n")

    print(f"  >>> OR: {avg_or:.2f}ms | UNION: {avg_union:.2f}ms | UNION ALL: {avg_ua:.2f}ms\n")
    conn.close()


def case4_stale_stats():
    print("=" * 70)
    print("案例4：统计信息过期 → ANALYZE")
    print("=" * 70)
    print("场景：初始小表查询计划，插入大量数据后计划退化，ANALYZE 修正\n")

    conn = sqlite3.connect(DB_PATH)

    conn.execute("DROP INDEX IF EXISTS idx_orders_customer")
    conn.execute("CREATE INDEX idx_orders_customer ON orders(customer_id)")
    conn.commit()

    print("--- 步骤1：初始 ANALYZE ---")
    conn.execute("ANALYZE")
    try:
        stat_before = conn.execute(
            "SELECT stat FROM sqlite_stat1 WHERE tbl='orders' AND idx='idx_orders_customer'"
        ).fetchone()
        print(f"  统计信息: {stat_before[0] if stat_before else 'N/A'}")
    except sqlite3.OperationalError:
        print("  (无统计信息)")

    print("\n--- 步骤2：插入大量新数据（不更新统计）---")
    random.seed(99)
    max_id = conn.execute("SELECT max(id) FROM orders").fetchone()[0]
    new_orders = []
    for i in range(max_id + 1, max_id + 100001):
        new_orders.append((i, random.randint(1, 5000), 'pending',
                          round(random.uniform(10, 1000), 2), '2024-12-31'))
    conn.executemany("INSERT INTO orders VALUES (?,?,?,?,?)", new_orders)
    conn.commit()
    total = conn.execute("SELECT count(*) FROM orders").fetchone()[0]
    print(f"  插入 10万 行，当前总行数: {total}")

    print("\n--- 步骤3：不 ANALYZE 直接查询（统计信息过期）---")
    show_plan(conn, "SELECT count(*) FROM orders WHERE customer_id = 2500", "过期统计计划")
    avg_stale, _ = benchmark(conn, "SELECT count(*) FROM orders WHERE customer_id = 2500", runs=3)
    print(f"  耗时: {avg_stale:.3f}ms")

    print("--- 步骤4：ANALYZE 后查询 ---")
    conn.execute("ANALYZE")
    stat_after = conn.execute(
        "SELECT stat FROM sqlite_stat1 WHERE tbl='orders' AND idx='idx_orders_customer'"
    ).fetchone()
    print(f"  统计信息: {stat_after[0] if stat_after else 'N/A'}")
    show_plan(conn, "SELECT count(*) FROM orders WHERE customer_id = 2500", "更新后计划")
    avg_fresh, _ = benchmark(conn, "SELECT count(*) FROM orders WHERE customer_id = 2500", runs=3)
    print(f"  耗时: {avg_fresh:.3f}ms\n")

    print(f"  >>> ANALYZE 前后耗时: {avg_stale:.3f}ms → {avg_fresh:.3f}ms")
    print(f"  >>> 统计信息已从旧值更新为 {stat_after[0] if stat_after else 'N/A'}\n")
    conn.close()


def case5_join_order():
    print("=" * 70)
    print("案例5：Join 顺序不当 → 调整")
    print("=" * 70)
    print("场景：小表 customers 过滤后 JOIN 大表 orders，对比不同写法\n")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_items_order ON order_items(order_id)")
    conn.commit()

    print("--- 反模式：先扫描大表 orders 再 JOIN ---")
    sql_bad = """
        SELECT count(*)
        FROM orders o
        JOIN customers c ON o.customer_id = c.id
        JOIN order_items oi ON oi.order_id = o.id
        WHERE c.vip = 1 AND o.status = 'shipped'
    """
    show_plan(conn, sql_bad, "计划（SQLite 自动优化）")
    avg_bad, n_bad = benchmark(conn, sql_bad, runs=2)
    print(f"  耗时: {avg_bad:.2f}ms, 结果: {n_bad}\n")

    print("--- 优化：先过滤小表 customers，再 JOIN ---")
    sql_good = """
        SELECT count(*)
        FROM customers c
        JOIN orders o ON o.customer_id = c.id
        JOIN order_items oi ON oi.order_id = o.id
        WHERE c.vip = 1 AND o.status = 'shipped'
    """
    show_plan(conn, sql_good, "计划（调整书写顺序）")
    avg_good, n_good = benchmark(conn, sql_good, runs=2)
    print(f"  耗时: {avg_good:.2f}ms, 结果: {n_good}\n")

    print("--- 使用子查询先过滤小表 ---")
    sql_subquery = """
        SELECT count(*)
        FROM (SELECT id FROM customers WHERE vip = 1) c
        JOIN orders o ON o.customer_id = c.id AND o.status = 'shipped'
        JOIN order_items oi ON oi.order_id = o.id
    """
    show_plan(conn, sql_subquery, "子查询先过滤计划")
    avg_sub, n_sub = benchmark(conn, sql_subquery, runs=2)
    print(f"  耗时: {avg_sub:.2f}ms, 结果: {n_sub}\n")

    print(f"  >>> 原始: {avg_bad:.2f}ms | 调整顺序: {avg_good:.2f}ms | 子查询: {avg_sub:.2f}ms\n")
    conn.close()


def main():
    print(f"SQLite 版本: {sqlite3.sqlite_version}\n")
    print("正在创建测试数据库...")
    setup_database()

    case1_full_table_scan()
    case2_n_plus_one()
    case3_or_condition()
    case4_stale_stats()
    case5_join_order()

    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    for ext in ['-wal', '-shm']:
        p = DB_PATH + ext
        if os.path.exists(p):
            os.remove(p)

    print("=" * 70)
    print("全部案例完成")
    print("=" * 70)


if __name__ == '__main__':
    main()