"""备份与恢复演示：pg_dump 逻辑备份、pg_basebackup 物理备份、WAL 归档、PITR 恢复流程"""
import os
import subprocess
import time
import shutil
from datetime import datetime

import psycopg2


def get_conn():
    return psycopg2.connect(
        host=os.getenv("PG_HOST", "localhost"),
        port=os.getenv("PG_PORT", "5432"),
        dbname=os.getenv("PG_DB", "postgres"),
        user=os.getenv("PG_USER", "postgres"),
        password=os.getenv("PG_PASS", "secret"),
    )


def run(cmd, check=True, capture=True):
    print(f"  $ {cmd}")
    result = subprocess.run(
        cmd, shell=True, text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if capture and result.stdout:
        for line in result.stdout.strip().splitlines():
            print(f"    {line}")
    if check and result.returncode != 0:
        if capture and result.stderr:
            for line in result.stderr.strip().splitlines():
                print(f"    [stderr] {line}")
        raise RuntimeError(f"命令失败 (exit={result.returncode}): {cmd}")
    return result


def get_pg_env():
    env = os.environ.copy()
    env["PGPASSWORD"] = os.getenv("PG_PASS", "secret")
    return env


def ensure_test_data():
    conn = get_conn()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS backup_demo")
    cur.execute("CREATE TABLE backup_demo (id int PRIMARY KEY, val text, ts timestamptz DEFAULT now())")
    cur.execute("INSERT INTO backup_demo SELECT i, 'row_' || i FROM generate_series(1, 1000) i")
    cur.execute("SELECT count(*) FROM backup_demo")
    count = cur.fetchone()[0]
    print(f"  已准备测试数据: {count} 行")
    conn.close()
    return count


def demo_pg_dump_logical():
    print("\n=== 1. pg_dump 逻辑备份 ===")
    backup_dir = "phase2/08-ops/_backup_output"
    os.makedirs(backup_dir, exist_ok=True)

    env = get_pg_env()
    host = os.getenv("PG_HOST", "localhost")
    port = os.getenv("PG_PORT", "5432")
    db = os.getenv("PG_DB", "postgres")
    user = os.getenv("PG_USER", "postgres")

    plain_file = os.path.join(backup_dir, "logical_plain.sql")
    custom_file = os.path.join(backup_dir, "logical_custom.dump")
    tar_file = os.path.join(backup_dir, "logical_tar.tar")
    parallel_file = os.path.join(backup_dir, "logical_parallel.dump")

    print("\n[1a] 纯文本格式（可读，psql 恢复）")
    run(f'pg_dump -h {host} -p {port} -U {user} -d {db} -F p -f "{plain_file}"', check=False)

    print("\n[1b] 自定义压缩格式（pg_restore 恢复，支持并行、选择性恢复）")
    run(f'pg_dump -h {host} -p {port} -U {user} -d {db} -F c -f "{custom_file}"', check=False)

    print("\n[1c] tar 格式（目录归档）")
    run(f'pg_dump -h {host} -p {port} -U {user} -d {db} -F t -f "{tar_file}"', check=False)

    print("\n[1d] 目录格式 + 并行备份（多表并行，大库加速）")
    parallel_dir = os.path.join(backup_dir, "logical_parallel_dir")
    if os.path.exists(parallel_dir):
        shutil.rmtree(parallel_dir)
    run(f'pg_dump -h {host} -p {port} -U {user} -d {db} -F d -j 4 -f "{parallel_dir}"', check=False)

    print("\n[1e] 仅备份 schema（不含数据）")
    schema_file = os.path.join(backup_dir, "schema_only.sql")
    run(f'pg_dump -h {host} -p {port} -U {user} -d {db} --schema-only -f "{schema_file}"', check=False)

    print("\n[1f] 仅备份指定表")
    table_file = os.path.join(backup_dir, "table_only.sql")
    run(f'pg_dump -h {host} -p {port} -U {user} -d {db} -t backup_demo -f "{table_file}"', check=False)

    print("\n  逻辑备份产物:")
    for f in os.listdir(backup_dir):
        path = os.path.join(backup_dir, f)
        if os.path.isfile(path):
            size = os.path.getsize(path)
            print(f"    {f}: {size} bytes")
        elif os.path.isdir(path):
            print(f"    {f}/ (目录)")

    return backup_dir


def demo_pg_basebackup_physical():
    print("\n=== 2. pg_basebackup 物理备份 ===")
    backup_dir = "phase2/08-ops/_basebackup_output"
    if os.path.exists(backup_dir):
        shutil.rmtree(backup_dir)
    os.makedirs(backup_dir, exist_ok=True)

    env = get_pg_env()
    host = os.getenv("PG_HOST", "localhost")
    port = os.getenv("PG_PORT", "5432")
    user = os.getenv("PG_USER", "postgres")

    print("\n[2a] 检查 WAL 归档是否已配置")
    conn = get_conn()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SHOW archive_mode")
    archive_mode = cur.fetchone()[0]
    cur.execute("SHOW wal_level")
    wal_level = cur.fetchone()[0]
    cur.execute("SHOW max_wal_senders")
    max_wal_senders = cur.fetchone()[0]
    print(f"    archive_mode = {archive_mode}")
    print(f"    wal_level = {wal_level}")
    print(f"    max_wal_senders = {max_wal_senders}")
    conn.close()

    if archive_mode != "on" and archive_mode != "always":
        print("    [警告] archive_mode 未开启，物理备份需配合 WAL 归档才能做 PITR")
        print("    请在 postgresql.conf 设置:")
        print("      archive_mode = on")
        print("      archive_command = 'test ! -f /archive/%f && cp %p /archive/%f'")
        print("      wal_level = replica")
        print("    本演示仍会尝试执行 pg_basebackup（仅全量备份，无 PITR）")

    print("\n[2b] 全量基础备份（-X stream 流式传输 WAL，-R 自动生成恢复配置）")
    cmd = (
        f'pg_basebackup -h {host} -p {port} -U {user} '
        f'-D "{backup_dir}" -Fp -Xs -P -R -c fast'
    )
    result = run(cmd, check=False)
    if result.returncode != 0:
        print("    [提示] pg_basebackup 失败常见原因:")
        print("      1. 未配置 max_wal_senders >= 1")
        print("      2. 连接用户无 REPLICATION 权限（需 pg_hba.conf 允许 replication）")
        print("      3. wal_level < replica")
        return None

    print("\n[2c] 查看物理备份内容")
    if os.path.exists(backup_dir):
        entries = os.listdir(backup_dir)
        print(f"    备份目录共 {len(entries)} 个条目:")
        for e in sorted(entries)[:15]:
            print(f"      {e}")
        if len(entries) > 15:
            print(f"      ... 共 {len(entries)} 项")

    return backup_dir


def demo_wal_archive_config():
    print("\n=== 3. WAL 归档配置演示 ===")

    print("\n[3a] 查看当前 WAL 归档配置")
    conn = get_conn()
    conn.autocommit = True
    cur = conn.cursor()
    for setting in ["archive_mode", "archive_command", "archive_timeout",
                     "wal_level", "max_wal_senders", "wal_keep_size"]:
        cur.execute(f"SHOW {setting}")
        print(f"    {setting} = {cur.fetchone()[0]}")

    print("\n[3b] 查看当前 WAL 文件")
    cur.execute("SELECT pg_current_wal_lsn()")
    current_lsn = cur.fetchone()[0]
    cur.execute("SELECT pg_walfile_name(pg_current_wal_lsn())")
    current_wal = cur.fetchone()[0]
    print(f"    当前 LSN: {current_lsn}")
    print(f"    当前 WAL 文件: {current_wal}")

    print("\n[3c] 手动切换 WAL（模拟 checkpoint 后归档）")
    cur.execute("SELECT pg_switch_wal()")
    new_wal = cur.fetchone()[0]
    print(f"    切换后 LSN: {new_wal}")

    print("\n[3d] 查看归档状态")
    cur.execute("""
        SELECT archived_count, failed_count,
               round(extract(epoch from (now() - last_archived_time))::numeric, 1) as seconds_since_last
        FROM pg_stat_archiver
    """)
    row = cur.fetchone()
    if row:
        print(f"    已归档 WAL 数: {row[0]}")
        print(f"    归档失败数: {row[1]}")
        print(f"    距上次归档秒数: {row[2]}")
    conn.close()

    print("\n[3e] 推荐的 postgresql.conf 归档配置:")
    print("    # 开启归档")
    print("    archive_mode = on")
    print("    # 归档命令（rsync 更安全，test 防止覆盖）")
    print("    archive_command = 'test ! -f /var/lib/pg_archive/%f && cp %p /var/lib/pg_archive/%f'")
    print("    # 强制归档间隔（即使未满也切换）")
    print("    archive_timeout = 300s")
    print("    # WAL 级别（replica 支持物理复制，logical 支持逻辑复制）")
    print("    wal_level = replica")
    print("    # 保留的 WAL 发送者数")
    print("    max_wal_senders = 10")
    print("    # 复制槽保留 WAL（防备从库断连后 WAL 被回收）")
    print("    max_replication_slots = 10")


def demo_pitr_recovery():
    print("\n=== 4. PITR 恢复流程演示 ===")

    print("\n[4a] PITR 原理")
    print("    1. 拥有基础备份（pg_basebackup 产物）+ 归档 WAL 文件")
    print("    2. 在备份目录创建 recovery.signal（PG 12+）或 recovery.conf（旧版）")
    print("    3. 设置恢复目标（时间点 / LSN / xid / 名称）")
    print("    4. 启动 PG，自动 replay WAL 至目标点后进入只读或提升为主")
    print("    5. 恢复完成后改写为可写（promote）")

    print("\n[4b] 记录当前时间（作为恢复目标示例）")
    target_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"    恢复目标时间: {target_time}")

    print("\n[4c] 恢复配置示例（postgresql.auto.conf 或 recovery.conf）:")
    print("    # 恢复到指定时间点")
    print(f"    restore_command = 'cp /var/lib/pg_archive/%f %p'")
    print(f"    recovery_target_time = '{target_time} +08:00'")
    print("    recovery_target_action = 'pause'   # pause|promote|shutdown")
    print("    # 或恢复到指定 LSN")
    print("    # recovery_target_lsn = '0/50000060'")
    print("    # 或恢复到指定事务 ID")
    print("    # recovery_target_xid = '12345'")
    print("    # 或恢复到命名的还原点")
    print("    # recovery_target_name = 'before_bulk_load'")

    print("\n[4d] 创建还原点（pg_create_restore_point）")
    conn = get_conn()
    conn.autocommit = True
    cur = conn.cursor()
    try:
        cur.execute("SELECT pg_create_restore_point('before_important_change')")
        lsn = cur.fetchone()[0]
        print(f"    已创建还原点 'before_important_change' @ LSN {lsn}")
    except psycopg2.Error as e:
        print(f"    创建还原点失败（需 superuser）: {e}")
    conn.close()

    print("\n[4e] 完整 PITR 操作步骤:")
    print("    步骤1: 停止当前 PG")
    print("      pg_ctl -D /var/lib/pgdata stop -m fast")
    print("    步骤2: 备份当前损坏的数据目录（以防恢复失败）")
    print("      mv /var/lib/pgdata /var/lib/pgdata_broken")
    print("    步骤3: 解压基础备份到数据目录")
    print("      tar xzf base_backup.tar.gz -C /var/lib/pgdata")
    print("    步骤4: 创建 recovery.signal（PG 12+）")
    print("      touch /var/lib/pgdata/recovery.signal")
    print("    步骤5: 写入恢复参数到 postgresql.auto.conf")
    print("      restore_command = 'cp /var/lib/pg_archive/%f %p'")
    print("      recovery_target_time = '<目标时间>'")
    print("      recovery_target_action = 'pause'")
    print("    步骤6: 启动 PG，自动进入恢复模式")
    print("      pg_ctl -D /var/lib/pgdata start")
    print("    步骤7: 监控恢复进度")
    print("      SELECT * FROM pg_stat_wal_receiver;")
    print("      -- 恢复完成后处于 pause 状态，检查数据正确性")
    print("    步骤8: 确认数据正确后提升为主库")
    print("      SELECT pg_wal_replay_resume();  -- 继续恢复")
    print("      SELECT pg_promote();            -- 提升为可写主库")


def demo_restore_from_logical():
    print("\n=== 5. 从逻辑备份恢复 ===")
    backup_dir = "phase2/08-ops/_backup_output"
    custom_file = os.path.join(backup_dir, "logical_custom.dump")

    print("\n[5a] 恢复到新数据库（避免覆盖原库）")
    env = get_pg_env()
    host = os.getenv("PG_HOST", "localhost")
    port = os.getenv("PG_PORT", "5432")
    user = os.getenv("PG_USER", "postgres")

    restore_db = "restore_test_db"
    conn = get_conn()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(f"DROP DATABASE IF EXISTS {restore_db}")
    cur.execute(f"CREATE DATABASE {restore_db}")
    conn.close()
    print(f"    已创建空数据库: {restore_db}")

    if os.path.exists(custom_file):
        print("\n[5b] 用 pg_restore 恢复自定义格式备份")
        run(f'pg_restore -h {host} -p {port} -U {user} -d {restore_db} -j 4 "{custom_file}"', check=False)

        print("\n[5c] 验证恢复结果")
        conn = psycopg2.connect(
            host=host, port=port, dbname=restore_db, user=user,
            password=os.getenv("PG_PASS", "secret"),
        )
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM backup_demo")
        count = cur.fetchone()[0]
        print(f"    恢复后 backup_demo 行数: {count}")
        conn.close()
    else:
        print("    [跳过] 自定义格式备份文件不存在（pg_dump 可能未执行）")

    print("\n[5d] 清理恢复测试库")
    conn = get_conn()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(f"DROP DATABASE IF EXISTS {restore_db}")
    conn.close()
    print(f"    已删除: {restore_db}")


def demo_backup_verification():
    print("\n=== 6. 备份完整性验证 ===")
    backup_dir = "phase2/08-ops/_backup_output"
    custom_file = os.path.join(backup_dir, "logical_custom.dump")

    print("\n[6a] 验证自定义格式备份可被 pg_restore 读取")
    if os.path.exists(custom_file):
        result = run(f'pg_restore --list "{custom_file}"', check=False)
        if result.returncode == 0:
            print("    [通过] 备份文件可正常读取")
        else:
            print("    [失败] 备份文件可能损坏")
    else:
        print("    [跳过] 备份文件不存在")

    print("\n[6b] 备份验证最佳实践:")
    print("    1. 定期在测试环境执行完整恢复演练（至少每月一次）")
    print("    2. 校验备份文件大小是否在合理范围")
    print("    3. 使用 pg_restore --list 检查备份内容完整性")
    print("    4. 对物理备份，可启动一个临时实例验证可恢复")
    print("    5. 监控归档 WAL 是否连续无缺口（pg_stat_archiver）")


def cleanup():
    print("\n=== 清理 ===")
    for d in ["phase2/08-ops/_backup_output", "phase2/08-ops/_basebackup_output"]:
        if os.path.exists(d):
            shutil.rmtree(d)
            print(f"  已删除: {d}")
    conn = get_conn()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS backup_demo")
    conn.close()
    print("  已删除: backup_demo 表")


def main():
    print("=" * 60)
    print("PostgreSQL 备份与恢复完整演示")
    print("=" * 60)

    try:
        conn = get_conn()
        conn.close()
        print("\n[OK] 已连接 PostgreSQL")
    except psycopg2.OperationalError as e:
        print(f"\n[失败] 无法连接 PostgreSQL: {e}")
        print("请确保 PostgreSQL 已启动，并设置环境变量:")
        print("  PG_HOST / PG_PORT / PG_DB / PG_USER / PG_PASS")
        return

    try:
        print("\n--- 准备测试数据 ---")
        ensure_test_data()

        demo_pg_dump_logical()
        demo_pg_basebackup_physical()
        demo_wal_archive_config()
        demo_pitr_recovery()
        demo_restore_from_logical()
        demo_backup_verification()

        cleanup()

        print("\n" + "=" * 60)
        print("演示完成")
        print("=" * 60)
    except Exception as e:
        print(f"\n[错误] {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()