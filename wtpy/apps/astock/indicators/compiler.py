"""Compile Tongdaxin AST into an executable plan (no Python eval)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from . import ast_nodes as A
from .builtins import BUILTINS
from .parser import FormulaError, parse_formula


@dataclass
class CompiledFormula:
    indicator_id: str
    source: str
    program: A.Program
    outputs: List[str]
    assigns: List[str]
    used_functions: Set[str] = field(default_factory=set)
    cross_period_refs: List[A.CrossPeriodRef] = field(default_factory=list)
    field_aliases_needed: Set[str] = field(default_factory=set)

    @property
    def has_xg(self) -> bool:
        return any(x.upper() == "XG" for x in self.outputs + self.assigns)


@dataclass
class CompileResult:
    ok: bool
    compiled: Optional[CompiledFormula] = None
    error: Optional[str] = None


class Compiler:
    def __init__(self, indicator_id: str = ""):
        self.indicator_id = indicator_id
        self.used_functions: Set[str] = set()
        self.cross_refs: List[A.CrossPeriodRef] = []
        self.names_needed: Set[str] = set()

    def compile_source(self, source: str) -> CompileResult:
        try:
            program = parse_formula(source, indicator=self.indicator_id)
            return self.compile_program(program, source)
        except FormulaError as e:
            return CompileResult(ok=False, error=str(e))
        except Exception as e:  # noqa: BLE001
            return CompileResult(ok=False, error=f"compile failed: {e}")

    def compile_program(self, program: A.Program, source: str = "") -> CompileResult:
        outputs: List[str] = []
        assigns: List[str] = []
        try:
            for stmt in program.statements:
                self._walk(stmt.expr)
                if stmt.output:
                    outputs.append(stmt.name)
                else:
                    assigns.append(stmt.name)
            # validate functions
            unknown = sorted(f for f in self.used_functions if f not in BUILTINS)
            if unknown:
                # find location of first unknown call
                loc = self._find_call_loc(program, unknown[0])
                raise FormulaError(
                    f"unsupported function '{unknown[0]}' "
                    f"(indicator={self.indicator_id or '?'})",
                    line=loc[0],
                    col=loc[1],
                    indicator=self.indicator_id,
                )
            compiled = CompiledFormula(
                indicator_id=self.indicator_id,
                source=source,
                program=program,
                outputs=outputs,
                assigns=assigns,
                used_functions=set(self.used_functions),
                cross_period_refs=list(self.cross_refs),
                field_aliases_needed=set(self.names_needed),
            )
            return CompileResult(ok=True, compiled=compiled)
        except FormulaError as e:
            return CompileResult(ok=False, error=str(e))

    def _walk(self, node: Optional[A.Node]) -> None:
        if node is None:
            return
        if isinstance(node, A.Call):
            fname = node.func.upper()
            self.used_functions.add(fname)
            self._check_context_fn_args(node, fname)
            for a in node.args:
                self._walk(a)
        elif isinstance(node, A.BinOp):
            self._walk(node.left)
            self._walk(node.right)
        elif isinstance(node, A.UnaryOp):
            self._walk(node.operand)
        elif isinstance(node, A.Name):
            self.names_needed.add(node.value.upper())
        elif isinstance(node, A.CrossPeriodRef):
            self.cross_refs.append(node)
        elif isinstance(node, (A.Number, A.StringLiteral)):
            return
        else:
            return

    def _check_context_fn_args(self, node: A.Call, fname: str) -> None:
        """NAMELIKE/DYNAINFO/SMA 的轻量参数检查：保存时报错，避免运行期才发现。

        NAMELIKE 与 DYNAINFO 实际由 runtime 拦截执行（builtins 注册的是占位），
        参数形态在编译期即可锁定。
        """
        err = None
        if fname == "NAMELIKE":
            if len(node.args) != 1 or not isinstance(node.args[0], A.StringLiteral):
                err = "NAMELIKE requires exactly one quoted string argument, e.g. NAMELIKE('ST')"
        elif fname == "DYNAINFO":
            a = node.args[0] if len(node.args) == 1 else None
            if not isinstance(a, A.Number) or float(a.value) != int(a.value):
                err = (
                    "DYNAINFO requires exactly one integer literal argument "
                    "(supported fields: 4/5/6/7 = open/high/low/close)"
                )
            elif not (4 <= int(a.value) <= 7):
                err = (
                    f"unsupported DYNAINFO field {int(a.value)} "
                    "(supported fields: 4/5/6/7 = open/high/low/close)"
                )
        elif fname == "SMA":
            if len(node.args) != 3:
                err = "SMA requires exactly 3 arguments: SMA(X,N,M)"
            else:
                n_arg, m_arg = node.args[1], node.args[2]
                if (
                    not isinstance(n_arg, A.Number)
                    or float(n_arg.value) != int(n_arg.value)
                    or not isinstance(m_arg, A.Number)
                    or float(m_arg.value) != int(m_arg.value)
                ):
                    err = "SMA requires integer literal N and M (variable periods not supported)"
                else:
                    n_val, m_val = int(n_arg.value), int(m_arg.value)
                    if n_val < 1:
                        err = f"SMA N must be >= 1 (got {n_val})"
                    elif m_val < 0 or m_val > n_val:
                        err = f"SMA M must satisfy 0 <= M <= N (got N={n_val}, M={m_val})"
        if err:
            raise FormulaError(
                err, line=node.line, col=node.col, indicator=self.indicator_id
            )

    def _find_call_loc(self, program: A.Program, func: str) -> Tuple[int, int]:
        found = (0, 0)

        def visit(n: Optional[A.Node]) -> None:
            nonlocal found
            if n is None:
                return
            if isinstance(n, A.Call) and n.func.upper() == func.upper():
                found = (n.line, n.col)
                return
            if isinstance(n, A.BinOp):
                visit(n.left)
                visit(n.right)
            elif isinstance(n, A.UnaryOp):
                visit(n.operand)
            elif isinstance(n, A.Call):
                for a in n.args:
                    visit(a)

        for s in program.statements:
            visit(s.expr)
        return found


def compile_formula(source: str, *, indicator_id: str = "") -> CompileResult:
    return Compiler(indicator_id=indicator_id).compile_source(source)
