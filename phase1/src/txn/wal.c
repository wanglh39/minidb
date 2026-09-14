#include "wal.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/*
 * WAL 记录磁盘格式（大端）：
 *   [rec_len 4B] [lsn 8B] [txn_id 4B] [type 1B] [type-specific data]
 *
 * BEGIN/COMMIT/ABORT: 无额外数据
 * UPDATE: [page_id 4B] [offset 2B] [length 2B] [old_data len B] [new_data len B]
 * CHECKPOINT: [max_lsn 8B]
 */

struct wal {
    FILE *fp;
    lsn_t last_lsn;
};

struct wal_iter {
    FILE *fp;
    uint8_t buf[PAGE_SIZE * 2 + 128];
    uint8_t data_buf[PAGE_SIZE];
};

static void wr_u32(FILE *fp, uint32_t v) {
    uint8_t b[4] = { (uint8_t)(v>>24), (uint8_t)(v>>16), (uint8_t)(v>>8), (uint8_t)v };
    fwrite(b, 1, 4, fp);
}

static void wr_u64(FILE *fp, uint64_t v) {
    uint8_t b[8];
    for (int i = 0; i < 8; i++) b[i] = (uint8_t)(v >> (56 - i * 8));
    fwrite(b, 1, 8, fp);
}

static void wr_u16(FILE *fp, uint16_t v) {
    uint8_t b[2] = { (uint8_t)(v>>8), (uint8_t)v };
    fwrite(b, 1, 2, fp);
}

static uint32_t rd_u32(const uint8_t *p) {
    return ((uint32_t)p[0]<<24)|((uint32_t)p[1]<<16)|((uint32_t)p[2]<<8)|p[3];
}

static uint64_t rd_u64(const uint8_t *p) {
    uint64_t v = 0;
    for (int i = 0; i < 8; i++) v = (v << 8) | p[i];
    return v;
}

static uint16_t rd_u16(const uint8_t *p) {
    return (uint16_t)(((uint16_t)p[0] << 8) | p[1]);
}

static lsn_t write_record_header(FILE *fp, txn_id_t txn_id, log_type_t type,
                                 uint32_t extra_len) {
    fseek(fp, 0, SEEK_END);
    long pos = ftell(fp);
    lsn_t lsn = (lsn_t)pos;

    uint32_t rec_len = 8 + 4 + 1 + extra_len;
    wr_u32(fp, rec_len);
    wr_u64(fp, lsn);
    wr_u32(fp, txn_id);
    fputc((int)type, fp);

    return lsn;
}

wal_t *wal_open(const char *path) {
    FILE *fp = fopen(path, "r+b");
    if (!fp) {
        fp = fopen(path, "w+b");
        if (!fp) return NULL;
    }
    fseek(fp, 0, SEEK_END);

    wal_t *wal = malloc(sizeof(wal_t));
    wal->fp = fp;
    wal->last_lsn = 0;
    return wal;
}

void wal_close(wal_t *wal) {
    if (!wal) return;
    if (wal->fp) {
        fflush(wal->fp);
        fclose(wal->fp);
    }
    free(wal);
}

lsn_t wal_begin(wal_t *wal, txn_id_t txn_id) {
    lsn_t lsn = write_record_header(wal->fp, txn_id, LOG_BEGIN, 0);
    fflush(wal->fp);
    wal->last_lsn = lsn;
    return lsn;
}

lsn_t wal_commit(wal_t *wal, txn_id_t txn_id) {
    lsn_t lsn = write_record_header(wal->fp, txn_id, LOG_COMMIT, 0);
    fflush(wal->fp);
    wal->last_lsn = lsn;
    return lsn;
}

lsn_t wal_abort(wal_t *wal, txn_id_t txn_id) {
    lsn_t lsn = write_record_header(wal->fp, txn_id, LOG_ABORT, 0);
    fflush(wal->fp);
    wal->last_lsn = lsn;
    return lsn;
}

lsn_t wal_update(wal_t *wal, txn_id_t txn_id,
                 page_id_t pid, uint16_t offset, uint16_t len,
                 const void *old_data, const void *new_data) {
    uint32_t extra = 4 + 2 + 2 + len + len;
    lsn_t lsn = write_record_header(wal->fp, txn_id, LOG_UPDATE, extra);

    wr_u32(wal->fp, pid);
    wr_u16(wal->fp, offset);
    wr_u16(wal->fp, len);
    fwrite(old_data, 1, len, wal->fp);
    fwrite(new_data, 1, len, wal->fp);
    fflush(wal->fp);

    wal->last_lsn = lsn;
    return lsn;
}

lsn_t wal_checkpoint(wal_t *wal, lsn_t max_lsn) {
    lsn_t lsn = write_record_header(wal->fp, 0, LOG_CHECKPOINT, 8);
    wr_u64(wal->fp, max_lsn);
    fflush(wal->fp);
    wal->last_lsn = lsn;
    return lsn;
}

void wal_flush(wal_t *wal) {
    fflush(wal->fp);
}

lsn_t wal_last_lsn(wal_t *wal) {
    return wal->last_lsn;
}

/* ---------- 遍历器 ---------- */

wal_iter_t *wal_iter_open(wal_t *wal) {
    wal_iter_t *it = malloc(sizeof(wal_iter_t));
    it->fp = wal->fp;
    fseek(it->fp, 0, SEEK_SET);
    return it;
}

bool wal_iter_next(wal_iter_t *it, log_record_t *rec) {
    FILE *fp = it->fp;

    uint8_t len_buf[4];
    size_t n = fread(len_buf, 1, 4, fp);
    if (n != 4) return false;

    uint32_t rec_len = rd_u32(len_buf);
    if (rec_len == 0 || rec_len > sizeof(it->buf)) return false;

    n = fread(it->buf, 1, rec_len, fp);
    if (n != rec_len) return false;

    const uint8_t *p = it->buf;
    rec->lsn = rd_u64(p);       p += 8;
    rec->txn_id = rd_u32(p);    p += 4;
    rec->type = (log_type_t)*p; p += 1;

    rec->page_id = INVALID_PAGE_ID;
    rec->offset = 0;
    rec->length = 0;
    rec->old_data = NULL;
    rec->new_data = NULL;

    if (rec->type == LOG_UPDATE) {
        rec->page_id = rd_u32(p);  p += 4;
        rec->offset = rd_u16(p);   p += 2;
        rec->length = rd_u16(p);   p += 2;

        memcpy(it->data_buf, p, rec->length);
        rec->old_data = it->data_buf;
        rec->new_data = it->data_buf + rec->length;
    }

    return true;
}

void wal_iter_close(wal_iter_t *it) {
    free(it);
}