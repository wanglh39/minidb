"""幻读演示：同一事务内两次范围查询，结果集行数不同

SQLite 的 BEGIN 默认提供快照隔离，无法在同一事务内复现幻读。
本脚本用 autocommit 模式模拟 Read Committed 行为复现幻读，
并用 BEGIN...COMMIT 展示快照隔离如何防止幻读。

运行：python phatom_read.py
"""
import sqlite3
import threading
import time
import os
import tempfile


def setup_db(db_path):
    for _ in range(5):
        try:
            if os.path.exists(db_path):
                os.remove(db_path)
            for ext in ['-wal', '-shm', '-journal']:
                p = db_path + ext
                if os.path.exists(p):
                    os.remove(p)
            break
        except PermissionError:
            time.sleep(0.2)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE orders(id INTEGER PRIMARY KEY, amount INTEGER)")
    for i in range(100):
        conn.execute("INSERT INTO orders VALUES(?, ?)", (i + 1, i * 10))
    conn.close()
    time.sleep(0.1)


def demonstrate_phantom_read(db_path):
    print("\n--- Read Committed 行为（autocommit 模拟）复现幻读 ---")

    results = {}

    def t2_reader():
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        time.sleep(0.05)
        c1 = conn.execute("SELECT count(*) FROM orders WHERE amount > 0").fetchone()[0]
        results['first'] = c1
        print(f"  [T2] 第一次范围查: count(amount>0) = {c1}")
        time.sleep(0.3)
        c2 = conn.execute("SELECT count(*) FROM orders WHERE amount > 0").fetchone()[0]
        results['second'] = c2
        print(f"  [T2] 第二次范围查: count(amount>0) = {c2}")
        conn.close()

    def t1_writer():
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        time.sleep(0.15)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO orders VALUES(999, 500)")
        conn.execute("COMMIT")
        print(f"  [T1] 插入新行 (id=999, amount=500) 并提交")
        conn.close()

    th1 = threading.Thread(target=t1_writer)
    th2 = threading.Thread(target=t2_reader)
    th1.start()
    th2.start()
    th1.join(timeout=5)
    th2.join(timeout=5)

    if results.get('first') != results.get('second'):
        print(f"  结果: ✗ 发生幻读（{results['first']} → {results['second']}，多了一行）")
        return True
    else:
        print(f"  结果: ✓ 未发生幻读")
        return False


def demonstrate_no_phantom_in_snapshot(db_path):
    print("\n--- 快照隔离（BEGIN...COMMIT）防止幻读 ---")

    results = {}

    def t2_reader():
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        time.sleep(0.05)
        conn.execute("BEGIN")
        c1 = conn.execute("SELECT count(*) FROM orders WHERE amount > 0").fetchone()[0]
        results['first'] = c1
        print(f"  [T2] 第一次范围查: count(amount>0) = {c1}")
        time.sleep(0.3)
        c2 = conn.execute("SELECT count(*) FROM orders WHERE amount > 0").fetchone()[0]
        results['second'] = c2
        print(f"  [T2] 第二次范围查: count(amount>0) = {c2}")
        conn.execute("COMMIT")
        conn.close()

    def t1_writer():
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        time.sleep(0.15)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO orders VALUES(999, 500)")
        conn.execute("COMMIT")
        print(f"  [T1] 插入新行 (id=999, amount=500) 并提交")
        conn.close()

    th1 = threading.Thread(target=t1_writer)
    th2 = threading.Thread(target=t2_reader)
    th1.start()
    th2.start()
    th1.join(timeout=5)
    th2.join(timeout=5)

    if results.get('first') == results.get('second'):
        print(f"  结果: ✓ 未发生幻读（两次都是 {results['first']}）")
        return True
    else:
        print(f"  结果: ✗ 发生幻读")
        return False


def demonstrate_phantom_vs_non_repeatable():
    print("\n--- 幻读 vs 不可重复读的区别 ---")
    print("不可重复读：已有行的【值】被修改")
    print("  T2: SELECT balance WHERE id=1  → 100")
    print("  T1: UPDATE balance=200 WHERE id=1; COMMIT")
    print("  T2: SELECT balance WHERE id=1  → 200  (同一行值变了)")
    print()
    print("幻读：【新行】被插入满足查询条件")
    print("  T2: SELECT count(*) WHERE amount>0  → 99")
    print("  T1: INSERT (id=999, amount=500); COMMIT")
    print("  T2: SELECT count(*) WHERE amount>0  → 100  (多了一行)")


def main():
    print("=== 幻读演示 ===")

    db_path = os.path.join(tempfile.gettempdir(), "phantom_demo.db")

    setup_db(db_path)
    demonstrate_phantom_read(db_path)

    setup_db(db_path)
    demonstrate_no_phantom_in_snapshot(db_path)

    demonstrate_phantom_vs_non_repeatable()

    print("\n=== 总结 ===")
    print("1. 幻读：范围查询结果集行数变化（新行插入或旧行删除）")
    print("2. Read Committed 下发生")
    print("3. Repeatable Read / 快照隔离下防止")
    print("4. PostgreSQL 的 RR 是 SI，已防止幻读（SQL标准RR允许）")
    print("5. SQLite 的 BEGIN 默认快照隔离，autocommit 模式类似 RC")

    for _ in range(5):
        try:
            if os.path.exists(db_path):
                os.remove(db_path)
            for ext in ['-wal', '-shm', '-journal']:
                p = db_path + ext
                if os.path.exists(p):
                    os.remove(p)
            break
        except PermissionError:
            time.sleep(0.2)


if __name__ == '__main__':
    main()
