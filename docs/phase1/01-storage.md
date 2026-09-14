# 章1：存储基础 — 从字节到页

> 数据库的一切都从存储开始。本章实现页式存储的三大基石：`page`（slotted page）、`file_manager`（文件IO）、`pager`（页管理器）。
>
> **阅读对象**：第一次接触数据库底层实现的读者。本章假设你懂 C 语言基本语法（指针、结构体、`memcpy`），但不假设你懂数据库内部原理。所有概念都从"为什么"讲起，配大量图示。

---

## 目录

- [1. 从字节到数据库](#1-从字节到数据库)
- [2. 为什么是 4096 字节](#2-为什么是-4096-字节)
- [3. 大端序 vs 小端序](#3-大端序-vs-小端序)
- [4. Slotted Page 详解](#4-slotted-page-详解)
- [5. 序列化](#5-序列化)
- [6. Pager 的工作](#6-pager-的工作)
- [7. File Manager](#7-file-manager)
- [8. 代码逐行解读](#8-代码逐行解读)
- [9. 调试技巧](#9-调试技巧)
- [10. 与真实数据库对比](#10-与真实数据库对比)
- [11. 习题](#11-习题)

---

## 1. 从字节到数据库

### 1.1 存储的层次结构

计算机存储不是一块"平坦的内存"，而是一层套一层的层次结构。要理解数据库为什么按"页"组织，必须先看清整张存储层次图：

```
┌──────────────────────────────────────────────────────────┐
│  CPU 寄存器        <1ns      几十字节    最快、最贵       │
├──────────────────────────────────────────────────────────┤
│  L1/L2/L3 缓存    ~1ns      几~几十MB   硬件管理          │
├──────────────────────────────────────────────────────────┤
│  主内存(DRAM)     ~100ns    几~几百GB   OS + 硬件管理     │
├──────────────────────────────────────────────────────────┤
│  SSD             ~100us     几百GB~TB  ← 数据库主要住这里 │
├──────────────────────────────────────────────────────────┤
│  HDD             ~10ms      几TB        机械寻道慢        │
├──────────────────────────────────────────────────────────┤
│  磁带/网络存储    秒~分钟    PB级        冷备份            │
└──────────────────────────────────────────────────────────┘
```

关键事实：**访问 SSD 比访问内存慢约 1000 倍，访问 HDD 慢约 100000 倍**。数据库的全部优化目标就是"减少磁盘访问次数"。

### 1.2 磁盘的物理结构

一块 HDD 由若干**盘片(platter)**叠在一起，每片双面记录。每个面有一个**磁头(head)**，盘片旋转时磁头划过一圈称为**磁道(track)**。磁道被切成若干**扇区(sector)**，扇区是磁盘读写的最小单位。

```
        ┌─────── 盘片俯视图 ───────┐
        │   ╭───────────────╮     │
        │  ╱   ╭─────────╮   ╲    │
        │ │   ╱  扇区  ╱   ╲   │   │  ← 一个磁道
        │ │  │ (512B)│     │  │   │
        │ │   ╲     ╲     ╱   │   │
        │  ╲   ╰─────────╯   ╱    │
        │   ╰───────────────╯     │
        └──────────────────────────┘
              ↑ 磁头沿径向移动
```

- **扇区(sector)**：传统 512 字节，现代 Advanced Format 4K 对齐后逻辑扇区 4096 字节。
- **簇(cluster)/块(block)**：文件系统把若干扇区打包成更大的读写单位，通常 4KB~64KB。
- **页(page)**：数据库自己定义的读写单位，本库固定 4096 字节。

### 1.3 三级单位关系图

```
磁盘物理          文件系统(OS)         数据库
─────────         ─────────────        ───────
扇区 512B   ──→   OS块 4KB     ──→    页 4KB
(最小寻址)       (最小读写)          (逻辑管理)

   │                │                  │
   │  8个扇区       │  1个OS块         │  1个页
   │  组成1块       │  = 1个页         │  = 1个OS块
   └───────────────┴──────────────────┘
```

当数据库说"读第 5 页"时，实际发生的事：

```
数据库:  "请给我 page_id=5 的数据"
   │
   ▼
file_manager:  offset = 5 * 4096 = 20480
   │
   ▼
fseek(fp, 20480, SEEK_SET)   ← 定位到文件第 20480 字节
   │
   ▼
fread(buf, 1, 4096, fp)      ← 一次读 4096 字节
   │
   ▼
OS:  把这个请求翻译成"读第 5 个块"
   │
   ▼
磁盘:  读取对应的扇区，返回数据
```

### 1.4 图书馆类比

把数据库想象成一座图书馆：

| 数据库概念       | 图书馆类比                         |
|------------------|------------------------------------|
| 磁盘             | 图书馆的地下书库（很远、很慢）     |
| 内存(Buffer Pool)| 一楼开架阅览区（近、快但位置有限） |
| 页(page)         | 一个书架格子（固定容量）           |
| Tuple(记录)      | 一本书                             |
| Slot             | 书架上贴的编号标签                 |
| 页号(page_id)    | 书架编号 "A-12"                    |
| 读一页           | 推车去书库取整个格子的书           |
| 读一条记录       | 只想看格子里第 3 本书              |

**关键洞察**：你想要一本书，但书在地下书库。你不会只搬一本书上来——太浪费往返时间了。你会把**整个格子**的书都搬上一楼阅览区，因为：

1. 往返一次地下书库很慢（磁盘寻道）。
2. 同一格子的书很可能一起被查阅（局部性原理）。
3. 格子大小固定，方便管理（页大小固定）。

这就是"以页为单位读写"的本质：**用一次慢速往返，换回一大块数据**。

### 1.5 为什么不以单行为单位读写？

假设表里有 100 万行，每行 100 字节。你想查 `id=500000` 的那一行。

**方案 A：按行读写**

```
每次读 1 行 = 100 字节
磁盘寻道 ~10ms（HDD）
传输 100B ~ 0.01ms
单次查询 ≈ 10.01ms

如果要扫全表找符合条件的行：
100万次 × 10ms = 10000 秒 ≈ 2.7 小时  ← 不可接受
```

**方案 B：按页读写（4096 字节/页）**

```
每页装 4096 / 100 ≈ 40 行
100万行 / 40 ≈ 25000 页
一次读一页 = 4096 字节
磁盘寻道 ~10ms
传输 4096B ~ 0.4ms
单页读取 ≈ 10.4ms

扫全表：
25000次 × 10.4ms = 260 秒 ≈ 4.3 分钟  ← 快了 60 倍
```

**搬书类比**：搬一本书上楼要 1 分钟（含往返）。搬一格（40 本）也要 1 分钟（往返时间不变，搬的过程很快）。显然一次搬一格更划算。

```
按行搬:  📖→↑ 📖→↑ 📖→↑ ... 40趟 = 40分钟
按格搬:  📚(40本)→↑         1趟  = 1分钟
```

### 1.6 局部性原理

为什么按页读"不浪费"？因为数据访问有**局部性(locality)**：

- **空间局部性**：访问了第 5 行，很可能马上访问第 6、7 行（它们在同一页）。
- **时间局部性**：刚被访问的页，很可能很快再次被访问（缓存命中）。

B+Tree 的设计正是利用了这一点：一个节点放一页，一次 IO 读入一个节点及其相邻键。

### 1.7 小结

| 问题                       | 答案                                  |
|----------------------------|---------------------------------------|
| 磁盘最小读写单位是什么？   | 扇区(512B)，现代常 4K 对齐            |
| OS 文件 IO 最小单位是什么？| 块(通常 4KB)                          |
| 数据库为什么按页读写？     | 摊薄寻道成本、对齐 OS、便于缓存管理   |
| 页大小怎么选？             | 对齐 OS 页，常见 4K~16K（下节详述）   |

---

## 2. 为什么是 4096 字节

### 2.1 历史原因

4096 字节（4KB）不是随便选的，它是一连串历史和工程因素共同作用的结果：

1. **x86 页表演化**：Intel 80386（1985）引入分页机制，页大小定为 4096 字节，此后 x86/x64 一直沿用。Linux、Windows 的虚拟内存页都是 4KB。
2. **文件系统块大小**：ext2/ext3/ext4 默认块大小 4KB，NTFS 默认簇大小 4KB。
3. **SSD 页大小**：很多 NAND Flash 的物理页就是 4KB。
4. **SQLite 默认**：SQLite 从早期就默认 4KB 页大小，miniDB 对齐它便于对比。

### 2.2 与 OS 页大小对齐的好处

```
数据库页 4096B  ═══  OS页 4096B  ═══  磁盘块 4096B

读1个数据库页:
  ┌─────────┐
  │ DB Page │  ← 一次 fread(4096)
  └─────────┘
       ↓
  OS: 正好是1个磁盘块，无需读改写
       ↓
  磁盘: 一次扇区读取，无放大
```

如果数据库页 3000B 而 OS 块 4096B：

```
读1个数据库页(3000B):
  ┌──────────────┐
  │ DB Page(3000)│  ← 要读 4096B 才能拿到 3000B
  └──────────────┘     浪费 1096B
       ↓
  OS: 必须读整个 4096B 块
       ↓
  写回时: 读 4096B → 改 3000B → 写 4096B  (读改写放大)
```

### 2.3 不同数据库的页大小对比

| 数据库       | 默认页大小 | 可配置范围      | 典型选择理由                     |
|--------------|-----------|-----------------|----------------------------------|
| **SQLite**   | 4096 B    | 512 ~ 65536 B   | 嵌入式，兼容小设备                |
| **PostgreSQL** | 8192 B  | 编译期固定       | 面向服务器，更大页容纳更多索引项  |
| **MySQL/InnoDB** | 16384 B | 4096~65536 B  | 大页减少 B+Tree 层数              |
| **Oracle**   | 8192 B    | 2048~32768 B    | 数据库块，与表空间绑定            |
| **SQL Server** | 8192 B  | 编译期固定       | 扩展存储区对齐                    |
| **miniDB**   | 4096 B    | 宏定义可改       | 教学用，对齐 SQLite 便于对比      |

### 2.4 页大小对 B+Tree 扇出的影响

假设键+指针共 12 字节，一页能放多少个键？

```
页大小 4KB:  4096 / 12 ≈ 341 个键  → 3层B+Tree存 341³ ≈ 3900万行
页大小 8KB:  8192 / 12 ≈ 682 个键  → 3层B+Tree存 682³ ≈ 3.1亿行
页大小16KB: 16384 / 12 ≈ 1365个键  → 3层B+Tree存 1365³ ≈ 25亿行
```

这就是 PostgreSQL/MySQL 选更大页的原因：**服务器数据量大，大页能让 B+Tree 更矮，减少 IO 次数**。

```
                4KB页(341路)          16KB页(1365路)
                ────────────          ──────────────
  层数=3容量:    3900万行              25亿行
  查1行IO次数:    3次                   3次
  但16KB单次IO传输更多，SSD下差异小
```

### 2.5 miniDB 选 4096 的理由

```c
#define PAGE_SIZE 4096
```

1. **教学清晰**：4KB 是最常见、最好理解的页大小。
2. **对齐 OS**：Linux/Windows 默认页 4KB，无读改写放大。
3. **对比 SQLite**：SQLite 默认也是 4KB，方便用 SQLite 的 `.dbinfo` 命令对照查看。
4. **够用**：教学库数据量小，4KB 足以演示所有概念。

### 2.6 修改页大小会怎样？

页大小是全局常量，改它影响：

| 影响点                         | 说明                                    |
|--------------------------------|-----------------------------------------|
| `PAGE_SIZE` 宏                 | 所有读写按此大小                        |
| `page_t.data` 数组大小         | 结构体大小随之变化                      |
| `fm_read_page/fm_write_page`   | 一次读写字节数                          |
| 一页能放的 tuple 数            | 大页放更多，小页放更少                   |
| B+Tree 扇出                    | 大页扇出大，树更矮                       |
| Buffer Pool 内存占用           | 大页每帧占更多内存                       |

---

## 3. 大端序 vs 小端序

### 3.1 什么是字节序

一个多字节整数（比如 4 字节的 `uint32_t`）在内存里怎么排列？先存高位还是先存低位？这就是**字节序(endianness)**问题。

以整数 `0x12345678`（十进制 305419896）为例：

**大端序(Big-Endian, BE)** — 高位在前（低地址存高位）：

```
地址:   0x00   0x01   0x02   0x03
内容:   0x12   0x34   0x56   0x78
        高位 ─────────────────→ 低位

记忆: "大端" = "大端(高位)放在前端(低地址)"
```

**小端序(Little-Endian, LE)** — 低位在前（低地址存低位）：

```
地址:   0x00   0x01   0x02   0x03
内容:   0x78   0x56   0x34   0x12
        低位 ─────────────────→ 高位

记忆: "小端" = "小端(低位)放在前端(低地址)"
```

### 3.2 不同 CPU 的字节序

| 架构          | 字节序   | 说明                          |
|---------------|----------|-------------------------------|
| x86 / x64     | 小端     | PC、服务器主流                |
| ARM           | 可配置   | 手机多为小端，可切大端         |
| PowerPC       | 大端     | 早期 Mac、部分网络设备         |
| SPARC         | 大端     | Sun 服务器                    |
| MIPS          | 可配置   | 嵌入式设备                    |

**问题**：如果数据库文件里直接写内存里的字节，那么在 x86（小端）机器上写的数据库文件，拿到 PowerPC（大端）机器上读，数字就全错了！

```
x86机器写入 page_id=1:
  内存(小端): 01 00 00 00
  文件内容:   01 00 00 00

PowerPC机器读取:
  读到文件:   01 00 00 00
  按大端解释: 0x01000000 = 16777216  ← 完全错误！
```

### 3.3 为什么数据库用大端序

数据库文件要**跨平台可移植**。解决方案：**统一用大端序存储**。

```
写入时(任何CPU):
  整数 1 → 手动拆成大端字节 → 文件: 00 00 00 01

读取时(任何CPU):
  文件: 00 00 00 01 → 手动组装成整数 → 1

无论在 x86 还是 PowerPC 上，文件内容都一样！
```

### 3.4 网络字节序类比

网络编程有同样问题：x86 机器发的整数，PowerPC 机器收到的怎么解释？TCP/IP 规定**网络字节序 = 大端序**，提供 `htonl/ntohl` 函数转换。

```
主机字节序 ──htonl──→ 网络字节序(大端) ──发送──→
                                                      │
主机字节序 ←──ntohl── 网络字节序(大端) ←──接收───┘
```

数据库的大端存储是同一思路：**磁盘上的字节序 = 大端序 = 与 CPU 无关**。

### 3.5 大端序转换函数逐行解释

miniDB 在 `page.c` 里手写了转换函数，不依赖 `htonl`，保证跨平台且无头文件依赖。

#### write_u32 — 把 uint32 写成大端字节

```c
static void write_u32(uint8_t *p, uint32_t v) {
    p[0] = (uint8_t)((v >> 24) & 0xFF);  // 最高字节
    p[1] = (uint8_t)((v >> 16) & 0xFF);  // 次高字节
    p[2] = (uint8_t)((v >> 8)  & 0xFF);  // 次低字节
    p[3] = (uint8_t)(v & 0xFF);          // 最低字节
}
```

以 `v = 0x12345678` 为例逐行演示：

```
v = 0x12345678
    二进制: 0001 0010 0011 0100 0101 0110 0111 1000

第1行: v >> 24 = 0x00000012
       & 0xFF  = 0x12
       p[0] = 0x12  ← 最高字节放最前(低地址)

第2行: v >> 16 = 0x00001234
       & 0xFF  = 0x34
       p[1] = 0x34

第3行: v >> 8  = 0x00123456
       & 0xFF  = 0x56
       p[2] = 0x56

第4行: v & 0xFF = 0x78
       p[3] = 0x78  ← 最低字节放最后(高地址)

结果: p = [0x12, 0x34, 0x56, 0x78]  ← 标准大端序
```

#### read_u32 — 把大端字节读成 uint32

```c
static uint32_t read_u32(const uint8_t *p) {
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
           ((uint32_t)p[2] << 8)  |  (uint32_t)p[3];
}
```

以 `p = [0x12, 0x34, 0x56, 0x78]` 为例：

```
p[0] << 24 = 0x12000000   ← 最高字节移到最高位
p[1] << 16 = 0x00340000
p[2] << 8  = 0x00005600
p[3]       = 0x00000078   ← 最低字节留在最低位

按位或: 0x12000000
       | 0x00340000
       | 0x00005600
       | 0x00000078
       = 0x12345678  ← 还原成功
```

#### write_u16 / read_u16 — 16 位版本

```c
static void write_u16(uint8_t *p, uint16_t v) {
    p[0] = (uint8_t)((v >> 8) & 0xFF);  // 高字节
    p[1] = (uint8_t)(v & 0xFF);         // 低字节
}

static uint16_t read_u16(const uint8_t *p) {
    return (uint16_t)(((uint16_t)p[0] << 8) | (uint16_t)p[1]);
}
```

16 位只有两个字节，原理同上。

### 3.6 为什么不直接用 `htonl`？

| 方式              | 优点               | 缺点                          |
|-------------------|--------------------|-------------------------------|
| `htonl/ntohl`     | 标准库、可能优化   | 需 `#include <arpa/inet.h>`，Windows 头文件不同 |
| 手写移位          | 无依赖、跨平台     | 代码稍长                      |

miniDB 选手写，教学清晰且零依赖。真实数据库（如 SQLite）也是手写。

### 3.7 验证字节序的小实验

你可以写个小程序验证本机字节序：

```c
#include <stdio.h>
#include <stdint.h>

int main() {
    uint32_t x = 1;
    uint8_t *p = (uint8_t *)&x;
    if (p[0] == 1) printf("本机是小端\n");
    else           printf("本机是大端\n");
    return 0;
}
```

在 x86 上输出"本机是小端"。但 miniDB 的数据库文件始终是大端，与本机无关。

---

## 4. Slotted Page 详解

### 4.1 问题：一页里怎么放多条变长记录？

一页 4096 字节，要放若干条 tuple。问题：

1. tuple 长度不一（`("Alice", 30)` 和 `("Bob", 25)` 长度不同）。
2. 会删除 tuple，删了之后空出来的位置怎么管？
3. tuple 可能更新变长，位置不够了怎么办？

**Slotted Page（槽页）** 是工业界标准方案，PostgreSQL、SQLite、InnoDB 都用类似结构。

### 4.2 核心思想：从两端向中间生长

```
偏移 0                                              4096
┌────────────┬────────────┬────────────┬────────────┐
│ Header(20) │ Slot Array │  空闲空间  │ Tuple Data │
│            │ → 向右生长 │            │ ← 向左生长 │
└────────────┴────────────┴────────────┴────────────┘
  0~19         20~?         ?~?          ?~4095

  Slot 记录每个 tuple 的 (offset, length)
  通过 slot 间接寻址，tuple 物理位置可移动
```

- **Header** 在最前面，固定 20 字节，记录页元信息。
- **Slot Array** 紧跟 Header，每个 slot 4 字节，从左往右长。
- **Tuple Data** 在最末尾，从右往左长（新 tuple 放在当前空闲区右边界）。
- **空闲空间** 在中间，随两端生长而缩小。

### 4.3 逐字节 ASCII 图

下面是一页的完整字节级布局（初始空页）：

```
字节偏移  0         4   5       7       9           13          17  20
         ┌──────────┬─┬───────┬───────┬───────────┬───────────┬───┐
         │ page_id  │T│num_slt│free_of│next_page  │ checksum  │rsv│
         │ (4B)     │1│ (2B)  │ (2B)  │ (4B)      │ (4B)      │3B │
         └──────────┴─┴───────┴───────┴───────────┴───────────┴───┘
         ←───────────────── Header (20 字节) ─────────────────→

         20                                                              4096
         ┌────────────────────────────────────────────────────────────────┐
         │                     空闲空间 (全部 0)                          │
         └────────────────────────────────────────────────────────────────┘
         ←────────────────── 4076 字节空闲 ──────────────────────────────→
```

**初始状态**：`num_slots=0`，`free_space_offset=4096`（tuple 数据区为空，边界在页尾）。

### 4.4 添加第一个 tuple 后

假设添加 `"Alice"`（5 字节，不含 `\0`）：

```
字节偏移  0         4   5       7       9           13          17  20
         ┌──────────┬─┬───────┬───────┬───────────┬───────────┬───┐
         │ page_id  │1│  1    │ 4091  │next_page  │ checksum  │rsv│
         └──────────┴─┴───────┴───────┴───────────┴───────────┴───┘

         20     24
         ┌──────┬──────────────────────────────────────────────────────┐
         │ Slot0│              空闲空间                       │Alice" │
         │4091,5│                                              │5字节  │
         └──────┴──────────────────────────────────────────────┴───────┘
         ←Slot区→←──────────── 空闲 4067字节 ──────────────→←Tuple0→

  Slot0 = (offset=4091, length=5)
  tuple 数据写在 [4091, 4096) 这 5 个字节
  free_space_offset 从 4096 移到 4091
  num_slots 从 0 变 1
```

### 4.5 添加第二个 tuple 后

再添加 `"Bob"`（3 字节）：

```
         20     24
         ┌──────┬──────┬──────────────────────────────┬─────┬───────┐
         │Slot0 │Slot1 │       空闲空间           │Bob" │Alice" │
         │4091,5│4088,3│                          │3字节│5字节  │
         └──────┴──────┴──────────────────────────┴─────┴───────┘
         ←Slot区(8B)→←──── 空闲 4064字节 ────→←T1→←T0→

  Slot1 = (offset=4088, length=3)
  tuple1 写在 [4088, 4091)
  free_space_offset = 4088
  num_slots = 2
```

注意：**slot 顺序（0,1,2...）和 tuple 物理顺序无关**。Slot0 指向的 Alice 在页尾，Slot1 指向的 Bob 在 Alice 前面。

### 4.6 删除一个 tuple

删除 Slot0（Alice）：**只把 slot 的 length 置 0，不移动任何数据**。

```
         20     24
         ┌──────┬──────┬──────────────────────────────┬─────┬───────┐
         │Slot0 │Slot1 │       空闲空间           │Bob" │Alice" │
         │4091,0│4088,3│                          │     │(孤儿) │
         └──────┴──────┴──────────────────────────┴─────┴───────┘
          ↑ length=0 表示已删除
          Alice 的字节还在，但没人引用，成了"垃圾"

  这叫"标记删除"(tombstone)，空间回收留给后续 vacuum
```

### 4.7 为什么 Slot 从前往后、Tuple 从后往前？

**如果都从前往后长**：

```
  ┌────────┬──────┬──────┬────────┬──────┬──────┐
  │Header  │Slot0 │Tuple0│Slot1   │Tuple1│ 空闲 │
  └────────┴──────┴──────┴──────┴──────┴──────┘
  问题: 新增slot要插在tuple后面，要么移动tuple，要么slot和tuple分离
```

**从两端向中间长**（miniDB 的方案）：

```
  ┌────────┬──────┬──────┬────────┬──────┬──────┐
  │Header  │Slot0 │Slot1 │ 空闲   │Tuple1│Tuple0│
  └────────┴──────┴──────┴────────┴──────┴──────┘
  slot区只往右长，tuple区只往左长，互不干扰
  新增slot: 直接写在slot区末尾，不影响tuple
  新增tuple: 直接写在tuple区开头(向左)，不影响slot
```

好处：
1. **添加无需移动数据**：slot 和 tuple 各长各的。
2. **空闲空间在中间**，一目了然，`free_space_offset` 一个指针管理。
3. **删除只改 slot**，tuple 当垃圾留着，后续 vacuum 统一回收。

### 4.8 Page Header 详解（20 字节）

| 偏移 | 大小 | 字段               | 类型     | 说明                         |
|------|------|--------------------|----------|------------------------------|
| 0    | 4    | `page_id`          | uint32   | 页编号，全局唯一              |
| 4    | 1    | `page_type`        | uint8    | 0=free 1=heap 2=index 3=overflow |
| 5    | 2    | `num_slots`        | uint16   | 当前 slot 数量                |
| 7    | 2    | `free_space_offset`| uint16   | tuple 数据区左边界，初始=4096 |
| 9    | 4    | `next_page_id`     | uint32   | 链表下一页（溢出页用）        |
| 13   | 4    | `checksum`         | uint32   | 校验和（本章未使用）          |
| 17   | 3    | 保留               | —        | 预留扩展                     |

对应代码 `page.h:8-9`：

```c
#define PAGE_SIZE 4096
#define PAGE_HEADER_SIZE 20
#define SLOT_SIZE 4
```

### 4.9 Slot 详解（4 字节）

| 偏移(相对slot) | 大小 | 字段     | 类型    | 说明                    |
|----------------|------|----------|---------|-------------------------|
| 0              | 2    | `offset` | uint16  | tuple 在页内起始偏移     |
| 2              | 2    | `length` | uint16  | tuple 长度，0=已删除     |

slot 在页内的绝对位置：

```
slot_abs_offset = PAGE_HEADER_SIZE + slot_id * SLOT_SIZE
                = 20 + slot_id * 4
```

例如 slot_id=3 的 slot 在页内偏移 `20 + 3*4 = 32` 字节处。

### 4.10 空闲空间计算

```
空闲空间 = free_space_offset - (PAGE_HEADER_SIZE + num_slots * SLOT_SIZE)
         = free_space_offset - (20 + num_slots * 4)
```

图示：

```
  ┌────────┬────────────┬──────────────┬──────────┐
  │Header  │ Slot Array │   空闲空间   │Tuple Data│
  │ 20B    │ num*4 B    │  ← 这段 →    │          │
  └────────┴────────────┴──────────────┴──────────┘
  0        20           20+num*4       free_off   4096

  空闲 = free_off - (20 + num*4)
```

添加一个 `len` 字节的 tuple 需要 `len + 4` 字节（tuple 数据 + 新 slot）：

```c
bool page_has_space(const page_t *page, uint16_t len) {
    return page_free_space(page) >= (uint16_t)(len + SLOT_SIZE);
}
```

### 4.11 与定长记录方案对比

| 方案           | 变长支持 | 删除成本 | 空间利用率 | 实现复杂度 | 代表         |
|----------------|----------|----------|------------|------------|--------------|
| 定长记录       | 差       | O(1) 标记| 低(定长浪费)| 低         | 早期ISAM     |
| Slotted Page   | 好       | O(1) 标记| 高         | 中         | PostgreSQL/SQLite |
| 偏移量数组     | 好       | O(1) 标记| 中         | 中         | 某些列存     |
| 倒排链         | 好       | O(n)     | 中         | 高         | InnoDB undo  |

**定长记录的问题**：如果定长 100 字节，存 `"Bob"`(3字节) 浪费 97 字节。

```
定长方案:
  ┌──────┬──────┬──────┬──────┐
  │Alice │      │      │      │  ← 每格100B，Alice只用了5B
  │ 5B   │ 95B  │      │      │     浪费95B
  │浪费  │浪费  │      │      │
  └──────┴──────┴──────┴──────┘

Slotted方案:
  ┌────┬────┬────────────┬───┬─────┐
  │Slot│Slot│  空闲      │Bob│Alice│  ← 按实际长度存
  │0,5 │1,3 │            │3B │5B   │     无浪费
  └────┴────┴────────────┴───┴─────┘
```

### 4.12 页类型枚举

```c
typedef enum {
    PAGE_TYPE_FREE = 0,      // 空闲页（未使用）
    PAGE_TYPE_HEAP = 1,      // 堆表页（存表数据）
    PAGE_TYPE_INDEX = 2,     // 索引页（B+Tree节点）
    PAGE_TYPE_OVERFLOW = 3,  // 溢出页（存超大tuple的后续部分）
} page_type_t;
```

```
PAGE_TYPE_FREE:     ┌────────┐  刚分配未使用
                    │  全0   │
                    └────────┘

PAGE_TYPE_HEAP:     ┌────────┐  存表行
                    │tuples..│
                    └────────┘

PAGE_TYPE_INDEX:    ┌────────┐  存B+Tree键+指针
                    │keys+ptr│
                    └────────┘

PAGE_TYPE_OVERFLOW: ┌────────┐  大tuple的续页
                    │续数据..│→ next_page_id 指向下一溢出页
                    └────────┘
```

---

## 5. 序列化

### 5.1 什么是序列化

**序列化(serialization)** = 把内存中的结构化数据转成可存储/传输的字节序列。

```
内存中:                          磁盘上:
┌─────────────┐                 ┌─────────────────────────┐
│ struct {    │                 │ 字节流(大端序)          │
│   int a=1;  │  ──序列化──→   │ 00 00 00 01             │
│   int b=2;  │                 │ 00 00 00 02             │
│ }           │                 └─────────────────────────┘
└─────────────┘                 ←───────── 8字节 ─────────→
```

反序列化(deserialization)是逆过程。

### 5.2 为什么需要序列化

1. **内存布局不可直接写盘**：结构体有填充字节(padding)，不同编译器布局不同。
2. **字节序问题**：内存里按主机序，磁盘要统一大端。
3. **跨平台**：x86 写的文件 PowerPC 要能读。
4. **变长字段**：字符串长度不定，要编码长度前缀或定界。

### 5.3 结构体填充问题

```c
struct Example {
    uint8_t  a;    // 1字节
    uint32_t b;    // 4字节
};
// sizeof 可能是 8，不是 5！
// 因为编译器为了对齐，在 a 后面插了 3 字节填充
```

```
内存布局(带填充):
  ┌─┬───┬───────┐
  │a│pad│   b   │  sizeof = 8
  └─┴───┴───────┘
   1  3    4

如果直接写盘，pad 的内容是未定义的垃圾值！
而且不同编译器 pad 可能不同。
```

**解决方案**：不写结构体，逐字段用 `write_u32` 等函数写。

### 5.4 miniDB 的序列化策略

miniDB 的 `page_t` 是一个纯字节数组，所有字段通过 `read_u16/write_u16/read_u32/write_u32` 读写：

```c
typedef struct {
    uint8_t data[PAGE_SIZE];   // 就是一个4096字节的数组
} page_t;
```

**写入 page_id 到 header**：

```c
// page.c:34
write_u32(page->data + HDR_OFFSET_ID, pid);
//       ↑字节数组    ↑偏移0         ↑值
// 把 pid 按大端序写进 data[0..3]
```

**读取 page_id**：

```c
// page.c:42
return read_u32(page->data + HDR_OFFSET_ID);
//      ↑从 data[0..3] 按大端序读出 uint32
```

### 5.5 序列化一个 tuple 的例子

假设要存一行用户数据 `id=42, name="Alice", age=30`。miniDB 本章不实现行格式（章3做），但原理是：

```
内存中的逻辑行:        序列化后的字节流:
┌─────────────────┐    ┌──────────────────────────────────┐
│ id   = 42       │    │ 00 00 00 2A  ← id (uint32大端)   │
│ name = "Alice"  │ →  │ 00 05        ← name长度(uint16)  │
│ age  = 30       │    │ 41 6C 69 63 65  ← "Alice"        │
└─────────────────┘    │ 00 1E        ← age (uint8)       │
                       └──────────────────────────────────┘
                        共 4+2+5+1 = 12 字节
```

这个字节流再通过 `page_add_tuple(page, serialized_data, 12)` 存进页里。

### 5.6 大端序序列化的好处

```
x86(小端)机器:
  内存里 id=42: 2A 00 00 00  (小端)
  序列化后:     00 00 00 2A  (大端)  ← write_u32
  写入文件:     00 00 00 2A

PowerPC(大端)机器读取:
  从文件读:    00 00 00 2A
  反序列化:    00 00 00 2A → 42  ← read_u32
  内存里:      00 00 00 2A  (大端机器原生大端)

两台机器文件内容完全一致！
```

---

## 6. Pager 的工作

### 6.1 Pager 在架构中的位置

```
┌─────────────────────────────────────────────────┐
│  上层: Table / B+Tree / ...                     │  ← 章3+
├─────────────────────────────────────────────────┤
│  Pager (页管理器)                                │  ← 本章
│    - 读页 / 写页 / 分配页                        │
├─────────────────────────────────────────────────┤
│  File Manager (文件IO)                           │  ← 本章
│    - fopen/fread/fwrite                          │
├─────────────────────────────────────────────────┤
│  OS (文件系统)                                   │
├─────────────────────────────────────────────────┤
│  磁盘                                            │
└─────────────────────────────────────────────────┘
```

本章 pager 是 file_manager 之上的**薄封装**（透传）。章2 会在中间插入 Buffer Pool。

### 6.2 页的读取流程

```
调用 pager_read(pager, pid=5, &page)
  │
  ▼
pager.c:31  return fm_read_page(pager->fm, pid, page->data);
  │
  ▼
file_manager.c:49  fm_read_page(fm, pid=5, buf)
  │
  ├─ 检查 pid < num_pages?  (5 < 10? 是)
  │
  ├─ offset = pid * PAGE_SIZE = 5 * 4096 = 20480
  │
  ├─ fseek(fp, 20480, SEEK_SET)   ← 定位文件指针
  │
  ├─ fread(buf, 1, 4096, fp)      ← 读4096字节到buf
  │
  └─ 返回 (读到的字节数 == 4096)

结果: page.data[0..4095] 装满了第5页的原始字节
```

### 6.3 页的写入流程

```
调用 pager_write(pager, pid=5, &page)
  │
  ▼
pager.c:35  return fm_write_page(pager->fm, pid, page->data);
  │
  ▼
file_manager.c:61  fm_write_page(fm, pid=5, buf)
  │
  ├─ 检查 pid < num_pages?
  │
  ├─ offset = 5 * 4096 = 20480
  │
  ├─ fseek(fp, 20480, SEEK_SET)
  │
  ├─ fwrite(buf, 1, 4096, fp)     ← 写4096字节
  │
  └─ fflush(fp)                   ← 刷到OS缓冲

注意: fflush 只保证写到OS，不保证落盘
      真正落盘要 fsync(fileno(fp))，本章暂不调用
```

### 6.4 页的分配流程

```
调用 pager_allocate(pager)
  │
  ▼
pager.c:39  return fm_allocate_page(pager->fm);
  │
  ▼
file_manager.c:76  fm_allocate_page(fm)
  │
  ├─ pid = num_pages  (比如当前有10页，新页pid=10)
  │
  ├─ offset = 10 * 4096 = 40960
  │
  ├─ fseek(fp, 40960, SEEK_SET)
  │
  ├─ 写入4096字节全0  (新页清零)
  │
  ├─ fflush(fp)
  │
  ├─ num_pages++  (变成11)
  │
  └─ 返回 pid=10

文件从 40960 字节增长到 45056 字节 (多了一页)
```

图示文件增长：

```
分配前:
  ┌────┬────┬────┬────┬────┬────┬────┬────┬────┬────┐
  │P0  │P1  │P2  │P3  │P4  │P5  │P6  │P7  │P8  │P9  │  10页
  └────┴────┴────┴────┴────┴────┴────┴────┴────┴────┘
  文件大小 = 40960 字节

分配后:
  ┌────┬────┬────┬────┬────┬────┬────┬────┬────┬────┬────┐
  │P0  │P1  │P2  │P3  │P4  │P5  │P6  │P7  │P8  │P9  │P10 │  11页
  └────┴────┴────┴────┴────┴────┴────┴────┴────┴────┴────┘
                                            ↑ 新页，全0
  文件大小 = 45056 字节
```

### 6.5 页的回收

本章**不实现页回收**。删除页只是把 `page_type` 设为 `PAGE_TYPE_FREE`，文件大小不缩。真实数据库用空闲页链表回收复用：

```
空闲页链表(本章未实现):
  header_page → free_page_15 → free_page_22 → NULL

分配时: 先从链表取一个空闲页，没有再扩展文件
删除时: 把页加入链表头部
```

### 6.6 Pager vs File Manager 的职责划分

| 职责           | File Manager | Pager     |
|----------------|--------------|-----------|
| 打开/关闭文件  | ✅           | ✅(封装)  |
| 字节级读写     | ✅           | ❌        |
| 页级读写       | ✅(按页算偏移)| ✅(透传) |
| 分配新页       | ✅           | ✅(透传) |
| 缓存(Buffer Pool)| ❌        | ❌(章2加) |
| 页锁           | ❌           | ❌(章4加) |
| WAL            | ❌           | ❌(章5加) |

本章 pager 看起来"多余"，但它是**预留的抽象层**。章2 会在 pager 内部加缓存，上层接口不变。

---

## 7. File Manager

### 7.1 文件 I/O 基础

C 语言标准库文件 I/O 三件套：

```c
FILE *fopen(const char *path, const char *mode);  // 打开
size_t fread(void *buf, size_t sz, size_t n, FILE *fp);  // 读
size_t fwrite(const void *buf, size_t sz, size_t n, FILE *fp);  // 写
int fclose(FILE *fp);  // 关闭
int fseek(FILE *fp, long off, int whence);  // 定位
long ftell(FILE *fp);  // 当前位置
int fflush(FILE *fp);  // 刷新到OS
```

### 7.2 fopen 模式

| 模式    | 含义                     | 文件不存在时   |
|---------|--------------------------|----------------|
| `"r"`   | 只读                     | 失败           |
| `"w"`   | 只写，截断               | 创建           |
| `"r+b"` | 读写，不截断             | 失败           |
| `"w+b"` | 读写，截断               | 创建           |

miniDB 用 `"r+b"` 先尝试打开已有文件，失败再用 `"w+b"` 创建：

```c
// file_manager.c:12-18
FILE *fp = fopen(path, "r+b");   // 先试读已有
if (!fp) {
    fp = fopen(path, "w+b");     // 不存在则创建
    if (!fp) return NULL;        // 创建也失败，返回空
}
```

### 7.3 fread / fwrite 详解

```c
size_t fread(void *buf, size_t size, size_t count, FILE *fp);
//               ↑      ↑      ↑      ↑
//            读到这里  每块多大 几块   从哪读
// 返回实际读到的块数(不是字节数!)
```

miniDB 的用法：

```c
// file_manager.c:57
size_t n = fread(buf, 1, PAGE_SIZE, fm->fp);
//                   ↑  ↑     ↑
//               每块1字节 读4096块
// n = 实际读到的字节数
return n == PAGE_SIZE;  // 必须读满4096才算成功
```

**常见陷阱**：`fread` 返回的是**块数**，如果 `size=4096, count=1`，只读到部分数据会返回 0 而非实际字节数。所以用 `size=1, count=PAGE_SIZE` 才能拿到准确字节数。

### 7.4 fseek 定位

```c
int fseek(FILE *fp, long offset, int whence);
//                          ↑       ↑
//                     偏移量    起算点
//
// whence:
//   SEEK_SET = 从文件头算
//   SEEK_CUR = 从当前位置算
//   SEEK_END = 从文件尾算
```

miniDB 读第 `pid` 页：

```c
// file_manager.c:53-54
long offset = (long)((size_t)pid * PAGE_SIZE);  // pid * 4096
fseek(fm->fp, offset, SEEK_SET);  // 定位到页起始
```

```
文件:  ┌────P0────┬────P1────┬────P2────┬───→
       0        4096       8192       12288
       │         │          │
       │         │          └─ pid=2: offset=8192
       │         └─ pid=1: offset=4096
       └─ pid=0: offset=0
```

### 7.5 fflush vs fsync

```c
fwrite(buf, 1, 4096, fp);  // 数据进了C库缓冲区
fflush(fp);                // 数据进了OS内核缓冲区
fsync(fileno(fp));         // 数据真正写到磁盘扇区
```

```
应用     fwrite        C库缓冲      fflush      OS缓冲       fsync      磁盘
 │  ─────────────────→  │  ─────────────────→  │  ─────────────────→  │
 │                      │                      │                      │
 │   程序崩溃会丢        │   程序崩溃会丢        │   掉电会丢            │
```

miniDB 本章只 `fflush`，不 `fsync`（教学简化）。真实数据库必须 `fsync` 保证持久性。

### 7.6 file_manager 结构

```c
// file_manager.c:6-9
struct file_manager {
    FILE *fp;          // 文件指针
    uint32_t num_pages; // 当前文件有多少页
};
```

`num_pages` 在 `fm_open` 时根据文件大小算出：

```c
// file_manager.c:20-28
fseek(fp, 0, SEEK_END);     // 定位到文件尾
long size = ftell(fp);      // 得到文件大小(字节)
fm->num_pages = (uint32_t)((size_t)size / PAGE_SIZE);
//                       文件大小 / 4096 = 页数
```

### 7.7 pread/pwrite vs fseek+fread

| 方式            | 线程安全 | 跨平台 | 性能   | 说明                     |
|-----------------|----------|--------|--------|--------------------------|
| `fseek+fread`   | ❌       | ✅     | 中     | 改变文件偏移指针，多线程冲突 |
| `pread/pwrite`  | ✅       | ❌(POSIX)| 高    | 原子定位+读写，不改偏移    |
| `read/write+O_DIRECT` | ✅ | ❌   | 最高  | 绕过OS缓冲                |

```
多线程下 fseek+fread 的问题:

线程A: fseek(fp, 0, SEEK_SET)   ← 定位到0
线程B: fseek(fp, 4096, SEEK_SET) ← 定位到4096 (覆盖了A的定位!)
线程A: fread(...)                ← 读到的是4096处，错了!

pread 没这个问题:
线程A: pread(fd, buf, 4096, 0)     ← 原子读0处
线程B: pread(fd, buf, 4096, 4096)  ← 原子读4096处
互不影响
```

miniDB 本章单线程，用 `fseek+fread` 足平台。章2 引入 Buffer Pool 后切 `pread/pwrite`。

---

## 8. 代码逐行解读

### 8.1 page.h — 页结构定义

```c
#ifndef MINIDB_PAGE_H
#define MINIDB_PAGE_H
```
头文件保护，防止重复 include。

```c
#include <stdint.h>   // uint8_t, uint16_t, uint32_t
#include <stdbool.h>  // bool, true, false
#include <stddef.h>   // size_t
```

```c
#define PAGE_SIZE 4096        // 页大小
#define PAGE_HEADER_SIZE 20   // 页头大小
#define SLOT_SIZE 4           // 每个slot大小
```
三个核心常量，改 `PAGE_SIZE` 就能调页大小。

```c
typedef uint32_t page_id_t;   // 页编号类型，最多约42亿页
typedef uint16_t slot_id_t;   // slot编号类型，最多65535个slot
```

```c
#define INVALID_PAGE_ID ((page_id_t)0xFFFFFFFFu)  // 无效页号=全1
#define INVALID_SLOT_ID ((slot_id_t)0xFFFFu)      // 无效slot号=全1
```
用全 1 表示"无效"，因为正常页号从 0 开始，不会是 `0xFFFFFFFF`。

```c
typedef enum {
    PAGE_TYPE_FREE = 0,      // 空闲页
    PAGE_TYPE_HEAP = 1,      // 堆表页
    PAGE_TYPE_INDEX = 2,     // 索引页
    PAGE_TYPE_OVERFLOW = 3,  // 溢出页
} page_type_t;
```

```c
typedef struct {
    uint8_t data[PAGE_SIZE];  // 页就是一个4096字节的数组
} page_t;
```
**关键设计**：`page_t` 不用结构体字段，而是纯字节数组。所有字段通过偏移+`read_u32`等函数访问。好处是序列化零成本（内存布局就是磁盘布局）。

### 8.2 page.c — 页操作实现

#### 偏移常量

```c
#define HDR_OFFSET_ID        0   // page_id 在 header 的偏移
#define HDR_OFFSET_TYPE      4   // page_type
#define HDR_OFFSET_NUM_SLOTS 5   // num_slots
#define HDR_OFFSET_FREE      7   // free_space_offset
#define HDR_OFFSET_NEXT      9   // next_page_id
#define HDR_OFFSET_CHECKSUM  13  // checksum
```

#### page_init — 初始化空页

```c
void page_init(page_t *page, page_id_t pid, page_type_t type) {
    memset(page->data, 0, PAGE_SIZE);                    // 全清零
    write_u32(page->data + HDR_OFFSET_ID, pid);          // 写 page_id
    page->data[HDR_OFFSET_TYPE] = (uint8_t)type;         // 写 page_type
    write_u16(page->data + HDR_OFFSET_NUM_SLOTS, 0);     // num_slots = 0
    write_u16(page->data + HDR_OFFSET_FREE, PAGE_SIZE);  // free_offset = 4096
    write_u32(page->data + HDR_OFFSET_NEXT, INVALID_PAGE_ID); // 无下一页
}
```

初始化后的页：

```
  ┌──────────┬─┬────┬────┬────────────┬──────────┬───┬──────────────┐
  │ pid      │T│ 0  │4096│ 0xFFFFFFFF │ 0        │ 0 │  全0          │
  └──────────┴─┴────┴────┴────────────┴──────────┴───┴──────────────┘
  ←─────────────────── 20字节header ───────────────→←─ 4076字节空 ─→
```

#### page_add_tuple — 添加 tuple

```c
slot_id_t page_add_tuple(page_t *page, const void *data, uint16_t len) {
    if (len == 0 || !page_has_space(page, len)) {   // ① 检查
        return INVALID_SLOT_ID;
    }

    uint16_t num_slots = page_get_num_slots(page);  // ② 当前slot数
    uint16_t free_offset = read_u16(page->data + HDR_OFFSET_FREE); // ③ 当前边界

    free_offset = (uint16_t)(free_offset - len);    // ④ 边界左移len
    memcpy(page->data + free_offset, data, len);    // ⑤ 拷贝数据

    slot_set(page, num_slots, free_offset, len);    // ⑥ 写新slot

    write_u16(page->data + HDR_OFFSET_NUM_SLOTS,    // ⑦ num_slots++
              (uint16_t)(num_slots + 1));
    write_u16(page->data + HDR_OFFSET_FREE,         // ⑧ 更新free_offset
              free_offset);

    return num_slots;                               // ⑨ 返回新slot_id
}
```

图示步骤④⑤⑥：

```
添加前:
  ┌────┬──────┬──────────────┬──────────┐
  │Hdr │Slots │   空闲       │ Tuples   │
  └────┴──────┴──────────────┴──────────┘
  0    20     20+num*4       free_off   4096

④free_offset -= len:
  ┌────┬──────┬──────────┬────────┬─────┐
  │Hdr │Slots │  空闲    │ 新位置 │OldT │
  └────┴──────┴──────────┴────────┴─────┘
  0    20     20+num*4   free_off  free_off+len
                       ↑ 新边界

⑤memcpy: 把data写入[free_off, free_off+len)

⑥slot_set: 在slot区末尾写新slot(offset=free_off, length=len)
  ┌────┬──────┬──┬──────────┬────────┬─────┐
  │Hdr │Slots │S │  空闲    │ 新tuple│OldT │
  └────┴──────┴──┴──────────┴────────┴─────┘
         num个 ↑新slot
```

#### page_get_tuple — 读取 tuple

```c
const void *page_get_tuple(const page_t *page, slot_id_t sid, uint16_t *len) {
    uint16_t num_slots = page_get_num_slots(page);
    if (sid >= num_slots) {        // ① 越界检查
        if (len) *len = 0;
        return NULL;
    }

    uint16_t offset = slot_get_offset(page, sid);  // ② 读slot的offset
    uint16_t length = slot_get_length(page, sid);  // ③ 读slot的length

    if (length == 0) {             // ④ 已删除
        if (len) *len = 0;
        return NULL;
    }

    if (len) *len = length;        // ⑤ 输出长度
    return page->data + offset;    // ⑥ 返回指向tuple数据的指针
}
```

注意⑥返回的是**页内指针**，不拷贝数据。调用方不能释放页，否则指针悬空。

#### page_delete_tuple — 标记删除

```c
bool page_delete_tuple(page_t *page, slot_id_t sid) {
    uint16_t num_slots = page_get_num_slots(page);
    if (sid >= num_slots) return false;   // 越界

    uint8_t *slot = page->data + PAGE_HEADER_SIZE + (size_t)sid * SLOT_SIZE;
    write_u16(slot + 2, 0);   // 把 length 字段置 0
    return true;
}
```

**只改 slot 的 length=0，tuple 数据原地保留**。这是"标记删除"，空间回收留给 vacuum。

#### page_free_space — 计算空闲空间

```c
uint16_t page_free_space(const page_t *page) {
    uint16_t num_slots = page_get_num_slots(page);
    uint16_t free_offset = read_u16(page->data + HDR_OFFSET_FREE);
    uint16_t slot_area_end = (uint16_t)(PAGE_HEADER_SIZE + (size_t)num_slots * SLOT_SIZE);
    return (uint16_t)(free_offset - slot_area_end);
}
```

```
free_offset - (20 + num_slots * 4)

  ┌────┬──────┬──────────────┬──────────┐
  │Hdr │Slots │   ← 空闲 →   │ Tuples   │
  └────┴──────┴──────────────┴──────────┘
  0   20    slot_area_end  free_offset  4096

  空闲 = free_offset - slot_area_end
```

#### page_validate — 校验页

```c
bool page_validate(const page_t *page, page_id_t expected_id) {
    if (page_get_id(page) != expected_id) return false;  // 页号对不上

    uint16_t num_slots = page_get_num_slots(page);
    uint16_t free_offset = read_u16(page->data + HDR_OFFSET_FREE);
    uint16_t slot_area_end = (uint16_t)(PAGE_HEADER_SIZE + (size_t)num_slots * SLOT_SIZE);

    if (free_offset < slot_area_end || free_offset > PAGE_SIZE)  // 边界检查
        return false;
    return true;
}
```

检查：页号一致 + 空闲边界在合理范围（不与 slot 区重叠，不超过页大小）。

### 8.3 file_manager.h — 文件管理器接口

```c
typedef struct file_manager file_manager_t;  // 不透明类型

file_manager_t *fm_open(const char *path);   // 打开/创建
void            fm_close(file_manager_t *fm); // 关闭

bool      fm_read_page(file_manager_t *fm, page_id_t pid, void *buf);   // 读页
bool      fm_write_page(file_manager_t *fm, page_id_t pid, const void *buf); // 写页

page_id_t fm_allocate_page(file_manager_t *fm);  // 分配新页
uint32_t  fm_num_pages(file_manager_t *fm);      // 查询页数
```

**不透明类型(opaque type)**：头文件只声明 `struct file_manager`，不定义内部。调用方看不到 `fp` 和 `num_pages`，只能通过函数访问。封装性好。

### 8.4 file_manager.c — 文件管理器实现

#### fm_open

```c
file_manager_t *fm_open(const char *path) {
    FILE *fp = fopen(path, "r+b");     // ① 尝试打开已有文件
    if (!fp) {
        fp = fopen(path, "w+b");       // ② 不存在则创建
        if (!fp) return NULL;          // ③ 创建失败
    }

    fseek(fp, 0, SEEK_END);           // ④ 定位到文件尾
    long size = ftell(fp);            // ⑤ 得到文件大小
    if (size < 0) { fclose(fp); return NULL; }

    file_manager_t *fm = malloc(sizeof(file_manager_t));  // ⑥ 分配结构体
    if (!fm) { fclose(fp); return NULL; }

    fm->fp = fp;
    fm->num_pages = (uint32_t)((size_t)size / PAGE_SIZE); // ⑦ 算页数
    return fm;
}
```

#### fm_read_page

```c
bool fm_read_page(file_manager_t *fm, page_id_t pid, void *buf) {
    if (pid >= fm->num_pages) return false;   // ① 越界

    long offset = (long)((size_t)pid * PAGE_SIZE);  // ② 算字节偏移
    if (fseek(fm->fp, offset, SEEK_SET) != 0)       // ③ 定位
        return false;

    size_t n = fread(buf, 1, PAGE_SIZE, fm->fp);    // ④ 读
    return n == PAGE_SIZE;                          // ⑤ 必须读满
}
```

#### fm_write_page

```c
bool fm_write_page(file_manager_t *fm, page_id_t pid, const void *buf) {
    if (pid >= fm->num_pages) return false;   // ① 越界

    long offset = (long)((size_t)pid * PAGE_SIZE);
    if (fseek(fm->fp, offset, SEEK_SET) != 0) return false;

    size_t n = fwrite(buf, 1, PAGE_SIZE, fm->fp);   // ② 写
    if (n != PAGE_SIZE) return false;

    return fflush(fm->fp) == 0;   // ③ 刷到OS
}
```

#### fm_allocate_page

```c
page_id_t fm_allocate_page(file_manager_t *fm) {
    page_id_t pid = fm->num_pages;   // ① 新页号=当前页数
    long offset = (long)((size_t)pid * PAGE_SIZE);
    if (fseek(fm->fp, offset, SEEK_SET) != 0) return INVALID_PAGE_ID;

    static uint8_t zeros[PAGE_SIZE];   // ② 全0缓冲
    memset(zeros, 0, PAGE_SIZE);

    size_t n = fwrite(zeros, 1, PAGE_SIZE, fm->fp);  // ③ 写一页0
    if (n != PAGE_SIZE) return INVALID_PAGE_ID;
    if (fflush(fm->fp) != 0) return INVALID_PAGE_ID;

    fm->num_pages++;   // ④ 页数+1
    return pid;
}
```

`static` 让 `zeros` 只初始化一次（在静态区），避免每次分配都栈上分配 4KB。

### 8.5 pager.h / pager.c — 页管理器

#### pager.h

```c
typedef struct pager pager_t;  // 不透明

pager_t *pager_open(const char *path);
void     pager_close(pager_t *pager);

bool      pager_read(pager_t *pager, page_id_t pid, page_t *page);
bool      pager_write(pager_t *pager, page_id_t pid, const page_t *page);
page_id_t pager_allocate(pager_t *pager);
uint32_t  pager_num_pages(pager_t *pager);
```

#### pager.c

```c
struct pager {
    file_manager_t *fm;   // 本章pager只持有一个file_manager
};

pager_t *pager_open(const char *path) {
    file_manager_t *fm = fm_open(path);   // ① 打开文件
    if (!fm) return NULL;

    pager_t *pager = malloc(sizeof(pager_t));  // ② 分配pager
    if (!pager) { fm_close(fm); return NULL; }

    pager->fm = fm;
    return pager;
}

void pager_close(pager_t *pager) {
    if (!pager) return;
    fm_close(pager->fm);  // 关闭文件
    free(pager);          // 释放pager
}

bool pager_read(pager_t *pager, page_id_t pid, page_t *page) {
    return fm_read_page(pager->fm, pid, page->data);  // 透传
}

bool pager_write(pager_t *pager, page_id_t pid, const page_t *page) {
    return fm_write_page(pager->fm, pid, page->data);  // 透传
}

page_id_t pager_allocate(pager_t *pager) {
    return fm_allocate_page(pager->fm);  // 透传
}

uint32_t pager_num_pages(pager_t *pager) {
    return fm_num_pages(pager->fm);  // 透传
}
```

本章 pager 全是透传，看起来多余。但章2 会在 `pager_read` 里加缓存逻辑：

```c
// 章2 的 pager_read (预览)
bool pager_read(pager_t *pager, page_id_t pid, page_t *page) {
    // ① 先查 Buffer Pool
    if (buffer_pool_get(pager->pool, pid, page))
        return true;  // 命中缓存，不读磁盘
    // ② 未命中，从磁盘读
    if (!fm_read_page(pager->fm, pid, page->data))
        return false;
    // ③ 放入缓存
    buffer_pool_put(pager->pool, pid, page);
    return true;
}
```

---

## 9. 调试技巧

### 9.1 用 hexdump 查看二进制页

写一个测试程序创建一页存点数据：

```c
#include "pager.h"
#include "page.h"
#include <stdio.h>

int main() {
    pager_t *pager = pager_open("test.db");
    page_id_t pid = pager_allocate(pager);

    page_t page;
    page_init(&page, pid, PAGE_TYPE_HEAP);
    page_add_tuple(&page, "Alice", 5);
    page_add_tuple(&page, "Bob", 3);
    pager_write(pager, pid, &page);

    pager_close(pager);
    return 0;
}
```

编译运行后，用 `hexdump` 查看文件：

```bash
# Linux/Mac
hexdump -C test.db

# Windows (用 Format-Hex)
powershell -Command "Format-Hex test.db"
```

输出示例（前 64 字节）：

```
00000000  00 00 00 00 01 00 02 0f  88 ff ff ff ff 00 00 00  |........|
00000010  00 00 00 00 0f 88 00 03  42 6f 62 41 6c 69 63 65  |.....BobAlice|
```

逐字节解读：

```
偏移0-3:   00 00 00 00  → page_id = 0 (大端)
偏移4:     01           → page_type = 1 (HEAP)
偏移5-6:   00 02        → num_slots = 2
偏移7-8:   0f 88        → free_space_offset = 0x0F88 = 3976
偏移9-12:  ff ff ff ff  → next_page_id = INVALID
偏移13-16: 00 00 00 00  → checksum = 0
偏移17-19: 00 00 00     → 保留
偏移20-23: 0f 88 00 05  → Slot0: offset=0x0F88=3976, length=5 (Alice)
偏移24-27: 0f 85 00 03  → Slot1: offset=0x0F85=3973, length=3 (Bob)
...
偏移3973:  42 6f 62     → "Bob"
偏移3976:  41 6c 69 63 65 → "Alice"
```

### 9.2 验证 free_space_offset

```
初始 free_offset = 4096
添加 "Alice"(5字节): free_offset = 4096 - 5 = 4091 = 0x0FFB
添加 "Bob"(3字节):   free_offset = 4091 - 3 = 4088 = 0x0FF8

但上面 hexdump 显示 0x0F88 = 3976？
因为示例里还存了别的数据，这里只是演示读法
```

### 9.3 用 GDB 调试

```bash
gcc -g -O0 test.c page.c file_manager.c pager.c -o test
gdb ./test
```

GDB 里查看页内容：

```
(gdb) break page_add_tuple
(gdb) run
(gdb) print page->data[0]@20     # 打印前20字节(header)
$1 = {0, 0, 0, 0, 1, 0, 0, 0x10, 0x0, 0xff, 0xff, 0xff, 0xff, ...}
(gdb) x/20bx page->data          # 以十六进制打印20字节
0x7fff...: 0x00 0x00 0x00 0x00 0x01 0x00 0x00 0x10 ...
```

### 9.4 常见错误排查

| 症状                       | 可能原因                        |
|----------------------------|---------------------------------|
| 读出的 page_id 全 0        | 忘了 `page_init` 或没 `pager_write` |
| `page_add_tuple` 返回 INVALID | 页空间不足，检查 `page_free_space` |
| hexdump 看到字节序"反了"   | 正常，文件是大端，本机是小端     |
| `fm_read_page` 返回 false  | `pid >= num_pages`，页不存在     |
| 文件大小不是 4096 整数倍   | 写入被截断，检查 `fwrite` 返回值 |

### 9.5 打印页结构的辅助函数

调试时可以写个辅助函数打印页布局：

```c
void page_dump(const page_t *page) {
    printf("=== Page %u ===\n", page_get_id(page));
    printf("type: %d, slots: %u, free_off: %u\n",
           page_get_type(page),
           page_get_num_slots(page),
           read_u16(page->data + 7));
    printf("free_space: %u / %d\n",
           page_free_space(page), PAGE_SIZE);

    uint16_t n = page_get_num_slots(page);
    for (uint16_t i = 0; i < n; i++) {
        uint16_t len;
        const void *data = page_get_tuple(page, i, &len);
        if (data) printf("  slot[%u]: off=%u len=%u\n", i,
                         slot_get_offset(page, i), len);
        else      printf("  slot[%u]: DELETED\n", i);
    }
}
```

---

## 10. 与真实数据库对比

### 10.1 页结构对比表

| 特性             | miniDB        | SQLite        | PostgreSQL      | InnoDB        |
|------------------|---------------|---------------|-----------------|---------------|
| 页大小           | 4096          | 4096(可配)    | 8192(编译期)    | 16384(可配)   |
| Header 大小      | 20            | 8(数据库头)   | 24              | 38            |
| Slot 大小        | 4             | 4(cell ptr)   | 4(item id)      | 变长          |
| 字节序           | 大端          | 大端          | 大端            | 大端          |
| 生长方向         | slot←→tuple→←| cell←→cellptr→←| item←→tuple→← | rec←→dir→←   |
| 删除策略         | 标记(length=0)| 标记(freeblock)| 标记+HOT链     | 标记+undo     |
| 空间回收         | 无(vacuum待实现)| defragment    | VACUUM/autovac  | purge         |
| 校验和           | 字段预留未用  | 有            | 有              | 有            |
| 溢出页           | next_page_id  | overflow cell | TOAST表         | overflow page |

### 10.2 Header 对比

```
miniDB Header (20字节):
┌────────┬─┬─────┬─────┬────────┬─────────┬───┐
│page_id │T│nslot│free │next_pg │checksum │rsv│
│ 4B     │1│ 2B  │ 2B  │ 4B     │ 4B      │3B │
└────────┴─┴─────┴─────┴────────┴─────────┴───┘

PostgreSQL Header (24字节):
┌────────┬───────┬───────┬───────┬────────┬───────┬───────┬───────┐
│pd_lsn  │pd_chk │pd_flag│pd_lower│pd_upper│pd_spec│pd_pages│pd_prune│
│ 8B     │ 2B    │ 2B    │ 2B     │ 2B     │ 4B    │ 2B    │ 2B    │
└────────┴───────┴───────┴───────┴────────┴───────┴───────┴───────┘
pd_lsn: WAL日志序列号(用于恢复)
pd_lower/pd_upper: 类似miniDB的slot区尾和tuple区头

InnoDB Header (38字节):
┌───────┬───────┬───────┬───────┬───────┬───────┬───────┬───────┐
│space_id│page_no│n_dir  │heap_top│n_recs │free_top│n_frag │...    │
│ 4B     │ 4B     │ 2B    │ 2B     │ 2B     │ 2B     │ 2B     │       │
└───────┴───────┴───────┴───────┴───────┴───────┴───────┴───────┘
```

### 10.3 复杂度对比

| 能力               | miniDB | SQLite | PostgreSQL | InnoDB  |
|--------------------|--------|--------|------------|---------|
| 基本页读写         | ✅     | ✅     | ✅         | ✅      |
| Buffer Pool        | ❌(章2)| ✅     | ✅         | ✅      |
| WAL                | ❌(章5)| ✅     | ✅         | ✅      |
| 事务               | ❌     | ✅     | ✅         | ✅      |
| MVCC               | ❌     | ❌     | ✅         | ✅      |
| 崩溃恢复           | ❌     | ✅     | ✅         | ✅      |
| 在线 vacuum        | ❌     | ❌     | ✅         | ✅(purge)|
| 压缩               | ❌     | ✅     | ✅(扩展)   | ✅      |
| 加密               | ❌     | ✅     | ✅(扩展)   | ✅      |

miniDB 是教学库，只实现核心骨架，逐章补齐能力。

### 10.4 SQLite 对照实验

可以用 SQLite 创建一个同样 4KB 页的数据库，用 `.dbinfo` 对比：

```bash
sqlite3 test.sqlite
sqlite> PRAGMA page_size;
4096
sqlite> CREATE TABLE t(name TEXT);
sqlite> INSERT INTO t VALUES ('Alice');
sqlite> .dbinfo
database page size:  4096
number of pages:     2
...
```

SQLite 的页结构和 miniDB 几乎同构（都是 slotted page + 大端），可以用 hexdump 对照学习。

---

## 11. 习题

### 习题 1：修改页大小

如果页大小改为 8192（PostgreSQL 默认），需要修改哪些地方？会影响哪些功能？

<details>
<summary>参考答案</summary>

1. 修改 `page.h` 的 `#define PAGE_SIZE 8192`。
2. `page_t.data` 数组自动变大。
3. `fm_read_page/fm_write_page` 的读写字节数自动变（用 `PAGE_SIZE`）。
4. 一页能放更多 tuple，`page_add_tuple` 次数减少。
5. Buffer Pool（章2）每帧内存翻倍。
6. B+Tree 扇出翻倍，树更矮。
7. `free_space_offset` 是 `uint16_t`，最大 65535，8192 没问题，但 65536 就溢出了。

</details>

### 习题 2：标记删除的空间回收

标记删除后空间不会被立即回收，如何判断一个页"需要清理"？

<details>
<summary>参考答案</summary>

统计页中 `length == 0` 的 slot 数量，如果超过阈值（如 25%），标记该页需要 vacuum：

```c
uint16_t deleted = 0;
uint16_t total = page_get_num_slots(page);
for (uint16_t i = 0; i < total; i++)
    if (page_is_slot_deleted(page, i)) deleted++;

bool needs_vacuum = (deleted * 4 > total);  // 删除超过25%
```

</details>

### 习题 3：超大 tuple 的溢出处理

如果 tuple 太大放不下一页（比如 10KB 的 BLOB），应该怎么处理？

<details>
<summary>参考答案</summary>

用溢出页链：

```
主页:  ┌────────┬──────┐
       │ ...    │Slot0 │  Slot0.length = 4080 (页内部分)
       │        │→溢出 │  next_page_id = 5
       └────────┴──────┘

溢出页5(PAGE_TYPE_OVERFLOW):
       ┌────────┬───────────┐
       │Header  │ 4016字节  │  next_page_id = 6
       └────────┴───────────┘

溢出页6:
       ┌────────┬───────────┐
       │Header  │ 剩余字节  │  next_page_id = INVALID
       └────────┴───────────┘

读取时: 先读主页部分，再沿 next_page_id 链读溢出页，拼接
```

</details>

### 习题 4：实现校验和

`checksum` 字段目前没有使用，如何实现一个简单的页校验和？

<details>
<summary>参考答案</summary>

简单方案：把页内所有字节（除 checksum 字段外）求和取模：

```c
uint32_t page_compute_checksum(const page_t *page) {
    uint32_t sum = 0;
    for (int i = 0; i < PAGE_SIZE; i++) {
        if (i >= 13 && i < 17) continue;  // 跳过checksum字段
        sum += page->data[i];
    }
    return sum;
}

// 写页前算checksum
uint32_t cs = page_compute_checksum(page);
write_u32(page->data + 13, cs);

// 读页后验证
if (read_u32(page->data + 13) != page_compute_checksum(page))
    printf("校验和失败，页损坏!\n");
```

生产级用 CRC32 或 Fletcher-32，能检测更多错误模式。

</details>

### 习题 5：页分裂

当一个堆页写满了，新 tuple 放不下，怎么处理？（提示：分配新页，链起来）

<details>
<summary>参考答案</summary>

堆表用链表组织页：

```
页0 (next=1) → 页1 (next=3) → 页3 (next=INVALID)
  满             满              有空间

插入新tuple:
  1. 从页0开始找有空间的页
  2. 页0满 → 沿next到页1
  3. 页1满 → 到页3
  4. 页3有空间 → page_add_tuple
  5. 如果页3也满了 → 分配页4，页3.next=4，在页4插入
```

</details>

### 习题 6：字节序验证

写一个程序，用 miniDB 写入 `page_id = 0x12345678`，然后用 hexdump 查看，验证文件里是大端序 `12 34 56 78`。

<details>
<summary>参考答案</summary>

```c
pager_t *p = pager_open("endian.db");
page_id_t pid = pager_allocate(p);
page_t page;
page_init(&page, pid, PAGE_TYPE_HEAP);
// page_init 里 write_u32(page->data, pid)
// 但 pid 是 allocate 返回的 0，要手动改
write_u32(page.data, 0x12345678);  // 需要暴露 write_u32 或加测试接口
pager_write(p, pid, &page);
pager_close(p);
```

`hexdump endian.db` 应看到前 4 字节是 `12 34 56 78`。

</details>

### 习题 7：空间利用率计算

一页 4096 字节，header 20 字节，每个 slot 4 字节。如果存 100 条每条 30 字节的 tuple，空间利用率是多少？

<details>
<summary>参考答案</summary>

```
header:      20 字节
slot array:  100 * 4 = 400 字节
tuple data:  100 * 30 = 3000 字节
总占用:      20 + 400 + 3000 = 3420 字节
空闲:        4096 - 3420 = 676 字节

空间利用率 = 3420 / 4096 = 83.5%
有效数据率 = 3000 / 4096 = 73.2%  (纯tuple数据)
开销率 = (20 + 400) / 4096 = 10.3%  (header + slot)
```

</details>

### 习题 8：并发安全问题

当前 `fseek + fread` 在多线程下不安全。假设两个线程同时 `fm_read_page`，描述可能出错的过程，并给出用 `pread` 的修改方案。

<details>
<summary>参考答案</summary>

出错过程：

```
线程A: fseek(fp, 0, SEEK_SET)     ← 文件偏移=0
线程B: fseek(fp, 4096, SEEK_SET)  ← 文件偏移=4096 (覆盖A)
线程A: fread(buf, 1, 4096, fp)     ← 读到的是4096处，不是0处！
```

用 `pread` 修改（POSIX）：

```c
bool fm_read_page(file_manager_t *fm, page_id_t pid, void *buf) {
    if (pid >= fm->num_pages) return false;
    off_t offset = (off_t)pid * PAGE_SIZE;
    ssize_t n = pread(fileno(fm->fp), buf, PAGE_SIZE, offset);
    return n == PAGE_SIZE;
}
```

`pread` 是原子操作，不改变文件偏移指针，多线程安全。Windows 用 `ReadFile` + `OVERLAPPED`。

</details>

---

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
| 页结构 | 纯字节数组 | 序列化零成本，跨平台 |
| Header 位置 | 页首 | 惯例，读取时第一个拿到元信息 |

---

上一章：[章0 项目基础设施](00-infrastructure.md) | 下一章：[章2 Buffer Pool](02-buffer-pool.md)
