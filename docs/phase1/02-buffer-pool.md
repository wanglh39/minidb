# 章2：Buffer Pool — 缓存管理

> 磁盘比内存慢约 10 万倍。Buffer Pool 让热数据驻留内存，是数据库性能的核心。

## 为什么需要 Buffer Pool？

没有 Buffer Pool 时，每次读页都要做一次磁盘 IO：

```
查询 → pager_read → 磁盘 IO → 返回页
```

有了 Buffer Pool：

```
查询 → bp_fetch_page → 命中缓存？→ 直接返回
                       未命中？→ 从磁盘读入缓存 → 返回
```

## 架构

```
┌──────────────────────────────────────────┐
│              Buffer Pool                  │
│                                           │
│  page_table (哈希: page_id → frame_id)   │
│                                           │
│  frames[0]  frames[1]  ...  frames[N-1]  │
│  ┌──────┐  ┌──────┐       ┌──────┐       │
│  │ page │  │ page │  ...  │ page │       │
│  │ pin=1│  │ pin=0│       │ pin=0│       │
│  │dirty │  │      │       │      │       │
│  └──────┘  └──────┘       └──────┘       │
│                                           │
│  free_list: [空闲frame...]                │
│  replacer:  LRU / Clock / LRU-K           │
└──────────────────────────────────────────┘
         ↕ pager (章1)
         ↕ 磁盘
```

## 三种替换算法

### LRU（最近最少使用）

维护一个按访问顺序排列的链表，淘汰头部（最久未使用）。

```
unpin(0) → [0]
unpin(1) → [0, 1]
unpin(2) → [0, 1, 2]
victim() → 0  (最久未使用)
```

**问题**：全表扫描会把热数据全部挤出缓存（扫描污染）。

### Clock（时钟算法）

每个 frame 有一个 reference bit。unpin 时置 1。victim 时：
- ref=1 → 置 0，跳过（给第二次机会）
- ref=0 → 淘汰

```
unpin(0) ref[0]=1
unpin(1) ref[1]=1
unpin(2) ref[2]=1

victim(): hand→0, ref=1→置0, hand→1, ref=1→置0, hand→2, ref=1→置0
          hand→0, ref=0→淘汰 0
```

**优点**：O(1) 决策，近似 LRU。MySQL InnoDB 用此算法。

### LRU-K（K=2）

记录每个 frame 的最近 K 次访问时间。淘汰策略：
- 优先淘汰访问次数 < K 的 frame（"冷"页）
- 否则淘汰第 K 次访问时间最久远的

```
unpin(0) unpin(0)  → frame 0 访问 2 次
unpin(1)            → frame 1 访问 1 次
victim() → 1  (访问次数 < K，优先淘汰)
```

**优点**：抗扫描污染。PostgreSQL 4.0+ 默认使用。

## fetch_page 工作流程

```
bp_fetch_page(pid):
  1. 查 page_table
     命中 → pin_count++, replacer.pin(), 返回 frame 指针
     未命中 ↓

  2. 找空闲 frame，或 replacer.victim() 淘汰一个
     被淘汰 frame 若 dirty → 写回磁盘

  3. 从磁盘读入该页到 frame
     更新 page_table: pid → frame_id
     pin_count = 1, dirty = false
     replacer.pin()

  4. 返回 frame 指针
```

## pin 机制

**pin_count** 防止正在使用的页被淘汰：

- `bp_fetch_page` → pin_count++
- `bp_unpin_page` → pin_count--
- pin_count > 0 时，frame 不在替换候选集中
- pin_count == 0 时，才加入替换候选集

```c
page_t *p = bp_fetch_page(bp, pid);  // pin_count = 1
// ... 使用 p 读写数据 ...
bp_unpin_page(bp, pid, is_dirty);    // pin_count = 0，可被淘汰
```

## dirty 标记

修改页后必须标记 dirty，淘汰时才会写回磁盘：

```c
page_t *p = bp_fetch_page(bp, pid);
page_add_tuple(p, "new data", 9);    // 修改了页
bp_unpin_page(bp, pid, true);        // true = dirty
```

## page_table 实现

用开放寻址哈希表（page_id → frame_id）：

```c
typedef struct {
    page_id_t pid;
    frame_id_t fid;
    uint8_t state;  // 0=空 1=占用 2=tombstone
} pt_entry_t;
```

- 哈希函数：`pid % capacity`
- 冲突处理：线性探测
- 删除：tombstone 标记（不立即清除，插入时可复用）

## 使用示例

```c
pager_t *pager = pager_open("mydb.dat");
buffer_pool_t *bp = bp_create(pager, 1024, REPLACER_LRU);

// 读取页（自动缓存）
page_t *p = bp_fetch_page(bp, 42);
page_add_tuple(p, "hello", 6);
bp_unpin_page(bp, 42, true);  // dirty

// 分配新页
page_t *new_p;
page_id_t new_pid = bp_new_page(bp, &new_p);
page_add_tuple(new_p, "world", 6);
bp_unpin_page(bp, new_pid, true);

// 查看统计
bp_stats_t stats;
bp_get_stats(bp, &stats);
printf("hits=%d misses=%d evictions=%d\n",
       stats.hits, stats.misses, stats.evictions);

bp_destroy(bp);  // 自动刷所有脏页
pager_close(pager);
```

## 并发说明

本章为**单线程版本**，聚焦缓存逻辑。多线程扩展点：

1. page_table 加读写锁（读多写少）
2. 每个 frame 加读写锁（支持并发读）
3. 替换算法加互斥锁
4. 章6 事务与并发会完整实现

## 设计决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 替换算法 | 三种都实现 | 对比教学，可切换 |
| LRU-K 的 K | 2 | PostgreSQL 默认值 |
| page_table | 开放寻址哈希 | O(1) 查找，教学清晰 |
| 并发 | 单线程 | 渐进式，章6 加锁 |

## 习题

1. 用 Clock 算法时，如果所有 frame 的 ref bit 都是 1，会发生什么？
2. LRU-K 中 K=1 等价于什么算法？K=∞ 呢？
3. 如果 pool_size 等于数据库总页数，替换算法还有意义吗？
4. bp_destroy 时为什么要先 flush_all？如果不刷会怎样？
5. 如何实现"预读"（read-ahead）来提高 Buffer Pool 命中率？

---

上一章：[章1 存储基础](01-storage.md) | 下一章：[章3 B+Tree 索引](03-btree.md)