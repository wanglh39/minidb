# PostgreSQL 运维 Runbook（操作手册）

> 本手册覆盖日常维护、常见故障排查与紧急操作。所有命令需根据实际环境调整路径、用户、端口。

---

## 0. 环境约定

| 变量 | 示例值 | 说明 |
|---|---|---|
| `PGDATA` | `/var/lib/postgresql/16/main` | 数据目录 |
| `PGARCH` | `/var/lib/postgresql/pg_archive` | WAL 归档目录 |
| `PGBIN` | `/usr/lib/postgresql/16/bin` | 二进制目录 |
| `PGPORT` | `5432` | 端口 |
| `PGUSER` | `postgres` | 超级用户 |
| 备份目录 | `/backup/pg/` | 基础备份与逻辑备份存放 |

```bash
export PGDATA=/var/lib/postgresql/16/main
export PGBIN=/usr/lib/postgresql/16/bin
export PGPORT=5432
export PGUSER=postgres
```

---

## 1. 日常维护清单

### 1.1 每日检查

| 检查项 | 命令/SQL | 预期 |
|---|---|---|
| 实例存活 | `pg_isready -p $PGPORT` | `accepting connections` |
| 复制状态 | `SELECT * FROM pg_stat_replication;` | 从库在线，延迟 < 1MB |
| 归档状态 | `SELECT * FROM pg_stat_archiver;` | `failed_count = 0` |
| 连接数 | `SELECT count(*) FROM pg_stat_activity;` | < 80% × max_connections |
| 锁等待 | `SELECT count(*) FROM pg_locks WHERE NOT granted;` | 0 |
| 死元组 | `SELECT sum(n_dead_tup) FROM pg_stat_user_tables;` | 无异常增长 |
| 磁盘空间 | `df -h $PGDATA` | 使用率 < 80% |
| 备份成功 | 检查备份日志/监控告警 | 最近一次备份成功 |
| 错误日志 | `tail -100 $PGDATA/log/postgresql-$(date +%Y-%m-%d).log` | 无 FATAL/PANIC |

### 1.2 每周检查

| 检查项 | 命令/SQL |
|---|---|
| 表膨胀率 | `SELECT relname, n_dead_tup::float/nullif(n_live_tup,0) FROM pg_stat_user_tables ORDER BY 2 DESC LIMIT 20;` |
| 索引膨胀 | `SELECT * FROM pgstattuple('表名');`（需 pgstattuple 扩展） |
| 慢查询 Top | `SELECT query, mean_exec_time, calls FROM pg_stat_statements ORDER BY mean_exec_time DESC LIMIT 20;` |
| 未使用索引 | `SELECT * FROM pg_stat_user_indexes WHERE idx_scan = 0;` |
| 统计信息新鲜度 | `SELECT relname, last_analyze, last_autoanalyze FROM pg_stat_user_tables;` |
| 备份恢复演练 | 在测试环境恢复最近一次备份并验证 |

### 1.3 每月检查

| 检查项 | 动作 |
|---|---|
| 容量趋势 | 分析过去 30 天数据增长，预测何时达到容量上限 |
| 安全审计 | 运行 `python security_check.py`，修复 ERROR 项 |
| 参数复审 | 对照 workload 调整 `shared_buffers`、`work_mem`、`max_connections` |
| PG 版本安全公告 | 关注 [PostgreSQL 安全公告](https://www.postgresql.org/support/security/) |
| 大版本升级评估 | 若当前版本 EOL 临近，规划升级 |

### 1.4 VACUUM 维护

```sql
-- 查看需要 VACUUM 的表
SELECT schemaname, relname, n_live_tup, n_dead_tup,
       n_dead_tup::float / nullif(n_live_tup, 0) AS dead_ratio
FROM pg_stat_user_tables
WHERE n_dead_tup > 10000
ORDER BY dead_ratio DESC;

-- 手动 VACUUM（不锁表，可在线执行）
VACUUM (ANALYZE, VERBOSE) 表名;

-- VACUUM FULL（锁表重建，回收空间给 OS，谨慎使用）
VACUUM FULL 表名;

-- 对大表分区执行，避免单次 VACUUM FULL 过长
VACUUM (ANALYZE, VERBOSE, INDEX_CLEANUP = ON) 分区表名;
```

> **注意**：`VACUUM FULL` 会获取 `ACCESS EXCLUSIVE` 锁，阻塞所有读写。生产环境应在维护窗口执行，或用 `pg_repack` 在线重建。

---

## 2. 紧急操作清单

### 2.1 磁盘空间告急

**症状**：`df -h` 显示 PG 数据盘使用率 > 95%，可能触发 `ERROR: could not extend file`。

```
排查流程:
1. 确认占用来源
   du -sh $PGDATA/* | sort -rh | head
   du -sh $PGDATA/base/* | sort -rh | head

2. 检查是否 WAL 堆积
   ls -lh $PGDATA/pg_wal/ | wc -l
   SELECT count(*) FROM pg_ls_waldir();

3. 检查归档是否卡住
   SELECT * FROM pg_stat_archiver;

处理（按优先级）:
A. WAL 堆积:
   - 检查 archive_command 是否正常
   - 检查复制槽是否堆积: SELECT * FROM pg_replication_slots;
   - 临时推进复制槽: pg_replication_slot_advance('slot_name', pg_current_wal_lsn())
   - 删除无用复制槽: pg_drop_replication_slot('slot_name')

B. 临时文件堆积:
   - 查看临时文件: SELECT * FROM pg_stat_activity WHERE query LIKE '%temp%';
   - 终止消耗临时空间的查询: SELECT pg_terminate_backend(pid);

C. 表膨胀:
   - 找最大表: SELECT relname, pg_total_relation_size(relid) FROM pg_stat_user_tables ORDER BY 2 DESC LIMIT 10;
   - VACUUM FULL（需维护窗口）或 pg_repack

D. 日志文件过大:
   - 检查 $PGDATA/log 大小
   - 配置 log_rotation_age / log_rotation_size
```

### 2.2 连接数耗尽

**症状**：`FATAL: sorry, too many clients already`。

```
排查流程:
1. 查看当前连接
   SELECT state, count(*) FROM pg_stat_activity GROUP BY state;

2. 找出连接最多的应用/数据库
   SELECT application_name, datname, count(*)
   FROM pg_stat_activity GROUP BY 1, 2 ORDER BY 3 DESC;

处理:
A. 释放 idle 连接:
   SELECT pg_terminate_backend(pid)
   FROM pg_stat_activity
   WHERE state = 'idle' AND query_start < now() - interval '10 minutes';

B. 释放 idle in transaction:
   SELECT pg_terminate_backend(pid)
   FROM pg_stat_activity
   WHERE state = 'idle in transaction'
     AND xact_start < now() - interval '5 minutes';

C. 紧急提高上限（需 reload）:
   ALTER SYSTEM SET max_connections = 300;
   SELECT pg_reload_conf();
   -- 注意: max_connections 需要 restart 才完全生效，但可先清理连接

D. 长期方案:
   - 引入 PgBouncer 连接池
   - 排查应用连接泄漏
   - 设置 idle_in_transaction_session_timeout
```

### 2.3 主库宕机（故障转移）

**症状**：主库不可连接，需将从库提升为新主库。

```
故障转移流程:
1. 确认主库确实不可用（避免脑裂）
   pg_isready -h 主库IP -p $PGPORT
   -- 多次确认，检查主机是否存活

2. 选择最合适的从库提升
   - 复制延迟最小的从库
   - 在各从库执行:
     SELECT pg_wal_replay_lsn(), pg_last_wal_receive_lsn(),
            pg_wal_lsn_diff(pg_last_wal_receive_lsn(), pg_wal_replay_lsn()) AS lag;

3. 提升从库为主库
   -- 方式1: pg_ctl
   $PGBIN/pg_ctl promote -D $PGDATA

   -- 方式2: SQL
   SELECT pg_promote(wait => true, wait_seconds => 60);

4. 验证新主库可写
   psql -h 新主库 -c "SELECT pg_is_in_recovery();"  -- 应返回 false
   psql -h 新主库 -c "CREATE TABLE _failover_test(x int); DROP TABLE _failover_test;"

5. 更新应用连接配置 / DNS / VIP 指向新主库

6. 旧主库恢复后，将其配置为新从库（重建或基于 pg_rewind）
   $PGBIN/pg_rewind --target-pgdata=$PGDATA --source-server='host=新主库 ...'
   修改 primary_conninfo 指向新主库
   $PGBIN/pg_ctl start -D $PGDATA

7. 通知相关团队，记录故障时间线
```

> **关键**：故障转移前必须确认旧主库已真正下线（ fencing），否则会出现双主导致数据分裂。

### 2.4 数据误删恢复

**症状**：`DROP TABLE` / `TRUNCATE` / `DELETE` 误操作。

```
恢复策略（按时间紧迫度）:

策略A: 事务未提交（最幸运）
   -- 在同一事务的另一个连接执行
   SELECT pg_terminate_backend(误操作事务的pid);
   -- 事务被回滚，数据恢复

策略B: 事务已提交，但有 PITR 备份
   1. 立即停止应用写入（避免覆盖 WAL）
   2. 记录误操作前的目标时间点
   3. 按 PITR 流程恢复到新实例（见 08-ops.md §PITR）
   4. 从恢复实例导出误删数据
   5. 导回生产库

策略C: 误删表，但文件未被覆盖
   -- PG 删除表只是删除 catalog，数据文件可能还在
   -- 立即停止 PG，不要重启（重启会清理）
   -- 手动找回文件并重建表（高难度，需专家操作）

策略D: 逻辑复制延迟
   -- 若有逻辑订阅，从订阅端导出数据
```

### 2.5 锁等待风暴

**症状**：大量连接卡在锁等待，业务超时。

```
排查流程:
1. 查看锁等待链
   SELECT
     blocked.pid AS blocked_pid, left(blocked.query, 60) AS blocked_query,
     blocking.pid AS blocking_pid, left(blocking.query, 60) AS blocking_query,
     now() - blocked.query_start AS waited
   FROM pg_stat_activity blocked
   JOIN pg_locks bl ON bl.pid = blocked.pid AND NOT bl.granted
   JOIN pg_locks ul ON ul.locktype = bl.locktype AND ul.relation = bl.relation AND ul.granted
   JOIN pg_stat_activity blocking ON blocking.pid = ul.pid
   ORDER BY waited DESC;

2. 找到锁源头
   -- 通常是某个长事务持有了 ACCESS EXCLUSIVE 锁
   -- 常见: VACUUM FULL / ALTER TABLE / 未提交的 DDL

处理:
A. 终止锁源头事务（谨慎，确认可回滚）
   SELECT pg_terminate_backend(阻塞者pid);

B. 若不能终止，等待其完成
   - 通知业务方降级
   - 监控等待数变化

C. 预防:
   - 设置 lock_timeout 避免无限等待
   - DDL 操作加 IF NOT EXISTS / CONCURRENTLY
   - 长事务拆短
```

### 2.6 复制中断

**症状**：从库复制停止，数据不一致。

```
排查流程:
1. 从库检查
   SELECT * FROM pg_stat_wal_receiver;
   -- status 应为 'streaming'

2. 主库检查
   SELECT * FROM pg_stat_replication;
   -- state 应为 'streaming'

3. 查看从库日志
   tail -100 $PGDATA/log/postgresql-*.log | grep -i 'replication\|wal\|stream'

常见原因与处理:
A. 网络中断:
   - 检查主从网络连通性
   - 恢复后自动重连

B. WAL 已被主库回收（从库断开太久）:
   -- 从库日志: "requested WAL segment has already been removed"
   -- 处理: 用复制槽避免（提前配置 max_replication_slots）
   -- 紧急: 重新做基础备份重建从库

C. 从库数据损坏:
   -- 重新 pg_basebackup 重建

D. 主库 wal_level 不足:
   -- 需重启主库提升 wal_level = replica

E. pg_hba.conf 未允许 replication 连接:
   -- 添加: host replication replicator 0.0.0.0/0 scram-sha-256
```

---

## 3. 常见故障排查

### 3.1 故障：无法连接数据库

```
步骤1: 确认 PG 进程存活
  ps aux | grep postgres
  systemctl status postgresql

步骤2: 确认端口监听
  ss -tlnp | grep 5432
  -- 未监听 → PG 未启动或 listen_addresses 配置错误

步骤3: 检查日志
  tail -200 $PGDATA/log/postgresql-*.log

步骤4: 确认 pg_hba.conf 允许客户端
  grep 客户端IP $PGDATA/pg_hba.conf

步骤5: 确认防火墙
  iptables -L -n | grep 5432
  firewall-cmd --list-ports

步骤6: 确认 SSL/认证方式匹配
  psql "host=... user=... sslmode=require"
```

### 3.2 故障：查询变慢

```
步骤1: 确认是否全局限（所有查询都慢）
  -- 检查系统负载
  top / iostat -x 1 / vmstat 1
  -- 检查 PG 锁等待
  SELECT count(*) FROM pg_locks WHERE NOT granted;

步骤2: 若单查询慢，用 EXPLAIN 分析
  EXPLAIN (ANALYZE, BUFFERS, VERBOSE) SELECT ...;

  关注:
  - Seq Scan（本应 Index Scan）→ 统计信息过期或索引缺失
  - 高 cost 节点 → 需优化
  - loops 很高 → 嵌套循环爆炸
  - rows 估算偏差大 → ANALYZE

步骤3: 检查统计信息
  SELECT relname, last_analyze, n_distinct
  FROM pg_stats WHERE tablename = '慢表';

步骤4: 检查索引是否可用
  SELECT indexrelname, idx_scan FROM pg_stat_user_indexes WHERE relname = '慢表';

步骤5: 检查是否被锁阻塞
  SELECT * FROM pg_stat_activity WHERE state = 'active' AND query LIKE '%慢查询%';

步骤6: 检查是否 IO 瓶颈
  SELECT * FROM pg_stat_database WHERE datname = current_database();
  -- blks_read 激增 → 缓存未命中
```

### 3.3 故障：VACUUM 不生效

```
步骤1: 确认是否有长事务阻止 VACUUM
  SELECT pid, xact_start, now() - xact_start AS age, left(query, 50)
  FROM pg_stat_activity
  WHERE xact_start IS NOT NULL
  ORDER BY age DESC;

  -- 长事务（含 idle in transaction、复制槽、备库查询）会阻止 VACUUM
  -- 回收 xmin 之后的死元组

步骤2: 确认复制槽是否阻止
  SELECT slot_name, xmin, catalog_xmin, active FROM pg_replication_slots;

步骤3: 确认 autovacuum 是否在跑
  SELECT * FROM pg_stat_activity WHERE query LIKE '%autovacuum%';

步骤4: 手动 VACUUM VERBOSE 观察输出
  VACUUM (VERBOSE) 表名;
  -- 关注 "found X removable, Y nonremovable row versions"
  -- 若 removable 但空间未回收，是因为有旧事务仍能看到

步骤5: 处理阻止者
  - 终止不必要的长事务
  - 推进或删除无用复制槽
  - 设置 idle_in_transaction_session_timeout
```

### 3.4 故障：OOM Killed

```
症状: PG 进程被 OS 杀，日志: "Out of memory" / dmesg: "Killed process"

原因排查:
1. work_mem 过大 × 并发数 → 内存爆炸
   SHOW work_mem;
   -- work_mem × (并发查询 × 排序/哈希数) 可能远超物理内存

2. maintenance_work_mem 过大（VACUUM/CREATE INDEX 时）
   SHOW maintenance_work_mem;

3. shared_buffers 设置过高

处理:
1. 调低 work_mem（如 4MB-16MB）
2. 限制并发连接数
3. 配置 OS:
   echo 'vm.overcommit_memory = 2' >> /etc/sysctl.conf
   sysctl -p
4. PG 配置:
   ALTER SYSTEM SET max_connections = 100;  -- 限制并发
   ALTER SYSTEM SET work_mem = '8MB';
```

### 3.5 故障：WAL 归档失败

```
步骤1: 查看归档失败计数
  SELECT archived_count, failed_count, last_failed_wal, last_failed_time
  FROM pg_stat_archiver;

步骤2: 检查归档目录
  ls -lh $PGARCH/
  df -h $PGARCH  -- 归档目录是否满

步骤3: 检查 archive_command
  SHOW archive_command;
  -- 手动执行测试该命令是否可用

步骤4: 查看日志
  grep -i 'archive' $PGDATA/log/postgresql-*.log | tail -50

常见原因:
- 归档目录磁盘满
- archive_command 中目标路径权限不足
- rsync/cp 命令本身失败
- 网络存储不可达

处理:
- 清理归档目录中已恢复不需要的旧 WAL（确认所有从库已接收）
- 修复 archive_command
- 归档会自动重试失败的 WAL
```

---

## 4. 备份与恢复操作

### 4.1 执行逻辑备份

```bash
# 全库自定义压缩格式
pg_dump -h localhost -U postgres -Fc -f /backup/pg/$(date +%Y%m%d_%H%M%S).dump mydb

# 仅 schema
pg_dump -h localhost -U postgres --schema-only -f /backup/pg/schema.sql mydb

# 指定表
pg_dump -h localhost -U postgres -t users -t orders -f /backup/pg/tables.dump mydb

# 并行目录格式（大库）
pg_dump -h localhost -U postgres -Fd -j 4 -f /backup/pg/$(date +%Y%m%d)_dir mydb
```

### 4.2 执行物理备份

```bash
# 确保 WAL 归档已配置
psql -c "SHOW archive_mode;"

# 基础备份
pg_basebackup -h localhost -U replicator -D /backup/pg/base_$(date +%Y%m%d) \
  -Fp -Xs -P -R -c fast

# 压缩 tar 格式
pg_basebackup -h localhost -U replicator -D /backup/pg/base_$(date +%Y%m%d).tar \
  -Ft -Xs -P -z
```

### 4.3 恢复逻辑备份

```bash
# 恢复到新库
createdb restore_db
pg_restore -h localhost -U postgres -d restore_db -j 4 /backup/pg/20260101_020000.dump

# 恢复到现有库（仅数据）
pg_restore -h localhost -U postgres -d mydb --data-only /backup/pg/20260101_020000.dump

# 列出备份内容
pg_restore --list /backup/pg/20260101_020000.dump

# 仅恢复指定表
pg_restore -h localhost -U postgres -d mydb -t users /backup/pg/20260101_020000.dump
```

### 4.4 执行 PITR

```bash
# 1. 停止当前 PG
$PGBIN/pg_ctl -D $PGDATA stop -m fast

# 2. 保存损坏的数据目录
mv $PGDATA $PGDATA.broken.$(date +%s)

# 3. 解压基础备份
mkdir $PGDATA
tar xzf /backup/pg/base_20260101.tar.gz -C $PGDATA

# 4. 写入恢复配置
cat > $PGDATA/postgresql.auto.conf <<EOF
restore_command = 'cp $PGARCH/%f %p'
recovery_target_time = '2026-01-01 14:30:00 +08:00'
recovery_target_action = 'pause'
EOF
touch $PGDATA/recovery.signal

# 5. 启动恢复
$PGBIN/pg_ctl -D $PGDATA start

# 6. 监控恢复进度
psql -c "SELECT * FROM pg_stat_wal_receiver;"
# 查看是否处于恢复中
psql -c "SELECT pg_is_wal_replay_paused();"

# 7. 检查数据正确性后，提升为主库
psql -c "SELECT pg_promote();"
```

---

## 5. 性能应急调参

> 以下参数可通过 `ALTER SYSTEM SET ...; SELECT pg_reload_conf();` 在线生效（无需重启）。

| 场景 | 参数 | 调整 | 说明 |
|---|---|---|---|
| 内存充足，缓存命中低 | `shared_buffers` | 增大 | 需重启 |
| 排序/哈希溢出磁盘 | `work_mem` | 增大 | 注意并发内存 |
| VACUUM/建索引慢 | `maintenance_work_mem` | 增大 | 可临时会话级设置 |
| 并发连接不够 | `max_connections` | 增大 | 需重启，优先用连接池 |
| 长查询卡死 | `statement_timeout` | 设置非 0 | 全局或会话级 |
| idle in txn 堆积 | `idle_in_transaction_session_timeout` | 设置 5-10min | 自动清理 |
| WAL 写入瓶颈 | `wal_compression` | on | 压缩 WAL |
| Checkpoint 太频繁 | `max_wal_size` | 增大 | 减少 IO 抖动 |
| 锁等待无限 | `lock_timeout` | 设置 | 避免雪崩 |

---

## 6. 升级操作

### 6.1 小版本升级（安全补丁）

```bash
# Debian/Ubuntu
apt-get update && apt-get upgrade postgresql-16

# RHEL/CentOS
yum update postgresql16-server

# 升级后重启
systemctl restart postgresql
```

### 6.2 大版本升级（pg_upgrade）

```bash
# 1. 安装新版本（如 17），保留旧版本
# 2. 停止旧版本
$PGBIN_16/pg_ctl -D $PGDATA_16 stop -m fast

# 3. 执行 pg_upgrade（in-place，不复制数据，最快）
$PGBIN_17/pg_upgrade \
  --old-bindir $PGBIN_16 --new-bindir $PGBIN_17 \
  --old-datadir $PGDATA_16 --new-datadir $PGDATA_17 \
  --check  # 先 dry-run

# 4. 确认 check 通过后正式执行
$PGBIN_17/pg_upgrade \
  --old-bindir $PGBIN_16 --new-bindir $PGBIN_17 \
  --old-datadir $PGDATA_16 --new-datadir $PGDATA_17 \
  --link  # 使用硬链接，秒级完成

# 5. 启动新版本
$PGBIN_17/pg_ctl -D $PGDATA_17 start

# 6. 运行 analyze 更新统计信息
$PGBIN_17/vacuumdb --all --analyze-in-stages
```

---

## 7. 联系与升级

| 角色 | 职责 | 联系方式 |
|---|---|---|
| DBA 值班 | 一线响应 | oncall-dba@company |
| DBA 负责人 | 重大决策 | dba-lead@company |
| 基础设施 | 主机/网络/存储 | infra@company |
| 应用负责人 | 确认业务影响 | 各业务 owner |

**升级路径**：一线 DBA → DBA 负责人 → 基础设施 + 应用方联合处置。