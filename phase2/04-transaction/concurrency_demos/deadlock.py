"""死锁演示：两个事务以相反顺序加锁，形成循环等待

SQLite 的写锁是全库级别（非行级），不会真正死锁，但会锁等待超时。
本脚本复现锁等待超时，并演示固定加锁顺序避免冲突。
PostgreSQL 支持行级锁，可发生真正的死锁（文末说明）。

运行：python deadlock.py
"""
import sqlite3
import threading
import time
import os
import tempfile


def setup_db(db_path):
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("DROP TABLE IF EXISTS accounts")
    conn.execute("CREATE TABLE accounts(id INTEGER PRIMARY KEY, balance INTEGER)")
    conn.execute("INSERT INTO accounts VALUES(1, 100)")
    conn.execute("INSERT INTO accounts VALUES(2, 200)")
    conn.close()
    time.sleep(0.1)


def demonstrate_lock_timeout(db_path):
    print("\n--- 复现锁等待超时（SQLite 全库写锁）---")
    print("T1: BEGIN IMMEDIATE 持有写锁")
    print("T2: BEGIN IMMEDIATE 等待写锁，超时后报错")

    results = {}

    def t1_writer():
        conn = sqlite3.connect(db_path, isolation_level=None, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE accounts SET balance=balance-10 WHERE id=1")
        print("  [T1] 获取写锁，更新 id=1")
        time.sleep(0.5)
        conn.execute("COMMIT")
        print("  [T1] 提交，释放写锁")
        results['t1'] = 'success'
        conn.close()

    def t2_writer():
        time.sleep(0.1)
        conn = sqlite3.connect(db_path, isolation_level=None, timeout=0.3)
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE accounts SET balance=balance-10 WHERE id=2")
            conn.execute("COMMIT")
            print("  [T2] 获取写锁，更新 id=2，提交")
            results['t2'] = 'success'
        except sqlite3.OperationalError as e:
            print(f"  [T2] 锁等待超时: {e}")
            results['t2'] = f'error: {e}'
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
        conn.close()

    th1 = threading.Thread(target=t1_writer)
    th2 = threading.Thread(target=t2_writer)
    th1.start()
    th2.start()
    th1.join(timeout=10)
    th2.join(timeout=10)

    if 'error' in str(results.get('t2', '')):
        print(f"  结果: ✓ 成功复现锁等待超时")
        return True
    else:
        print(f"  结果: 未超时（可能时序未对齐）")
        return False


def demonstrate_sequential_access(db_path):
    print("\n--- 顺序访问避免锁冲突 ---")
    print("T1 先完整执行，T2 等 T1 完成后再执行")

    results = {}

    def t1_writer():
        conn = sqlite3.connect(db_path, isolation_level=None, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE accounts SET balance=balance-10 WHERE id=1")
        print("  [T1] 更新 id=1")
        conn.execute("UPDATE accounts SET balance=balance+10 WHERE id=2")
        print("  [T1] 更新 id=2")
        conn.execute("COMMIT")
        print("  [T1] 提交")
        results['t1'] = 'success'
        conn.close()

    def t2_writer():
        time.sleep(0.3)
        conn = sqlite3.connect(db_path, isolation_level=None, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE accounts SET balance=balance+5 WHERE id=1")
        print("  [T2] 更新 id=1")
        conn.execute("UPDATE accounts SET balance=balance-5 WHERE id=2")
        print("  [T2] 更新 id=2")
        conn.execute("COMMIT")
        print("  [T2] 提交")
        results['t2'] = 'success'
        conn.close()

    th1 = threading.Thread(target=t1_writer)
    th2 = threading.Thread(target=t2_writer)
    th1.start()
    th2.start()
    th1.join(timeout=10)
    th2.join(timeout=10)

    if results.get('t1') == 'success' and results.get('t2') == 'success':
        print(f"  结果: ✓ 两个事务都成功")
        return True
    else:
        print(f"  结果: {results}")
        return False


def demonstrate_busy_timeout(db_path):
    print("\n--- busy_timeout 设置等待超时 ---")

    conn = sqlite3.connect(db_path, isolation_level=None, timeout=2.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=2000")
    bt = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    print(f"  busy_timeout = {bt} ms")
    conn.close()
    print("  说明：等待方会阻塞等待，若超时则抛 OperationalError")


def explain_deadlock_conditions():
    print("\n--- 死锁的 Coffman 四条件 ---")
    print("1. 互斥：资源同一时刻只能被一个事务持有")
    print("2. 持有并等待：持有部分锁的同时请求新锁")
    print("3. 不可抢占：不能强行夺走别人的锁")
    print("4. 循环等待：存在事务的环形等待链")
    print()
    print("破坏任一条件即可避免死锁：")
    print("  破坏2：一次性请求所有锁")
    print("  破坏4：固定加锁顺序")


def explain_pg_vs_sqlite():
    print("\n--- PostgreSQL vs SQLite 死锁处理 ---")
    print("SQLite：全库写锁，不会真正死锁，只有锁等待超时")
    print("PostgreSQL：行级锁，可发生真正死锁")
    print()
    print("PostgreSQL 死锁示例：")
    print("  T1: UPDATE accounts SET ... WHERE id=1;  -- 锁住行1")
    print("  T2: UPDATE accounts SET ... WHERE id=2;  -- 锁住行2")
    print("  T1: UPDATE accounts SET ... WHERE id=2;  -- 等待T2的行2锁")
    print("  T2: UPDATE accounts SET ... WHERE id=1;  -- 等待T1的行1锁")
    print("  → PostgreSQL 检测到循环等待，abort 一方")
    print()
    print("PostgreSQL 排查命令：")
    print("  SELECT * FROM pg_stat_activity WHERE state='active';")
    print("  SELECT * FROM pg_locks WHERE NOT granted;")


def main():
    print("=== 死锁演示 ===")

    db_path = os.path.join(tempfile.gettempdir(), "deadlock_demo.db")

    setup_db(db_path)
    demonstrate_lock_timeout(db_path)

    setup_db(db_path)
    demonstrate_sequential_access(db_path)

    demonstrate_busy_timeout(db_path)
    explain_deadlock_conditions()
    explain_pg_vs_sqlite()

    print("\n=== 总结 ===")
    print("1. 死锁：循环等待，事务互相阻塞")
    print("2. SQLite 全库写锁，不会真正死锁，只有锁等待超时")
    print("3. PostgreSQL 行级锁，可死锁，等待图检测自动 abort")
    print("4. 避免策略：固定加锁顺序、一次性加锁、短事务")

    try:
        os.remove(db_path)
    except Exception:
        pass
    for ext in ['-wal', '-shm', '-journal']:
        p = db_path + ext
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


if __name__ == '__main__':
    main()
