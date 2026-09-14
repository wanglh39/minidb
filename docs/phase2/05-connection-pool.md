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

#### Statement Pooling

```
每条 SQL 执行完立即归还连接
BEGIN ... COMMIT 会被拆到不同 server 连接 → 事务语义破坏！
```

- 仅适用于无事务的简单查询工具（如 psql 自动补全、BI 工具的元数据查询）
- **不要用于应用代码**

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

### PgBouncer 的限制

| 不能用 | 原因 |
|---|---|
| 临时表跨事务 | transaction 模式 COMMIT 后换连接，临时表随连接销毁 |
| `SET` 持久化 | 同上，会话变量随连接归还而丢失 |
| `LISTEN/NOTIFY` | 通知绑定在具体连接上 |
| 两阶段提交 | `PREPARE TRANSACTION` 需要连接保持 |
| SQL 级 PREPARE | pgbouncer 自己管理 prepared statement，需开启 `max_prepared_statements` |

> **解决方案**：需要会话状态的操作，用一条**直连 PG**的连接（绕过 PgBouncer），或用 `pgbouncer` 的 `pool_mode = session` 专门给这类连接。

## Python 连接池方案

### 方案对比

| 方案 | 库 | 异步 | 适用 | 池化方式 |
|---|---|---|---|---|
| psycopg2 + 手写池 | psycopg2 | 否 | 同步传统应用 | `ThreadedConnectionPool` 或自写 |
| psycopg3 | psycopg | 可选 | 新项目，同步异步都支持 | 内置 `ConnectionPool` |
| asyncpg | asyncpg | 是 | 高并发异步应用 | 内置 `Pool` |
| SQLAlchemy | sqlalchemy | 可选 | ORM 应用 | `QueuePool` / `NullPool` |
| SQLite 连接复用 | sqlite3 | 否 | 嵌入式 | 单连接 + check_same_thread=False 或自写池 |

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

## ORM 的 N+1 查询问题

### 什么是 N+1

```
查询 N 个用户，再查每个用户的文章 → 1 次查用户 + N 次查文章 = N+1 次查询

本应：1 次 JOIN 查询 = 1 次查询
```

这是 ORM 最经典的性能陷阱。ORM 默认 lazy loading 关联对象，导致循环里隐式触发查询。

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

## 事务边界管理

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

### 事务边界的反模式

| 反模式 | 问题 | 后果 |
|---|---|---|
| 事务跨 HTTP 请求 | 连接持有时间 = 用户思考时间 | 连接被占，池耗尽 |
| 事务里调外部 API | API 超时 → 连接一直被占 | 锁持有过久，死锁 |
| 忘记 COMMIT/ROLLBACK | 连接带着未决事务归还 | 下个使用者读到脏数据 |
| 嵌套 BEGIN | SQLite 报错 / PG 创建 savepoint | 行为不符预期 |
| 事务里做大量计算 | 持有锁时间长 | 阻塞其他事务 |

> **黄金法则**：事务要**短**。打开事务 → 执行 SQL → 立即提交/回滚。事务里不要做 I/O（除数据库外）、不要等用户输入、不要 sleep。

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

### 防泄漏的最佳实践

| 实践 | 说明 |
|---|---|
| **永远用 try/finally** | `getconn` 后紧跟 `try`，`finally` 里 `putconn` |
| **用上下文管理器** | `with pool.connection() as conn:` 自动归还 |
| **用框架的声明式事务** | `@transactional` 装饰器自动管理 |
| **设置池超时** | `pool_timeout=5`，宁可快速失败也不要无限等待 |
| **监控池水位** | 告警阈值：活跃连接 > 80% 池大小 |
| **连接存活检测** | `pool_pre_ping=True`，借出前验证连接活着 |

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

## 习题

1. **连接开销测量**：写脚本，对比"每次新建连接执行 1 条查询"与"复用连接执行 1000 条查询"的总耗时，计算连接建立占比。
2. **池大小调优**：用 `pool_demo.py` 的 `sweep_pool_size()` 扫描池大小 1-50，画出吞吐量曲线，找到拐点。解释为什么拐点通常在 CPU 核数附近。
3. **N+1 量化**：在 `n_plus_1_demo.py` 中，把用户数从 10 扩到 1000，记录三种方案的查询数和耗时，画出 N+1 的线性增长曲线。
4. **泄漏复现**：修改 `app.py` 的某个端点，故意不归还连接，用 `pool.stats()` 观察可用连接数递减，用 `pool.leak_report()` 打印泄漏栈。
5. **事务边界**：写一个转账函数 `transfer(from_id, to_id, amount)`，分别用"每条 SQL 独立连接"和"事务内同连接"两种实现，制造中途异常，观察余额是否一致。
6. **异步对比**：用 `app.py` 的异步端点并发请求 100 次，对比同步版本同样 100 次的耗时。解释为什么 I/O 密集场景异步快，CPU 密集场景差异不大。
7. **PgBouncer 模式**：描述一个必须用 session pooling 而不能用 transaction pooling 的场景（提示：临时表），并说明如何在同一个 PgBouncer 实例里同时支持两种模式。
8. **selectinload vs joinedload**：构造一对多关系（1 用户 100 帖子），分别用两种 eager loading 查 10 个用户，对比返回行数和耗时，验证"一对多用 selectinload"的结论。