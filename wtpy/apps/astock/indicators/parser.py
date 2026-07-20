"""Tokenizer and recursive-descent parser for Tongdaxin formulas."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from . import ast_nodes as A


@dataclass
class Token:
    type: str
    value: str
    line: int
    col: int


class FormulaError(ValueError):
    def __init__(self, message: str, line: int = 0, col: int = 0, indicator: str = ""):
        loc = f"{line}:{col}" if line else "?"
        prefix = f"[{indicator}] " if indicator else ""
        super().__init__(f"{prefix}Formula error at {loc}: {message}")
        self.line = line
        self.col = col
        self.indicator = indicator


class TokenizeError(FormulaError):
    pass


class ParseError(FormulaError):
    pass


def tokenize(source: str) -> List[Token]:
    tokens: List[Token] = []
    i = 0
    n = len(source)
    line = 1
    col = 1

    def advance(k: int = 1) -> None:
        nonlocal i, line, col
        for _ in range(k):
            if i < n and source[i] == "\n":
                line += 1
                col = 1
            else:
                col += 1
            i += 1

    while i < n:
        ch = source[i]
        if ch in " \t\r":
            advance()
            continue
        if ch == "\n":
            advance()
            continue
        if ch == "{":
            start_line, start_col = line, col
            advance()
            while i < n and source[i] != "}":
                advance()
            if i >= n:
                raise TokenizeError("unclosed comment", start_line, start_col)
            advance()
            continue
        if ch == "/" and i + 1 < n and source[i + 1] == "/":
            while i < n and source[i] != "\n":
                advance()
            continue

        start_line, start_col = line, col

        if source.startswith(":=", i):
            tokens.append(Token("ASSIGN", ":=", start_line, start_col))
            advance(2)
            continue
        if source.startswith(">=", i):
            tokens.append(Token("OP", ">=", start_line, start_col))
            advance(2)
            continue
        if source.startswith("<=", i):
            tokens.append(Token("OP", "<=", start_line, start_col))
            advance(2)
            continue
        if source.startswith("<>", i) or source.startswith("!=", i):
            tokens.append(Token("OP", "<>", start_line, start_col))
            advance(2)
            continue
        if source.startswith("&&", i):
            tokens.append(Token("OP", "AND", start_line, start_col))
            advance(2)
            continue
        if source.startswith("||", i):
            tokens.append(Token("OP", "OR", start_line, start_col))
            advance(2)
            continue

        if ch == ":":
            tokens.append(Token("COLON", ":", start_line, start_col))
            advance()
            continue
        if ch == ",":
            tokens.append(Token("COMMA", ",", start_line, start_col))
            advance()
            continue
        if ch == "(":
            tokens.append(Token("LPAREN", "(", start_line, start_col))
            advance()
            continue
        if ch == ")":
            tokens.append(Token("RPAREN", ")", start_line, start_col))
            advance()
            continue
        if ch == ";":
            tokens.append(Token("SEMI", ";", start_line, start_col))
            advance()
            continue
        if ch in "+-*/^":
            tokens.append(Token("OP", ch, start_line, start_col))
            advance()
            continue
        if ch in "><=":
            tokens.append(Token("OP", ch, start_line, start_col))
            advance()
            continue

        if ch == '"':
            advance()
            buf = []
            while i < n and source[i] != '"':
                buf.append(source[i])
                advance()
            if i >= n:
                raise TokenizeError("unclosed string", start_line, start_col)
            advance()
            tokens.append(Token("STRING", "".join(buf), start_line, start_col))
            continue

        if ch.isdigit() or (ch == "." and i + 1 < n and source[i + 1].isdigit()):
            buf = []
            while i < n and (source[i].isdigit() or source[i] == "."):
                buf.append(source[i])
                advance()
            tokens.append(Token("NUMBER", "".join(buf), start_line, start_col))
            continue

        if ch.isalpha() or ch == "_" or ("\u4e00" <= ch <= "\u9fff"):
            buf = []
            while i < n:
                c = source[i]
                if c.isalnum() or c == "_" or ("\u4e00" <= c <= "\u9fff"):
                    buf.append(c)
                    advance()
                else:
                    break
            word = "".join(buf)
            up = word.upper()
            if up in ("AND", "OR", "NOT"):
                tokens.append(Token("OP", up, start_line, start_col))
            else:
                tokens.append(Token("NAME", word, start_line, start_col))
            continue

        raise TokenizeError(f"unexpected character {ch!r}", start_line, start_col)

    tokens.append(Token("EOF", "", line, col))
    return tokens


class Parser:
    def __init__(self, tokens: List[Token], *, indicator: str = ""):
        self.tokens = tokens
        self.pos = 0
        self.indicator = indicator

    def cur(self) -> Token:
        return self.tokens[self.pos]

    def eat(self, ttype: Optional[str] = None) -> Token:
        tok = self.cur()
        if ttype and tok.type != ttype:
            raise ParseError(
                f"expected {ttype}, got {tok.type}({tok.value!r})",
                tok.line,
                tok.col,
                self.indicator,
            )
        self.pos += 1
        return tok

    def parse(self) -> A.Program:
        stmts: List[A.Assign] = []
        while self.cur().type != "EOF":
            if self.cur().type == "SEMI":
                self.eat("SEMI")
                continue
            stmts.append(self.parse_statement())
            if self.cur().type == "SEMI":
                self.eat("SEMI")
        return A.Program(statements=stmts, line=1, col=1)

    def parse_statement(self) -> A.Assign:
        name_tok = self.eat("NAME")
        if self.cur().type == "ASSIGN":
            self.eat("ASSIGN")
            expr = self.parse_expr()
            return A.Assign(
                name=name_tok.value,
                expr=expr,
                output=False,
                line=name_tok.line,
                col=name_tok.col,
            )
        if self.cur().type == "COLON":
            self.eat("COLON")
            expr = self.parse_expr()
            return A.Assign(
                name=name_tok.value,
                expr=expr,
                output=True,
                line=name_tok.line,
                col=name_tok.col,
            )
        raise ParseError(
            "expected := or : after name",
            name_tok.line,
            name_tok.col,
            self.indicator,
        )

    # precedence: OR < AND < compare < + - < * / < unary < primary
    def parse_expr(self) -> A.Node:
        return self.parse_or()

    def parse_or(self) -> A.Node:
        node = self.parse_and()
        while self.cur().type == "OP" and self.cur().value == "OR":
            op = self.eat("OP")
            rhs = self.parse_and()
            node = A.BinOp(op="OR", left=node, right=rhs, line=op.line, col=op.col)
        return node

    def parse_and(self) -> A.Node:
        node = self.parse_compare()
        while self.cur().type == "OP" and self.cur().value == "AND":
            op = self.eat("OP")
            rhs = self.parse_compare()
            node = A.BinOp(op="AND", left=node, right=rhs, line=op.line, col=op.col)
        return node

    def parse_compare(self) -> A.Node:
        node = self.parse_add()
        while self.cur().type == "OP" and self.cur().value in (
            ">",
            "<",
            ">=",
            "<=",
            "=",
            "<>",
        ):
            op = self.eat("OP")
            rhs = self.parse_add()
            node = A.BinOp(op=op.value, left=node, right=rhs, line=op.line, col=op.col)
        return node

    def parse_add(self) -> A.Node:
        node = self.parse_mul()
        while self.cur().type == "OP" and self.cur().value in ("+", "-"):
            op = self.eat("OP")
            rhs = self.parse_mul()
            node = A.BinOp(op=op.value, left=node, right=rhs, line=op.line, col=op.col)
        return node

    def parse_mul(self) -> A.Node:
        node = self.parse_unary()
        while self.cur().type == "OP" and self.cur().value in ("*", "/", "^"):
            op = self.eat("OP")
            rhs = self.parse_unary()
            node = A.BinOp(op=op.value, left=node, right=rhs, line=op.line, col=op.col)
        return node

    def parse_unary(self) -> A.Node:
        if self.cur().type == "OP" and self.cur().value in ("+", "-", "NOT"):
            op = self.eat("OP")
            operand = self.parse_unary()
            return A.UnaryOp(op=op.value, operand=operand, line=op.line, col=op.col)
        return self.parse_primary()

    def parse_primary(self) -> A.Node:
        tok = self.cur()
        if tok.type == "NUMBER":
            self.eat("NUMBER")
            return A.Number(value=float(tok.value), line=tok.line, col=tok.col)
        if tok.type == "STRING":
            self.eat("STRING")
            return self._string_to_node(tok)
        if tok.type == "LPAREN":
            self.eat("LPAREN")
            node = self.parse_expr()
            self.eat("RPAREN")
            return node
        if tok.type == "NAME":
            name_tok = self.eat("NAME")
            if self.cur().type == "LPAREN":
                self.eat("LPAREN")
                args: List[A.Node] = []
                if self.cur().type != "RPAREN":
                    args.append(self.parse_expr())
                    while self.cur().type == "COMMA":
                        self.eat("COMMA")
                        args.append(self.parse_expr())
                self.eat("RPAREN")
                return A.Call(
                    func=name_tok.value.upper(),
                    args=args,
                    line=name_tok.line,
                    col=name_tok.col,
                )
            return A.Name(value=name_tok.value, line=name_tok.line, col=name_tok.col)
        raise ParseError(
            f"unexpected token {tok.type}({tok.value!r})",
            tok.line,
            tok.col,
            self.indicator,
        )

    def _string_to_node(self, tok: Token) -> A.Node:
        """Parse cross-period refs like MACD.DIF#MIN60 inside strings."""
        raw = tok.value
        if "#" in raw:
            left, period = raw.split("#", 1)
            if "." in left:
                ind, field = left.split(".", 1)
            else:
                ind, field = left, ""
            return A.CrossPeriodRef(
                indicator=ind,
                field=field,
                period=period.upper(),
                raw=raw,
                line=tok.line,
                col=tok.col,
            )
        return A.StringLiteral(value=raw, line=tok.line, col=tok.col)


def parse_formula(source: str, *, indicator: str = "") -> A.Program:
    tokens = tokenize(source)
    return Parser(tokens, indicator=indicator).parse()
