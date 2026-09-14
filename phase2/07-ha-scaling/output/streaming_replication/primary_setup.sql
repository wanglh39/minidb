-- 在主库执行：创建复制用户
CREATE ROLE replicator WITH REPLICATION LOGIN PASSWORD 'repl_pass';

-- 查看复制用户
SELECT rolname, rolreplication FROM pg_roles WHERE rolreplication = true;
