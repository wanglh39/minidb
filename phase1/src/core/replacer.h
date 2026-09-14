#ifndef MINIDB_REPLACER_H
#define MINIDB_REPLACER_H

#include <stdint.h>
#include <stdbool.h>

typedef int32_t frame_id_t;
#define INVALID_FRAME_ID (-1)

typedef enum {
    REPLACER_LRU = 0,
    REPLACER_CLOCK = 1,
    REPLACER_LRU_K = 2,
} replacer_type_t;

typedef struct replacer replacer_t;

replacer_t *replacer_create(replacer_type_t type, int capacity);
void        replacer_destroy(replacer_t *r);

void       replacer_pin(replacer_t *r, frame_id_t fid);
void       replacer_unpin(replacer_t *r, frame_id_t fid);
frame_id_t replacer_victim(replacer_t *r);

int        replacer_size(replacer_t *r);

#endif