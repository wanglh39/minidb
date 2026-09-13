# miniDB — 从零造一个关系型数据库

> 用 C 语言从字节级开始造一个迷你 RDBMS，再深入 SQLite/PostgreSQL 实战。

## 为什么做这个项目？

数据库是后端开发的核心基础设施，但大多数教程只讲"怎么用 SQL"，
不讲**数据库内部到底怎么工作**。

本项目通过亲手造一个数据库，让你理解：

- 数据库为什么以"页"为单位读写？
- B+Tree 为什么比 B-Tree 更适合做索引？
- 事务的 ACID 到底怎么保证？MVCC 是什么？
- 拔掉电源后，数据库怎么恢复数据？
- SQL 语句是怎么被解析、优化、执行的？

## 两阶段路线

### 阶段1：从零造数据库

用 C 语言造一个支持 SQL、事务、崩溃恢复的迷你 RDBMS。

[:octicons-arrow-right-24: 进入阶段1](phase1/index.md)

### 阶段2：实际数据库如何使用

基于阶段1的源码级理解，深入 SQLite/PostgreSQL 实战。

[:octicons-arrow-right-24: 进入阶段2](phase2/index.md)

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