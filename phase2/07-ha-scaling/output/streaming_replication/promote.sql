-- 故障转移：将备库提升为主库
-- PostgreSQL 12+
pg_ctl promote -D /var/lib/postgresql/data

-- 或用 SQL
SELECT pg_promote(wait => true, wait_seconds => 60);

-- 提升后检查
SELECT pg_is_in_recovery();  -- 应返回 false
