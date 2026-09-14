"""写偏斜演示：两个事务各自读取重叠数据集，各自修改不相交子集，合起来破坏约束

经典案例：医院排班，至少留1人值班。Alice和Bob各自下班，结果无人值班。
快照隔离下复现，悲观锁（BEGIN IMMEDIATE 串行化）下防止。

运行：python write_skew.py
"""
import sqlite3
import threading
import time
import os
import tempfile


def setup_db(db_path):
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("DROP TABLE IF EXISTS doctors")
    conn.execute("CREATE TABLE doctors(name TEXT PRIMARY KEY, on_call INTEGER)")
    conn.execute("INSERT INTO doctors VALUES('Alice', 1)")
    conn.execute("INSERT INTO doctors VALUES('Bob', 1)")
    conn.close()
    time.sleep(0.1)


def check_constraint(db_path):
    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT count(*) FROM doctors WHERE on_call=1").fetchone()[0]
    conn.close()
    return count


def demonstrate_write_skew(db_path):
    print("\n--- 快照隔离下复现写偏斜 ---")
    print("约束：至少1人值班（count(on_call=1) >= 1）")
    print("初始：Alice=值班, Bob=值班")
    print("时序：T2先读快照(count=2) → T1完整执行(Alice下班) → T2基于旧快照写(Bob下班)")

    results = {}

    def t2_bob_off():
        conn = sqlite3.connect(db_path, isolation_level=None, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        count = conn.execute("SELECT count(*) FROM doctors WHERE on_call=1").fetchone()[0]
        results['t2_count'] = count
        print(f"  [T2] 查询值班人数: {count} (>=2，可以下班)")
        time.sleep(0.4)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE doctors SET on_call=0 WHERE name='Bob'")
        conn.execute("COMMIT")
        print(f"  [T2] Bob 下班，提交（基于旧快照count={count}的决策）")
        conn.close()

    def t1_alice_off():
        time.sleep(0.1)
        conn = sqlite3.connect(db_path, isolation_level=None, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN IMMEDIATE")
        count = conn.execute("SELECT count(*) FROM doctors WHERE on_call=1").fetchone()[0]
        results['t1_count'] = count
        print(f"  [T1] 查询值班人数: {count} (>=2，可以下班)")
        conn.execute("UPDATE doctors SET on_call=0 WHERE name='Alice'")
        conn.execute("COMMIT")
        print(f"  [T1] Alice 下班，提交")
        conn.close()

    th1 = threading.Thread(target=t1_alice_off)
    th2 = threading.Thread(target=t2_bob_off)
    th1.start()
    th2.start()
    th1.join(timeout=10)
    th2.join(timeout=10)

    final_count = check_constraint(db_path)
    print(f"  最终值班人数: {final_count}")
    if final_count == 0:
        print(f"  结果: ✗ 发生写偏斜！无人值班，约束被破坏")
        return True
    else:
        print(f"  结果: ✓ 约束保持")
        return False


def demonstrate_pessimistic_lock(db_path):
    print("\n--- 悲观锁（BEGIN IMMEDIATE 串行化）防止写偏斜 ---")
    print("用 BEGIN IMMEDIATE 获取写锁，强制事务串行执行")

    results = {}

    def t1_alice_off():
        conn = sqlite3.connect(db_path, isolation_level=None, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            conn.execute("BEGIN IMMEDIATE")
            count = conn.execute("SELECT count(*) FROM doctors WHERE on_call=1").fetchone()[0]
            print(f"  [T1] 查询值班人数: {count}")
            time.sleep(0.2)
            if count >= 2:
                conn.execute("UPDATE doctors SET on_call=0 WHERE name='Alice'")
                conn.execute("COMMIT")
                print(f"  [T1] Alice 下班，提交")
                results['t1_action'] = 'off'
            else:
                conn.execute("ROLLBACK")
                print(f"  [T1] 仅剩1人，Alice 不能下班，回滚")
                results['t1_action'] = 'stay'
        except sqlite3.OperationalError as e:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            print(f"  [T1] 被阻塞: {e}")
            results['t1_action'] = 'error'
        conn.close()

    def t2_bob_off():
        conn = sqlite3.connect(db_path, isolation_level=None, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            conn.execute("BEGIN IMMEDIATE")
            count = conn.execute("SELECT count(*) FROM doctors WHERE on_call=1").fetchone()[0]
            print(f"  [T2] 查询值班人数: {count}")
            time.sleep(0.2)
            if count >= 2:
                conn.execute("UPDATE doctors SET on_call=0 WHERE name='Bob'")
                conn.execute("COMMIT")
                print(f"  [T2] Bob 下班，提交")
                results['t2_action'] = 'off'
            else:
                conn.execute("ROLLBACK")
                print(f"  [T2] 仅剩1人，Bob 不能下班，回滚")
                results['t2_action'] = 'stay'
        except sqlite3.OperationalError as e:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            print(f"  [T2] 被阻塞: {e}")
            results['t2_action'] = 'error'
        conn.close()

    th1 = threading.Thread(target=t1_alice_off)
    th2 = threading.Thread(target=t2_bob_off)
    th1.start()
    th2.start()
    th1.join(timeout=15)
    th2.join(timeout=15)

    final_count = check_constraint(db_path)
    print(f"  最终值班人数: {final_count}")
    if final_count >= 1:
        print(f"  结果: ✓ 约束保持，至少1人值班")
        return True
    else:
        print(f"  结果: ✗ 约束被破坏")
        return False


def demonstrate_optimistic_retry(db_path):
    print("\n--- 乐观锁（检查+重试）防止写偏斜 ---")
    print("提交前重新检查约束，不满足则回滚")

    results = {}

    def t1_alice_off():
        conn = sqlite3.connect(db_path, isolation_level=None, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        for attempt in range(3):
            try:
                conn.execute("BEGIN IMMEDIATE")
                count = conn.execute("SELECT count(*) FROM doctors WHERE on_call=1").fetchone()[0]
                if count >= 2:
                    conn.execute("UPDATE doctors SET on_call=0 WHERE name='Alice'")
                    conn.execute("COMMIT")
                    results['t1'] = f'off (attempt {attempt+1})'
                    print(f"  [T1] Alice 下班，提交 (第{attempt+1}次尝试)")
                    break
                else:
                    conn.execute("ROLLBACK")
                    results['t1'] = 'stay'
                    print(f"  [T1] 仅剩{count}人，Alice 留守")
                    break
            except sqlite3.OperationalError:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                time.sleep(0.2)
        conn.close()

    def t2_bob_off():
        conn = sqlite3.connect(db_path, isolation_level=None, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        for attempt in range(3):
            try:
                conn.execute("BEGIN IMMEDIATE")
                count = conn.execute("SELECT count(*) FROM doctors WHERE on_call=1").fetchone()[0]
                if count >= 2:
                    conn.execute("UPDATE doctors SET on_call=0 WHERE name='Bob'")
                    conn.execute("COMMIT")
                    results['t2'] = f'off (attempt {attempt+1})'
                    print(f"  [T2] Bob 下班，提交 (第{attempt+1}次尝试)")
                    break
                else:
                    conn.execute("ROLLBACK")
                    results['t2'] = 'stay'
                    print(f"  [T2] 仅剩{count}人，Bob 留守")
                    break
            except sqlite3.OperationalError:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                time.sleep(0.2)
        conn.close()

    th1 = threading.Thread(target=t1_alice_off)
    th2 = threading.Thread(target=t2_bob_off)
    th1.start()
    th2.start()
    th1.join(timeout=15)
    th2.join(timeout=15)

    final_count = check_constraint(db_path)
    print(f"  最终值班人数: {final_count}")
    if final_count >= 1:
        print(f"  结果: ✓ 约束保持")
        return True
    else:
        print(f"  结果: ✗ 约束被破坏")
        return False


def main():
    print("=== 写偏斜演示 ===")

    db_path = os.path.join(tempfile.gettempdir(), "write_skew_demo.db")

    setup_db(db_path)
    demonstrate_write_skew(db_path)

    setup_db(db_path)
    demonstrate_pessimistic_lock(db_path)

    setup_db(db_path)
    demonstrate_optimistic_retry(db_path)

    print("\n=== 总结 ===")
    print("1. 写偏斜：各自读重叠集、写不相交集，合起来破坏约束")
    print("2. Snapshot Isolation 防不住（无写写冲突）")
    print("3. 防止方法：")
    print("   a. Serializable（PG SSI 检测 rw 依赖环）")
    print("   b. 悲观锁 BEGIN IMMEDIATE / SELECT FOR UPDATE")
    print("   c. 乐观锁（提交前重检约束 + 重试）")
    print("4. SQLite 不支持真 SSI，需用锁或应用层检查")

    try:
        conn = sqlite3.connect(db_path)
        conn.close()
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
