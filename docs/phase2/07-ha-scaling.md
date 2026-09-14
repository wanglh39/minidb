# 章7：高可用与扩展

> 单机数据库是系统的单点故障（SPOF）。本章覆盖 PostgreSQL 高可用（流复制、逻辑复制、Patroni 自动故障转移、PgBouncer 连接池）与扩展（读写分离、Citus 水平分片、垂直 vs 水平扩展）的完整工程实践。

## 高可用核心概念

### RPO 与 RTO

```
故障发生                    数据恢复                业务恢复
  │                            │                       │
  ▼                            ▼                       ▼
───┬───────────────────────────┬───────────────────────┬───► 时间
  │      数据丢失窗口            │   系统恢复窗口          │
  │◄──── RPO ────►             │◄───── RTO ───────────►│
  │                            │                       │
  │  Recovery Point Objective  │  Recovery Time Objective
  │  容许丢失的数据量           │  容许停机的时间
```

| 指标 | 含义 | 单位 | 典型要求 | 实现方式 |
|---|---|---|---|---|
| **RPO** | 恢复点目标，容许丢失的最大数据量 | 时间或字节 | 0（零丢失）| 同步复制 |
| **RTO** | 恢复时间目标，容许的最大停机时间 | 时间 | < 30s | 自动故障转移 |
| **MTBF** | 平均无故障时间 | 时间 | 越大越好 | 冗余硬件 |
| **MTTR** | 平均修复时间 | 时间 | 越小越好 | 自动化运维 |

> **关键公式**：系统可用性 = MTBF / (MTBF + MTTR)。要达到 99.99%（年停机 52.6 分钟），需要 MTBF >> MTTR。

### 故障转移模式

```
┌──────────── 主动-被动（Active-Passive）────────────┐
│                                                    │
│   ┌──────────┐    WAL 流    ┌──────────┐          │
│   │  Primary │ ───────────► │  Standby │          │
│   │  (读写)  │              │  (只读)  │          │
│   └──────────┘              └──────────┘          │
│        ▲                          │                │
│        │     故障时提升            │                │
│        └──────────────────────────┘                │
│                                                    │
└────────────────────────────────────────────────────┘

┌──────────── 主动-主动（Active-Active）────────────┐
│                                                    │
│   ┌──────────┐  双向逻辑复制  ┌──────────┐        │
│   │  Node A  │ ◄───────────► │  Node B  │        │
│   │  (读写)  │               │  (读写)  │        │
│   └──────────┘               └──────────┘        │
│                                                    │
│   冲突解决复杂，通常按地域分片写入                  │
│                                                    │
└────────────────────────────────────────────────────┘
```

### 脑裂问题

```
正常状态：                     脑裂状态：
┌────────┐    ┌────────┐      ┌────────┐    ┌────────┐
│Primary │◄──►│Standby │      │Primary │ ✗  │Standby │
└────────┘    └────────┘      └────────┘    └────────┘
     ▲                            ▲              ▲
     │                            │              │
  网络正常                     网络分区
  1 个主库                     2 个主库（数据冲突！）
```

> **脑裂（Split-Brain）**：网络分区导致备库误以为主库宕机而提升自己，出现两个主库。解决方案：**fencing**（STONITH 射杀旧主）、**多数派投票**（Patroni + etcd）、**见证节点**（witness）。

## PostgreSQL 流复制（物理复制）

### 架构

```
                    ┌─────────────────────────────────────┐
                    │            Primary (主库)            │
                    │  ┌─────────────┐  ┌──────────────┐  │
                    │  │ WAL Buffer  │─►│  WAL Segment │  │
                    │  └─────────────┘  └──────┬───────┘  │
                    │                          │          │
                    │  ┌─────────────┐         │          │
                    │  │ walsender    │◄────────┘          │
                    │  │ (1 per replica)│                  │
                    │  └──────┬──────┘                     │
                    └─────────┼────────────────────────────┘
                              │ WAL 流（TCP）
                              ▼
                    ┌─────────────────────────────────────┐
                    │           Standby (备库)             │
                    │  ┌─────────────┐  ┌──────────────┐  │
                    │  │ walreceiver │─►│ WAL Redo     │  │
                    │  └─────────────┘  └──────┬───────┘  │
                    │                          │          │
                    │  ┌─────────────┐         │          │
                    │  │ Buffer Pool │◄────────┘          │
                    │  │ (只读查询)  │                    │
                    │  └─────────────┘                    │
                    └─────────────────────────────────────┘
```

### 同步 vs 异步复制

```
异步复制（默认）：                 同步复制：
  Primary  ──commit──►            Primary  ──commit──┐
     │                              │                 │
     │ 立即返回成功                 │ 等待             │
     ▼                              ▼                 ▼
  (WAL 异步发送)                 Standby 收到 WAL   返回成功

  RPO > 0（可能丢数据）            RPO = 0（零丢失）
  RTO 小，性能好                  RTO 小，性能略差
```

| 模式 | `synchronous_commit` | RPO | 性能 | 适用场景 |
|---|---|---|---|---|
| 异步 | `off` 或 `local` | > 0 | 最好 | 读多写少、容忍少量丢失 |
| 同步 | `on` | 0 | 略差 | 金融交易、强一致 |
| 远程写 | `remote_write` | 0 | 中等 | 备库已写到 OS cache |
| 远程应用 | `remote_apply` | 0 | 最差 | 备库已 redo 应用 |

### 主库配置

```ini
# postgresql.conf (Primary)
wal_level = replica                    # minimal / replica / logical
max_wal_senders = 10                   # walsender 进程数
wal_keep_size = 10240                  # 保留 WAL（MB），防止备库断连后追不上
hot_standby = on                       # 备库允许只读查询（主库也开，便于切换）

# 同步复制（可选）
synchronous_standby_names = 'standby1' # FIRST 1 (standby1) 或 ANY 1 (standby1)
synchronous_commit = on                # on / remote_write / remote_apply

# 检查点相关
checkpoint_timeout = 5min
max_wal_size = 1GB
```

```sql
-- 创建复制用户
CREATE ROLE replicator WITH REPLICATION LOGIN PASSWORD 'repl_pass';

-- pg_hba.conf 允许备库连接
-- host replication replicator 192.168.1.0/24 md5
```

### 备库配置

```bash
# 1. 用 pg_basebackup 做基础备份
pg_basebackup -h primary_host -U replicator -D /var/lib/postgresql/standby \
              -Fp -Xs -P -R

# -Fp  plain 格式
# -Xs  stream 模式传 WAL
# -P   显示进度
# -R   写 standby.signal 和 primary_conninfo
```

```ini
# postgresql.conf (Standby)
hot_standby = on
hot_standby_feedback = on             # 备库的死锁信息反馈给主库
max_standby_streaming_delay = 30s     # 备库查询与 WAL 应用的冲突延迟

# primary_conninfo（也可写在 postgresql.auto.conf）
primary_conninfo = 'host=primary_host port=5432 user=replicator password=repl_pass'
```

```
# standby.signal 文件存在 → 进入备库模式
# PostgreSQL 12+ 用信号文件替代旧的 recovery.conf
```

### 复制状态检查

```sql
-- 在主库执行：查看所有备库的复制状态
SELECT
    application_name,
    client_addr,
    state,                              -- streaming / catchup
    sync_state,                         -- sync / async / potential
    sent_lsn,                           -- 已发送的 WAL 位置
    write_lsn,                          -- 备库已写的 WAL 位置
    flush_lsn,                          -- 备库已刷盘的 WAL 位置
    replay_lsn,                         -- 备库已应用的 WAL 位置
    pg_wal_lsn_diff(sent_lsn, replay_lsn) AS lag_bytes
FROM pg_stat_replication;

-- 在备库执行：查看自身状态
SELECT
    pg_is_in_recovery(),                -- true
    pg_last_wal_receive_lsn(),          -- 收到的最后 WAL
    pg_last_wal_replay_lsn(),           -- 应用的最后 WAL
    pg_last_xact_replay_timestamp();    -- 最后重放的事务时间

-- 计算复制延迟（秒）
SELECT
    now() - pg_last_xact_replay_timestamp() AS replication_lag
FROM pg_stat_replication;
```

## 逻辑复制（发布/订阅）

### 物理复制 vs 逻辑复制

```
物理复制：                        逻辑复制：
┌──────────┐                     ┌──────────┐
│ Primary  │                     │ Primary  │
│ ┌──────┐ │                     │ ┌──────┐ │
│ │ WAL  │ │  字节流复制         │ │ WAL  │ │
│ │(字节)│ │ ──────────────►    │ │(逻辑)│ │ 解码
│ └──────┘ │                     │ └──────┘ │
└──────────┘                     └────┬─────┘
                                      │ INSERT/UPDATE/DELETE
                                      ▼
                                 ┌──────────┐
                                 │Subscriber│
                                 │ ┌──────┐ │
                                 │ │ Table │ │
                                 │ └──────┘ │
                                 └──────────┘

  整个实例复制                    指定表复制
  备库不可写                      订阅端可写
  版本必须一致                    跨版本可行
  无法跨平台                      可跨平台
```

| 特性 | 物理复制 | 逻辑复制 |
|---|---|---|
| **粒度** | 整个实例 | 表级别 |
| **备库可写** | 否（只读） | 是 |
| **版本要求** | 主备一致 | 可不同版本 |
| **平台要求** | 架构一致 | 可跨平台 |
| **DDL 复制** | 是 | 否（需手动同步） |
| **初始数据** | pg_basebackup | COPY 自动同步 |
| **典型用途** | HA 故障转移 | 数据集成、ETL |

### 发布与订阅

```sql
-- ════════ 在主库（发布端）════════

-- 创建发布：发布 users 表的所有变更
CREATE PUBLICATION pub_users FOR TABLE users;

-- 只发布 INSERT（不同步 UPDATE/DELETE）
CREATE PUBLICATION pub_users_insert FOR TABLE users WITH (publish = 'insert');

-- 发布多张表
CREATE PUBLICATION pub_all FOR TABLE users, orders, products;

-- 查看发布
SELECT * FROM pg_publication;
SELECT * FROM pg_publication_tables;
```

```sql
-- ════════ 在备库（订阅端）════════

-- 表必须先存在且结构兼容
CREATE TABLE users (
    id   SERIAL PRIMARY KEY,
    name TEXT,
    email TEXT
);

-- 创建订阅
CREATE SUBSCRIPTION sub_users
    CONNECTION 'host=primary_host port=5432 dbname=mydb user=replicator password=repl_pass'
    PUBLICATION pub_users;

-- 订阅会自动：
-- 1. 做初始 COPY 同步现有数据
-- 2. 持续接收并应用增量变更

-- 查看订阅状态
SELECT * FROM pg_subscription;
SELECT * FROM pg_stat_subscription;
```

### 逻辑解码内部

```
WAL 记录 → 逻辑解码插件 → 逻辑变更消息 → 输出插件 → 网络传输

常用输出插件：
  pgoutput    PostgreSQL 内置（逻辑复制用）
  test_decoding  调试用
  wal2json    输出 JSON 格式
  pglogical   第三方高级逻辑复制
```

```sql
-- 用 pg_logical_slot_get_changes 查看逻辑变更
SELECT * FROM pg_logical_slot_get_changes('my_slot', NULL, NULL);

-- 输出示例：
-- xid  | lsn       | data
-- 1234 | 0/401E928 | BEGIN 1234
-- 1234 | 0/401E960 | table public.users: INSERT: id[integer]:1 name[text]:'Alice'
-- 1234 | 0/401E9A0 | COMMIT 1234
```

## 读写分离架构

### 拓扑

```
                    ┌─────────────────┐
                    │   Application   │
                    └────────┬────────┘
                             │
                             ▼
                    ┌─────────────────┐
                    │  Read/Write     │
                    │  Router         │
                    │  (PgBouncer /   │
                    │   应用层)       │
                    └────────┬────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
              ▼              ▼              ▼
        ┌──────────┐  ┌──────────┐  ┌──────────┐
        │ Primary  │  │ Standby1 │  │ Standby2 │
        │  (写)    │  │  (读)    │  │  (读)    │
        └──────────┘  └──────────┘  └──────────┘
              │              ▲              ▲
              │   WAL 流     │              │
              └──────────────┴──────────────┘
```

### 路由策略

```python
class ReadWriteRouter:
    WRITE_PREFIXES = ('INSERT', 'UPDATE', 'DELETE', 'MERGE',
                      'CREATE', 'DROP', 'ALTER', 'TRUNCATE', 'GRANT', 'REVOKE')
    READ_PREFIXES = ('SELECT', 'WITH', 'EXPLAIN', 'SHOW')

    def route(self, sql: str) -> str:
        first_word = sql.strip().split()[0].upper()
        if first_word in self.WRITE_PREFIXES:
            return 'primary'
        if first_word in self.READ_PREFIXES:
            return 'standby'
        return 'primary'
```

| SQL 类型 | 路由目标 | 说明 |
|---|---|---|
| `SELECT` | Standby | 只读查询走备库 |
| `INSERT/UPDATE/DELETE` | Primary | 写操作走主库 |
| `SELECT ... FOR UPDATE` | Primary | 加锁查询走主库 |
| `CREATE/DROP/ALTER` | Primary | DDL 走主库 |
| 事务内 `SELECT` | Primary | 写后读保证一致性 |

### 复制延迟问题

```
用户写入 Primary：                立即从 Standby 读：
  INSERT INTO orders ...           SELECT * FROM orders WHERE id = 100
       │                                │
       ▼                                ▼
  Primary 已提交                  Standby 还没收到 WAL
       │                                │
       └──── 用户读到旧数据或查不到 ────┘
       │
       ▼  "读己之写" 一致性被破坏！
```

**解决方案**：

```
1. 会话粘滞（Session Stickiness）
   写后 N 秒内同会话的读都走主库

2. 延迟检测回退
   查询前检查复制延迟，超阈值走主库

3. 同步复制
   用 synchronous_commit = remote_apply，但性能差

4. LSN 等待
   写后拿到 lsn，读前等待备库 replay 到该 lsn
```

```sql
-- 方案 4：LSN 等待（pg_wait_lsn 扩展或应用层轮询）
-- 写入后获取 LSN
SELECT pg_current_wal_lsn() AS commit_lsn;

-- 在备库等待该 LSN 被应用
SELECT pg_wal_lsn_diff(pg_last_wal_replay_lsn(), '0/401E928');
-- 返回 >= 0 表示已应用，可安全读
```

## Patroni 自动故障转移

### 架构

```
                    ┌─────────────────────────┐
                    │      etcd 集群 (DCS)     │
                    │   ┌─────┐ ┌─────┐ ┌─────┐│
                    │   │ etcd│ │ etcd│ │ etcd││
                    │   │  1  │ │  2  │ │  3  ││
                    │   └─────┘ └─────┘ └─────┘│
                    └────────────┬────────────┘
                                 │
                    ┌────────────┼────────────┐
                    │            │            │
                    ▼            ▼            ▼
              ┌──────────┐ ┌──────────┐ ┌──────────┐
              │ Patroni  │ │ Patroni  │ │ Patroni  │
              │  (PG 1)  │ │  (PG 2)  │ │  (PG 3)  │
              └──────────┘ └──────────┘ └──────────┘
                    │            │            │
                    ▼            ▼            ▼
              ┌──────────┐ ┌──────────┐ ┌──────────┐
              │  Leader  │ │ Replica  │ │ Replica  │
              │  (读写)  │ │  (只读)  │ │  (只读)  │
              └──────────┘ └──────────┘ └──────────┘

  Patroni 通过 etcd 进行 leader 选举
  Leader 宕机 → etcd 租约过期 → 新 leader 选举 → 提升 replica
```

### Patroni 配置

```yaml
# patroni.yml
scope: pg-cluster
name: node1

restapi:
  listen: 0.0.0.0:8008
  connect_address: 192.168.1.10:8008

etcd:
  hosts: 192.168.1.100:2379,192.168.1.101:2379,192.168.1.102:2379

bootstrap:
  dcs:
    ttl: 30                          # leader 租约（秒）
    loop_wait: 10                    # 心跳间隔
    retry_timeout: 10
    maximum_lag_on_failover: 1048576 # 备库最大延迟（字节），超此不提升
    synchronous_mode: true           # 同步复制模式
    postgresql:
      use_pg_rewind: true            # 旧主重新加入时用 pg_rewind
      parameters:
        wal_level: replica
        max_wal_senders: 10
        hot_standby: on
        synchronous_commit: on

postgresql:
  listen: 0.0.0.0:5432
  connect_address: 192.168.1.10:5432
  data_dir: /var/lib/postgresql/data
  bin_dir: /usr/lib/postgresql/bin
  authentication:
    replication:
      username: replicator
      password: repl_pass
    superuser:
      username: postgres
      password: pg_pass
  pg_hba:
    - host replication replicator 0.0.0.0/0 md5
    - host all all 0.0.0.0/0 md5

tags:
  nofailover: false
  noloadbalance: false
  clonefrom: false
```

### 故障转移流程

```
1. Leader 宕机
   │
   ▼
2. etcd 检测到 Leader 的 Patroni 心跳停止
   │  (TTL=30s 租约过期)
   ▼
3. etcd 释放 /pg-cluster/leader 锁
   │
   ▼
4. 其余 Patroni 节点竞争获取锁
   │  (先到先得，但会检查 maximum_lag_on_failover)
   ▼
5. 获胜节点执行 promote
   │  pg_ctl promote → 备库变主库
   ▼
6. 其余节点重新指向新 Leader
   │  更新 primary_conninfo
   ▼
7. 客户端通过 haproxy / VIP 感知新主库
   │
   ▼
8. 旧 Leader 恢复后用 pg_rewind 同步差异，重新加入为 Replica
```

### Patroni REST API

```bash
# 查看集群状态
patronictl list
# + Cluster: pg-cluster (1234) ----+----+-----------+
# | Member | Host        | Role    | State    | TL | Lag in MB |
# | node1  | 192.168.1.10| Leader  | running  | 42 |           |
# | node2  | 192.168.1.11| Replica | streaming| 42 |         0 |
# | node3  | 192.168.1.12| Replica | streaming| 42 |         0 |
# +--------+-------------+---------+----------+----+-----------+

# 手动切换
patronictl switchover pg-cluster

# 重初始化节点
patronictl reinit pg-cluster node3

# REST API 健康检查（给 HAProxy 用）
curl http://192.168.1.10:8008/health       # 200 = 健康
curl http://192.168.1.10:8008/primary      # 200 = 是主库
curl http://192.168.1.10:8008/replica      # 200 = 是备库
```

### HAProxy 配置

```yaml
# haproxy.cfg
listen pg_cluster
    bind *:5432
    mode tcp
    option httpchk GET /primary
    http-check expect status 200
    default-server inter 3s fall 3 rise 2 on-marked-down shutdown-sessions
    server node1 192.168.1.10:5432 check port 8008
    server node2 192.168.1.11:5432 check port 8008
    server node3 192.168.1.12:5432 check port 8008

listen pg_replicas
    bind *:5433
    mode tcp
    balance roundrobin
    option httpchk GET /replica
    http-check expect status 200
    server node2 192.168.1.11:5432 check port 8008
    server node3 192.168.1.12:5432 check port 8008
```

## PgBouncer 连接池

### 为什么需要连接池

```
无连接池：                        有连接池（PgBouncer）：
客户端 1 ─┐                       客户端 1 ─┐
客户端 2 ─┼─► 100 个 PG backend    客户端 2 ─┼─► PgBouncer ─► 10 个 PG backend
客户端 3 ─┤  (每个连接一个进程)     客户端 3 ─┤    (复用连接)
...      ─┤                       ...      ─┤
客户端100─┘                       客户端100─┘

  100 个进程，内存开销大           10 个进程，内存开销小
  连接建立慢                       连接建立快（复用）
```

### 池化模式

| 模式 | 说明 | 事务支持 | 适用场景 |
|---|---|---|---|
| **session** | 一个客户端连接绑定一个服务端连接 | 完整 | 兼容性最好，但池化效果差 |
| **transaction** | 事务结束后归还连接 | 仅事务内 | **推荐**，大多数 Web 应用 |
| **statement** | 每条语句后归还 | 无 | 简单查询，不支持事务 |
| **user** | 按用户分池 | 完整 | 多用户隔离 |

> **关键限制**：`transaction` 模式下，`SET`、`PREPARE`、临时表等会话级状态在事务结束后丢失。用 `server_reset_query` 清理。

### PgBouncer 配置

```ini
# pgbouncer.ini
[databases]
mydb = host=127.0.0.1 port=5432 dbname=mydb
; 写库
mydb_write = host=primary_host port=5432 dbname=mydb
; 读库
mydb_read = host=standby_host port=5432 dbname=mydb

[pgbouncer]
listen_addr = 0.0.0.0
listen_port = 6432

auth_type = md5
auth_file = /etc/pgbouncer/userlist.txt

pool_mode = transaction              ; session / transaction / statement
max_client_conn = 1000               ; 客户端最大连接数
default_pool_size = 20               ; 每个数据库/用户的服务端连接数
reserve_pool_size = 5                ; 额外保留连接
reserve_pool_timeout = 3             ; 等待保留连接的超时（秒）
server_idle_timeout = 300            ; 空闲连接超时
server_lifetime = 3600               ; 服务端连接最大生命
query_wait_timeout = 120             ; 客户端排队等待超时

server_reset_query = DISCARD ALL     ; 归还连接时清理会话状态
ignore_startup_parameters = extra_float_digits
```

### 连接数计算

```
假设：
  应用实例数 = 20
  每实例最大连接 = 50
  PG max_connections = 100

无 PgBouncer：
  总连接 = 20 × 50 = 1000  ►  超过 PG 限制！

有 PgBouncer（transaction 模式）：
  客户端连接 = 20 × 50 = 1000（PgBouncer 接受）
  服务端连接 = 20 × default_pool_size = 20 × 20 = 400  ►  仍可能超

  调整：default_pool_size = 100 / 20 = 5
  服务端连接 = 20 × 5 = 100  ►  刚好

  公式：default_pool_size = PG max_connections / 应用实例数
```

## 水平扩展

### Citus 架构

```
                    ┌──────────────────────┐
                    │   Coordinator Node   │
                    │  (元数据 + 路由)      │
                    │                      │
                    │  ┌────────────────┐  │
                    │  │  查询规划器     │  │
                    │  └───────┬────────┘  │
                    └──────────┼───────────┘
                               │ 分发子查询
              ┌────────────────┼────────────────┐
              │                │                │
              ▼                ▼                ▼
        ┌──────────┐     ┌──────────┐     ┌──────────┐
        │ Worker 1 │     │ Worker 2 │     │ Worker 3 │
        │          │     │          │     │          │
        │ shard_0  │     │ shard_1  │     │ shard_2  │
        │ shard_3  │     │ shard_4  │     │ shard_5  │
        └──────────┘     └──────────┘     └──────────┘

  分布表：按分布键哈希分片到所有 Worker
  引用表：每个 Worker 存全量副本（广播）
  本地表：仅存于 Coordinator
```

### Citus 分布表

```sql
-- 创建分布表（按 user_id 哈希分片）
CREATE TABLE events (
    id          BIGSERIAL,
    user_id     BIGINT,
    event_type  TEXT,
    created_at  TIMESTAMPTZ,
    payload     JSONB
);

SELECT create_distributed_table('events', 'user_id');

-- 查看分片
SELECT * FROM pg_dist_partition WHERE logicalrelid = 'events'::regclass;

-- 查看分片放置
SELECT
    shardid,
    nodename,
    nodeport,
    shardstate
FROM pg_dist_shard_placement
WHERE shardid IN (
    SELECT shardid FROM pg_dist_shard
    WHERE logicalrelid = 'events'::regclass
);
```

```sql
-- 引用表（广播到所有节点，用于 JOIN）
CREATE TABLE users (
    user_id     BIGINT PRIMARY KEY,
    name        TEXT,
    region      TEXT
);

SELECT create_reference_table('users');

-- 分布表 JOIN 引用表：本地 JOIN，无需跨节点
SELECT e.event_type, u.name, u.region
FROM events e JOIN users u ON e.user_id = u.user_id
WHERE e.created_at > now() - interval '1 day';
```

### 分片策略

```
1. 哈希分片（最常用）
   shard = hash(distribution_key) % num_shards

   优点：数据均匀分布
   缺点：扩容需要 rehash（Citus 用一致性哈希优化）

2. 范围分片
   shard 0: user_id ∈ [0, 10000)
   shard 1: user_id ∈ [10000, 20000)
   shard 2: user_id ∈ [20000, 30000)

   优点：范围查询高效
   缺点：可能热点

3. 列表分片
   shard 0: region = 'US'
   shard 1: region = 'EU'
   shard 2: region = 'Asia'

   优点：地域就近访问
   缺点：分布不均
```

### 跨分片查询

```sql
-- 单分片查询（最快）：WHERE 包含分布键
SELECT * FROM events WHERE user_id = 12345;
-- Citus 路由到单个 shard，等价于单机查询

-- 多分片查询（并行）：
SELECT count(*) FROM events WHERE created_at > '2024-01-01';
-- Citus 向所有 shard 发 count(*)，汇总结果

-- 跨分片 JOIN（慢）：
SELECT * FROM events e1 JOIN events e2 ON e1.user_id = e2.user_id;
-- 需要 redistribute，数据在网络中移动

-- 聚合下推：
SELECT user_id, count(*) FROM events GROUP BY user_id;
-- 每个 shard 本地聚合 → Coordinator 汇总
```

| 查询类型 | 是否下推 | 性能 |
|---|---|---|
| 分布键过滤 | 单分片 | 极快 |
| 聚合 + GROUP BY 分布键 | 下推 | 快 |
| 聚合 + GROUP BY 非分布键 | 部分下推 | 中等 |
| 跨分片 JOIN | 需 redistribute | 慢 |
| 引用表 JOIN 分布表 | 本地 JOIN | 快 |

## 垂直扩展 vs 水平扩展

```
垂直扩展（Scale Up）：           水平扩展（Scale Out）：
                                  
  ┌──────────┐                   ┌──────────┐ ┌──────────┐
  │  CPU:    │                   │  CPU:    │ │  CPU:    │
  │  64 核   │                   │  8 核    │ │  8 核    │
  │  RAM:    │                   │  RAM:    │ │  RAM:    │
  │  512GB   │                   │  32GB    │ │  32GB    │
  │  SSD:    │                   │  SSD:    │ │  SSD:    │
  │  20TB    │                   │  2TB     │ │  2TB     │
  └──────────┘                   └──────────┘ └──────────┘
       ▲                              ▲            ▲
       │                              └─────┬──────┘
  升级硬件                              │
  有上限                                ▼
                                  ┌──────────┐
                                  │  Router  │
                                  └──────────┘
```

| 维度 | 垂直扩展 | 水平扩展 |
|---|---|---|
| **方式** | 升级单机硬件 | 增加节点数 |
| **上限** | 硬件物理上限 | 理论上无限 |
| **成本** | 边际成本递增（高端硬件贵） | 边际成本线性 |
| **复杂度** | 低（应用无感知） | 高（分片、路由、分布式事务） |
| **一致性** | 强一致 | 最终一致（多数场景） |
| **运维** | 简单 | 复杂（多节点管理） |
| **故障影响** | 整机故障 | 单节点故障 |
| **适用阶段** | 初期、数据量中 | 数据量大、单机扛不住 |

> **实践经验**：先垂直扩展到极限（通常 64 核 + 512GB + NVMe SSD 能扛 TB 级数据），再考虑水平扩展。过早分片会引入不必要的复杂度。

### 扩展决策树

```
                    ┌─────────────────┐
                    │  性能瓶颈？      │
                    └────────┬────────┘
                             │
                    ┌────────┴────────┐
                    │                 │
                 CPU/IO            连接数
                    │                 │
                    ▼                 ▼
            ┌───────────┐     ┌───────────┐
            │ 单机扛得住？│     │  PgBouncer │
            └─────┬─────┘     │  连接池    │
                  │           └───────────┘
          ┌───────┴───────┐
          │               │
         是              否
          │               │
          ▼               ▼
   ┌───────────┐   ┌───────────┐
   │ 调优索引   │   │ 读写分离   │
   │ 升级硬件   │   │ + 备库读   │
   └───────────┘   └─────┬─────┘
                         │
                  ┌──────┴──────┐
                  │             │
               扛得住         扛不住
                  │             │
                  ▼             ▼
            (结束)       ┌───────────┐
                        │  水平分片   │
                        │  Citus     │
                        └───────────┘
```

## 文件清单

| 文件 | 内容 |
|---|---|
| `streaming_replication.py` | 流复制配置生成 + 状态检查 |
| `read_write_split.py` | 读写分离路由 + 复制延迟处理 |
| `sharding_demo.py` | 哈希/范围分片 + 跨分片查询 |
| `docker-compose.yml` | PG 主备 + PgBouncer + Patroni 部署 |

## 习题

1. 在 docker-compose 环境中启动主备，用 `streaming_replication.py` 生成配置，验证 `pg_stat_replication` 中 `lag_bytes` 为 0

2. 修改 `read_write_split.py`，实现会话粘滞：写操作后 5 秒内同会话的读都路由到主库，并用 sqlite3 模拟验证

3. 用 `sharding_demo.py` 向 4 个分片插入 10000 条数据，验证数据均匀分布（每分片约 2500 条），并实现跨分片 `COUNT(*)` 聚合

4. 配置 Patroni 集群（3 节点 + etcd），手动 kill 主库的 Patroni 进程，观察故障转移过程，记录 RTO

5. 对比同步复制和异步复制的写性能：用 `pgbench` 跑 TPS 基准，计算同步复制带来的性能下降百分比