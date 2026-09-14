# 章0：项目基础设施

> 搭建可构建、可测试、可持续集成的项目骨架。

本章是整个项目的"第零步"。在写任何一行数据库逻辑代码之前，我们先把工程基础设施搭好：构建系统、测试框架、目录结构、CI、文档站。这一章不涉及数据库原理，但它是后续所有章节的地基。地基没搭好，后面写再多代码也会被构建、测试、协作问题拖累。

---

## 为什么要造数据库？

在你打开这一章之前，大概率会有一个疑问：**市面上有 SQLite、PostgreSQL、MySQL，为什么还要自己造一个？** 这一节就来回答这个问题。

### 只学 SQL vs 从底层理解

大多数数据库教程只教你"怎么用"：写 `SELECT`、建索引、配连接池。这就像学开车只学了方向盘和油门，却不知道引擎里发生了什么。一旦车子抛锚，你只能干瞪眼。

下表对比两种学习路径的差异：

| 维度 | 只学 SQL（应用层） | 从底层造一遍（系统层） |
|------|--------------------|------------------------|
| 学习成本 | 低，几天能上手 | 高，需要 C 语言 + 系统编程基础 |
| 能解决的问题 | 增删改查、简单调优 | 慢查询根因、死锁、崩溃恢复、容量规划 |
| 遇到疑难时的状态 | 查文档、搜 Stack Overflow | 能直接读数据库源码定位问题 |
| 对后端架构的理解 | 停留在"数据库是个黑盒" | 理解 IO、缓存、并发、持久化的权衡 |
| 面试竞争力 | 会用的人太多 | 能讲清 B+Tree、MVCC、WAL 的人很少 |
| 职业天花板 | CRUD 工程师 | 能做基础设施、存储引擎、中间件 |

### 教学价值：把"黑盒"变成"白盒"

数据库是后端开发最核心的基础设施。但它的内部原理被层层封装：SQL 接口 → 优化器 → 执行器 → 存储引擎 → 文件系统 → 操作系统。只学 SQL 的人，永远只看到最外层。

本项目要做的，就是**从最内层（字节、页）开始，一层层往外造**，直到你能用 SQL 查询自己造的数据库。走完这一遭，你会真正理解这些"面试八股"背后的物理意义：

- 数据库为什么以"页"（通常是 4KB）为单位读写？—— 因为操作系统和磁盘都是按块 IO 的。
- B+Tree 为什么比 B-Tree 更适合做索引？—— 因为 B+Tree 的数据全在叶子层，范围扫描时不用回溯内部节点。
- 事务的 ACID 到底怎么保证？—— 用 WAL（预写日志）保证持久性，用 MVCC（多版本并发控制）保证隔离性。
- 拔掉电源后，数据库怎么恢复数据？—— 重放 WAL 中的 redo 日志。
- SQL 语句是怎么被解析、优化、执行的？—— 词法分析 → 语法分析 → 逻辑计划 → 优化 → 物理计划 → 执行。

### 为什么用 C 语言？

你可能会问：用 Python 或 Go 造不行吗？为什么要用 C？

| 语言 | 适合造数据库吗 | 原因 |
|------|----------------|------|
| Python | 不适合 | 太慢，且无法直接操作内存和文件 IO 的细节 |
| Go | 可以，但隔了一层 | 有 GC，内存布局不透明，看不到"字节级"细节 |
| Rust | 可以，但学习曲线陡 | 适合生产级存储引擎，但本项目是教学，C 更纯粹 |
| **C** | **最适合教学** | **直接操作指针、内存、文件，所见即所得；SQLite 也是 C 写的** |

SQLite 是世界上部署最广的数据库（每台手机里都有），它就是用 C 写的。用 C 造数据库，你能直接对照 SQLite 的源码学习，这是其他语言给不了的。

### 本项目的最终产出

走完阶段 1 的 11 章，你将得到一个：

- 支持 `CREATE / INSERT / SELECT / UPDATE / DELETE` 的迷你 RDBMS
- 用 B+Tree 做索引，支持点查和范围扫描
- 有 WAL 和崩溃恢复，拔电源后数据不丢
- 有 MVCC 事务，支持并发读写
- 有手写的 SQL 词法/语法分析器
- 有查询优化器和 Volcano 执行引擎
- 全程有单元测试覆盖，CI 自动验证

这不是玩具——它能让你在面试时讲清楚每一个"为什么"，而不是背八股文。

---

## 目标

在写任何数据库代码之前，先搭好工程基础设施：

- [x] CMake 构建系统
- [x] Unity 单元测试框架
- [x] 目录结构
- [ ] GitHub Actions CI
- [ ] 文档站自动部署

本章结束时，你应该能做到：

1. 在本地一键构建项目：`cmake -B build -S phase1 && cmake --build build`
2. 一键运行所有测试：`ctest --test-dir build --output-on-failure`
3. 看懂项目里每一个 `CMakeLists.txt` 的每一行
4. 看懂 `.github/workflows/ci.yml` 的每一行
5. 能在本地预览文档站：`mkdocs serve`
6. 理解为什么目录要这样划分

---

## C 语言开发环境搭建

在开始之前，你需要一台装好 C 语言工具链的电脑。这一节手把手教你搭环境，覆盖 Windows、macOS、Linux 三个平台。

### 你需要安装什么

下表列出了本项目需要的全部工具：

| 工具 | 作用 | 必需吗 |
|------|------|--------|
| GCC 或 Clang | C 编译器 | 必需 |
| CMake ≥ 3.16 | 构建系统 | 必需 |
| Git | 版本控制 | 必需 |
| VS Code 或 CLion | 代码编辑器 | 推荐 |
| Python 3 | 跑文档站 MkDocs | 文档才需要 |
| Make 或 Ninja | CMake 的后端生成器 | 通常随编译器自带 |

### 平台 1：Windows

Windows 上装 C 语言环境有两条路。推荐第一条（WSL），因为本项目用到了 POSIX 文件 API，WSL 最省事。

**路线 A：WSL（推荐）**

WSL（Windows Subsystem for Linux）让你在 Windows 里跑一个真 Linux。本项目大量使用 POSIX API（如 `pread`/`pwrite`），WSL 能直接跑，而纯 Windows 需要额外适配。

```bash
# 1. 以管理员身份打开 PowerShell，安装 WSL + Ubuntu
wsl --install -d Ubuntu

# 2. 重启电脑后，打开 "Ubuntu" 应用，设置用户名和密码

# 3. 在 WSL 里更新包列表并安装工具链
sudo apt update
sudo apt install -y build-essential cmake git

# 4. 验证安装
gcc --version      # 应输出 gcc 11.x 或更高
cmake --version    # 应输出 cmake 3.22.x 或更高
git --version

# 5. 把项目从 Windows 侧克隆到 WSL（在 WSL 终端里执行）
cd ~
git clone <你的仓库地址> database
cd database
```

**路线 B：纯 Windows（MSYS2）**

如果你不想用 WSL，可以用 MSYS2 提供的 MinGW 工具链：

```bash
# 1. 从 https://www.msys2.org/ 下载并安装 MSYS2

# 2. 打开 "MSYS2 UCRT64" 终端，更新系统
pacman -Syu

# 3. 安装工具链
pacman -S --needed mingw-w64-ucrt-x86_64-gcc mingw-w64-ucrt-x86_64-cmake git make

# 4. 验证
gcc --version
cmake --version
```

### 平台 2：macOS

macOS 自带 Clang，但 CMake 和 Git 可能需要手动装。推荐用 Homebrew：

```bash
# 1. 安装 Homebrew（如果还没装）
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# 2. 安装工具
brew install cmake git

# 3. 验证
clang --version    # macOS 默认编译器是 clang
cmake --version
git --version
```

### 平台 3：Linux（Ubuntu/Debian）

```bash
sudo apt update
sudo apt install -y build-essential cmake git
```

### 编辑器配置：VS Code

VS Code 是免费且轻量的选择。装好以下扩展：

| 扩展名 | 作用 |
|--------|------|
| C/C++（Microsoft） | 语法高亮、跳转定义、调试 |
| CMake Tools | CMake 项目集成 |
| CMake | CMakeLists.txt 语法高亮 |

装好后，在项目根目录创建 `.vscode/settings.json`（此处仅描述，不需要你手动建）：

```json
{
  "cmake.buildDirectory": "${workspaceFolder}/build",
  "cmake.configureSettings": {
    "CMAKE_BUILD_TYPE": "Debug"
  }
}
```

然后用 `Ctrl+Shift+P` → `CMake: Configure` → `CMake: Build` 即可图形化构建。

### 编辑器配置：CLion

CLion 是 JetBrains 出品的 C/C++ IDE，对 CMake 项目有原生支持。打开项目根目录即可，它会自动识别 `CMakeLists.txt`。CLion 是付费软件，但学生可免费申请。

### 验证环境：编译一个 Hello World

在确认环境搭好之前，别急着碰本项目。先编译一个最小程序：

```bash
# 创建 hello.c
cat > hello.c << 'EOF'
#include <stdio.h>
int main(void) {
    printf("Hello, DB!\n");
    return 0;
}
EOF

# 编译并运行
gcc hello.c -o hello
./hello
# 应输出: Hello, DB!
```

如果能看到 `Hello, DB!`，说明你的 C 工具链没问题，可以继续了。

### 验证环境：构建本项目

```bash
# 在项目根目录执行
cmake -B build -S phase1
cmake --build build

# 运行测试
ctest --test-dir build --output-on-failure

# 运行数据库
./build/minidb
```

如果以上命令全部成功，恭喜你，环境搭好了。如果报错，跳到本章末尾的"常见问题"一节。

---

## CMake 详解

本项目用 CMake 作为构建系统。如果你以前只写过单文件 C 程序（`gcc main.c -o main`），这一节会带你从零理解 CMake。

### 什么是 CMake？为什么不用 gcc 手动编译？

假设你的项目有 10 个 `.c` 文件，依赖 3 个外部库。手动编译是这样的：

```bash
gcc -c src/a.c -o build/a.o -Iinclude -Ilib1/include
gcc -c src/b.c -o build/b.o -Iinclude -Ilib2/include
# ... 重复 10 次
gcc build/a.o build/b.o ... -o myapp -Llib1/lib -lfoo -Llib2/lib -lbar
```

这还只是 Debug 版。如果要 Release 版，加 `-O2`；要加测试，再编译一堆测试文件；换到 Windows，路径分隔符都不一样……手动管理很快就会崩溃。

**CMake 就是一个"构建脚本生成器"**。你写一份 `CMakeLists.txt` 描述项目结构，CMake 根据它生成对应平台的构建文件（Linux 上生成 Makefile 或 Ninja 文件，Windows 上生成 Visual Studio 工程），然后你再用这些文件去编译。

```
  CMakeLists.txt ──→ cmake 命令 ──→ Makefile / .sln ──→ make / msbuild ──→ 可执行文件
                       (配置阶段)        (生成文件)          (构建阶段)
```

### CMake 的核心概念

| 概念 | 类比 | 说明 |
|------|------|------|
| `project()` | 项目名 | 声明项目名称和使用的语言 |
| `add_library()` | 打包一堆 .c 成一个 .a/.lib | 把相关源文件编译成静态/动态库 |
| `add_executable()` | 造一个可执行文件 | 把源文件编译成能跑的程序 |
| `target_include_directories()` | 告诉编译器头文件在哪 | 等价于 `gcc -I` |
| `target_link_libraries()` | 告诉链接器要链接哪个库 | 等价于 `gcc -l` |
| `add_subdirectory()` | 进入子目录继续读 CMakeLists | 让构建系统递归处理子目录 |
| `FetchContent` | 自动下载第三方库 | 从 GitHub 拉取依赖，不用手动管理 |

### 逐行解读根目录 CMakeLists.txt

本项目的根 `CMakeLists.txt` 只有 18 行，但每一行都有意义：

```cmake
cmake_minimum_required(VERSION 3.16)
project(minidb C)

set(CMAKE_C_STANDARD 99)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS_OFF)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Debug)
endif()

if(CMAKE_C_COMPILER_ID MATCHES "GNU|Clang")
    add_compile_options(-Wall -Wextra -Wpedantic)
elseif(CMAKE_C_COMPILER_ID MATCHES "MSVC")
    add_compile_options(/W4)
endif()

add_subdirectory(phase1)
```

逐行解释：

| 行 | 代码 | 含义 |
|----|------|------|
| 1 | `cmake_minimum_required(VERSION 3.16)` | 要求 CMake 版本至少 3.16（FetchContent_MakeAvailable 需要这个版本） |
| 2 | `project(minidb C)` | 项目名叫 minidb，用 C 语言 |
| 4 | `set(CMAKE_C_STANDARD 99)` | 使用 C99 标准 |
| 5 | `set(CMAKE_C_STANDARD_REQUIRED ON)` | 强制要求 C99，编译器不支持就报错而非降级 |
| 6 | `set(CMAKE_C_EXTENSIONS OFF)` | 关闭编译器扩展（如 GCC 的 `__attribute__`），保证可移植 |
| 8-10 | `if(NOT CMAKE_BUILD_TYPE)...` | 如果用户没指定构建类型，默认用 Debug |
| 12-16 | `if(CMAKE_C_COMPILER_ID...)` | 如果是 GCC/Clang，开 `-Wall -Wextra -Wpedantic`；如果是 MSVC，开 `/W4` |
| 18 | `add_subdirectory(phase1)` | 进入 `phase1/` 目录，继续读那里的 CMakeLists.txt |

### 逐行解读 phase1/CMakeLists.txt

这个文件做了三件事：拉取 Unity 测试框架、定义核心库、进入子目录。

```cmake
enable_testing()

include(FetchContent)
FetchContent_Declare(
    unity
    GIT_REPOSITORY https://github.com/ThrowTheSwitch/Unity.git
    GIT_TAG v2.5.2
    GIT_SHALLOW TRUE
)
FetchContent_MakeAvailable(unity)
```

| 行 | 含义 |
|----|------|
| `enable_testing()` | 启用 `ctest` 命令，这样构建后可以用 `ctest` 跑测试 |
| `include(FetchContent)` | 引入 FetchContent 模块，用于自动下载第三方库 |
| `FetchContent_Declare(unity ...)` | 声明一个叫 `unity` 的依赖，从 GitHub 拉取，锁定 v2.5.2 版本 |
| `GIT_SHALLOW TRUE` | 只拉取最新一次提交（`--depth=1`），加快下载速度 |
| `FetchContent_MakeAvailable(unity)` | 真正下载并让 unity 的 CMakeLists 参与构建 |

接下来是核心库定义：

```cmake
set(MINIDB_INCLUDE_DIRS
    ${CMAKE_CURRENT_SOURCE_DIR}/src
    ${CMAKE_CURRENT_SOURCE_DIR}/src/core
    ${CMAKE_CURRENT_SOURCE_DIR}/src/storage
    # ... 其他目录
)

add_library(minidb_core
    src/core/page.c
    src/core/file_manager.c
    src/core/pager.c
    # ... 其他源文件
)
target_include_directories(minidb_core PUBLIC ${MINIDB_INCLUDE_DIRS})

add_subdirectory(src)
add_subdirectory(tests)
```

| 代码 | 含义 |
|------|------|
| `set(MINIDB_INCLUDE_DIRS ...)` | 把所有模块的头文件目录收集到一个变量里 |
| `${CMAKE_CURRENT_SOURCE_DIR}` | 当前 CMakeLists.txt 所在目录 |
| `add_library(minidb_core ...)` | 把所有核心源文件编译成一个静态库 `libminidb_core.a` |
| `target_include_directories(... PUBLIC ...)` | 设置头文件搜索路径；`PUBLIC` 表示这个路径会传递给依赖此库的目标 |
| `add_subdirectory(src)` | 进入 `src/`，那里有 `add_executable(minidb main.c)` |
| `add_subdirectory(tests)` | 进入 `tests/`，那里定义了所有测试可执行文件 |

### PUBLIC vs PRIVATE：一个容易混淆的点

```cmake
target_include_directories(minidb_core PUBLIC ${MINIDB_INCLUDE_DIRS})
target_link_libraries(test_page PRIVATE unity minidb_core)
```

- `PUBLIC`：既给自己用，也给依赖我的目标用。`minidb_core` 的头文件目录是 `PUBLIC`，因为谁链接 `minidb_core`，谁就需要能找到它的头文件。
- `PRIVATE`：只给自己用，不传递。`test_page` 链接 `unity` 和 `minidb_core` 是 `PRIVATE`，因为 `test_page` 是最终可执行文件，没人会再依赖它。

### 常用 CMake 命令速查

| 命令 | 作用 |
|------|------|
| `cmake -B build -S phase1` | 配置项目，构建目录设在 `build/`，源码在 `phase1/` |
| `cmake --build build` | 编译 |
| `cmake --build build --parallel` | 多核并行编译（更快） |
| `cmake --build build --target clean` | 清理构建产物 |
| `ctest --test-dir build --output-on-failure` | 运行测试，失败的打印输出 |
| `cmake -B build -S phase1 -DCMAKE_BUILD_TYPE=Release` | 用 Release 模式（开 `-O2` 优化） |

---

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

### 构建类型

CMake 内置了四种构建类型，通过 `CMAKE_BUILD_TYPE` 切换：

| 类型 | 编译选项 | 用途 |
|------|----------|------|
| `Debug` | `-O0 -g` | 开发调试，带调试符号，不优化 |
| `Release` | `-O3 -DNDEBUG` | 发布，全优化，关断言 |
| `RelWithDebInfo` | `-O2 -g -DNDEBUG` | 发布但保留调试符号 |
| `MinSizeRel` | `-Os -DNDEBUG` | 最小体积 |

本项目默认 Debug。要切 Release：

```bash
cmake -B build -S phase1 -DCMAKE_BUILD_TYPE=Release
cmake --build build
```

---

## 目录结构设计

好的目录结构让代码"各司其职"。这一节解释本项目为什么这样分目录。

### 完整目录树

```
database/
├── CMakeLists.txt          # 根构建文件
├── BLUEPRINT.md            # 项目蓝图
├── README.md
├── docs/                   # 文档站源码
│   ├── mkdocs.yml          # MkDocs 配置
│   ├── index.md
│   ├── phase1/             # 阶段1文档
│   └── phase2/             # 阶段2文档
├── .github/workflows/      # CI/CD 配置
│   ├── ci.yml              # 构建+测试
│   └── docs.yml            # 文档部署
├── phase1/                 # 阶段1：从零造数据库
│   ├── CMakeLists.txt
│   ├── src/
│   │   ├── CMakeLists.txt
│   │   ├── main.c           # 程序入口
│   │   ├── core/            # 页、Buffer Pool 等核心设施
│   │   ├── storage/         # B+Tree、堆表存储
│   │   ├── txn/             # WAL、事务、MVCC
│   │   ├── sql/             # 词法/语法分析、AST
│   │   ├── optimizer/       # 查询优化器
│   │   ├── executor/        # 执行引擎
│   │   └── server/          # 网络协议、CLI
│   ├── tests/               # 镜像 src 结构
│   │   ├── CMakeLists.txt
│   │   ├── core/
│   │   ├── storage/
│   │   ├── txn/
│   │   └── ...
│   ├── benchmarks/
│   └── examples/
└── phase2/                 # 阶段2：实战
```

### 每个目录的职责

| 目录 | 职责 | 对应真实数据库的什么 |
|------|------|----------------------|
| `src/core/` | 最底层：页、文件管理、Buffer Pool | SQLite 的 pager、PostgreSQL 的 buffer manager |
| `src/storage/` | 存储结构：B+Tree、堆表、Tuple | SQLite 的 btree.c、PostgreSQL 的 heapam.c |
| `src/txn/` | 事务子系统：WAL、锁、MVCC | PostgreSQL 的 xlog.c、lockmgr |
| `src/sql/` | SQL 前端：词法、语法、AST | SQLite 的 tokenizer/parser |
| `src/optimizer/` | 查询优化 | PostgreSQL 的 planner |
| `src/executor/` | 执行引擎：各算子 | PostgreSQL 的 executor |
| `src/server/` | 网络层和 CLI | PostgreSQL 的 backend/ |
| `tests/` | 单元测试，镜像 src 结构 | 所有正经项目都有的测试目录 |

### 为什么按"子系统"分目录，而不是按"文件类型"分？

有些项目按文件类型分目录（所有 `.h` 放 `include/`，所有 `.c` 放 `src/`）。这在小型项目里 OK，但在数据库这种复杂项目里，按子系统分更好：

```
按文件类型分（不推荐）：        按子系统分（本项目采用）：
include/                       src/
  pager.h                        core/pager.h + pager.c
  btree.h                        storage/btree.h + btree.c
  wal.h                          txn/wal.h + wal.c
src/
  pager.c
  btree.c
  wal.c
```

按子系统分的好处：

1. **高内聚**：`btree.h` 和 `btree.c` 放一起，改 B+Tree 只用看一个目录。
2. **低耦合**：子系统之间的依赖通过明确的头文件暴露，不会偷偷互相依赖。
3. **可独立测试**：每个子系统的测试也放一起（`tests/storage/test_btree.c`），出问题好定位。
4. **对照真实数据库**：PostgreSQL 的源码也是按子系统分目录的（`src/backend/storage/`、`src/backend/optimizer/` 等）。

### 测试目录为什么镜像 src？

```
src/core/pager.c       ──→  tests/core/test_pager.c
src/storage/btree.c    ──→  tests/storage/test_btree.c
src/txn/wal.c          ──→  tests/txn/test_wal.c
```

镜像结构让你一眼看出"哪个源文件有没有测试"。如果 `src/storage/heap.c` 存在但 `tests/storage/test_heap.c` 不存在，就知道 heap 还没测试覆盖。

---

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

### 模块依赖关系

各子系统不是平级的，有明确的依赖方向（上层可用下层，反之不行）：

```
                层级          子系统
                ────          ──────
                 4            server/      （最上层：网络、CLI）
                 |              ↑
                 3            executor/    （执行引擎）
                 |              ↑
                 2            optimizer/   （查询优化）
                 |            sql/         （SQL 解析）
                 |              ↑
                 1            txn/         （事务）
                 |            storage/     （存储结构）
                 |              ↑
                 0            core/        （最底层：页、缓存）
```

这个分层是**单向依赖**：`server` 可以调 `executor`，但 `core` 绝不能调 `server`。这保证了底层模块不知道上层的存在，可以独立测试和复用。

---

## Unity 测试框架

### 什么是单元测试？

单元测试就是**给每个函数写一个小程序，自动验证它的输出是否符合预期**。举个例子，你写了个 `add` 函数：

```c
int add(int a, int b) { return a + b; }
```

怎么保证它是对的？肉眼看着是对的，但也许某天有人改成了 `a - b`，肉眼看不出来。单元测试就是写代码来验证：

```c
#include "unity.h"

void test_add(void) {
    TEST_ASSERT_EQUAL(3, add(1, 2));
    TEST_ASSERT_EQUAL(0, add(-1, 1));
    TEST_ASSERT_EQUAL(-5, add(-2, -3));
}

int main(void) {
    UNITY_BEGIN();
    RUN_TEST(test_add);
    return UNITY_END();
}
```

运行后如果 `add` 实现正确，输出 `OK`；如果有人改错了，输出具体哪一行失败、期望值和实际值。

### 为什么需要单元测试？

| 不写测试 | 写测试 |
|----------|--------|
| 改一处代码，不知道有没有改坏别的 | 改完跑一遍测试，立刻知道 |
| 调试靠 `printf`，删了又加 | 测试就是永久的"断言" |
| 不敢重构 | 重构后跑测试，绿了就放心 |
| Bug 复现一次就过去了 | Bug 变成回归测试，永不再犯 |

对于数据库这种复杂系统，**没有测试等于裸奔**。你改了 B+Tree 的分裂逻辑，怎么知道没改坏 Buffer Pool？跑一遍测试就知道。

### 为什么选 Unity？

C 语言的单元测试框架有很多，下表对比几个主流的：

| 框架 | 特点 | 适合本项目吗 |
|------|------|---------------|
| Google Test | C++ 的，C 用起来别扭 | 不适合 |
| CMocka | 纯 C，但 API 稍繁琐 | 可以，但略重 |
| **Unity** | **极简，一个头文件搞定，API 直观** | **最适合教学** |
| Check | 需要额外库，fork 子进程跑测试 | 太重 |

Unity 的优点：

1. **极简**：核心就是一个 `unity.h` 头文件 + 一个 `unity.c` 源文件。
2. **API 直观**：`TEST_ASSERT_EQUAL(期望, 实际)`，一看就懂。
3. **CMake 集成简单**：用 `FetchContent` 一拉就能用。
4. **被广泛使用**：很多嵌入式项目都用它，文档齐全。

### Unity 的常用断言宏

| 宏 | 用途 | 示例 |
|----|------|------|
| `TEST_ASSERT_EQUAL(exp, act)` | 整数相等 | `TEST_ASSERT_EQUAL(3, add(1,2))` |
| `TEST_ASSERT_TRUE(cond)` | 条件为真 | `TEST_ASSERT_TRUE(ptr != NULL)` |
| `TEST_ASSERT_FALSE(cond)` | 条件为假 | `TEST_ASSERT_FALSE(is_empty(list))` |
| `TEST_ASSERT_NULL(ptr)` | 指针为 NULL | `TEST_ASSERT_NULL(failed_alloc())` |
| `TEST_ASSERT_NOT_NULL(ptr)` | 指针非 NULL | `TEST_ASSERT_NOT_NULL(malloc(10))` |
| `TEST_ASSERT_EQUAL_STRING(exp, act)` | 字符串相等 | `TEST_ASSERT_EQUAL_STRING("ok", status())` |
| `TEST_ASSERT_EQUAL_MEMORY(exp, act, n)` | 内存块相等 | 比较二进制数据 |
| `TEST_ASSERT_GREATER_THAN(threshold, act)` | 大于 | `TEST_ASSERT_GREATER_THAN(0, size)` |
| `TEST_ASSERT_LESS_THAN(threshold, act)` | 小于 | `TEST_ASSERT_LESS_THAN(100, usage)` |
| `TEST_ASSERT_FLOAT_WITHIN(delta, exp, act)` | 浮点近似 | `TEST_ASSERT_FLOAT_WITHIN(0.01, 3.14, pi)` |

### 一个完整的测试示例

以本项目的 `tests/core/test_page.c` 为例，它的结构是这样的：

```c
#include "unity.h"
#include "page.h"

void setUp(void) {}
void tearDown(void) {}

void test_page_init_sets_zeroed_bytes(void) {
    Page p;
    page_init(&p, 4096);
    TEST_ASSERT_EQUAL(4096, p.size);
    TEST_ASSERT_NOT_NULL(p.data);
}

void test_page_write_and_read(void) {
    Page p;
    page_init(&p, 4096);
    page_write(&p, 0, "hello", 5);
    char buf[6] = {0};
    page_read(&p, 0, buf, 5);
    TEST_ASSERT_EQUAL_STRING("hello", buf);
}

int main(void) {
    UNITY_BEGIN();
    RUN_TEST(test_page_init_sets_zeroed_bytes);
    RUN_TEST(test_page_write_and_read);
    return UNITY_END();
}
```

关键点：

- `setUp` / `tearDown`：每个测试前/后自动调用的钩子，用于初始化和清理。
- 每个测试函数以 `test_` 开头（约定，不是强制）。
- `RUN_TEST(函数名)` 注册一个测试。
- `UNITY_BEGIN()` 和 `UNITY_END()` 是测试的起止标记。

### 如何运行测试

```bash
# 1. 配置并构建（会同时编译测试）
cmake -B build -S phase1
cmake --build build

# 2. 运行所有测试
ctest --test-dir build --output-on-failure

# 3. 运行单个测试套件
ctest --test-dir build -R page --output-on-failure

# 4. 直接运行测试可执行文件（看更详细的输出）
./build/test_page
```

`ctest` 的常用参数：

| 参数 | 作用 |
|------|------|
| `--test-dir build` | 测试在 build 目录里 |
| `--output-on-failure` | 失败时打印测试的 stdout |
| `-R <正则>` | 只跑名字匹配正则的测试 |
| `-E <正则>` | 排除名字匹配正则的测试 |
| `-j N` | N 个测试并行跑 |

### 测试如何链接到被测代码

看 `tests/CMakeLists.txt` 里的一行：

```cmake
add_executable(test_page core/test_page.c)
target_link_libraries(test_page PRIVATE unity minidb_core)
add_test(NAME page COMMAND test_page)
```

这三行做了三件事：

1. `add_executable`：把 `test_page.c` 编译成可执行文件 `test_page`。
2. `target_link_libraries`：链接 `unity`（测试框架）和 `minidb_core`（被测代码）。
3. `add_test`：把 `test_page` 注册到 `ctest`，名字叫 `page`。

这样 `ctest` 就知道有哪些测试、怎么跑它们。

---

## 测试框架

使用 [Unity](https://github.com/ThrowTheSwitch/Unity)（极简 C 单元测试框架），
通过 CMake FetchContent 自动拉取。

测试文件镜像 `src/` 目录结构，例如 `src/core/pager.c` 对应 `tests/core/test_pager.c`。

### 本项目已有的测试套件

下表列出本项目当前的所有测试套件（对应 `tests/CMakeLists.txt` 中的 `add_test`）：

| 测试名 | 测试文件 | 被测模块 |
|--------|----------|----------|
| `sanity` | `test_sanity.c` | 构建系统本身能否跑通 |
| `page` | `core/test_page.c` | 页的读写 |
| `file_manager` | `core/test_file_manager.c` | 文件管理器 |
| `pager` | `core/test_pager.c` | 分页器 |
| `replacer` | `core/test_replacer.c` | 缓存替换策略（LRU/Clock） |
| `buffer_pool` | `core/test_buffer_pool.c` | Buffer Pool |
| `btree` | `storage/test_btree.c` | B+Tree |
| `heap` | `storage/test_heap.c` | 堆表 |
| `wal` | `txn/test_wal.c` | 预写日志 |
| `txn` | `txn/test_txn.c` | 事务 |
| `parser` | `sql/test_parser.c` | SQL 解析器 |
| `optimizer` | `optimizer/test_optimizer.c` | 查询优化器 |
| `executor` | `executor/test_executor.c` | 执行引擎 |
| `integration` | `executor/test_integration.c` | 端到端集成测试 |

---

## GitHub Actions CI

### 什么是 CI/CD？

CI（Continuous Integration，持续集成）的意思是：**每次你提交代码，服务器自动帮你构建 + 跑测试**。如果构建或测试失败，立刻通知你。

CD（Continuous Deployment/Delivery，持续部署）更进一步：测试通过后自动部署到生产环境或文档站。

```
你 git push ──→ GitHub 收到推送 ──→ 启动 CI 服务器（一台干净的 Ubuntu）
                                      ├── 拉取你的代码
                                      ├── 安装编译器
                                      ├── cmake 构建
                                      ├── ctest 跑测试
                                      └── 全绿？→ 合并 / 部署
                                          失败？→ 通知你修
```

### 为什么 CI 很重要？

| 没有 CI | 有 CI |
|---------|-------|
| "我电脑上能跑" —— 但别人电脑上不行 | 服务器是干净环境，能跑才算数 |
| 合并到 main 才发现编译错误 | push 时就发现，不用等合并 |
| 测试靠自觉，有人忘了跑 | 服务器强制跑，跑不过不让合 |
| 只在一种系统上测过 | CI 可以同时在 Ubuntu + Windows + macOS 上测 |

### workflow 文件逐行解读

本项目的 CI 配置在 `.github/workflows/ci.yml`。GitHub Actions 的配置文件叫 "workflow"，用 YAML 格式。

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  build-and-test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        compiler: [gcc, clang]

    steps:
      - uses: actions/checkout@v4

      - name: Set up compiler
        run: |
          if [ "${{ matrix.compiler }}" = "clang" ]; then
            echo "CC=clang" >> $GITHUB_ENV
            echo "CXX=clang++" >> $GITHUB_ENV
          else
            echo "CC=gcc" >> $GITHUB_ENV
            echo "CXX=g++" >> $GITHUB_ENV
          fi

      - name: Configure
        run: cmake -B build -S phase1

      - name: Build
        run: cmake --build build --parallel

      - name: Test
        run: ctest --test-dir build --output-on-failure

  build-windows:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4

      - name: Configure
        run: cmake -B build -S phase1

      - name: Build
        run: cmake --build build --config Debug

      - name: Test
        run: ctest --test-dir build --output-on-failure -C Debug
```

逐段解释：

| 段 | 代码 | 含义 |
|----|------|------|
| `name: CI` | workflow 的名字 | 显示在 GitHub Actions 页面 |
| `on: push: branches: [main]` | 触发条件 | 有人 push 到 main 分支时触发 |
| `on: pull_request: branches: [main]` | 触发条件 | 有人向 main 发 PR 时触发 |
| `jobs: build-and-test` | 定义一个 job | 叫 `build-and-test` |
| `runs-on: ubuntu-latest` | 运行环境 | 在最新版 Ubuntu 上跑 |
| `strategy.matrix.compiler: [gcc, clang]` | 矩阵构建 | 同样的步骤跑两遍：一遍用 gcc，一遍用 clang |
| `uses: actions/checkout@v4` | 第一步 | 拉取你的代码到 CI 服务器 |
| `Set up compiler` | 第二步 | 根据 matrix 里的 compiler 值设置环境变量 |
| `Configure` | 第三步 | `cmake -B build -S phase1` 配置项目 |
| `Build` | 第四步 | `cmake --build build --parallel` 并行编译 |
| `Test` | 第五步 | `ctest` 跑所有测试 |
| `build-windows` | 第二个 job | 在 Windows 上也跑一遍，确保跨平台 |

### 矩阵构建是什么意思？

`matrix.compiler: [gcc, clang]` 会让 `build-and-test` 这个 job 跑 **两次**：一次 `compiler=gcc`，一次 `compiler=clang`。这样能确保你的代码在两种编译器下都能编译通过——GCC 和 Clang 的警告和行为有时有差异。

你可以把矩阵想象成笛卡尔积：

```
compiler × OS = 组合
gcc   × Ubuntu = job 1
clang × Ubuntu = job 2
MSVC  × Windows = job 3（在 build-windows 里单独定义）
```

### CI 运行在哪里？

GitHub 提供"runner"——一台临时分配的虚拟机。CI 跑完后虚拟机销毁，下次跑又分配一台全新的。这保证了环境干净，不会因为上次残留的文件影响这次构建。

| runner | 环境 |
|--------|------|
| `ubuntu-latest` | Ubuntu 22.04，预装 gcc/clang/cmake/git |
| `windows-latest` | Windows Server 2022，预装 MSVC |
| `macos-latest` | macOS 14，预装 clang |

### 如何查看 CI 运行结果？

1. 在 GitHub 仓库页面点 "Actions" 标签页。
2. 每次 push/PR 都会有一条记录。
3. 绿色对勾 = 全部通过；红色叉 = 有失败。
4. 点进去能看到每一步的日志。

### 如何在本地模拟 CI？

在 push 之前，你可以在本地先跑一遍 CI 会跑的命令：

```bash
cmake -B build -S phase1
cmake --build build --parallel
ctest --test-dir build --output-on-failure
```

如果本地能过，CI 大概率也能过（除非有平台差异）。

---

## MkDocs 文档站

### 什么是文档站？

文档站就是把 Markdown 文档渲染成一个漂亮的网站。你现在读的这个文档，最终会被 MkDocs 渲染成 HTML，部署到 GitHub Pages，任何人都能通过网址访问。

```
Markdown 源码 (.md) ──→ MkDocs 构建 ──→ HTML 网站 ──→ GitHub Pages ──→ 浏览器访问
```

### 为什么不用 README 就好？

| 只用 README | 用文档站 |
|-------------|----------|
| 一个文件越写越长，没法导航 | 多页面，有侧边栏导航 |
| 没有搜索 | 全文搜索 |
| 没有代码高亮、复制按钮 | 有 |
| 手机上看 Markdown 源码很丑 | 自适应网页 |

### MkDocs Material 是什么？

MkDocs 是一个 Python 写的静态站点生成器。MkDocs Material 是它的一个主题（皮肤），长得很现代，功能丰富：

- 侧边栏导航
- 亮/暗色切换
- 全文搜索
- 代码块带复制按钮和行号
- 表格、警告框、标签页等丰富排版

### 配置文件解读

本项目的文档配置在 `docs/mkdocs.yml`。挑几个关键配置解释：

```yaml
site_name: miniDB — 从零造一个关系型数据库

theme:
  name: material
  language: zh
  features:
    - navigation.tabs       # 顶部标签页导航
    - navigation.expand     # 侧边栏默认展开
    - search.suggest        # 搜索建议
    - content.code.copy     # 代码块复制按钮
  palette:
    - scheme: default       # 亮色主题
    - scheme: slate         # 暗色主题
  font:
    text: Noto Sans SC      # 正文中文字体
    code: JetBrains Mono    # 代码等宽字体

markdown_extensions:
  - admonition              # !!! note/warning 提示框
  - pymdownx.superfences    # 代码块增强
  - pymdownx.tabbed         # 标签页
  - pymdownx.tasklist       # 复选框（- [x]）

nav:
  - 首页: index.md
  - 阶段1 - 从零造数据库:
      - 章0 项目基础设施: phase1/00-infrastructure.md
      - 章1 存储基础: phase1/01-storage.md
      # ...
```

| 配置 | 含义 |
|------|------|
| `site_name` | 网站标题 |
| `theme.name: material` | 用 Material 主题 |
| `theme.language: zh` | 界面语言中文 |
| `theme.features` | 开启的功能特性 |
| `theme.palette` | 亮/暗色主题切换 |
| `markdown_extensions` | Markdown 扩展语法 |
| `nav` | 导航栏结构（手动指定页面顺序） |

### 如何本地预览文档站

```bash
# 1. 进入 docs 目录
cd docs

# 2. 安装依赖（只需第一次）
pip install mkdocs-material pymdown-extensions

# 3. 启动本地预览服务器
mkdocs serve

# 4. 浏览器打开 http://127.0.0.1:8000
```

`mkdocs serve` 会启动一个本地 HTTP 服务器，并且**监听文件变化**——你改了任何 `.md` 文件，浏览器刷新一下就能看到效果，不用重新构建。

### 文档如何自动部署

本项目的 `.github/workflows/docs.yml` 配置了文档自动部署：

```yaml
name: Docs

on:
  push:
    branches: [main]
  workflow_dispatch:

permissions:
  contents: write

jobs:
  deploy-docs:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install MkDocs Material
        run: pip install mkdocs-material pymdown-extensions

      - name: Build site
        run: mkdocs build --strict
        working-directory: docs

      - name: Deploy to GitHub Pages
        run: mkdocs gh-deploy --force
        working-directory: docs
```

流程是：你 push 到 main → CI 自动装 Python + MkDocs → 构建 HTML → `mkdocs gh-deploy` 把 HTML 推到 `gh-pages` 分支 → GitHub Pages 自动发布。

| 步骤 | 作用 |
|------|------|
| `setup-python` | 安装 Python 3.12 |
| `Install MkDocs Material` | 安装文档工具 |
| `mkdocs build --strict` | 构建网站，`--strict` 表示有警告就失败 |
| `mkdocs gh-deploy --force` | 把构建结果推到 `gh-pages` 分支 |

---

## Git 工作流

### 为什么用 Git？

Git 是版本控制系统——它记录你代码的每一次改动，让你能：

- 回退到任意历史版本
- 多人协作不互相覆盖
- 开新功能时不影响主干
- 追溯某行代码是谁、什么时候、为什么改的

### 分支策略

本项目采用简单的功能分支工作流：

```
main（主干，永远可编译可测试）
  │
  ├── feature/ch0-cmake        ← 章0 的开发分支
  ├── feature/ch1-pager        ← 章1 的开发分支
  ├── feature/ch2-buffer-pool  ← 章2 的开发分支
  └── ...
```

| 分支 | 用途 | 谁在上面提交 |
|------|------|--------------|
| `main` | 主干，始终可编译、测试全绿 | 只通过 PR 合并，不直接提交 |
| `feature/chN-xxx` | 开发某一章的功能 | 你自己 |
| `fix/xxx` | 修 Bug | 你自己 |

### 为什么每章独立分支？

1. **不互相干扰**：你在写章3 的 B+Tree，同时章2 的 Buffer Pool 还在改，分两个分支就不会冲突。
2. **可回退**：如果某章走错了方向，直接丢掉那个分支，main 不受影响。
3. **可审查**：每章完成后发 PR，自己审查一遍再合并到 main。
4. **教学清晰**：每个分支对应一个学习单元，`git log` 能看到这一章的演进过程。

### 提交规范

本项目遵循"约定式提交"（Conventional Commits）：

```
<类型>: <简短描述>

<可选的详细说明>
```

常用类型：

| 类型 | 含义 | 示例 |
|------|------|------|
| `feat` | 新功能 | `feat: 实现页的读写` |
| `fix` | 修 Bug | `fix: 修复 B+Tree 分裂时的越界` |
| `docs` | 文档 | `docs: 补充章0 的 CMake 说明` |
| `test` | 测试 | `test: 给 pager 补充边界测试` |
| `refactor` | 重构 | `refactor: 提取公共的页校验逻辑` |
| `chore` | 杂项 | `chore: 更新 .gitignore` |

好的提交信息 vs 坏的：

```
✗ "update"                    ← 更新了什么？？
✗ "fix bug"                   ← 哪个 bug？怎么修的？
✓ "fix: 修复 B+Tree 插入时叶子节点分裂的页号计算错误"
```

### 常用 Git 命令速查

| 命令 | 作用 |
|------|------|
| `git checkout -b feature/ch1-pager` | 创建并切换到新分支 |
| `git add -A && git commit -m "feat: ..."` | 提交所有改动 |
| `git push -u origin feature/ch1-pager` | 推送新分支到远程 |
| `git checkout main && git merge feature/ch1-pager` | 合并到 main |
| `git log --oneline --graph` | 查看提交历史 |
| `git diff` | 查看未提交的改动 |
| `git stash` | 暂存当前改动（切分支时不想提交） |

---

## 代码规范

- C99，不使用编译器扩展
- `-Wall -Wextra -Wpedantic` 全开
- 头文件用 include guard
- 模块对外暴露 `.h`，内部细节放 `.c`

### include guard

每个头文件都要有 include guard，防止被重复包含：

```c
#ifndef MINIDB_PAGER_H
#define MINIDB_PAGER_H

/* 函数声明 */

#endif /* MINIDB_PAGER_H */
```

### 模块对外接口

每个子系统对外只暴露一个 `.h`，内部细节用 `static` 藏起来：

```
core/
  pager.h    ← 对外接口：只有函数声明
  pager.c    ← 实现：static 函数是内部的，外部看不到
```

### 命名约定

| 类型 | 风格 | 示例 |
|------|------|------|
| 函数 | `snake_case`，模块名前缀 | `pager_read_page`、`btree_insert` |
| 类型 | `snake_case` + `_t` 后缀 | `page_t`、`btree_node_t` |
| 宏 | `UPPER_CASE` | `PAGE_SIZE`、`MAX_PAGES` |
| 文件 | `snake_case` | `buffer_pool.c`、`test_pager.c` |

---

## 常见问题

这一节收集了新手最容易遇到的问题。

### Q1：cmake 报错 "CMake 3.16 or higher is required"

你的 CMake 版本太低。升级：

```bash
# Ubuntu
sudo apt remove cmake
sudo pip install --upgrade cmake

# macOS
brew upgrade cmake

# Windows (MSYS2)
pacman -S mingw-w64-ucrt-x86_64-cmake
```

### Q2：编译报错 "unknown type name 'size_t'"

忘了 `#include <stddef.h>`（或 `<string.h>`）。`size_t` 不是内置类型，需要包含标准头文件。

### Q3：链接报错 "undefined reference to 'xxx'"

某个函数声明了但没实现，或者实现了但没加到 `CMakeLists.txt` 的 `add_library` 里。检查：

1. `.c` 文件里有没有实现这个函数？
2. `CMakeLists.txt` 的 `add_library(minidb_core ...)` 列表里有没有这个 `.c` 文件？

### Q4：测试跑不过，提示 "FetchContent failed"

Unity 是从 GitHub 拉取的，需要网络。如果你在国内网络不好，可以：

```bash
# 配置 git 代理
git config --global http.proxy http://127.0.0.1:7890

# 或者手动克隆 Unity 到本地，然后改 CMakeLists 用本地路径
```

### Q5：Windows 上构建失败，提示 "pread/pwrite not found"

`pread`/`pwrite` 是 POSIX 函数，Windows 原生不支持。解决方案：

1. 用 WSL（推荐，见"C 语言开发环境搭建"一节）
2. 或者在代码里用 `#ifdef _WIN32` 提供替代实现

### Q6：ctest 显示 "0 tests passed"

可能 `enable_testing()` 没被调用，或者测试没注册。检查 `phase1/CMakeLists.txt` 第一行是不是 `enable_testing()`，以及 `tests/CMakeLists.txt` 里有没有 `add_test(...)`。

### Q7：改了代码但测试结果没变

可能忘了重新编译。确保：

```bash
cmake --build build          # 重新编译
ctest --test-dir build --output-on-failure  # 再跑测试
```

或者一步到位：

```bash
cmake --build build && ctest --test-dir build --output-on-failure
```

### Q8：mkdocs serve 报错 "command not found"

没装 MkDocs。执行：

```bash
pip install mkdocs-material pymdown-extensions
```

### Q9：CI 在 Windows 上失败但本地（Linux）通过

平台差异。常见原因：

- 路径分隔符：Linux 用 `/`，Windows 用 `\`。C 代码里用 `/` 通常都能工作。
- POSIX API：`pread`/`pwrite` 在 Windows 上不存在。
- 行尾符：Git 可能把 LF 换成 CRLF，影响某些文本比较。

先在本地用 WSL 或 MSYS2 复现，再修。

### Q10：构建很慢

```bash
# 并行编译（用所有 CPU 核心）
cmake --build build --parallel

# 或指定核心数
cmake --build build -j 8
```

也可以换 Ninja 作为构建后端（比 Make 快很多）：

```bash
cmake -B build -S phase1 -G Ninja
cmake --build build
```

---

## 习题

完成以下练习题以巩固本章内容。

### 习题 1：验证你的环境

**任务**：在你的电脑上完成以下操作，并记录每步的输出：

1. 运行 `gcc --version` 或 `clang --version`
2. 运行 `cmake --version`
3. 运行 `git --version`
4. 克隆本项目并执行 `cmake -B build -S phase1 && cmake --build build`
5. 运行 `ctest --test-dir build --output-on-failure`，记录通过了多少个测试

**交付**：把以上 5 步的命令和输出贴到一个文本文件里。

### 习题 2：读懂 CMakeLists

**任务**：回答以下问题（参考本项目的 `phase1/CMakeLists.txt`）：

1. `minidb_core` 这个库包含哪些源文件？
2. 如果要新增一个源文件 `src/core/log.c`，需要改 CMakeLists.txt 的哪一行？怎么改？
3. `target_include_directories` 用的是 `PUBLIC` 还是 `PRIVATE`？为什么？
4. Unity 的版本号写在哪里？如果要升级到 v2.6.0，改哪里？

### 习题 3：写一个最小的 CMake 项目

**任务**：在项目外的地方创建一个新目录 `my_cmake_test/`，里面放：

```
my_cmake_test/
├── CMakeLists.txt
└── main.c
```

`main.c` 打印 "Hello, CMake!"。`CMakeLists.txt` 用 `cmake_minimum_required`、`project`、`add_executable` 三个命令。构建并运行。

### 习题 4：写一个 Unity 测试

**任务**：写一个函数 `int max_of_three(int a, int b, int c)` 返回三个数中的最大值。然后用 Unity 写至少 4 个测试用例覆盖：

- 三个不同的数
- 有相等的数
- 全是负数
- 包含 0

确保 `ctest` 能跑通你的测试。

### 习题 5：画依赖图

**任务**：用 ASCII 画图表示本项目的模块依赖关系，要求：

- 包含 `core`、`storage`、`txn`、`sql`、`optimizer`、`executor`、`server` 七个模块
- 用箭头表示"依赖"（A → B 表示 A 依赖 B）
- 标出哪一层是最底层、哪一层是最顶层

### 习题 6：解释 CI workflow

**任务**：看 `.github/workflows/ci.yml`，回答：

1. 这个 workflow 在什么情况下会触发？
2. `build-and-test` 这个 job 跑几次？为什么？
3. 如果你想加一个 macOS 的构建 job，怎么写？
4. `--parallel` 参数的作用是什么？为什么 CI 里要用它？

### 习题 7：修一个"坏"提交

**任务**：假设你写了这样一个提交：

```
git commit -m "update"
```

用 `git commit --amend -m "docs: 补充章0 习题"` 修改提交信息。然后解释为什么 "update" 是不好的提交信息。

### 习题 8：本地预览文档站

**任务**：

1. 安装 MkDocs Material
2. 在 `docs/` 下运行 `mkdocs serve`
3. 在浏览器打开 `http://127.0.0.1:8000`
4. 修改 `docs/phase1/00-infrastructure.md`，加一行文字，刷新浏览器看效果
5. 截图交付

---

## 小结

本章没有写任何数据库代码，但搭好了整个项目的骨架。你现在应该能：

- 用 CMake 构建和测试项目
- 看懂每一个 `CMakeLists.txt`
- 用 Unity 写单元测试
- 看懂 CI workflow
- 在本地预览文档站
- 理解目录结构和模块分层

这些基础设施看起来"没用"，但它们是后续 11 章的承重墙。从下一章开始，我们就要往这个骨架里填真正的数据库代码了。

| 概念 | 本章你学了什么 | 后续章节会用到 |
|------|----------------|----------------|
| CMake | 怎么构建项目 | 每章都会 `cmake --build` |
| Unity | 怎么写测试 | 每章都会写 `test_xxx.c` |
| CI | 怎么自动化验证 | 每次 push 都会触发 |
| 目录结构 | 代码放哪 | 每章往对应子目录加文件 |
| Git 分支 | 怎么管理开发 | 每章开一个 feature 分支 |

---

下一章：[章1 存储基础](01-storage.md)
