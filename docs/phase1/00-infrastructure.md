# 章0：项目基础设施

> 搭建可构建、可测试、可持续集成的项目骨架。

## 目标

在写任何数据库代码之前，先搭好工程基础设施：

- [x] CMake 构建系统
- [x] Unity 单元测试框架
- [x] 目录结构
- [ ] GitHub Actions CI
- [ ] 文档站自动部署

## 构建系统

项目使用 CMake 3.16+，C99 标准。

```bash
# 配置 + 构建
cmake -B build -S phase1
cmake --build build

# 运行测试
ctest --test-dir build --output-on-failure

# 运行数据库
./build/minidb
```

## 目录约定

```
phase1/
├── src/
│   ├── core/        # 页、Buffer Pool 等核心设施
│   ├── storage/     # B+Tree、堆表存储
│   ├── txn/         # WAL、事务、MVCC
│   ├── sql/         # 词法/语法分析、AST
│   ├── optimizer/   # 查询优化器
│   ├── executor/    # 执行引擎
│   ├── server/      # 网络协议、CLI
│   └── main.c
├── tests/           # 镜像 src 结构
├── benchmarks/
└── examples/
```

## 测试框架

使用 [Unity](https://github.com/ThrowTheSwitch/Unity)（极简 C 单元测试框架），
通过 CMake FetchContent 自动拉取。

测试文件镜像 `src/` 目录结构，例如 `src/core/pager.c` 对应 `tests/core/test_pager.c`。

## 代码规范

- C99，不使用编译器扩展
- `-Wall -Wextra -Wpedantic` 全开
- 头文件用 include guard
- 模块对外暴露 `.h`，内部细节放 `.c`

---

下一章：[章1 存储基础](01-storage.md)