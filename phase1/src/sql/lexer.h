#ifndef MINIDB_LEXER_H
#define MINIDB_LEXER_H

#include <stdint.h>
#include <stdbool.h>

typedef enum {
    TOK_KEYWORD = 0,
    TOK_IDENT = 1,
    TOK_NUMBER = 2,
    TOK_STRING = 3,
    TOK_OP = 4,
    TOK_LPAREN = 5,
    TOK_RPAREN = 6,
    TOK_COMMA = 7,
    TOK_SEMICOLON = 8,
    TOK_EOF = 9,
    TOK_ERROR = 10,
} token_type_t;

typedef enum {
    KW_SELECT, KW_FROM, KW_WHERE, KW_INSERT, KW_INTO, KW_VALUES,
    KW_UPDATE, KW_SET, KW_DELETE, KW_CREATE, KW_TABLE,
    KW_AND, KW_OR, KW_NULL, KW_INT, KW_FLOAT, KW_PRIMARY, KW_KEY,
    KW_NOT,
} keyword_t;

typedef struct {
    token_type_t type;
    keyword_t keyword;
    char text[64];
    int32_t int_val;
    float float_val;
} token_t;

typedef struct lexer lexer_t;

lexer_t *lexer_create(const char *sql);
void     lexer_destroy(lexer_t *lex);
token_t  lexer_next(lexer_t *lex);
token_t  lexer_peek(lexer_t *lex);

#endif