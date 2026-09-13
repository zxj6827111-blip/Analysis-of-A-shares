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
        stock_name: str = "",
    ):
        self.compiled = compiled
        self.cross_period_data = cross_period_data or {}
        self.allow_missing_cross = allow_missing_cross
        # NAMELIKE 需要股票名称上下文（per-stock）；空串=调用方未提供，
        # 一旦公式用到 NAMELIKE 会显式报错（缺名称策略：报错可见，不静默放行）。
        self.stock_name = stock_name or ""
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
            # 上下文函数：编译期已校验参数形态（compiler._check_context_fn_args），
            # 在 get_builtin 之前拦截，避免走通用序列求值路径。
            if fname == "NAMELIKE":
                return self._eval_namelike(node)
            if fname == "DYNAINFO":
                return self._eval_dynainfo(node)
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

    def _eval_namelike(self, node: A.Call) -> np.ndarray:
        """NAMELIKE(字符串)：品种名称是否以参数开头（通达信官方语义=前缀匹配）。

        编译期已保证恰一个引号字符串参数；缺名称上下文时显式报错（策略：
        报错可见，不静默放行），调用方须按三级来源解析股票名称并传入。
        """
        pat = node.args[0].value
        if not self.stock_name:
            raise FormulaError(
                f"NAMELIKE('{pat}') requires stock name context; "
                "name unavailable for this code (三级名称来源均未命中)",
                node.line,
                node.col,
                self.compiled.indicator_id,
            )
        hit = 1.0 if self.stock_name.startswith(pat) else 0.0
        return np.full(self.n, hit, dtype=np.float64)

    def _eval_dynainfo(self, node: A.Call) -> np.ndarray:
        """DYNAINFO(k)：即时行情字段的盘后日线映射（本系统历史计算约定）。

        4=开盘价→OPEN、5=最高价→HIGH、6=最低价→LOW、7=收盘价→CLOSE；
        编译期已锁死 k 为 4..7 的整数字面量，返回对应价格序列（=0 等
        比较由后续 BinOp 完成，此处不做布尔化）。
        """
        k = int(node.args[0].value)
        key = {4: "OPEN", 5: "HIGH", 6: "LOW", 7: "CLOSE"}.get(k)
        if key is None or key not in self.env:
            raise FormulaError(
                f"DYNAINFO({k}) requires {key} series, but bars lack it",
                node.line,
                node.col,
                self.compiled.indicator_id,
            )
        return self.env[key]

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
    stock_name: str = "",
) -> RuntimeResult:
    cr = compile_formula(source, indicator_id=indicator_id)
    if not cr.ok or cr.compiled is None:
        return RuntimeResult(error=cr.error or "compile failed")
    # if cross refs and not allowed missing, pre-check
    rt = FormulaRuntime(
        cr.compiled,
        cross_period_data=cross_period_data,
        allow_missing_cross=allow_missing_cross,
        stock_name=stock_name,
    )
    return rt.run(bars)
