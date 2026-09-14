# 章8：数据库运维

> 数据库上线只是开始，长期稳定运行靠的是备份、监控、容量规划与安全加固。本章覆盖 PostgreSQL 生产运维的完整闭环：备份策略、PITR、监控指标、容量规划、安全加固、故障排查与 Runbook。

## 运维全景

```
                    ┌─────────────────────────────────────────┐
                    │            数据库运维闭环                 │
                    └─────────────────────────────────────────┘
                                      │
          ┌──────────────┬───────────┼───────────┬──────────────┐
          ▼              ▼           ▼           ▼              ▼
     ┌─────────┐   ┌─────────┐ ┌─────────┐ ┌─────────┐   ┌─────────┐
     │  备份   │   │  监控   │ │  容量   │ │  安全   │   │  故障   │
     │  恢复   │   │  告警   │ │  规划   │ │  加固   │   │  排查   │
     └────┬────┘   └────┬────┘ └────┬────┘ └────┬────┘   └────┬────┘
          │              │           │           │              │
          ▼              ▼           ▼           ▼              ▼
     pg_dump        连接数       表大小       SSL/TLS       Runbook
     pg_basebackup  缓存命中     索引大小     pg_hba.conf   紧急操作
     WAL 归档       锁等待       增长趋势     角色权限      日常维护
     PITR           死元组                   数据加密
                    复制延迟
```

> **核心原则**：备份不是运维的全部，但没有备份的运维等于零。监控发现问题，容量规划预防问题，安全加固防止灾难，Runbook 标准化响应。

---

## 1. 备份策略

### 1.1 备份方式对比

| 方式 | 工具 | 类型 | 一致性 | 恢复粒度 | 速度 | 适用场景 |
|---|---|---|---|---|---|---|
| **逻辑备份** | `pg_dump` / `pg_dumpall` | SQL 语句 | 事务一致 | 库/表/行 | 慢 | 小库、跨版本迁移、选择性恢复 |
| **物理备份** | `pg_basebackup` | 数据文件 | 块级一致 | 整库 | 快 | 大库、PITR 基础备份 |
| **WAL 归档** | `archive_command` | WAL 文件 | 流式 | 任意时间点 | 持续 | PITR、复制 |
| **快照备份** | LVM / ZFS / EBS | 块设备 | 崩溃一致 | 整库 | 极快 | 云环境、大库 |
| **连续备份** | pgBackRest / Barman | 物理+WAL | 一致 | PITR | 快 | 生产推荐 |

### 1.2 逻辑备份：pg_dump

```bash
# 纯文本格式（人类可读，psql 恢复）
pg_dump -h localhost -U postgres -F p -f backup.sql mydb

# 自定义压缩格式（pg_restore 恢复，支持并行、选择性）
pg_dump -h localhost -U postgres -F c -f backup.dump mydb

# tar 格式
pg_dump -h localhost -U postgres -F t -f backup.tar mydb

# 目录格式 + 并行（大库加速）
pg_dump -h localhost -U postgres -F d -j 4 -f backup_dir/ mydb

# 仅 schema（不含数据）
pg_dump --schema-only -f schema.sql mydb

# 仅数据
pg_dump --data-only -f data.sql mydb

# 指定表
pg_dump -t users -t orders -f tables.dump mydb

# 排除表
pg_dump -T huge_audit_log -f backup.dump mydb

# 全集群（所有库 + 角色 + 表空间）
pg_dumpall -f cluster.sql
```

**格式选择决策树**：

```
需要跨大版本迁移? ──是──> 纯文本 (F p) 或 自定义 (F c)
     │否
需要选择性恢复部分表? ──是──> 自定义 (F c) + pg_restore -t
     │否
库很大 (>100GB)? ──是──> 目录格式 (F d) + 并行 (-j)
     │否
需要人类可读? ──是──> 纯文本 (F p)
     │否
默认选择 ──────────────> 自定义 (F c)
```

### 1.3 物理备份：pg_basebackup

```bash
# 前置配置（postgresql.conf）
# wal_level = replica
# max_wal_senders = 10
# archive_mode = on
# archive_command = 'test ! -f /archive/%f && cp %p /archive/%f'

# 全量基础备份
pg_basebackup -h localhost -U replicator -D /backup/base \
  -Fp -Xs -P -R -c fast

# 参数说明:
#   -Fp   plain 格式（直接拷贝文件）
#   -Xs   stream 方式传输 WAL（不依赖归档）
#   -P    显示进度
#   -R    自动生成 standby.signal + primary_conninfo
#   -c fast  checkpoint 模式（快速，不等待）
```

**物理备份原理**：

```
pg_basebackup 流程:
  1. 向主库发送 BASE_BACKUP 命令
  2. 主库执行 checkpoint，记录起始 WAL 位置
  3. 主库启动 WAL 流式发送（walsender 进程）
  4. 主库逐文件拷贝数据文件到备份端
  5. 备份端接收并写入
  6. 主库发送结束 WAL 位置 + backup_label
  7. 备份端写入 backup_label（恢复时据此定位起点）

恢复时:
  PG 启动 → 读 backup_label → 从起始 WAL 位置 replay → 结合归档 WAL → 至最新
```

### 1.4 WAL 归档

```ini
# postgresql.conf
archive_mode = on
archive_command = 'test ! -f /var/lib/pg_archive/%f && cp %p /var/lib/pg_archive/%f'
archive_timeout = 300s
wal_level = replica
max_wal_senders = 10
max_replication_slots = 10
```

| 参数 | 说明 | 推荐值 |
|---|---|---|
| `archive_mode` | 开启归档 | `on`（主库）/ `always`（主+备） |
| `archive_command` | 归档命令 | `test ! -f ... && cp ...`（防覆盖） |
| `archive_timeout` | 强制切换 WAL 间隔 | `300s`（低写入库需设小） |
| `wal_level` | WAL 信息量 | `replica`（物理复制）/ `logical`（逻辑复制） |
| `max_wal_senders` | WAL 发送进程上限 | 从库数 + 2 |
| `wal_keep_size` | 额外保留 WAL 量 | `1GB`（给从库缓冲） |

```sql
-- 查看归档状态
SELECT archived_count, failed_count, last_archived_wal, last_archived_time,
       last_failed_wal, last_failed_time
FROM pg_stat_archiver;

-- 手动切换 WAL（触发归档）
SELECT pg_switch_wal();

-- 查看当前 WAL 位置
SELECT pg_current_wal_lsn(), pg_walfile_name(pg_current_wal_lsn());
```

### 1.5 备份保留策略

| 备份类型 | 保留周期 | 存储位置 | 说明 |
|---|---|---|---|
| 每日逻辑备份 | 7 天 | 本地 + 异地 | 快速单表恢复 |
| 每周基础备份 | 4 周 | 本地 + 异地 | PITR 基准 |
| WAL 归档 | 30 天 | 异地 | 任意时间点恢复 |
| 月度全量 | 12 个月 | 冷存储 | 长期归档 |
| 年度全量 | 7 年 | 冷存储 | 合规要求 |

> **3-2-1 原则**：至少 3 份副本，2 种介质，1 份异地。

### 1.6 Python 备份演示

```python
# phase2/08-ops/backup_demo.py
# 演示 pg_dump 逻辑备份、pg_basebackup 物理备份、WAL 归档配置、PITR 流程

# 运行方式:
#   export PG_HOST=localhost PG_PORT=5432 PG_DB=postgres PG_USER=postgres PG_PASS=secret
#   python phase2/08-ops/backup_demo.py

# 核心函数:
#   demo_pg_dump_logical()      # 4 种格式逻辑备份
#   demo_pg_basebackup_physical()  # 物理基础备份
#   demo_wal_archive_config()   # WAL 归档配置与状态
#   demo_pitr_recovery()        # PITR 恢复流程
#   demo_restore_from_logical() # 从逻辑备份恢复
#   demo_backup_verification()  # 备份完整性验证
```

---

## 2. PITR（Point-in-Time Recovery）

### 2.1 PITR 原理

```
时间轴 ──────────────────────────────────────────────►
       t0          t1          t2          t3          t4
       │           │           │           │           │
    基础备份     误删表      想恢复到这    发现问题    现在
       │                       │
       └───── replay WAL ──────┘
             (从归档目录读取)

恢复流程:
  1. 停止当前 PG
  2. 用 t0 的基础备份替换数据目录
  3. 配置 restore_command 从归档目录读 WAL
  4. 配置 recovery_target_time = t2
  5. 启动 PG → 自动 replay WAL 从 t0 到 t2
  6. 到达 t2 后 pause（可检查数据）或 promote（提升为可写）
```

### 2.2 恢复目标选项

| 参数 | 说明 | 示例 |
|---|---|---|
| `recovery_target_time` | 恢复到指定时间点 | `'2026-01-01 14:30:00 +08:00'` |
| `recovery_target_lsn` | 恢复到指定 LSN | `'0/50000060'` |
| `recovery_target_xid` | 恢复到指定事务 ID | `'12345'` |
| `recovery_target_name` | 恢复到命名还原点 | `'before_bulk_load'` |
| `recovery_target_action` | 到达目标后行为 | `pause` / `promote` / `shutdown` |

```sql
-- 创建命名还原点（需 superuser）
SELECT pg_create_restore_point('before_important_migration');
```

### 2.3 完整 PITR 操作

```bash
# 步骤1: 停止 PG
$PGBIN/pg_ctl -D $PGDATA stop -m fast

# 步骤2: 保存当前（损坏的）数据目录
mv $PGDATA $PGDATA.broken

# 步骤3: 解压基础备份
mkdir $PGDATA
tar xzf /backup/base_20260101.tar.gz -C $PGDATA

# 步骤4: 写恢复配置
cat > $PGDATA/postgresql.auto.conf <<EOF
restore_command = 'cp /var/lib/pg_archive/%f %p'
recovery_target_time = '2026-01-01 14:30:00 +08:00'
recovery_target_action = 'pause'
EOF
touch $PGDATA/recovery.signal

# 步骤5: 启动恢复
$PGBIN/pg_ctl -D $PGDATA start

# 步骤6: 监控恢复进度（另一终端）
psql -c "SELECT pg_is_in_recovery();"          -- true
psql -c "SELECT * FROM pg_stat_wal_receiver;"

# 步骤7: 恢复暂停后检查数据
psql -c "SELECT count(*) FROM important_table;"

# 步骤8: 确认数据正确，提升为主库
psql -c "SELECT pg_promote();"
```

### 2.4 PITR 注意事项

| 注意点 | 说明 |
|---|---|
| **恢复目标不精确** | `recovery_target_time` 精度到秒，可能恢复到事务中间 |
| **recovery_target_action = pause** | 恢复后暂停在只读状态，便于检查后再决定 promote |
| **恢复后不可回退** | promote 后无法再回到更早时间点，需重新做 PITR |
| **WAL 必须连续** | 归档 WAL 不能有缺口，否则恢复会停在缺口处 |
| **时间戳时区** | `recovery_target_time` 必须带时区，否则用服务器时区 |
| **命名还原点更安全** | `pg_create_restore_point` 比 time 精确 |

---

## 3. 监控指标

### 3.1 关键指标清单

| 类别 | 指标 | 采集 SQL | 告警阈值 |
|---|---|---|---|
| **连接** | 活跃连接数 | `SELECT count(*) FROM pg_stat_activity` | > 80% × max_connections |
| **连接** | idle in transaction | `... WHERE state = 'idle in transaction'` | > 0 持续 5min |
| **缓存** | 缓冲池命中率 | `blks_hit / (blks_hit + blks_read)` | < 99% |
| **锁** | 等待锁数 | `SELECT count(*) FROM pg_locks WHERE NOT granted` | > 0 |
| **事务** | 最长事务时长 | `now() - xact_start` | > 60s |
| **死元组** | 死元组总数 | `SELECT sum(n_dead_tup) FROM pg_stat_user_tables` | 增长趋势异常 |
| **复制** | 字节延迟 | `pg_wal_lsn_diff(sent_lsn, replay_lsn)` | > 16MB |
| **复制** | 回放延迟 | `replay_lag` | > 5s |
| **归档** | 归档失败数 | `pg_stat_archiver.failed_count` | > 0 |
| **磁盘** | 数据盘使用率 | `df -h $PGDATA` | > 80% |
| **WAL** | WAL 目录文件数 | `count(*) FROM pg_ls_waldir()` | 异常增长 |
| **复制槽** | 非活跃槽保留字节 | `pg_wal_lsn_diff(current, restart_lsn)` | 持续增长 |

### 3.2 连接数监控

```sql
-- 按状态分布
SELECT state, count(*) FROM pg_stat_activity GROUP BY state ORDER BY 2 DESC;

-- 按数据库分布
SELECT datname, count(*) FROM pg_stat_activity
WHERE datname IS NOT NULL GROUP BY datname ORDER BY 2 DESC;

-- 按应用分布（需应用设置 application_name）
SELECT application_name, count(*) FROM pg_stat_activity
WHERE application_name != '' GROUP BY 1 ORDER BY 2 DESC;

-- 连接来源 IP
SELECT client_addr, count(*) FROM pg_stat_activity
WHERE client_addr IS NOT NULL GROUP BY 1 ORDER BY 2 DESC;
```

### 3.3 缓存命中率

```sql
-- 数据库级命中率
SELECT datname, blks_hit, blks_read,
       round(blks_hit::numeric / nullif(blks_hit + blks_read, 0), 4) AS hit_ratio
FROM pg_stat_database WHERE datname IS NOT NULL;

-- 表级命中率（需 pg_stat_user_tables）
SELECT relname,
       heap_blks_hit, heap_blks_read,
       round(heap_blks_hit::numeric / nullif(heap_blks_hit + heap_blks_read, 0), 4)
FROM pg_stat_user_tables
WHERE heap_blks_hit + heap_blks_read > 0
ORDER BY 4 ASC LIMIT 20;
```

> **命中率低的原因**：`shared_buffers` 太小、表远大于内存、全表扫描（Seq Scan）绕过缓存、冷数据被频繁访问。

### 3.4 锁等待检测

```sql
-- 锁等待链：谁阻塞了谁
SELECT
    blocked.pid        AS blocked_pid,
    left(blocked.query, 60) AS blocked_query,
    blocking.pid       AS blocking_pid,
    left(blocking.query, 60) AS blocking_query,
    l.locktype,
    l.mode             AS blocked_mode,
    l2.mode            AS blocking_mode,
    now() - blocked.query_start AS waited
FROM pg_locks l
JOIN pg_stat_activity blocked  ON blocked.pid = l.pid
JOIN pg_locks l2 ON l2.locktype = l.locktype
                AND l2.relation = l.relation
                AND l2.granted AND l2.pid != l.pid
JOIN pg_stat_activity blocking ON blocking.pid = l2.pid
WHERE NOT l.granted
ORDER BY waited DESC;
```

**锁兼容矩阵（部分）**：

| 请求 \ 持有 | AccessShare | RowExclusive | AccessExclusive |
|---|---|---|---|
| **AccessShare** | ✅ | ✅ | ❌ |
| **RowExclusive** | ✅ | ✅ | ❌ |
| **Share** | ✅ | ❌ | ❌ |
| **AccessExclusive** | ❌ | ❌ | ❌ |

> `SELECT` 持有 AccessShare，`INSERT/UPDATE/DELETE` 持有 RowExclusive，`TRUNCATE/VACUUM FULL/ALTER` 持有 AccessExclusive。

### 3.5 死元组监控

```sql
-- 死元组堆积
SELECT schemaname, relname, n_live_tup, n_dead_tup,
       round(n_dead_tup::numeric / nullif(n_live_tup, 0), 4) AS dead_ratio,
       last_autovacuum
FROM pg_stat_user_tables
WHERE n_dead_tup > 0
ORDER BY n_dead_tup DESC;

-- autovacuum 是否跟上
SELECT relname, n_dead_tup,
       n_dead_tup > autovacuum_vacuum_threshold
       + autovacuum_vacuum_scale_factor * n_live_tup AS need_vacuum
FROM pg_stat_user_tables, (
    SELECT setting::int AS autovacuum_vacuum_threshold
    FROM pg_settings WHERE name = 'autovacuum_vacuum_threshold'
) t1, (
    SELECT setting::float AS autovacuum_vacuum_scale_factor
    FROM pg_settings WHERE name = 'autovacuum_vacuum_scale_factor'
) t2;
```

### 3.6 复制延迟监控

```sql
-- 主库视角：各从库延迟
SELECT application_name, client_addr, state, sync_state,
       write_lag, flush_lag, replay_lag,
       pg_wal_lsn_diff(sent_lsn, replay_lsn) AS byte_lag
FROM pg_stat_replication
ORDER BY byte_lag DESC;

-- 从库视角：自身接收/回放状态
SELECT status, receive_start_lsn, write_lsn, flush_lsn, replay_lsn,
       write_lag, flush_lag, replay_lag
FROM pg_stat_wal_receiver;

-- 复制槽保留的 WAL（非活跃槽会无限堆积）
SELECT slot_name, active, restart_lsn,
       pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) AS retained_bytes
FROM pg_replication_slots;
```

**复制延迟原因排查**：

```
延迟增大
  ├─ 从库负载高（大查询占用资源）→ 查 pg_stat_activity
  ├─ 从库回放被长查询阻塞（hot_standby_feedback）→ 查从库长查询
  ├─ 网络带宽不足 → 查网络吞吐
  ├─ 主库写入突增 → 查主库 WAL 生成速率
  └─ 从库磁盘 IO 瓶颈 → iostat
```

### 3.7 Checkpoint 统计

```sql
SELECT buffers_checkpoint, buffers_backend, buffers_clean,
       checkpoints_req, checkpoints_timed,
       round(buffers_checkpoint::numeric /
             nullif(buffers_checkpoint + buffers_backend + buffers_clean, 0), 4) AS ckpt_ratio
FROM pg_stat_bgwriter;
```

| 指标 | 含义 | 理想 |
|---|---|---|
| `buffers_checkpoint` | checkpoint 写入缓冲数 | 占比高（>90%） |
| `buffers_backend` | backend 自己写缓冲数 | 占比低（<10%） |
| `checkpoints_req` | 请求的 checkpoint（WAL 满触发） | 少 |
| `checkpoints_timed` | 定时 checkpoint | 多 |

> `buffers_backend` 占比高说明 checkpoint 不够频繁，backend 被迫自己刷脏页，导致查询延迟抖动。调大 `max_wal_size` 或 `checkpoint_timeout`。

### 3.8 Python 监控脚本

```python
# phase2/08-ops/monitoring.py
# 收集 9 大类指标：连接、缓存、锁、长事务、死元组、复制、表空间、慢查询、checkpoint

# 运行方式:
#   export PG_HOST=localhost PG_PORT=5432 PG_DB=postgres PG_USER=postgres PG_PASS=secret
#   export LONG_TXN_THRESHOLD=60
#   python phase2/08-ops/monitoring.py

# 输出示例:
#   === 1. 连接数指标 ===
#     最大连接数:     100
#     当前活跃连接:   12  (12.0%)
#   === 3. 锁等待检测 ===
#     [OK] 当前无锁等待
#   === 6. 复制延迟监控 ===
#     [信息] 当前无流复制从库
```

---

## 4. 容量规划

### 4.1 表与索引大小

```sql
-- 表大小（含 toast、索引）
SELECT schemaname, relname,
       pg_size_pretty(pg_relation_size(relid))     AS table_size,
       pg_size_pretty(pg_indexes_size(relid))      AS index_size,
       pg_size_pretty(pg_total_relation_size(relid)) AS total_size,
       n_live_tup
FROM pg_stat_user_tables
ORDER BY pg_total_relation_size(relid) DESC LIMIT 20;

-- 单表详细
SELECT
    pg_size_pretty(pg_relation_size('orders'))       AS heap_size,
    pg_size_pretty(pg_table_size('orders'))          AS table_size,
    pg_size_pretty(pg_indexes_size('orders'))        AS index_size,
    pg_size_pretty(pg_total_relation_size('orders')) AS total_size;

-- 索引大小 Top
SELECT schemaname, indexrelname, relname,
       pg_size_pretty(pg_relation_size(indexrelid)) AS index_size,
       idx_scan
FROM pg_stat_user_indexes
ORDER BY pg_relation_size(indexrelid) DESC LIMIT 20;
```

**空间构成**：

```
pg_total_relation_size = pg_relation_size (堆表)
                       + pg_indexes_size  (所有索引)
                       + toast 表 + toast 索引

pg_table_size = pg_relation_size + toast 表
```

### 4.2 增长趋势

```sql
-- 方法1：定期采样记录表大小（推荐建表存储）
CREATE TABLE IF NOT EXISTS ops.growth_sample (
    ts        timestamptz DEFAULT now(),
    relid     oid,
    size      bigint
);

-- 每小时采样（cron）
INSERT INTO ops.growth_sample (relid, size)
SELECT relid, pg_total_relation_size(relid)
FROM pg_stat_user_tables;

-- 计算日均增长
SELECT relname,
       max(size) - min(size) AS growth_bytes,
       pg_size_pretty(max(size) - min(size)) AS growth_pretty,
       round((max(size) - min(size))::numeric /
             nullif(extract(epoch FROM max(ts) - min(ts))/86400, 0), 2) AS bytes_per_day,
       pg_size_pretty(((max(size) - min(size)) /
             nullif(extract(epoch FROM max(ts) - min(ts))/86400, 0))::bigint) AS per_day
FROM ops.growth_sample s
JOIN pg_class c ON c.oid = s.relid
GROUP BY c.relname
ORDER BY 2 DESC;
```

```sql
-- 方法2：利用 pg_stat_user_tables 的估算
-- n_live_tup × 平均行大小 ≈ 表大小
SELECT relname, n_live_tup,
       pg_total_relation_size(relid) / nullif(n_live_tup, 0) AS avg_row_bytes,
       pg_size_pretty(pg_total_relation_size(relid)) AS current_size
FROM pg_stat_user_tables
WHERE n_live_tup > 0
ORDER BY pg_total_relation_size(relid) DESC;
```

### 4.3 容量预测

```
预测公式:
  当前大小 + 日均增长 × 天数 = 未来大小

示例:
  当前 500GB，日均增长 2GB，预测 90 天后:
  500 + 2 × 90 = 680GB

  磁盘 1TB，使用率阈值 80% (800GB):
  (800 - 500) / 2 = 150 天后触达阈值

规划动作:
  - < 30 天触达 → 立即扩容或清理
  - 30-90 天触达 → 规划扩容窗口
  - > 90 天触达 → 纳入季度规划
```

### 4.4 索引空间优化

```sql
-- 未使用的索引（占用空间但无扫描）
SELECT schemaname, indexrelname, relname,
       pg_size_pretty(pg_relation_size(indexrelid)) AS index_size,
       idx_scan
FROM pg_stat_user_indexes
WHERE idx_scan = 0
  AND schemaname NOT IN ('pg_catalog', 'information_schema')
ORDER BY pg_relation_size(indexrelid) DESC;

-- 重复索引
SELECT pg_size_pretty(pg_relation_size(idx1.indexrelid)) AS size,
       idx1.relname, idx1.indexrelname, idx2.indexrelname
FROM pg_stat_user_indexes idx1
JOIN pg_stat_user_indexes idx2
  ON idx1.relid = idx2.relid
  AND idx1.indexrelid < idx2.indexrelid
  AND pg_get_indexdef(idx1.indexrelid) = pg_get_indexdef(idx2.indexrelid);
```

> **注意**：`idx_scan = 0` 不一定意味着索引无用（可能是唯一约束或外键约束）。删除前确认非约束依赖。

### 4.5 表空间与分区规划

```sql
-- 表空间使用
SELECT pg_tablespace_name(oid), pg_tablespace_location(oid)
FROM pg_tablespace;

-- 分区表大小分布
SELECT relid::regclass AS partition,
       pg_size_pretty(pg_total_relation_size(relid)) AS size,
       n_live_tup
FROM pg_stat_user_tables
WHERE relid IN (
    SELECT inhrelid FROM pg_inherits
    WHERE inhparent = 'orders'::regclass
)
ORDER BY pg_total_relation_size(relid) DESC;
```

**分区策略与容量**：

| 分区方式 | 适用 | 容量优势 |
|---|---|---|
| **范围分区** | 时间序列数据 | 老分区可整体归档/删除，瞬时释放空间 |
| **列表分区** | 地域/租户 | 按租户独立管理 |
| **哈希分区** | 均匀分布 | 写入分散到多表空间 |

---

## 5. 安全加固

### 5.1 SSL/TLS 配置

```ini
# postgresql.conf
ssl = on
ssl_cert_file = '/etc/postgresql/ssl/server.crt'
ssl_key_file  = '/etc/postgresql/ssl/server.key'
ssl_ca_file   = '/etc/postgresql/ssl/ca.crt'
ssl_min_protocol_version = 'TLSv1.2'
ssl_ciphers = 'HIGH:MEDIUM:+3DES:!aNULL'
```

```bash
# 生成自签名证书（测试用，生产用 CA 签发）
openssl req -new -x509 -days 365 -nodes \
  -text -out server.crt \
  -keyout server.key \
  -subj "/CN=pg.example.com"

chmod 600 server.key
chown postgres:postgres server.key server.crt
```

```sql
-- 验证 SSL 连接
SELECT ssl, ssl_version, ssl_cipher
FROM pg_stat_ssl JOIN pg_stat_activity USING (pid)
WHERE pid = pg_backend_pid();
```

### 5.2 pg_hba.conf 配置

```conf
# TYPE  DATABASE    USER        ADDRESS          METHOD          OPTIONS
# 本地连接
local   all         postgres                    peer
local   all         all                         scram-sha-256

# 复制连接（仅限复制用户）
host    replication replicator  10.0.0.0/8       scram-sha-256

# 应用连接（限制来源网段）
host    appdb       appuser     10.0.1.0/24      scram-sha-256   sslmode=require

# 管理连接（仅限跳板机）
host    all         dba         10.0.0.5/32      scram-sha-256   sslmode=require

# 拒绝其他
host    all         all         0.0.0.0/0        reject
```

**认证方式对比**：

| 方式 | 安全性 | 说明 |
|---|---|---|
| `trust` | ❌ 极危险 | 免密登录，仅限本地调试 |
| `password` | ❌ 危险 | 明文传输密码 |
| `md5` | ⚠️ 一般 | 哈希传输，易受离线破解 |
| `scram-sha-256` | ✅ 推荐 | PG 13+ 默认，加盐挑战响应 |
| `peer` | ✅ 限定本地 | 依赖 OS 用户映射 |
| `cert` | ✅ 强 | 客户端证书认证 |
| `ldap` / `radius` | ✅ 集中 | 对接企业认证 |

### 5.3 角色权限

```sql
-- 遵循最小权限原则
-- 1. 应用用户：仅授予所需库表的权限
CREATE ROLE appuser LOGIN PASSWORD '...' CONNECTION LIMIT 50;
GRANT CONNECT ON DATABASE appdb TO appuser;
GRANT USAGE ON SCHEMA app TO appuser;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA app TO appuser;
ALTER DEFAULT PRIVILEGES IN SCHEMA app
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO appuser;

-- 2. 只读用户（报表/分析）
CREATE ROLE reader LOGIN PASSWORD '...';
GRANT CONNECT ON DATABASE appdb TO reader;
GRANT USAGE ON SCHEMA app TO reader;
GRANT SELECT ON ALL TABLES IN SCHEMA app TO reader;

-- 3. 复制用户（仅复制权限）
CREATE ROLE replicator REPLICATION LOGIN PASSWORD '...';

-- 4. DBA（受限超管，不用真实 superuser）
CREATE ROLE dba LOGIN PASSWORD '...';
GRANT pg_signal_backend TO dba;
GRANT pg_rotate_logfile TO dba;
-- 避免直接给 superuser，用 pg_signal_backend 等细粒度角色

-- 检查权限
SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls
FROM pg_roles ORDER BY rolname;
```

**权限层级**：

```
superuser (最高，避免日常使用)
  └─ createdb + createrole + replication + bypassrls (DBA)
       └─ schema owner (建表建索引)
            └─ table writer (DML)
                 └─ table reader (SELECT)
                      └─ CONNECT only (仅登录)
```

### 5.4 密码策略

```sql
-- 设置密码加密方式
ALTER SYSTEM SET password_encryption = 'scram-sha-256';
SELECT pg_reload_conf();

-- 设置密码有效期
ALTER ROLE appuser VALID UNTIL '2026-12-31';

-- 修改密码（会自动用 password_encryption 加密）
ALTER ROLE appuser PASSWORD 'new_strong_password';

-- 查看密码类型
SELECT rolname,
       CASE
           WHEN rolpassword LIKE 'SCRAM-SHA-256%' THEN 'scram-sha-256'
           WHEN rolpassword LIKE 'md5%' THEN 'md5'
           ELSE 'other'
       END AS pwd_type
FROM pg_authid WHERE rolcanlogin;
```

```sql
-- 密码复杂度（需 password_check_hook 或外部工具）
-- PG 14+ 可用 password_check_hook
-- 或用 pgcrypto + 触发器自实现
```

### 5.5 数据加密

| 层级 | 方式 | 透明度 | 性能 |
|---|---|---|---|
| **磁盘级** | LUKS / dm-crypt / EBS 加密 | 对 PG 透明 | 内核级，开销小 |
| **文件系统级** | ZFS 加密 / eCryptfs | 对 PG 透明 | 中等 |
| **列级** | pgcrypto 扩展 | 应用需改 SQL | 函数调用开销 |
| **传输级** | SSL/TLS | 对 PG 透明 | 握手开销 |

```sql
-- pgcrypto 列级加密
CREATE EXTENSION pgcrypto;

CREATE TABLE users (
    id       serial PRIMARY KEY,
    name     text,
    ssn      bytea  -- 加密存储
);

-- 写入（应用持有密钥）
INSERT INTO users (name, ssn)
VALUES ('alice', encrypt('123-45-6789', 'secret_key', 'aes'));

-- 读取
SELECT name, convert_from(decrypt(ssn, 'secret_key', 'aes'), 'utf8') AS ssn
FROM users;

-- 不可逆加密（密码哈希）
SELECT crypt('user_password', gen_salt('bf', 8));  -- bcrypt
-- 验证
SELECT crypt('input', stored_hash) = stored_hash AS matched;
```

### 5.6 审计日志

```ini
# postgresql.conf
log_connections = on
log_disconnections = on
log_statement = 'ddl'              # none|ddl|mod|all
log_min_duration_statement = 1000  # 记录 >1s 的慢查询
log_lock_waits = on
log_temp_files = 0                 # 记录所有临时文件使用
log_checkpoints = on
log_line_prefix = '%m [%p] %u@%d %h '
log_directory = 'log'
log_rotation_age = 1d
log_rotation_size = 100MB
log_truncate_on_rotation = on
```

| `log_statement` | 记录内容 | 日志量 |
|---|---|---|
| `none` | 不记录 SQL | 最小 |
| `ddl` | DDL（CREATE/ALTER/DROP） | 小 |
| `mod` | DDL + DML（INSERT/UPDATE/DELETE） | 中 |
| `all` | 所有 SQL | 大 |

> 生产环境推荐 `ddl` + `log_min_duration_statement`，避免 `all` 导致日志量爆炸。

### 5.7 行级安全（RLS）

```sql
-- 启用 RLS
ALTER TABLE orders ENABLE ROW LEVEL SECURITY;
-- 强制 RLS（对表 owner 也生效）
ALTER TABLE orders FORCE ROW LEVEL SECURITY;

-- 策略：用户只能看自己的订单
CREATE POLICY orders_isolation ON orders
    FOR SELECT
    USING (user_id = current_setting('app.user_id')::int);

-- 应用设置当前用户
SET app.user_id = 42;
SELECT * FROM orders;  -- 仅返回 user_id = 42 的行
```

### 5.8 Python 安全检查

```python
# phase2/08-ops/security_check.py
# 检查 8 大安全维度：SSL、pg_hba、用户权限、密码策略、连接安全、数据加密、审计日志、RLS

# 运行方式:
#   export PG_HOST=localhost PG_PORT=5432 PG_DB=postgres PG_USER=postgres PG_PASS=secret
#   python phase2/08-ops/security_check.py

# 输出:
#   [OK]   SSL 已开启
#   [ERROR] 存在 trust 认证（免密登录），极度危险
#   [WARN] 未安装 pgcrypto 扩展
#   ...
#   === 安全检查报告 ===
#     总检查项: 25
#     通过:     18
#     警告:     5
#     错误:     2
```

---

## 6. 常见故障与排查

### 6.1 故障速查表

| 症状 | 可能原因 | 首步排查 |
|---|---|---|
| 连接被拒 | PG 未启动 / 监听地址 / pg_hba | `pg_isready`、日志 |
| `too many clients` | 连接耗尽 | `SELECT count(*) FROM pg_stat_activity` |
| 查询变慢 | 统计信息过期 / 锁等待 / IO 瓶颈 | `EXPLAIN ANALYZE` |
| 磁盘满 | WAL 堆积 / 表膨胀 / 归档失败 | `df -h`、`pg_stat_archiver` |
| 主从延迟 | 从库负载 / 网络 / 大事务 | `pg_stat_replication` |
| OOM Killed | work_mem × 并发过大 | `dmesg`、`work_mem` |
| VACUUM 不生效 | 长事务 / 复制槽 / idle in txn | `pg_stat_activity` |
| 恢复卡住 | WAL 缺口 / 归档目录不可达 | `pg_stat_wal_receiver`、日志 |

### 6.2 连接被拒排查流程

```
无法连接
  ├─ pg_isready 返回拒绝? ──是──> PG 未启动 / 启动失败
  │                              ├─ 查日志: tail $PGDATA/log/*.log
  │                              ├─ 查端口: ss -tlnp | grep 5432
  │                              └─ 查配置: postgresql.conf 语法
  │
  ├─ pg_isready 接受但 psql 报认证失败?
  │                              ├─ pg_hba.conf 是否允许该 IP/用户
  │                              ├─ 认证方式是否匹配（md5 vs scram）
  │                              ├─ 密码是否正确
  │                              └─ SSL 配置是否匹配（sslmode）
  │
  └─ 网络层?
                                 ├─ telnet host 5432
                                 ├─ 防火墙: iptables / firewalld
                                 └─ DNS 解析
```

### 6.3 查询变慢排查流程

```
查询变慢
  ├─ 是全局限速（所有查询都慢）?
  │   ├─ 系统负载: top / iostat / vmstat
  │   ├─ 锁等待: SELECT count(*) FROM pg_locks WHERE NOT granted
  │   ├─ 连接数: SELECT count(*) FROM pg_stat_activity
  │   └─ 检查是否在 checkpoint: pg_stat_bgwriter
  │
  └─ 是单查询慢?
      ├─ EXPLAIN (ANALYZE, BUFFERS) SELECT ...
      │   ├─ Seq Scan 本应 Index Scan? → ANALYZE 或建索引
      │   ├─ rows 估算偏差大? → ANALYZE
      │   ├─ 高 cost 节点? → 优化 SQL 或索引
      │   └─ Buffers: shared hit 低? → 增 shared_buffers
      ├─ 是否被锁阻塞? → pg_locks + pg_stat_activity
      ├─ 是否等待 IO? → iostat 查看 await
      └─ 是否临时文件溢出? → log_temp_files
```

### 6.4 磁盘满排查流程

```
磁盘满 (df -h > 95%)
  ├─ du -sh $PGDATA/* 定位占用
  │
  ├─ pg_wal/ 占用大?
  │   ├─ 归档失败: SELECT * FROM pg_stat_archiver
  │   ├─ 复制槽堆积: SELECT * FROM pg_replication_slots
  │   ├─ max_wal_size 太大: SHOW max_wal_size
  │   └─ 处理: 修复归档 / 推进复制槽 / pg_switch_wal()
  │
  ├─ base/ (数据文件) 占用大?
  │   ├─ 找最大表: SELECT relname, pg_total_relation_size(relid) ...
  │   ├─ 表膨胀: VACUUM FULL 或 pg_repack
  │   └─ 正常增长: 扩容或分区归档
  │
  └─ log/ 占用大?
      └─ 配置 log_rotation_age / log_rotation_size
```

### 6.5 复制延迟排查流程

```
复制延迟 (byte_lag 持续增长)
  ├─ 从库负载高?
  │   ├─ 从库长查询: SELECT * FROM pg_stat_activity WHERE state='active'
  │   ├─ 从库 IO: iostat -x 1
  │   └─ 处理: 终止非必要查询 / 从库扩容
  │
  ├─ 主库写入突增?
  │   ├─ 查 WAL 生成速率: pg_wal_lsn_diff(pg_current_wal_lsn(), 上次采样)
  │   └─ 等写入峰值过去
  │
  ├─ 网络?
  │   ├─ 带宽测试: iperf3
  │   └─ 处理: 升级带宽 / 压缩 WAL (wal_compression=on)
  │
  └─ 从库回放被阻塞?
      ├─ hot_standby_feedback = on 时，从库长查询会阻止主库 VACUUM
      ├─ max_standby_streaming_delay 设置
      └─ 终止从库长查询
```

---

## 7. Runbook（操作手册）

> 完整操作手册见 `phase2/08-ops/runbook.md`，此处为摘要。

### 7.1 日常维护清单

| 频率 | 检查项 | 命令 |
|---|---|---|
| 每日 | 实例存活 | `pg_isready` |
| 每日 | 复制状态 | `SELECT * FROM pg_stat_replication` |
| 每日 | 归档状态 | `SELECT * FROM pg_stat_archiver` |
| 每日 | 连接数 | `SELECT count(*) FROM pg_stat_activity` |
| 每日 | 锁等待 | `SELECT count(*) FROM pg_locks WHERE NOT granted` |
| 每日 | 磁盘空间 | `df -h $PGDATA` |
| 每周 | 死元组 | `SELECT sum(n_dead_tup) FROM pg_stat_user_tables` |
| 每周 | 慢查询 Top | `pg_stat_statements` |
| 每周 | 未使用索引 | `pg_stat_user_indexes WHERE idx_scan = 0` |
| 每月 | 容量趋势 | 分析增长曲线 |
| 每月 | 安全审计 | `python security_check.py` |
| 每月 | 备份恢复演练 | 在测试环境恢复 |

### 7.2 紧急操作清单

| 场景 | 首步动作 | 参考 |
|---|---|---|
| 磁盘满 | 定位占用来源，清理 WAL/日志/膨胀 | runbook §2.1 |
| 连接耗尽 | 终止 idle 连接，提高上限 | runbook §2.2 |
| 主库宕机 | 确认宕机，提升从库 | runbook §2.3 |
| 数据误删 | 停写入，PITR 恢复 | runbook §2.4 |
| 锁等待风暴 | 找锁源头，终止或等待 | runbook §2.5 |
| 复制中断 | 查从库状态，重建或修复 | runbook §2.6 |

---

## 8. 与 miniDB 的对照

| 运维维度 | miniDB | PostgreSQL | 差异原因 |
|---|---|---|---|
| **备份** | 单文件拷贝 | pg_dump / pg_basebackup / WAL 归档 | PG 需支持在线、PITR、选择性 |
| **恢复** | 重启加载 | PITR / pg_restore / 流复制切换 | PG 需任意时间点恢复 |
| **监控** | 无 | 9 大类指标 + 扩展 | PG 是生产级 C/S |
| **安全** | 无 | SSL / pg_hba / RLS / 审计 | PG 面向网络多用户 |
| **高可用** | 无 | 流复制 + 故障转移 | miniDB 是嵌入式 |
| **VACUUM** | 无 | autovacuum + 手动 | PG 的 MVCC 需清理死元组 |
| **连接管理** | 无 | max_connections + 连接池 | miniDB 单进程 |

---

## 9. 文件清单

| 文件 | 内容 |
|---|---|
| `backup_demo.py` | pg_dump/pg_basebackup/WAL 归档/PITR 演示 |
| `monitoring.py` | 9 大类监控指标采集 |
| `security_check.py` | 8 大维度安全检查 |
| `runbook.md` | 操作手册（日常/紧急/故障排查） |

---

## 10. 习题

1. **备份对比**：对同一数据库分别用 `pg_dump -F p`、`-F c`、`-F d -j 4` 执行备份，对比耗时、产物大小、恢复方式。

2. **PITR 演练**：在测试库执行基础备份 → 开启 WAL 归档 → 插入数据 → 记录时间点 t1 → 误删表 → 用 PITR 恢复到 t1 → 验证数据。

3. **监控脚本扩展**：在 `monitoring.py` 基础上增加"索引使用率"指标，找出 `idx_scan = 0` 且体积大于 100MB 的索引。

4. **锁等待复现**：在两个终端分别执行 `BEGIN; LOCK TABLE t IN ACCESS EXCLUSIVE MODE;` 和 `SELECT * FROM t;`，用监控脚本观察锁等待链，理解锁兼容矩阵。

5. **安全加固**：运行 `security_check.py`，修复所有 ERROR 项，包括移除 trust 认证、启用 SSL、设置 scram-sha-256、配置密码有效期。

6. **容量预测**：为一张增长表建立 `growth_sample` 采样表，连续采样 7 天，计算日均增长，预测 90 天后大小，判断是否需要扩容。

7. **故障转移演练**：搭建一主一从测试环境，模拟主库宕机（`pg_ctl stop -m immediate`），执行从库提升（`pg_promote`），验证新主库可写，再用 `pg_rewind` 将旧主库配置为新从库。

8. **VACUUM 调优**：构造一张频繁更新的表，观察 `n_dead_tup` 增长，调整 `autovacuum_vacuum_scale_factor` 使 autovacuum 更早触发，对比调优前后的死元组水平。