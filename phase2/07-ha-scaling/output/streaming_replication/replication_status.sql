-- ════════ 在主库执行 ════════

-- 1. 查看所有备库的复制状态
SELECT
    application_name  AS name,
    client_addr       AS client,
    state             AS state,
    sync_state        AS sync,
    sent_lsn          AS sent,
    write_lsn         AS written,
    flush_lsn         AS flushed,
    replay_lsn        AS replayed,
    pg_wal_lsn_diff(sent_lsn, replay_lsn) AS lag_bytes
FROM pg_stat_replication;

-- 2. 查看 walsender 进程
SELECT pid, state, sent_lsn, write_lsn, flush_lsn, replay_lsn
FROM pg_stat_wal_sender;

-- 3. 当前 WAL 位置
SELECT pg_current_wal_lsn()  AS current_lsn,
       pg_walfile_name(pg_current_wal_lsn()) AS wal_file;


-- ════════ 在备库执行 ════════

-- 4. 备库状态
SELECT pg_is_in_recovery() AS in_recovery;

-- 5. 最后接收/重放的 WAL
SELECT pg_last_wal_receive_lsn() AS receive_lsn,
       pg_last_wal_replay_lsn()  AS replay_lsn,
       pg_last_xact_replay_timestamp() AS last_replay_time;

-- 6. 复制延迟（秒）
SELECT now() - pg_last_xact_replay_timestamp() AS lag_seconds;

-- 7. 复制延迟（字节）
SELECT pg_wal_lsn_diff(
           pg_last_wal_receive_lsn(),
           pg_last_wal_replay_lsn()
       ) AS apply_lag_bytes;

-- 8. buffer 命中率（备库读性能）
SELECT blks_hit, blks_read,
       round(blks_hit::numeric / nullif(blks_hit + blks_read, 0), 4) AS hit_ratio
FROM pg_stat_database WHERE datname = current_database();
