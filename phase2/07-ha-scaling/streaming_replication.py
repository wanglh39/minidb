import os
import textwrap
from pathlib import Path

OUTPUT_DIR = Path(__file__).parent / "output" / "streaming_replication"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


PRIMARY_CONF = """\
# postgresql.conf - Primary (主库)
# 由 streaming_replication.py 生成

# ─── 复制相关 ───
wal_level = replica
max_wal_senders = 10
wal_keep_size = 10240
hot_standby = on

# ─── 同步复制（按需开启）───
# synchronous_standby_names = 'FIRST 1 (standby1)'
# synchronous_commit = on

# ─── 检查点 ───
checkpoint_timeout = 5min
max_wal_size = 1GB
min_wal_size = 256MB

# ─── WAL 归档（可选）───
# archive_mode = on
# archive_command = 'cp %p /var/lib/postgresql/archive/%f'

# ─── 连接 ───
listen_addresses = '*'
port = 5432
max_connections = 100

# ─── 日志 ───
log_connections = on
log_replication_commands = on
"""


STANDBY_CONF = """\
# postgresql.conf - Standby (备库)
# 由 streaming_replication.py 生成

# ─── 备库相关 ───
hot_standby = on
hot_standby_feedback = on
max_standby_streaming_delay = 30s

# ─── 连接 ───
listen_addresses = '*'
port = 5432
max_connections = 100

# ─── primary_conninfo 写在 postgresql.auto.conf ───
"""


PRIMARY_HBA = """\
# pg_hba.conf - Primary
# TYPE  DATABASE     USER        ADDRESS          METHOD
local   all          all                          trust
host    all          all         127.0.0.1/32     md5
host    replication  replicator  0.0.0.0/0        md5
host    all          all         0.0.0.0/0        md5
"""


REPLICATION_SETUP_SQL = """\
-- 在主库执行：创建复制用户
CREATE ROLE replicator WITH REPLICATION LOGIN PASSWORD 'repl_pass';

-- 查看复制用户
SELECT rolname, rolreplication FROM pg_roles WHERE rolreplication = true;
"""


STATUS_CHECK_SQL = """\
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
"""


PROMOTE_SQL = """\
-- 故障转移：将备库提升为主库
-- PostgreSQL 12+
pg_ctl promote -D /var/lib/postgresql/data

-- 或用 SQL
SELECT pg_promote(wait => true, wait_seconds => 60);

-- 提升后检查
SELECT pg_is_in_recovery();  -- 应返回 false
"""


def generate_primary_config(output_dir: Path) -> dict[str, Path]:
    paths = {}
    paths["postgresql.conf"] = output_dir / "primary_postgresql.conf"
    paths["postgresql.conf"].write_text(PRIMARY_CONF, encoding="utf-8")

    paths["pg_hba.conf"] = output_dir / "primary_pg_hba.conf"
    paths["pg_hba.conf"].write_text(PRIMARY_HBA, encoding="utf-8")

    paths["setup.sql"] = output_dir / "primary_setup.sql"
    paths["setup.sql"].write_text(REPLICATION_SETUP_SQL, encoding="utf-8")
    return paths


def generate_standby_config(output_dir: Path, primary_host: str = "primary",
                            primary_port: int = 5432) -> dict[str, Path]:
    paths = {}
    paths["postgresql.conf"] = output_dir / "standby_postgresql.conf"
    paths["postgresql.conf"].write_text(STANDBY_CONF, encoding="utf-8")

    auto_conf = (
        f"# postgresql.auto.conf - Standby\n"
        f"# 由 streaming_replication.py 生成\n"
        f"primary_conninfo = 'host={primary_host} port={primary_port} "
        f"user=replicator password=repl_pass application_name=standby1'\n"
        f"primary_slot_name = 'standby_slot'\n"
    )
    paths["postgresql.auto.conf"] = output_dir / "standby_postgresql.auto.conf"
    paths["postgresql.auto.conf"].write_text(auto_conf, encoding="utf-8")

    paths["standby.signal"] = output_dir / "standby.signal"
    paths["standby.signal"].write_text("", encoding="utf-8")

    basebackup_cmd = (
        f"pg_basebackup -h {primary_host} -p {primary_port} -U replicator "
        f"-D /var/lib/postgresql/standby -Fp -Xs -P -R "
        f"-S standby_slot -C\n"
    )
    paths["basebackup.sh"] = output_dir / "standby_basebackup.sh"
    paths["basebackup.sh"].write_text(basebackup_cmd, encoding="utf-8")
    return paths


def generate_status_sql(output_dir: Path) -> dict[str, Path]:
    paths = {}
    paths["status.sql"] = output_dir / "replication_status.sql"
    paths["status.sql"].write_text(STATUS_CHECK_SQL, encoding="utf-8")

    paths["promote.sql"] = output_dir / "promote.sql"
    paths["promote.sql"].write_text(PROMOTE_SQL, encoding="utf-8")
    return paths


def print_summary(primary_paths: dict, standby_paths: dict, status_paths: dict) -> None:
    print("=" * 60)
    print("PostgreSQL 流复制配置生成完成")
    print("=" * 60)

    print("\n主库配置：")
    for name, path in primary_paths.items():
        print(f"  {name:20s} → {path}")

    print("\n备库配置：")
    for name, path in standby_paths.items():
        print(f"  {name:20s} → {path}")

    print("\n状态检查 SQL：")
    for name, path in status_paths.items():
        print(f"  {name:20s} → {path}")

    print("\n" + "=" * 60)
    print("部署步骤：")
    print("=" * 60)
    print(textwrap.dedent("""\
    1. 启动主库，加载 primary_postgresql.conf 和 primary_pg_hba.conf
    2. 在主库执行 primary_setup.sql 创建复制用户
    3. 在主库创建复制槽：
       SELECT pg_create_physical_replication_slot('standby_slot');
    4. 在备库机器执行 standby_basebackup.sh 做基础备份
    5. 将 standby_postgresql.conf 和 standby_postgresql.auto.conf
       放入备库 data 目录，创建 standby.signal 文件
    6. 启动备库
    7. 在主库执行 replication_status.sql 验证复制状态
    """))


def main() -> None:
    primary_paths = generate_primary_config(OUTPUT_DIR)
    standby_paths = generate_standby_config(OUTPUT_DIR)
    status_paths = generate_status_sql(OUTPUT_DIR)
    print_summary(primary_paths, standby_paths, status_paths)


if __name__ == "__main__":
    main()