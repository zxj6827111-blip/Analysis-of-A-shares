"""AST node definitions for Tongdaxin formula language."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional, Union


@dataclass
class Node:
    line: int = 1
    col: int = 1


@dataclass
class Number(Node):
    value: float = 0.0


@dataclass
class StringLiteral(Node):
    value: str = ""


@dataclass
class Name(Node):
    value: str = ""


@dataclass
class UnaryOp(Node):
    op: str = ""
    operand: Optional[Node] = None


@dataclass
class BinOp(Node):
    op: str = ""
    left: Optional[Node] = None
    right: Optional[Node] = None


@dataclass
class Call(Node):
    func: str = ""
    args: List[Node] = field(default_factory=list)


@dataclass
class CrossPeriodRef(Node):
    """Reference like "MACD.DIF#MIN60"."""

    indicator: str = ""
    field: str = ""
    period: str = ""
    raw: str = ""


@dataclass
class Assign(Node):
    name: str = ""
    expr: Optional[Node] = None
    output: bool = False  # NAME: expr (draw/output) vs NAME:= expr


@dataclass
class Program(Node):
    statements: List[Assign] = field(default_factory=list)


Expr = Union[Number, StringLiteral, Name, UnaryOp, BinOp, Call, CrossPeriodRef]
