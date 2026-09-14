"""观测 PostgreSQL 内部状态：缓冲池、WAL、死元组、锁"""
import psycopg2
import os

def get_conn():
    return psycopg2.connect(
        host=os.getenv('PG_HOST', 'localhost'),
        port=os.getenv('PG_PORT', '5432'),
        dbname=os.getenv('PG_DB', 'postgres'),
        user=os.getenv('PG_USER', 'postgres'),
        password=os.getenv('PG_PASS', 'secret')
    )

def show_server_info():
    conn = get_conn()
    cur = conn.cursor()
    print("=== PostgreSQL 服务器信息 ===")
    cur.execute("SELECT version()")
    print(cur.fetchone()[0])
    cur.execute("SELECT current_setting('block_size'), current_setting('shared_buffers')")
    print(f"块大小: {cur.fetchone()}")
    conn.close()

def show_buffer_stats():
    conn = get_conn()
    cur = conn.cursor()
    print("\n=== 缓冲池统计 ===")
    cur.execute("""
        SELECT datname, blks_hit, blks_read,
               round(blks_hit::numeric / nullif(blks_hit + blks_read, 0), 4) as hit_ratio
        FROM pg_stat_database WHERE datname IS NOT NULL
    """)
    for row in cur.fetchall():
        print(f"  {row[0]:15s} hit={row[1]:>10} read={row[2]:>10} ratio={row[3]}")
    conn.close()

def show_wal_info():
    conn = get_conn()
    cur = conn.cursor()
    print("\n=== WAL 信息 ===")
    cur.execute("SELECT pg_current_wal_lsn()")
    print(f"  当前 LSN: {cur.fetchone()[0]}")
    cur.execute("SHOW wal_level")
    print(f"  WAL level: {cur.fetchone()[0]}")
    cur.execute("SHOW max_wal_size")
    print(f"  最大 WAL: {cur.fetchone()[0]}")
    conn.close()

def show_table_stats():
    conn = get_conn()
    cur = conn.cursor()
    print("\n=== 表统计 ===")
    cur.execute("""
        SELECT relname, n_live_tup, n_dead_tup,
               last_autovacuum, last_autoanalyze
        FROM pg_stat_user_tables
        ORDER BY n_dead_tup DESC NULLS LAST
        LIMIT 10
    """)
    for row in cur.fetchall():
        print(f"  {row[0]:20s} live={row[1]:>8} dead={row[2]:>8} last_av={row[3]}")
    conn.close()

def main():
    try:
        show_server_info()
        show_buffer_stats()
        show_wal_info()
        show_table_stats()
    except psycopg2.OperationalError as e:
        print(f"无法连接 PostgreSQL: {e}")
        print("请确保 PG 正在运行，或设置 PG_HOST/PG_PORT/PG_USER/PG_PASS 环境变量")

if __name__ == '__main__':
    main()