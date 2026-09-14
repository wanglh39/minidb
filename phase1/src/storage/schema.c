#include "schema.h"
#include <string.h>

void schema_init(schema_t *s, const char *name) {
    memset(s, 0, sizeof(schema_t));
    strncpy(s->name, name, MAX_NAME_LEN - 1);
    s->num_cols = 0;
}

int schema_add_col(schema_t *s, const char *name, col_type_t type, bool nullable) {
    if (s->num_cols >= MAX_COLS) return -1;
    col_def_t *col = &s->cols[s->num_cols];
    strncpy(col->name, name, MAX_NAME_LEN - 1);
    col->type = type;
    col->nullable = nullable;
    return s->num_cols++;
}

uint16_t schema_col_size(col_type_t type) {
    switch (type) {
        case COL_INT32: return 4;
        case COL_INT64: return 8;
        case COL_FLOAT: return 4;
    }
    return 0;
}

uint16_t schema_null_bm_size(const schema_t *s) {
    return (uint16_t)((s->num_cols + 7) / 8);
}

uint16_t schema_tuple_size(const schema_t *s) {
    uint16_t size = schema_null_bm_size(s);
    for (int i = 0; i < s->num_cols; i++) {
        size += schema_col_size(s->cols[i].type);
    }
    return size;
}

int schema_find_col(const schema_t *s, const char *name) {
    for (int i = 0; i < s->num_cols; i++) {
        if (strcmp(s->cols[i].name, name) == 0) return i;
    }
    return -1;
}