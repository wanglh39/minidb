#include "catalog.h"
#include <stdlib.h>
#include <string.h>

catalog_t *catalog_create(void) {
    return calloc(1, sizeof(catalog_t));
}

void catalog_destroy(catalog_t *c) {
    free(c);
}

void catalog_add_table(catalog_t *c, const char *name,
                       int num_rows, int num_pages,
                       bool has_index, const char *index_col) {
    if (c->num_tables >= CAT_MAX_TABLES) return;
    catalog_entry_t *e = &c->tables[c->num_tables++];
    strncpy(e->name, name, CAT_MAX_NAME - 1);
    e->num_rows  = num_rows;
    e->num_pages = num_pages;
    e->has_index = has_index;
    if (index_col)
        strncpy(e->index_col, index_col, CAT_MAX_NAME - 1);
    else
        e->index_col[0] = '\0';
}

const catalog_entry_t *catalog_lookup(const catalog_t *c, const char *name) {
    for (int i = 0; i < c->num_tables; i++) {
        if (strcmp(c->tables[i].name, name) == 0)
            return &c->tables[i];
    }
    return NULL;
}