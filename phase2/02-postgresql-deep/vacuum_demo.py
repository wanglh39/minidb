"""VACUUM 效果演示：批量更新产生死元组，VACUUM 回收空间"""
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

def main():
    try:
        conn = get_conn()
        conn.autocommit = True
        cur = conn.cursor()

        print("=== VACUUM 效果演示 ===\n")

        cur.execute("DROP TABLE IF EXISTS vacuum_demo")
        cur.execute("CREATE TABLE vacuum_demo (id int PRIMARY KEY, val text)")

        cur.execute("INSERT INTO vacuum_demo SELECT i, 'initial' FROM generate_series(1, 10000) i")
        cur.execute("ANALYZE vacuum_demo")

        cur.execute("SELECT pg_size_pretty(pg_total_relation_size('vacuum_demo'))")
        print(f"初始大小: {cur.fetchone()[0]}")

        cur.execute("UPDATE vacuum_demo SET val = 'updated' WHERE id <= 5000")

        cur.execute("SELECT n_live_tup, n_dead_tup FROM pg_stat_user_tables WHERE relname = 'vacuum_demo'")
        live, dead = cur.fetchone()
        print(f"更新后: live={live}, dead={dead}")

        cur.execute("SELECT pg_size_pretty(pg_total_relation_size('vacuum_demo'))")
        print(f"更新后大小: {cur.fetchone()[0]}")

        cur.execute("VACUUM vacuum_demo")
        cur.execute("SELECT n_live_tup, n_dead_tup FROM pg_stat_user_tables WHERE relname = 'vacuum_demo'")
        live, dead = cur.fetchone()
        print(f"VACUUM后: live={live}, dead={dead}")

        cur.execute("SELECT pg_size_pretty(pg_total_relation_size('vacuum_demo'))")
        print(f"VACUUM后大小: {cur.fetchone()[0]}")

        cur.execute("VACUUM FULL vacuum_demo")
        cur.execute("SELECT pg_size_pretty(pg_total_relation_size('vacuum_demo'))")
        print(f"VACUUM FULL后大小: {cur.fetchone()[0]}")

        cur.execute("DROP TABLE vacuum_demo")
        conn.close()

    except psycopg2.OperationalError as e:
        print(f"无法连接 PostgreSQL: {e}")

if __name__ == '__main__':
    main()