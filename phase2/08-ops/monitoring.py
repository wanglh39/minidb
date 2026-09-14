"""PostgreSQL 监控脚本：关键指标收集、锁等待检测、长事务检测、复制延迟、表空间使用"""
import os
import sys
import time
from collections import defaultdict

import psycopg2
from psycopg2.extras import RealDictCursor


def get_conn():
    return psycopg2.connect(
        host=os.getenv("PG_HOST", "localhost"),
        port=os.getenv("PG_PORT", "5432"),
        dbname=os.getenv("PG_DB", "postgres"),
        user=os.getenv("PG_USER", "postgres"),
        password=os.getenv("PG_PASS", "secret"),
    )


def section(title):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def collect_connection_metrics():
    section("1. 连接数指标")
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SHOW max_connections")
    max_conn = int(cur.fetchone()[0])

    cur.execute("""
        SELECT state, count(*) as cnt
        FROM pg_stat_activity
        GROUP BY state
        ORDER BY cnt DESC
    """)
    by_state = cur.fetchall()
    total = sum(r[1] for r in by_state)

    print(f"  最大连接数:     {max_conn}")
    print(f"  当前活跃连接:   {total}  ({total / max_conn * 100:.1f}%)")
    print(f"  空闲连接池余量: {max_conn - total}")
    print()
    print(f"  {'状态':<20} {'数量':>8} {'占比':>8}")
    print(f"  {'-' * 38}")
    for state, cnt in by_state:
        pct = cnt / total * 100 if total > 0 else 0
        print(f"  {str(state):<20} {cnt:>8} {pct:>7.1f}%")

    cur.execute("""
        SELECT datname, count(*) as cnt
        FROM pg_stat_activity
        WHERE datname IS NOT NULL
        GROUP BY datname
        ORDER BY cnt DESC
        LIMIT 10
    """)
    by_db = cur.fetchall()
    print()
    print(f"  按数据库分布（Top 10）:")
    print(f"  {'数据库':<25} {'连接数':>8}")
    print(f"  {'-' * 35}")
    for db, cnt in by_db:
        print(f"  {db:<25} {cnt:>8}")

    cur.execute("""
        SELECT application_name, count(*) as cnt
        FROM pg_stat_activity
        WHERE application_name IS NOT NULL AND application_name != ''
        GROUP BY application_name
        ORDER BY cnt DESC
        LIMIT 10
    """)
    by_app = cur.fetchall()
    if by_app:
        print()
        print(f"  按应用分布（Top 10）:")
        print(f"  {'应用':<30} {'连接数':>8}")
        print(f"  {'-' * 40}")
        for app, cnt in by_app:
            print(f"  {app:<30} {cnt:>8}")

    conn.close()
    return {"max_connections": max_conn, "active": total, "by_state": by_state}


def collect_cache_metrics():
    section("2. 缓存命中率")
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT datname,
               blks_hit,
               blks_read,
               round(blks_hit::numeric / nullif(blks_hit + blks_read, 0), 4) as hit_ratio
        FROM pg_stat_database
        WHERE datname IS NOT NULL
        ORDER BY (blks_hit + blks_read) DESC
    """)
    rows = cur.fetchall()
    print(f"  {'数据库':<20} {'命中':>12} {'未命中':>12} {'命中率':>10}")
    print(f"  {'-' * 56}")
    for db, hit, read, ratio in rows:
        ratio_str = f"{ratio:.2%}" if ratio is not None else "N/A"
        print(f"  {db:<20} {hit:>12} {read:>12} {ratio_str:>10}")

    cur.execute("SHOW shared_buffers")
    shared = cur.fetchone()[0]
    cur.execute("SHOW effective_cache_size")
    effective = cur.fetchone()[0]
    print()
    print(f"  shared_buffers (PG 缓冲池):      {shared}")
    print(f"  effective_cache_size (含 OS 缓存): {effective}")
    print(f"  建议: 命中率 < 99% 应考虑增大 shared_buffers")

    conn.close()
    return rows


def collect_lock_waiting():
    section("3. 锁等待检测")
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            blocked.pid        AS blocked_pid,
            blocked.query      AS blocked_query,
            blocking.pid       AS blocking_pid,
            blocking.query     AS blocking_query,
            l.locktype,
            l.mode             AS blocked_mode,
            l2.mode            AS blocking_mode,
            a.usename          AS blocked_user,
            a2.usename         AS blocking_user,
            now() - blocked.query_start AS blocked_duration
        FROM pg_locks l
        JOIN pg_stat_activity blocked  ON blocked.pid = l.pid
        JOIN pg_locks l2              ON l2.locktype = l.locktype
                                    AND l2.relation = l.relation
                                    AND l2.granted
                                    AND l2.pid != l.pid
        JOIN pg_stat_activity blocking ON blocking.pid = l2.pid
        JOIN pg_stat_activity a        ON a.pid = blocked.pid
        JOIN pg_stat_activity a2       ON a2.pid = blocking.pid
        WHERE NOT l.granted
        ORDER BY blocked_duration DESC
    """)
    rows = cur.fetchall()

    if not rows:
        print("  [OK] 当前无锁等待")
    else:
        print(f"  [警告] 发现 {len(rows)} 个锁等待:")
        print()
        for i, (bpid, bquery, spid, squery, ltype, bmode, smode, buser, suser, dur) in enumerate(rows, 1):
            print(f"  --- 锁等待 #{i} ---")
            print(f"    被阻塞: PID={bpid} 用户={buser} 等待模式={bmode}")
            print(f"      SQL: {bquery[:80]}")
            print(f"    阻塞者: PID={spid} 用户={suser} 持有模式={smode}")
            print(f"      SQL: {squery[:80]}")
            print(f"    等待时长: {dur}")
            print()

    cur.execute("""
        SELECT locktype, mode, count(*) as cnt
        FROM pg_locks
        WHERE granted
        GROUP BY locktype, mode
        ORDER BY cnt DESC
    """)
    granted = cur.fetchall()
    print(f"  当前已授予锁统计:")
    print(f"  {'锁类型':<15} {'模式':<20} {'数量':>6}")
    print(f"  {'-' * 43}")
    for ltype, mode, cnt in granted:
        print(f"  {ltype:<15} {mode:<20} {cnt:>6}")

    conn.close()
    return rows


def collect_long_transactions():
    section("4. 长事务检测")
    conn = get_conn()
    cur = conn.cursor()

    threshold_seconds = int(os.getenv("LONG_TXN_THRESHOLD", "60"))

    cur.execute("""
        SELECT
            pid,
            usename,
            datname,
            application_name,
            state,
            now() - xact_start  AS xact_duration,
            now() - query_start AS query_duration,
            left(query, 100)    AS query_preview
        FROM pg_stat_activity
        WHERE xact_start IS NOT NULL
          AND now() - xact_start > make_interval(secs => %s)
          AND state != 'idle'
        ORDER BY xact_duration DESC
    """, (threshold_seconds,))
    rows = cur.fetchall()

    if not rows:
        print(f"  [OK] 无超过 {threshold_seconds}s 的长事务")
    else:
        print(f"  [警告] 发现 {len(rows)} 个超过 {threshold_seconds}s 的长事务:")
        print()
        for pid, user, db, app, state, xdur, qdur, query in rows:
            print(f"    PID={pid} 用户={user} 库={db} 状态={state}")
            print(f"      事务时长: {xdur}  查询时长: {qdur}")
            print(f"      SQL: {query}")
            print()

    cur.execute("""
        SELECT
            pid,
            now() - xact_start AS xact_duration,
            state
        FROM pg_stat_activity
        WHERE xact_start IS NOT NULL
        ORDER BY xact_duration DESC
        LIMIT 5
    """)
    top5 = cur.fetchall()
    print(f"  最长事务 Top 5:")
    print(f"  {'PID':>8} {'时长':<25} {'状态':<15}")
    print(f"  {'-' * 50}")
    for pid, dur, state in top5:
        print(f"  {pid:>8} {str(dur):<25} {state:<15}")

    cur.execute("""
        SELECT count(*) AS idle_in_txn
        FROM pg_stat_activity
        WHERE state = 'idle in transaction'
    """)
    idle_txn = cur.fetchone()[0]
    print()
    print(f"  idle in transaction 连接数: {idle_txn}")
    if idle_txn > 0:
        print(f"  [警告] 存在 idle in transaction，会阻止 VACUUM 回收死元组，需尽快处理")

    conn.close()
    return rows


def collect_dead_tuples():
    section("5. 死元组统计")
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            schemaname,
            relname,
            n_live_tup,
            n_dead_tup,
            round(n_dead_tup::numeric / nullif(n_live_tup, 0), 4) AS dead_ratio,
            last_autovacuum,
            last_autoanalyze,
            now() - last_autovacuum AS since_autovacuum
        FROM pg_stat_user_tables
        WHERE n_dead_tup > 0
        ORDER BY n_dead_tup DESC
        LIMIT 20
    """)
    rows = cur.fetchall()

    if not rows:
        print("  [OK] 无死元组堆积")
    else:
        print(f"  死元组堆积表（Top 20）:")
        print(f"  {'schema':<12} {'表':<25} {'live':>10} {'dead':>10} {'死率':>8} {'距上次autovacuum':<25}")
        print(f"  {'-' * 92}")
        for sch, tbl, live, dead, ratio, avac, aana, since in rows:
            ratio_str = f"{ratio:.2%}" if ratio is not None else "N/A"
            since_str = str(since).split(".")[0] if since else "N/A"
            print(f"  {sch:<12} {tbl:<25} {live:>10} {dead:>10} {ratio_str:>8} {since_str:<25}")

    cur.execute("SHOW autovacuum")
    av_enabled = cur.fetchone()[0]
    cur.execute("SHOW autovacuum_vacuum_threshold")
    av_threshold = cur.fetchone()[0]
    cur.execute("SHOW autovacuum_vacuum_scale_factor")
    av_scale = cur.fetchone()[0]
    print()
    print(f"  autovacuum 配置:")
    print(f"    开启状态: {av_enabled}")
    print(f"    阈值: {av_threshold} + 比例 {av_scale} × n_live_tup")
    print(f"  建议: 死率 > 10% 的表应检查 autovacuum 是否跟上")

    conn.close()
    return rows


def collect_replication_lag():
    section("6. 复制延迟监控")
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT count(*) FROM pg_stat_replication
    """)
    replica_count = cur.fetchone()[0]

    if replica_count == 0:
        print("  [信息] 当前无流复制从库（主库角色或单机部署）")
        cur.execute("SELECT pg_is_in_recovery()")
        in_recovery = cur.fetchone()[0]
        if in_recovery:
            print("  当前实例为从库（standby），查看自身同步状态:")
            cur.execute("""
                SELECT synced_lsn, write_lag, flush_lag, replay_lag
                FROM pg_stat_wal_receiver
            """)
            for row in cur.fetchall():
                print(f"    synced_lsn={row[0]} write_lag={row[1]} flush_lag={row[2]} replay_lag={row[3]}")
        conn.close()
        return None

    print(f"  流复制从库数: {replica_count}")
    print()

    cur.execute("""
        SELECT
            application_name,
            client_addr,
            state,
            sync_state,
            sent_lsn,
            write_lsn,
            flush_lsn,
            replay_lsn,
            write_lag,
            flush_lag,
            replay_lag,
            pg_wal_lsn_diff(sent_lsn, replay_lsn) AS byte_lag
        FROM pg_stat_replication
        ORDER BY byte_lag DESC
    """)
    rows = cur.fetchall()
    print(f"  {'应用名':<20} {'客户端':<16} {'状态':<12} {'同步':<8} {'字节延迟':>12} {'写延迟':<12} {'回放延迟':<12}")
    print(f"  {'-' * 96}")
    for app, addr, state, sync, sent, write, flush, replay, wlag, flag, rlag, blag in rows:
        print(f"  {app:<20} {str(addr):<16} {state:<12} {sync:<8} {int(blag):>12} {str(wlag):<12} {str(rlag):<12}")

    print()
    print(f"  延迟告警阈值建议:")
    print(f"    字节延迟 > 16MB   → 网络或从库写入瓶颈")
    print(f"    回放延迟 > 5s     → 从库查询压力大或长事务阻塞回放")
    print(f"    持续增长不收敛    → 需检查从库负载或网络带宽")

    cur.execute("""
        SELECT slot_name, plugin, slot_type, active, restart_lsn,
               pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) AS retained_bytes
        FROM pg_replication_slots
    """)
    slots = cur.fetchall()
    if slots:
        print()
        print(f"  复制槽（保留 WAL 防回收）:")
        print(f"  {'槽名':<25} {'类型':<10} {'活跃':>6} {'保留字节':>14}")
        print(f"  {'-' * 58}")
        for name, plugin, stype, active, rlsn, rbytes in slots:
            print(f"  {name:<25} {stype:<10} {str(active):>6} {int(rbytes):>14}")
        inactive = [s for s in slots if not s[3]]
        if inactive:
            print()
            print(f"  [警告] 存在非活跃复制槽，WAL 持续堆积可能导致主库磁盘满!")

    conn.close()
    return rows


def collect_tablespace_usage():
    section("7. 表空间使用监控")
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            pg_tablespace_name(oid) AS spcname,
            pg_tablespace_location(oid) AS location
        FROM pg_tablespace
    """)
    spaces = cur.fetchall()
    print(f"  表空间列表:")
    for name, loc in spaces:
        print(f"    {name}: {loc}")

    print()
    cur.execute("""
        SELECT
            schemaname,
            relname,
            pg_size_pretty(pg_relation_size(relid))        AS table_size,
            pg_size_pretty(pg_indexes_size(relid))         AS index_size,
            pg_size_pretty(pg_total_relation_size(relid))  AS total_size,
            pg_total_relation_size(relid)                  AS total_bytes,
            n_live_tup
        FROM pg_stat_user_tables
        ORDER BY pg_total_relation_size(relid) DESC
        LIMIT 20
    """)
    rows = cur.fetchall()
    print(f"  表空间使用 Top 20:")
    print(f"  {'schema':<12} {'表':<25} {'表大小':>10} {'索引大小':>10} {'总大小':>10} {'行数':>10}")
    print(f"  {'-' * 80}")
    for sch, tbl, tsize, isize, total, total_b, live in rows:
        print(f"  {sch:<12} {tbl:<25} {tsize:>10} {isize:>10} {total:>10} {live:>10}")

    print()
    cur.execute("""
        SELECT
            schemaname,
            indexrelname,
            relname          AS table_name,
            pg_size_pretty(pg_relation_size(indexrelid)) AS index_size,
            idx_scan,
            idx_tup_read,
            idx_tup_fetch
        FROM pg_stat_user_indexes
        ORDER BY pg_relation_size(indexrelid) DESC
        LIMIT 15
    """)
    idx_rows = cur.fetchall()
    print(f"  索引空间使用 Top 15:")
    print(f"  {'schema':<12} {'索引':<30} {'表':<20} {'大小':>10} {'扫描数':>10}")
    print(f"  {'-' * 84}")
    for sch, idx, tbl, size, scan, tread, tfetch in idx_rows:
        print(f"  {sch:<12} {idx:<30} {tbl:<20} {size:>10} {scan:>10}")

    cur.execute("""
        SELECT
            datname,
            pg_size_pretty(pg_database_size(datname)) AS db_size,
            pg_database_size(datname) AS db_bytes
        FROM pg_database
        ORDER BY pg_database_size(datname) DESC
    """)
    db_rows = cur.fetchall()
    print()
    print(f"  数据库总大小:")
    print(f"  {'数据库':<20} {'大小':>12}")
    print(f"  {'-' * 34}")
    for db, size, bytes_ in db_rows:
        print(f"  {db:<20} {size:>12}")

    conn.close()
    return rows


def collect_slow_queries():
    section("8. 慢查询统计")
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SHOW log_min_duration_statement")
    threshold = cur.fetchone()[0]
    print(f"  慢查询日志阈值 (log_min_duration_statement): {threshold}")

    cur.execute("""
        SELECT
            query,
            calls,
            round(total_exec_time::numeric, 2)      AS total_ms,
            round(mean_exec_time::numeric, 2)        AS mean_ms,
            round(max_exec_time::numeric, 2)         AS max_ms,
            rows
        FROM pg_stat_statements
        ORDER BY mean_exec_time DESC
        LIMIT 10
    """)
    try:
        rows = cur.fetchall()
        if rows:
            print(f"  平均耗时 Top 10（来自 pg_stat_statements）:")
            print(f"  {'调用次数':>8} {'总耗时ms':>12} {'平均ms':>10} {'最大ms':>10} {'行数':>8}  SQL")
            print(f"  {'-' * 90}")
            for q, calls, total, mean, mx, nrows in rows:
                preview = q.strip().replace("\n", " ")[:50]
                print(f"  {calls:>8} {total:>12} {mean:>10} {mx:>10} {nrows:>8}  {preview}")
        else:
            print("  [信息] pg_stat_statements 无数据")
    except psycopg2.Error as e:
        print(f"  [提示] pg_stat_statements 扩展未启用: {e}")
        print("  启用方法: 在 postgresql.conf 设置 shared_preload_libraries = 'pg_stat_statements'")
        print("           然后在目标库执行 CREATE EXTENSION pg_stat_statements;")

    conn.close()


def collect_checkpoint_stats():
    section("9. Checkpoint 统计")
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            buffers_checkpoint,
            buffers_backend,
            buffers_clean,
            checkpoints_req,
            checkpoints_timed,
            round(buffers_checkpoint::numeric /
                  nullif(buffers_checkpoint + buffers_backend + buffers_clean, 0), 4) AS ckpt_ratio
        FROM pg_stat_bgwriter
    """)
    row = cur.fetchone()
    if row:
        bc, bb, bcl, cr, ct, ratio = row
        print(f"  checkpoint 写入缓冲数:    {bc}")
        print(f"  backend 自己写入缓冲数:   {bb}")
        print(f"  bgwriter 写入缓冲数:      {bcl}")
        print(f"  请求的 checkpoint 数:     {cr}")
        print(f"  定时 checkpoint 数:       {ct}")
        print(f"  checkpoint 写入占比:      {ratio:.2%}" if ratio else "  checkpoint 写入占比: N/A")
        print()
        if bb > bc and bb > 0:
            print(f"  [警告] backend 写入占比过高，说明 checkpoint 间隔太长或 shared_buffers 太小")
            print(f"         建议增大 checkpoint_timeout 或 max_wal_size")

    cur.execute("SHOW checkpoint_timeout")
    print(f"\n  checkpoint_timeout: {cur.fetchone()[0]}")
    cur.execute("SHOW max_wal_size")
    print(f"  max_wal_size:       {cur.fetchone()[0]}")

    conn.close()


def generate_summary():
    section("监控摘要")
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            (SELECT count(*) FROM pg_stat_activity) AS connections,
            (SELECT count(*) FROM pg_stat_activity WHERE state = 'idle in transaction') AS idle_in_txn,
            (SELECT count(*) FROM pg_locks WHERE NOT granted) AS waiting_locks,
            (SELECT count(*) FROM pg_stat_replication) AS replicas,
            (SELECT sum(n_dead_tup) FROM pg_stat_user_tables) AS total_dead,
            (SELECT count(*) FROM pg_replication_slots WHERE NOT active) AS inactive_slots
    """)
    row = cur.fetchone()
    connections, idle_txn, waiting_locks, replicas, total_dead, inactive_slots = row

    print(f"  连接数:           {connections}")
    print(f"  idle in txn:      {idle_txn}   {'[警告]' if idle_txn > 0 else '[OK]'}")
    print(f"  等待锁:           {waiting_locks}   {'[警告]' if waiting_locks > 0 else '[OK]'}")
    print(f"  从库数:           {replicas}")
    print(f"  总死元组:         {total_dead}")
    print(f"  非活跃复制槽:     {inactive_slots}   {'[警告]' if inactive_slots > 0 else '[OK]'}")

    issues = []
    if idle_txn > 0:
        issues.append("存在 idle in transaction 连接")
    if waiting_locks > 0:
        issues.append("存在锁等待")
    if total_dead and total_dead > 100000:
        issues.append(f"死元组堆积 ({total_dead})")
    if inactive_slots and inactive_slots > 0:
        issues.append("存在非活跃复制槽")

    print()
    if issues:
        print(f"  [需关注] 发现 {len(issues)} 个问题:")
        for i, issue in enumerate(issues, 1):
            print(f"    {i}. {issue}")
    else:
        print(f"  [OK] 所有关键指标正常")

    conn.close()
    return issues


def main():
    print("=" * 60)
    print("PostgreSQL 运维监控脚本")
    print(f"采集时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    try:
        conn = get_conn()
        conn.close()
    except psycopg2.OperationalError as e:
        print(f"\n[失败] 无法连接 PostgreSQL: {e}")
        print("请设置环境变量: PG_HOST / PG_PORT / PG_DB / PG_USER / PG_PASS")
        sys.exit(1)

    collect_connection_metrics()
    collect_cache_metrics()
    collect_lock_waiting()
    collect_long_transactions()
    collect_dead_tuples()
    collect_replication_lag()
    collect_tablespace_usage()
    collect_slow_queries()
    collect_checkpoint_stats()
    generate_summary()

    print()
    print("=" * 60)
    print("监控采集完成")
    print("=" * 60)


if __name__ == "__main__":
    main()