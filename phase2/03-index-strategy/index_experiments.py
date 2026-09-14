"""
index_experiments.py — 索引策略实战实验（SQLite）

实验内容：
  1. 建表 + 10万行测试数据
  2. 无索引 vs B-Tree 索引 等值/范围查询对比
  3. 复合索引最左前缀原则演示
  4. 覆盖索引 Index-Only Scan 演示
  5. 索引失效场景（LIKE 前导% / 函数 / OR / 列运算）
  6. TPC-H Q1-Q5 在不同索引策略下的耗时对比

运行：
  python index_experiments.py
"""
import os
import sqlite3
import time

from generate_tpch import build as build_tpch, DB_PATH, load_queries, run_q1_to_q5

N_ROWS = 100_000
N_REPEAT = 200
SEED = 42

EXPERIMENT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "index_exp.db")


def _bench(conn: sqlite3.Connection, sql: str, params=(), n: int = N_REPEAT) -> float:
    t0 = time.perf_counter()
    for _ in range(n):
        conn.execute(sql, params).fetchall()
    return (time.perf_counter() - t0) / n * 1000.0


def _explain(conn: sqlite3.Connection, sql: str, params=()) -> str:
    plan = conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
    return " | ".join(str(r) for r in plan)


def _drop_all_indexes(conn: sqlite3.Connection, table: str):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=? AND name NOT LIKE 'sqlite_%'",
        (table,),
    ).fetchall()
    for (idx_name,) in rows:
        conn.execute(f"DROP INDEX IF EXISTS {idx_name}")


def setup_experiment_db(db_path: str = EXPERIMENT_DB) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    if os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    conn.execute(
        """
        CREATE TABLE t (
            id  INTEGER PRIMARY KEY,
            a   INTEGER NOT NULL,
            b   INTEGER NOT NULL,
            c   TEXT    NOT NULL,
            d   REAL    NOT NULL
        )
        """
    )

    import random
    rng = random.Random(SEED)
    batch = []
    for i in range(1, N_ROWS + 1):
        a = rng.randint(1, 1000)
        b = rng.randint(1, 500)
        c = f"str_{rng.randint(1, 10000):06d}"
        d = round(rng.uniform(0.0, 1000.0), 4)
        batch.append((i, a, b, c, d))
        if len(batch) >= 5000:
            conn.executemany("INSERT INTO t VALUES (?,?,?,?,?)", batch)
            batch.clear()
    if batch:
        conn.executemany("INSERT INTO t VALUES (?,?,?,?,?)", batch)
    conn.commit()
    conn.execute("ANALYZE")
    return conn


def experiment_1_basic(conn: sqlite3.Connection):
    print("\n" + "=" * 78)
    print("实验1：无索引 vs B-Tree 索引（等值 / 范围查询）")
    print("=" * 78)
    print(f"表 t 行数: {conn.execute('SELECT COUNT(*) FROM t').fetchone()[0]}")
    print(f"每个查询重复 {N_REPEAT} 次取平均\n")

    _drop_all_indexes(conn, "t")
    conn.execute("ANALYZE")

    target_a = 500
    sql_eq = "SELECT * FROM t WHERE a = ?"
    sql_range = "SELECT * FROM t WHERE a BETWEEN ? AND ?"

    t_no = _bench(conn, sql_eq, (target_a,))
    plan_no = _explain(conn, sql_eq, (target_a,))
    t_range_no = _bench(conn, sql_range, (490, 510))
    plan_range_no = _explain(conn, sql_range, (490, 510))

    conn.execute("CREATE INDEX idx_a ON t(a)")
    conn.execute("ANALYZE")
    t_btree = _bench(conn, sql_eq, (target_a,))
    plan_btree = _explain(conn, sql_eq, (target_a,))
    t_range_btree = _bench(conn, sql_range, (490, 510))
    plan_range_btree = _explain(conn, sql_range, (490, 510))

    print(f"{'场景':<28} {'耗时(ms)':>10} {'加速比':>10}")
    print("-" * 78)
    print(f"{'等值 无索引':<28} {t_no:>10.3f} {'1.00x':>10}")
    print(f"{'等值 B-Tree idx(a)':<28} {t_btree:>10.3f} {t_no/t_btree:>9.1f}x")
    print(f"{'范围 无索引':<28} {t_range_no:>10.3f} {'1.00x':>10}")
    print(f"{'范围 B-Tree idx(a)':<28} {t_range_btree:>10.3f} {t_range_no/t_range_btree:>9.1f}x")
    print()
    print(f"执行计划-等值无索引: {plan_no}")
    print(f"执行计划-等值B-Tree : {plan_btree}")
    print(f"执行计划-范围无索引: {plan_range_no}")
    print(f"执行计划-范围B-Tree : {plan_range_btree}")

    _drop_all_indexes(conn, "t")
    conn.execute("ANALYZE")


def experiment_2_composite(conn: sqlite3.Connection):
    print("\n" + "=" * 78)
    print("实验2：复合索引 (a, b, c) 最左前缀原则")
    print("=" * 78)

    _drop_all_indexes(conn, "t")
    conn.execute("CREATE INDEX idx_abc ON t(a, b, c)")
    conn.execute("ANALYZE")

    cases = [
        ("a=500 AND b=250 AND c LIKE 'str_0001%'", "SELECT * FROM t WHERE a=500 AND b=250 AND c LIKE 'str_0001%'"),
        ("a=500 AND b=250", "SELECT * FROM t WHERE a=500 AND b=250"),
        ("a=500", "SELECT * FROM t WHERE a=500"),
        ("b=250", "SELECT * FROM t WHERE b=250"),
        ("c LIKE 'str_0001%'", "SELECT * FROM t WHERE c LIKE 'str_0001%'"),
        ("a=500 AND c LIKE 'str_0001%'", "SELECT * FROM t WHERE a=500 AND c LIKE 'str_0001%'"),
        ("a>400 AND b=250", "SELECT * FROM t WHERE a>400 AND b=250"),
    ]

    print(f"\n{'WHERE 条件':<40} {'耗时(ms)':>10}  执行计划")
    print("-" * 78)
    for label, sql in cases:
        t = _bench(conn, sql, n=50)
        plan = _explain(conn, sql)
        used = "SEARCH" in plan and "USING" in plan
        flag = "✅" if used else "❌"
        print(f"{flag} {label:<38} {t:>10.3f}  {plan}")

    _drop_all_indexes(conn, "t")
    conn.execute("ANALYZE")


def experiment_3_covering(conn: sqlite3.Connection):
    print("\n" + "=" * 78)
    print("实验3：覆盖索引 Index-Only Scan")
    print("=" * 78)

    _drop_all_indexes(conn, "t")
    conn.execute("ANALYZE")

    sql_cover = "SELECT a, b FROM t WHERE a = ?"

    conn.execute("CREATE INDEX idx_a_only ON t(a)")
    conn.execute("ANALYZE")
    t_non_cover = _bench(conn, sql_cover, (500,))
    plan_non_cover = _explain(conn, sql_cover, (500,))

    _drop_all_indexes(conn, "t")
    conn.execute("CREATE INDEX idx_a_b_cover ON t(a, b)")
    conn.execute("ANALYZE")
    t_cover = _bench(conn, sql_cover, (500,))
    plan_cover = _explain(conn, sql_cover, (500,))

    sql_non_cover_col = "SELECT a, b, d FROM t WHERE a = ?"
    t_need_table = _bench(conn, sql_non_cover_col, (500,))
    plan_need_table = _explain(conn, sql_non_cover_col, (500,))

    print(f"\n{'场景':<40} {'耗时(ms)':>10}  执行计划")
    print("-" * 78)
    print(f"{'idx(a) 非覆盖 (SELECT a,b)':<40} {t_non_cover:>10.3f}  {plan_non_cover}")
    print(f"{'idx(a,b) 覆盖 (SELECT a,b)':<40} {t_cover:>10.3f}  {plan_cover}")
    print(f"{'idx(a,b) 退化 (SELECT a,b,d)':<40} {t_need_table:>10.3f}  {plan_need_table}")
    print()
    print(f"覆盖索引加速比: {t_non_cover/t_cover:.2f}x")
    print("说明: SQLite EXPLAIN 出现 'COVERING INDEX' 即 Index-Only Scan，不回表")

    _drop_all_indexes(conn, "t")
    conn.execute("ANALYZE")


def experiment_4_failure(conn: sqlite3.Connection):
    print("\n" + "=" * 78)
    print("实验4：索引失效场景")
    print("=" * 78)

    _drop_all_indexes(conn, "t")
    conn.execute("CREATE INDEX idx_a ON t(a)")
    conn.execute("CREATE INDEX idx_b ON t(b)")
    conn.execute("CREATE INDEX idx_c ON t(c)")
    conn.execute("PRAGMA case_sensitive_like = ON")
    conn.execute("ANALYZE")

    cases = [
        ("LIKE 前缀+覆盖 (走索引)", "SELECT c FROM t WHERE c LIKE 'str_0001%'"),
        ("LIKE 前导% SELECT * (失效)", "SELECT * FROM t WHERE c LIKE '%0001%'"),
        ("OR 跨列 (可能 BitmapOr)", "SELECT * FROM t WHERE a = 500 OR b = 250"),
        ("列参与运算 (失效)", "SELECT * FROM t WHERE a + 1 = 501"),
        ("等值改写后 (走索引)", "SELECT * FROM t WHERE a = 500"),
        ("不等式 (选择性低)", "SELECT * FROM t WHERE a != 500"),
        ("UPPER(c) 函数 (失效)", "SELECT c FROM t WHERE UPPER(c) = 'STR_000100'"),
    ]

    print(f"\n{'场景':<32} {'耗时(ms)':>10}  执行计划")
    print("-" * 78)
    for label, sql in cases:
        t = _bench(conn, sql, n=50)
        plan = _explain(conn, sql)
        used = "SEARCH" in plan and "USING" in plan
        flag = "✅" if used else "❌"
        print(f"{flag} {label:<30} {t:>10.3f}  {plan}")

    print()
    print("修复示例: 对 UPPER(c) 建表达式索引")
    conn.execute("CREATE INDEX idx_c_upper ON t(UPPER(c))")
    conn.execute("ANALYZE")
    sql_fixed = "SELECT * FROM t WHERE UPPER(c) = 'STR_000100'"
    t_fixed = _bench(conn, sql_fixed, n=50)
    plan_fixed = _explain(conn, sql_fixed)
    print(f"✅ {'UPPER(c) 表达式索引':<30} {t_fixed:>10.3f}  {plan_fixed}")

    _drop_all_indexes(conn, "t")
    conn.execute("PRAGMA case_sensitive_like = OFF")
    conn.execute("ANALYZE")


def experiment_5_tpch():
    print("\n" + "=" * 78)
    print("实验5：TPC-H Q1-Q5 不同索引策略对比")
    print("=" * 78)

    tpch_db = DB_PATH
    if not os.path.exists(tpch_db):
        print("[tpch] 数据库不存在，先生成 ...")
        build_tpch(verbose=True).close()

    queries = load_queries()

    strategies = [
        ("无索引", []),
        ("单列索引", [
            "CREATE INDEX idx_l_shipdate ON lineitem(l_shipdate)",
            "CREATE INDEX idx_o_custkey ON orders(o_custkey)",
            "CREATE INDEX idx_o_status ON orders(o_orderstatus)",
            "CREATE INDEX idx_o_date ON orders(o_orderdate)",
            "CREATE INDEX idx_c_custkey ON customer(c_custkey)",
            "CREATE INDEX idx_c_region ON customer(c_region)",
            "CREATE INDEX idx_c_name ON customer(c_name)",
            "CREATE INDEX idx_l_orderkey ON lineitem(l_orderkey)",
        ]),
        ("复合索引", [
            "CREATE INDEX idx_l_shipdate ON lineitem(l_shipdate)",
            "CREATE INDEX idx_o_custkey ON orders(o_custkey)",
            "CREATE INDEX idx_o_status_date ON orders(o_orderstatus, o_orderdate)",
            "CREATE INDEX idx_o_date_cust ON orders(o_orderdate, o_custkey)",
            "CREATE INDEX idx_c_custkey ON customer(c_custkey)",
            "CREATE INDEX idx_c_region ON customer(c_region)",
            "CREATE INDEX idx_c_name ON customer(c_name)",
            "CREATE INDEX idx_l_orderkey ON lineitem(l_orderkey)",
        ]),
        ("覆盖索引", [
            "CREATE INDEX idx_l_shipdate_cover ON lineitem(l_shipdate, l_returnflag, l_linestatus, l_quantity, l_extendedprice, l_discount, l_tax)",
            "CREATE INDEX idx_o_custkey_cover ON orders(o_custkey, o_orderkey, o_orderdate, o_totalprice, o_orderstatus)",
            "CREATE INDEX idx_o_status_date_cover ON orders(o_orderstatus, o_orderdate, o_custkey, o_orderkey, o_totalprice)",
            "CREATE INDEX idx_o_date_cust_cover ON orders(o_orderdate, o_custkey, o_orderkey, o_totalprice, o_orderstatus)",
            "CREATE INDEX idx_c_custkey_cover ON customer(c_custkey, c_name, c_region)",
            "CREATE INDEX idx_c_region_cover ON customer(c_region, c_custkey, c_name)",
            "CREATE INDEX idx_c_name_cover ON customer(c_name, c_custkey, c_region)",
            "CREATE INDEX idx_l_orderkey_cover ON lineitem(l_orderkey)",
        ]),
    ]

    results = {}
    for strat_name, idx_sqls in strategies:
        print(f"\n--- 策略: {strat_name} ---")
        conn = sqlite3.connect(tpch_db)
        conn.execute("PRAGMA journal_mode=WAL")
        for idx_sql in idx_sqls:
            conn.execute(idx_sql)
        conn.execute("ANALYZE")

        row = {}
        for qid in ["Q1", "Q2", "Q3", "Q4", "Q5"]:
            sql = queries.get(qid)
            if not sql:
                continue
            try:
                t = _bench(conn, sql, n=5)
                plan = _explain(conn, sql)
                row[qid] = (t, plan)
                used = "USING INDEX" in plan or "USING COVERING INDEX" in plan
                flag = "✅" if used else "❌"
                print(f"  {qid}: {t:>8.3f} ms  {flag}  {plan[:80]}")
            except Exception as e:
                row[qid] = (None, str(e))
                print(f"  {qid}: ERROR {e}")
        results[strat_name] = row

        conn.close()
        conn = sqlite3.connect(tpch_db)
        cur = conn.execute("SELECT name, tbl_name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'")
        for name, tbl in cur.fetchall():
            conn.execute(f"DROP INDEX IF EXISTS {name}")
        conn.commit()
        conn.close()

    print("\n" + "=" * 78)
    print("TPC-H 耗时汇总 (ms, 5次平均)")
    print("=" * 78)
    header = f"{'策略':<14}" + "".join(f"{q:>12}" for q in ["Q1", "Q2", "Q3", "Q4", "Q5"])
    print(header)
    print("-" * 78)
    base = {q: results["无索引"][q][0] for q in ["Q1", "Q2", "Q3", "Q4", "Q5"] if q in results["无索引"] and results["无索引"][q][0]}
    for strat_name, _ in strategies:
        row = results[strat_name]
        cells = []
        for q in ["Q1", "Q2", "Q3", "Q4", "Q5"]:
            v = row.get(q)
            if v is None or v[0] is None:
                cells.append(f"{'ERR':>12}")
            else:
                t = v[0]
                speedup = base.get(q, t) / t if t > 0 else 0
                cells.append(f"{t:>8.2f}({speedup:>3.1f}x)")
        print(f"{strat_name:<14}" + "".join(cells))


def main():
    print("=" * 78)
    print("索引策略实战实验 — SQLite")
    print(f"测试表行数: {N_ROWS}, 每查询重复: {N_REPEAT}")
    print("=" * 78)

    conn = setup_experiment_db()
    experiment_1_basic(conn)
    experiment_2_composite(conn)
    experiment_3_covering(conn)
    experiment_4_failure(conn)
    conn.close()

    experiment_5_tpch()

    print("\n[done] 全部实验完成")


if __name__ == "__main__":
    main()