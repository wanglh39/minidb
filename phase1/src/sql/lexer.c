#include "lexer.h"
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include <strings.h>

struct lexer {
    const char *src;
    int pos;
    int len;
    token_t peeked;
    bool has_peek;
};

static const struct { const char *text; keyword_t kw; } kw_table[] = {
    {"SELECT", KW_SELECT}, {"FROM", KW_FROM}, {"WHERE", KW_WHERE},
    {"INSERT", KW_INSERT}, {"INTO", KW_INTO}, {"VALUES", KW_VALUES},
    {"UPDATE", KW_UPDATE}, {"SET", KW_SET}, {"DELETE", KW_DELETE},
    {"CREATE", KW_CREATE}, {"TABLE", KW_TABLE}, {"AND", KW_AND},
    {"OR", KW_OR}, {"NULL", KW_NULL}, {"INT", KW_INT}, {"FLOAT", KW_FLOAT},
    {"PRIMARY", KW_PRIMARY}, {"KEY", KW_KEY}, {"NOT", KW_NOT},
    {NULL, 0},
};

static int lookup_keyword(const char *text) {
    for (int i = 0; kw_table[i].text; i++) {
        if (strcasecmp(text, kw_table[i].text) == 0) return (int)kw_table[i].kw;
    }
    return -1;
}

lexer_t *lexer_create(const char *sql) {
    lexer_t *lex = malloc(sizeof(lexer_t));
    lex->src = sql;
    lex->pos = 0;
    lex->len = (int)strlen(sql);
    lex->has_peek = false;
    return lex;
}

void lexer_destroy(lexer_t *lex) {
    free(lex);
}

static token_t make_tok(token_type_t type) {
    token_t t;
    memset(&t, 0, sizeof(t));
    t.type = type;
    return t;
}

static token_t lex_number(lexer_t *lex) {
    token_t t = make_tok(TOK_NUMBER);
    int start = lex->pos;
    bool is_float = false;

    while (lex->pos < lex->len && (isdigit((unsigned char)lex->src[lex->pos]) || lex->src[lex->pos] == '.')) {
        if (lex->src[lex->pos] == '.') is_float = true;
        lex->pos++;
    }

    int n = lex->pos - start;
    if (n >= 64) n = 63;
    strncpy(t.text, lex->src + start, n);
    t.text[n] = '\0';

    if (is_float) t.float_val = (float)atof(t.text);
    else          t.int_val = atoi(t.text);

    return t;
}

static token_t lex_ident(lexer_t *lex) {
    token_t t = make_tok(TOK_IDENT);
    int start = lex->pos;

    while (lex->pos < lex->len && (isalnum((unsigned char)lex->src[lex->pos]) || lex->src[lex->pos] == '_')) {
        lex->pos++;
    }

    int n = lex->pos - start;
    if (n >= 64) n = 63;
    strncpy(t.text, lex->src + start, n);
    t.text[n] = '\0';

    int kw = lookup_keyword(t.text);
    if (kw >= 0) {
        t.type = TOK_KEYWORD;
        t.keyword = (keyword_t)kw;
    }
    return t;
}

static token_t lex_string(lexer_t *lex) {
    token_t t = make_tok(TOK_STRING);
    lex->pos++;
    int start = lex->pos;

    while (lex->pos < lex->len && lex->src[lex->pos] != '\'') lex->pos++;

    int n = lex->pos - start;
    if (n >= 64) n = 63;
    strncpy(t.text, lex->src + start, n);
    t.text[n] = '\0';

    if (lex->pos < lex->len) lex->pos++;
    return t;
}

token_t lexer_next(lexer_t *lex) {
    if (lex->has_peek) {
        lex->has_peek = false;
        return lex->peeked;
    }

    while (lex->pos < lex->len && isspace((unsigned char)lex->src[lex->pos])) lex->pos++;

    if (lex->pos >= lex->len) return make_tok(TOK_EOF);

    char c = lex->src[lex->pos];

    if (isdigit((unsigned char)c)) return lex_number(lex);
    if (isalpha((unsigned char)c) || c == '_') return lex_ident(lex);
    if (c == '\'') return lex_string(lex);
    if (c == '(') { lex->pos++; return make_tok(TOK_LPAREN); }
    if (c == ')') { lex->pos++; return make_tok(TOK_RPAREN); }
    if (c == ',') { lex->pos++; return make_tok(TOK_COMMA); }
    if (c == ';') { lex->pos++; return make_tok(TOK_SEMICOLON); }
    if (c == '*') { token_t t = make_tok(TOK_OP); t.text[0]='*'; t.text[1]='\0'; lex->pos++; return t; }

    if (c == '=' || c == '!' || c == '<' || c == '>') {
        token_t t = make_tok(TOK_OP);
        t.text[0] = c;
        lex->pos++;
        if (lex->pos < lex->len && lex->src[lex->pos] == '=') {
            t.text[1] = '=';
            t.text[2] = '\0';
            lex->pos++;
        } else {
            t.text[1] = '\0';
        }
        return t;
    }

    token_t err = make_tok(TOK_ERROR);
    err.text[0] = c;
    err.text[1] = '\0';
    lex->pos++;
    return err;
}

token_t lexer_peek(lexer_t *lex) {
    if (!lex->has_peek) {
        lex->peeked = lexer_next(lex);
        lex->has_peek = true;
    }
    return lex->peeked;
}