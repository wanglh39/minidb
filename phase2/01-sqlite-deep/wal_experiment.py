"""WAL vs Rollback Journal 性能对比实验"""
import sqlite3
import time
import os
import threading

NUM_ROWS = 10000

def benchmark_insert(journal_mode, db_path):
    if os.path.exists(db_path):
        os.remove(db_path)
    for ext in ['-wal', '-shm', '-journal']:
        if os.path.exists(db_path + ext):
            os.remove(db_path + ext)

    conn = sqlite3.connect(db_path)
    conn.execute(f"PRAGMA journal_mode={journal_mode}")
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, val TEXT)")

    t0 = time.time()
    conn.execute("BEGIN")
    for i in range(NUM_ROWS):
        conn.execute("INSERT INTO t VALUES(?, ?)", (i, f"value_{i}" * 3))
    conn.execute("COMMIT")
    elapsed = time.time() - t0

    db_size = os.path.getsize(db_path)
    wal_size = os.path.getsize(db_path + '-wal') if os.path.exists(db_path + '-wal') else 0

    conn.close()
    return elapsed, db_size, wal_size

def benchmark_read(journal_mode, db_path):
    conn = sqlite3.connect(db_path)
    conn.execute(f"PRAGMA journal_mode={journal_mode}")

    t0 = time.time()
    rows = conn.execute("SELECT * FROM t WHERE id >= 100 AND id < 200").fetchall()
    elapsed_point = time.time() - t0

    t0 = time.time()
    rows = conn.execute("SELECT * FROM t WHERE val LIKE 'value_1%'").fetchall()
    elapsed_scan = time.time() - t0

    t0 = time.time()
    count = conn.execute("SELECT count(*) FROM t").fetchone()
    elapsed_agg = time.time() - t0

    conn.close()
    return elapsed_point, elapsed_scan, elapsed_agg

def concurrent_test(db_path="concurrent.db"):
    if os.path.exists(db_path):
        os.remove(db_path)
    for ext in ['-wal', '-shm', '-journal']:
        if os.path.exists(db_path + ext):
            os.remove(db_path + ext)

    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, val TEXT)")
    conn.close()

    results = {'read_count': 0, 'write_count': 0, 'errors': 0}

    def writer():
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        for i in range(200):
            try:
                conn.execute("INSERT INTO t VALUES(?, ?)", (i, f"w{i}"))
                results['write_count'] += 1
            except Exception as e:
                results['errors'] += 1
            time.sleep(0.001)
        conn.close()

    def reader():
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        for _ in range(200):
            try:
                conn.execute("SELECT count(*) FROM t").fetchone()
                results['read_count'] += 1
            except Exception as e:
                results['errors'] += 1
            time.sleep(0.001)
        conn.close()

    t0 = time.time()
    wt = threading.Thread(target=writer)
    rt = threading.Thread(target=reader)
    wt.start()
    rt.start()
    wt.join()
    rt.join()
    elapsed = time.time() - t0

    return elapsed, results

def main():
    print("=== SQLite WAL vs Rollback Journal ===\n")

    print(f"插入 {NUM_ROWS} 行:")
    for mode in ['DELETE', 'WAL']:
        elapsed, db_size, wal_size = benchmark_insert(mode, f"bench_{mode.lower()}.db")
        print(f"  {mode:8s}: {elapsed:.3f}s  db={db_size//1024}KB  wal={wal_size//1024}KB")

    print(f"\n查询性能:")
    for mode in ['DELETE', 'WAL']:
        ep, es, ea = benchmark_read(mode, f"bench_{mode.lower()}.db")
        print(f"  {mode:8s}: point={ep*1000:.2f}ms  scan={es*1000:.2f}ms  agg={ea*1000:.2f}ms")

    print(f"\nWAL 并发读写:")
    elapsed, results = concurrent_test()
    print(f"  耗时: {elapsed:.3f}s")
    print(f"  读成功: {results['read_count']}")
    print(f"  写成功: {results['write_count']}")
    print(f"  错误: {results['errors']}")

if __name__ == '__main__':
    main()