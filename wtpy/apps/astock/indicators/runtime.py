"""Vectorized Tongdaxin formula runtime (no Python eval)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np

from . import ast_nodes as A
from .builtins import get_builtin
from .compiler import CompiledFormula, compile_formula
from .parser import FormulaError


PRICE_ALIASES = {
    "C": "close",
    "CLOSE": "close",
    "O": "open",
    "OPEN": "open",
    "H": "high",
    "HIGH": "high",
    "L": "low",
    "LOW": "low",
    "V": "volume",
    "VOL": "volume",
    "VOLUME": "volume",
    "AMOUNT": "amount",
}


@dataclass
class RuntimeResult:
    variables: Dict[str, np.ndarray] = field(default_factory=dict)
    outputs: Dict[str, np.ndarray] = field(default_factory=dict)
    signal: Optional[np.ndarray] = None  # XG if present
    error: Optional[str] = None
    blocked_dependencies: List[str] = field(default_factory=list)


class FormulaRuntime:
    def __init__(
        self,
        compiled: CompiledFormula,
        *,
        cross_period_data: Optional[Dict[str, np.ndarray]] = None,
        allow_missing_cross: bool = False,
    ):
        self.compiled = compiled
        self.cross_period_data = cross_period_data or {}
        self.allow_missing_cross = allow_missing_cross
        self.env: Dict[str, np.ndarray] = {}
        self.n = 0

    def run(self, bars: Dict[str, np.ndarray]) -> RuntimeResult:
        self.n = len(bars.get("close", bars.get("date", [])))
        self.env = {}
        # inject OHLCV
        for key in ("open", "high", "low", "close", "volume", "amount", "date"):
            if key in bars:
                self.env[key.upper()] = np.asarray(bars[key], dtype=np.float64)
        # aliases
        if "CLOSE" in self.env:
            self.env["C"] = self.env["CLOSE"]
        if "OPEN" in self.env:
            self.env["O"] = self.env["OPEN"]
        if "HIGH" in self.env:
            self.env["H"] = self.env["HIGH"]
        if "LOW" in self.env:
            self.env["L"] = self.env["LOW"]
        if "VOLUME" in self.env:
            self.env["V"] = self.env["VOLUME"]
            self.env["VOL"] = self.env["VOLUME"]

        blocked: List[str] = []
        try:
            for stmt in self.compiled.program.statements:
                val = self._eval(stmt.expr)
                self.env[stmt.name.upper()] = val
            outputs = {
                name: self.env[name.upper()]
                for name in self.compiled.outputs
                if name.upper() in self.env
            }
            # also expose assigns as variables
            variables = {
                k: v
                for k, v in self.env.items()
                if k not in ("O", "H", "L", "C", "V", "VOL", "OPEN", "HIGH", "LOW", "CLOSE", "VOLUME", "AMOUNT", "DATE")
            }
            signal = None
            if "XG" in self.env:
                signal = self._to_bool(self.env["XG"])
            # check cross period blocks
            for ref in self.compiled.cross_period_refs:
                key = ref.raw.upper()
                if key not in self.cross_period_data and ref.period:
                    blocked.append(ref.period)
            if blocked and not self.allow_missing_cross:
                return RuntimeResult(
                    variables=variables,
                    outputs=outputs,
                    signal=None,
                    error=(
                        f"Cross-period data missing for {sorted(set(blocked))}; "
                        "formal run disabled (no WEEK/MONTH substitution for MIN60)."
                    ),
                    blocked_dependencies=sorted(set(blocked)),
                )
            return RuntimeResult(
                variables=variables,
                outputs=outputs,
                signal=signal,
                blocked_dependencies=sorted(set(blocked)),
            )
        except FormulaError as e:
            return RuntimeResult(error=str(e))
        except Exception as e:  # noqa: BLE001
            return RuntimeResult(error=f"runtime error: {e}")

    def _eval(self, node: Optional[A.Node]) -> np.ndarray:
        if node is None:
            raise FormulaError("empty expression")
        if isinstance(node, A.Number):
            return np.full(self.n, float(node.value), dtype=np.float64)
        if isinstance(node, A.StringLiteral):
            # bare strings not usable as series
            raise FormulaError(
                f"unexpected string literal {node.value!r}",
                node.line,
                node.col,
                self.compiled.indicator_id,
            )
        if isinstance(node, A.CrossPeriodRef):
            key = node.raw.upper()
            # also try FIELD#PERIOD
            alt = f"{node.field}#{node.period}".upper() if node.field else key
            if key in self.cross_period_data:
                return np.asarray(self.cross_period_data[key], dtype=np.float64)
            if alt in self.cross_period_data:
                return np.asarray(self.cross_period_data[alt], dtype=np.float64)
            # missing series -> NaN with dependency flag handled later
            if self.allow_missing_cross:
                return np.full(self.n, np.nan, dtype=np.float64)
            raise FormulaError(
                f"cross-period reference unavailable: {node.raw} "
                f"(period={node.period})",
                node.line,
                node.col,
                self.compiled.indicator_id,
            )
        if isinstance(node, A.Name):
            key = node.value.upper()
            if key in self.env:
                return self.env[key]
            # price alias map
            mapped = PRICE_ALIASES.get(key)
            if mapped and mapped.upper() in self.env:
                return self.env[mapped.upper()]
            raise FormulaError(
                f"undefined name '{node.value}'",
                node.line,
                node.col,
                self.compiled.indicator_id,
            )
        if isinstance(node, A.UnaryOp):
            v = self._eval(node.operand)
            if node.op == "+":
                return v
            if node.op == "-":
                return -v
            if node.op == "NOT":
                return (~self._to_bool(v)).astype(np.float64)
            raise FormulaError(f"unknown unary op {node.op}", node.line, node.col)
        if isinstance(node, A.BinOp):
            left = self._eval(node.left)
            right = self._eval(node.right)
            op = node.op
            if op == "+":
                return left + right
            if op == "-":
                return left - right
            if op == "*":
                return left * right
            if op == "/":
                with np.errstate(divide="ignore", invalid="ignore"):
                    return np.where(right == 0, np.nan, left / right)
            if op == "^":
                return np.power(left, right)
            if op == ">":
                return (left > right).astype(np.float64)
            if op == "<":
                return (left < right).astype(np.float64)
            if op == ">=":
                return (left >= right).astype(np.float64)
            if op == "<=":
                return (left <= right).astype(np.float64)
            if op == "=":
                return (left == right).astype(np.float64)
            if op == "<>":
                return (left != right).astype(np.float64)
            if op == "AND":
                return (self._to_bool(left) & self._to_bool(right)).astype(np.float64)
            if op == "OR":
                return (self._to_bool(left) | self._to_bool(right)).astype(np.float64)
            raise FormulaError(f"unknown operator {op}", node.line, node.col)
        if isinstance(node, A.Call):
            fname = node.func.upper()
            try:
                fn = get_builtin(fname)
            except KeyError:
                raise FormulaError(
                    f"unsupported function '{fname}' "
                    f"(indicator={self.compiled.indicator_id})",
                    node.line,
                    node.col,
                    self.compiled.indicator_id,
                )
            args = [self._eval(a) for a in node.args]
            try:
                return fn(*args)
            except TypeError as e:
                raise FormulaError(
                    f"bad arguments for {fname}: {e}",
                    node.line,
                    node.col,
                    self.compiled.indicator_id,
                )
        raise FormulaError(
            f"unknown node type {type(node).__name__}",
            getattr(node, "line", 0),
            getattr(node, "col", 0),
        )

    @staticmethod
    def _to_bool(x: np.ndarray) -> np.ndarray:
        arr = np.asarray(x)
        if arr.dtype == np.bool_:
            return arr
        return np.nan_to_num(arr.astype(np.float64), nan=0.0) != 0.0


def run_formula(
    source: str,
    bars: Dict[str, np.ndarray],
    *,
    indicator_id: str = "",
    cross_period_data: Optional[Dict[str, np.ndarray]] = None,
    allow_missing_cross: bool = False,
) -> RuntimeResult:
    cr = compile_formula(source, indicator_id=indicator_id)
    if not cr.ok or cr.compiled is None:
        return RuntimeResult(error=cr.error or "compile failed")
    # if cross refs and not allowed missing, pre-check
    rt = FormulaRuntime(
        cr.compiled,
        cross_period_data=cross_period_data,
        allow_missing_cross=allow_missing_cross,
    )
    return rt.run(bars)
