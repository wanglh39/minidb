"""不可重复读演示：同一事务内两次读同一行结果不同

SQLite 的 BEGIN 默认提供快照隔离，无法在同一事务内复现不可重复读。
本脚本用 autocommit 模式（每条语句独立快照）模拟 Read Committed 行为，
并用 BEGIN...COMMIT 展示快照隔离如何防止不可重复读。

运行：python non_repeatable_read.py
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
    conn.execute("CREATE TABLE accounts(id INTEGER PRIMARY KEY, balance INTEGER)")
    conn.execute("INSERT INTO accounts VALUES(1, 100)")
    conn.close()
    time.sleep(0.1)


def demonstrate_read_committed_behavior(db_path):
    print("\n--- Read Committed 行为（autocommit 模拟）---")
    print("每条 SELECT 独立获取快照，能看到中间提交的修改")

    results = {}

    def t2_reader():
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        time.sleep(0.05)
        r1 = conn.execute("SELECT balance FROM accounts WHERE id=1").fetchone()[0]
        results['first'] = r1
        print(f"  [T2] 第一次读: balance = {r1}")
        time.sleep(0.3)
        r2 = conn.execute("SELECT balance FROM accounts WHERE id=1").fetchone()[0]
        results['second'] = r2
        print(f"  [T2] 第二次读: balance = {r2}")
        conn.close()

    def t1_writer():
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        time.sleep(0.15)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE accounts SET balance=200 WHERE id=1")
        conn.execute("COMMIT")
        print(f"  [T1] 修改并提交: balance = 200")
        conn.close()

    th1 = threading.Thread(target=t1_writer)
    th2 = threading.Thread(target=t2_reader)
    th1.start()
    th2.start()
    th1.join(timeout=5)
    th2.join(timeout=5)

    if results.get('first') != results.get('second'):
        print(f"  结果: ✗ 发生不可重复读（{results['first']} → {results['second']}）")
        print(f"  说明: autocommit 模式下每条语句独立，类似 Read Committed")
        return True
    else:
        print(f"  结果: ✓ 未发生不可重复读")
        return False


def demonstrate_snapshot_isolation(db_path):
    print("\n--- 快照隔离（BEGIN...COMMIT）防止不可重复读 ---")
    print("整个事务用同一快照，中间的提交不可见")

    results = {}

    def t2_reader():
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        time.sleep(0.05)
        conn.execute("BEGIN")
        r1 = conn.execute("SELECT balance FROM accounts WHERE id=1").fetchone()[0]
        results['first'] = r1
        print(f"  [T2] 第一次读: balance = {r1}")
        time.sleep(0.3)
        r2 = conn.execute("SELECT balance FROM accounts WHERE id=1").fetchone()[0]
        results['second'] = r2
        print(f"  [T2] 第二次读: balance = {r2}")
        conn.execute("COMMIT")
        conn.close()

    def t1_writer():
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        time.sleep(0.15)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE accounts SET balance=200 WHERE id=1")
        conn.execute("COMMIT")
        print(f"  [T1] 修改并提交: balance = 200")
        conn.close()

    th1 = threading.Thread(target=t1_writer)
    th2 = threading.Thread(target=t2_reader)
    th1.start()
    th2.start()
    th1.join(timeout=5)
    th2.join(timeout=5)

    if results.get('first') == results.get('second'):
        print(f"  结果: ✓ 未发生不可重复读（两次都是 {results['first']}）")
        print(f"  说明: BEGIN...COMMIT 内用同一快照，SQLite 默认快照隔离")
        return True
    else:
        print(f"  结果: ✗ 发生不可重复读")
        return False


def explain_sqlite_isolation():
    print("\n--- SQLite 隔离级别说明 ---")
    print("SQLite 的隔离模型与 SQL 标准不同：")
    print("  - 不支持 SET TRANSACTION ISOLATION LEVEL")
    print("  - journal_mode=DELETE: 串行化（写时全库锁）")
    print("  - journal_mode=WAL: 快照隔离（读不阻塞写）")
    print("  - BEGIN...COMMIT: 整个事务一个快照（≥ Repeatable Read）")
    print("  - autocommit(isolation_level=None): 每条语句独立（≈ Read Committed）")
    print()
    print("要在同一事务内复现不可重复读，需用 PostgreSQL：")
    print("  BEGIN ISOLATION LEVEL READ COMMITTED;")
    print("  SELECT ...; -- 第一次")
    print("  -- [另一事务修改并提交]")
    print("  SELECT ...; -- 第二次，值变了")
    print("  COMMIT;")


def main():
    print("=== 不可重复读演示 ===")

    db_path = os.path.join(tempfile.gettempdir(), "nrr_demo.db")

    setup_db(db_path)
    demonstrate_read_committed_behavior(db_path)

    setup_db(db_path)
    demonstrate_snapshot_isolation(db_path)

    explain_sqlite_isolation()

    print("\n=== 总结 ===")
    print("1. 不可重复读：同一事务内两次读同一行，值不同")
    print("2. Read Committed 下发生（每条语句新快照）")
    print("3. Repeatable Read / 快照隔离下防止（事务固定快照）")
    print("4. SQLite 的 BEGIN 默认提供快照隔离，autocommit 模式类似 RC")
    print("5. 要严格复现需 PostgreSQL（支持 RC 隔离级别）")

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
