#include "tuple.h"
#include <stdlib.h>
#include <string.h>

static bool bm_is_null(const uint8_t *data, int col) {
    return (data[col / 8] >> (col % 8)) & 1;
}

static void bm_set_null(uint8_t *data, int col, bool is_null) {
    if (is_null) data[col / 8] |= (uint8_t)(1 << (col % 8));
    else         data[col / 8] &= (uint8_t)~(1 << (col % 8));
}

static uint16_t col_offset(const schema_t *s, int col) {
    uint16_t off = schema_null_bm_size(s);
    for (int i = 0; i < col; i++) {
        off += schema_col_size(s->cols[i].type);
    }
    return off;
}

tuple_t *tuple_create(const schema_t *schema) {
    tuple_t *t = malloc(sizeof(tuple_t));
    t->schema = schema;
    t->length = schema_tuple_size(schema);
    t->data = calloc(1, t->length);
    return t;
}

void tuple_destroy(tuple_t *t) {
    if (!t) return;
    free(t->data);
    free(t);
}

void tuple_set_int32(tuple_t *t, int col, int32_t val) {
    bm_set_null(t->data, col, false);
    memcpy(t->data + col_offset(t->schema, col), &val, 4);
}

int32_t tuple_get_int32(const tuple_t *t, int col) {
    int32_t val;
    memcpy(&val, t->data + col_offset(t->schema, col), 4);
    return val;
}

void tuple_set_int64(tuple_t *t, int col, int64_t val) {
    bm_set_null(t->data, col, false);
    memcpy(t->data + col_offset(t->schema, col), &val, 8);
}

int64_t tuple_get_int64(const tuple_t *t, int col) {
    int64_t val;
    memcpy(&val, t->data + col_offset(t->schema, col), 8);
    return val;
}

void tuple_set_float(tuple_t *t, int col, float val) {
    bm_set_null(t->data, col, false);
    memcpy(t->data + col_offset(t->schema, col), &val, 4);
}

float tuple_get_float(const tuple_t *t, int col) {
    float val;
    memcpy(&val, t->data + col_offset(t->schema, col), 4);
    return val;
}

void tuple_set_null(tuple_t *t, int col) {
    bm_set_null(t->data, col, true);
}

bool tuple_is_null(const tuple_t *t, int col) {
    return bm_is_null(t->data, col);
}

uint16_t tuple_serialize(const tuple_t *t, uint8_t *buf) {
    memcpy(buf, t->data, t->length);
    return t->length;
}

void tuple_deserialize(tuple_t *t, const uint8_t *buf, uint16_t len) {
    memcpy(t->data, buf, len);
    t->length = len;
}