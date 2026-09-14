"""隔离级别对比：在 SQLite 中对比不同隔离级别的行为

SQLite 通过 journal_mode 和 isolation_level 参数控制隔离行为。
本脚本对比 DEFERRED / IMMEDIATE / EXCLUSIVE 以及 WAL 下的并发表现。

运行：python isolation_levels.py
"""
import sqlite3
import threading
import time
import os
import tempfile


def setup_db(db_path, journal_mode='WAL'):
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute(f"PRAGMA journal_mode={journal_mode}")
    conn.execute("DROP TABLE IF EXISTS counter")
    conn.execute("CREATE TABLE counter(id INTEGER PRIMARY KEY, val INTEGER)")
    conn.execute("INSERT INTO counter VALUES(1, 0)")
    conn.close()
    time.sleep(0.1)


def test_concurrent_increment(db_path, num_threads=10, increments_per_thread=100):
    results = {'success': 0, 'errors': 0, 'final_val': 0}

    def worker():
        conn = sqlite3.connect(db_path, isolation_level=None, timeout=10.0)
        for _ in range(increments_per_thread):
            try:
                conn.execute("BEGIN IMMEDIATE")
                val = conn.execute("SELECT val FROM counter WHERE id=1").fetchone()[0]
                conn.execute("UPDATE counter SET val=? WHERE id=1", (val + 1,))
                conn.execute("COMMIT")
                results['success'] += 1
            except sqlite3.OperationalError:
                results['errors'] += 1
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                time.sleep(0.001)
        conn.close()

    threads = [threading.Thread(target=worker) for _ in range(num_threads)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.time() - t0

    conn = sqlite3.connect(db_path)
    results['final_val'] = conn.execute("SELECT val FROM counter WHERE id=1").fetchone()[0]
    conn.close()
    results['elapsed'] = elapsed
    return results


def test_atomic_update(db_path, num_threads=10, increments_per_thread=100):
    results = {'success': 0, 'errors': 0, 'final_val': 0}

    def worker():
        conn = sqlite3.connect(db_path, isolation_level=None, timeout=10.0)
        for _ in range(increments_per_thread):
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("UPDATE counter SET val=val+1 WHERE id=1")
                conn.execute("COMMIT")
                results['success'] += 1
            except sqlite3.OperationalError:
                results['errors'] += 1
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                time.sleep(0.001)
        conn.close()

    threads = [threading.Thread(target=worker) for _ in range(num_threads)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.time() - t0

    conn = sqlite3.connect(db_path)
    results['final_val'] = conn.execute("SELECT val FROM counter WHERE id=1").fetchone()[0]
    conn.close()
    results['elapsed'] = elapsed
    return results


def test_optimistic_increment(db_path, num_threads=10, increments_per_thread=100):
    results = {'success': 0, 'errors': 0, 'retries': 0, 'final_val': 0}

    def worker():
        conn = sqlite3.connect(db_path, isolation_level=None, timeout=10.0)
        for _ in range(increments_per_thread):
            done = False
            for attempt in range(50):
                try:
                    val = conn.execute("SELECT val FROM counter WHERE id=1").fetchone()[0]
                    conn.execute("BEGIN IMMEDIATE")
                    cur = conn.execute(
                        "UPDATE counter SET val=? WHERE id=1 AND val=?",
                        (val + 1, val)
                    )
                    if cur.rowcount == 1:
                        conn.execute("COMMIT")
                        results['success'] += 1
                        done = True
                        break
                    else:
                        conn.execute("ROLLBACK")
                        results['retries'] += 1
                        time.sleep(0.0005)
                except sqlite3.OperationalError:
                    try:
                        conn.execute("ROLLBACK")
                    except Exception:
                        pass
                    results['retries'] += 1
                    time.sleep(0.001)
            if not done:
                results['errors'] += 1
        conn.close()

    threads = [threading.Thread(target=worker) for _ in range(num_threads)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.time() - t0

    conn = sqlite3.connect(db_path)
    results['final_val'] = conn.execute("SELECT val FROM counter WHERE id=1").fetchone()[0]
    conn.close()
    results['elapsed'] = elapsed
    return results


def demonstrate_isolation_levels_table():
    print("\n--- 四级隔离级别对照表 ---")
    print(f"{'级别':<22} {'脏读':<8} {'不可重复读':<14} {'幻读':<8} {'写偏斜':<8}")
    print("-" * 62)
    print(f"{'Read Uncommitted':<22} {'可能':<8} {'可能':<14} {'可能':<8} {'可能':<8}")
    print(f"{'Read Committed':<22} {'防止':<8} {'可能':<14} {'可能':<8} {'可能':<8}")
    print(f"{'Repeatable Read':<22} {'防止':<8} {'防止':<14} {'可能*':<8} {'可能':<8}")
    print(f"{'Serializable':<22} {'防止':<8} {'防止':<14} {'防止':<8} {'防止':<8}")
    print()
    print("* SQL标准允许，但 PostgreSQL/SI 和 SQLite(WAL) 的 RR 防止幻读")
    print()
    print("SQLite 实际行为：")
    print("  journal_mode=DELETE : 串行化（写时全库锁）")
    print("  journal_mode=WAL    : 快照隔离（读不阻塞写）")
    print("  isolation_level=None: autocommit")
    print("  isolation_level='DEFERRED'  : 延迟加锁")
    print("  isolation_level='IMMEDIATE' : 立即加写锁")
    print("  isolation_level='EXCLUSIVE' : 独占加锁")


def main():
    print("=== 隔离级别对比 ===")

    demonstrate_isolation_levels_table()

    db_path = os.path.join(tempfile.gettempdir(), "isolation_demo.db")
    num_threads = 3
    inc_per_thread = 20
    expected = num_threads * inc_per_thread

    print(f"\n--- 并发自增测试（{num_threads}线程 × {inc_per_thread}次 = 期望{expected}）---")

    setup_db(db_path, 'WAL')
    print("\n[方式1] 读-改-写（非原子，BEGIN IMMEDIATE）:")
    r = test_concurrent_increment(db_path, num_threads, inc_per_thread)
    print(f"  成功: {r['success']}, 错误: {r['errors']}, 最终值: {r['final_val']}, 耗时: {r['elapsed']:.3f}s")
    if r['final_val'] == expected:
        print(f"  ✓ 结果正确（{r['final_val']} == {expected}）")
    else:
        print(f"  ✗ 结果错误（{r['final_val']} != {expected}，丢失{expected - r['final_val']}次更新）")

    setup_db(db_path, 'WAL')
    print("\n[方式2] 原子更新（UPDATE val=val+1，DB保证原子性）:")
    r = test_atomic_update(db_path, num_threads, inc_per_thread)
    print(f"  成功: {r['success']}, 错误: {r['errors']}, 最终值: {r['final_val']}, 耗时: {r['elapsed']:.3f}s")
    if r['final_val'] == expected:
        print(f"  ✓ 结果正确（{r['final_val']} == {expected}）")
    else:
        print(f"  ✗ 结果错误")

    setup_db(db_path, 'WAL')
    print("\n[方式3] 乐观锁（CAS: UPDATE WHERE val=旧值）:")
    r = test_optimistic_increment(db_path, num_threads, inc_per_thread)
    print(f"  成功: {r['success']}, 重试: {r['retries']}, 错误: {r['errors']}, 最终值: {r['final_val']}, 耗时: {r['elapsed']:.3f}s")
    if r['final_val'] == expected:
        print(f"  ✓ 结果正确（{r['final_val']} == {expected}）")
    else:
        print(f"  ✗ 结果错误")

    print("\n--- journal_mode 对比 ---")
    for mode in ['DELETE', 'WAL']:
        setup_db(db_path, mode)
        r = test_atomic_update(db_path, num_threads, inc_per_thread)
        status = "✓" if r['final_val'] == expected else "✗"
        print(f"  {mode:8s}: 最终值={r['final_val']} ({status}), 耗时={r['elapsed']:.3f}s, 错误={r['errors']}")

    print("\n=== 总结 ===")
    print("1. 非原子读-改-写 + BEGIN IMMEDIATE：锁串行化保证正确")
    print("2. 原子 UPDATE val=val+1：DB 保证单语句原子性，最简洁")
    print("3. 乐观锁（CAS）：无死锁，高冲突时重试多、性能差")
    print("4. WAL 模式并发优于 DELETE（回滚日志）模式")
    print("5. SQLite 不支持 SET TRANSACTION ISOLATION LEVEL，通过锁模式控制")

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
