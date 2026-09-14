"""EXPLAIN 使用演示：建测试数据、对比有/无索引的执行计划、解读 SCAN vs SEARCH"""
import sqlite3
import time
import os

DB_PATH = "explain_demo.db"


def setup_database():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode = WAL")
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
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            region TEXT NOT NULL,
            vip INTEGER NOT NULL
        )
    """)

    import random
    random.seed(42)
    statuses = ['pending', 'paid', 'shipped', 'cancelled']
    regions = ['CN', 'US', 'EU', 'JP', 'KR']

    customers = [(i, f"customer_{i}", random.choice(regions), random.randint(0, 1)) for i in range(1, 1001)]
    conn.executemany("INSERT INTO customers VALUES (?,?,?,?)", customers)

    orders = []
    for i in range(1, 100001):
        cid = random.randint(1, 1000)
        status = random.choice(statuses)
        amount = round(random.uniform(10, 1000), 2)
        created = f"2024-{random.randint(1,12):02d}-{random.randint(1,28):02d}"
        orders.append((i, cid, status, amount, created))
    conn.executemany("INSERT INTO orders VALUES (?,?,?,?,?)", orders)
    conn.commit()
    conn.close()


def explain_query_plan(conn, sql, params=()):
    print(f"  SQL: {sql}")
    rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
    for r in rows:
        print(f"    id={r[0]} parent={r[1]} detail={r[3]}")
    print()
    return rows


def explain_bytecode(conn, sql, max_lines=15):
    print(f"  SQL: {sql}")
    rows = list(conn.execute(f"EXPLAIN {sql}"))
    print(f"    共 {len(rows)} 条 VDBE 指令，前 {max_lines} 条：")
    for r in rows[:max_lines]:
        print(f"    {r[0]:3d}  {r[1]:15s} p1={r[2]} p2={r[3]} p3={r[4]} p4={str(r[5])[:20]}")
    if len(rows) > max_lines:
        print(f"    ... (省略 {len(rows) - max_lines} 条)")
    print()


def benchmark(conn, sql, params=(), runs=5):
    times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        conn.execute(sql, params).fetchall()
        t1 = time.perf_counter()
        times.append((t1 - t0) * 1000)
    avg = sum(times) / len(times)
    return avg, min(times), max(times)


def demo_scan_vs_search():
    print("=" * 70)
    print("演示1：全表扫描 (SCAN) vs 索引查找 (SEARCH)")
    print("=" * 70)

    conn = sqlite3.connect(DB_PATH)

    print("\n--- 无索引时：按 customer_id 查询 ---")
    explain_query_plan(conn, "SELECT * FROM orders WHERE customer_id = 42")
    avg, lo, hi = benchmark(conn, "SELECT * FROM orders WHERE customer_id = 42")
    print(f"  耗时: avg={avg:.2f}ms min={lo:.2f}ms max={hi:.2f}ms\n")

    print("--- 创建索引 ---")
    conn.execute("CREATE INDEX idx_orders_customer ON orders(customer_id)")
    conn.commit()

    print("--- 有索引后：按 customer_id 查询 ---")
    explain_query_plan(conn, "SELECT * FROM orders WHERE customer_id = 42")
    avg, lo, hi = benchmark(conn, "SELECT * FROM orders WHERE customer_id = 42")
    print(f"  耗时: avg={avg:.3f}ms min={lo:.3f}ms max={hi:.3f}ms\n")

    print("--- 覆盖索引：只查 customer_id 列 ---")
    explain_query_plan(conn, "SELECT customer_id FROM orders WHERE customer_id = 42")
    avg, lo, hi = benchmark(conn, "SELECT customer_id FROM orders WHERE customer_id = 42")
    print(f"  耗时: avg={avg:.3f}ms min={lo:.3f}ms max={hi:.3f}ms\n")

    conn.close()


def demo_join_plan():
    print("=" * 70)
    print("演示2：JOIN 的执行计划")
    print("=" * 70)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id)")
    conn.commit()

    print("\n--- JOIN 查询（customers 有主键索引，orders 有 customer_id 索引）---")
    explain_query_plan(conn, """
        SELECT c.name, o.id, o.amount
        FROM customers c JOIN orders o ON c.id = o.customer_id
        WHERE c.region = 'CN'
    """)

    print("--- 三表 JOIN ---")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
    conn.commit()
    explain_query_plan(conn, """
        SELECT c.name, o.id, o.amount
        FROM customers c
        JOIN orders o ON c.id = o.customer_id
        WHERE c.region = 'CN' AND o.status = 'shipped'
    """)

    conn.close()


def demo_aggregate_sort_plan():
    print("=" * 70)
    print("演示3：聚合与排序的执行计划")
    print("=" * 70)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id)")
    conn.commit()

    print("\n--- GROUP BY 聚合 ---")
    explain_query_plan(conn, """
        SELECT customer_id, count(*) AS cnt
        FROM orders
        GROUP BY customer_id
    """)

    print("--- ORDER BY + LIMIT ---")
    explain_query_plan(conn, "SELECT * FROM orders ORDER BY amount DESC LIMIT 10")

    print("--- ORDER BY（无索引列）---")
    explain_query_plan(conn, "SELECT * FROM orders ORDER BY amount DESC")

    print("--- ORDER BY（有索引列）---")
    explain_query_plan(conn, "SELECT * FROM orders ORDER BY customer_id DESC LIMIT 10")

    conn.close()


def demo_analyze_stats():
    print("=" * 70)
    print("演示4：ANALYZE 统计信息")
    print("=" * 70)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")

    print("\n--- ANALYZE 前的 sqlite_stat1 ---")
    try:
        rows = conn.execute("SELECT * FROM sqlite_stat1").fetchall()
        for r in rows:
            print(f"    tbl={r[0]} idx={r[1]} stat={r[2]}")
    except sqlite3.OperationalError:
        print("    (sqlite_stat1 不存在，尚未 ANALYZE)")

    print("\n--- 执行 ANALYZE ---")
    conn.execute("ANALYZE")
    conn.commit()

    print("--- ANALYZE 后的 sqlite_stat1 ---")
    rows = conn.execute("SELECT * FROM sqlite_stat1").fetchall()
    for r in rows:
        print(f"    tbl={r[0]:15s} idx={str(r[1]):25s} stat={r[2]}")

    print("\n--- 解读 stat 字段 ---")
    for r in rows:
        if r[1] and r[2]:
            parts = r[2].split()
            print(f"    {r[0]}.{r[1]}: 总行数={parts[0]}", end="")
            if len(parts) > 1:
                print(f" 不同值数≈{parts[1]}", end="")
                if int(parts[0]) > 0 and int(parts[1]) > 0:
                    selectivity = int(parts[1]) / int(parts[0])
                    print(f" 选择度={selectivity:.4f}", end="")
            print()

    conn.close()


def demo_bytecode():
    print("=" * 70)
    print("演示5：VDBE 字节码 (EXPLAIN)")
    print("=" * 70)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id)")
    conn.commit()

    print("\n--- 索引查找的字节码 ---")
    explain_bytecode(conn, "SELECT * FROM orders WHERE customer_id = 42")

    print("--- 全表扫描的字节码 ---")
    explain_bytecode(conn, "SELECT * FROM orders WHERE amount > 500")

    conn.close()


def main():
    print(f"SQLite 版本: {sqlite3.sqlite_version}\n")
    print("正在创建测试数据库 (1000 客户, 10万 订单)...")
    setup_database()
    print("数据库创建完成\n")

    demo_scan_vs_search()
    demo_join_plan()
    demo_aggregate_sort_plan()
    demo_analyze_stats()
    demo_bytecode()

    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    for ext in ['-wal', '-shm']:
        p = DB_PATH + ext
        if os.path.exists(p):
            os.remove(p)

    print("=" * 70)
    print("演示完成")
    print("=" * 70)


if __name__ == '__main__':
    main()