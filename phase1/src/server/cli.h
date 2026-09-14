#ifndef MINIDB_CLI_H
#define MINIDB_CLI_H

#include "executor.h"
#include "catalog.h"

typedef struct {
    catalog_t *catalog;
    exec_table_t tables[16];
    int num_tables;
} db_context_t;

db_context_t *db_context_create(void);
void          db_context_destroy(db_context_t *ctx);
void          db_context_add_table(db_context_t *ctx, const char *name,
                                   exec_row_t *rows, int num_rows,
                                   exec_col_meta_t *cols, int num_cols,
                                   bool has_index, const char *index_col);

void cli_run(db_context_t *ctx);

#endif