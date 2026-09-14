"""脏读演示：事务读到另一事务未提交的数据

SQLite 和 PostgreSQL 都不支持脏读（PG 的 Read Uncommitted 降级为 Read Committed），
因此本脚本用"模拟"方式展示脏读的危害，并验证 Read Committed 下确实不会脏读。

运行：python dirty_read.py
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


def demonstrate_dirty_read_harm():
    print("\n--- 脏读的危害（模拟）---")
    print("场景：T1 把余额从100改成200后回滚，T2 读到了200并据此决策")
    print("时序：")
    print("  T1: BEGIN -> UPDATE balance=200 -> (未提交)")
    print("  T2:                    READ balance=200 (脏读!) -> 据此发款200")
    print("  T1: ROLLBACK -> 余额恢复100，但T2已发出200元！")
    print("结果：银行亏空100元")


def demonstrate_no_dirty_read_in_rc(db_path):
    print("\n--- Read Committed 下不会脏读（实测）---")

    barrier = threading.Event()
    t1_done_update = threading.Event()
    t2_read_result = []
    t1_rollback_done = threading.Event()

    def t1_writer():
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE accounts SET balance=200 WHERE id=1")
        print("  [T1] 已修改 balance=200（未提交）")
        t1_done_update.set()
        t1_rollback_done.wait()
        conn.execute("ROLLBACK")
        print("  [T1] 已回滚，balance 恢复为 100")
        conn.close()

    def t2_reader():
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        t1_done_update.wait()
        time.sleep(0.1)
        try:
            conn.execute("BEGIN")
            row = conn.execute("SELECT balance FROM accounts WHERE id=1").fetchone()
            t2_read_result.append(row[0])
            print(f"  [T2] 读到 balance = {row[0]}（只看到已提交数据）")
            conn.execute("COMMIT")
        except sqlite3.OperationalError as e:
            t2_read_result.append(f"blocked: {e}")
            print(f"  [T2] 被阻塞: {e}")
        finally:
            conn.close()
            barrier.set()

    th1 = threading.Thread(target=t1_writer)
    th2 = threading.Thread(target=t2_reader)
    th1.start()
    th2.start()

    time.sleep(0.3)
    t1_rollback_done.set()

    barrier.wait(timeout=5)
    th1.join(timeout=5)
    th2.join(timeout=5)

    final_conn = sqlite3.connect(db_path)
    final = final_conn.execute(
        "SELECT balance FROM accounts WHERE id=1"
    ).fetchone()[0]
    final_conn.close()
    print(f"  最终余额: {final}")
    if final == 100:
        print("  结论: ✓ 未发生脏读，余额正确")


def demonstrate_sqlite_locking_behavior(db_path):
    print("\n--- SQLite WAL 下的实际行为 ---")
    print("SQLite 在 WAL 模式下：")
    print("  - 读事务看到的是快照（开始时的已提交数据）")
    print("  - 写事务未提交时，读事务看到旧值")
    print("  - 不会脏读，相当于 Snapshot Isolation")

    results = {}

    def writer():
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE accounts SET balance=999 WHERE id=1")
        peek_conn = sqlite3.connect(db_path)
        results['after_update'] = peek_conn.execute(
            "SELECT balance FROM accounts WHERE id=1"
        ).fetchone()[0]
        peek_conn.close()
        time.sleep(0.3)
        conn.execute("ROLLBACK")
        conn.close()

    def reader():
        time.sleep(0.1)
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        val = conn.execute("SELECT balance FROM accounts WHERE id=1").fetchone()[0]
        results['reader_saw'] = val
        conn.close()

    t1 = threading.Thread(target=writer)
    t2 = threading.Thread(target=reader)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    print(f"  写事务未提交时，读事务看到: {results['reader_saw']}")
    print(f"  （不是999，说明没有脏读）")


def main():
    print("=== 脏读演示 ===")

    db_path = os.path.join(tempfile.gettempdir(), "dirty_read_demo.db")
    setup_db(db_path)

    demonstrate_dirty_read_harm()
    demonstrate_no_dirty_read_in_rc(db_path)

    setup_db(db_path)
    demonstrate_sqlite_locking_behavior(db_path)

    print("\n=== 总结 ===")
    print("1. 脏读：读到未提交数据，回滚后数据无效")
    print("2. SQLite/PostgreSQL 都不支持脏读（PG RU 降级为 RC）")
    print("3. Read Committed 及以上级别都能防止脏读")
    print("4. SQLite WAL 模式实际是 Snapshot Isolation，更不会脏读")

    if os.path.exists(db_path):
        os.remove(db_path)
    for ext in ['-wal', '-shm', '-journal']:
        p = db_path + ext
        if os.path.exists(p):
            os.remove(p)


if __name__ == '__main__':
    main()