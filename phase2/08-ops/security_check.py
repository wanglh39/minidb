"""PostgreSQL 安全检查：pg_hba.conf 配置、用户权限、SSL 配置、密码策略、数据加密"""
import os
import re
import sys

import psycopg2
from psycopg2.extras import RealDictCursor


def get_conn():
    return psycopg2.connect(
        host=os.getenv("PG_HOST", "localhost"),
        port=os.getenv("PG_PORT", "5432"),
        dbname=os.getenv("PG_DB", "postgres"),
        user=os.getenv("PG_USER", "postgres"),
        password=os.getenv("PG_PASS", "secret"),
    )


CHECKS = []
WARNINGS = []
ERRORS = []


def ok(msg):
    CHECKS.append(("OK", msg))
    print(f"  [OK]   {msg}")


def warn(msg):
    WARNINGS.append(msg)
    CHECKS.append(("WARN", msg))
    print(f"  [WARN] {msg}")


def error(msg):
    ERRORS.append(msg)
    CHECKS.append(("ERROR", msg))
    print(f"  [ERROR] {msg}")


def section(title):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def check_ssl_config():
    section("1. SSL 配置检查")
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SHOW ssl")
    ssl_on = cur.fetchone()[0]
    if ssl_on == "on":
        ok(f"SSL 已开启 (ssl = {ssl_on})")
    else:
        error(f"SSL 未开启 (ssl = {ssl_on})，生产环境必须启用 SSL")

    cur.execute("SHOW ssl_cert_file")
    cert = cur.fetchone()[0]
    cur.execute("SHOW ssl_key_file")
    key = cur.fetchone()[0]
    cur.execute("SHOW ssl_ca_file")
    ca = cur.fetchone()[0]
    print(f"  证书文件: {cert}")
    print(f"  私钥文件: {key}")
    print(f"  CA 文件:  {ca}")

    cur.execute("SHOW ssl_ciphers")
    ciphers = cur.fetchone()[0]
    print(f"  加密套件: {ciphers}")

    cur.execute("SHOW ssl_min_protocol_version")
    min_proto = cur.fetchone()[0]
    print(f"  最低协议版本: {min_proto}")
    if min_proto in ("TLSv1.2", "TLSv1.3"):
        ok(f"SSL 最低协议版本安全 ({min_proto})")
    else:
        warn(f"SSL 最低协议版本偏低 ({min_proto})，建议至少 TLSv1.2")

    cur.execute("""
        SELECT pid, usename, application_name, client_addr, ssl, ssl_version, ssl_cipher
        FROM pg_stat_ssl
        JOIN pg_stat_activity USING (pid)
        WHERE ssl IS NOT NULL
        LIMIT 10
    """)
    ssl_conns = cur.fetchall()
    if ssl_conns:
        print(f"  当前 SSL 连接示例:")
        for pid, user, app, addr, ssl, ver, cipher in ssl_conns:
            status = "SSL" if ssl else "明文"
            print(f"    PID={pid} 用户={user} 地址={addr} {status} {ver} {cipher}")

    conn.close()


def check_pg_hba():
    section("2. pg_hba.conf 配置检查")
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SHOW hba_file")
    hba_path = cur.fetchone()[0]
    print(f"  pg_hba.conf 路径: {hba_path}")

    try:
        with open(hba_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except (FileNotFoundError, PermissionError) as e:
        warn(f"无法读取 pg_hba.conf: {e}")
        print("  将通过 pg_hba_file_rules 视图检查")
        cur.execute("""
            SELECT line_number, type, database, user_name, address, auth_method, options
            FROM pg_hba_file_rules
            ORDER BY line_number
        """)
        rules = cur.fetchall()
        _analyze_hba_rules(rules)
        conn.close()
        return

    print(f"  共 {len(lines)} 行")
    print()
    rules = []
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) >= 4:
            rtype = parts[0]
            db = parts[1]
            user = parts[2]
            addr = parts[3] if len(parts) > 3 else ""
            method = parts[4] if len(parts) > 4 else ""
            rules.append((i, rtype, db, user, addr, method, ""))

    _analyze_hba_rules(rules)

    print()
    print(f"  有效规则列表:")
    print(f"  {'行号':>6} {'类型':<10} {'数据库':<15} {'用户':<15} {'地址':<20} {'方法':<12}")
    print(f"  {'-' * 82}")
    for r in rules:
        print(f"  {r[0]:>6} {r[1]:<10} {r[2]:<15} {r[3]:<15} {r[4]:<20} {r[5]:<12}")

    conn.close()


def _analyze_hba_rules(rules):
    has_md5 = any(r[5] == "md5" for r in rules)
    has_password = any(r[5] == "password" for r in rules)
    has_trust = any(r[5] == "trust" for r in rules)
    has_ident = any(r[5] == "ident" for r in rules)
    has_scram = any(r[5] in ("scram-sha-256",) for r in rules)

    if has_trust:
        error("存在 trust 认证（免密登录），极度危险，必须移除")
    if has_password:
        error("存在 password 认证（明文传输密码），必须改为 scram-sha-256 或 md5")
    if has_md5 and not has_scram:
        warn("使用 md5 认证，建议升级为 scram-sha-256（PG 13+ 默认）")
    if has_scram:
        ok("使用 scram-sha-256 认证（推荐）")

    for r in rules:
        line, rtype, db, user, addr, method, _ = r
        if addr in ("0.0.0.0/0", "::/0"):
            if method in ("trust", "password"):
                error(f"第{line}行: 对全网开放且认证方式不安全 ({method})")
            elif method in ("md5", "scram-sha-256"):
                warn(f"第{line}行: 对全网开放 ({addr})，生产环境应限制来源 IP")

    for r in rules:
        line, rtype, db, user, addr, method, _ = r
        if rtype == "local" and method == "trust":
            error(f"第{line}行: 本地连接使用 trust，本地提权风险")


def check_user_permissions():
    section("3. 用户权限检查")
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            rolname,
            rolsuper,
            rolcreatedb,
            rolcreaterole,
            rolreplication,
            rolbypassrls,
            rolcanlogin,
            rolvaliduntil
        FROM pg_roles
        ORDER BY rolname
    """)
    roles = cur.fetchall()
    print(f"  角色总数: {len(roles)}")
    print()
    print(f"  {'角色':<25} {'超管':>5} {'建库':>5} {'建角色':>6} {'复制':>5} {'绕RLS':>6} {'可登录':>6} {'有效期':<20}")
    print(f"  {'-' * 90}")
    for name, sup, cdb, cr, rep, brls, login, valid in roles:
        def yn(v):
            return "是" if v else ""
        valid_str = str(valid).split(".")[0] if valid else "永不过期"
        print(f"  {name:<25} {yn(sup):>5} {yn(cdb):>5} {yn(cr):>6} {yn(rep):>5} {yn(brls):>6} {yn(login):>6} {valid_str:<20}")

    print()
    superusers = [r for r in roles if r[1]]
    if len(superusers) > 2:
        warn(f"超级用户数 {len(superusers)} 偏多，建议仅保留 1-2 个")
    else:
        ok(f"超级用户数 {len(superusers)} 合理")

    for name, sup, cdb, cr, rep, brls, login, valid in roles:
        if name == "postgres" and not sup:
            warn("postgres 角色不是超级用户（可能被降权）")
        if login and valid is None and name not in ("postgres",):
            warn(f"登录角色 '{name}' 密码永不过期，建议设置 rolvaliduntil")

    cur.execute("""
        SELECT
            n.nspname,
            c.relname,
            pg_get_userbyid(c.relowner) AS owner
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'v', 'm')
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND c.relowner != (SELECT oid FROM pg_roles WHERE rolname = 'postgres')
        ORDER BY n.nspname, c.relname
        LIMIT 20
    """)
    non_pg_owned = cur.fetchall()
    if non_pg_owned:
        print()
        print(f"  非 postgres 拥有的对象（前 20）:")
        for nsp, rel, owner in non_pg_owned:
            print(f"    {nsp}.{rel} -> owner: {owner}")

    conn.close()


def check_password_policy():
    section("4. 密码策略检查")
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SHOW password_encryption")
    enc = cur.fetchone()[0]
    print(f"  密码加密方式 (password_encryption): {enc}")
    if enc == "scram-sha-256":
        ok("使用 scram-sha-256 加密密码（推荐）")
    elif enc == "md5":
        warn("使用 md5 加密密码，建议改为 scram-sha-256")
    else:
        warn(f"密码加密方式: {enc}")

    cur.execute("""
        SELECT
            rolname,
            rolcanlogin,
            (rolpassword IS NOT NULL) AS has_password,
            CASE
                WHEN rolpassword IS NULL THEN '无密码'
                WHEN rolpassword LIKE 'SCRAM-SHA-256%' THEN 'scram-sha-256'
                WHEN rolpassword LIKE 'md5%' THEN 'md5'
                ELSE '其他'
            END AS password_type
        FROM pg_authid
        WHERE rolcanlogin = true
        ORDER BY rolname
    """)
    login_roles = cur.fetchall()
    print()
    print(f"  可登录角色的密码状态:")
    print(f"  {'角色':<25} {'有密码':>8} {'类型':<15}")
    print(f"  {'-' * 50}")
    for name, login, has_pwd, ptype in login_roles:
        print(f"  {name:<25} {'是' if has_pwd else '否':>8} {ptype:<15}")

    for name, login, has_pwd, ptype in login_roles:
        if not has_pwd:
            error(f"可登录角色 '{name}' 无密码")
        if ptype == "md5":
            warn(f"角色 '{name}' 使用 md5 密码，建议改用 scram-sha-256")

    cur.execute("SHOW password_check_hook")
    hook = cur.fetchone()[0]
    if hook and hook != "(none)":
        ok(f"已配置密码检查钩子: {hook}")
    else:
        warn("未配置 password_check_hook，密码复杂度无强制校验")

    conn.close()


def check_connection_security():
    section("5. 连接安全检查")
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SHOW listen_addresses")
    listen = cur.fetchone()[0]
    print(f"  listen_addresses: {listen}")
    if listen == "*":
        warn("监听所有网卡，生产环境应限制为特定 IP")
    elif listen == "localhost":
        ok("仅监听 localhost（本地访问）")
    else:
        ok(f"监听指定地址: {listen}")

    cur.execute("SHOW port")
    port = cur.fetchone()[0]
    print(f"  端口: {port}")

    cur.execute("SHOW max_connections")
    max_conn = cur.fetchone()[0]
    print(f"  max_connections: {max_conn}")

    cur.execute("SHOW superuser_reserved_connections")
    reserved = cur.fetchone()[0]
    print(f"  超管保留连接: {reserved}")
    if int(reserved) < 3:
        warn("超管保留连接数偏低，连接耗尽时无法介入排查")

    cur.execute("SHOW statement_timeout")
    stmt_timeout = cur.fetchone()[0]
    print(f"  全局 statement_timeout: {stmt_timeout}")
    if stmt_timeout == "0":
        warn("statement_timeout = 0（无限制），长查询可能耗尽资源")

    cur.execute("SHOW idle_in_transaction_session_timeout")
    idle_timeout = cur.fetchone()[0]
    print(f"  idle_in_transaction_session_timeout: {idle_timeout}")
    if idle_timeout == "0":
        warn("idle_in_transaction_session_timeout = 0，idle in txn 连接不会被自动清理")

    cur.execute("SHOW connection_security")
    conn_sec = cur.fetchone()[0]
    print(f"  connection_security: {conn_sec}")

    conn.close()


def check_data_encryption():
    section("6. 数据加密检查")
    conn = get_conn()
    cur = conn.cursor()

    print("  [信息] PostgreSQL 本身不提供透明数据加密（TDE），需通过以下方式:")
    print("    1. 磁盘级加密: LUKS / dm-crypt / EBS 加密")
    print("    2. 文件系统级: ZFS 加密 / eCryptfs")
    print("    3. 列级加密:   pgcrypto 扩展")

    cur.execute("""
        SELECT extname FROM pg_extension WHERE extname = 'pgcrypto'
    """)
    has_pgcrypto = cur.fetchone()
    if has_pgcrypto:
        ok("已安装 pgcrypto 扩展，可做列级加密")
    else:
        warn("未安装 pgcrypto 扩展，敏感字段无法列级加密")
        print("    安装: CREATE EXTENSION pgcrypto;")
        print("    用法: INSERT INTO users (name, ssn) VALUES ('alice', encrypt('123-45-6789', 'key', 'aes'));")

    cur.execute("""
        SELECT
            n.nspname, c.relname,
            a.attname, format_type(a.atttypid, a.atttypmod) AS type
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid
        WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND c.relkind = 'r'
          AND a.attnum > 0
          AND a.attname ~* '(password|passwd|secret|token|key|salt|ssn|credit)'
        ORDER BY n.nspname, c.relname, a.attname
        LIMIT 20
    """)
    sensitive = cur.fetchall()
    if sensitive:
        print()
        print(f"  疑似敏感字段（名称匹配）:")
        for nsp, rel, att, typ in sensitive:
            print(f"    {nsp}.{rel}.{att} ({typ})")
        cur.execute("""
            SELECT n.nspname, c.relname, a.attname
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_attribute a ON a.attrelid = c.oid
            WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND c.relkind = 'r'
              AND a.attnum > 0
              AND a.attname ~* '(password|passwd|secret|token)'
              AND format_type(a.atttypid, a.atttypmod) = 'text'
        """)
        plaintext = cur.fetchall()
        if plaintext:
            warn("敏感字段使用明文 text 类型，建议用 bytea 或加密函数处理")
            for nsp, rel, att in plaintext:
                print(f"    {nsp}.{rel}.{att}")

    conn.close()


def check_audit_logging():
    section("7. 审计日志检查")
    conn = get_conn()
    cur = conn.cursor()

    settings = [
        "log_connections",
        "log_disconnections",
        "log_statement",
        "log_duration",
        "log_min_duration_statement",
        "log_lock_waits",
        "log_temp_files",
        "log_checkpoints",
        "log_line_prefix",
        "log_directory",
    ]
    print(f"  {'参数':<35} {'值':<30}")
    print(f"  {'-' * 67}")
    for s in settings:
        cur.execute(f"SHOW {s}")
        val = cur.fetchone()[0]
        print(f"  {s:<35} {val:<30}")

    print()
    cur.execute("SHOW log_statement")
    log_stmt = cur.fetchone()[0]
    if log_stmt == "none":
        warn("log_statement = none，未记录任何 SQL 语句")
    elif log_stmt == "all":
        ok("log_statement = all，记录所有 SQL（审计充分但日志量大）")
    else:
        ok(f"log_statement = {log_stmt}")

    cur.execute("SHOW log_connections")
    if cur.fetchone()[0] == "on":
        ok("已记录连接日志")
    else:
        warn("未记录连接日志 (log_connections = off)")

    cur.execute("SHOW log_lock_waits")
    if cur.fetchone()[0] == "on":
        ok("已记录锁等待")
    else:
        warn("未记录锁等待 (log_lock_waits = off)，排查锁问题困难")

    conn.close()


def check_rls_policies():
    section("8. 行级安全（RLS）策略检查")
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            n.nspname,
            c.relname,
            c.relrowsecurity AS rls_enabled,
            c.relforcerowsecurity AS rls_forced
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND c.relkind = 'r'
        ORDER BY n.nspname, c.relname
    """)
    tables = cur.fetchall()
    rls_enabled = [t for t in tables if t[2]]
    print(f"  用户表总数: {len(tables)}")
    print(f"  启用 RLS 表数: {len(rls_enabled)}")

    if rls_enabled:
        print()
        print(f"  {'schema':<12} {'表':<25} {'RLS':>5} {'强制':>5}")
        print(f"  {'-' * 50}")
        for nsp, rel, en, force in rls_enabled:
            print(f"  {nsp:<12} {rel:<25} {'是' if en else '':>5} {'是' if force else '':>5}")

    cur.execute("""
        SELECT
            schemaname, tablename, policyname, permissive, roles, cmd, qual
        FROM pg_policies
        ORDER BY schemaname, tablename, policyname
        LIMIT 20
    """)
    policies = cur.fetchall()
    if policies:
        print()
        print(f"  RLS 策略（前 20）:")
        for nsp, tbl, pname, perm, roles, cmd, qual in policies:
            print(f"    {nsp}.{tbl}.{pname} ({cmd}) perm={perm} roles={roles}")
            if qual:
                print(f"      USING: {qual}")

    conn.close()


def generate_report():
    section("安全检查报告")
    total = len(CHECKS)
    ok_count = len([c for c in CHECKS if c[0] == "OK"])
    warn_count = len(WARNINGS)
    error_count = len(ERRORS)

    print(f"  总检查项: {total}")
    print(f"  通过:     {ok_count}")
    print(f"  警告:     {warn_count}")
    print(f"  错误:     {error_count}")
    print()

    if ERRORS:
        print(f"  [严重] 需立即修复的安全问题:")
        for i, e in enumerate(ERRORS, 1):
            print(f"    {i}. {e}")
        print()

    if WARNINGS:
        print(f"  [建议] 需关注的安全警告:")
        for i, w in enumerate(WARNINGS, 1):
            print(f"    {i}. {w}")

    print()
    if error_count == 0 and warn_count == 0:
        print(f"  [结论] 安全配置良好")
    elif error_count == 0:
        print(f"  [结论] 无严重问题，建议处理 {warn_count} 个警告")
    else:
        print(f"  [结论] 存在 {error_count} 个严重安全问题，必须立即修复")

    return error_count, warn_count


def main():
    print("=" * 60)
    print("PostgreSQL 安全检查脚本")
    print("=" * 60)

    try:
        conn = get_conn()
        conn.close()
    except psycopg2.OperationalError as e:
        print(f"\n[失败] 无法连接 PostgreSQL: {e}")
        print("请设置环境变量: PG_HOST / PG_PORT / PG_DB / PG_USER / PG_PASS")
        sys.exit(1)

    check_ssl_config()
    check_pg_hba()
    check_user_permissions()
    check_password_policy()
    check_connection_security()
    check_data_encryption()
    check_audit_logging()
    check_rls_policies()
    generate_report()

    print()
    print("=" * 60)
    print("安全检查完成")
    print("=" * 60)

    if ERRORS:
        sys.exit(2)


if __name__ == "__main__":
    main()