#ifndef MINIDB_TUPLE_H
#define MINIDB_TUPLE_H

#include "schema.h"
#include <stdint.h>
#include <stdbool.h>

typedef struct {
    const schema_t *schema;
    uint8_t *data;
    uint16_t length;
} tuple_t;

tuple_t *tuple_create(const schema_t *schema);
void     tuple_destroy(tuple_t *t);

void     tuple_set_int32(tuple_t *t, int col, int32_t val);
int32_t  tuple_get_int32(const tuple_t *t, int col);
void     tuple_set_int64(tuple_t *t, int col, int64_t val);
int64_t  tuple_get_int64(const tuple_t *t, int col);
void     tuple_set_float(tuple_t *t, int col, float val);
float    tuple_get_float(const tuple_t *t, int col);

void     tuple_set_null(tuple_t *t, int col);
bool     tuple_is_null(const tuple_t *t, int col);

uint16_t tuple_serialize(const tuple_t *t, uint8_t *buf);
void     tuple_deserialize(tuple_t *t, const uint8_t *buf, uint16_t len);

#endif