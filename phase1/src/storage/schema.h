#ifndef MINIDB_SCHEMA_H
#define MINIDB_SCHEMA_H

#include <stdint.h>
#include <stdbool.h>

#define MAX_COLS 16
#define MAX_NAME_LEN 32

typedef enum {
    COL_INT32 = 0,
    COL_INT64 = 1,
    COL_FLOAT = 2,
} col_type_t;

typedef struct {
    char name[MAX_NAME_LEN];
    col_type_t type;
    bool nullable;
} col_def_t;

typedef struct {
    char name[MAX_NAME_LEN];
    int num_cols;
    col_def_t cols[MAX_COLS];
} schema_t;

void     schema_init(schema_t *s, const char *name);
int      schema_add_col(schema_t *s, const char *name, col_type_t type, bool nullable);
uint16_t schema_col_size(col_type_t type);
uint16_t schema_null_bm_size(const schema_t *s);
uint16_t schema_tuple_size(const schema_t *s);
int      schema_find_col(const schema_t *s, const char *name);

#endif