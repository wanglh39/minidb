"""Join 策略对比：不同数据量下 SQLite 的 Join 计划选择与耗时

SQLite 不像 PostgreSQL 那样显式输出 Hash Join / Merge Join / Nested Loop，
但其查询计划中的 SCAN/SEARCH 顺序和索引使用反映了底层 Join 策略。
本脚本通过不同数据规模和索引状态，观察 SQLite 的 Join 策略选择。
"""
import sqlite3
import time
import os
import random

DB_PATH = "join_strategies.db"


def create_tables(conn):
    conn.execute("DROP TABLE IF EXISTS big_a")
    conn.execute("DROP TABLE IF EXISTS big_b")
    conn.execute("DROP TABLE IF EXISTS small_s")
    conn.execute("DROP TABLE IF EXISTS ordered_o")
    conn.commit()


def populate(conn, name, n_rows, with_index=True, ordered=False):
    conn.execute(f"DROP TABLE IF EXISTS {name}")
    conn.execute(f"CREATE TABLE {name} (id INTEGER PRIMARY KEY, key INTEGER, val TEXT)")

    rows = []
    if ordered:
        for i in range(1, n_rows + 1):
            rows.append((i, i, f"val_{i}"))
    else:
        keys = list(range(1, n_rows + 1))
        random.shuffle(keys)
        for i, k in enumerate(keys, 1):
            rows.append((i, k, f"val_{k}"))

    conn.executemany(f"INSERT INTO {name} VALUES (?,?,?)", rows)
    if with_index:
        conn.execute(f"CREATE INDEX idx_{name}_key ON {name}(key)")
    conn.commit()


def show_plan(conn, sql):
    rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()
    for r in rows:
        print(f"    {r[3]}")
    print()


def benchmark(conn, sql, runs=3):
    times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        n = len(conn.execute(sql).fetchall())
        t1 = time.perf_counter()
        times.append((t1 - t0) * 1000)
    return sum(times) / len(times), n


def scenario_small_drives_big():
    print("=" * 70)
    print("场景1：小表驱动大表（Nested Loop 策略）")
    print("=" * 70)
    print("  小表 100 行，大表 10万 行，大表有 key 索引")
    print("  期望：小表 SCAN，大表 SEARCH USING INDEX（类似 Nested Loop）\n")

    conn = sqlite3.connect(DB_PATH)
    create_tables(conn)
    populate(conn, "small_s", 100, with_index=True)
    populate(conn, "big_a", 100000, with_index=True)

    sql = "SELECT count(*) FROM small_s s JOIN big_a b ON s.key = b.key"
    print("--- 执行计划 ---")
    show_plan(conn, sql)
    avg, n = benchmark(conn, sql)
    print(f"  耗时: {avg:.2f}ms, 结果: {n}\n")

    print("--- 对比：大表驱动小表（书写顺序反过来）---")
    sql_rev = "SELECT count(*) FROM big_a b JOIN small_s s ON s.key = b.key"
    show_plan(conn, sql_rev)
    avg_rev, n = benchmark(conn, sql_rev)
    print(f"  耗时: {avg_rev:.2f}ms, 结果: {n}\n")

    print(f"  >>> 小驱动大: {avg:.2f}ms | 大驱动小: {avg_rev:.2f}ms")
    print(f"  >>> SQLite 优化器会自动选择更优的 Join 顺序\n")
    conn.close()


def scenario_big_join_big_with_index():
    print("=" * 70)
    print("场景2：大表 JOIN 大表（有索引）")
    print("=" * 70)
    print("  两表各 5万 行，都有 key 索引")
    print("  期望：SQLite 利用索引进行类似 Nested Loop + Index 的策略\n")

    conn = sqlite3.connect(DB_PATH)
    create_tables(conn)
    populate(conn, "big_a", 50000, with_index=True)
    populate(conn, "big_b", 50000, with_index=True)

    sql = "SELECT count(*) FROM big_a a JOIN big_b b ON a.key = b.key"
    print("--- 执行计划 ---")
    show_plan(conn, sql)
    avg, n = benchmark(conn, sql, runs=2)
    print(f"  耗时: {avg:.2f}ms, 结果: {n}\n")
    conn.close()


def scenario_big_join_big_no_index():
    print("=" * 70)
    print("场景3：大表 JOIN 大表（无索引）")
    print("=" * 70)
    print("  两表各 1万 行，都无 key 索引（迫使 SQLite 用其他策略）")
    print("  期望：SQLite 可能创建 AUTOMATIC INDEX 或全表嵌套扫描\n")

    conn = sqlite3.connect(DB_PATH)
    create_tables(conn)
    populate(conn, "big_a", 10000, with_index=False)
    populate(conn, "big_b", 10000, with_index=False)

    sql = "SELECT count(*) FROM big_a a JOIN big_b b ON a.key = b.key"
    print("--- 执行计划 ---")
    show_plan(conn, sql)
    avg, n = benchmark(conn, sql, runs=2)
    print(f"  耗时: {avg:.2f}ms, 结果: {n}\n")

    print("--- 对比：手动建索引后 ---")
    conn.execute("CREATE INDEX idx_a_key ON big_a(key)")
    conn.execute("CREATE INDEX idx_b_key ON big_b(key)")
    conn.commit()
    show_plan(conn, sql)
    avg2, n = benchmark(conn, sql, runs=2)
    print(f"  耗时: {avg2:.2f}ms, 结果: {n}\n")

    print(f"  >>> 无索引: {avg:.2f}ms | 有索引: {avg2:.2f}ms")
    print(f"  >>> 提升: {avg/avg2:.0f} 倍\n")
    conn.close()


def scenario_ordered_join():
    print("=" * 70)
    print("场景4：有序数据 JOIN（类似 Merge Join 的前提）")
    print("=" * 70)
    print("  两表各 5万 行，key 列有序（按 id 顺序插入递增 key）")
    print("  期望：有序数据 + 索引可能让 SQLite 选择更高效的扫描方式\n")

    conn = sqlite3.connect(DB_PATH)
    create_tables(conn)
    populate(conn, "ordered_o", 50000, with_index=True, ordered=True)
    populate(conn, "big_a", 50000, with_index=True, ordered=True)

    sql = "SELECT count(*) FROM ordered_o o JOIN big_a b ON o.key = b.key"
    print("--- 执行计划（有序 + 有索引）---")
    show_plan(conn, sql)
    avg_ordered, n = benchmark(conn, sql, runs=2)
    print(f"  耗时: {avg_ordered:.2f}ms, 结果: {n}\n")

    print("--- 对比：无序数据 + 有索引 ---")
    create_tables(conn)
    populate(conn, "ordered_o", 50000, with_index=True, ordered=False)
    populate(conn, "big_a", 50000, with_index=True, ordered=False)
    show_plan(conn, sql)
    avg_unordered, n = benchmark(conn, sql, runs=2)
    print(f"  耗时: {avg_unordered:.2f}ms, 结果: {n}\n")

    print(f"  >>> 有序: {avg_ordered:.2f}ms | 无序: {avg_unordered:.2f}ms\n")
    conn.close()


def scenario_different_sizes():
    print("=" * 70)
    print("场景5：不同数据量下的 Join 耗时对比")
    print("=" * 70)
    print("  小表从 10 到 5000 行，大表固定 5万 行，观察耗时变化\n")

    conn = sqlite3.connect(DB_PATH)
    create_tables(conn)
    populate(conn, "big_a", 50000, with_index=True)

    print(f"  {'小表行数':>8} {'耗时(ms)':>10} {'计划摘要':>30}")
    print(f"  {'-'*8} {'-'*10} {'-'*30}")

    for small_n in [10, 50, 100, 500, 1000, 5000]:
        conn.execute("DROP TABLE IF EXISTS small_s")
        conn.execute("CREATE TABLE small_s (id INTEGER PRIMARY KEY, key INTEGER, val TEXT)")
        rows = []
        random.seed(42)
        keys = random.sample(range(1, 50001), small_n)
        for i, k in enumerate(keys, 1):
            rows.append((i, k, f"val_{k}"))
        conn.executemany("INSERT INTO small_s VALUES (?,?,?)", rows)
        conn.execute("CREATE INDEX idx_small_s_key ON small_s(key)")
        conn.commit()

        sql = "SELECT count(*) FROM small_s s JOIN big_a b ON s.key = b.key"
        plan = conn.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()
        plan_summary = plan[0][3] if plan else "N/A"
        avg, n = benchmark(conn, sql, runs=3)
        print(f"  {small_n:>8} {avg:>10.2f} {plan_summary:>30}")

    print()
    conn.close()


def scenario_non_equi_join():
    print("=" * 70)
    print("场景6：非等值连接（只能用 Nested Loop）")
    print("=" * 70)
    print("  a.key < b.key 的非等值连接，无法用 Hash/Merge Join\n")

    conn = sqlite3.connect(DB_PATH)
    create_tables(conn)
    populate(conn, "small_s", 100, with_index=True)
    populate(conn, "big_a", 10000, with_index=True)

    sql = "SELECT count(*) FROM small_s s JOIN big_a b ON s.key < b.key"
    print("--- 执行计划 ---")
    show_plan(conn, sql)
    avg, n = benchmark(conn, sql, runs=2)
    print(f"  耗时: {avg:.2f}ms, 结果: {n}")

    print("\n--- 对比：等值连接 ---")
    sql_eq = "SELECT count(*) FROM small_s s JOIN big_a b ON s.key = b.key"
    show_plan(conn, sql_eq)
    avg_eq, n_eq = benchmark(conn, sql_eq, runs=2)
    print(f"  耗时: {avg_eq:.2f}ms, 结果: {n_eq}\n")

    print(f"  >>> 非等值: {avg:.2f}ms | 等值: {avg_eq:.2f}ms")
    print(f"  >>> 非等值连接只能 Nested Loop，通常比等值慢\n")
    conn.close()


def main():
    print(f"SQLite 版本: {sqlite3.sqlite_version}\n")
    print("Join 策略对比实验\n")

    scenario_small_drives_big()
    scenario_big_join_big_with_index()
    scenario_big_join_big_no_index()
    scenario_ordered_join()
    scenario_different_sizes()
    scenario_non_equi_join()

    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    for ext in ['-wal', '-shm']:
        p = DB_PATH + ext
        if os.path.exists(p):
            os.remove(p)

    print("=" * 70)
    print("实验完成")
    print("=" * 70)


if __name__ == '__main__':
    main()