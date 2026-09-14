# 章7：高可用与扩展

> 单机数据库是系统的单点故障（SPOF）。本章覆盖 PostgreSQL 高可用（流复制、逻辑复制、Patroni 自动故障转移、PgBouncer 连接池）与扩展（读写分离、Citus 水平分片、垂直 vs 水平扩展）的完整工程实践。
>
> **本章学习路线**：先理解高可用核心概念（RPO/RTO/脑裂）→ 掌握 PostgreSQL 两种复制方式（物理/逻辑）→ 学会读写分离与连接池 → 用 Patroni 实现自动故障转移 → 最后用 Citus 做水平分片。每节配有架构图、配置示例和可运行的 Python 演示。

---

## 1. 高可用基础

### 1.1 为什么需要高可用

新手常问："我的数据库跑得好好的，为什么要折腾高可用？"答案很简单——**硬件一定会坏**。磁盘会老化、内存条会报错、网络会中断、机房会断电。如果你只有一个数据库实例，任何一个环节故障，整个业务就停了。

```
单点故障（Single Point of Failure, SPOF）：

         ┌──────────┐
         │  应用    │
         └────┬─────┘
              │
              ▼
         ┌──────────┐
         │ 单机 DB  │ ◄── 唯一实例，挂了全挂
         └──────────┘
              │
              ▼
         ┌──────────┐
         │  磁盘    │ ◄── 磁盘损坏 = 数据丢失
         └──────────┘

  故障影响：业务完全中断，可能数据永久丢失
  恢复时间：取决于备份频率和故障类型，可能数小时
```

高可用的核心思想是**冗余**：准备多个数据库实例，一个挂了另一个顶上。但冗余带来新问题：数据怎么同步？谁来决定切换？切换时会不会有两个主库？这些问题就是本章要解决的。

### 1.2 RPO 与 RTO

RPO 和 RTO 是衡量高可用能力的两个核心指标，新手必须先搞清楚它们。

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

**新手理解要点**：
- RPO 关心"丢多少数据"，RTO 关心"停多少时间"
- RPO = 0 要求同步复制（主库提交前等备库收到）
- RTO < 30s 要求自动故障转移（人工切换太慢）
- 不同业务对 RPO/RTO 要求不同：银行要 RPO=0，博客可以 RPO=几秒

**可用性等级对照表**：

| 可用性 % | 年停机时间 | 月停机时间 | 适用场景 |
|---|---|---|---|
| 99% | 3.65 天 | 7.2 小时 | 内部工具、测试环境 |
| 99.9% | 8.76 小时 | 43.2 分钟 | 一般企业应用 |
| 99.99% | 52.6 分钟 | 4.32 分钟 | 电商、SaaS |
| 99.999% | 5.26 分钟 | 25.9 秒 | 金融、电信 |
| 99.9999% | 31.5 秒 | 2.59 秒 | 极少数核心系统 |

> 每多一个 9，成本大约增加 10 倍。新手不要盲目追求 5 个 9，先搞清楚业务真正需要多少。

### 1.3 故障转移模式

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

| 模式 | 数据流向 | 冲突风险 | 复杂度 | 典型用途 |
|---|---|---|---|---|
| 主动-被动 | 单向（主→备） | 无 | 低 | PostgreSQL HA 主流方案 |
| 主动-主动 | 双向 | 高 | 高 | 多地域多活、CockroachDB |

**新手建议**：从主动-被动开始，这是 PostgreSQL 最成熟的 HA 模式。主动-主动涉及冲突解决（同一条记录在两个节点同时修改），非常复杂，不要轻易尝试。

### 1.4 脑裂问题

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

**脑裂的后果**：
- 两个主库各自接受写入，数据分叉
- 网络恢复后无法自动合并，需人工修数据
- 可能丢失两边各自的写入

**三种防脑裂方案对比**：

| 方案 | 原理 | 优点 | 缺点 |
|---|---|---|---|
| STONITH | 故障时直接射杀旧主（断电/重启） | 彻底避免双主 | 需要硬件支持 |
| 多数派投票 | N/2+1 节点同意才能当主 | 软件实现，通用 | 需要奇数节点 |
| 见证节点 | 引入第三方仲裁者 | 2 节点也能用 | 见证节点本身要可靠 |

Patroni 用的是多数派投票（通过 etcd/ZooKeeper/Consul 实现），本章后面会详细讲。

---

## 2. PostgreSQL 流复制（物理复制）

### 2.1 物理复制原理

流复制是 PostgreSQL 高可用的基石。它把主库的 WAL（预写日志）实时发送给备库，备库重放 WAL 来保持数据一致。

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

**WAL 是什么？** WAL（Write-Ahead Log）是 PostgreSQL 的预写日志，所有数据修改在写入实际数据文件前，先写 WAL。这样即使突然断电，重启时也能用 WAL 恢复到一致状态。流复制就是把这份 WAL 实时传给备库。

**流复制工作流程**：
1. 主库执行写操作，生成 WAL 记录到 WAL Buffer
2. WAL Buffer 刷盘成 WAL Segment
3. walsender 进程读取 WAL Segment，通过 TCP 发送给备库
4. 备库 walreceiver 进程接收 WAL
5. 备库执行 WAL Redo（重放），把变更应用到数据页
6. 备库 Buffer Pool 更新，只读查询能看到新数据

### 2.2 同步 vs 异步复制

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

**新手选择建议**：
- 先用异步复制（默认），简单且性能好
- 只有业务不能容忍任何数据丢失时才上同步
- 同步复制会降低写吞吐量约 10-30%

### 2.3 主库配置步骤

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

**参数逐行解释**：
- `wal_level = replica`：WAL 记录足够信息让备库重放。`minimal` 不支持复制，`logical` 额外记录逻辑变更用于逻辑复制
- `max_wal_senders = 10`：最多 10 个备库同时连接。每个备库占一个 walsender 进程
- `wal_keep_size = 10240`：保留 10GB WAL，备库短暂断连后重连还能追上
- `hot_standby = on`：备库启动后接受只读查询。主库也开这个，方便切换后原主库变备库时能查询

```sql
-- 创建复制用户
CREATE ROLE replicator WITH REPLICATION LOGIN PASSWORD 'repl_pass';

-- pg_hba.conf 允许备库连接
-- host replication replicator 192.168.1.0/24 md5
```

**pg_hba.conf 是什么？** 它是 PostgreSQL 的客户端认证配置文件，决定哪些 IP 可以用什么方式连接。上面这行表示：允许 192.168.1.0/24 网段的备库用 replicator 用户通过 md5 密码认证进行复制连接。

### 2.4 备库配置步骤

```bash
# 1. 用 pg_basebackup 做基础备份
pg_basebackup -h primary_host -U replicator -D /var/lib/postgresql/standby \
              -Fp -Xs -P -R

# -Fp  plain 格式
# -Xs  stream 模式传 WAL
# -P   显示进度
# -R   写 standby.signal 和 primary_conninfo
```

**pg_basebackup 做了什么**：
1. 连接主库，发起一次基础备份
2. 把主库所有数据文件复制到本地（-Fp 用 plain 格式）
3. 同时流式接收期间的 WAL（-Xs stream 模式）
4. 写入 standby.signal 文件和 primary_conninfo（-R）
5. 完成后备库数据目录就是主库某个时间点的完整快照

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

**关键参数解释**：
- `hot_standby_feedback = on`：备库把自己的快照信息反馈给主库，避免主库 vacuum 清理掉备库还在用的行
- `max_standby_streaming_delay = 30s`：当备库有长查询正在读某行，而主库的 WAL 要修改这行时，备库最多等 30 秒，超时取消查询并应用 WAL

### 2.5 复制状态监控 SQL

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

**LSN 是什么？** LSN（Log Sequence Number）是 WAL 的位置标识，格式如 `0/401E928`。通过比较两个 LSN 可以知道备库落后多少。`pg_wal_lsn_diff(a, b)` 返回 a 和 b 之间的字节差。

**监控查询解读**：

| 字段 | 含义 | 健康值 |
|---|---|---|
| `state` | 复制状态 | `streaming`（正常流式传输） |
| `sync_state` | 同步状态 | `sync` 或 `async` |
| `sent_lsn` | 主库已发送位置 | 持续增长 |
| `replay_lsn` | 备库已应用位置 | 紧跟 sent_lsn |
| `lag_bytes` | 延迟字节数 | 接近 0 |

### 2.6 复制槽（Replication Slot）

复制槽解决一个问题：备库断连后，主库不知道该保留多少 WAL，可能把备库还没收到的 WAL 删了。复制槽让主库知道"备库收到哪了"，保留必要的 WAL。

```sql
-- 创建物理复制槽
SELECT pg_create_physical_replication_slot('standby_slot');
-- 返回：slot_name | lsn
--        standby_slot | 0/401E928

-- 查看复制槽
SELECT slot_name, slot_type, active, restart_lsn
FROM pg_replication_slots;

-- 删除复制槽
SELECT pg_drop_replication_slot('standby_slot');
```

> **警告**：复制槽会一直保留 WAL，如果备库永久宕机，主库 WAL 会无限堆积撑满磁盘。务必监控复制槽状态，及时清理失效槽。

---

## 3. 逻辑复制（发布/订阅）

### 3.1 物理复制 vs 逻辑复制

```
物理复制：                        逻辑复制：
┌──────────┐                     ┌──────────┐
│ Primary  │                     │ Primary  │
│ ┌──────┐ │                     │ ┌──────┐ │
│ │ WAL  │ │  字节流复制         │ │ WAL  │ │ 解码
│ │(字节)│ │ ──────────────►    │ │(逻辑)│ │
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

**新手怎么选**：
- 要做 HA 故障转移 → 物理复制（备库只读，切换简单）
- 要把部分表同步到另一个库做分析 → 逻辑复制（订阅端可写，可选表）
- 要跨 PostgreSQL 版本迁移 → 逻辑复制（物理复制要求版本一致）

### 3.2 发布与订阅模型

逻辑复制用"发布/订阅"模型，类似消息队列：主库发布变更，备库订阅接收。

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

**发布/订阅工作流程**：
1. 主库创建发布，指定哪些表的哪些变更（INSERT/UPDATE/DELETE）要发布
2. 备库创建订阅，连接主库并指定订阅哪个发布
3. 订阅自动做初始同步：把现有数据 COPY 过来
4. 之后主库每次变更，逻辑解码生成变更消息，发送给订阅端
5. 订阅端应用变更到本地表

### 3.3 逻辑解码内部

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

**逻辑解码是什么**：把 WAL 里的二进制字节流"翻译"回逻辑操作（INSERT/UPDATE/DELETE）。物理复制直接传字节流，逻辑复制先解码成逻辑操作再传。这就是为什么逻辑复制能跨版本——不同版本的 WAL 格式可能不同，但 INSERT 语义是通用的。

### 3.4 逻辑复制适用场景

| 场景 | 说明 | 为什么用逻辑复制 |
|---|---|---|
| 数据集成 | 把多个库的数据汇总到一个分析库 | 可选表、订阅端可写 |
| 在线升级 | PostgreSQL 12 升级到 16 | 跨版本，减少停机 |
| 部分同步 | 只同步几张关键表 | 表级粒度 |
| 多活 | 两个库互相订阅 | 双向复制（需处理冲突） |
| CDC 到 Kafka | 用 wal2json + Debezium | 逻辑解码输出 JSON |

> **新手注意**：逻辑复制不复制 DDL（CREATE/ALTER/DROP）。如果主库加了一列，备库不会自动加，需要手动在备库执行相同的 ALTER TABLE。这是逻辑复制最常见的坑。

---

## 4. 读写分离架构

### 4.1 为什么需要读写分离

大多数业务都是"读多写少"：电商商品浏览远多于下单，博客阅读远少于发文章。单主库扛不住所有读请求时，把读分散到备库就能成倍提升读吞吐量。

```
无读写分离：                   有读写分离：
  ┌────────┐                     ┌────────┐
  │  应用  │                     │  应用  │
  └───┬────┘                     └───┬────┘
      │ 1000 QPS                     │ 1000 QPS
      ▼                              ▼
  ┌────────┐                     ┌────────┐
  │Primary │ ◄── 扛不住          │ Router │
  │ 读写   │                     └───┬────┘
  └────────┘              ┌─────────┼─────────┐
                          ▼         ▼         ▼
                     ┌────────┐ ┌────────┐ ┌────────┐
                     │Primary │ │Standby1│ │Standby2│
                     │  写    │ │  读    │ │  读    │
                     └────────┘ └────────┘ └────────┘
                     200 写 QPS  400 读   400 读
```

### 4.2 架构拓扑

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

路由可以放在两个位置：
1. **应用层**：应用代码里判断 SQL 类型，连不同的库。灵活但侵入业务代码
2. **中间件层**：用 PgBouncer 或 ProxySQL 等中间件路由。对应用透明

### 4.3 路由策略

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

**三种路由策略对比**：

| 策略 | 原理 | 优点 | 缺点 |
|---|---|---|---|
| 基于 SQL 类型 | 解析 SQL 第一个关键字 | 简单 | 事务内读可能读到旧数据 |
| 基于会话粘滞 | 写后 N 秒内同会话读走主库 | 解决写后读一致性 | N 秒内备库空闲 |
| 基于延迟检测 | 查询前检查备库延迟，超阈值走主库 | 数据一致 | 增加检查开销 |

### 4.4 复制延迟问题

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

这是读写分离最经典的问题：用户刚下单，立即刷新页面却看不到订单，因为备库还没收到这条记录的 WAL。

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

**新手实践建议**：
- 优先用会话粘滞（最简单，覆盖 90% 场景）
- 对一致性要求极高的查询（如支付后查订单）用 LSN 等待
- 不要无脑用同步复制，性能影响太大

---

## 5. Patroni 自动故障转移

### 5.1 为什么需要 Patroni

手动故障转移流程：发现主库挂了 → 人工确认 → 登录备库执行 promote → 修改应用连接串 → 重启应用。整个过程至少 10-30 分钟，RTO 太长。Patroni 把这个过程自动化，RTO 可控制在 30 秒内。

### 5.2 自动故障转移架构

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

### 5.3 etcd / ZooKeeper 的角色

Patroni 不自己选主，而是依赖一个分布式配置存储（DCS）来协调。可选 etcd、ZooKeeper 或 Consul。

| DCS | 语言 | 特点 | 推荐度 |
|---|---|---|---|
| **etcd** | Go | 轻量、Kubernetes 标配 | 推荐 |
| ZooKeeper | Java | 成熟但重、需 JVM | 传统 |
| Consul | Go | 自带服务发现 | 多功能场景 |
| raft | Python | Patroni 内置，仅测试 | 不推荐生产 |

**DCS 的核心作用**：
1. **存储集群元数据**：谁是 leader、连接信息、配置参数
2. **提供分布式锁**：leader 选举靠抢锁
3. **租约机制**：leader 心跳续租，宕机后租约过期释放锁

```
etcd 中的关键键：
  /pg-cluster/leader      → "node1"        (谁是主库)
  /pg-cluster/leader-lock → lease(node1)   (主库租约)
  /pg-cluster/config      → {ttl:30, ...}  (集群配置)
  /pg-cluster/members/node1 → {conn_url, ...}
  /pg-cluster/status      → {timeline, ...}
```

### 5.4 选主过程详解

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

**选主时的延迟检查**：Patroni 不会盲目提升延迟最大的备库。每个备库竞选前会检查自己的复制延迟，超过 `maximum_lag_on_failover`（默认 1MB）就放弃竞选。这保证新主库的数据不会太旧。

### 5.5 Split Brain 预防

Patroni 通过以下机制预防脑裂：

```
正常情况：
  etcd 持有 leader 锁 ← node1 持续续租（每 loop_wait=10s）
  node1 是主库，node2/node3 是备库

网络分区（node1 与 etcd 断开）：
  node1 无法续租 → TTL=30s 后 etcd 释放锁
  node2/node3 竞选 → node2 获胜，提升为主库
  node1 仍在运行，但无法续租 → Patroni 主动 demote 自己
  → node1 降级为备库（或停止），避免双主

最坏情况（node1 与 etcd 断开但仍在运行）：
  最多 30s（TTL）内可能有两个主库
  → 用同步复制 + fencing 进一步降低风险
```

| 防脑裂机制 | 原理 | 效果 |
|---|---|---|
| etcd 租约 | 主库必须持续续租 | 网络分区后最多 TTL 秒双主 |
| 同步复制 | 主库提交需备库确认 | 备库数据不落后 |
| maximum_lag_on_failover | 延迟大的备库不竞选 | 新主库数据较新 |
| pg_rewind | 旧主重新加入时回退差异 | 旧主不会分叉 |
| STONITH（可选） | 物理射杀旧主 | 彻底避免双主 |

### 5.6 Patroni 配置

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

**关键配置解释**：
- `scope: pg-cluster`：集群名，同一 DCS 里不同集群用 scope 区分
- `ttl: 30`：leader 租约 30 秒。TTL 太小容易误切换，太大 RTO 长
- `loop_wait: 10`：Patroni 每 10 秒检查一次集群状态
- `maximum_lag_on_failover: 1048576`：备库延迟超过 1MB 不允许提升为主
- `synchronous_mode: true`：开启同步复制，进一步防脑裂
- `use_pg_rewind: true`：旧主恢复后用 pg_rewind 回退差异再重新加入

### 5.7 Patroni REST API

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

**REST API 端点说明**：

| 端点 | 返回 200 条件 | 用途 |
|---|---|---|
| `/health` | Patroni 进程存活 | 通用健康检查 |
| `/primary` | 该节点是主库 | HAProxy 路由写请求 |
| `/replica` | 该节点是备库且已就绪 | HAProxy 路由读请求 |
| `/read-only` | 该节点是备库（可能未就绪） | 宽松的读路由 |
| `/reload` | 配置重载成功 | 运维操作 |

### 5.8 HAProxy 配置

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

**HAProxy 工作原理**：
- 应用连 5432 端口（写端口）→ HAProxy 检查每个节点的 `/primary` → 只转发到返回 200 的节点（主库）
- 应用连 5433 端口（读端口）→ HAProxy 检查 `/replica` → 轮询转发到返回 200 的节点（备库）
- 主库切换后，旧主 `/primary` 返回非 200，新主返回 200，HAProxy 自动切换路由

---

## 6. PgBouncer 连接池

### 6.1 连接池在高可用中的角色

PostgreSQL 每个连接 fork 一个后端进程，进程间内存独立。1000 个连接 = 1000 个进程，内存开销巨大。PgBouncer 在中间复用连接，让 1000 个客户端共享 20 个数据库连接。

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

**在高可用中的作用**：
1. **连接复用**：减少 PG 后端进程数，降低内存
2. **故障透明**：后端故障转移时，PgBouncer 重连新主库，客户端无感知
3. **连接限流**：防止突发流量打挂数据库

### 6.2 池化模式

| 模式 | 说明 | 事务支持 | 适用场景 |
|---|---|---|---|
| **session** | 一个客户端连接绑定一个服务端连接 | 完整 | 兼容性最好，但池化效果差 |
| **transaction** | 事务结束后归还连接 | 仅事务内 | **推荐**，大多数 Web 应用 |
| **statement** | 每条语句后归还 | 无 | 简单查询，不支持事务 |
| **user** | 按用户分池 | 完整 | 多用户隔离 |

> **关键限制**：`transaction` 模式下，`SET`、`PREPARE`、临时表等会话级状态在事务结束后丢失。用 `server_reset_query` 清理。

**新手怎么选**：
- Web 应用大多数用 `transaction`（推荐）
- 需要会话级状态（临时表、SET 变量）用 `session`
- `statement` 模式几乎不用，不支持事务限制太大

### 6.3 PgBouncer 配置示例

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

**配置逐行解释**：
- `max_client_conn = 1000`：PgBouncer 最多接受 1000 个客户端连接
- `default_pool_size = 20`：每个数据库+用户组合最多 20 个后端连接
- `reserve_pool_size = 5`：正常 20 个不够时，额外开 5 个
- `server_reset_query = DISCARD ALL`：连接归还池前执行 DISCARD ALL 清理会话状态
- `query_wait_timeout = 120`：客户端等连接超 120 秒报错，防止无限排队

### 6.4 连接数计算

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

**新手公式**：`default_pool_size = PG max_connections / 应用实例数`。留 20% 余量，比如 PG 100 连接、20 个应用实例，`default_pool_size = 100 * 0.8 / 20 = 4`。

---

## 7. 水平扩展

### 7.1 Citus 架构

当单机扛不住时，把数据分散到多台机器——这就是水平分片。Citus 是 PostgreSQL 的分片扩展，把表按分布键哈希到多个 Worker 节点。

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

**三种表类型**：

| 类型 | 数据分布 | 用途 | JOIN 性能 |
|---|---|---|---|
| 分布表 | 按分布键哈希到各 Worker | 大表（事件、订单） | 同分布键可本地 JOIN |
| 引用表 | 每个 Worker 存全量 | 小表（用户、地区） | 与分布表本地 JOIN |
| 本地表 | 仅 Coordinator | 元数据、配置 | 仅 Coordinator 内 JOIN |

### 7.2 Citus 分布表

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

### 7.3 分片策略

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

**分片策略选择**：

| 策略 | 分布均匀度 | 范围查询 | 扩容难度 | 适用 |
|---|---|---|---|---|
| 哈希分片 | 好 | 差 | 中（一致性哈希优化） | 通用，无明确范围查询 |
| 范围分片 | 可能不均 | 好 | 高（需重新划分边界） | 时间序列、按 ID 范围 |
| 列表分片 | 取决于数据 | 差 | 低（加新分片） | 地域分片 |

### 7.4 一致性哈希图解

一致性哈希解决扩容时数据迁移问题。普通哈希扩容时几乎所有数据要重新分片，一致性哈希只迁移 1/n。

```
普通哈希（扩容 4→5 分片）：
  hash(key) % 4 → hash(key) % 5
  几乎所有 key 的分片都变了，需迁移 ~80% 数据

一致性哈希环：
              0
         ╱        ╲
       shard0     shard4（新加）
       │            │
       │   shard2   │
       │  ╱      ╲  │
       │ shard1  shard3
        ╲    │    ╱
         ╲  │  ╱
          ╲ │ ╱
           ╲│╱
            ∞

  key 哈希后落在环上某点，顺时针找到第一个 shard
  新加 shard4 只影响它前面那段 key（约 1/5）
  迁移量 ≈ 1/n = 20%（理论最优）
```

```
扩容前（4 分片）：              扩容后（5 分片）：
  shard0: 2500 条                shard0: 2000 条（迁移 500）
  shard1: 2500 条                shard1: 2000 条（迁移 500）
  shard2: 2500 条                shard2: 2000 条（迁移 500）
  shard3: 2500 条                shard3: 2000 条（迁移 500）
                                 shard4: 2000 条（接收 2000）
  总迁移：~20%（理论最优 1/5）    总迁移：~20%
```

### 7.5 跨分片查询

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

**新手优化原则**：
1. 查询尽量带分布键（走单分片最快）
2. 聚合查询用 GROUP BY 分布键（可下推）
3. 大表 JOIN 小表时把小表做引用表（本地 JOIN）
4. 避免跨分片 JOIN（数据要在网络中移动）

---

## 8. 垂直扩展 vs 水平扩展

### 8.1 两种扩展方式

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

### 8.2 优缺点对比表

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

### 8.3 成本考量

```
垂直扩展成本曲线：
  16核 + 64GB   →  $1,000/月
  32核 + 128GB  →  $3,000/月  (3倍价格，2倍性能)
  64核 + 256GB  →  $8,000/月  (8倍价格，4倍性能)
  128核 + 512GB →  $20,000/月 (20倍价格，8倍性能)

  → 高端硬件边际成本急剧上升

水平扩展成本曲线：
  1节点 (8核+32GB)  →  $500/月
  2节点              →  $1,000/月  (线性)
  4节点              →  $2,000/月  (线性)
  8节点              →  $4,000/月  (线性)

  → 但需加上运维成本（人力、监控、网络）
```

> **实践经验**：先垂直扩展到极限（通常 64 核 + 512GB + NVMe SSD 能扛 TB 级数据），再考虑水平扩展。过早分片会引入不必要的复杂度。

### 8.4 什么时候选择什么

| 场景 | 推荐方式 | 原因 |
|---|---|---|
| 数据量 < 1TB | 垂直 | 单机足够，无需分片复杂度 |
| 数据量 1-10TB | 垂直优先，备库读 | 加备库扛读压力 |
| 数据量 > 10TB | 水平 | 单机存不下 |
| 写 QPS > 10万 | 水平 | 单主库写吞吐有上限 |
| 读 QPS > 10万 | 读写分离 | 加备库即可 |
| 强一致要求高 | 垂直 | 分布式事务复杂且慢 |
| 地域分布 | 水平 | 数据就近存储 |

### 8.5 扩展决策树

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

---

## 9. Docker 部署

### 9.1 docker-compose.yml 总览

项目提供了完整的 docker-compose.yml，一键启动主备复制 + PgBouncer + Patroni + HAProxy。文件位于 `phase2/07-ha-scaling/docker-compose.yml`。

```
整体架构：
  ┌─────────────────────────────────────────────┐
  │              docker-compose                 │
  │                                             │
  │  ┌─────────┐                                │
  │  │  etcd   │ ◄── DCS（Patroni 选主用）      │
  │  └────┬────┘                                │
  │       │                                     │
  │  ┌────┴────────┐  ┌──────────────┐         │
  │  │pg-primary   │  │pg-standby    │         │
  │  │  (主库)     │◄─┤  (备库)      │         │
  │  └─────────────┘  └──────────────┘         │
  │                                             │
  │  ┌─────────────┐                            │
  │  │ pgbouncer   │ ◄── 连接池 (6432 端口)     │
  │  └─────────────┘                            │
  │                                             │
  │  ┌──────────────┐  ┌──────────────┐        │
  │  │patroni-primary│  │patroni-standby│       │
  │  │  (5440)      │  │  (5441)      │        │
  │  └──────┬───────┘  └──────┬───────┘        │
  │         │                  │                │
  │  ┌──────┴──────────────────┴───────┐       │
  │  │          haproxy (5430)         │       │
  │  └─────────────────────────────────┘       │
  └─────────────────────────────────────────────┘
```

### 9.2 docker-compose.yml 逐行解读

**etcd 服务**（DCS，Patroni 选主用）：

```yaml
etcd:
  image: quay.io/coreos/etcd:v3.5.10      # etcd 3.5.10 镜像
  container_name: minidb-etcd
  environment:
    ETCD_NAME: etcd1                       # 节点名
    ETCD_DATA_DIR: /etcd-data              # 数据目录
    ETCD_INITIAL_ADVERTISE_PEER_URLS: http://etcd:2380  # 对等网络
    ETCD_LISTEN_PEER_URLS: http://0.0.0.0:2380
    ETCD_ADVERTISE_CLIENT_URLS: http://etcd:2379       # 客户端接口
    ETCD_LISTEN_CLIENT_URLS: http://0.0.0.0:2379
    ETCD_INITIAL_CLUSTER: etcd1=http://etcd:2380       # 集群成员
    ETCD_INITIAL_CLUSTER_STATE: new                    # 全新集群
    ETCD_INITIAL_CLUSTER_TOKEN: minidb-etcd-cluster    # 集群 token
  ports:
    - "2379:2379"                          # 客户端端口
    - "2380:2380"                          # 对等端口
  healthcheck:                             # 健康检查
    test: ["CMD", "etcdctl", "endpoint", "health"]
```

**pg-primary 服务**（主库）：

```yaml
pg-primary:
  image: postgres:16                       # PostgreSQL 16
  environment:
    POSTGRES_USER: postgres                # 超级用户
    POSTGRES_PASSWORD: pg_pass
    POSTGRES_DB: mydb
  entrypoint:
    - bash
    - -c
    - |
      docker-entrypoint.sh postgres &      # 启动 PG
      PG_PID=$$!
      until pg_isready -U postgres; do sleep 1; done  # 等就绪
      psql -U postgres -c "CREATE ROLE replicator WITH REPLICATION LOGIN PASSWORD 'repl_pass';" || true
      psql -U postgres -c "SELECT pg_create_physical_replication_slot('standby_slot');" || true
      wait $$PG_PID
  command:
    - "postgres"
    - "-c"
    - "wal_level=replica"                  # 开启复制
    - "-c"
    - "max_wal_senders=10"
    - "-c"
    - "wal_keep_size=1024MB"
    - "-c"
    - "hot_standby=on"
    - "-c"
    - "synchronous_commit=on"              # 同步复制
```

**pg-standby 服务**（备库）：

```yaml
pg-standby:
  image: postgres:16
  entrypoint:
    - bash
    - -c
    - |
      rm -rf /var/lib/postgresql/data/*
      until pg_isready -h pg-primary -p 5432 -U replicator; do sleep 1; done  # 等主库
      PGPASSWORD=repl_pass pg_basebackup \   # 基础备份
        -h pg-primary -p 5432 -U replicator \
        -D /var/lib/postgresql/data \
        -Fp -Xs -P -R -S standby_slot -C
      echo "primary_conninfo = 'host=pg-primary port=5432 user=replicator password=repl_pass application_name=standby1'" \
        >> /var/lib/postgresql/data/postgresql.auto.conf
      touch /var/lib/postgresql/data/standby.signal  # 标记为备库
      exec docker-entrypoint.sh postgres
  depends_on:
    pg-primary:
      condition: service_healthy           # 等主库健康才启动
```

**pgbouncer 服务**（连接池）：

```yaml
pgbouncer:
  image: edoburu/pgbouncer:1.21.0
  environment:
    DB_USER: postgres
    DB_PASSWORD: pg_pass
    DB_HOST: pg-primary                    # 连主库
    DB_PORT: 5432
    DB_NAME: mydb
    POOL_MODE: transaction                 # 事务级池化
    MAX_CLIENT_CONN: 1000
    DEFAULT_POOL_SIZE: 20
    SERVER_RESET_QUERY: "DISCARD ALL"
  ports:
    - "6432:5432"                          # 客户端连 6432
```

**patroni-primary / patroni-standby 服务**（自动故障转移）：

```yaml
patroni-primary:
  image: harbor.zhihui.com/patroni:3.0.1-pg16
  environment:
    PATRONI_CONFIGURATION: |
      scope: pg-cluster                    # 集群名
      name: patroni-primary
      restapi:
        listen: 0.0.0.0:8008               # REST API
      etcd:
        host: etcd:2379                    # 连 etcd
      bootstrap:
        dcs:
          ttl: 30                          # 租约 30 秒
          maximum_lag_on_failover: 1048576
      # ... 其余配置
  ports:
    - "8008:8008"                          # REST API 端口
    - "5440:5432"                          # PG 端口
```

**haproxy 服务**（负载均衡 + 自动路由）：

```yaml
haproxy:
  image: haproxy:2.8
  volumes:
    - ./haproxy.cfg:/usr/local/etc/haproxy/haproxy.cfg:ro  # 挂载配置
  ports:
    - "5430:5432"                          # 应用连 5430 → 自动路由到主库
    - "8404:8404"                          # HAProxy 监控界面
```

### 9.3 启动与验证

```bash
# 启动所有服务
cd phase2/07-ha-scaling
docker compose up -d

# 查看状态
docker compose ps

# 连主库验证
psql -h localhost -p 5432 -U postgres -d mydb -c "SELECT pg_is_in_recovery();"
#  → f（false，是主库）

# 连备库验证
psql -h localhost -p 5433 -U postgres -d mydb -c "SELECT pg_is_in_recovery();"
#  → t（true，是备库）

# 连 PgBouncer
psql -h localhost -p 6432 -U postgres -d mydb -c "SELECT 1;"

# 连 HAProxy（自动路由到主库）
psql -h localhost -p 5430 -U postgres -d mydb -c "SELECT 1;"

# 查看复制状态
psql -h localhost -p 5432 -U postgres -d mydb -c "
SELECT application_name, state, sync_state,
       pg_wal_lsn_diff(sent_lsn, replay_lsn) AS lag_bytes
FROM pg_stat_replication;"
```

### 9.4 端口对照表

| 端口 | 服务 | 用途 |
|---|---|---|
| 5432 | pg-primary | 直连主库 |
| 5433 | pg-standby | 直连备库 |
| 5430 | haproxy | 自动路由（推荐应用连这个） |
| 6432 | pgbouncer | 连接池 |
| 5440 | patroni-primary | Patroni 管理的 PG（主） |
| 5441 | patroni-standby | Patroni 管理的 PG（备） |
| 8008 | patroni-primary REST | 健康检查 |
| 8009 | patroni-standby REST | 健康检查 |
| 2379 | etcd | DCS 客户端 |
| 2380 | etcd | DCS 对等 |
| 8404 | haproxy | 监控界面 |

---

## 10. Python 脚本详解

项目提供三个 Python 脚本，用 sqlite3 模拟 PostgreSQL 行为，无需真实 PG 即可学习概念。所有脚本位于 `phase2/07-ha-scaling/` 目录。

### 10.1 streaming_replication.py

**用途**：生成流复制所需的配置文件和 SQL 脚本，不实际连接数据库。

**运行方式**：
```bash
cd phase2/07-ha-scaling
python streaming_replication.py
```

**输出文件**（生成在 `output/streaming_replication/` 目录）：

| 文件 | 内容 |
|---|---|
| `primary_postgresql.conf` | 主库 postgresql.conf |
| `primary_pg_hba.conf` | 主库认证配置 |
| `primary_setup.sql` | 创建复制用户的 SQL |
| `standby_postgresql.conf` | 备库 postgresql.conf |
| `standby_postgresql.auto.conf` | 备库连接主库的配置 |
| `standby.signal` | 标记备库的空文件 |
| `standby_basebackup.sh` | 基础备份命令 |
| `replication_status.sql` | 复制状态检查 SQL |
| `promote.sql` | 故障转移 SQL |

**典型输出**：
```
============================================================
PostgreSQL 流复制配置生成完成
============================================================

主库配置：
  postgresql.conf      → .../output/streaming_replication/primary_postgresql.conf
  pg_hba.conf          → .../output/streaming_replication/primary_pg_hba.conf
  setup.sql            → .../output/streaming_replication/primary_setup.sql

备库配置：
  postgresql.conf      → .../output/streaming_replication/standby_postgresql.conf
  postgresql.auto.conf → .../output/streaming_replication/standby_postgresql.auto.conf
  standby.signal       → .../output/streaming_replication/standby.signal
  basebackup.sh        → .../output/streaming_replication/standby_basebackup.sh

状态检查 SQL：
  status.sql           → .../output/streaming_replication/replication_status.sql
  promote.sql          → .../output/streaming_replication/promote.sql

============================================================
部署步骤：
============================================================
    1. 启动主库，加载 primary_postgresql.conf 和 primary_pg_hba.conf
    2. 在主库执行 primary_setup.sql 创建复制用户
    3. 在主库创建复制槽：
       SELECT pg_create_physical_replication_slot('standby_slot');
    4. 在备库机器执行 standby_basebackup.sh 做基础备份
    5. 将 standby_postgresql.conf 和 standby_postgresql.auto.conf
       放入备库 data 目录，创建 standby.signal 文件
    6. 启动备库
    7. 在主库执行 replication_status.sql 验证复制状态
```

**核心函数**：
- `generate_primary_config()`：生成主库配置（postgresql.conf、pg_hba.conf、setup.sql）
- `generate_standby_config()`：生成备库配置（postgresql.conf、auto.conf、standby.signal、basebackup.sh）
- `generate_status_sql()`：生成状态检查和故障转移 SQL
- `print_summary()`：打印部署步骤指引

### 10.2 read_write_split.py

**用途**：用 sqlite3 模拟主备库，演示读写分离路由、延迟回退、会话粘滞、负载均衡。

**运行方式**：
```bash
cd phase2/07-ha-scaling
python read_write_split.py
```

**包含 5 个演示**：

| 演示 | 内容 | 关键类/方法 |
|---|---|---|
| demo_basic_routing | 基础读写分离：写走主库，读走备库 | `ReadWriteRouter.route()` |
| demo_lag_fallback | 备库延迟超阈值时回退主库 | `_pick_standby()` |
| demo_session_sticky | 写后 5 秒内读走主库 | `_is_sticky()` |
| demo_locking_query | SELECT FOR UPDATE 走主库 | `LOCKING_PATTERN` 正则 |
| demo_load_balance | 多备库轮询负载均衡 | `_standby_idx` 轮询 |

**核心类 `ReadWriteRouter`**：

```python
class ReadWriteRouter:
    def __init__(self, primary, standbys,
                 max_lag_seconds=1.0,        # 备库最大延迟
                 max_lag_bytes=1024*1024,    # 备库最大延迟字节
                 session_sticky_seconds=0.0): # 会话粘滞时间
```

**路由决策流程**：
```
SQL 进来
  │
  ├─ 是 BEGIN/START? → 主库（标记进入事务）
  ├─ 是 COMMIT/ROLLBACK? → 主库（标记退出事务）
  ├─ 在事务中? → 主库（保证一致性）
  ├─ 有 FOR UPDATE/SHARE? → 主库（加锁查询）
  ├─ 是写操作? → 主库（记录最后写时间）
  ├─ 在会话粘滞期? → 主库（写后 N 秒内）
  ├─ 是读操作? → 选延迟合格的备库（轮询）
  └─ 默认 → 主库（保守策略）
```

**典型输出**：
```
============================================================
演示 1：基础读写分离路由
============================================================
  [主库] INSERT INTO users (id, name, email) VALUES (1, 'Alice', 'a@x.com')
  [主库] INSERT INTO users (id, name, email) VALUES (2, 'Bob', 'b@x.com')
  [主库] UPDATE users SET email = 'alice@x.com' WHERE id = 1
  [备库(standby1)] SELECT * FROM users WHERE id = 1
  [备库(standby2)] SELECT count(*) FROM users
  [备库(standby1)] SELECT * FROM users WHERE id = 2
  [主库] DELETE FROM users WHERE id = 2
  [备库(standby2)] SELECT count(*) FROM users

路由统计：
  主库查询数: 4
  备库 standby1 查询数: 2
  备库 standby2 查询数: 2
```

**延迟回退演示输出**：
```
演示 2：复制延迟超阈值回退主库
  [主库] INSERT INTO users (id, name, email) VALUES (1, 'Alice', 'a@x.com')

模拟备库延迟 2 秒（超过阈值 0.5s）：
  [警告] 所有备库延迟超阈值，回退到主库
  [主库] SELECT * FROM users WHERE id = 1

模拟备库恢复正常：
  [备库(standby1)] SELECT * FROM users WHERE id = 1
```

### 10.3 sharding_demo.py

**用途**：用 sqlite3 模拟分片，演示哈希分片、范围分片、一致性哈希、跨分片查询、Citus 概念。

**运行方式**：
```bash
cd phase2/07-ha-scaling
python sharding_demo.py
```

**包含 5 个演示**：

| 演示 | 内容 | 关键类/函数 |
|---|---|---|
| demo_hash_sharding | 哈希分片，验证均匀分布 | `hash_shard()`, `ShardedTable` |
| demo_range_sharding | 范围分片，按 user_id 区间 | `range_shard()` |
| demo_consistent_hash | 一致性哈希扩容，验证迁移量 | `ConsistentHashRing` |
| demo_cross_shard_query | 跨分片聚合查询 | `count_all()`, `group_by_user()` |
| demo_citus_concept | Citus 查询路由模拟 | 概念讲解 + 模拟 |

**核心函数**：

```python
def hash_shard(key: int, num_shards: int) -> int:
    """哈希分片：md5(key) % num_shards"""
    h = hashlib.md5(str(key).encode()).hexdigest()
    return int(h, 16) % num_shards

def range_shard(key: int, boundaries: list[int]) -> int:
    """范围分片：按边界划分"""
    for i, bound in enumerate(boundaries):
        if key < bound:
            return i
    return len(boundaries)
```

**一致性哈希环 `ConsistentHashRing`**：
- 每个分片有 150 个虚拟节点（均匀分布）
- key 哈希后顺时针找第一个虚拟节点
- 扩容时只影响新节点前面那段 key

**典型输出**：

```
============================================================
演示 1：哈希分片
============================================================

分片分布（共 4 个分片）：
  分片   行数    min_key    max_key
----------------------------------------
     0   2503          1     100000
     1   2498          2      99999
     2   2501          3     100000
     3   2498          1      99998

总行数: 10000, 平均: 2500, 标准差: 2.24
均衡度: 0.9991 (1.0 = 完美均匀)

跨分片 COUNT(*): 10000
跨分片 SUM(amount): 502345.67

按事件类型聚合（下推 + 汇总）:
  click     : 2512
  login     : 2489
  purchase  : 2498
  view      : 2501

单分片查询 user_id=12345: 2 条（路由到分片 2）
```

**一致性哈希扩容输出**：
```
演示 3：一致性哈希（扩容时最小数据迁移）

扩容前（4 分片）分布:
  分片 0: 2487 条
  分片 1: 2513 条
  分片 2: 2488 条
  分片 3: 2512 条

扩容后（5 分片）分布:
  分片 0: 1998 条
  分片 1: 2010 条
  分片 2: 1990 条
  分片 3: 2015 条
  分片 4: 1987 条

迁移数据量: 2012 / 10000 = 20.1%
理论最优: 20.0%（1/n）
```

> 迁移量 20.1% 接近理论最优 20%，验证了一致性哈希的优势。

**Citus 概念模拟输出**：
```
演示 5：Citus 分布表概念模拟

Q1: SELECT * FROM events WHERE user_id = 123
  → 路由到单分片（分片 = hash(123) % 4 = 3）
  → 等价单机查询，延迟最低

Q2: SELECT count(*) FROM events
  → fan-out 到 4 个分片: count(*)
  → Coordinator 汇总: 8000

Q3: SELECT user_id, count(*) FROM events GROUP BY user_id
  → 每分片本地 GROUP BY → 汇总 4867 组

Q4: SELECT e.* FROM events e JOIN users u ON e.user_id = u.user_id
  → users 是引用表 → 每分片本地 JOIN → 无需网络传输
```

---

## 11. 文件清单

| 文件 | 内容 | 行数 |
|---|---|---|
| `streaming_replication.py` | 流复制配置生成 + 状态检查 | 237 |
| `read_write_split.py` | 读写分离路由 + 复制延迟处理 | 300 |
| `sharding_demo.py` | 哈希/范围分片 + 跨分片查询 | 368 |
| `docker-compose.yml` | PG 主备 + PgBouncer + Patroni 部署 | 283 |

所有文件位于 `phase2/07-ha-scaling/` 目录。Python 脚本用 sqlite3 模拟，无需真实 PostgreSQL 即可运行学习。

---

## 12. 习题

1. **流复制验证**：在 docker-compose 环境中启动主备，用 `streaming_replication.py` 生成配置，验证 `pg_stat_replication` 中 `lag_bytes` 为 0。尝试在主库插入 10000 条数据，观察备库延迟变化。

2. **会话粘滞实现**：修改 `read_write_split.py`，实现会话粘滞：写操作后 5 秒内同会话的读都路由到主库，并用 sqlite3 模拟验证。提示：参考 `demo_session_sticky()` 函数。

3. **分片均匀性验证**：用 `sharding_demo.py` 向 4 个分片插入 10000 条数据，验证数据均匀分布（每分片约 2500 条），并实现跨分片 `COUNT(*)` 聚合。计算均衡度（1 - 标准差/平均值）。

4. **Patroni 故障转移**：配置 Patroni 集群（3 节点 + etcd），手动 kill 主库的 Patroni 进程，观察故障转移过程，记录 RTO。提示：用 `patronictl list` 观察状态变化。

5. **同步 vs 异步性能对比**：对比同步复制和异步复制的写性能：用 `pgbench` 跑 TPS 基准，计算同步复制带来的性能下降百分比。预期同步复制比异步慢 10-30%。

6. **一致性哈希扩容**：用 `sharding_demo.py` 的 `ConsistentHashRing` 模拟从 4 分片扩容到 5、6、7、8 分片，记录每次迁移数据量百分比，验证接近理论最优 1/n。画出迁移率随分片数变化的曲线。

7. **脑裂场景分析**：在 docker-compose 中模拟网络分区：用 `docker network disconnect` 断开主库与 etcd 的网络，观察 Patroni 行为。记录多长时间后触发故障转移，是否有脑裂风险。

8. **读写分离 + 连接池综合**：搭建 PgBouncer + 1 主 2 备的读写分离架构。用 Python 并发 100 个连接跑混合负载（80% 读 + 20% 写），对比有无 PgBouncer 时的数据库连接数和响应延迟。

---

## 附录：常见问题

### Q1：流复制和逻辑复制能同时用吗？

可以。一个备库可以同时是物理复制的 standby 和逻辑复制的订阅端。但通常不会这么做，物理复制用于 HA，逻辑复制用于数据集成，用途不同。

### Q2：Patroni 切换时应用会断连吗？

会短暂断连（通常 1-5 秒）。用 HAProxy + 连接重试可以做到应用基本无感。PgBouncer 在后端切换时会自动重连新主库。

### Q3：Citus 和分库分表中间件（如 MyCat）有什么区别？

Citus 是 PostgreSQL 原生扩展，查询规划器知道分片信息，能做下推优化。中间件是外部代理，只能做简单路由，无法下推聚合。Citus 性能通常更好。

### Q4：同步复制为什么慢？

主库 commit 时必须等备库收到 WAL（甚至应用 WAL）才返回。这个等待增加了写延迟。备库远距离时网络延迟会放大这个问题。跨机房同步复制延迟可能增加 10-50ms。

### Q5：一致性哈希为什么用虚拟节点？

如果不加虚拟节点，环上只有几个点，数据分布会很不均匀。加 150 个虚拟节点后，每个分片在环上有 150 个点，数据分布接近均匀。虚拟节点数越多越均匀，但内存开销也越大。

### Q6：PgBouncer transaction 模式为什么不能用临时表？

transaction 模式下，事务结束就归还连接。临时表是会话级的，事务结束后临时表数据还在，但下次可能拿到不同的后端连接，临时表就"消失"了。需要用 session 模式或把临时表改成正规表。
