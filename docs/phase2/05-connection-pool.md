# 章5：连接池与后端集成

> 数据库连接是昂贵的资源。本章讲透连接池的原理（为什么需要、PgBouncer 怎么做）、Python 后端如何正确使用连接池、ORM 的 N+1 问题、事务边界管理、连接泄漏排查，以及异步数据库访问。所有 Python 示例用 SQLite 演示，零外部依赖可直接运行。

## 为什么需要连接池

### TCP 连接的开销

一次数据库连接不是"打个招呼"那么简单。以 PostgreSQL 为例，建立一条连接要经过：

```
客户端                          服务端
  │                               │
  │─── TCP 三次握手 ─────────────→│  1 个 RTT
  │─── 启动 TLS（可选）─────────→│  1-2 个 RTT
  │─── 认证握手（用户名/密码）──→│  1 个 RTT + 查 pg_shadow
  │─── 加载会话变量 ────────────→│  查 GUC 默认值
  │─── 设置 search_path ────────→│
  │─── 就绪（ReadyForQuery）────→│
  │                               │
  └─── 可以发 SQL 了 ────────────┘
```

| 步骤 | 耗时量级 | 说明 |
|---|---|---|
| TCP 三次握手 | 1 RTT（局域网 ~0.1ms，跨城 ~10ms） | 内核协议栈开销 |
| TLS 握手 | 1-2 RTT | 若启用 SSL |
| 认证 | 1 RTT + 查系统表 | MD5/SCRAM 验证 |
| 会话初始化 | 查询 GUC、search_path | 后端进程 fork 后的初始化 |
| **合计** | **局域网 ~2-5ms，跨网 ~30-100ms** | 每次新建都要付 |

> **关键点**：这条连接的建立成本是**固定开销**，和你要执行的 SQL 无关。如果每处理一个 HTTP 请求就新建一条连接、用完销毁，那么短查询下连接建立时间可能比查询执行时间还长。

#### TCP 三次握手到底在做什么

新手常问："三次握手为什么是三次，不是两次？"用一句话讲：**两次只能确认一方收到，三次才能双向确认都收到**。

```
客户端                      服务端
  │                            │
  │── SYN（我想连你）─────────→│   服务端知道：客户端能发
  │                            │
  │←── SYN+ACK（我也想连你）──│   客户端知道：服务端能收能发
  │                            │
  │── ACK（收到你的回复）─────→│   服务端知道：客户端能收
  │                            │
  └──── 可以传数据了 ─────────┘
```

| 握手次数 | 客户端知道 | 服务端知道 | 够吗 |
|---|---|---|---|
| 1 次（SYN） | — | 客户端能发 | 不够 |
| 2 次（SYN+ACK） | 服务端能收能发 | 客户端能发 | 客户端不知道服务端能不能收 |
| 3 次（ACK） | 服务端能收能发 | 客户端能收能发 | **够，双向通路确认** |

每次握手都要一个 RTT（Round-Trip Time，往返时延）。同城局域网 RTT 约 0.1ms，跨城约 5-20ms，跨国约 100-300ms。这就是为什么"数据库和应用放同一个机房"是黄金法则——RTT 越小，建连越快，每条 SQL 也越快。

#### 认证握手的额外开销

TCP 通了还不算完，数据库还要验证你是谁：

```
客户端                              PostgreSQL
  │                                    │
  │── StartupMessage(user, db) ───────→│
  │←── AuthenticationRequest(MD5/SCRAM)│  要求认证
  │── 认证响应（加密后的密码）────────→│  查 pg_shadow 比对
  │←── AuthenticationOk ───────────────│  认证通过
  │←── ParameterStatus（时区、编码…）──│  下发会话参数
  │←── ReadyForQuery ──────────────────│  可以发 SQL 了
```

| 认证方式 | 耗时 | 安全性 | 建议 |
|---|---|---|---|
| `trust` | 最低 | 无密码，危险 | 仅本地调试 |
| `password` | 低 | 明文传输 | 禁用 |
| `md5` | 低 | 哈希但易重放 | 旧系统兼容 |
| `scram-sha-256` | 中 | 现代标准 | **推荐** |
| `cert` | 中 | 客户端证书 | 高安全场景 |

> **新手提示**：认证本身也要查一次系统表（`pg_shadow` 存密码哈希），所以"建连"= 网络握手 + 认证查询 + 会话初始化，三段开销叠加。

### PostgreSQL 进程模型的代价

PostgreSQL 是**每连接一进程**（process-per-connection）模型：

```
postmaster（主进程，监听 5432）
  │
  ├── fork → backend_1（连接1）  占用 ~5-10MB 内存
  ├── fork → backend_2（连接2）  占用 ~5-10MB 内存
  ├── fork → backend_3（连接3）  占用 ~5-10MB 内存
  └── ... 1000 个连接 → 5-10GB 内存
```

| 问题 | 说明 |
|---|---|
| **内存占用** | 每个 backend 进程独占 5-10MB（含 local 缓存、排序区、上下文） |
| **fork 开销** | Linux fork + exec 约 1-2ms，高并发下成为瓶颈 |
| **上下文切换** | 进程间切换比线程切换贵（TLB 刷新） |
| **连接数上限** | `max_connections` 默认 100，超过要排队或报错 |

#### 为什么 PG 用进程而不是线程

这是历史决定。PostgreSQL 诞生于 1996 年，当时多线程编程还不成熟，而"每连接 fork 一个进程"是 Unix 最经典的并发模型，简单且隔离性强：

| 模型 | 隔离性 | 内存共享 | 崩溃影响 | 代表 |
|---|---|---|---|---|
| 每连接一进程（PG） | 强 | 无（各自独立） | 一个 backend 崩溃不影响其他 | PostgreSQL、Oracle |
| 每连接一线程（MySQL） | 弱 | 共享进程内存 | 一个线程崩溃可能拖垮整个进程 | MySQL、MariaDB |

> **代价**：进程比线程重。一个 PG backend 至少 5MB 起步，1000 个连接就要 5GB 内存，光维持连接就吃掉一大块内存，还没算实际查询的排序、聚合缓冲。这就是为什么 PG 需要 PgBouncer——把 1000 个客户端连接复用到 20 个 backend 进程上。

#### 连接数限制的连锁反应

`max_connections` 不是越大越好，它和内存、文件描述符、CPU 调度都相关：

```
max_connections = 1000
  → 1000 个 backend 进程 × 5MB = 5GB 内存（仅连接）
  → 1000 个文件描述符（要调 ulimit -n）
  → 1000 个进程的 CPU 调度开销（上下文切换）
  → shared_pool 被大量 backend 平摊，每个分到的缓存变小
```

| `max_connections` | 内存占用（仅连接） | 建议 | 适用 |
|---|---|---|---|
| 100（默认） | ~500MB | 小型应用 | 单机 PG |
| 200-300 | ~1.5GB | 中型应用 | 加 PgBouncer 后 |
| 500+ | ~2.5GB+ | 不推荐 | 应该用 PgBouncer 省连接 |

> **正确做法**：`max_connections` 保持 100-200，前面放 PgBouncer。PgBouncer 可以接 5000 个客户端连接，但只向 PG 开 20 个 server 连接。这样 PG 内存可控，应用侧也不受限。

> **对比 SQLite**：SQLite 是嵌入式，没有网络连接开销，"连接"就是打开一个文件。但 SQLite 的并发写入受限于单写者锁，连接池在 SQLite 中的作用主要是**复用已打开的文件句柄和已解析的 SQL**，而非省网络连接。

### 不用连接池会怎样

假设一个 Web 服务，每秒处理 1000 个请求，每个请求用一次数据库：

```
无连接池：
  每秒新建 1000 条连接 × 3ms 建连 = 3 秒/秒 的 CPU 时间花在建连上
  峰值时 backend 进程数 = 并发请求数 → PG 内存爆炸

有连接池（池大小 20）：
  启动时建 20 条连接 = 60ms 一次性成本
  之后 1000 个请求复用这 20 条连接 → PG 只有 20 个 backend
  每个请求的"建连"成本 = 0（从池里借，~1μs）
```

| 场景 | 无连接池 | 有连接池（20） | 提升 |
|---|---|---|---|
| 1000 请求/秒，每请求 1 条 SQL | 3000ms 建连/秒 | 0ms 建连/秒 | 建连开销归零 |
| PG backend 进程数 | 1000 | 20 | 内存省 50 倍 |
| PG 内存（仅连接） | 5-10GB | 100-200MB | 省 50 倍 |
| 单请求延迟 | 3ms 建连 + 1ms 查询 = 4ms | 0ms 建连 + 1ms 查询 = 1ms | 快 4 倍 |

> **新手记住**：连接池的核心价值不是"快一点"，而是"把固定开销摊到多次请求上"。请求越多、连接建立越慢（跨网），收益越大。

## PgBouncer 原理

PgBouncer 是 PostgreSQL 最流行的连接池中间件。它坐在应用和 PG 之间：

```
应用1 ─┐
应用2 ─┼──→ PgBouncer（池化）──→ PostgreSQL（少量 backend）
应用3 ─┘        │
                 ├── client 连接（多，廉价）
                 └── server 连接（少，昂贵）
```

### 三种池化模式

| 模式 | 连接绑定 | 适用场景 | 事务内能用的功能 |
|---|---|---|---|
| **session pooling** | 客户端连接 ←→ server 连接，整会话绑定 | 通用，最保守 | 全部（临时表、SET、PREPARE） |
| **transaction pooling** | 事务结束时归还连接 | 绝大多数 Web 应用 | 仅事务内；事务间不保留状态 |
| **statement pooling** | 每条语句执行完归还 | 简单查询工具 | 无状态；不支持事务 |

#### Session Pooling

```
客户端连接 C1 ──── BEGIN ──── INSERT ──── COMMIT ──── SELECT ──── 断开
                    │                                          │
                    └──────── 绑定到 server S1 ────────────────┘
                    
C1 断开后，S1 才归还到池里
```

- 优点：会话内状态完整保留（临时表、SET 变量、PREPARE 语句、LISTEN/NOTIFY）
- 缺点：客户端闲着不查询时，server 连接也被占着 → 池大小要 ≥ 并发客户端数 → 几乎没省

| 场景 | 是否适合 session 模式 | 原因 |
|---|---|---|
| psql 交互式终端 | ✅ | 用户会保留临时表、SET 变量 |
| 长连接的批处理脚本 | ✅ | 脚本依赖会话状态 |
| Web 应用（请求短） | ❌ | 连接被占着不查询，浪费 |
| 微服务高并发 | ❌ | 池大小要等于客户端数，没省 |

#### Transaction Pooling

```
客户端连接 C1 ──── BEGIN ──── INSERT ──── COMMIT ──── (空闲) ──── SELECT ────
                    │           │           │                      │           │
                    ├── 绑定 S1 ─────────────┘                      └── 绑定 S2 ┘
                    
COMMIT 后 S1 立即归还到池，C1 空闲时不占 server 连接
```

- 优点：server 连接数 ≈ 并发事务数（远小于并发客户端数），**这是 PgBouncer 的核心价值**
- 缺点：事务间不保留状态。`SET search_path` 在 COMMIT 后丢失；临时表在 COMMIT 后消失
- **Web 应用首选**：HTTP 请求通常 = 一个事务，请求结束 = 事务结束 = 连接归还

| 场景 | 是否适合 transaction 模式 | 原因 |
|---|---|---|
| Web 应用（每请求一事务） | ✅ | 请求结束即归还，连接利用率最高 |
| 微服务高并发 | ✅ | 5000 客户端 → 20 server |
| 需要临时表的批处理 | ❌ | COMMIT 后临时表消失 |
| 用 LISTEN/NOTIFY | ❌ | 通知绑定在具体连接 |

#### Statement Pooling

```
每条 SQL 执行完立即归还连接
BEGIN ... COMMIT 会被拆到不同 server 连接 → 事务语义破坏！
```

- 仅适用于无事务的简单查询工具（如 psql 自动补全、BI 工具的元数据查询）
- **不要用于应用代码**

> **新手警告**：statement 模式下 `BEGIN; UPDATE ...; COMMIT;` 三条语句可能被分到三个不同的 server 连接上执行，事务完全失效。除非你百分之百确定只跑无事务的只读查询，否则别用。

### PgBouncer 配置示例

```ini
[databases]
postgres = host=127.0.0.1 port=5432 dbname=app

[pgbouncer]
listen_addr = 0.0.0.0
listen_port = 6432
auth_type = scram-sha-256
auth_file = /etc/pgbouncer/userlist.txt

pool_mode = transaction
max_client_conn = 1000
default_pool_size = 20
reserve_pool_size = 5
reserve_pool_timeout = 3
server_idle_timeout = 600
```

| 参数 | 含义 | 调优建议 |
|---|---|---|
| `pool_mode` | 池化模式 | Web 应用用 `transaction` |
| `max_client_conn` | 最大客户端连接 | 1000-5000，廉价 |
| `default_pool_size` | 每个 DB 的 server 连接数 | ≈ CPU 核数 × 2 ~ 50 |
| `reserve_pool_size` | 排队满后额外连接 | 5-10，应对突发 |
| `server_idle_timeout` | server 空闲多久后关闭 | 600s，平衡复用与资源释放 |

> **计算公式**：`default_pool_size × 数据库数量 ≤ PG 的 max_connections`。如果有多个 DB 通过同一个 PgBouncer，要算总账。

#### 配置逐行解读

```ini
[databases]
# 数据库连接目标：PgBouncer 收到的连接按 dbname 转发到真实 PG
postgres = host=127.0.0.1 port=5432 dbname=app
#           ↑ PG 真实地址            ↑ 真实库名
```

```ini
[pgbouncer]
listen_addr = 0.0.0.0   # 监听所有网卡（生产建议只监听内网）
listen_port = 6432       # PgBouncer 端口，应用连这个，不连 5432
auth_type = scram-sha-256  # 认证方式，和 PG 保持一致
auth_file = /etc/pgbouncer/userlist.txt  # 用户密码文件
```

```ini
pool_mode = transaction       # 池化模式（最关键参数）
max_client_conn = 1000        # 客户端连接上限（廉价，可以开大）
default_pool_size = 20        # 每个 DB×user 的 server 连接数
reserve_pool_size = 5         # 池满后临时扩容
reserve_pool_timeout = 3     # 排队超过 3 秒才动用 reserve
server_idle_timeout = 600    # server 空闲 10 分钟后关闭
```

> **新手常见错误**：把 `default_pool_size` 设成 100 以为越大越好。实际上 server 连接数应该 ≈ CPU 核数 × 2，因为 PG 一个时刻只能用这么多 CPU，多了反而上下文切换开销大。

### PgBouncer 的限制

| 不能用 | 原因 |
|---|---|
| 临时表跨事务 | transaction 模式 COMMIT 后换连接，临时表随连接销毁 |
| `SET` 持久化 | 同上，会话变量随连接归还而丢失 |
| `LISTEN/NOTIFY` | 通知绑定在具体连接上 |
| 两阶段提交 | `PREPARE TRANSACTION` 需要连接保持 |
| SQL 级 PREPARE | pgbouncer 自己管理 prepared statement，需开启 `max_prepared_statements` |

> **解决方案**：需要会话状态的操作，用一条**直连 PG**的连接（绕过 PgBouncer），或用 `pgbouncer` 的 `pool_mode = session` 专门给这类连接。

#### 在同一个 PgBouncer 里混合两种模式

可以通过"虚拟数据库名"给不同应用分配不同模式：

```ini
[databases]
; Web 应用走 transaction 模式
app_web = host=127.0.0.1 port=5432 dbname=app pool_mode=transaction

; 批处理脚本走 session 模式（需要临时表）
app_batch = host=127.0.0.1 port=5432 dbname=app pool_mode=session

[pgbouncer]
; 全局默认模式
pool_mode = transaction
default_pool_size = 20
```

```
应用连 app_web   → transaction 模式（20 个 server 连接复用）
应用连 app_batch → session 模式（每个客户端独占一个 server 连接）
```

> **关键**：两个虚拟名指向同一个真实库，但池化策略不同。Web 应用用 `app_web`，批处理用 `app_batch`，互不干扰。

## Python 连接池方案

### 方案对比

| 方案 | 库 | 异步 | 适用 | 池化方式 |
|---|---|---|---|---|
| psycopg2 + 手写池 | psycopg2 | 否 | 同步传统应用 | `ThreadedConnectionPool` 或自写 |
| psycopg3 | psycopg | 可选 | 新项目，同步异步都支持 | 内置 `ConnectionPool` |
| asyncpg | asyncpg | 是 | 高并发异步应用 | 内置 `Pool` |
| SQLAlchemy | sqlalchemy | 可选 | ORM 应用 | `QueuePool` / `NullPool` |
| SQLite 连接复用 | sqlite3 | 否 | 嵌入式 | 单连接 + check_same_thread=False 或自写池 |

#### 选型决策树

```
你的应用用什么数据库？
├── PostgreSQL
│   ├── 需要异步？ ──是──→ asyncpg（性能最佳）
│   │                └──否──┐
│   ├── 用 ORM？ ──是──→ SQLAlchemy + psycopg3
│   │              └──否──┐
│   └──→ psycopg3（新项目）或 psycopg2（老项目）
└── SQLite
    ├── 需要异步？ ──是──→ aiosqlite 或 asyncio.to_thread
    └──→ sqlite3 + 自写池（见 app.py）
```

### psycopg2 的 ThreadedConnectionPool

```python
from psycopg2 import pool

pg_pool = pool.ThreadedConnectionPool(
    minconn=5,      # 最小连接数（启动时创建）
    maxconn=20,     # 最大连接数（超过则阻塞等待）
    host='localhost', dbname='app', user='postgres', password='secret'
)

def query(sql, params=()):
    conn = pg_pool.getconn()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
        cur.close()
        return rows
    finally:
        pg_pool.putconn(conn)   # 必须归还！否则泄漏
```

| 参数 | 含义 |
|---|---|
| `minconn` | 启动时预创建的连接数 |
| `maxconn` | 上限，超过时 `getconn()` 阻塞直到有连接归还 |
| **线程安全** | 内部有锁，多线程可共用一个池实例 |

> **坑**：`getconn()` 后如果异常没走到 `putconn()`，连接就泄漏了。必须用 `try/finally`。

#### 用上下文管理器封装（推荐）

```python
from contextlib import contextmanager

@contextmanager
def db_conn():
    conn = pg_pool.getconn()
    try:
        yield conn
    finally:
        pg_pool.putconn(conn)

# 使用：自动归还，不会泄漏
with db_conn() as conn:
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE id=%s", (42,))
    user = cur.fetchone()
```

> **对比**：`try/finally` 写法每次都要写 5 行样板代码，`with` 写法只要 3 行，且不可能忘记归还。生产代码统一用 `with`。

### asyncpg 的 Pool（异步原生）

```python
import asyncio
import asyncpg

async def main():
    pool = await asyncpg.create_pool(
        host='localhost', database='app', user='postgres', password='secret',
        min_size=5, max_size=20, max_queries=50000, max_inactive_connection_lifetime=300
    )
    async with pool.acquire() as conn:
        rows = await conn.fetch('SELECT * FROM users WHERE id=$1', 42)
    await pool.close()

asyncio.run(main())
```

| 参数 | 含义 |
|---|---|
| `min_size` | 最小连接数 |
| `max_size` | 最大连接数 |
| `max_queries` | 单连接执行多少次查询后自动重建（防止内存泄漏） |
| `max_inactive_connection_lifetime` | 空闲连接最大存活秒数 |

> **asyncpg 优势**：纯异步、协议级实现（不走 psycopg2 的同步阻塞），单连接吞吐可达 psycopg2 的 3-5 倍。

### SQLAlchemy 的 QueuePool

```python
from sqlalchemy import create_engine

engine = create_engine(
    'postgresql+psycopg2://postgres:secret@localhost/app',
    pool_size=20,           # 持续保持的连接数
    max_overflow=10,        # 允许临时超出的连接数
    pool_timeout=30,        # 获取连接超时秒数
    pool_recycle=3600,      # 连接最大存活秒数（防 MySQL 8h 断连）
    pool_pre_ping=True      # 借出前先 ping 一下，防死连接
)

with engine.connect() as conn:
    result = conn.execute(text("SELECT * FROM users WHERE id=:id"), {"id": 42})
```

| 参数 | 含义 | 建议值 |
|---|---|---|
| `pool_size` | 池中常驻连接 | 10-20 |
| `max_overflow` | 超出 pool_size 的临时连接 | 5-10 |
| `pool_timeout` | 池满时等待秒数 | 30 |
| `pool_recycle` | 连接重建周期 | 3600（PG）/ 28800（MySQL 8h 以下） |
| `pool_pre_ping` | 借出前检测存活 | True（生产必开） |
| `poolclass=NullPool` | 不池化 | 仅用于测试/单次脚本 |

> **`pool_pre_ping`**：生产环境必开。否则 PG 重启后池里全是死连接，第一个请求会报 `OperationalError: server closed the connection`。

#### SQLAlchemy 池的工作流程

```
engine.connect() 被调用
        │
        ├── 池里有空闲连接？ ──是──→ 取出，pool_pre_ping 测活
        │                        │       ├── 活 → 返回这个连接
        │                        │       └── 死 → 丢弃，再取下一个
        │                        └──否──→ 当前连接数 < pool_size+max_overflow？
        │                                 ├── 是 → 新建连接，返回
        │                                 └── 否 → 阻塞等待 pool_timeout 秒
        │                                          ├── 超时 → 抛 TimeoutError
        │                                          └── 有连接归还 → 取出返回
        │
with 块结束
        │
        └── 连接归还到池（不关闭），等下次复用
```

### SQLite 的连接复用

SQLite 没有网络连接，但连接对象本身有开销（打开文件、读 schema、维护 prepared statement 缓存）。多线程下 SQLite 默认禁止跨线程用同一连接（`check_same_thread=True`），需要池化：

```python
import sqlite3
import queue
import threading

class SQLitePool:
    def __init__(self, path, size=10):
        self.path = path
        self.pool = queue.Queue(maxsize=size)
        self.local = threading.local()
        for _ in range(size):
            conn = sqlite3.connect(path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self.pool.put(conn)

    def getconn(self):
        return self.pool.get()

    def putconn(self, conn):
        self.pool.put(conn)

    @contextmanager
    def connection(self):
        conn = self.getconn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self.putconn(conn)
```

| SQLite 池化收益 | 说明 |
|---|---|
| 复用 prepared statement 缓存 | 同一连接重复执行相同 SQL 跳过解析 |
| 复用文件句柄 | 避免反复 open/close |
| WAL 下多读并发 | 多个连接可同时读 |
| **注意** | 写入仍串行（WAL 单写者），池大小对写无益 |

#### SQLite PRAGMA 逐条解释

```python
conn = sqlite3.connect(path, check_same_thread=False)
conn.execute("PRAGMA journal_mode=WAL")      # 1. 日志模式
conn.execute("PRAGMA foreign_keys=ON")       # 2. 外键约束
conn.execute("PRAGMA busy_timeout=5000")     # 3. 锁等待
```

| PRAGMA | 作用 | 不设的后果 |
|---|---|---|
| `journal_mode=WAL` | 写前日志（Write-Ahead Log），读写不互斥 | 默认 DELETE 模式，写时阻塞所有读 |
| `foreign_keys=ON` | 启用外键约束检查 | 默认 OFF，外键形同虚设 |
| `busy_timeout=5000` | 遇到锁时等 5 秒再报错 | 默认立即报 `database is locked` |
| `check_same_thread=False` | 允许跨线程用连接（配合池） | 默认禁止，池化必须关掉 |

> **新手提示**：`check_same_thread=False` 打开后，SQLite 不再检查线程，但你必须自己保证"同一时刻只有一个线程用一条连接"——这正是连接池做的事：借出时独占，归还后别人才能借。

## ORM 的 N+1 查询问题

### 什么是 N+1

```
查询 N 个用户，再查每个用户的文章 → 1 次查用户 + N 次查文章 = N+1 次查询

本应：1 次 JOIN 查询 = 1 次查询
```

这是 ORM 最经典的性能陷阱。ORM 默认 lazy loading 关联对象，导致循环里隐式触发查询。

### 为什么 N+1 慢

新手常想："多查几次数据库而已，能慢多少？"慢在三个地方叠加：

| 慢在哪 | 说明 | 量级 |
|---|---|---|
| **网络往返** | 每次查询一个 RTT，N 次就是 N×RTT | 100 次 × 1ms = 100ms |
| **查询解析** | 每条 SQL 都要解析、规划、优化 | 100 次 × 0.5ms = 50ms |
| **连接占用** | 每次查询占连接（即使复用），串行执行 | 100 次串行 vs 1 次 |

```
N+1（100 用户）：
  1 次查用户（1ms）+ 100 次查文章（100 × 1ms）= 101ms
  而且是串行的：必须先拿到用户 id，才能查文章

JOIN（100 用户）：
  1 次查询（2ms，结果稍大）= 2ms
  一次往返，一次解析
```

> **关键**：N+1 的慢是**线性增长**的（O(N)），而正确写法是**常数**的（O(1)）。N=10 时差距不明显，N=1000 时差 1000 倍。

### 复现 N+1（SQLAlchemy 风格）

```python
users = session.query(User).all()        # 1 次查询：SELECT * FROM users
for u in users:
    print(u.name, u.posts)               # 每次访问 u.posts 触发：
                                         #   SELECT * FROM posts WHERE user_id=?
# 总计：1 + len(users) 次查询
```

| 用户数 | N+1 查询数 | 正确查询数 | 浪费 |
|---|---|---|---|
| 10 | 11 | 1 | 10 倍 |
| 100 | 101 | 1 | 100 倍 |
| 1000 | 1001 | 1 | 1000 倍 |

### 三种解决方案

#### 方案1：JOIN（手动 SQL）

```sql
SELECT u.id, u.name, p.id, p.title
FROM users u
LEFT JOIN posts p ON p.user_id = u.id
ORDER BY u.id;
```

- 1 次查询，但结果需要应用层"摊平"成对象树
- 最快，但要手写映射逻辑

```python
# 应用层摊平
users_map = {}
for r in rows:
    uid = r["user_id"]
    if uid not in users_map:
        users_map[uid] = {"user": {"id": uid, "name": r["user_name"]}, "posts": []}
    if r["post_id"] is not None:
        users_map[uid]["posts"].append({"id": r["post_id"], "title": r["post_title"]})
result = list(users_map.values())
```

#### 方案2：Eager Loading（ORM）

```python
# SQLAlchemy
users = session.query(User).options(joinedload(User.posts)).all()

# Django
users = User.objects.prefetch_related('posts').all()

# Peewee
users = User.select().prefetch(Post.select())
```

| 策略 | SQL 形式 | 适用 |
|---|---|---|
| `joinedload` / `select_related` | LEFT JOIN | 一对一、多对一（关联对象少） |
| `selectinload` / `prefetch_related` | 第二条 `IN` 查询 | 一对多、多对多（避免 JOIN 行数爆炸） |

#### 方案3：批量 IN 查询（手动两步）

```python
users = session.query(User).all()                          # 1 次
user_ids = [u.id for u in users]
posts = session.query(Post).filter(Post.user_id.in_(user_ids)).all()  # 1 次
posts_by_user = defaultdict(list)
for p in posts:
    posts_by_user[p.user_id].append(p)
for u in users:
    u.posts = posts_by_user[u.id]
# 总计：2 次查询，与 N 无关
```

#### 三种方案代码对比

| 方案 | 查询数 | 代码量 | 适合 | 代码示例 |
|---|---|---|---|---|
| JOIN | 1 | 中（要摊平） | 手写 SQL、性能极致 | `SELECT ... LEFT JOIN ...` |
| Eager Load | 1 或 2 | 少（一行） | ORM 项目、生产首选 | `query.options(joinedload(...))` |
| 批量 IN | 2 | 中（要组装） | 手写、分批控制 | `filter(id.in_(ids))` |

```python
# === 完整对比（伪代码，体现思路差异） ===

# 方案1：JOIN
rows = db.execute("SELECT u.*, p.* FROM users u LEFT JOIN posts p ON p.user_id=u.id")
result = flatten(rows)  # 应用层摊平

# 方案2：Eager Load（ORM 一行搞定）
result = session.query(User).options(selectinload(User.posts)).all()

# 方案3：批量 IN
users = db.execute("SELECT * FROM users").fetchall()
ids = [u["id"] for u in users]
posts = db.execute("SELECT * FROM posts WHERE user_id IN (?)", ids).fetchall()
result = assemble(users, posts)  # 应用层组装
```

### selectinload vs joinedload 的选择

```
一对多：User → Posts
  joinedload：1 次 JOIN，但 User 字段重复 N 次（行数 = 帖子数）→ 网络传输大
  selectinload：2 次查询，User 不重复 → 网络传输小，但多 1 个 RTT

多对一：Post → User
  joinedload：1 次 JOIN，行数 = 帖子数，User 字段重复但无膨胀 → 好
  selectinload：2 次查询 → 没必要
```

| 关联类型 | 推荐 | 原因 |
|---|---|---|
| 多对一（N:1） | `joinedload` | JOIN 不膨胀 |
| 一对一 | `joinedload` | 同上 |
| 一对多（1:N） | `selectinload` | 避免 JOIN 行数膨胀 |
| 多对多（M:N） | `selectinload` | 同上 |

#### 行数膨胀的直观例子

```
10 个用户，每个 100 帖子，共 1000 帖子

joinedload（LEFT JOIN）：
  返回 1000 行，每行都有 user 的 5 个字段 + post 的 3 个字段
  user 字段重复 100 次 → 传输 1000 × (5+3) = 8000 字段值

selectinload（2 次查询）：
  第 1 次：SELECT * FROM users → 10 行 × 5 字段 = 50 字段值
  第 2 次：SELECT * FROM posts WHERE user_id IN (...) → 1000 行 × 3 字段 = 3000 字段值
  合计 3050 字段值，比 JOIN 少 60%
```

> **新手记住**：一对多用 `selectinload`（`prefetch_related`），多对一用 `joinedload`（`select_related`）。这是 ORM 性能调优的第一课。

## 事务边界管理

### 为什么事务边界重要

事务是"要么全做，要么全不做"的保证。但这个保证有个前提：**所有操作必须在同一条连接上、同一个事务里**。连接池让这件事变复杂——因为连接会被复用，稍不留神就把一个事务的操作分散到不同连接上。

```
转账 100 元：A 扣 100，B 加 100
  正确（一个事务）：A-100 和 B+100 同时成功或同时失败
  错误（跨连接）：A-100 成功了，B+100 失败了 → 100 元消失了！
```

| 场景 | 事务保护 | 后果 |
|---|---|---|
| 同连接同事务 | ✅ 原子性保证 | 一致 |
| 同连接不同事务 | ❌ 两条 SQL 各自提交 | 中途崩溃不一致 |
| 不同连接 | ❌ 完全独立的事务 | 更危险 |

### 事务边界的三个层次

```
层次1：单条语句自动事务（autocommit）
  每条 SQL 自成一个事务，自动提交
  适合：简单脚本、DDL

层次2：显式事务块
  BEGIN
    SQL1
    SQL2
    SQL3
  COMMIT / ROLLBACK
  适合：需要原子性的业务操作

层次3：声明式事务（框架/装饰器）
  @transactional
  def transfer(from_id, to_id, amount):
      ...
  适合：业务层，与连接池配合
```

### 连接池下的事务边界

**核心原则：事务跨越的代码段内，必须持有同一条连接。**

```python
# 错误：每次 query 从池里借不同连接，事务跨了连接！
def transfer(from_id, to_id, amount):
    query("UPDATE accounts SET balance=balance-? WHERE id=?", (amount, from_id))  # 连接 A
    query("UPDATE accounts SET balance=balance+? WHERE id=?", (amount, to_id))    # 连接 B
    # 两条 UPDATE 在不同连接的不同事务里，不原子！

# 正确：借一次连接，整个事务用同一条
def transfer(from_id, to_id, amount):
    conn = pool.getconn()
    try:
        conn.execute("BEGIN")
        conn.execute("UPDATE accounts SET balance=balance-? WHERE id=?", (amount, from_id))
        conn.execute("UPDATE accounts SET balance=balance+? WHERE id=?", (amount, to_id))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        pool.putconn(conn)
```

#### 跨连接事务的错误演示

```python
# 反面教材：每条 SQL 独立借连接
def transfer_wrong(from_id, to_id, amount):
    with pool.connection() as conn:  # 借连接 A
        conn.execute("UPDATE accounts SET balance=balance-? WHERE id=?",
                     (amount, from_id))  # A 扣款，自动提交
    # 连接 A 归还。此时 A 已经扣了钱！
    raise RuntimeError("模拟失败")  # 中途崩溃
    with pool.connection() as conn:  # 借连接 B（可能不是 A）
        conn.execute("UPDATE accounts SET balance=balance+? WHERE id=?",
                     (amount, to_id))  # B 加款，永远执行不到
```

```
执行前：A=1000, B=1000
执行后：A=900, B=1000  ← 100 元消失了！
```

> **这就是为什么事务必须包在一个 `with pool.transaction()` 块里**：要么两条都成功，要么两条都不执行（回滚）。

### 用上下文管理器封装

```python
from contextlib import contextmanager

@contextmanager
def transaction(pool):
    conn = pool.getconn()
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        pool.putconn(conn)

# 使用
with transaction(pool) as conn:
    conn.execute("UPDATE accounts SET balance=balance-? WHERE id=?", (amount, from_id))
    conn.execute("UPDATE accounts SET balance=balance+? WHERE id=?", (amount, to_id))
```

#### `with transaction()` 的执行流程

```
with transaction(pool) as conn:
      │
      ├── 进入 with：getconn() 借连接，执行 BEGIN
      │
      │   yield conn  ← 把连接交给 with 块里的代码
      │
      │   with 块里的 SQL 都用这个 conn（同一条连接）
      │
      ├── with 块正常结束：执行 COMMIT，putconn() 归还
      │
      └── with 块抛异常：执行 ROLLBACK，putconn() 归还，重新抛出异常
```

| 阶段 | 正常路径 | 异常路径 |
|---|---|---|
| 进入 | `getconn` + `BEGIN` | 同左 |
| 执行 | `yield conn`，跑 with 块 | 同左，with 块抛异常 |
| 退出 | `COMMIT` + `putconn` | `ROLLBACK` + `putconn` + `raise` |
| 结果 | 事务提交，连接归还 | 事务回滚，连接归还，异常上抛 |

### 事务边界的反模式

| 反模式 | 问题 | 后果 |
|---|---|---|
| 事务跨 HTTP 请求 | 连接持有时间 = 用户思考时间 | 连接被占，池耗尽 |
| 事务里调外部 API | API 超时 → 连接一直被占 | 锁持有过久，死锁 |
| 忘记 COMMIT/ROLLBACK | 连接带着未决事务归还 | 下个使用者读到脏数据 |
| 嵌套 BEGIN | SQLite 报错 / PG 创建 savepoint | 行为不符预期 |
| 事务里做大量计算 | 持有锁时间长 | 阻塞其他事务 |

> **黄金法则**：事务要**短**。打开事务 → 执行 SQL → 立即提交/回滚。事务里不要做 I/O（除数据库外）、不要等用户输入、不要 sleep。

#### 反模式详解：事务跨 HTTP 请求

```python
# 反面教材：在请求 A 里开事务，在请求 B 里提交
# 这在无状态 Web 服务里根本做不到，但新手常犯的变体是：
@app.route("/checkout/start")
def checkout_start():
    conn = pool.getconn()
    conn.execute("BEGIN")
    conn.execute("UPDATE cart SET status='processing' WHERE user_id=?", (user_id,))
    session['conn_id'] = id(conn)  # 把连接 id 存 session
    # 连接不归还！等用户点"确认支付"再提交
    # 问题：用户可能关掉浏览器，连接永远不归还 → 泄漏

@app.route("/checkout/confirm")
def checkout_confirm():
    conn = find_conn_by_id(session['conn_id'])  # 找回连接
    conn.execute("COMMIT")
    pool.putconn(conn)
```

> **正确做法**：用"状态字段"代替跨请求事务。`checkout_start` 把状态改成 `processing` 并提交，`checkout_confirm` 读到 `processing` 再改成 `done`。事务不跨请求。

### 事务隔离级别与连接池

```python
# 池里的连接设置一次隔离级别，复用时保持
conn = pool.getconn()
conn.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")  # 默认
# 或 SERIALIZABLE / REPEATABLE READ / READ UNCOMMITTED

# PgBouncer transaction 模式下：SET TRANSACTION 只影响下一个事务
# 不能用 SET default_transaction_isolation（会话级，会被 transaction 模式丢弃）
```

| 隔离级别 | PgBouncer transaction 模式 | 说明 |
|---|---|---|
| READ COMMITTED | ✅ 支持 | 每个事务独立快照 |
| REPEATABLE READ | ✅ 支持 | 同上 |
| SERIALIZABLE | ✅ 支持 | 同上 |
| 会话级 SET | ❌ 丢失 | 用 `SET TRANSACTION` 代替 |

## 连接泄漏排查

### 什么是连接泄漏

```python
def bad_function():
    conn = pool.getconn()       # 借了连接
    cur = conn.cursor()
    cur.execute("SELECT ...")
    if cur.fetchone()[0] == 0:
        return None             # ← 这里 return 了，没 putconn！连接泄漏
    ...
    pool.putconn(conn)          # 只有走到这里才归还
```

泄漏的连接：被借出但永远不会归还，池里可用连接越来越少，最终 `getconn()` 阻塞或超时，整个服务卡死。

#### 泄漏的渐进过程

```
池大小 10，每秒泄漏 1 个：

t=0s:  可用=10, 借出=0
t=1s:  可用=9,  借出=1（泄漏）
t=2s:  可用=8,  借出=2（泄漏）
...
t=9s:  可用=1,  借出=9（泄漏）
t=10s: 可用=0,  借出=10（泄漏）→ 新请求 getconn() 阻塞
t=15s: getconn() 超时，服务报 500
```

| 阶段 | 可用连接 | 表现 |
|---|---|---|
| 早期 | 缓慢减少 | 无感知，服务正常 |
| 中期 | < 20% | 偶尔请求变慢（排队等连接） |
| 晚期 | = 0 | 所有请求阻塞，超时报错 |
| 最终 | — | 服务假死，必须重启 |

> **可怕之处**：泄漏是**静默**的。没有异常，没有日志，直到池耗尽才爆发。所以必须主动监控。

### 排查手段

#### 1. 池的统计信息

```python
# 自定义池：记录借出/归还
class TrackedPool:
    def __init__(self, ...):
        self.checked_out = {}  # conn_id → (stack_trace, timestamp)

    def getconn(self):
        conn = super().getconn()
        self.checked_out[id(conn)] = (traceback.format_stack(), time.time())
        return conn

    def putconn(self, conn):
        self.checked_out.pop(id(conn), None)
        super().putconn(conn)

    def leak_report(self):
        for conn_id, (stack, t) in self.checked_out.items():
            age = time.time() - t
            if age > 10:  # 借出超过 10 秒未还
                print(f"连接 {conn_id} 借出 {age:.1f}s 未还")
                print("借出位置:", ''.join(stack))
```

#### 2. 数据库侧观察

```sql
-- PostgreSQL：看每个连接的状态和持续时间
SELECT pid, usename, state, query, 
       now() - query_start AS query_duration,
       now() - state_change AS state_duration,
       application_name
FROM pg_stat_activity
WHERE state != 'idle'
ORDER BY query_duration DESC;

-- 看连接数分布
SELECT state, count(*) FROM pg_stat_activity GROUP BY state;
```

| `state` | 含义 | 健康状态 |
|---|---|---|
| `active` | 正在执行 SQL | 短暂正常，长则有问题 |
| `idle` | 空闲等待下一条 SQL | 正常（池里的连接） |
| `idle in transaction` | 事务未提交，连接空闲 | **危险**：泄漏或事务太长 |
| `idle in transaction (aborted)` | 事务出错未回滚 | **危险** |

> **`idle in transaction` 是连接泄漏的头号信号**。如果看到连接长时间处于这个状态，说明某个事务开了没提交/回滚。

#### 3. 连接池超时告警

```python
# 给 getconn 加超时，超时说明池快耗尽了
try:
    conn = pool.getconn(timeout=5)
except PoolTimeout:
    logger.error("连接池耗尽！当前借出: %d", pool.checked_out_count)
    for stack in pool.leak_stacks():
        logger.error("泄漏点:\n%s", stack)
    raise
```

#### 4. 栈追踪定位泄漏点

```python
import traceback

class TrackedPool:
    def getconn(self):
        conn = self._pool.get()
        # 记录借出时的调用栈
        self._checked_out[id(conn)] = {
            'time': time.time(),
            'stack': traceback.format_stack()  # 关键：存栈
        }
        return conn

    def leak_report(self, threshold=10):
        for cid, info in self._checked_out.items():
            if time.time() - info['time'] > threshold:
                print(f"泄漏连接 {cid}，借出于：")
                print(''.join(info['stack']))  # 打印谁借的
```

```
输出示例：
泄漏连接 12345，借出于：
  File "app.py", line 156, in handle_request
    conn = pool.getconn()
  File "app.py", line 178, in process_order
    return bad_function(conn)   ← 泄漏点在这
```

> **栈追踪是定位泄漏的核武器**。每个借出都记下调用栈，一旦发现泄漏，直接看栈就知道是哪行代码借了不还。

### 防泄漏的最佳实践

| 实践 | 说明 |
|---|---|
| **永远用 try/finally** | `getconn` 后紧跟 `try`，`finally` 里 `putconn` |
| **用上下文管理器** | `with pool.connection() as conn:` 自动归还 |
| **用框架的声明式事务** | `@transactional` 装饰器自动管理 |
| **设置池超时** | `pool_timeout=5`，宁可快速失败也不要无限等待 |
| **监控池水位** | 告警阈值：活跃连接 > 80% 池大小 |
| **连接存活检测** | `pool_pre_ping=True`，借出前验证连接活着 |

#### with 语句为什么能防泄漏

```python
# 不用 with：容易泄漏
conn = pool.getconn()
cur = conn.cursor()
cur.execute("SELECT ...")
if condition:
    return cur.fetchone()  # ← 忘了 putconn，泄漏！
pool.putconn(conn)  # 只有走到这里才归还

# 用 with：不可能泄漏
with pool.connection() as conn:
    cur = conn.cursor()
    cur.execute("SELECT ...")
    if condition:
        return cur.fetchone()  # ← with 的 __exit__ 会自动 putconn
# 无论 return、break、异常，__exit__ 都会执行，连接一定归还
```

> **原理**：`with` 语句的 `__exit__` 方法**保证执行**，不管块里是正常返回、抛异常还是被信号打断。把 `putconn` 放在 `__exit__` 里，就从根本上杜绝了"忘记归还"。

## 异步数据库访问

### 为什么需要异步

同步模型下，一个请求占一个线程，线程阻塞在等数据库 I/O 时不做其他事：

```
同步：100 个请求 → 100 个线程 → 100 条 DB 连接
  线程在等 DB 时空转（或阻塞），浪费资源

异步：100 个请求 → 1 个事件循环 → 少量 DB 连接
  等 DB 时协程挂起，事件循环去处理其他请求
  I/O 密集场景吞吐量高 5-10 倍
```

#### 事件循环是怎么工作的

```
事件循环（单线程）：
  ┌─────────────────────────────────────────┐
  │  while True:                            │
  │    ready = select(所有待 I/O 的协程)     │  ← 哪个 I/O 就绪了？
  │    for coro in ready:                   │
  │      coro.run_until_blocked()           │  ← 跑到再次阻塞
  └─────────────────────────────────────────┘

协程 A：await db.query("SELECT ...")
  │
  ├── 发出查询，挂起（不阻塞线程）
  │
  │   事件循环趁机跑协程 B、C、D...
  │
  └── 查询结果回来，被事件循环唤醒，继续执行
```

| 模型 | 线程数 | DB 连接数 | CPU 利用 | 适合 |
|---|---|---|---|---|
| 同步（一请求一线程） | 100 | 100 | 低（大多阻塞在 I/O） | CPU 密集 |
| 异步（事件循环） | 1 | 10-20 | 高（I/O 等待时跑别的） | I/O 密集 |

> **关键**：异步不省 CPU，省的是"等待时的空闲"。I/O 密集（等数据库、等网络、等磁盘）的场景收益最大；CPU 密集（纯计算）的场景异步没优势，甚至更慢（协程调度开销）。

### asyncpg（PostgreSQL 异步原生）

```python
import asyncio
import asyncpg

async def get_user(pool, user_id):
    async with pool.acquire() as conn:
        return await conn.fetchrow('SELECT * FROM users WHERE id=$1', user_id)

async def main():
    pool = await asyncpg.create_pool('postgresql://postgres:secret@localhost/app',
                                      min_size=5, max_size=20)
    # 并发查 100 个用户，共用池里的连接
    tasks = [get_user(pool, i) for i in range(100)]
    users = await asyncio.gather(*tasks)
    await pool.close()

asyncio.run(main())
```

| 特性 | asyncpg | psycopg2 |
|---|---|---|
| 异步 | ✅ 原生 | ❌（需 gevent/eventlet 打补丁） |
| 协议 | 直接实现 PG 协议 | 包 libpq C 库 |
| Prepared statement | ✅ 连接级缓存 | 需手动 |
| 性能 | ~3-5x | 1x |
| 生态 | 较新 | 成熟 |

#### asyncpg 为什么快

| 原因 | 说明 |
|---|---|
| 纯协议实现 | 不经过 libpq C 库，直接拼/解析 PG 协议字节 |
| 零拷贝解码 | 结果直接解码成 Python 对象，不经过中间层 |
| 异步 I/O | 等 DB 时不阻塞，一个线程管上千连接 |
| Prepared statement 自动缓存 | 相同 SQL 第二次执行跳过解析 |

### aiosqlite（SQLite 异步）

```python
import asyncio
import aiosqlite

async def main():
    async with aiosqlite.connect("test.db") as db:
        await db.execute("CREATE TABLE IF NOT EXISTS t(id INTEGER, val TEXT)")
        await db.executemany("INSERT INTO t VALUES(?, ?)", [(i, f"v{i}") for i in range(100)])
        await db.commit()
        async with db.execute("SELECT * FROM t") as cursor:
            rows = await cursor.fetchall()
    # aiosqlite 内部用线程池跑 sqlite3（sqlite3 是同步的）

asyncio.run(main())
```

> **aiosqlite 本质**：SQLite 的 C API 是同步的，aiosqlite 在一个独立线程里跑 sqlite3，用 asyncio 的线程池桥接。所以它不是"真异步"，而是"不阻塞事件循环"。

### 用标准库实现异步 SQLite（零依赖）

不装 aiosqlite 也能在异步代码里用 SQLite——用 `asyncio.to_thread` 把同步调用丢到线程池：

```python
import asyncio
import sqlite3

async def async_query(db_path, sql, params=()):
    def _sync():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return rows
    return await asyncio.to_thread(_sync)

async def main():
    rows = await async_query("test.db", "SELECT * FROM users WHERE id=?", (42,))
    print(rows)

asyncio.run(main())
```

| 方式 | 依赖 | 适用 |
|---|---|---|
| `aiosqlite` | 需安装 | 生产，连接复用 |
| `asyncio.to_thread` | 标准库 | 教学/简单场景，每次新连接 |
| 自写异步池 | 标准库 | 生产，零依赖（见 `app.py`） |

### 异步连接池的注意事项

| 事项 | 说明 |
|---|---|
| **池大小 ≤ max_connections** | 异步不省 DB 连接数，只省线程数 |
| **不要在协程里长持连接** | `async with pool.acquire()` 块要短，否则和同步泄漏一样 |
| **asyncio.gather 的并发度** | 1000 个协程同时 acquire 可能超过池大小 → 用 semaphore 限流 |
| **混合同步/异步** | 同一进程里混用 psycopg2 和 asyncpg 要小心事件循环阻塞 |

#### 用 Semaphore 限流

```python
import asyncio

async def bounded_query(pool, sem, user_id):
    async with sem:  # 限制并发度
        async with pool.acquire() as conn:
            return await conn.fetchrow('SELECT * FROM users WHERE id=$1', user_id)

async def main():
    pool = await asyncpg.create_pool(..., max_size=20)
    sem = asyncio.Semaphore(20)  # 最多 20 个并发查询
    tasks = [bounded_query(pool, sem, i) for i in range(1000)]
    results = await asyncio.gather(*tasks)  # 1000 个协程，但最多 20 个同时查
```

> **为什么需要 Semaphore**：`asyncio.gather` 会同时启动 1000 个协程，如果每个都 `acquire` 连接，池大小 20 根本不够——后面的协程会排队等连接。用 Semaphore 把并发度限制在 20，就和池大小匹配了。

## Python 后端示例详解

本节逐文件解读 `backend_example/` 下的三个示例脚本，帮助新手把前面的理论和真实代码对应起来。

### app.py：连接池 + 路由 + 事务边界

`app.py` 用 Python 标准库实现了一个迷你 Web 服务，模拟 Flask/FastAPI 的路由风格，包含连接池、同步/异步查询、事务边界演示。

#### 文件结构总览

```
app.py
├── SQLiteConnectionPool   连接池类（核心）
├── init_db                初始化数据库
├── get_users_n_plus_1     N+1 查询（反面教材）
├── get_users_join         JOIN 解决 N+1
├── get_users_two_queries  两次查询解决 N+1
├── transfer               正确的事务转账
├── transfer_wrong         错误的跨连接转账
├── SyncHandler            HTTP 请求处理
├── run_sync_server        启动同步服务
├── async_get_users_join   异步查询
├── run_async_demo         异步演示
└── main                   入口
```

#### SQLiteConnectionPool 解读

```python
class SQLiteConnectionPool:
    def __init__(self, path, pool_size=10, init_sql=None):
        self.path = path
        self.pool_size = pool_size
        self._pool = queue.Queue(maxsize=pool_size)   # 用队列存连接
        self._all_conns = []                           # 所有连接（用于 close）
        self._checked_out = {}                         # 借出记录（用于泄漏检测）
        self._lock = threading.Lock()                  # 保护 _checked_out 的锁
        for _ in range(pool_size):
            conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            conn.row_factory = sqlite3.Row             # 让结果像字典一样访问
            conn.execute("PRAGMA journal_mode=WAL")    # WAL 模式
            conn.execute("PRAGMA foreign_keys=ON")     # 外键约束
            conn.execute("PRAGMA busy_timeout=5000")   # 锁等待 5 秒
            self._pool.put(conn)
            self._all_conns.append(conn)
```

| 设计点 | 代码 | 为什么这么写 |
|---|---|---|
| 用 `queue.Queue` 存连接 | `self._pool = queue.Queue(...)` | Queue 线程安全，`get/put` 自带阻塞 |
| `isolation_level=None` | `sqlite3.connect(..., isolation_level=None)` | 关掉 sqlite3 的自动事务，自己控制 BEGIN/COMMIT |
| `check_same_thread=False` | 同上 | 允许跨线程用连接（池化必需） |
| 记录借出栈 | `self._checked_out[id(conn)] = (time, stack)` | 泄漏检测的核武器 |
| `_all_conns` 单独存 | `self._all_conns.append(conn)` | close 时要关所有连接，不能只关池里的 |

#### `connection()` 和 `transaction()` 上下文管理器

```python
@contextmanager
def connection(self):
    conn = self.getconn()
    try:
        yield conn
    finally:
        self.putconn(conn)       # 无论正常/异常都归还

@contextmanager
def transaction(self):
    conn = self.getconn()
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")   # 正常结束提交
    except Exception:
        conn.execute("ROLLBACK") # 异常回滚
        raise
    finally:
        self.putconn(conn)       # 无论如何都归还
```

| 方法 | 事务 | 归还连接 | 用途 |
|---|---|---|---|
| `connection()` | 无（autocommit） | ✅ | 单条查询 |
| `transaction()` | 有（BEGIN/COMMIT） | ✅ | 多条需原子性的操作 |

> **新手注意**：`transaction()` 里 `BEGIN` 和 `COMMIT` 之间 `yield conn`，with 块里的代码用这个 `conn` 执行 SQL。如果 with 块抛异常，走 `except` 执行 `ROLLBACK`。这是"事务边界"在代码里的具体体现。

#### 路由和 HTTP 处理

```python
SyncHandler.routes = {
    "/users/n+1": lambda q: get_users_n_plus_1(),        # 反面教材
    "/users/join": lambda q: get_users_join(),           # JOIN 解决
    "/users/two-query": lambda q: get_users_two_queries(),# 两次查询解决
    "/pool/stats": lambda q: pool.stats(),               # 查池状态
    "/pool/leak-check": lambda q: {"leaks": pool.leak_report(silent=True)},# 查泄漏
}
```

| 路由 | 功能 | 对应章节 |
|---|---|---|
| `/users/n+1` | N+1 查询演示 | N+1 问题 |
| `/users/join` | JOIN 一次查询 | 方案1 |
| `/users/two-query` | 两次查询 + 组装 | 方案3 |
| `/pool/stats` | 查看池状态 | 连接池监控 |
| `/pool/leak-check` | 泄漏检测 | 连接泄漏 |

#### `transfer` vs `transfer_wrong` 对比

```python
# 正确：事务内转账
def transfer(from_id, to_id, amount):
    with pool.transaction() as conn:          # 一个事务
        from_balance = conn.execute(
            "SELECT balance FROM accounts WHERE id=?", (from_id,)).fetchone()[0]
        if from_balance < amount:
            raise ValueError(f"余额不足")
        conn.execute("UPDATE accounts SET balance=balance-? WHERE id=?", (amount, from_id))
        conn.execute("UPDATE accounts SET balance=balance+? WHERE id=?", (amount, to_id))
        # 两条 UPDATE 在同一事务，要么都成功要么都回滚

# 错误：跨连接，无事务
def transfer_wrong(from_id, to_id, amount):
    with pool.connection() as conn:           # 连接 A，autocommit
        conn.execute("UPDATE accounts SET balance=balance-? WHERE id=?", (amount, from_id))
    # A 扣款已提交！连接 A 归还
    raise RuntimeError("模拟失败")            # 崩溃
    with pool.connection() as conn:           # 连接 B
        conn.execute("UPDATE accounts SET balance=balance+? WHERE id=?", (amount, to_id))
    # B 加款永远执行不到 → 100 元消失
```

| 函数 | 事务 | 崩溃后 | 余额 |
|---|---|---|---|
| `transfer` | ✅ 一个事务 | 回滚，A 和 B 都不变 | 一致 |
| `transfer_wrong` | ❌ 两个 autocommit | A 扣了 B 没加 | **不一致** |

#### 异步部分解读

```python
async def async_get_users_join():
    def _query():
        with pool.connection() as conn:
            rows = conn.execute("""SELECT ... LEFT JOIN ...""").fetchall()
        return [...]
    return await asyncio.to_thread(_query)   # 把同步查询丢到线程池
```

> **设计思路**：SQLite 的 C API 是同步的，没法真异步。`app.py` 用 `asyncio.to_thread` 把同步查询丢到线程池，不阻塞事件循环。这是"零依赖异步"的实用模式。

### n_plus_1_demo.py：N+1 问题量化

这个脚本创建博客数据（用户 + 帖子），用四种方式查询，对比查询次数和耗时。

#### 四种查询方案

```python
def approach_n_plus_1():
    """模拟 ORM lazy loading：先查用户，再循环查每个用户的帖子"""
    users = counter.fetchall("SELECT id, name FROM users")       # 1 次
    for u in users:
        posts = counter.fetchall("SELECT ... FROM posts WHERE user_id=?", (u["id"],))  # N 次
    # 总计 1 + N 次

def approach_join():
    """用 LEFT JOIN 一次查出，应用层摊平"""
    rows = counter.fetchall("SELECT u.*, p.* FROM users u LEFT JOIN posts p ON ...")
    # 1 次，然后应用层把行摊平成对象树

def approach_two_queries():
    """两次查询 + 应用层组装（模拟 selectinload）"""
    users = counter.fetchall("SELECT id, name FROM users")       # 1 次
    posts = counter.fetchall("SELECT id, user_id, title FROM posts")  # 1 次
    # 应用层用 defaultdict 组装
    # 总计 2 次

def approach_batch_in():
    """分批 IN 查询"""
    users = counter.fetchall("SELECT id, name FROM users")       # 1 次
    for i in range(0, len(users), BATCH):
        batch = users[i:i + BATCH]
        ids = [u["id"] for u in batch]
        posts = counter.fetchall("SELECT ... WHERE user_id IN (...)", ids)  # ceil(N/BATCH) 次
    # 总计 1 + ceil(N/BATCH) 次
```

| 方案 | 查询数 | 代码 | 对应 ORM |
|---|---|---|---|
| `approach_n_plus_1` | 1 + N | 最少（ORM 默认） | lazy loading |
| `approach_join` | 1 | 中（要摊平） | `joinedload` |
| `approach_two_queries` | 2 | 中（要组装） | `selectinload` |
| `approach_batch_in` | 1 + N/20 | 多（分批逻辑） | 手动分批 |

#### QueryCounter：统计查询次数

```python
class QueryCounter:
    def __init__(self, conn):
        self.conn = conn
        self.count = 0

    def execute(self, sql, params=()):
        self.count += 1                    # 每次执行 +1
        return self.conn.execute(sql, params)
```

> **作用**：包装 sqlite3 连接，每次执行 SQL 都计数。这样就能精确知道每种方案发了多少条 SQL，而不只是测耗时。这是量化 N+1 问题的关键工具。

#### `sweep_user_count`：展示线性增长

```python
def sweep_user_count():
    for n in [10, 50, 100, 200, 500]:
        NUM_USERS = n
        setup_db_silent()
        r1 = approach_n_plus_1()
        r2 = approach_join()
        print(f"{n} 用户: N+1={r1['queries']}次, JOIN={r2['queries']}次")
```

```
预期输出：
  用户数   N+1 查询数   JOIN 查询数
      10          11            1
      50          51            1
     100         101            1
     200         201            1
     500         501            1
```

> **直观结论**：N+1 随用户数线性增长，JOIN 永远是 1。这就是为什么 N+1 在数据量大时致命。

### pool_demo.py：连接池性能对比

这个脚本对比"有池 vs 无池"、扫描不同池大小的性能、演示连接泄漏和池耗尽。

#### `compare_pool_vs_no_pool`：有池 vs 无池

```python
def no_pool_worker(ops_per_thread):
    for _ in range(ops_per_thread):
        conn = sqlite3.connect(DB_PATH)          # 每次新建连接
        conn.execute("PRAGMA journal_mode=WAL")
        row = conn.execute("SELECT ...").fetchone()
        conn.close()                             # 每次关闭

def pool_worker(pool, ops_per_thread):
    for _ in range(ops_per_thread):
        with pool.connection() as conn:          # 从池借
            row = conn.execute("SELECT ...").fetchone()
        # with 结束自动归还
```

| 方案 | 每次操作 | 开销 |
|---|---|---|
| `no_pool_worker` | connect + PRAGMA + query + close | 高（反复建连） |
| `pool_worker` | 借连接 + query + 归还 | 低（连接复用） |

#### `sweep_pool_size`：池大小扫描

```python
def sweep_pool_size():
    sizes = [1, 2, 4, 8, 10, 15, 20, 30, 50]
    for size in sizes:
        pool = SimplePool(DB_PATH, size=size)
        # 20 线程 × 25 操作 = 500 操作
        run_threaded(pool_worker, NUM_THREADS, pool, ops_per_thread)
        # 记录吞吐量
```

```
预期输出：
  池大小   耗时(ms)   吞吐(op/s)
       1       高        低    ← 太小，线程排队等连接
       2       ↓         ↑
       4       ↓         ↑
       8       ↓         ↑
      10      最低      最高   ← 拐点（≈ 瓶颈资源数）
      15       ↑         ↓    ← 太大，竞争开销
      20       ↑         ↓
      50      更高       ↓    ← 更大反而更慢
```

> **拐点在哪**：池大小的拐点通常在"瓶颈资源数"附近。对 SQLite 是磁盘 I/O 并发度，对 PostgreSQL 是 CPU 核数 × 2。太小则排队，太大则竞争，中间最优。

#### `demonstrate_leak`：泄漏演示

```python
def demonstrate_leak():
    pool = SimplePool(DB_PATH, size=5)
    # 正常使用
    for i in range(3):
        with pool.connection() as conn:          # 借了就还
            conn.execute("SELECT ...")
    print(pool.stats())  # available=5, checked_out=0

    # 制造泄漏
    leaked = []
    for i in range(3):
        conn = pool.getconn()                    # 借了不还
        leaked.append(conn)
        conn.execute("SELECT ...")
    print(pool.stats())  # available=2, checked_out=3

    # 泄漏检测
    leaks = pool.leak_report(threshold=0)        # 发现 3 个泄漏
    for cid, age, stack in leaks:
        print(f"连接 {cid}, 借出 {age:.3f}s")
        print(f"借出位置: {stack[-2].strip()}")  # 打印谁借的
```

| 步骤 | 可用 | 借出 | 说明 |
|---|---|---|---|
| 初始 | 5 | 0 | 池满 |
| 3 次正常使用 | 5 | 0 | 借了就还 |
| 借 1 不还 | 4 | 1 | 泄漏开始 |
| 借 2 不还 | 3 | 2 | |
| 借 3 不还 | 2 | 3 | `leak_report` 发现 3 个 |

#### `demonstrate_pool_exhaustion`：池耗尽

```python
def demonstrate_pool_exhaustion():
    pool = SimplePool(DB_PATH, size=3)
    conn1 = pool.getconn()
    conn2 = pool.getconn()
    conn3 = pool.getconn()           # 池空了
    try:
        conn4 = pool.getconn_with_timeout(timeout=2)  # 等 2 秒
    except queue.Empty:
        print("超时！池已耗尽")       # 快速失败
```

> **生产启示**：`getconn` 必须设超时。无限等待会让整个服务卡死；设 5 秒超时，至少能快速失败、记录日志、返回 503，让上游重试。

## 连接池性能对比

### 有池 vs 无池的实测数据

用 `pool_demo.py` 的 `compare_pool_vs_no_pool()` 在典型配置下（20 线程 × 25 操作 = 500 次查询，SQLite WAL 模式）跑出的代表性数据：

| 方案 | 耗时(ms) | 吞吐(op/s) | 加速比 |
|---|---|---|---|
| 无连接池 | ~800 | ~625 | 1.0x |
| 有连接池（10） | ~200 | ~2500 | ~4x |

> **注意**：SQLite 是嵌入式，"建连"只是打开文件，开销比 PG 的 TCP 连接小得多。如果是 PostgreSQL，无池的代价会高 10-100 倍，连接池的收益更显著。

#### PostgreSQL 下的对比（估算）

| 方案 | 单次建连 | 500 次查询总建连 | 查询执行 | 总耗时 |
|---|---|---|---|---|
| 无池（局域网） | 3ms | 1500ms | 500ms | ~2000ms |
| 有池（20） | 0ms（复用） | 60ms（一次性） | 500ms | ~560ms |
| 无池（跨城） | 30ms | 15000ms | 500ms | ~15500ms |
| 有池（20，跨城） | 0ms | 600ms（一次性） | 500ms | ~1100ms |

> **跨网场景**：连接池收益巨大（14 倍）。这也是为什么"数据库和应用同机房"是黄金法则——RTT 小，建连快，每条 SQL 也快。

### 不同池大小的性能曲线

`sweep_pool_size()` 扫描池大小 1-50 的典型结果：

```
吞吐量(op/s)
  │
  │                    ┌─────┐  ← 拐点（最优）
  │                  ╱       ╲
  │                ╱           ╲
  │              ╱               ╲
  │            ╱                   ╲  ← 太大反而下降（竞争开销）
  │          ╱
  │        ╱
  │      ╱
  │    ╱
  │  ╱  ← 太小（排队等连接）
  │╱
  └────────────────────────────────── 池大小
     1   2   4   8  10  15  20  30  50
```

| 池大小 | 吞吐(op/s) | 瓶颈 | 说明 |
|---|---|---|---|
| 1 | ~300 | 排队 | 20 线程抢 1 个连接，串行 |
| 2 | ~600 | 排队 | 仍有排队 |
| 4 | ~1200 | 排队缓解 | 接近并发度 |
| 8 | ~2200 | I/O | 接近磁盘并发度 |
| **10** | **~2500** | **I/O** | **拐点，最优** |
| 15 | ~2400 | 锁竞争 | 略降 |
| 20 | ~2200 | 锁竞争 | 下降 |
| 50 | ~1800 | 严重竞争 | 连接太多，调度开销大 |

#### 拐点为什么在 CPU 核数 / I/O 并发度附近

```
池大小 < 瓶颈资源数：
  连接不够，线程排队等连接 → 连接是瓶颈 → 加大池大小提升吞吐

池大小 ≈ 瓶颈资源数：
  连接数刚好匹配资源数 → 资源充分利用 → 吞吐最高

池大小 > 瓶颈资源数：
  连接太多，但资源（CPU/IO）就那么多 → 多余连接互相竞争
  → 上下文切换、锁竞争开销增大 → 吞吐下降
```

| 数据库类型 | 拐点位置 | 原因 |
|---|---|---|
| SQLite（WAL） | 磁盘 I/O 并发度（~4-10） | 写串行，读并发受磁盘限制 |
| PostgreSQL | CPU 核数 × 2 | PG 一个连接一个进程，CPU 是瓶颈 |
| MySQL | CPU 核数 × 2 ~ 50 | 线程模型，可稍多 |

> **调优口诀**：从 `pool_size = CPU 核数` 开始测，翻倍和减半都试一下，取吞吐最高点。不要盲目开大。

### 连接数与内存的关系

```
PostgreSQL，每个 backend 5-10MB：

  池大小 20  → 20 个 backend → 100-200MB 内存（仅连接）
  池大小 100 → 100 个 backend → 500MB-1GB 内存
  池大小 500 → 500 个 backend → 2.5-5GB 内存

  加上 shared_buffers（默认 128MB）、work_mem（每排序 4MB）...
  池大小 500 的 PG 可能吃掉 8GB+ 内存
```

| 池大小 | PG 内存（仅连接） | 含查询缓存 | 评价 |
|---|---|---|---|
| 20 | 100-200MB | ~500MB | 合理 |
| 50 | 250-500MB | ~1GB | 中型应用 |
| 100 | 500MB-1GB | ~2GB | 大型，需调 shared_buffers |
| 500 | 2.5-5GB | ~8GB+ | 不推荐，用 PgBouncer |

> **结论**：直接开 500 个 PG 连接不如"20 个 PG 连接 + PgBouncer 接 5000 个客户端"。前者吃 8GB 内存，后者吃 200MB 内存，效果一样。

## 架构总览

```
                         Web 服务进程
┌──────────────────────────────────────────────────┐
│  HTTP 请求 ─→ 路由 ─→ 业务逻辑                   │
│                    │                              │
│                    ├─ 同步路径：                   │
│                    │   thread → pool.getconn()    │
│                    │          → SQL → putconn()   │
│                    │                              │
│                    └─ 异步路径：                   │
│                        coroutine → pool.acquire() │
│                                 → await SQL       │
└──────────────────────┬───────────────────────────┘
                       │
              ┌────────┴────────┐
              │  连接池          │
              │  min=5 max=20    │
              └────────┬────────┘
                       │
              ┌────────┴────────┐
              │  PgBouncer       │  ← 可选，进一步池化
              │  pool_mode=txn   │
              └────────┬────────┘
                       │
              ┌────────┴────────┐
              │  PostgreSQL      │
              │  max_conn=100    │
              └─────────────────┘
```

| 层 | 连接数 | 作用 |
|---|---|---|
| 应用连接池 | 20 | 限制应用侧并发，复用连接 |
| PgBouncer | 1000 client → 20 server | 汇聚多应用，transaction 模式 |
| PostgreSQL | 20 | 实际执行 SQL 的 backend |

### 连接数在各层的收敛

```
1000 个并发用户请求
        │
        ▼
应用层（4 个实例，每实例池 20）= 80 条到 PgBouncer 的连接
        │
        ▼
PgBouncer（transaction 模式，default_pool_size=20）= 20 条到 PG 的连接
        │
        ▼
PostgreSQL：20 个 backend 进程，100MB 内存
```

| 层 | 连接数 | 内存 | 说明 |
|---|---|---|---|
| 用户 | 1000 | — | 并发请求 |
| 应用实例 ×4 | 80 | 80 × 线程开销 | 每实例池 20 |
| PgBouncer | 20（到 PG） | ~50MB | transaction 模式收敛 |
| PostgreSQL | 20 backend | ~200MB | 实际执行 |

> **这就是连接池架构的价值**：1000 个并发请求最终只让 PG 维护 20 个进程，内存可控，性能不降。

## 与真实后端实践对比

真实 Web 框架（Django/Flask/FastAPI）都内置了数据库连接池管理。理解它们的做法，有助于把本章的原理用到生产。

### Django 的数据库连接

Django 默认每个请求开始时从池里借连接，请求结束时归还。Django 的"池"实际上是**每线程一个连接**，靠 `CONN_MAX_AGE` 控制连接复用。

```python
# settings.py
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'app',
        'USER': 'postgres',
        'PASSWORD': 'secret',
        'HOST': 'localhost',
        'CONN_MAX_AGE': 60,   # 连接复用 60 秒（默认 0 = 每请求新建）
        'CONN_HEALTH_CHECKS': True,  # Django 4.1+，借出前 ping
        'OPTIONS': {
            'connect_timeout': 5,
        },
    }
}
```

| 参数 | 含义 | 建议 |
|---|---|---|
| `CONN_MAX_AGE=0` | 每请求新建连接（默认） | ❌ 性能差 |
| `CONN_MAX_AGE=60` | 连接复用 60 秒 | ✅ 生产推荐 |
| `CONN_MAX_AGE=None` | 永不关闭 | ❌ 会累积死连接 |
| `CONN_HEALTH_CHECKS=True` | 借出前 ping | ✅ 生产必开 |

```python
# Django 视图里不用手动管连接
def user_list(request):
    users = User.objects.prefetch_related('posts').all()  # 自动借/还连接
    return render(request, 'list.html', {'users': users})
```

> **Django 的池模型**：不是真正的"池"，而是 thread-local 连接。每个线程一个连接，`CONN_MAX_AGE` 控制连接多久后重建。配合 gunicorn 的 worker（每 worker 一个进程，每进程 N 线程），总连接数 = worker 数 × 线程数。

### Flask + SQLAlchemy 的数据库连接

Flask 本身不带数据库层，通常配 SQLAlchemy：

```python
from flask import Flask
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'postgresql://postgres:secret@localhost/app'
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_size': 20,
    'max_overflow': 10,
    'pool_timeout': 30,
    'pool_recycle': 3600,
    'pool_pre_ping': True,
}
db = SQLAlchemy(app)

@app.route('/users')
def users():
    users = User.query.options(db.selectinload(User.posts)).all()
    return jsonify([u.to_dict() for u in users])
    # 请求结束自动归还连接
```

| Flask-SQLAlchemy 做法 | 对应本章概念 |
|---|---|
| `pool_size=20` | 应用连接池大小 |
| `pool_pre_ping=True` | 借出前检测存活 |
| `selectinload` | 解决 N+1（一对多） |
| 请求结束自动归还 | 事务边界 = 请求边界 |

### FastAPI + asyncpg 的数据库连接

FastAPI 是异步框架，配 asyncpg 性能最佳：

```python
from fastapi import FastAPI
import asyncpg

app = FastAPI()
pool = None

@app.on_event("startup")
async def startup():
    global pool
    pool = await asyncpg.create_pool(
        'postgresql://postgres:secret@localhost/app',
        min_size=5, max_size=20,
    )

@app.on_event("shutdown")
async def shutdown():
    await pool.close()

@app.get("/users/{user_id}")
async def get_user(user_id: int):
    async with pool.acquire() as conn:
        return await conn.fetchrow('SELECT * FROM users WHERE id=$1', user_id)
    # async with 结束自动归还
```

| FastAPI 做法 | 对应本章概念 |
|---|---|
| `create_pool` 在 startup | 启动时建池 |
| `async with pool.acquire()` | 借/还连接（异步版 with） |
| `await conn.fetchrow` | 异步查询，不阻塞事件循环 |
| `pool.close()` 在 shutdown | 优雅关闭 |

### 三框架对比

| 特性 | Django | Flask + SQLAlchemy | FastAPI + asyncpg |
|---|---|---|---|
| 异步 | 部分（3.x+） | 部分（2.x+） | ✅ 原生 |
| 连接池 | thread-local | SQLAlchemy QueuePool | asyncpg Pool |
| N+1 解决 | `prefetch_related` | `selectinload` | 手写 SQL |
| 事务边界 | 请求 = 事务 | 请求 = 事务 | 手动 `async with` |
| ORM | ✅ 内置 | ✅ SQLAlchemy | ❌（通常裸 SQL） |
| 性能 | 中 | 中 | 高 |
| 适合 | 传统 Web | 中小型 | 高并发 API |

#### 各框架的 N+1 解法对照

```python
# Django
users = User.objects.prefetch_related('posts').all()    # selectinload 等价
users = User.objects.select_related('profile').all()    # joinedload 等价（多对一）

# Flask + SQLAlchemy
users = User.query.options(db.selectinload(User.posts)).all()  # 一对多
users = User.query.options(db.joinedload(User.profile)).all()  # 多对一

# FastAPI + asyncpg（无 ORM，手写）
users = await conn.fetch('SELECT * FROM users')
posts = await conn.fetch('SELECT * FROM posts WHERE user_id = ANY($1)', user_ids)
# 应用层组装
```

> **新手建议**：不管用哪个框架，记住三件事——(1) 用连接池别每请求新建；(2) 一对多用 `prefetch_related`/`selectinload`；(3) 事务别跨请求。这三条能避免 90% 的数据库性能问题。

### 生产配置清单

| 检查项 | Django | Flask | FastAPI |
|---|---|---|---|
| 连接复用 | `CONN_MAX_AGE=60` | `pool_recycle=3600` | `max_inactive_connection_lifetime=300` |
| 存活检测 | `CONN_HEALTH_CHECKS=True` | `pool_pre_ping=True` | asyncpg 自动 |
| 池超时 | — | `pool_timeout=30` | `command_timeout=30` |
| N+1 防护 | `prefetch_related` | `selectinload` | 手写 JOIN |
| 事务管理 | `@transaction.atomic` | `with db.session.begin()` | `async with conn.transaction()` |
| 监控 | django.db.connection.queries | `engine.pool.status()` | `pool.get_size()` |

## 与阶段1的对照

| 概念 | 阶段1（miniDB） | 阶段2（真实数据库） |
|---|---|---|
| 连接 | 单进程内嵌 | TCP 连接 + 后端进程 |
| 并发 | 单线程串行 | 多进程/多协程 |
| 连接池 | 无需 | 必需（PgBouncer / 应用池） |
| 事务边界 | 函数调用 | 跨网络，需显式 BEGIN/COMMIT |
| N+1 | 不适用（无 ORM） | ORM 常见陷阱 |
| 异步 | 不适用 | asyncpg / aiosqlite |

## 文件清单

| 文件 | 内容 |
|---|---|
| `backend_example/app.py` | 后端示例：连接池 + 路由 + 同步/异步 + 事务边界 |
| `backend_example/n_plus_1_demo.py` | N+1 问题演示与解决 |
| `backend_example/pool_demo.py` | 连接池对比、池大小扫描、泄漏演示 |

### 如何运行示例

```bash
# 进入示例目录
cd phase2/05-connection-pool/backend_example

# 运行 app.py（演示查询对比、事务边界、异步）
python app.py

# 启动 HTTP 服务（另起一个终端测试路由）
python app.py server
# 然后用浏览器或 curl 访问：
#   curl http://localhost:8000/users/join
#   curl http://localhost:8000/pool/stats

# 运行 N+1 对比
python n_plus_1_demo.py

# 运行连接池对比（有池 vs 无池、池大小扫描、泄漏演示）
python pool_demo.py
```

| 命令 | 看什么 |
|---|---|
| `python app.py` | 三种查询的耗时对比、事务边界演示、异步演示 |
| `python app.py server` | 启动 HTTP 服务，用 curl 测路由 |
| `python n_plus_1_demo.py` | N+1 随用户数线性增长的表格 |
| `python pool_demo.py` | 有池 vs 无池加速比、池大小拐点、泄漏检测 |

## 习题

1. **连接开销测量**：写脚本，对比"每次新建连接执行 1 条查询"与"复用连接执行 1000 条查询"的总耗时，计算连接建立占比。
2. **池大小调优**：用 `pool_demo.py` 的 `sweep_pool_size()` 扫描池大小 1-50，画出吞吐量曲线，找到拐点。解释为什么拐点通常在 CPU 核数附近。
3. **N+1 量化**：在 `n_plus_1_demo.py` 中，把用户数从 10 扩到 1000，记录三种方案的查询数和耗时，画出 N+1 的线性增长曲线。
4. **泄漏复现**：修改 `app.py` 的某个端点，故意不归还连接，用 `pool.stats()` 观察可用连接数递减，用 `pool.leak_report()` 打印泄漏栈。
5. **事务边界**：写一个转账函数 `transfer(from_id, to_id, amount)`，分别用"每条 SQL 独立连接"和"事务内同连接"两种实现，制造中途异常，观察余额是否一致。
6. **异步对比**：用 `app.py` 的异步端点并发请求 100 次，对比同步版本同样 100 次的耗时。解释为什么 I/O 密集场景异步快，CPU 密集场景差异不大。
7. **PgBouncer 模式**：描述一个必须用 session pooling 而不能用 transaction pooling 的场景（提示：临时表），并说明如何在同一个 PgBouncer 实例里同时支持两种模式。
8. **selectinload vs joinedload**：构造一对多关系（1 用户 100 帖子），分别用两种 eager loading 查 10 个用户，对比返回行数和耗时，验证"一对多用 selectinload"的结论。

### 习题提示

| 题号 | 提示 |
|---|---|
| 1 | 用 `time.perf_counter()` 测时间；新建连接用 `sqlite3.connect()`，复用则只 connect 一次 |
| 2 | 拐点在"瓶颈资源数"附近，SQLite 是磁盘 I/O 并发度，PG 是 CPU 核数 × 2 |
| 3 | 改 `NUM_USERS`，跑 `sweep_user_count()`，看 N+1 列线性增长、JOIN 列恒为 1 |
| 4 | 在路由函数里 `conn = pool.getconn()` 后直接 `return`，不 `putconn` |
| 5 | 参考 `transfer` 和 `transfer_wrong`，用 `try/except` 制造异常，查余额对比 |
| 6 | 用 `asyncio.gather` 并发 100 个 `async_get_users_join()`，对比 100 次串行同步 |
| 7 | 临时表在 COMMIT 后消失（transaction 模式换连接）；用虚拟数据库名分模式 |
| 8 | 10 用户 × 100 帖子：joinedload 返回 1000 行（User 重复），selectinload 返回 10+1000 行但不重复 |
