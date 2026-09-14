# 章2：Buffer Pool — 缓存管理

> 磁盘比内存慢约 10 万倍。Buffer Pool 让热数据驻留内存，是数据库性能的核心。
> 本章是 miniDB 性能故事的开端：没有它，数据库每次读页都要做一次磁盘 IO，
> 有了它，绝大多数读请求能在内存里直接命中，性能可以提升几个数量级。

---

## 目录

- [1. 为什么需要 Buffer Pool](#1-为什么需要-buffer-pool)
- [2. 缓存基础理论](#2-缓存基础理论)
- [3. LRU 详解](#3-lru-详解)
- [4. Clock 算法](#4-clock-管法)
- [5. LRU-K](#5-lru-k)
- [6. 三种算法对比](#6-三种算法对比)
- [7. Buffer Pool 实现](#7-buffer-pool-实现)
- [8. 并发考虑](#8-并发考虑)
- [9. 代码逐行解读](#9-代码逐行解读)
- [10. 与真实数据库对比](#10-与真实数据库对比)
- [11. 习题](#11-习题)

---

## 1. 为什么需要 Buffer Pool

### 1.1 一个生活类比：书桌 vs 书架

想象你在写一篇论文，需要查阅很多参考书：

```
┌─────────────────────────────────────────────────────────────┐
│                                                             │
│   📚 书架（容量大，但远，拿一次很慢）                        │
│   ┌──────────────────────────────────────────────┐         │
│   │ 书A  书B  书C  书D  书E  书F  书G  ... 书Z    │         │
│   └──────────────────────────────────────────────┘         │
│          ↑                                                   │
│          │ 走过去拿（慢，类似磁盘 IO）                       │
│          │                                                   │
│   📖 书桌（容量小，但近，伸手就能翻）                        │
│   ┌────────────────────────┐                               │
│   │ 书A  书C  书E           │  ← 你最近在用的几本            │
│   └────────────────────────┘                               │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

- **书架** = 磁盘：容量大（TB 级），但每次拿书要站起来走过去，很慢（毫秒级）。
- **书桌** = 内存：容量小（GB 级），但伸手就能翻，很快（纳秒级）。
- **Buffer Pool** = 你决定把哪几本书放在书桌上的策略。

如果你每查一个资料都要去书架拿一次书，写论文的效率会极低。
聪明的做法是：把**经常要查的几本书**先拿到书桌上，下次直接翻书桌上的就行。
这就是 Buffer Pool 在做的事情。

### 1.2 没有 Buffer Pool 时的世界

没有 Buffer Pool 时，每次读页都要做一次磁盘 IO：

```
查询 "SELECT * FROM users WHERE id = 42"
        │
        ▼
   pager_read(page_id=42)
        │
        ▼
   ┌─────────────┐
   │  磁盘 IO     │  ← 慢！大约 10ms（机械硬盘）/ 0.1ms（SSD）
   │  seek+read  │
   └─────────────┘
        │
        ▼
   返回 page 42 的内容
```

问题在于：如果你下一秒又查 `id = 42`，**又要做一次磁盘 IO**！
明明刚才读过，为什么还要再去磁盘拿一遍？这就是没有缓存的痛。

### 1.3 有了 Buffer Pool 之后

```
查询 "SELECT * FROM users WHERE id = 42"
        │
        ▼
   bp_fetch_page(page_id=42)
        │
        ├─ 命中缓存？─ 是 ─→ 直接返回内存中的页 ✅ 快！（纳秒级）
        │
        └─ 未命中？── 否 ─→ 从磁盘读入缓存 → 返回
                            （这一次慢，但下次就快了）
```

关键洞察：**第一次读还是慢的，但第二次、第三次……都快了**。
数据库的工作负载里，同一页被反复访问是常态（比如索引根节点、热点数据行）。

### 1.4 直读写文件的三大问题

如果直接读写文件，不做任何缓存管理，会有三个严重问题：

| 问题 | 说明 | 后果 |
|------|------|------|
| **慢** | 每次读都走磁盘 | 无法支撑高并发查询 |
| **无缓存** | 重复读同一页也要再 IO | 浪费 IO 带宽 |
| **无管理** | 不知道哪些页该留、哪些该换 | 内存用满后随机淘汰，命中率低 |

Buffer Pool 一举解决这三个问题：
1. **慢** → 命中缓存时直接返回内存页，快 10 万倍。
2. **无缓存** → 用 page_table 记录"哪些页已经在内存里"，避免重复 IO。
3. **无管理** → 用替换算法（LRU / Clock / LRU-K）决定淘汰谁，让热数据留下。

### 1.5 性能差距有多大？

来看一组直观的数字（以 4KB 页为例）：

```
┌──────────────────────────────────────────────────────────┐
│  操作                          耗时（数量级）            │
├──────────────────────────────────────────────────────────┤
│  内存读取（缓存命中）          ~100 ns                   │
│  SSD 读取（缓存未命中）        ~100 μs   （慢 1000 倍）  │
│  机械硬盘读取（缓存未命中）    ~10 ms    （慢 10 万倍）  │
└──────────────────────────────────────────────────────────┘
```

假设一个查询要读 100 个页：

- **全命中缓存**：100 × 100ns = 10 微秒，瞬间完成。
- **全未命中（SSD）**：100 × 100μs = 10 毫秒，慢 1000 倍。
- **全未命中（HDD）**：100 × 10ms = 1 秒，慢 10 万倍！

这就是为什么**缓存命中率**是数据库性能的第一指标。后面会详细讲。

### 1.6 小结

```
┌─────────────────────────────────────────────────────────┐
│  Buffer Pool 的三个职责：                                │
│                                                          │
│  1. 缓存：把磁盘上的页读入内存，避免重复 IO              │
│  2. 查找：用哈希表快速判断"这个页在不在内存里"          │
│  3. 替换：内存满了时，决定淘汰哪个页（替换算法）        │
│                                                          │
│  没有它，数据库每次读都要走磁盘，慢得无法用。           │
└─────────────────────────────────────────────────────────┘
```

---

## 2. 缓存基础理论

在深入 LRU、Clock、LRU-K 这些具体算法之前，我们先理解缓存背后的基本理论。
理论搞懂了，所有算法都是同一套思想的不同变体。

### 2.1 局部性原理（Principle of Locality）

局部性是所有缓存能够生效的**物理基础**。它有两面：

#### 时间局部性（Temporal Locality）

> 刚被访问的数据，很可能很快再次被访问。

例子：一个循环里反复读写同一个变量 `i`：

```c
for (int i = 0; i < 1000; i++) {
    sum += array[i];  // sum 被访问 1000 次
}
```

`sum` 第一次被访问后，CPU 缓存、数据库 Buffer Pool 都会把它留下，
因为接下来 999 次访问都能命中缓存。

在数据库里：一个事务里反复更新同一行数据，或者反复查同一个索引节点，
都是时间局部性的体现。

```
访问序列: A B A A C A B A
                ↑       ↑
            刚访问过A   又访问A（时间局部性）
```

#### 空间局部性（Spatial Locality）

> 被访问数据附近的数据，很可能很快也被访问。

例子：顺序扫描一个数组：

```c
for (int i = 0; i < 1000; i++) {
    sum += array[i];  // 访问 array[0], array[1], array[2]...
}
```

访问 `array[0]` 后，`array[1]`、`array[2]` 很快也会被访问。
所以 CPU 会把整个缓存行（通常 64 字节）都加载进来。

在数据库里：顺序扫描一张表时，页 0、页 1、页 2 会依次被访问。
所以很多数据库会做**预读**（read-ahead）：读页 0 时顺便把页 1、2、3 也读进来。

```
访问序列: page0 page1 page2 page3
                ↑     ↑
            相邻的页，很快也会被访问（空间局部性）
```

#### 局部性是缓存的前提

如果没有局部性（每次访问的都是随机的不重复的数据），缓存就毫无用处：
你缓存什么，什么就不再被访问。好在**真实工作负载几乎都有局部性**，
这就是缓存能大幅提升性能的根本原因。

```
┌─────────────────────────────────────────────────────────┐
│  有局部性 → 缓存命中率高 → 性能好                        │
│  无局部性 → 缓存命中率低 → 缓存形同虚设                 │
│                                                          │
│  替换算法的目标：利用局部性，把"未来还会被访问"的页留下  │
└─────────────────────────────────────────────────────────┘
```

### 2.2 缓存命中率（Cache Hit Rate）

缓存命中率 = 命中次数 / (命中次数 + 未命中次数)

```
命中率 = hits / (hits + misses)
```

例：读 100 次页，90 次命中缓存，10 次未命中：

```
命中率 = 90 / (90 + 10) = 90%
```

#### 命中率对性能的影响

命中率的小幅变化，会带来性能的巨大差异。来看一个简单的模型：

设：
- 缓存命中耗时 `t_hit = 0.1 μs`（内存访问）
- 缓存未命中耗时 `t_miss = 100 μs`（SSD 读取）
- 命中率 = `h`

则平均每次访问耗时：

```
t_avg = h × t_hit + (1 - h) × t_miss
```

代入不同命中率：

| 命中率 h | 平均耗时 t_avg | 相对全命中 |
|----------|----------------|------------|
| 100% | 0.1 μs | 1× |
| 99% | 1.1 μs | 11× |
| 95% | 5.1 μs | 51× |
| 90% | 10.1 μs | 101× |
| 80% | 20.1 μs | 201× |
| 50% | 50.1 μs | 501× |
| 0% | 100 μs | 1000× |

**关键洞察**：命中率从 99% 掉到 95%，看起来只掉了 4 个百分点，
但平均耗时慢了将近 5 倍！这就是为什么数据库拼命优化缓存。

```
┌─────────────────────────────────────────────────────────┐
│  缓存命中率的"陡峭悬崖"：                                │
│                                                          │
│  耗时                                                    │
│   ↑                                                      │
│   │            ┌─── 99% 以下性能急剧恶化                 │
│   │           /                                          │
│   │         /                                            │
│   │       /                                              │
│   │     /                                                │
│   │___/__________________________________ 命中率 →       │
│   0%        80%   90%   95%   99%  100%                  │
└─────────────────────────────────────────────────────────┘
```

### 2.3 为什么缓存如此重要

把上面的理论综合起来，缓存重要的原因可以归纳为三点：

1. **磁盘和内存的速度差是 10 万倍**，这个差距不会消失（物理决定）。
2. **真实工作负载有局部性**，所以缓存命中率可以做到 90%+。
3. **命中率对性能是指数级敏感**，90% 和 99% 性能差 10 倍。

所以：**缓存是数据库性能的第一性原理**。几乎所有数据库优化的第一步，
都是看缓存命中率够不够高。

### 2.4 替换算法要解决的核心问题

内存是有限的，磁盘是几乎无限的。当内存装满了，又有新页要读进来，
就必须淘汰一个旧页。**淘汰谁？** 这就是替换算法要回答的问题。

```
┌─────────────────────────────────────────────────────────┐
│  替换算法的核心问题：                                     │
│                                                          │
│  内存满了，要读入新页 X，应该淘汰哪个旧页？              │
│                                                          │
│  理想答案：淘汰"未来最久不会被访问"的页（Bélády 算法）  │
│  但未来无法预知，所以只能根据"过去"来猜测"未来"          │
│                                                          │
│  不同算法 = 不同的猜测策略                                │
└─────────────────────────────────────────────────────────┘
```

#### Bélády 最优算法（理论上的天花板）

1966 年 László Bélády 证明：淘汰"未来最久不被访问"的页，命中率最高。
这叫 **Bélády 算法**（或 MIN 算法）。

问题是：它需要知道**未来的访问序列**，现实中不可能做到。
所以 Bélády 算法只是一个**理论基准**，用来衡量其他算法有多接近最优。

```
┌──────────────────────────────────────────────────┐
│  算法           命中率（同一工作负载）            │
├──────────────────────────────────────────────────┤
│  Bélády (最优)  100%  ← 理论上限，不可实现       │
│  LRU-K          ~95%  ← 接近最优                 │
│  LRU            ~90%  ← 实用，但怕扫描污染       │
│  Clock          ~88%  ← LRU 的廉价近似           │
│  FIFO           ~70%  ← 很差，几乎没人用         │
│  Random         ~60%  ← 最差，纯随机淘汰         │
└──────────────────────────────────────────────────┘
```

（以上数字是示意，实际命中率取决于工作负载。）

接下来我们逐一讲解 miniDB 实现的三种算法：LRU、Clock、LRU-K。

---

## 3. LRU 详解

LRU（Least Recently Used，最近最少使用）是最经典的替换算法。
它的思想非常直观：**淘汰最久没被访问的页**。

### 3.1 LRU 的核心思想

回到书桌的类比：你的书桌只能放 3 本书，现在桌上有：

```
书桌（容量 3）：
┌──────────────────────────┐
│  书A  书B  书C            │
│  最近读过 ←──→ 最久没读   │
└──────────────────────────┘
```

- 你刚翻过书A，书B是之前翻的，书C是最早翻的、已经很久没碰了。
- 现在要拿书D，桌子满了，**把最久没读的书C放回书架**，腾位置给书D。

这就是 LRU：**按"最后一次访问时间"排序，淘汰最老的**。

### 3.2 LRU 的操作规则

LRU 维护一个**按访问时间排序**的链表：

- **访问某页**（pin / 命中）：把该页移到链表**头部**（最近使用）。
- **淘汰**（victim）：取链表**尾部**（最久未使用）的页，移除它。

```
链表头部 = 最近使用          链表尾部 = 最久未使用

[最近] ← A ← B ← C ← D ← E [最久]
  ↑                           ↑
  新访问的页放这里             淘汰从这里取
```

注意：miniDB 的实现里 `lru_order[0]` 是最久未使用（victim），
`lru_order[size-1]` 是最近使用（新 unpin 的加在末尾）。方向相反，但逻辑一样。

### 3.3 一个完整的 LRU 演示

假设缓存容量 = 3，访问序列：A B C A D

**第 1 步：访问 A（未命中，读入）**

```
lru_order: [A]
              ↑ 最久=最近
```

**第 2 步：访问 B（未命中，读入）**

```
lru_order: [A, B]
            ↑最久  ↑最近
```

**第 3 步：访问 C（未命中，读入）**

```
lru_order: [A, B, C]
            ↑最久     ↑最近
```

**第 4 步：访问 A（命中！）**

A 已经在缓存里，把它移到最近位置。miniDB 的实现是 pin 时从链表移除，
unpin 时加到末尾，效果一样：

```
lru_order: [B, C, A]    （A 被重新放到最近位置）
            ↑最久     ↑最近
```

**第 5 步：访问 D（未命中，需要淘汰）**

victim = lru_order[0] = B（最久未使用），淘汰 B，读入 D：

```
淘汰前: [B, C, A]
         ↑淘汰这个
淘汰后: [C, A, D]
         ↑最久     ↑最近
```

最终缓存里是 C、A、D。B 被淘汰了，因为它是最久没被访问的。

### 3.4 链表 + 哈希表实现（标准做法）

朴素的链表实现有个问题：**把某页移到头部，需要先在链表里找到它**，
链表查找是 O(n) 的。为了 O(1) 查找，加一个哈希表：

```
┌─────────────────────────────────────────────────────────┐
│  LRU = 双向链表 + 哈希表                                 │
│                                                          │
│  哈希表: page_id → 链表节点指针                          │
│                                                          │
│  链表:  head ← node ← node ← node ← tail                │
│         最近使用                最久未使用               │
│                                                          │
│  操作复杂度（都是 O(1)）：                               │
│    访问: 哈希表查到节点 → 链表移到头部  = O(1)          │
│    淘汰: 取 tail → 哈希表删除           = O(1)          │
│    插入: 链表头部插入 → 哈希表添加      = O(1)          │
└─────────────────────────────────────────────────────────┘
```

图解一次"访问已有页 B"的过程：

```
访问前:
  哈希表: {A→nodeA, B→nodeB, C→nodeC}
  链表:   head ← A ← B ← C ← tail

第1步: 哈希表查 B → 得到 nodeB          O(1)
第2步: 从链表摘出 nodeB                 O(1)（双向链表）
第3步: 把 nodeB 插到 head               O(1)

访问后:
  哈希表: {A→nodeA, B→nodeB, C→nodeC}  （不变）
  链表:   head ← B ← A ← C ← tail      （B 在最前了）
```

### 3.5 miniDB 的 LRU 实现（简化版）

miniDB 为了教学清晰，没有用双向链表+哈希表，而是用**数组**模拟链表。
复杂度是 O(n)，但代码更简单易懂：

```c
// replacer.c 中的 LRU 相关字段
struct replacer {
    bool *in_set;          // in_set[fid] = 该 frame 是否在替换候选集中
    int size;              // 候选集大小
    frame_id_t *lru_order; // 数组：lru_order[0]=最久未使用, [size-1]=最近使用
    ...
};
```

#### unpin 操作（加入候选集）

```c
// replacer.c:157-159
case REPLACER_LRU:
    r->lru_order[r->size - 1] = fid;  // 新 unpin 的页放数组末尾（最近使用）
    break;
```

图解：

```
unpin(C) 之前:
  lru_order: [A, B]    size=2

unpin(C) 之后:
  lru_order: [A, B, C]  size=3
                      ↑ C 加在末尾（最近使用位置）
```

#### pin 操作（从候选集移除）

```c
// replacer.c:142-144
if (r->type == REPLACER_LRU) {
    lru_remove(r, fid);  // 从 lru_order 数组中删除 fid
}
r->in_set[fid] = false;
r->size--;
```

`lru_remove` 是一个朴素的数组删除（O(n)）：

```c
// replacer.c:64-73
static void lru_remove(replacer_t *r, frame_id_t fid) {
    for (int i = 0; i < r->size; i++) {
        if (r->lru_order[i] == fid) {
            memmove(&r->lru_order[i], &r->lru_order[i + 1],
                    (r->size - i - 1) * sizeof(frame_id_t));
            r->size--;
            return;
        }
    }
}
```

图解 `lru_remove(B)`：

```
删除前: lru_order = [A, B, C, D]   size=4
                i=1

memmove: 把 [C, D] 往前移一格
        lru_order = [A, C, D, D]   size=3
                                 ↑ 末尾的 D 是残留，但 size=3 不会访问到
```

#### victim 操作（淘汰最久未使用）

```c
// replacer.c:75-81
static frame_id_t lru_victim(replacer_t *r) {
    if (r->size == 0) return INVALID_FRAME_ID;
    frame_id_t victim = r->lru_order[0];  // 数组头部 = 最久未使用
    lru_remove(r, victim);                // 从数组移除
    r->in_set[victim] = false;            // 标记不在候选集
    return victim;
}
```

图解：

```
victim() 之前:
  lru_order = [A, B, C]   size=3
               ↑ 取这个（最久未使用）

victim() 之后:
  lru_order = [B, C]      size=2
  返回 A
```

### 3.6 LRU 的问题：扫描污染

LRU 看起来很完美，但有一个致命弱点：**全表扫描会冲掉所有热数据**。

假设缓存容量 = 3，热数据是 A、B、C，现在做一次全表扫描读 X、Y、Z：

```
初始:   [A, B, C]   ← 热数据都在

扫描 X: [B, C, X]   ← A 被淘汰！
扫描 Y: [C, X, Y]   ← B 被淘汰！
扫描 Z: [X, Y, Z]   ← C 被淘汰！热数据全没了！

现在查 A: 未命中，又要去磁盘读 😱
```

这就是**扫描污染**（scan pollution）：一次冷数据扫描，把热数据全冲走了。
后续查询命中率暴跌，性能急剧下降。

这个痛点催生了两个改进算法：
- **Clock**：用"第二次机会"缓解扫描污染，且实现更廉价。
- **LRU-K**：用"历史访问次数"区分冷热，抗扫描污染能力更强。

---

## 4. Clock 算法

Clock（时钟算法）是 LRU 的廉价近似。它用**一个 reference bit** 代替"访问时间排序"，
实现复杂度从 O(n) 降到 O(1)，性能接近 LRU。

### 4.1 Clock 的核心思想：第二次机会（Second Chance）

想象一个圆形的时钟，每个 frame 是时钟上的一个格子。
时钟有一个**指针（hand）**，顺时针转动。

每个 frame 有一个 **reference bit（引用位）**：
- 被访问（unpin）时，ref = 1
- 被淘汰检查时，如果 ref = 1，给它**第二次机会**：ref 置 0，跳过
- 如果 ref = 0，淘汰它

```
┌─────────────────────────────────────────────────────────┐
│  Clock 的直觉：                                          │
│                                                          │
│  ref=1 表示"最近被访问过，可能还热，再给一次机会"        │
│  ref=0 表示"给过机会了，但没再被访问，可以淘汰了"        │
│                                                          │
│  相比 LRU 的精确时间排序，Clock 只记"最近有没有被访问"   │
│  信息少了，但实现简单很多，且效果接近 LRU                │
└─────────────────────────────────────────────────────────┘
```

### 4.2 Clock 的结构图

```
              frame 0 (ref=1)
             ╱
       hand ╱
         ↻  ╱
          ╱
frame 3 ────●──── frame 1
(ref=0)     │      (ref=1)
            │
            │
        frame 2
        (ref=0)
```

- 时针 `hand` 顺时针转动（0 → 1 → 2 → 3 → 0 → ...）
- 每次 victim，从 hand 当前位置开始转，找第一个 ref=0 的 frame 淘汰
- 转的过程中遇到 ref=1 的，置 0（给过第二次机会了）

### 4.3 一个完整的 Clock 演示

容量 = 3，访问序列：A B C A D

**第 1 步：unpin A**

```
ref: [1, -, -]   hand=0
      ↑
     A
```

**第 2 步：unpin B**

```
ref: [1, 1, -]   hand=0
      A  B
```

**第 3 步：unpin C**

```
ref: [1, 1, 1]   hand=0
      A  B  C
```

**第 4 步：访问 A（命中，pin 然后 unpin）**

pin 把 A 从候选集移除，unpin 又加回来并置 ref=1：

```
ref: [1, 1, 1]   hand=0   （A 的 ref 还是 1）
      A  B  C
```

**第 5 步：读入 D，需要 victim()**

时针从 hand=0 开始转：

```
第1轮 hand→0: ref[0]=1 → 置0, 跳过     ref: [0,1,1]
第1轮 hand→1: ref[1]=1 → 置0, 跳过     ref: [0,0,1]
第1轮 hand→2: ref[2]=1 → 置0, 跳过     ref: [0,0,0]
第2轮 hand→0: ref[0]=0 → 淘汰 frame 0 (即 A)！
```

D 读入 frame 0：

```
ref: [1, 0, 0]   hand=1   （D 的 ref=1，hand 停在淘汰位置+1）
      D  B  C
```

### 4.4 miniDB 的 Clock 实现

```c
// replacer.c:85-103
static frame_id_t clock_victim(replacer_t *r) {
    if (r->size == 0) return INVALID_FRAME_ID;

    for (int attempts = 0; attempts < r->capacity * 2; attempts++) {
        frame_id_t fid = r->clock_hand;                          // 当前指针位置
        r->clock_hand = (r->clock_hand + 1) % r->capacity;      // 指针前进一格

        if (!r->in_set[fid]) continue;  // 不在候选集，跳过

        if (r->clock_ref[fid]) {
            r->clock_ref[fid] = false;  // ref=1 → 置0，给第二次机会
        } else {
            r->in_set[fid] = false;     // ref=0 → 淘汰
            r->size--;
            return fid;
        }
    }
    return INVALID_FRAME_ID;  // 转了两圈还没找到（理论上不会发生）
}
```

逐行解读：

| 行 | 代码 | 作用 |
|----|------|------|
| 86 | `if (r->size == 0)` | 候选集空，没有可淘汰的 |
| 88 | `for ... capacity * 2` | 最多转两圈（第一圈全置0，第二圈必能找到） |
| 89 | `fid = r->clock_hand` | 取当前指针位置的 frame |
| 90 | `clock_hand = (hand+1) % capacity` | 指针前进一格（环形） |
| 92 | `if (!in_set[fid]) continue` | 不在候选集（被 pin 了），跳过 |
| 94 | `if (clock_ref[fid])` | ref=1：给第二次机会 |
| 95 | `clock_ref[fid] = false` | 置 0 |
| 97-99 | `else` | ref=0：淘汰这个 frame |

#### unpin 时设置 ref=1

```c
// replacer.c:160-161
case REPLACER_CLOCK:
    r->clock_ref[fid] = true;  // 被 unpin（可被淘汰）时，ref 置 1
    break;
```

### 4.5 为什么最多转两圈？

第一圈：把所有 ref=1 的置 0（给第二次机会）。
第二圈：所有 ref 都是 0 了，必然能找到第一个在候选集里的 frame 淘汰。

```
第1圈: 1→0, 1→0, 1→0   （全部给了第二次机会）
第2圈: 0→淘汰!          （必然成功）
```

所以 `capacity * 2` 次尝试一定能找到 victim（前提是候选集非空）。

### 4.6 Clock vs LRU 性能对比

| 维度 | LRU | Clock |
|------|-----|-------|
| **决策复杂度** | O(n)（miniDB 数组实现）/ O(1)（标准链表+哈希） | O(1)（均摊） |
| **内存开销** | 每页存链表节点指针 | 每页 1 个 bit |
| **命中率** | 较高 | 略低于 LRU（约低 2-5%） |
| **抗扫描污染** | 差 | 略好（第二次机会挡掉一部分） |
| **实现难度** | 中等 | 简单 |
| **谁在用** | 教学常见 | MySQL InnoDB（变种） |

Clock 的最大优势是**实现简单、开销低**，所以在工业界（如 MySQL）更受欢迎。
LRU 虽然命中率略高，但维护链表的开销在并发场景下是个问题。

### 4.7 Clock 的变种

工业界很少用纯 Clock，而是用变种：

- **GClock / GClock**：用计数器代替 1 个 bit，记录"被访问了几次"。
- **2-List Clock**：分热冷两个 Clock，类似 LRU-2 的思想。
- **MySQL InnoDB 的自适应哈希 + Clock**：热点页直接哈希查找，不走 Clock。

miniDB 实现的是最朴素的 Clock，便于理解核心思想。

---

## 5. LRU-K

LRU-K 是 LRU 的改进版，由 Reza O'Neil 等人在 1993 年提出。
核心思想：**不只看"最后一次访问"，而是看"最近 K 次访问"**来判断冷热。

miniDB 采用 K=2，这也是 PostgreSQL 8.0+ 的默认值。

### 5.1 为什么 LRU-K 比 LRU 好？

LRU 只看"最后一次访问时间"，所以一个**只被访问过一次的冷页**，
只要它是最近访问的，就会排在前面，把热页挤掉。

LRU-K（K=2）看"最近两次访问"，能区分：

```
┌─────────────────────────────────────────────────────────┐
│  页 A: 被访问过 10 次，最近一次在 1 秒前   → 热页        │
│  页 B: 被访问过 1 次，最近一次在 0.5 秒前   → 冷页        │
│                                                          │
│  LRU:    B 比 A "更近"使用，淘汰时先淘汰 A 😱            │
│  LRU-2:  B 只访问过 1 次（<K=2），优先淘汰 B ✅          │
└─────────────────────────────────────────────────────────┘
```

这就是 LRU-K 抗扫描污染的关键：**访问次数不到 K 的页被视为"冷页"，优先淘汰**。

### 5.2 LRU-K（K=2）的核心规则

对每个 frame，记录：
- `count`：被访问过几次
- `first`：第 1 次访问的时间戳
- `last`：最近一次访问的时间戳

淘汰策略：

```
┌─────────────────────────────────────────────────────────┐
│  LRU-K (K=2) 淘汰规则：                                  │
│                                                          │
│  1. 优先淘汰 count < K 的 frame（冷页，访问次数不够）    │
│     在这些冷页中，淘汰 first 最小的（最早被访问的冷页）  │
│                                                          │
│  2. 如果所有 frame 的 count >= K（都是热页）             │
│     淘汰 first 最小的（第 K 次访问时间最久远的）         │
└─────────────────────────────────────────────────────────┘
```

### 5.3 历史记录队列图解

每个 frame 维护一个长度为 K 的时间戳队列：

```
K=2, 访问序列: A A B A C

frame A 的时间戳队列:
  访问1: [t0]           count=1, first=t0, last=t0
  访问2: [t0, t1]       count=2, first=t0, last=t1  (满了，保留最近K个)
  访问4: [t1, t3]       count=3, first=t1, last=t3  (滑动窗口)

frame B 的时间戳队列:
  访问3: [t2]           count=1, first=t2, last=t2  (冷页！)

frame C 的时间戳队列:
  访问5: [t4]           count=1, first=t4, last=t4  (冷页！)
```

victim 时：
- B 和 C 的 count=1 < K=2，都是冷页
- 在冷页中，B 的 first=t2 < C 的 first=t4，所以**淘汰 B**

### 5.4 miniDB 的 LRU-K 实现

```c
// replacer.c:107-134
static frame_id_t lruk_victim(replacer_t *r) {
    if (r->size == 0) return INVALID_FRAME_ID;

    frame_id_t victim = INVALID_FRAME_ID;
    uint64_t best_key = UINT64_MAX;

    for (frame_id_t fid = 0; fid < r->capacity; fid++) {
        if (!r->in_set[fid]) continue;  // 不在候选集，跳过

        uint64_t key;
        if (r->lruk_count[fid] < LRU_K) {
            key = r->lruk_first[fid];   // 冷页：用 first 时间戳
        } else {
            key = r->lruk_first[fid];   // 热页：也用 first（第K次访问时间）
        }

        if (key < best_key) {
            best_key = key;
            victim = fid;
        }
    }

    if (victim != INVALID_FRAME_ID) {
        r->in_set[victim] = false;
        r->size--;
    }
    return victim;
}
```

注意：miniDB 的实现里，冷页和热页都用 `first` 作为 key。
这是因为 miniDB 用了简化策略：`first` 在 count<K 时是第一次访问时间，
在 count>=K 时是第 K 次访问时间（通过滑动窗口维护）。
两者统一比较 `first` 最小的淘汰。

#### unpin 时更新历史记录

```c
// replacer.c:163-173
case REPLACER_LRU_K:
    r->lruk_count[fid]++;
    if (r->lruk_count[fid] == 1) {
        r->lruk_first[fid] = r->lruk_time;   // 第一次访问
        r->lruk_last[fid] = r->lruk_time;
    } else {
        r->lruk_first[fid] = r->lruk_last[fid];  // 滑动窗口：first = 上次的 last
        r->lruk_last[fid] = r->lruk_time;         // last = 当前时间
    }
    r->lruk_time++;   // 全局逻辑时钟 +1
    break;
```

图解滑动窗口（K=2）：

```
第1次 unpin(A): count=1, first=t0, last=t0
  队列: [t0]

第2次 unpin(A): count=2, first=t0, last=t1
  队列: [t0, t1]   （first 不变，last 更新）

第3次 unpin(A): count=3, first=t1, last=t2
  队列: [t1, t2]   （滑动！first 变成上次的 last）

第4次 unpin(A): count=4, first=t2, last=t3
  队列: [t2, t3]   （继续滑动）
```

关键：`first` 始终是"倒数第 K 次访问的时间"。
对 K=2，`first` 是"上一次访问的时间"（不是最近一次，是倒数第二次）。

### 5.5 LRU-K 完整演示

容量 = 2，访问序列：A B A C

**第 1 步：unpin A**

```
A: count=1, first=0, last=0    time=1
```

**第 2 步：unpin B**

```
A: count=1, first=0, last=0
B: count=1, first=1, last=1    time=2
```

**第 3 步：unpin A（A 第二次访问，变热页）**

```
A: count=2, first=0, last=2    （first 还是 0，因为这是第2次访问）
B: count=1, first=1, last=1    time=3
```

**第 4 步：读入 C，需要 victim()**

比较 first：
- A: first=0（热页，第2次访问在 t=0）
- B: first=1（冷页，第1次访问在 t=1）

按规则，应该优先淘汰冷页。但 miniDB 统一比较 first：
A 的 first=0 < B 的 first=1，所以**淘汰 A**？

这里 miniDB 的简化实现和标准 LRU-K 有差异。标准 LRU-K 会优先淘汰冷页 B。
miniDB 为了代码简洁，冷热页统一比较 first，结果可能淘汰热页 A。

> ⚠️ **教学说明**：miniDB 的 LRU-K 是简化版，主要展示"历史记录"和"滑动窗口"的思想。
> 生产级 LRU-K（如 PostgreSQL）会严格区分冷热页，并有单独的"冷区"和"热区"。

### 5.6 LRU-K vs LRU：抗扫描污染对比

场景：缓存容量 = 3，热数据 A B C（各被访问 5 次），然后扫描 X Y Z（各访问 1 次）

**LRU 的遭遇：**

```
扫描前: [A, B, C]   ← 热数据
扫描 X: [B, C, X]   ← A 淘汰
扫描 Y: [C, X, Y]   ← B 淘汰
扫描 Z: [X, Y, Z]   ← C 淘汰，热数据全没了！
```

**LRU-2 的表现：**

```
扫描前: A(count=5), B(count=5), C(count=5)   ← 都是热页
扫描 X: X(count=1) 要读入，victim?
        - A,B,C 的 count>=2，X 的 count=1
        - 但 X 还没进缓存，要淘汰一个已有的
        - 淘汰 first 最小的热页（假设是 A）
        [B, C, X]
扫描 Y: Y(count=1) 要读入
        - 淘汰 first 最小的（B 或 C 或 X）
        - X 是冷页(count=1)，first 较大（刚访问）
        - B/C 是热页，first 是倒数第2次访问，可能更小
        - 可能淘汰 B 或 C
```

LRU-2 也会淘汰一些热页，但**冷页 X 一旦成为 victim 候选，会被优先淘汰**，
因为它的 count=1 < K=2。所以扫描结束后，LRU-2 能更快把冷页赶走，恢复热数据。

```
┌─────────────────────────────────────────────────────────┐
│  扫描污染后恢复速度：                                    │
│                                                          │
│  LRU:   热数据全没了，要重新从磁盘读，命中率长期低迷    │
│  LRU-2: 冷页优先淘汰，热数据较快回归，命中率恢复快      │
└─────────────────────────────────────────────────────────┘
```

---

## 6. 三种算法对比

### 6.1 性能对比表

| 维度 | LRU | Clock | LRU-K (K=2) |
|------|-----|-------|-------------|
| **决策复杂度** | O(n)（miniDB）/ O(1)（标准） | O(1) 均摊 | O(n)（miniDB） |
| **每次访问开销** | O(1) 移动到头部 | O(1) 置 ref=1 | O(1) 更新历史 |
| **内存开销/页** | 链表指针（8B） | 1 bit | count+first+last（24B） |
| **命中率（随机负载）** | 高 | 略低 2-5% | 最高 |
| **命中率（扫描负载）** | 差（扫描污染） | 略好 | 好（抗污染） |
| **抗扫描污染** | ❌ 差 | ⚠️ 一般 | ✅ 好 |
| **实现复杂度** | 中等 | 简单 | 较复杂 |
| **并发友好度** | 差（链表操作多） | 好（位操作原子） | 中等 |
| **谁在用** | 教学 / Redis | MySQL InnoDB | PostgreSQL 8.0+ |

### 6.2 命中率随工作负载变化

```
命中率
  ↑
  │  ★★★★★★★★★★★★★★★★★★★★★★★★★  LRU-K
  │  ★★★★★★★★★★★★★★★★★★★★★★★★
  │  ★★★★★★★★★★★★★★★★★★★★★★★
  │  ★★★★★★★★★★★★★★★★★★★★★★
  │  ★★★★★★★★★★★★★★★★★★★★★
  │
  │  ●●●●●●●●●●●●●●●●●●●●●●●●●●  LRU
  │  ●●●●●●●●●●●●●●●●●●●●●●●●●
  │  ●●●●●●●●●●●●●●●●●●●●●●●●
  │
  │  ○○○○○○○○○○○○○○○○○○○○○○○○  Clock
  │  ○○○○○○○○○○○○○○○○○○○○○○○
  │  ○○○○○○○○○○○○○○○○○○○○○○
  │
  └──────────────────────────────────→ 工作负载
     随机    时序局部   扫描    混合
```

- **随机负载**：三者差距小，LRU-K 略胜。
- **时序局部性强的负载**：LRU 和 LRU-K 接近，Clock 略低。
- **扫描负载**：LRU 暴跌，LRU-K 最好，Clock 居中。
- **混合负载**：LRU-K 最稳定。

### 6.3 适用场景

```
┌─────────────────────────────────────────────────────────┐
│  选型决策树：                                            │
│                                                          │
│  你的工作负载是？                                        │
│    │                                                     │
│    ├─ 大量全表扫描（OLAP）                               │
│    │    └─→ LRU-K（抗扫描污染）                          │
│    │                                                     │
│    ├─ 高并发点查（OLTP）                                 │
│    │    └─→ Clock（并发友好，开销低）                    │
│    │                                                     │
│    ├─ 教学 / 简单系统                                    │
│    │    └─→ LRU（直观易懂）                              │
│    │                                                     │
│    └─ 内存极度紧张                                       │
│         └─→ Clock（每页只需 1 bit）                      │
└─────────────────────────────────────────────────────────┘
```

### 6.4 miniDB 为什么三种都实现？

miniDB 是教学项目，三种算法都实现是为了**对比教学**：

```c
// replacer.h:10-14
typedef enum {
    REPLACER_LRU = 0,
    REPLACER_CLOCK = 1,
    REPLACER_LRU_K = 2,
} replacer_type_t;
```

创建 Buffer Pool 时可以指定用哪种：

```c
buffer_pool_t *bp = bp_create(pager, 1024, REPLACER_LRU);     // 用 LRU
buffer_pool_t *bp = bp_create(pager, 1024, REPLACER_CLOCK);   // 用 Clock
buffer_pool_t *bp = bp_create(pager, 1024, REPLACER_LRU_K);   // 用 LRU-K
```

这样你可以用同一个工作负载跑三遍，对比命中率的差异，直观感受算法差异。

---

## 7. Buffer Pool 实现

前面讲了替换算法，现在看 Buffer Pool 如何**整体协调**缓存、查找、替换。

### 7.1 整体架构

```
┌──────────────────────────────────────────────────────┐
│                   Buffer Pool                         │
│                                                       │
│  ┌─────────────────────────────────────────────┐    │
│  │  page_table (哈希表: page_id → frame_id)    │    │
│  │  开放寻址，线性探测                          │    │
│  └─────────────────────────────────────────────┘    │
│                                                       │
│  ┌────────┐  ┌────────┐       ┌────────┐            │
│  │frame 0 │  │frame 1 │  ...  │frame N │            │
│  │────────│  │────────│       │────────│            │
│  │ page   │  │ page   │       │ page   │            │
│  │ pin=1  │  │ pin=0  │       │ pin=0  │            │
│  │ dirty  │  │ clean  │       │ clean  │            │
│  │ pid=42 │  │ pid=7  │       │ pid=99 │            │
│  └────────┘  └────────┘       └────────┘            │
│                                                       │
│  free_list: [空闲的 frame_id...]                      │
│  replacer:  LRU / Clock / LRU-K (管理可淘汰的 frame) │
└──────────────────────────────────────────────────────┘
          ↕ pager (章1：磁盘读写)
          ↕ 磁盘文件
```

### 7.2 核心数据结构

```c
// buffer_pool.c:11-29
struct buffer_pool {
    pager_t *pager;          // 磁盘读写接口
    int pool_size;           // 缓存能放多少页

    page_t    *frames;       // 物理页数组：frames[fid] 是第 fid 个槽位的页内容
    int       *pin_count;    // pin_count[fid] = 该 frame 被几个线程引用
    bool      *dirty;        // dirty[fid] = 该页是否被修改过（淘汰时要写回磁盘）
    page_id_t *frame_pid;    // frame_pid[fid] = 该 frame 当前装的是哪个磁盘页

    pt_entry_t *pt;          // page_table：哈希表
    int pt_cap;              // 哈希表容量（= pool_size * 2）

    replacer_t *repl;        // 替换器（LRU/Clock/LRU-K）

    frame_id_t *free_list;   // 空闲 frame 列表
    int free_count;          // 空闲 frame 数量

    bp_stats_t stats;        // 统计信息（命中、未命中、淘汰、脏写）
};
```

图解这些数组的关系：

```
fid:        0        1        2        3
         ┌──────┬──────┬──────┬──────┐
frames:  │ page │ page │ page │ page │   ← 实际页内容（4KB each）
         ├──────┼──────┼──────┼──────┤
pin_cnt: │  1   │  0   │  2   │  0   │   ← 引用计数
         ├──────┼──────┼──────┼──────┤
dirty:   │ true │false │false │false │   ← 是否脏
         ├──────┼──────┼──────┼──────┤
frame_pid│  42  │  7   │  99  │  --   │   ← 装的是哪个磁盘页
         └──────┴──────┴──────┴──────┘

page_table (哈希):
  pid 42 → fid 0
  pid 7  → fid 1
  pid 99 → fid 2

free_list: []  （没有空闲 frame，都装了页）
replacer 候选集: {fid 1, fid 3}  （pin_count=0 的才能被淘汰）
```

### 7.3 pin / unpin 概念

**pin_count** 是 Buffer Pool 最重要的并发控制机制：
它防止"正在被使用的页"被淘汰掉。

```
┌─────────────────────────────────────────────────────────┐
│  pin_count 的含义：                                      │
│                                                          │
│  pin_count = 0：没人用这个页，可以被淘汰（在替换器中）  │
│  pin_count > 0：有人在用，不能淘汰（不在替换器中）      │
│                                                          │
│  bp_fetch_page → pin_count++  （我要用了，别淘汰它）    │
│  bp_unpin_page → pin_count--  （我用完了，可以淘汰了）  │
└─────────────────────────────────────────────────────────┘
```

为什么需要 pin？想象这个场景：

```
线程1: p = bp_fetch_page(42)    // 读到 frame 0
        // 正在修改 p 的数据...
        //                        线程2: bp_fetch_page(99) 缓存满了
        //                        线程2: victim() → 淘汰 frame 0？！
        //                        线程2: 把 frame 0 写回磁盘，读入页 99
        p->data[0] = 'X'         // 写到了页 99 的内存！数据错乱！
```

有了 pin_count，线程1 fetch 时 pin=1，frame 0 不会被淘汰，悲剧避免。

### 7.4 脏页追踪（dirty flag）

页被读入缓存后，可能被修改。修改后的页和磁盘上的版本不一致，叫**脏页**。
脏页被淘汰时，必须先写回磁盘，否则修改丢失。

```
bp_fetch_page(42)        → dirty=false（从磁盘读的，和磁盘一致）
page_add_tuple(p, ...)   → 修改了内存中的页
bp_unpin_page(42, true)  → dirty=true（标记为脏）
                          → 但不立即写回磁盘！（延迟写）
                          → 淘汰时或 flush 时才写回
```

为什么延迟写（lazy write）？因为如果每次修改都立即写磁盘，
一个页被修改 100 次就要写 100 次磁盘。延迟写让 100 次修改合并成 1 次磁盘写。

```
┌─────────────────────────────────────────────────────────┐
│  脏页写回时机：                                          │
│                                                          │
│  1. 该页被淘汰时（bp_evict 检查 dirty）                  │
│  2. 主动 flush（bp_flush_page / bp_flush_all）           │
│  3. bp_destroy 时（flush_all 所有脏页）                  │
└─────────────────────────────────────────────────────────┘
```

### 7.5 哈希表查找（page_table）

page_table 是一个**开放寻址哈希表**，映射 `page_id → frame_id`。
它的作用是 O(1) 判断"这个页在不在缓存里，在哪个 frame"。

```c
// buffer_pool.c:5-9
typedef struct {
    page_id_t pid;     // 键：磁盘页号
    frame_id_t fid;    // 值：缓存帧号
    uint8_t state;     // 0=空 1=占用 2=tombstone（墓碑）
} pt_entry_t;
```

#### 哈希表结构图

```
pt_cap = 8 (假设)

index:    0     1     2     3     4     5     6     7
       ┌─────┬─────┬─────┬─────┬─────┬─────┬─────┬─────┐
pt:    │空   │占用 │墓碑 │占用 │空   │占用 │空   │占用 │
       │     │pid=1│     │pid=3│     │pid=5│     │pid=7│
       │     │fid=0│     │fid=2│     │fid=1│     │fid=3│
       └─────┴─────┴─────┴─────┴─────┴─────┴─────┴─────┘

查找 pid=3:
  hash(3) = 3 % 8 = 3 → pt[3].pid=3 ✅ 命中，返回 fid=2

查找 pid=11:
  hash(11) = 11 % 8 = 3 → pt[3].pid=3 ≠ 11
  线性探测: pt[4].state=空 → 未找到，返回 INVALID_FRAME_ID
```

#### 三种状态

- **空（state=0）**：这个槽位没存东西，查找遇到空槽就可以停了。
- **占用（state=1）**：存了一个映射。
- **墓碑（state=2）**：被删除了，但不能直接置空（会打断探测链）。

为什么删除要墓碑？看这个例子：

```
插入 pid=3, pid=11, pid=19（都 hash 到 index 3）:
  pt[3]=pid3, pt[4]=pid11, pt[5]=pid19

如果删除 pid=11 时直接置空:
  pt[3]=pid3, pt[4]=空,   pt[5]=pid19

查找 pid=19:
  hash(19)=3 → pt[3].pid=3 ≠ 19
  探测: pt[4]=空 → 停！返回"未找到" 😱 但 pid=19 明明在 pt[5]！

用墓碑:
  pt[3]=pid3, pt[4]=墓碑, pt[5]=pid19

查找 pid=19:
  hash(19)=3 → pt[3].pid=3 ≠ 19
  探测: pt[4]=墓碑 → 继续（不能停）
  探测: pt[5].pid=19 ✅ 命中！
```

### 7.6 替换器协作

Buffer Pool 和替换器（replacer）的协作关系：

```
┌─────────────────────────────────────────────────────────┐
│  Buffer Pool            Replacer (LRU/Clock/LRU-K)      │
│                                                          │
│  pin_count: 1 → 0       unpin(frame) → 加入候选集       │
│  pin_count: 0 → 1       pin(frame)  → 移出候选集        │
│  需要淘汰时             victim()    → 返回一个 frame    │
└─────────────────────────────────────────────────────────┘
```

关键：**只有 pin_count=0 的 frame 才在替换器的候选集里**。
pin_count>0 的 frame 正在被使用，替换器根本看不到它。

### 7.7 fetch_page 完整流程

```
bp_fetch_page(pid):
  │
  ├─ 1. pt_lookup(pid) 查哈希表
  │     │
  │     ├─ 命中（fid 有效）:
  │     │    pin_count[fid]++
  │     │    replacer_pin(fid)     ← 从候选集移除（现在被用了）
  │     │    stats.hits++
  │     │    return &frames[fid]   ← 直接返回内存页
  │     │
  │     └─ 未命中:
  │          stats.misses++
  │          ↓
  │
  ├─ 2. bp_get_frame() 找一个空闲 frame
  │     │
  │     ├─ free_list 非空: 取一个空闲 frame
  │     │
  │     └─ free_list 空: bp_evict()
  │          │
  │          ├─ replacer_victim() → 得到 fid
  │          ├─ if dirty[fid]: pager_write(写回磁盘)
  │          ├─ pt_delete(旧的 pid)
  │          └─ return fid
  │          ↓
  │
  ├─ 3. pager_read(pid, &frames[fid])  ← 从磁盘读入页
  │
  ├─ 4. 更新元数据:
  │     frame_pid[fid] = pid
  │     pin_count[fid] = 1
  │     dirty[fid] = false
  │     pt_insert(pid, fid)
  │     replacer_pin(fid)   ← 刚读入，正在用，移出候选集
  │
  └─ 5. return &frames[fid]
```

---

## 8. 并发考虑

miniDB 的 Buffer Pool 是**单线程版本**，聚焦缓存逻辑。
但真实数据库是多线程的，并发是绕不开的话题。

### 8.1 为什么需要锁？

假设两个线程同时操作 Buffer Pool，不加锁会出什么问题？

#### 问题1：竞态条件（Race Condition）

```
线程1: pt_lookup(42) → 未命中
                            线程2: pt_lookup(42) → 未命中
线程1: 从磁盘读页42到frame0
                            线程2: 从磁盘读页42到frame1
线程1: pt_insert(42→0)
                            线程2: pt_insert(42→1)  ← 覆盖了！

现在页42在 frame0 和 frame1 都有一份，浪费空间，且数据可能不一致！
```

#### 问题2：淘汰正在使用的页

```
线程1: fetch_page(42) → frame0, pin_count=1
线程1: 正在修改 frame0 的数据...
                            线程2: fetch_page(99) 缓存满
                            线程2: victim() → frame0？！（如果没 pin 保护）
                            线程2: frame0 被覆盖成页99
线程1: 继续修改 frame0 → 修改了页99的数据！数据错乱！
```

#### 问题3：统计错误

```
线程1: stats.hits++   (读 hits=5, 写 hits=6)
                            线程2: stats.hits++   (读 hits=5, 写 hits=6)
结果: hits=6，但实际应该是 7（两次命中）
```

`++` 不是原子操作：它包含"读-改-写"三步，两线程交错会丢失更新。

### 8.2 需要加锁的地方

```
┌─────────────────────────────────────────────────────────┐
│  Buffer Pool 并发需要的锁：                              │
│                                                          │
│  1. page_table 读写锁    ← 读多写少，用 rwlock          │
│  2. 每个 frame 的锁      ← 支持并发读同一页             │
│  3. replacer 互斥锁      ← 替换算法的状态要保护         │
│  4. free_list 互斥锁     ← 空闲列表是全局共享           │
│  5. stats 原子操作       ← 统计计数用原子变量           │
└─────────────────────────────────────────────────────────┘
```

### 8.3 锁的粒度选择

锁太粗（一个全局锁）→ 并发度低，等于串行。
锁太细（每个字段一把锁）→ 锁开销大，容易死锁。

真实数据库的常见选择：

```
┌─────────────────────────────────────────────────────────┐
│  锁粒度谱系（从粗到细）：                                │
│                                                          │
│  全局锁 ─── 页表锁 ─── frame锁 ─── 字段锁               │
│     │          │          │          │                   │
│   最简单     常见       PostgreSQL   太复杂              │
│   并发差     并发中等    并发好       很少用              │
└─────────────────────────────────────────────────────────┘
```

### 8.4 真实数据库如何处理并发缓存

#### PostgreSQL 的做法

PostgreSQL 用**缓冲区描述符锁**（BufferDesc lock）+ **IO 锁**：

```
1. 查 page_table（哈希表，有共享锁）
2. 找到 frame → 对该 frame 加 pin（原子操作）
3. 释放哈希表锁
4. 对 frame 加内容锁（读锁或写锁）
5. 操作数据
6. 释放内容锁
7. unpin（原子操作）
```

关键技巧：**pin 不用锁**（用原子操作），因为 pin/unpin 非常频繁，
用锁会成瓶颈。只有淘汰时才需要加锁。

#### MySQL InnoDB 的做法

InnoDB 用**缓冲池互斥锁**（buf_pool mutex）+ **页锁**：

```
1. 加 buf_pool mutex
2. 查哈希表
3. 命中: pin++（在 mutex 保护下）, 释放 mutex
4. 未命中: 找 victim, 释放 mutex, 读磁盘（IO 不持锁）
5. 加页锁, 操作数据, 释放页锁
6. unpin
```

关键技巧：**IO 时不持锁**（磁盘 IO 很慢，持锁会阻塞所有人）。

### 8.5 miniDB 的并发扩展点

miniDB 目前单线程，但预留了扩展点：

```c
// 未来要加的锁（伪代码）
struct buffer_pool {
    pthread_rwlock_t pt_lock;      // page_table 读写锁
    pthread_mutex_t  repl_lock;    // replacer 互斥锁
    pthread_mutex_t  free_lock;    // free_list 互斥锁
    // 每个 frame 的锁可以用单独的数组
    pthread_rwlock_t *frame_locks; // 每个 frame 一把读写锁
    ...
};
```

加锁后的 fetch_page 伪代码：

```c
page_t *bp_fetch_page(bp, pid) {
    pthread_rwlock_rdlock(&bp->pt_lock);      // 读锁
    fid = pt_lookup(bp, pid);
    if (fid != INVALID) {
        bp->pin_count[fid]++;                 // TODO: 原子操作
        pthread_rwlock_unlock(&bp->pt_lock);
        return &bp->frames[fid];
    }
    pthread_rwlock_unlock(&bp->pt_lock);

    pthread_rwlock_wrlock(&bp->pt_lock);      // 写锁
    // ... 淘汰、读磁盘、插入 ...
    pthread_rwlock_unlock(&bp->pt_lock);
    return &bp->frames[fid];
}
```

> 完整的并发实现会在**章6 事务与并发**中讲解。

---

## 9. 代码逐行解读

现在逐行解读 miniDB 的核心代码。建议对照源码阅读。

### 9.1 replacer.h — 替换器接口

```c
#ifndef MINIDB_REPLACER_H
#define MINIDB_REPLACER_H

#include <stdint.h>
#include <stdbool.h>

typedef int32_t frame_id_t;              // frame 编号，32位整数
#define INVALID_FRAME_ID (-1)            // 无效 frame（淘汰失败时返回）

typedef enum {
    REPLACER_LRU = 0,                    // 最近最少使用
    REPLACER_CLOCK = 1,                  // 时钟算法
    REPLACER_LRU_K = 2,                  // LRU-K (K=2)
} replacer_type_t;

typedef struct replacer replacer_t;      // 不透明结构体（实现细节隐藏）

// 生命周期
replacer_t *replacer_create(replacer_type_t type, int capacity);
void        replacer_destroy(replacer_t *r);

// 核心操作
void       replacer_pin(replacer_t *r, frame_id_t fid);   // 从候选集移除
void       replacer_unpin(replacer_t *r, frame_id_t fid); // 加入候选集
frame_id_t replacer_victim(replacer_t *r);                // 选一个淘汰

int        replacer_size(replacer_t *r);                  // 候选集大小

#endif
```

设计要点：
- **不透明结构体**：`struct replacer` 的定义在 .c 文件里，外部看不到内部字段。
  这是 C 语言封装的常用手法。
- **三种算法统一接口**：pin/unpin/victim 签名一样，buffer_pool 不用关心具体算法。

### 9.2 replacer.c — 替换器实现

#### 结构体定义

```c
#define LRU_K 2   // K 的值，固定为 2

struct replacer {
    replacer_type_t type;     // 算法类型
    int capacity;             // 最大 frame 数
    bool *in_set;             // in_set[fid] = 是否在候选集中
    int size;                 // 候选集当前大小

    // LRU 专用
    frame_id_t *lru_order;    // 数组：[0]=最久未使用, [size-1]=最近使用

    // Clock 专用
    bool *clock_ref;          // clock_ref[fid] = 引用位
    int clock_hand;           // 时钟指针

    // LRU-K 专用
    int *lruk_count;          // lruk_count[fid] = 访问次数
    uint64_t *lruk_first;     // lruk_first[fid] = 第1次（或第K次）访问时间
    uint64_t *lruk_last;      // lruk_last[fid] = 最近访问时间
    uint64_t lruk_time;       // 全局逻辑时钟（每次 unpin 递增）
};
```

注意：所有算法共用 `in_set` 和 `size`，只有算法专属字段分开。
这样切换算法时不用重新分配 `in_set`。

#### replacer_create

```c
replacer_t *replacer_create(replacer_type_t type, int capacity) {
    replacer_t *r = calloc(1, sizeof(replacer_t));  // calloc 清零
    if (!r) return NULL;                            // 分配失败

    r->type = type;
    r->capacity = capacity;
    r->in_set = calloc(capacity, sizeof(bool));     // 全 false（都不在候选集）
    r->size = 0;

    switch (type) {                                 // 只分配该算法需要的字段
        case REPLACER_LRU:
            r->lru_order = malloc(capacity * sizeof(frame_id_t));
            break;
        case REPLACER_CLOCK:
            r->clock_ref = calloc(capacity, sizeof(bool));  // 全 false
            r->clock_hand = 0;
            break;
        case REPLACER_LRU_K:
            r->lruk_count = calloc(capacity, sizeof(int));  // 全 0
            r->lruk_first = calloc(capacity, sizeof(uint64_t));
            r->lruk_last  = calloc(capacity, sizeof(uint64_t));
            r->lruk_time = 0;
            break;
    }
    return r;
}
```

技巧：用 `calloc` 而非 `malloc`，因为 calloc 会清零，
初始状态（不在候选集、ref=0、count=0）正好都是零值。

#### replacer_pin（从候选集移除）

```c
void replacer_pin(replacer_t *r, frame_id_t fid) {
    if (fid < 0 || fid >= r->capacity) return;  // 越界检查
    if (!r->in_set[fid]) return;                // 不在候选集，无需移除

    if (r->type == REPLACER_LRU) {
        lru_remove(r, fid);                     // LRU 要从链表数组移除
    }
    // Clock 和 LRU-K 不需要额外操作（in_set 标记足够）
    r->in_set[fid] = false;                     // 标记不在候选集
    r->size--;
}
```

为什么 LRU 要 `lru_remove` 而 Clock 不用？
因为 LRU 的 `lru_order` 数组必须保持紧凑（没有空洞），
而 Clock 靠 `in_set` 标记跳过，不需要紧凑。

#### replacer_unpin（加入候选集）

```c
void replacer_unpin(replacer_t *r, frame_id_t fid) {
    if (fid < 0 || fid >= r->capacity) return;  // 越界检查
    if (r->in_set[fid]) return;                 // 已经在候选集，不重复加

    r->in_set[fid] = true;                      // 标记在候选集
    r->size++;

    switch (r->type) {
        case REPLACER_LRU:
            r->lru_order[r->size - 1] = fid;    // 加到数组末尾（最近使用）
            break;
        case REPLACER_CLOCK:
            r->clock_ref[fid] = true;           // 置引用位
            break;
        case REPLACER_LRU_K:
            r->lruk_count[fid]++;               // 访问次数+1
            if (r->lruk_count[fid] == 1) {
                r->lruk_first[fid] = r->lruk_time;   // 第一次访问
                r->lruk_last[fid] = r->lruk_time;
            } else {
                r->lruk_first[fid] = r->lruk_last[fid];  // 滑动窗口
                r->lruk_last[fid] = r->lruk_time;
            }
            r->lruk_time++;                     // 全局时钟+1
            break;
    }
}
```

### 9.3 buffer_pool.h — Buffer Pool 接口

```c
typedef struct buffer_pool buffer_pool_t;  // 不透明结构体

typedef struct {
    int hits;          // 缓存命中次数
    int misses;        // 缓存未命中次数
    int evictions;     // 淘汰次数
    int dirty_writes;  // 脏页写回次数
} bp_stats_t;

// 生命周期
buffer_pool_t *bp_create(pager_t *pager, int pool_size, replacer_type_t rtype);
void           bp_destroy(buffer_pool_t *bp);

// 核心操作
page_t   *bp_fetch_page(buffer_pool_t *bp, page_id_t pid);  // 获取页（自动缓存）
void      bp_unpin_page(buffer_pool_t *bp, page_id_t pid, bool is_dirty);  // 释放页
page_id_t bp_new_page(buffer_pool_t *bp, page_t **page);    // 分配新页
bool      bp_flush_page(buffer_pool_t *bp, page_id_t pid);  // 刷一页到磁盘
void      bp_flush_all(buffer_pool_t *bp);                  // 刷所有脏页

void      bp_get_stats(buffer_pool_t *bp, bp_stats_t *stats);  // 获取统计
```

### 9.4 buffer_pool.c — Buffer Pool 实现

#### bp_create

```c
buffer_pool_t *bp_create(pager_t *pager, int pool_size, replacer_type_t rtype) {
    buffer_pool_t *bp = calloc(1, sizeof(buffer_pool_t));
    if (!bp) return NULL;

    bp->pager = pager;
    bp->pool_size = pool_size;
    bp->pt_cap = pool_size * 2;   // 哈希表容量是池大小的2倍（降低冲突）

    // 分配各个数组
    bp->frames    = malloc(pool_size * sizeof(page_t));     // 页内容
    bp->pin_count = calloc(pool_size, sizeof(int));         // 全0
    bp->dirty     = calloc(pool_size, sizeof(bool));        // 全false
    bp->frame_pid = malloc(pool_size * sizeof(page_id_t));
    bp->pt        = calloc(bp->pt_cap, sizeof(pt_entry_t)); // 哈希表
    bp->free_list = malloc(pool_size * sizeof(frame_id_t));

    // 初始化：所有 frame 都是空闲的
    for (int i = 0; i < pool_size; i++) {
        bp->frame_pid[i] = INVALID_PAGE_ID;  // 没装任何页
        bp->free_list[i] = i;                // 空闲列表 = [0, 1, 2, ..., N-1]
    }
    bp->free_count = pool_size;

    bp->repl = replacer_create(rtype, pool_size);  // 创建替换器
    return bp;
}
```

为什么哈希表容量是池大小的 2 倍？
哈希表装载因子 = 元素数 / 容量。池满时元素数 = pool_size，
装载因子 = pool_size / (2 * pool_size) = 0.5。
装载因子 0.5 时，开放寻址哈希的冲突率低，查找快。

#### bp_fetch_page（核心函数）

```c
page_t *bp_fetch_page(buffer_pool_t *bp, page_id_t pid) {
    // 1. 查哈希表
    frame_id_t fid = pt_lookup(bp, pid);
    if (fid != INVALID_FRAME_ID) {
        // 命中！
        bp->pin_count[fid]++;           // pin 住，防止被淘汰
        replacer_pin(bp->repl, fid);    // 从替换器候选集移除
        bp->stats.hits++;
        return &bp->frames[fid];        // 直接返回内存页
    }

    // 2. 未命中
    bp->stats.misses++;

    // 3. 找一个空闲 frame 或淘汰一个
    fid = bp_get_frame(bp);
    if (fid == INVALID_FRAME_ID) return NULL;  // 淘汰失败（所有页都被 pin）

    // 4. 从磁盘读入
    if (!pager_read(bp->pager, pid, &bp->frames[fid])) return NULL;

    // 5. 更新元数据
    bp->frame_pid[fid] = pid;          // 记录这个 frame 装的是哪个页
    bp->pin_count[fid] = 1;            // 调用者正在用，pin=1
    bp->dirty[fid] = false;            // 刚从磁盘读的，不脏
    pt_insert(bp, pid, fid);           // 加入哈希表
    replacer_pin(bp->repl, fid);       // 从替换器移除（正在用）

    return &bp->frames[fid];
}
```

#### bp_unpin_page

```c
void bp_unpin_page(buffer_pool_t *bp, page_id_t pid, bool is_dirty) {
    frame_id_t fid = pt_lookup(bp, pid);
    if (fid == INVALID_FRAME_ID) return;  // 页不在缓存，无法 unpin

    if (is_dirty) bp->dirty[fid] = true;  // 标记为脏
    bp->pin_count[fid]--;                 // 解除 pin

    if (bp->pin_count[fid] == 0) {
        // 没人用了，加入替换器候选集（可被淘汰）
        replacer_unpin(bp->repl, fid);
    }
}
```

关键：只有 `pin_count` 降到 0 才加入候选集。
如果多个线程 pin 了同一页，要等最后一个 unpin 才能淘汰。

#### bp_evict（淘汰）

```c
static frame_id_t bp_evict(buffer_pool_t *bp) {
    // 1. 让替换器选一个 victim
    frame_id_t fid = replacer_victim(bp->repl);
    if (fid == INVALID_FRAME_ID) return INVALID_FRAME_ID;  // 没有可淘汰的

    bp->stats.evictions++;

    // 2. 如果是脏页，先写回磁盘
    if (bp->dirty[fid]) {
        pager_write(bp->pager, bp->frame_pid[fid], &bp->frames[fid]);
        bp->dirty[fid] = false;
        bp->stats.dirty_writes++;
    }

    // 3. 从哈希表删除旧映射
    pt_delete(bp, bp->frame_pid[fid]);
    bp->frame_pid[fid] = INVALID_PAGE_ID;  // 标记 frame 空闲
    return fid;
}
```

#### bp_get_frame（获取可用 frame）

```c
static frame_id_t bp_get_frame(buffer_pool_t *bp) {
    if (bp->free_count > 0) {
        return bp->free_list[--bp->free_count];  // 优先用空闲 frame
    }
    return bp_evict(bp);  // 没有空闲的，淘汰一个
}
```

优先用空闲 frame（O(1)），只有空闲列表空了才淘汰。
这样新建的 Buffer Pool 前期不需要淘汰，性能好。

#### bp_new_page（分配新页）

```c
page_id_t bp_new_page(buffer_pool_t *bp, page_t **page) {
    // 1. 在磁盘上分配新页
    page_id_t pid = pager_allocate(bp->pager);
    if (pid == INVALID_PAGE_ID) return INVALID_PAGE_ID;

    // 2. 获取一个 frame
    frame_id_t fid = bp_get_frame(bp);
    if (fid == INVALID_FRAME_ID) return INVALID_PAGE_ID;

    // 3. 初始化页内容
    page_init(&bp->frames[fid], pid, PAGE_TYPE_HEAP);
    bp->frame_pid[fid] = pid;
    bp->pin_count[fid] = 1;
    bp->dirty[fid] = true;     // 新页算脏（要写回磁盘）
    pt_insert(bp, pid, fid);
    replacer_pin(bp->repl, fid);

    if (page) *page = &bp->frames[fid];
    return pid;
}
```

#### bp_flush_all（刷所有脏页）

```c
void bp_flush_all(buffer_pool_t *bp) {
    for (int fid = 0; fid < bp->pool_size; fid++) {
        if (bp->frame_pid[fid] != INVALID_PAGE_ID && bp->dirty[fid]) {
            pager_write(bp->pager, bp->frame_pid[fid], &bp->frames[fid]);
            bp->dirty[fid] = false;
            bp->stats.dirty_writes++;
        }
    }
}
```

`bp_destroy` 会先调用 `bp_flush_all`，确保所有修改都写回磁盘。
如果不刷，进程退出后内存释放，脏页的修改就丢失了！

---

## 10. 与真实数据库对比

miniDB 的 Buffer Pool 是教学版，和工业级数据库有哪些异同？

### 10.1 miniDB vs PostgreSQL vs SQLite

| 维度 | miniDB | PostgreSQL | SQLite |
|------|--------|------------|--------|
| **缓存名称** | buffer_pool | shared buffers | page cache |
| **默认大小** | 1024 页（4MB） | 128MB | 操作系统页缓存 |
| **替换算法** | LRU / Clock / LRU-K | Clock sweep（类似 Clock） | LRU（OS 内核） |
| **并发** | 单线程 | 多线程 + 锁 | 单线程（写）/ 多读 |
| **预读** | 无 | 有（read-ahead） | 有（mmap 预读） |
| **脏页写回** | 淘汰时 / flush | 后台 writer 线程 | 检查点 |
| **哈希表** | 开放寻址 | 哈希表 + 冲突链 | B-tree 直接定位 |
| **双缓冲** | 无 | 无（double cache 避免策略） | 有（mmap + 用户态缓存） |

### 10.2 PostgreSQL Shared Buffers

PostgreSQL 的 Buffer Pool 叫 **shared buffers**，是所有后端进程共享的内存区域。

```
┌─────────────────────────────────────────────────────────┐
│  PostgreSQL Buffer Pool 架构                             │
│                                                          │
│  ┌─────────────────────────────────────────────┐        │
│  │  Buffer Descriptors 数组                     │        │
│  │  每个 descriptor: {tag, flags, pin, ...}     │        │
│  └─────────────────────────────────────────────┘        │
│  ┌─────────────────────────────────────────────┐        │
│  │  Buffer Pool 共享内存                        │        │
│  │  (所有进程 mmap 同一块共享内存)              │        │
│  └─────────────────────────────────────────────┘        │
│  ┌─────────────────────────────────────────────┐        │
│  │  Hash Table: BufferTag → BufferDesc          │        │
│  └─────────────────────────────────────────────┘        │
│  ┌─────────────────────────────────────────────┐        │
│  │  Free List                                   │        │
│  └─────────────────────────────────────────────┘        │
│                                                          │
│  替换算法: Clock Sweep（类似 miniDB 的 Clock）          │
│  脏页写回: 后台 bgwriter 线程定期刷                      │
│  预读: 顺序扫描时预读 32 个页                           │
└─────────────────────────────────────────────────────────┘
```

和 miniDB 的关键差异：

1. **共享内存**：PostgreSQL 用 `mmap` 让多个进程共享同一块物理内存，
   miniDB 是单进程内的内存。
2. **后台 writer**：PostgreSQL 有专门的 `bgwriter` 线程异步刷脏页，
   避免淘汰时同步写磁盘卡顿。miniDB 是淘汰时同步写。
3. **预读**：PostgreSQL 检测到顺序访问模式时，会预读多个页。
   miniDB 没有预读。
4. **Buffer Tag**：PostgreSQL 的 tag 是 `(relfilenode, fork, blocknum)` 三元组，
   能区分不同表、不同 fork（main/fsm/vm）。miniDB 只有 `page_id`。

### 10.3 SQLite 的 mmap 方案

SQLite 走了完全不同的路线：**让操作系统来管缓存**。

```
┌─────────────────────────────────────────────────────────┐
│  SQLite 的缓存策略                                       │
│                                                          │
│  数据库文件 ──mmap──→ 进程地址空间                       │
│                       │                                  │
│                       ├─ OS 内核页缓存（自动管理）       │
│                       │                                  │
│                       └─ 用户态访问 mmap 区域            │
│                                                          │
│  替换算法: OS 内核的 LRU（用户不用管）                   │
│  脏页写回: OS 的 writeback（或 fsync 时）                │
│  预读: OS 内核的预读（readahead）                        │
└─────────────────────────────────────────────────────────┘
```

优点：
- 实现简单（把缓存管理外包给 OS）。
- 自动利用 OS 的预读、页缓存优化。
- 多个进程打开同一数据库，自动共享 OS 页缓存。

缺点：
- **双缓冲**：SQLite 用户态可能还有自己的缓存，和 OS 缓存重复，浪费内存。
- **控制力弱**：无法自定义替换算法、无法精确控制刷盘时机。
- **大数据库性能差**：mmap 映射整个文件，文件很大时虚拟地址空间吃紧。

miniDB 和 PostgreSQL 都选择**自己管缓存**，因为数据库比 OS 更懂自己的访问模式。

### 10.4 共同的核心思想

尽管实现差异很大，三者的核心思想一致：

```
┌─────────────────────────────────────────────────────────┐
│  所有 Buffer Pool 的共同骨架：                           │
│                                                          │
│  1. 一个固定大小的内存池（frames 数组）                  │
│  2. 一个映射 page_id → frame_id 的哈希表                 │
│  3. 一个替换算法决定淘汰谁                               │
│  4. pin 机制防止正在用的页被淘汰                         │
│  5. dirty 标记追踪修改，淘汰时写回                       │
│                                                          │
│  miniDB 把这五点用最简代码实现，是理解工业级实现的基础   │
└─────────────────────────────────────────────────────────┘
```

### 10.5 miniDB 没有但真实数据库有的

| 特性 | 作用 | miniDB | 真实数据库 |
|------|------|--------|------------|
| **预读** | 顺序访问时提前读多个页 | ❌ | ✅ |
| **后台 writer** | 异步刷脏页，避免淘汰卡顿 | ❌ | ✅ |
| **双缓冲区** | 新旧两个池，淘汰时复制而非原地覆盖 | ❌ | ✅（部分） |
| **压缩** | 内存中存压缩页，节省空间 | ❌ | ✅（部分） |
| **大页支持** | 用 2MB 大页减少 TLB miss | ❌ | ✅ |
| **NUMA 感知** | 多 CPU 节点时本地分配 | ❌ | ✅ |

这些优化在 miniDB 里省略了，因为它们不影响对**核心缓存逻辑**的理解。
理解了 miniDB，再看 PostgreSQL 源码会顺畅很多。

---

## 11. 习题

### 基础题

**题1**：用 Clock 算法时，如果所有 frame 的 ref bit 都是 1，会发生什么？

<details>
<summary>参考答案</summary>

时针会转一整圈，把所有 ref=1 置为 0（给所有页第二次机会），
然后第二圈必然找到 ref=0 的页淘汰。所以最多转两圈，一定能选出 victim。
这就是代码里 `capacity * 2` 次尝试的原因。
</details>

**题2**：LRU-K 中 K=1 等价于什么算法？K=∞ 呢？

<details>
<summary>参考答案</summary>

- K=1：只看最近 1 次访问，等价于 **LRU**。
- K=∞：要看无限次历史，等价于 **FIFO**（因为所有页的"第 K 次访问"都是它第一次被访问的时间，最早进入的就是最早被淘汰的）。
</details>

**题3**：如果 pool_size 等于数据库总页数，替换算法还有意义吗？

<details>
<summary>参考答案</summary>

没有实际意义。所有页都能放进缓存，永远不会淘汰，替换算法的 victim 永远不会被调用。
但代码里还是要有替换算法，只是它不会被触发。这种情况叫"缓存全装下"，命中率 100%。
</details>

**题4**：`bp_destroy` 时为什么要先 `flush_all`？如果不刷会怎样？

<details>
<summary>参考答案</summary>

`bp_destroy` 会 `free` 掉所有内存。如果有脏页没写回磁盘，
`free` 后内存被回收，修改就永久丢失了。`flush_all` 确保所有修改落盘。
</details>

**题5**：如何实现"预读"（read-ahead）来提高 Buffer Pool 命中率？

<details>
<summary>参考答案</summary>

检测到顺序访问模式（连续读 page 0, 1, 2, ...）时，
在读 page N 时异步把 page N+1, N+2, N+3 也读入缓存。
这样下次读 page N+1 时直接命中。

实现要点：
- 需要一个后台线程做异步 IO
- 需要检测"是否顺序访问"（记录上次访问的 page_id，看是否连续）
- 预读太多会污染缓存（预读的页没用上），需要限制预读量
</details>

### 进阶题

**题6**：miniDB 的 LRU 用数组实现，`lru_remove` 是 O(n) 的。如何改成 O(1)？

<details>
<summary>参考答案</summary>

用**双向链表 + 哈希表**：
- 双向链表按访问顺序排列，head=最近使用，tail=最久未使用。
- 哈希表 frame_id → 链表节点指针，O(1) 找到节点。
- 移到头部：链表摘出 + 插入头部，都是 O(1)。
- 淘汰：取 tail，O(1)。
</details>

**题7**：miniDB 的 LRU-K 实现里，冷页和热页都用 `first` 作为淘汰 key，这和标准 LRU-K 有什么差异？

<details>
<summary>参考答案</summary>

标准 LRU-K 优先淘汰冷页（count < K），在冷页中再按 first 淘汰。
miniDB 统一比较 first，可能淘汰一个 first 较小的热页，而非 first 较大的冷页。
差异在于：miniDB 的抗扫描污染能力弱于标准 LRU-K，但代码更简洁。
</details>

**题8**：如果两个线程同时 `bp_fetch_page(42)`，都未命中，会发生什么？如何修复？

<details>
<summary>参考答案</summary>

不加锁的话：两个线程都发现未命中，都从磁盘读页 42 到不同 frame，
都 `pt_insert(42 → fid)`，导致页 42 在两个 frame 各有一份，浪费空间且可能不一致。

修复：在 `pt_lookup` 和 `pt_insert` 之间加锁。或者用"先查再插"模式：
加写锁后再次查哈希表（可能另一个线程已经插入了），如果已插入就直接用。
</details>

**题9**：为什么 `bp_new_page` 把 `dirty` 设为 `true`，而 `bp_fetch_page` 设为 `false`？

<details>
<summary>参考答案</summary>

- `bp_new_page`：页是新建的，磁盘上还没有，必须写回，所以 dirty=true。
- `bp_fetch_page`：页是从磁盘读的，内存内容和磁盘一致，没修改过，所以 dirty=false。
</details>

**题10**：设计一个实验，对比三种替换算法在扫描负载下的命中率。写出实验步骤。

<details>
<summary>参考答案</summary>

步骤：
1. 创建一个 Buffer Pool，pool_size=100，分别用 LRU / Clock / LRU-K。
2. 先访问 page 0-49 各 10 次（建立热数据）。
3. 再顺序访问 page 100-200（扫描污染）。
4. 再访问 page 0-49 各 10 次（看热数据还在不在）。
5. 用 `bp_get_stats` 读取命中率。
6. 对比三种算法的命中率差异。

预期：LRU 命中率最低（热数据被冲走），LRU-K 最高（抗污染），Clock 居中。
</details>

### 思考题

**题11**：如果内存足够大，能装下整个数据库，还需要 Buffer Pool 吗？

**题12**：Clock 算法的 ref bit 只有 1 位，如果改成 2 位（记录"最近被访问过几次"），会有什么效果？

**题13**：为什么真实数据库不用 LRU 而用 Clock 或 LRU-K？LRU 命中率不是更高吗？

**题14**：miniDB 的 page_table 用开放寻址哈希，如果改成链式哈希，各有什么优缺点？

**题15**：如果 `bp_fetch_page` 返回的页指针，调用者用完之后忘记 `bp_unpin_page`，会有什么后果？

---

## 设计决策总结

| 决策 | 选择 | 理由 |
|---|---|---|
| 替换算法 | 三种都实现 | 对比教学，可切换 |
| LRU-K 的 K | 2 | PostgreSQL 默认值 |
| page_table | 开放寻址哈希 | O(1) 查找，教学清晰 |
| 哈希表容量 | pool_size × 2 | 装载因子 0.5，冲突低 |
| 脏页写回 | 淘汰时 + flush | 延迟写，合并多次修改 |
| 并发 | 单线程 | 渐进式，章6 加锁 |
| LRU 实现 | 数组（非链表） | 代码简单，O(n) 可接受 |

---

## 本章小结

```
┌─────────────────────────────────────────────────────────┐
│  Buffer Pool 的五个关键点：                              │
│                                                          │
│  1. 缓存：磁盘慢、内存快，缓存是性能核心                 │
│  2. 查找：哈希表 O(1) 判断页是否在内存                   │
│  3. 替换：LRU / Clock / LRU-K 决定淘汰谁                 │
│  4. pin：防止正在用的页被淘汰                            │
│  5. dirty：追踪修改，淘汰时写回磁盘                      │
│                                                          │
│  理解这五点，就理解了所有数据库 Buffer Pool 的骨架        │
└─────────────────────────────────────────────────────────┘
```

---

上一章：[章1 存储基础](01-storage.md) | 下一章：[章3 B+Tree 索引](03-btree.md)
