"""
generate_tpch.py — 生成简化版 TPC-H 数据集（SF=0.01）

产出：
  - data/tpch.db (SQLite)
  - 三张表：customer(1500) / orders(15000) / lineitem(60000)
  - 提供 Q1-Q5 的执行函数，供 index_experiments.py 调用

运行：
  python generate_tpch.py
"""
import os
import random
import sqlite3
from datetime import date, timedelta

SF = 0.01
N_CUSTOMER = int(150000 * SF)
N_ORDERS_PER_CUST = 10
N_LINEITEM_PER_ORDER = 4
SEED = 42

REGIONS = ["ASIA", "EUROPE", "AMERICA", "AFRICA", "MIDDLE EAST"]
NATIONS = {
    "ASIA": ["CHINA", "JAPAN", "INDIA", "VIETNAM", "INDONESIA"],
    "EUROPE": ["GERMANY", "FRANCE", "UK", "ITALY", "SPAIN"],
    "AMERICA": ["USA", "CANADA", "BRAZIL", "ARGENTINA", "MEXICO"],
    "AFRICA": ["EGYPT", "KENYA", "NIGERIA", "SOUTH AFRICA", "MOROCCO"],
    "MIDDLE EAST": ["IRAN", "IRAQ", "SAUDI ARABIA", "TURKEY", "UAE"],
}
ORDER_STATUS = ["O", "F", "P"]
LINE_STATUS = ["O", "F"]
RETURN_FLAG = ["N", "R", "A"]

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DATA_DIR, "tpch.db")


def _rng():
    return random.Random(SEED)


def _date_to_str(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def _random_date(rng: random.Random, start: date, end: date) -> date:
    delta = (end - start).days
    return start + timedelta(days=rng.randint(0, delta))


def create_schema(conn: sqlite3.Connection):
    conn.executescript(
        """
        DROP TABLE IF EXISTS lineitem;
        DROP TABLE IF EXISTS orders;
        DROP TABLE IF EXISTS customer;

        CREATE TABLE customer (
            c_custkey     INTEGER PRIMARY KEY,
            c_name        TEXT    NOT NULL,
            c_address     TEXT,
            c_nation      TEXT    NOT NULL,
            c_region      TEXT    NOT NULL,
            c_phone       TEXT,
            c_acctbal     REAL,
            c_mktsegment  TEXT,
            c_comment     TEXT
        );

        CREATE TABLE orders (
            o_orderkey      INTEGER PRIMARY KEY,
            o_custkey       INTEGER NOT NULL,
            o_orderstatus   TEXT    NOT NULL,
            o_totalprice    REAL    NOT NULL,
            o_orderdate     TEXT    NOT NULL,
            o_orderpriority TEXT,
            o_clerk         TEXT,
            o_shippriority  INTEGER,
            o_comment       TEXT
        );

        CREATE TABLE lineitem (
            l_orderkey      INTEGER NOT NULL,
            l_partkey       INTEGER,
            l_suppkey       INTEGER,
            l_linenumber    INTEGER NOT NULL,
            l_quantity      REAL    NOT NULL,
            l_extendedprice REAL    NOT NULL,
            l_discount      REAL    NOT NULL,
            l_tax           REAL    NOT NULL,
            l_returnflag    TEXT    NOT NULL,
            l_linestatus    TEXT    NOT NULL,
            l_shipdate      TEXT    NOT NULL,
            l_commitdate    TEXT,
            l_receiptdate   TEXT,
            l_shipinstruct  TEXT,
            l_shipmode      TEXT,
            l_comment       TEXT,
            PRIMARY KEY (l_orderkey, l_linenumber)
        );
        """
    )


def generate_customer(conn: sqlite3.Connection, rng: random.Random):
    rows = []
    for ck in range(1, N_CUSTOMER + 1):
        region = rng.choice(REGIONS)
        nation = rng.choice(NATIONS[region])
        rows.append(
            (
                ck,
                f"Customer_{ck:09d}",
                f"Address_{ck}_{rng.randint(1, 1000)}",
                nation,
                region,
                f"{rng.randint(10, 99)}-{rng.randint(100, 999)}-{rng.randint(100, 999)}-{rng.randint(1000, 9999)}",
                round(rng.uniform(-999.99, 9999.99), 2),
                rng.choice(["BUILDING", "AUTOMOBILE", "MACHINERY", "HOUSEHOLD", "FURNITURE"]),
                f"comment {ck}",
            )
        )
    conn.executemany(
        """
        INSERT INTO customer VALUES (?,?,?,?,?,?,?,?,?)
        """,
        rows,
    )
    return rows


def generate_orders(conn: sqlite3.Connection, rng: random.Random):
    rows = []
    ok = 1
    start = date(1992, 1, 1)
    end = date(1998, 12, 31)
    for ck in range(1, N_CUSTOMER + 1):
        for _ in range(N_ORDERS_PER_CUST):
            odate = _random_date(rng, start, end)
            status = rng.choices(ORDER_STATUS, weights=[0.5, 0.4, 0.1])[0]
            total = round(rng.uniform(100.0, 50000.0), 2)
            rows.append(
                (
                    ok,
                    ck,
                    status,
                    total,
                    _date_to_str(odate),
                    rng.choice(["1-URGENT", "2-HIGH", "3-MEDIUM", "4-NOT SPECIFIED", "5-LOW"]),
                    f"Clerk#{rng.randint(1, 1000):09d}",
                    rng.choice([0, 1]),
                    f"order comment {ok}",
                )
            )
            ok += 1
    conn.executemany(
        """
        INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?)
        """,
        rows,
    )
    return rows


def generate_lineitem(conn: sqlite3.Connection, rng: random.Random, n_orders: int):
    rows = []
    for ok in range(1, n_orders + 1):
        odate = date(1992, 1, 1) + timedelta(days=rng.randint(0, 2555))
        n_items = N_LINEITEM_PER_ORDER
        for ln in range(1, n_items + 1):
            qty = rng.randint(1, 50)
            price = round(rng.uniform(100.0, 2000.0), 2)
            ext_price = round(qty * price, 2)
            disc = round(rng.uniform(0.0, 0.10), 2)
            tax = round(rng.uniform(0.0, 0.08), 2)
            rflag = rng.choice(RETURN_FLAG)
            lstatus = rng.choice(LINE_STATUS)
            ship = _random_date(rng, odate, odate + timedelta(days=30))
            commit = _random_date(rng, odate, odate + timedelta(days=20))
            receipt = ship + timedelta(days=rng.randint(1, 15))
            rows.append(
                (
                    ok,
                    rng.randint(1, 200000),
                    rng.randint(1, 10000),
                    ln,
                    qty,
                    ext_price,
                    disc,
                    tax,
                    rflag,
                    lstatus,
                    _date_to_str(ship),
                    _date_to_str(commit),
                    _date_to_str(receipt),
                    rng.choice(["DELIVER IN PERSON", "COLLECT COD", "NONE", "TAKE BACK RETURN"]),
                    rng.choice(["REG AIR", "AIR", "RAIL", "SHIP", "TRUCK", "MAIL", "FOB"]),
                    f"line comment {ok}-{ln}",
                )
            )
    conn.executemany(
        """
        INSERT INTO lineitem VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        rows,
    )
    return rows


def build(db_path: str = DB_PATH, verbose: bool = True) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    if os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    rng = _rng()
    if verbose:
        print(f"[generate_tpch] SF={SF}  customer={N_CUSTOMER}  orders={N_CUSTOMER*N_ORDERS_PER_CUST}  lineitem={N_CUSTOMER*N_ORDERS_PER_CUST*N_LINEITEM_PER_ORDER}")
        print(f"[generate_tpch] DB: {db_path}")

    create_schema(conn)
    if verbose:
        print("[generate_tpch] generating customer ...")
    generate_customer(conn, rng)
    conn.commit()
    if verbose:
        print("[generate_tpch] generating orders ...")
    generate_orders(conn, rng)
    conn.commit()
    n_orders = N_CUSTOMER * N_ORDERS_PER_CUST
    if verbose:
        print("[generate_tpch] generating lineitem ...")
    generate_lineitem(conn, rng, n_orders)
    conn.commit()

    conn.execute("ANALYZE")
    if verbose:
        for t in ("customer", "orders", "lineitem"):
            cnt = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"[generate_tpch] {t}: {cnt} rows")
    return conn


def load_queries() -> dict:
    sql_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "queries", "tpch_queries.sql")
    with open(sql_path, "r", encoding="utf-8") as f:
        text = f.read()

    queries = {}
    current = None
    buf = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("-- Q") and len(s) <= 6:
            if current is not None:
                queries[current] = "\n".join(buf).strip()
            current = s.replace("-- ", "")
            buf = []
        elif current is not None and not s.startswith("--") and s:
            buf.append(line)
    if current is not None:
        queries[current] = "\n".join(buf).strip()
    return queries


def run_q1_to_q5(conn: sqlite3.Connection, verbose: bool = True):
    queries = load_queries()
    results = {}
    for qid in ["Q1", "Q2", "Q3", "Q4", "Q5"]:
        sql = queries.get(qid)
        if not sql:
            continue
        cur = conn.execute(sql)
        rows = cur.fetchall()
        results[qid] = rows
        if verbose:
            print(f"[run] {qid}: {len(rows)} rows  cols={len(cur.description) if cur.description else 0}")
    return results


def main():
    conn = build()
    print("\n[main] running Q1-Q5 (no index) ...")
    run_q1_to_q5(conn)
    conn.close()
    print(f"\n[main] done. DB at {DB_PATH}")


if __name__ == "__main__":
    main()