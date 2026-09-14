import sqlite3
import hashlib
import random
import math
import bisect
from dataclasses import dataclass, field
from typing import Optional, Callable

random.seed(42)


def hash_shard(key: int, num_shards: int) -> int:
    h = hashlib.md5(str(key).encode()).hexdigest()
    return int(h, 16) % num_shards


def range_shard(key: int, boundaries: list[int]) -> int:
    for i, bound in enumerate(boundaries):
        if key < bound:
            return i
    return len(boundaries)


class ConsistentHashRing:
    def __init__(self, num_shards: int, num_virtual_nodes: int = 150):
        self.num_shards = num_shards
        self.num_virtual_nodes = num_virtual_nodes
        self.ring: dict[int, int] = {}
        self.sorted_hashes: list[int] = []
        for shard_id in range(num_shards):
            self._add_shard_vnodes(shard_id)

    def _add_shard_vnodes(self, shard_id: int) -> None:
        for vn in range(self.num_virtual_nodes):
            key = f"shard-{shard_id}-vn-{vn}".encode()
            h = int(hashlib.md5(key).hexdigest(), 16)
            if h in self.ring:
                continue
            self.ring[h] = shard_id
            bisect.insort(self.sorted_hashes, h)

    def get_shard(self, key: int) -> int:
        h = int(hashlib.md5(str(key).encode()).hexdigest(), 16)
        idx = bisect.bisect_left(self.sorted_hashes, h)
        if idx == len(self.sorted_hashes):
            idx = 0
        return self.ring[self.sorted_hashes[idx]]

    def add_shard(self, shard_id: int) -> dict[int, int]:
        old_mapping: dict[int, int] = {}
        for h in self.sorted_hashes:
            old_mapping[h] = self.ring[h]
        self._add_shard_vnodes(shard_id)
        self.num_shards += 1
        migration: dict[int, int] = {}
        for h in self.sorted_hashes:
            new_shard = self.ring[h]
            old_shard = old_mapping.get(h, new_shard)
            if old_shard != new_shard and old_shard != shard_id:
                migration[old_shard] = migration.get(old_shard, 0) + 1
        return migration


@dataclass
class ShardStats:
    shard_id: int
    row_count: int = 0
    min_key: Optional[int] = None
    max_key: Optional[int] = None


class ShardedTable:
    def __init__(
        self,
        num_shards: int = 4,
        shard_func: Optional[Callable[[int, int], int]] = None,
    ):
        self.num_shards = num_shards
        self.shard_func = shard_func or hash_shard
        self.shards: list[sqlite3.Connection] = []
        self.stats: list[ShardStats] = []
        for i in range(num_shards):
            conn = sqlite3.connect(":memory:", check_same_thread=False)
            conn.execute(
                "CREATE TABLE events ("
                "id INTEGER PRIMARY KEY, "
                "user_id INTEGER, "
                "event_type TEXT, "
                "amount REAL, "
                "created_at TEXT)"
            )
            conn.execute("CREATE INDEX idx_user ON events(user_id)")
            self.shards.append(conn)
            self.stats.append(ShardStats(shard_id=i))

    def _get_shard(self, user_id: int) -> int:
        return self.shard_func(user_id, self.num_shards)

    def insert(self, id_: int, user_id: int, event_type: str,
               amount: float, created_at: str) -> int:
        shard_id = self._get_shard(user_id)
        self.shards[shard_id].execute(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?)",
            (id_, user_id, event_type, amount, created_at),
        )
        self.shards[shard_id].commit()
        s = self.stats[shard_id]
        s.row_count += 1
        s.min_key = user_id if s.min_key is None else min(s.min_key, user_id)
        s.max_key = user_id if s.max_key is None else max(s.max_key, user_id)
        return shard_id

    def query_single_shard(self, user_id: int) -> list[tuple]:
        shard_id = self._get_shard(user_id)
        return self.shards[shard_id].execute(
            "SELECT * FROM events WHERE user_id = ?", (user_id,)
        ).fetchall()

    def query_all_shards(self, sql: str, params: tuple = ()) -> list[tuple]:
        results: list[tuple] = []
        for shard in self.shards:
            results.extend(shard.execute(sql, params).fetchall())
        return results

    def count_all(self) -> int:
        total = 0
        for shard in self.shards:
            total += shard.execute("SELECT count(*) FROM events").fetchone()[0]
        return total

    def sum_amount(self) -> float:
        total = 0.0
        for shard in self.shards:
            row = shard.execute("SELECT coalesce(sum(amount), 0) FROM events").fetchone()
            total += row[0]
        return total

    def group_by_user(self) -> dict[int, int]:
        result: dict[int, int] = {}
        for shard in self.shards:
            for user_id, cnt in shard.execute(
                "SELECT user_id, count(*) FROM events GROUP BY user_id"
            ).fetchall():
                result[user_id] = result.get(user_id, 0) + cnt
        return result

    def group_by_event_type(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for shard in self.shards:
            for et, cnt in shard.execute(
                "SELECT event_type, count(*) FROM events GROUP BY event_type"
            ).fetchall():
                result[et] = result.get(et, 0) + cnt
        return result

    def distributed_join(self, user_ids: list[int]) -> list[tuple]:
        results: list[tuple] = []
        for uid in user_ids:
            shard_id = self._get_shard(uid)
            rows = self.shards[shard_id].execute(
                "SELECT * FROM events WHERE user_id = ?", (uid,)
            ).fetchall()
            results.extend(rows)
        return results

    def print_distribution(self) -> None:
        print(f"\n分片分布（共 {self.num_shards} 个分片）：")
        print(f"{'分片':>6} {'行数':>8} {'min_key':>10} {'max_key':>10}")
        print("-" * 40)
        for s in self.stats:
            print(f"{s.shard_id:>6} {s.row_count:>8} "
                  f"{str(s.min_key):>10} {str(s.max_key):>10}")
        total = sum(s.row_count for s in self.stats)
        avg = total / self.num_shards
        variance = sum((s.row_count - avg) ** 2 for s in self.stats) / self.num_shards
        stddev = math.sqrt(variance)
        print(f"\n总行数: {total}, 平均: {avg:.0f}, 标准差: {stddev:.2f}")
        if avg > 0:
            print(f"均衡度: {1 - stddev / avg:.4f} (1.0 = 完美均匀)")


def demo_hash_sharding() -> None:
    print("=" * 60)
    print("演示 1：哈希分片")
    print("=" * 60)

    table = ShardedTable(num_shards=4, shard_func=hash_shard)

    for i in range(10000):
        user_id = random.randint(1, 100000)
        event_type = random.choice(["click", "view", "purchase", "login"])
        amount = round(random.uniform(1, 100), 2)
        created_at = f"2024-01-{random.randint(1, 31):02d}"
        table.insert(i, user_id, event_type, amount, created_at)

    table.print_distribution()

    print(f"\n跨分片 COUNT(*): {table.count_all()}")
    print(f"跨分片 SUM(amount): {table.sum_amount():.2f}")

    by_type = table.group_by_event_type()
    print(f"\n按事件类型聚合（下推 + 汇总）:")
    for et, cnt in sorted(by_type.items()):
        print(f"  {et:10s}: {cnt}")

    sample_user = 12345
    rows = table.query_single_shard(sample_user)
    print(f"\n单分片查询 user_id={sample_user}: {len(rows)} 条（路由到分片 {hash_shard(sample_user, 4)}）")


def demo_range_sharding() -> None:
    print("\n" + "=" * 60)
    print("演示 2：范围分片")
    print("=" * 60)

    boundaries = [25000, 50000, 75000]
    table = ShardedTable(
        num_shards=4,
        shard_func=lambda key, n: range_shard(key, boundaries),
    )

    for i in range(10000):
        user_id = random.randint(1, 100000)
        event_type = "purchase"
        amount = round(random.uniform(10, 500), 2)
        created_at = f"2024-02-{random.randint(1, 28):02d}"
        table.insert(i, user_id, event_type, amount, created_at)

    table.print_distribution()
    print("\n范围分片适合范围查询，但可能分布不均")


def demo_consistent_hash() -> None:
    print("\n" + "=" * 60)
    print("演示 3：一致性哈希（扩容时最小数据迁移）")
    print("=" * 60)

    ring = ConsistentHashRing(num_shards=4)
    keys = list(range(10000))
    old_mapping = {k: ring.get_shard(k) for k in keys}

    old_dist: dict[int, int] = {}
    for s in old_mapping.values():
        old_dist[s] = old_dist.get(s, 0) + 1
    print("扩容前（4 分片）分布:")
    for s in sorted(old_dist):
        print(f"  分片 {s}: {old_dist[s]} 条")

    migration = ring.add_shard(shard_id=4)

    new_mapping = {k: ring.get_shard(k) for k in keys}
    moved = sum(1 for k in keys if old_mapping[k] != new_mapping[k])

    new_dist: dict[int, int] = {}
    for s in new_mapping.values():
        new_dist[s] = new_dist.get(s, 0) + 1
    print(f"\n扩容后（5 分片）分布:")
    for s in sorted(new_dist):
        print(f"  分片 {s}: {new_dist[s]} 条")

    print(f"\n迁移数据量: {moved} / {len(keys)} = {moved / len(keys) * 100:.1f}%")
    print(f"理论最优: {1 / 5 * 100:.1f}%（1/n）")


def demo_cross_shard_query() -> None:
    print("\n" + "=" * 60)
    print("演示 4：跨分片聚合查询")
    print("=" * 60)

    table = ShardedTable(num_shards=4)

    for i in range(5000):
        user_id = random.randint(1, 1000)
        event_type = random.choice(["click", "view", "purchase"])
        amount = round(random.uniform(1, 100), 2)
        created_at = f"2024-03-{random.randint(1, 31):02d}"
        table.insert(i, user_id, event_type, amount, created_at)

    print("1) 单分片查询（最快）- WHERE user_id = 42:")
    rows = table.query_single_shard(42)
    print(f"   结果: {len(rows)} 条，仅扫描 1 个分片")

    print("\n2) 跨分片 COUNT(*) - 全表扫描:")
    total = table.count_all()
    print(f"   结果: {total} 条，扫描全部 4 个分片")

    print("\n3) 跨分片 GROUP BY user_id - 聚合下推:")
    by_user = table.group_by_user()
    print(f"   结果: {len(by_user)} 个不同用户")
    top5 = sorted(by_user.items(), key=lambda x: -x[1])[:5]
    print(f"   Top 5 用户: {top5}")

    print("\n4) 跨分片 GROUP BY event_type - 聚合下推:")
    by_type = table.group_by_event_type()
    for et, cnt in sorted(by_type.items()):
        print(f"   {et:10s}: {cnt}")

    print("\n5) 分布式 JOIN（按 user_id 路由）:")
    target_users = [100, 200, 300, 400, 500]
    results = table.distributed_join(target_users)
    print(f"   查询 {len(target_users)} 个用户，共 {len(results)} 条记录")
    print(f"   每个查询只路由到对应分片，无需 redistribute")


def demo_citus_concept() -> None:
    print("\n" + "=" * 60)
    print("演示 5：Citus 分布表概念模拟")
    print("=" * 60)

    print("""
Citus 关键概念：

1. 分布表（Distributed Table）
   SELECT create_distributed_table('events', 'user_id');
   → 按 user_id 哈希分片到所有 Worker 节点

2. 引用表（Reference Table）
   SELECT create_reference_table('users');
   → 每个节点存全量副本，用于 JOIN

3. 本地表（Local Table）
   → 仅存于 Coordinator 节点

查询路由规则：
   - WHERE 含分布键 → 路由到单分片（最快）
   - GROUP BY 分布键 → 聚合下推
   - 跨分片 JOIN → 需要 redistribute（慢）
   - 分布表 JOIN 引用表 → 本地 JOIN（快）
""")

    table = ShardedTable(num_shards=4)

    for i in range(8000):
        user_id = random.randint(1, 5000)
        event_type = random.choice(["click", "purchase"])
        amount = round(random.uniform(1, 50), 2)
        table.insert(i, user_id, event_type, amount, "2024-04-01")

    print("模拟 Citus 查询执行计划：\n")

    print("Q1: SELECT * FROM events WHERE user_id = 123")
    print("  → 路由到单分片（分片 = hash(123) % 4 = "
          f"{hash_shard(123, 4)}）")
    print("  → 等价单机查询，延迟最低\n")

    print("Q2: SELECT count(*) FROM events")
    print("  → fan-out 到 4 个分片: count(*)")
    print("  → Coordinator 汇总: " + str(table.count_all()) + "\n")

    print("Q3: SELECT user_id, count(*) FROM events GROUP BY user_id")
    by_user = table.group_by_user()
    print(f"  → 每分片本地 GROUP BY → 汇总 {len(by_user)} 组\n")

    print("Q4: SELECT e.* FROM events e JOIN users u ON e.user_id = u.user_id")
    print("  → users 是引用表 → 每分片本地 JOIN → 无需网络传输")


def main() -> None:
    demo_hash_sharding()
    demo_range_sharding()
    demo_consistent_hash()
    demo_cross_shard_query()
    demo_citus_concept()
    print("\n所有演示完成。")


if __name__ == "__main__":
    main()