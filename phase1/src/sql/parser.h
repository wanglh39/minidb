#ifndef MINIDB_PARSER_H
#define MINIDB_PARSER_H

#include "ast.h"

typedef struct parser parser_t;

parser_t    *parser_create(const char *sql);
void         parser_destroy(parser_t *p);
ast_stmt_t  *parser_parse(parser_t *p);

#endif