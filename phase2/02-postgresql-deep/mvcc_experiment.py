"""MVCC 版本链实验：观察 xmin/xmax、更新创建新版本、死元组"""
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

        print("=== MVCC 版本链实验 ===\n")

        cur.execute("DROP TABLE IF EXISTS mvcc_demo")
        cur.execute("CREATE TABLE mvcc_demo (id int PRIMARY KEY, val text)")

        cur.execute("INSERT INTO mvcc_demo VALUES (1, 'original')")
        cur.execute("SELECT xmin::text, xmax::text, id, val FROM mvcc_demo")
        print("插入后:", cur.fetchone())

        cur.execute("UPDATE mvcc_demo SET val = 'v2' WHERE id = 1")
        cur.execute("SELECT xmin::text, xmax::text, id, val FROM mvcc_demo")
        print("更新后:", cur.fetchone())

        cur.execute("UPDATE mvcc_demo SET val = 'v3' WHERE id = 1")
        cur.execute("SELECT xmin::text, xmax::text, id, val FROM mvcc_demo")
        print("再次更新:", cur.fetchone())

        cur.execute("SELECT n_live_tup, n_dead_tup FROM pg_stat_user_tables WHERE relname = 'mvcc_demo'")
        print("统计:", cur.fetchone())

        cur.execute("VACUUM mvcc_demo")
        cur.execute("SELECT n_live_tup, n_dead_tup FROM pg_stat_user_tables WHERE relname = 'mvcc_demo'")
        print("VACUUM后:", cur.fetchone())

        cur.execute("DROP TABLE mvcc_demo")
        conn.close()

    except psycopg2.OperationalError as e:
        print(f"无法连接 PostgreSQL: {e}")

if __name__ == '__main__':
    main()