-- TPC-H 简化版 Q1-Q5 标准 SQL
-- 数据集：customer / orders / lineitem（SF=0.01）
-- 适配 SQLite 语法（SQLite 无 EXTRACT，用 strftime）

-- ============================================================
-- Q1：价目汇总（lineitem 按 shipdate 过滤，聚合）
-- 业务：统计某日期前已发货商品的按 returnflag、linestatus 分组的
--       数量、金额、折扣后金额等汇总。索引收益最大场景之一。
-- 适合索引：lineitem(l_shipdate)
-- ============================================================
-- Q1
SELECT
    l_returnflag,
    l_linestatus,
    SUM(l_quantity)                                       AS sum_qty,
    SUM(l_extendedprice)                                  AS sum_base_price,
    SUM(l_extendedprice * (1 - l_discount))               AS sum_disc_price,
    SUM(l_extendedprice * (1 - l_discount) * (1 + l_tax)) AS sum_charge,
    AVG(l_quantity)                                       AS avg_qty,
    AVG(l_extendedprice)                                  AS avg_price,
    AVG(l_discount)                                       AS avg_disc,
    COUNT(*)                                              AS count_order
FROM lineitem
WHERE l_shipdate <= date('1993-06-02')
GROUP BY l_returnflag, l_linestatus
ORDER BY l_returnflag, l_linestatus;


-- ============================================================
-- Q2：按客户查订单（按 c_custkey 过滤 customer，JOIN orders）
-- 业务：给定客户编号，查其所有订单的简要信息。
-- 适合索引：customer(c_custkey) 主键、orders(o_custkey)
-- ============================================================
-- Q2
SELECT
    c.c_custkey,
    c.c_name,
    o.o_orderkey,
    o.o_orderdate,
    o.o_totalprice,
    o.o_orderstatus
FROM customer AS c
JOIN orders AS o ON c.c_custkey = o.o_custkey
WHERE c.c_custkey = 100
ORDER BY o.o_orderdate DESC;


-- ============================================================
-- Q3：按状态+日期过滤订单（o_orderstatus='F' 且 o_orderdate < d）
-- 业务：查询未发货（F 表示 final/已结算，此处借用为过滤演示）
--       且在某日期前的订单，按 o_orderdate 升序返回。
-- 适合复合索引：orders(o_orderstatus, o_orderdate)
-- ============================================================
-- Q3
SELECT
    o.o_orderkey,
    o.o_custkey,
    o.o_orderdate,
    o.o_totalprice,
    c.c_name
FROM orders AS o
JOIN customer AS c ON o.o_custkey = c.c_custkey
WHERE o.o_orderstatus = 'F'
  AND o.o_orderdate < date('1993-03-15')
ORDER BY o.o_orderdate;


-- ============================================================
-- Q4：按日期范围 + 客户地区过滤（JOIN customer）
-- 业务：查询某日期范围内、客户地区为 ASIA 的订单，
--       统计每个订单的 lineitem 数量。
-- 适合索引：orders(o_orderdate) + customer(c_region)
-- 覆盖索引演示：orders(o_orderdate) INCLUDE (o_custkey, o_orderkey)
-- ============================================================
-- Q4
SELECT
    o.o_orderkey,
    o.o_orderdate,
    c.c_name,
    c.c_region,
    (SELECT COUNT(*) FROM lineitem l WHERE l.l_orderkey = o.o_orderkey) AS item_count
FROM orders AS o
JOIN customer AS c ON o.o_custkey = c.c_custkey
WHERE o.o_orderdate BETWEEN date('1994-01-01') AND date('1994-12-31')
  AND c.c_region = 'ASIA'
ORDER BY o.o_orderdate
LIMIT 100;


-- ============================================================
-- Q5：按客户名前缀过滤（c_name LIKE 'Customer_0001%'）
-- 业务：按客户名前缀模糊匹配，查其订单总额。
-- 适合索引：customer(c_name) —— 前缀 LIKE 可走索引
-- 失效对照：LIKE '%0001%' 不走索引
-- ============================================================
-- Q5
SELECT
    c.c_custkey,
    c.c_name,
    COUNT(o.o_orderkey)   AS order_count,
    SUM(o.o_totalprice)   AS total_amount
FROM customer AS c
JOIN orders AS o ON c.c_custkey = o.o_custkey
WHERE c.c_name LIKE 'Customer_0000001%'
GROUP BY c.c_custkey, c.c_name
ORDER BY total_amount DESC;


-- ============================================================
-- 失效对照查询（用于实验对比）
-- ============================================================

-- Q5-fail：前导 % 通配符，不走索引
-- SELECT c.c_name FROM customer AS c WHERE c.c_name LIKE '%0000001%';

-- Q1-fail：对 l_shipdate 施加 date() 函数，不走索引
-- SELECT COUNT(*) FROM lineitem WHERE date(l_shipdate) <= '1998-09-02';

-- Q3-fail：列在表达式左侧参与运算
-- SELECT * FROM orders WHERE o_orderdate + 1 < date('1995-03-15');