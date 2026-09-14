# miniDB — 从零造一个关系型数据库

> 一个教学性项目：用 C 语言从字节级开始造一个迷你 RDBMS，再深入 SQLite/PostgreSQL 实战。

## 为什么做这个项目？

数据库是后端开发的核心基础设施，但大多数教程只讲"怎么用 SQL"，不讲"数据库内部到底怎么工作"。
本项目通过**亲手造一个数据库**，让你理解：

- 数据库为什么以"页"为单位读写？
- B+Tree 为什么比 B-Tree 更适合做索引？
- 事务的 ACID 到底怎么保证？MVCC 是什么？
- 拔掉电源后，数据库怎么恢复数据？
- SQL 语句是怎么被解析、优化、执行的？

理解了这些，你在后端开发中就能：写出更高效的查询、正确选择索引、排查死锁、调优慢查询、做容量规划。

## 两阶段路线

### 阶段1：从零造数据库（C 语言）

用 C 语言从字节级开始，造出一个支持 SQL、事务、崩溃恢复的迷你 RDBMS。

| 章 | 主题 |
|---|---|
| 0 | 项目基础设施（CMake / Unity 测试 / CI） |
| 1 | 存储基础：从字节到页 |
| 2 | Buffer Pool：缓存管理 |
| 3 | B+Tree 索引 |
| 4 | 堆表存储 |
| 5 | WAL 与崩溃恢复 |
| 6 | 事务与并发控制（MVCC） |
| 7 | SQL 解析（手写递归下降） |
| 8 | 查询优化 |
| 9 | 执行引擎（Volcano 模型） |
| 10 | 网络协议与 CLI 客户端 |
| 11 | 集成与压测 |

### 阶段2：实际数据库如何使用

基于阶段1的源码级理解，深入 SQLite/PostgreSQL 实战。

| 章 | 主题 |
|---|---|
| 1 | SQLite 深入（与 miniDB 对照） |
| 2 | PostgreSQL 架构深入 |
| 3 | 索引策略实战 |
| 4 | 事务与隔离级别实战 |
| 5 | 连接池与后端集成（Python） |
| 6 | 查询性能调优 |
| 7 | 高可用与扩展 |
| 8 | 数据库运维 |

## 快速开始

```bash
# 构建阶段1
cmake -B build -S phase1
cmake --build build

# 运行测试
ctest --test-dir build --output-on-failure

# 启动数据库
./build/minidb
```

## 文档站

在线文档：**https://wanglh39.github.io/minidb/**

本地预览文档：

```bash
pip install mkdocs-material
mkdocs serve
```

## 项目结构

详见 [BLUEPRINT.md](BLUEPRINT.md)。

## License

MIT