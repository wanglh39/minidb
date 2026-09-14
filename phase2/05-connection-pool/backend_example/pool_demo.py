"""连接池对比演示：无池 vs 有池、池大小扫描、连接泄漏

用 SQLite + Python 标准库实现，零外部依赖。
"""
import sqlite3
import time
import os
import queue
import threading
import traceback
from contextlib import contextmanager

DB_PATH = "pool_demo.db"
NUM_OPERATIONS = 500
NUM_THREADS = 20


def setup_db():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, val TEXT, ts REAL)")
    for i in range(1000):
        conn.execute("INSERT INTO t(id, val, ts) VALUES(?, ?, ?)",
                     (i, f"value_{i}", time.time()))
    conn.commit()
    conn.close()


class SimplePool:
    def __init__(self, path, size):
        self.path = path
        self.size = size
        self._pool = queue.Queue(maxsize=size)
        self._all = []
        self._checked_out = {}
        self._lock = threading.Lock()
        for _ in range(size):
            conn = sqlite3.connect(path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            self._pool.put(conn)
            self._all.append(conn)

    @contextmanager
    def connection(self):
        conn = self._pool.get()
        with self._lock:
            self._checked_out[id(conn)] = (time.time(), traceback.format_stack())
        try:
            yield conn
        finally:
            with self._lock:
                self._checked_out.pop(id(conn), None)
            self._pool.put(conn)

    def getconn(self):
        conn = self._pool.get()
        with self._lock:
            self._checked_out[id(conn)] = (time.time(), traceback.format_stack())
        return conn

    def getconn_with_timeout(self, timeout=5):
        conn = self._pool.get(timeout=timeout)
        with self._lock:
            self._checked_out[id(conn)] = (time.time(), traceback.format_stack())
        return conn

    def putconn(self, conn):
        with self._lock:
            self._checked_out.pop(id(conn), None)
        self._pool.put(conn)

    def stats(self):
        with self._lock:
            return {"size": self.size, "available": self._pool.qsize(),
                    "checked_out": len(self._checked_out)}

    def leak_report(self, threshold=5):
        now = time.time()
        leaks = []
        with self._lock:
            for cid, (t, stack) in self._checked_out.items():
                if now - t > threshold:
                    leaks.append((cid, now - t, stack))
        return leaks

    def close(self):
        for c in self._all:
            c.close()


def no_pool_worker(ops_per_thread):
    count = 0
    for _ in range(ops_per_thread):
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        row = conn.execute("SELECT val FROM t WHERE id=?", (count % 1000,)).fetchone()
        conn.close()
        count += 1
    return count


def pool_worker(pool, ops_per_thread):
    count = 0
    for _ in range(ops_per_thread):
        with pool.connection() as conn:
            row = conn.execute("SELECT val FROM t WHERE id=?", (count % 1000,)).fetchone()
        count += 1
    return count


def run_threaded(fn, n_threads, *args):
    threads = []
    results = []
    result_lock = threading.Lock()

    def wrapper():
        r = fn(*args)
        with result_lock:
            results.append(r)

    for _ in range(n_threads):
        t = threading.Thread(target=wrapper)
        threads.append(t)
        t.start()
    for t in threads:
        t.join()
    return results


def compare_pool_vs_no_pool():
    print("=== 无连接池 vs 有连接池 ===\n")
    ops_per_thread = NUM_OPERATIONS // NUM_THREADS

    print(f"配置: {NUM_THREADS} 线程 × {ops_per_thread} 操作/线程 = {NUM_OPERATIONS} 操作\n")

    t0 = time.time()
    run_threaded(no_pool_worker, NUM_THREADS, ops_per_thread)
    t_no = time.time() - t0

    pool = SimplePool(DB_PATH, size=10)
    t0 = time.time()
    run_threaded(pool_worker, NUM_THREADS, pool, ops_per_thread)
    t_pool = time.time() - t0
    pool.close()

    print(f"{'方案':<20} {'耗时(ms)':>10} {'吞吐(op/s)':>12} {'加速比':>10}")
    print("-" * 55)
    print(f"{'无连接池':<20} {t_no*1000:>10.1f} {NUM_OPERATIONS/t_no:>12.0f} {1.0:>10.2f}x")
    print(f"{'有连接池(10)':<20} {t_pool*1000:>10.1f} {NUM_OPERATIONS/t_pool:>12.0f} "
          f"{t_no/t_pool:>10.2f}x")
    print()
    return t_no, t_pool


def sweep_pool_size():
    print("=== 池大小对性能的影响 ===\n")
    sizes = [1, 2, 4, 8, 10, 15, 20, 30, 50]
    ops_per_thread = NUM_OPERATIONS // NUM_THREADS

    print(f"{'池大小':>8} {'耗时(ms)':>10} {'吞吐(op/s)':>12} {'可用(结束时)':>14}")
    print("-" * 50)

    results = []
    for size in sizes:
        pool = SimplePool(DB_PATH, size=size)
        t0 = time.time()
        run_threaded(pool_worker, NUM_THREADS, pool, ops_per_thread)
        elapsed = time.time() - t0
        stats = pool.stats()
        pool.close()
        throughput = NUM_OPERATIONS / elapsed
        results.append((size, elapsed, throughput))
        print(f"{size:>8} {elapsed*1000:>10.1f} {throughput:>12.0f} {stats['available']:>14}")

    print()
    best = max(results, key=lambda x: x[2])
    print(f"最佳池大小: {best[0]}（吞吐 {best[2]:.0f} op/s）")
    print(f"线程数: {NUM_THREADS}，最佳池大小通常 ≈ 瓶颈资源数（此处为磁盘 I/O 并发度）\n")
    return results


def demonstrate_leak():
    print("=== 连接泄漏演示 ===\n")

    pool = SimplePool(DB_PATH, size=5)
    print(f"初始状态: {pool.stats()}")

    print("\n--- 正常使用（借了就还）---")
    for i in range(3):
        with pool.connection() as conn:
            conn.execute("SELECT val FROM t WHERE id=?", (i,)).fetchone()
    print(f"3 次正常使用后: {pool.stats()}")

    print("\n--- 制造泄漏（借了不还）---")
    leaked_conns = []
    for i in range(3):
        conn = pool.getconn()
        leaked_conns.append(conn)
        conn.execute("SELECT val FROM t WHERE id=?", (i,)).fetchone()
        print(f"借出连接 #{i+1} 但不归还 → {pool.stats()}")

    leaks = pool.leak_report(threshold=0)
    print(f"\n泄漏检测（threshold=0s）: 发现 {len(leaks)} 个泄漏连接")
    for cid, age, stack in leaks:
        print(f"  连接 {cid}, 借出 {age:.3f}s")
        print(f"  借出位置: {stack[-2].strip()}")

    print("\n--- 归还泄漏的连接 ---")
    for conn in leaked_conns:
        pool.putconn(conn)
    print(f"归还后: {pool.stats()}")

    pool.close()
    print()


def demonstrate_pool_exhaustion():
    print("=== 连接池耗尽演示 ===\n")

    pool = SimplePool(DB_PATH, size=3)
    print(f"池大小: 3")

    conn1 = pool.getconn()
    conn2 = pool.getconn()
    conn3 = pool.getconn()
    print(f"借出 3 个连接: {pool.stats()}")

    print("\n尝试再借（会阻塞 2 秒后超时）...")
    t0 = time.time()
    try:
        conn4 = pool.getconn_with_timeout(timeout=2)
    except queue.Empty:
        elapsed = time.time() - t0
        print(f"超时！等了 {elapsed:.1f}s，池已耗尽")
        print("→ 生产环境应设置 pool_timeout，快速失败而非无限等待")

    pool.putconn(conn1)
    print(f"\n归还 1 个后: {pool.stats()}")
    conn4 = pool.getconn()
    print(f"再次借出成功: {pool.stats()}")

    pool.putconn(conn2)
    pool.putconn(conn3)
    pool.putconn(conn4)
    pool.close()
    print()


def main():
    print("=== 连接池对比演示 ===\n")
    setup_db()

    compare_pool_vs_no_pool()
    sweep_pool_size()
    demonstrate_leak()
    demonstrate_pool_exhaustion()

    print("=== 结论 ===")
    print("1. 连接池避免反复建连，吞吐量显著提升")
    print("2. 池大小有拐点：太小 → 排队等待；太大 → 资源竞争（SQLite 写串行）")
    print("3. 借出必须归还：用 try/finally 或上下文管理器")
    print("4. 设置超时：池耗尽时快速失败，配合监控告警")
    print("5. 泄漏检测：记录借出栈，超时未还则报告")

    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)


if __name__ == "__main__":
    main()