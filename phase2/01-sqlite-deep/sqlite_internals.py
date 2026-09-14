"""探索 SQLite 内部结构：文件头、页类型、B-Tree、PRAGMA"""
import sqlite3
import struct
import os

def create_sample_db(path="sample.db"):
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER)")
    conn.execute("CREATE INDEX idx_age ON users(age)")
    for i in range(100):
        conn.execute("INSERT INTO users VALUES(?, ?, ?)", (i, f"user_{i}", 20 + i % 50))
    conn.commit()
    conn.close()
    return path

def read_file_header(path):
    with open(path, 'rb') as f:
        header = f.read(100)
    print("=== SQLite 文件头 ===")
    print(f"魔数: {header[0:16]}")
    page_size = struct.unpack('>H', header[16:18])[0]
    print(f"页大小: {page_size} bytes")
    print(f"写版本: {header[18]}")
    print(f"读版本: {header[19]}")
    print(f"文件变更计数: {struct.unpack('>I', header[24:28])[0]}")
    db_size = struct.unpack('>I', header[28:32])[0]
    print(f"数据库大小: {db_size} 页 ({db_size * page_size} bytes)")
    print(f"文本编码: {struct.unpack('>I', header[56:60])[0]} (1=UTF8, 2=UTF16le, 3=UTF16be)")
    print(f"用户版本: {struct.unpack('>I', header[60:64])[0]}")
    print(f"增量真空模式: {header[64]}")
    print(f"应用ID: {struct.unpack('>I', header[68:72])[0]}")
    return page_size

def read_page(path, page_num, page_size):
    with open(path, 'rb') as f:
        f.seek((page_num - 1) * page_size)
        return f.read(page_size)

def analyze_btree_page(page, page_num):
    page_types = {2: '内部索引', 5: '内部表', 10: '叶子索引', 13: '叶子表'}
    ptype = page[0]
    print(f"\n=== 页 {page_num} ===")
    print(f"类型: {page_types.get(ptype, f'未知({ptype})')}")

    if ptype in (2, 5, 10, 13):
        first_freeblock = struct.unpack('>H', page[1:3])[0]
        num_cells = struct.unpack('>H', page[3:5])[0]
        cell_content_start = struct.unpack('>H', page[5:7])[0]
        print(f"cell 数量: {num_cells}")
        print(f"空闲块起始: {first_freeblock}")
        print(f"cell 内容起始: {cell_content_start}")

        if ptype in (2, 5):
            right_most = struct.unpack('>I', page[8:12])[0]
            print(f"最右子页: {right_most}")

def query_pragmas(path):
    conn = sqlite3.connect(path)
    print("\n=== PRAGMA 查询 ===")
    pragmas = [
        'page_size', 'page_count', 'journal_mode',
        'freelist_count', 'schema_version', 'user_version',
        'encoding', 'cache_size', 'wal_autocheckpoint'
    ]
    for p in pragmas:
        try:
            result = conn.execute(f"PRAGMA {p}").fetchone()
            print(f"  {p}: {result[0]}")
        except:
            print(f"  {p}: N/A")
    conn.close()

def show_schema(path):
    conn = sqlite3.connect(path)
    print("\n=== Schema ===")
    for row in conn.execute("SELECT type, name, tbl_name, rootpage FROM sqlite_master"):
        print(f"  {row[0]:6s} {row[1]:15s} rootpage={row[3]}")
    conn.close()

def main():
    print("SQLite 内部结构探索\n")
    path = create_sample_db()
    page_size = read_file_header(path)

    for i in range(1, 5):
        page = read_page(path, i, page_size)
        analyze_btree_page(page, i)

    query_pragmas(path)
    show_schema(path)

    print(f"\n文件大小: {os.path.getsize(path)} bytes")

if __name__ == '__main__':
    main()