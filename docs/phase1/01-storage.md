# 章1：存储基础 — 从字节到页

> 数据库的一切都从存储开始。本章实现页式存储的三大基石：`page`（slotted page）、`file_manager`（文件IO）、`pager`（页管理器）。

## 为什么以"页"为单位？

操作系统以 4KB 块管理文件，磁盘以 512B~4KB 扇区寻址。如果数据库按字节读写，每次操作都要做一次系统调用和磁盘寻道，代价极高。

**按页（4096 字节）读写的好处：**

1. 一次 IO 读写一整块数据，摊薄寻道成本
2. 页对齐 OS 块，避免读改写放大
3. 固定大小便于缓存管理（Buffer Pool 以页为单位）
4. B+Tree 一个节点正好放一页

## Slotted Page 结构

这是 PostgreSQL、SQLite、InnoDB 都用的经典页结构。核心思想：**页内数据从两端向中间生长**。

```
偏移 0                                              4096
┌────────────┬────────────┬────────────┬────────────┐
│ Header(20) │ Slot Array │  空闲空间  │ Tuple Data │
│            │ → 向右生长 │            │ ← 向左生长 │
└────────────┴────────────┴────────────┴────────────┘
```

### 为什么这样设计？

- **变长记录**：不同 tuple 大小不同，slot 记录每个 tuple 的 offset 和 length
- **删除高效**：删除只把 slot 的 length 置 0，不搬移数据
- **顺序无关**：tuple 的物理顺序与 slot 顺序无关，可通过 slot 间接寻址

### Page Header（20 字节）

| 偏移 | 大小 | 字段 | 说明 |
|---|---|---|---|
| 0 | 4 | page_id | 页编号 |
| 4 | 1 | page_type | 页类型（堆/索引/溢出） |
| 5 | 2 | num_slots | slot 数量 |
| 7 | 2 | free_space_offset | 空闲空间起始偏移 |
| 9 | 4 | next_page_id | 链表下一页（溢出页用） |
| 13 | 4 | checksum | 校验和 |
| 17 | 3 | 保留 | — |

### Slot（4 字节）

| 偏移 | 大小 | 字段 | 说明 |
|---|---|---|---|
| 0 | 2 | offset | tuple 在页内偏移 |
| 2 | 2 | length | tuple 长度，0 = 已删除 |

### 空闲空间计算

```
空闲空间 = free_space_offset - (PAGE_HEADER_SIZE + num_slots * SLOT_SIZE)
```

添加一个 len 字节的 tuple 需要消耗 `len + SLOT_SIZE` 字节（tuple 数据 + 新 slot）。

## 字节序

磁盘上的整数用**大端序**（网络字节序）存储，确保跨平台一致。

```c
// 大端写入 uint32
static void write_u32(uint8_t *p, uint32_t v) {
    p[0] = (v >> 24) & 0xFF;
    p[1] = (v >> 16) & 0xFF;
    p[2] = (v >> 8)  & 0xFF;
    p[3] = v & 0xFF;
}
```

!!! note "为什么用大端？"
    SQLite 也用大端存储，这样数据库文件可以在不同字节序的机器间直接拷贝。
    内存中按主机字节序处理，读写磁盘时转换。

## 三个模块

### page — 页内操作

```c
page_init(&page, 42, PAGE_TYPE_HEAP);       // 初始化空页
slot_id_t sid = page_add_tuple(&page, data, len);  // 添加 tuple
const void *data = page_get_tuple(&page, sid, &len); // 读取 tuple
page_delete_tuple(&page, sid);              // 标记删除
uint16_t free = page_free_space(&page);     // 查询空闲空间
```

### file_manager — 文件 IO

```c
file_manager_t *fm = fm_open("test.db");    // 打开/创建文件
page_id_t pid = fm_allocate_page(fm);       // 分配新页
fm_read_page(fm, pid, buf);                 // 读一页
fm_write_page(fm, pid, buf);                // 写一页
fm_close(fm);                               // 关闭
```

!!! note "pread/pwrite vs fseek+fread"
    本章用 `fseek + fread/fwrite`（标准C，跨平台）。
    真实数据库用 `pread/pwrite`（线程安全，不改变文件偏移指针）。
    章2 引入 Buffer Pool 后会切换到线程安全的 IO。

### pager — 页管理器

```c
pager_t *pager = pager_open("test.db");     // 打开数据库
page_id_t pid = pager_allocate(pager);      // 分配页
pager_read(pager, pid, &page);              // 读页到 page_t
pager_write(pager, pid, &page);             // 写页
pager_close(pager);                         // 关闭
```

!!! note "pager 和 file_manager 的关系"
    pager 是 file_manager 之上的薄封装，本章只做简单的透传。
    章2 会在 pager 和 file_manager 之间插入 Buffer Pool，
    实现"热页驻留内存、冷页按需加载"。

## 使用示例

```c
pager_t *pager = pager_open("mydb.dat");

// 分配一个堆表页
page_id_t pid = pager_allocate(pager);

// 在页中插入数据
page_t page;
page_init(&page, pid, PAGE_TYPE_HEAP);
page_add_tuple(&page, "Alice", 6);
page_add_tuple(&page, "Bob", 4);
pager_write(pager, pid, &page);

// 读回验证
page_t read;
pager_read(pager, pid, &read);
uint16_t len;
printf("%s\n", (char *)page_get_tuple(&read, 0, &len));  // Alice

pager_close(pager);
```

## 设计决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 页大小 | 4096 | 对齐 OS 页，SQLite 默认值 |
| 字节序 | 大端 | 跨平台一致 |
| 删除策略 | 标记删除 | 高效，空间回收留给 vacuum |
| IO 接口 | fseek+fread | 跨平台，章2 换 pread/pwrite |

## 习题

1. 如果页大小改为 8192（PostgreSQL 默认），需要修改哪些地方？
2. 标记删除后空间不会被立即回收，如何判断一个页"需要清理"？
3. 如果 tuple 太大放不下一页，应该怎么处理？（提示：溢出页）
4. checksum 字段目前没有使用，如何实现一个简单的页校验和？

---

上一章：[章0 项目基础设施](00-infrastructure.md) | 下一章：[章2 Buffer Pool](02-buffer-pool.md)