"""miniDB vs SQLite 功能与性能对照"""
import sqlite3
import time
import os
import subprocess

NUM_ROWS = 10000

def sqlite_benchmark():
    db_path = "vs_sqlite.db"
    if os.path.exists(db_path):
        os.remove(db_path)

    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, age INTEGER, score INTEGER)")

    t0 = time.time()
    conn.execute("BEGIN")
    for i in range(NUM_ROWS):
        conn.execute("INSERT INTO users VALUES(?, ?, ?)", (i, 20 + i % 50, i * 2))
    conn.execute("COMMIT")
    insert_time = time.time() - t0

    t0 = time.time()
    rows = conn.execute("SELECT * FROM users").fetchall()
    scan_time = time.time() - t0

    t0 = time.time()
    for i in range(1000):
        conn.execute("SELECT * FROM users WHERE id = ?", (i * 10)).fetchall()
    point_time = time.time() - t0

    t0 = time.time()
    rows = conn.execute("SELECT * FROM users WHERE age > 50").fetchall()
    filter_time = time.time() - t0

    t0 = time.time()
    rows = conn.execute("SELECT id FROM users WHERE age > 30").fetchall()
    project_time = time.time() - t0

    conn.close()
    os.remove(db_path)

    return {
        'insert': insert_time,
        'scan': scan_time,
        'point_lookup': point_time,
        'filter': filter_time,
        'project': project_time,
    }

def minidb_benchmark():
    minidb_path = "/tmp/minidb.exe"
    if not os.path.exists(minidb_path):
        return None

    queries = [
        ("insert", "INSERT INTO users (id, age, score) VALUES (1, 25, 85);"),
        ("scan", "SELECT * FROM users;"),
        ("filter", "SELECT * FROM users WHERE age > 28;"),
        ("project", "SELECT id FROM users WHERE age > 28;"),
    ]

    results = {}
    for name, sql in queries:
        try:
            t0 = time.time()
            proc = subprocess.run(
                [minidb_path],
                input=sql + "\n.exit\n",
                capture_output=True, text=True, timeout=5
            )
            results[name] = time.time() - t0
        except:
            results[name] = None

    return results

def print_comparison():
    print("=== miniDB vs SQLite 对照表 ===\n")

    print("功能对照:")
    features = [
        ("SQL 子集",        "SELECT/INSERT/DELETE/CREATE",  "完整 SQL + 扩展"),
        ("数据类型",        "INT32, INT64, FLOAT",          "INTEGER, TEXT, REAL, BLOB, NULL"),
        ("索引",            "B+Tree (固定阶)",              "B-Tree (变长)"),
        ("事务",            "2PL + MVCC",                   "WAL + 库级锁"),
        ("并发",            "行级锁",                       "单写多读 (WAL)"),
        ("存储",            "Slotted Page 4096B",           "B-Tree 页 512~65536B"),
        ("执行",            "Volcano 迭代器",               "字节码 VM"),
        ("优化",            "启发式 + 简单代价",            "基于代价"),
        ("网络",            "无 (CLI only)",                "无 (嵌入式)"),
        ("扩展",            "无",                           "C 扩展 API"),
    ]
    print(f"  {'特性':20s} {'miniDB':30s} {'SQLite':30s}")
    print(f"  {'-'*20} {'-'*30} {'-'*30}")
    for feat, mini, lite in features:
        print(f"  {feat:20s} {mini:30s} {lite:30s}")

    print(f"\n性能对比 ({NUM_ROWS} 行):")
    sqlite_res = sqlite_benchmark()

    print(f"  {'操作':20s} {'SQLite':15s}")
    print(f"  {'-'*20} {'-'*15}")
    for op, time_val in sqlite_res.items():
        print(f"  {op:20s} {time_val*1000:10.2f} ms")

    minidb_res = minidb_benchmark()
    if minidb_res:
        print(f"\n  miniDB CLI 查询:")
        for op, time_val in minidb_res.items():
            if time_val:
                print(f"    {op:20s} {time_val*1000:10.2f} ms")
            else:
                print(f"    {op:20s}       N/A")
    else:
        print(f"\n  (miniDB 可执行文件未找到，跳过)")

def main():
    print_comparison()

if __name__ == '__main__':
    main()