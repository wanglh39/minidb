# 章11：集成测试与压测

> 各模块单独测试通过不代表整体可用。本章进行端到端集成测试和性能压测，验证 Parser → Optimizer → Executor 全链路正确性和吞吐量。

## 集成测试

### 测试策略

用 100 行的表 `big(id, val)` 进行全链路测试：

```
SQL 文本 → Parser → AST → Optimizer → Plan → Executor → ResultSet → 验证
```

### 测试覆盖

| 测试 | SQL | 验证点 |
|---|---|---|
| `test_integ_full_scan` | `SELECT * FROM big` | 全表 100 行 |
| `test_integ_index_lookup` | `WHERE id = 50` | 索引查找 1 行 |
| `test_integ_range_query` | `WHERE id >= 10 AND id < 20` | 范围查询 10 行 |
| `test_integ_project_filter` | `SELECT val WHERE id >= 90` | 投影 + 过滤 |
| `test_integ_no_results` | `WHERE id = 99999` | 空结果 |
| `test_integ_not_equal` | `WHERE id != 0` | 不等过滤 99 行 |
| `test_integ_first_half` | `WHERE id < 50` | 前半 50 行 |
| `test_integ_second_half` | `WHERE id >= 50` | 后半 50 行 |
| `test_integ_project_all` | `SELECT id FROM big` | 全表投影 |
| `test_integ_delete_parse` | `DELETE FROM big WHERE id = 5` | DELETE 解析 |
| `test_integ_create_parse` | `CREATE TABLE test (...)` | CREATE 解析 |

## 压测

### 方法

10,000 行表，重复执行查询，测量吞吐量。

### 结果

```
=== miniDB Benchmark ===
Table: 10000 rows

Parse:     10000 queries in 15.0 ms  (666,667 q/s)
Optimize:  10000 plans in 19.0 ms  (526,316 p/s)
SeqScan:   102400 rows in 8.0 ms  (12,800,000 rows/s)
Filter:    102400 rows in 19.0 ms  (5,389,474 rows/s)
E2E:       400 queries, 302400 rows in 108.0 ms  (3,704 q/s, 2,800,000 rows/s)
```

### 分析

| 指标 | 数值 | 说明 |
|---|---|---|
| **Parse** | 667K q/s | 手写递归下降，零分配热路径 |
| **Optimize** | 526K p/s | 启发式规则 + 代价估算 |
| **SeqScan** | 12.8M rows/s | 内存数组遍历，缓存友好 |
| **Filter** | 5.4M rows/s | 逐行求值，分支预测友好 |
| **E2E** | 3.7K q/s | 含解析+优化+执行+结果收集 |

### 瓶颈分析

```
E2E 耗时分解:
  Parse:     ~15%  (15/108)
  Optimize:  ~18%  (19/108)
  Execute:   ~67%  (74/108)
```

执行占大头，主要是结果集收集（`rs->rows[rs->num_rows++] = row` 涉及结构体拷贝）。

### 优化建议

| 优化 | 预期提升 | 难度 |
|---|---|---|
| 向量化执行 | 3-5x | 高 |
| 避免结果集拷贝 | 1.5x | 低 |
| 预编译 SQL | 省去 Parse | 中 |
| SIMD 谓词求值 | 2-3x | 高 |

## 全链路验证

```
miniDB> SELECT id FROM users WHERE age > 28;
AST: SELECT id FROM users WHERE age > 28
Plan:
  Project(id)  [rows=2 cost=1.6]
    Filter(age >)  [rows=2 cost=1.5]
      SeqScan(users)  [rows=5 cost=1.0]
Result:
id
---
2
3
5
(3 rows)
```

每个环节都可观察：AST、Plan、Result。这是教学数据库的核心价值——**透明**。

## 阶段1 总结

### 11 章实现内容

| 章 | 模块 | 代码量 | 测试数 |
|---|---|---|---|
| 1 | 存储基础 | ~600 行 | 15 |
| 2 | Buffer Pool | ~500 行 | 10 |
| 3 | B+Tree | ~400 行 | 8 |
| 4 | 堆表 | ~500 行 | 6 |
| 5 | WAL | ~300 行 | 5 |
| 6 | 事务/MVCC | ~400 行 | 6 |
| 7 | SQL 解析 | ~400 行 | 11 |
| 8 | 优化器 | ~300 行 | 10 |
| 9 | 执行引擎 | ~250 行 | 10 |
| 10 | CLI | ~200 行 | — |
| 11 | 集成/压测 | ~300 行 | 11 |
| **合计** | | **~4150 行** | **92** |

### 数据库架构

```
SQL 文本
    │
    ▼
┌─────────┐     ┌─────────┐     ┌─────────┐     ┌─────────┐
│  Lexer  │ ──▶ │ Parser  │ ──▶ │Optimizer│ ──▶ │Executor │
└─────────┘     └─────────┘     └─────────┘     └─────────┘
                                      │              │
                                      ▼              ▼
                                ┌─────────┐    ┌─────────┐
                                │ Catalog │    │ResultSet│
                                └─────────┘    └─────────┘
                                      │
                                      ▼
                ┌─────────────────────────────────────┐
                │          Storage Stack               │
                │  ┌───────┐  ┌───────┐  ┌────────┐  │
                │  │  B+Tree│  │  Heap  │  │  WAL   │  │
                │  └───────┘  └───────┘  └────────┘  │
                │  ┌───────┐  ┌───────┐  ┌────────┐  │
                │  │BufferPool│ │ Pager │  │  MVCC  │  │
                │  └───────┘  └───────┘  └────────┘  │
                │  ┌───────────────────────────────┐  │
                │  │         File Manager           │  │
                │  └───────────────────────────────┘  │
                └─────────────────────────────────────┘
```

### 与真实数据库的对比

| 特性 | miniDB | SQLite | PostgreSQL |
|---|---|---|---|
| 语言 | C | C | C |
| 存储 | Slotted Page | B-Tree | Heap + Index |
| Buffer Pool | LRU/Clock | mmap | LRU + ARC |
| WAL | 单文件 | WAL 文件 | WAL + Checkpoint |
| 并发 | 2PL + MVCC | WAL | MVCC |
| SQL | 子集 | 完整 | 完整 |
| 优化器 | 启发式 | 基于代价 | 基于代价 |
| 执行 | Volcano | 字节码 | Volcano |
| 网络 | 无 | 嵌入式 | TCP |

## 文件清单

| 文件 | 职责 |
|---|---|
| `test_integration.c` | 11 个端到端集成测试 |
| `benchmark.c` | 性能压测 |

## 下一步

阶段1 完成！接下来进入**阶段2**：深入 SQLite 和 PostgreSQL 的实战，了解工业级数据库如何实现这些功能。