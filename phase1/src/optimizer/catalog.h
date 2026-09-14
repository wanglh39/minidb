#ifndef MINIDB_CATALOG_H
#define MINIDB_CATALOG_H

#include <stdint.h>
#include <stdbool.h>

#define CAT_MAX_TABLES 16
#define CAT_MAX_NAME 32

typedef struct {
    char name[CAT_MAX_NAME];
    int  num_rows;
    int  num_pages;
    bool has_index;
    char index_col[CAT_MAX_NAME];
} catalog_entry_t;

typedef struct {
    catalog_entry_t tables[CAT_MAX_TABLES];
    int num_tables;
} catalog_t;

catalog_t *catalog_create(void);
void       catalog_destroy(catalog_t *c);
void       catalog_add_table(catalog_t *c, const char *name,
                             int num_rows, int num_pages,
                             bool has_index, const char *index_col);
const catalog_entry_t *catalog_lookup(const catalog_t *c, const char *name);

#endif