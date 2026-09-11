"""Rule (indicator) library: list, validate, create user formulas."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config import AStockConfig, get_default_config
from ..data.io_util import atomic_write_json, atomic_write_text
from ..indicators.compiler import compile_formula
from ..indicators.models import IndicatorSpec
from ..indicators.registry import BAGUA_SPEC, IndicatorRegistry
from ..indicators.tn6_importer import file_sha256

logger = logging.getLogger(__name__)

# 用户规则 ID 白名单：与 _slug 的 Unicode \w 口径对齐（含中日韩/西里尔等
# 词字符），同时天然排除 `/`、`\`、`.`、空格与控制字符，杜绝穿越字符进入
# user_dir 拼接（别名反查允许，但落盘一律用 spec.id）。
_USER_RULE_ID_RE = re.compile(r"user_\w+", re.UNICODE)

# 规则名/描述/分类入口清洗：C0（保留 \t\n\r 供描述多行文本）、\x7f、C1、
# U+FFFE/U+FFFF 与孤立代理都会在 Excel 导出/UTF-8 编码处炸，统一替换为 `_`。
_ILLEGAL_RULE_TEXT_CHARS = frozenset(
    [chr(c) for c in range(0x00, 0x09)]
    + [chr(c) for c in range(0x0B, 0x0D)]
    + [chr(c) for c in range(0x0E, 0x20)]
    + [chr(c) for c in range(0x7F, 0xA0)]
    + ["\ufffe", "\uffff"]
) | frozenset(chr(c) for c in range(0xD800, 0xE000))


def _require_user_rule_id(rule_id: Any) -> str:
    rid = str(rule_id or "")
    if not _USER_RULE_ID_RE.fullmatch(rid):
        raise ValueError(f"invalid user rule id: {rule_id!r}")
    return rid


def _sanitize_rule_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    if any(ch in _ILLEGAL_RULE_TEXT_CHARS for ch in text):
        return "".join(
            "_" if ch in _ILLEGAL_RULE_TEXT_CHARS else ch for ch in text
        )
    return text


def _slug(name: str) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "_", name.strip(), flags=re.UNICODE)
    s = s.strip("_")[:40] or "rule"
    return s


def _find_case_insensitive_name(directory: Path, name: str) -> Optional[str]:
    """目录里与 name 忽略大小写同名的现有条目名，无则 None。

    Windows/macOS 的文件系统本身就把 UPPER.txt 与 upper.txt 当同一个文件，
    Linux 不会。导入查重若只靠 ``Path.exists()``，同一次操作会在两个平台
    得到不同结果（Linux 会导入出两条显示名相同、ID 仅大小写不同的规则），
    所以显式按小写文件名比对，让行为与平台无关。
    """
    want = name.lower()
    try:
        for entry in directory.iterdir():
            if entry.name.lower() == want:
                return entry.name
    except OSError:
        # 目录不存在/不可读：视为没有同名文件，交给后续写入路径报错
        return None
    return None


# 进程级 sidecar 写锁：同一进程内不同 RuleService 实例（不同 storage_root）
# 也共享，保证 user_registry/categories/hidden 的读-改-写串行、不丢更新。
_RULE_SIDECAR_LOCK = threading.RLock()


def rule_to_public(spec: IndicatorSpec, *, include_formula: bool = False) -> Dict[str, Any]:
    params = spec.parameters or {}
    d = {
        "id": spec.id,
        "name": spec.name,
        "description": str(params.get("description") or ""),
        "category": str(params.get("category") or ""),
        "kind": spec.kind,
        "output_type": spec.output_type,
        "compile_status": spec.compile_status,
        "backtestable": spec.backtestable,
        "supported_periods": list(spec.supported_periods or ()),
        "aliases": list(spec.aliases or []),
        "dependencies": list(spec.dependencies or []),
        "failure_reason": spec.failure_reason,
        "min60_day_proxy": bool((spec.parameters or {}).get("min60_day_proxy")),
        "min60_native": bool((spec.parameters or {}).get("min60_native")),
        "min60_proxy_note": (spec.parameters or {}).get("min60_proxy_note", ""),
        "is_default_visible": spec.is_default_visible,
        "archived": bool((spec.parameters or {}).get("archived")),
        "formal_note": "User formulas default to research (txt_self_source) unless confirmed.",
    }
    if str(spec.id).startswith("user_"):
        d["source"] = "user"
    elif spec.kind == "native":
        d["source"] = "system"
    else:
        d["source"] = "builtin"
    if include_formula:
        d["formula_text"] = spec.formula_text
        d["source_file"] = spec.source_file
        d["source_sha256"] = spec.source_sha256
        d["package_sha256"] = spec.package_sha256
    return d


class RuleService:
    def __init__(self, cfg: Optional[AStockConfig] = None):
        self.cfg = cfg or get_default_config()
        self.cfg.ensure_dirs()
        self.user_dir = Path(self.cfg.storage_root) / "indicators" / "user"
        self.user_dir.mkdir(parents=True, exist_ok=True)
        self.user_registry_path = Path(self.cfg.storage_root) / "indicators" / "user_registry.json"
        # 串行化所有「读-改-写」sidecar 入口，避免并发丢更新/半截 JSON。
        # RLock：load_full_registry 等在锁内可重入。
        self._lock = _RULE_SIDECAR_LOCK

    def _load_user_registry(self) -> IndicatorRegistry:
        if self.user_registry_path.exists():
            try:
                return IndicatorRegistry.load(self.user_registry_path)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "用户规则注册表损坏（%s），按空注册表继续: %s",
                    self.user_registry_path,
                    e,
                )
        return IndicatorRegistry([])

    def _save_user_registry(self, reg: IndicatorRegistry) -> None:
        reg.save(self.user_registry_path)

    def load_full_registry(self) -> IndicatorRegistry:
        """Bootstrap system indicators + merge user rules.

        min60_available is True when the warehouse holds a minute_vendor/60m
        dataset (true 60-minute bars imported from the vendor CSV archives).
        With it, #MIN60 formulas compile to native minute mode instead of the
        day-line MACD research proxy.
        """
        min60_available = False
        try:
            from ..data.dataset_store import DatasetStore
            store = DatasetStore(self.cfg.market_data_root)
            for mid in store.list_manifests():
                m = store.load_manifest(mid, deep_copy=False)
                if m and m.source == "minute_vendor" and m.period == "60m" \
                        and m.status == "ready" and m.symbol_count and m.symbol_count > 0:
                    min60_available = True
                    break
        except Exception:
            min60_available = False
        try:
            reg = IndicatorRegistry.bootstrap(
                self.cfg.indicator_dir, self.cfg.mapping_path,
                min60_available=min60_available,
            )
        except Exception:
            reg = IndicatorRegistry([BAGUA_SPEC])
        user = self._load_user_registry()
        for s in user.list():
            reg.register(s)
        # persist combined snapshot for CLI list convenience
        try:
            reg.save(self.cfg.registry_path)
        except Exception:
            pass
        return reg

    def list_rules(
        self,
        *,
        include_archived: bool = False,
        include_hidden: bool = False,
    ) -> List[Dict[str, Any]]:
        reg = self.load_full_registry()
        hidden = set() if include_hidden else self._load_hidden_ids()
        rows = []
        for s in reg.list():
            pub = rule_to_public(s)
            if pub.get("archived") and not include_archived:
                continue
            if not s.is_default_visible and not include_archived:
                continue
            if s.id in hidden and not include_hidden:
                continue
            pub["hidden"] = s.id in self._load_hidden_ids()
            pub["deletable"] = True
            pub["delete_mode"] = "hard" if str(s.id).startswith("user_") else "hide"
            rows.append(pub)
        rows.sort(key=lambda r: (0 if r["source"] == "builtin" else 1, r["name"]))
        return rows

    def get_rule(self, rule_id: str, *, include_formula: bool = True) -> Dict[str, Any]:
        reg = self.load_full_registry()
        spec = reg.get(rule_id)
        return rule_to_public(spec, include_formula=include_formula)

    def validate_formula(self, formula_text: str, *, name: str = "draft") -> Dict[str, Any]:
        text = (formula_text or "").strip()
        if not text:
            return {"ok": False, "error": "formula_text is empty"}
        if len(text) > 200_000:
            return {"ok": False, "error": "formula_text too long"}
        cr = compile_formula(text, indicator_id=name)
        if not cr.ok:
            return {"ok": False, "error": cr.error or "compile failed"}
        has_xg = bool(cr.compiled and cr.compiled.has_xg)
        if not has_xg:
            return {
                "ok": False,
                "error": "formula must define XG (pick/signal output) for backtest",
                "has_xg": False,
            }
        deps = []
        if re.search(r"#MIN60", text, flags=re.I):
            deps.append("MIN60")
        if re.search(r"#MIN1\b", text, flags=re.I):
            deps.append("MIN1")
        return {
            "ok": True,
            "has_xg": True,
            "outputs": list(cr.compiled.outputs) if cr.compiled else [],
            "assigns": list(cr.compiled.assigns) if cr.compiled else [],
            "dependencies": deps,
        }

    def _apply_user_formula(
        self,
        spec: IndicatorSpec,
        *,
        formula_text: str,
        name: str,
    ) -> Dict[str, Any]:
        """create/update 共用的公式落地：validate + dependencies + MIN60 代理
        标记 + MIN1 拒绝 + failure_reason。调用前 spec 可已有其他 parameters
        （description/category 等），旧 min60 标记在不再引用时被清除。"""
        v = self.validate_formula(formula_text, name=name)
        if not v.get("ok"):
            raise ValueError(v.get("error") or "invalid formula")
        deps = list(v.get("dependencies") or [])
        if "MIN1" in deps:
            raise ValueError("MIN1 dependency is not supported without minute bars")
        text = formula_text.strip()
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        params = dict(spec.parameters or {})
        for key in ("min60_day_proxy", "min60_proxy_note", "min60_native"):
            params.pop(key, None)
        failure = None
        if "MIN60" in deps:
            params["min60_day_proxy"] = True
            params["min60_proxy_note"] = (
                "MACD.DIF#MIN60 / MACD.DEA#MIN60 filled from daily DIF/DEA"
            )
            failure = (
                "MIN60 satisfied via day-line MACD research proxy "
                "(min60_day_proxy); not true 60-minute data."
            )
        spec.formula_text = text
        spec.source_sha256 = sha
        spec.version = f"user:{sha[:12]}"
        spec.content_id = sha[:16]
        spec.compile_status = "ready"
        spec.parameters = params
        spec.failure_reason = failure
        spec.dependencies = deps
        return v

    def create_rule(
        self,
        *,
        name: str,
        formula_text: str,
        description: str = "",
        category: str = "",
        periods: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        name = _sanitize_rule_text(name or "").strip()
        if not name:
            raise ValueError("name is required")
        clean_description = _sanitize_rule_text(description)
        clean_category = _sanitize_rule_text(category or "").strip()
        if len(clean_category) > self._MAX_CATEGORY_LEN:
            raise ValueError(
                f"category name too long (max {self._MAX_CATEGORY_LEN})"
            )
        text = (formula_text or "").strip()
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        rid = f"user_{_slug(name)}_{sha[:8]}"
        path = self.user_dir / f"{rid}.txt"
        with self._lock:
            periods_t = tuple(periods or ["DAY", "WEEK", "MONTH"])
            spec = IndicatorSpec(
                id=rid,
                name=name,
                version=f"user:{sha[:12]}",
                kind="tdx_formula",
                output_type="signal",
                supported_periods=periods_t,
                source_file=None,
                source_sha256=sha,
                package_file=None,
                package_sha256=None,
                compile_status="ready",
                parameters={
                    "description": clean_description,
                    "category": clean_category,
                    "created_at": int(time.time()),
                    "source": "user",
                    "archived": False,
                },
                aliases=[name],
                content_id=sha[:16],
                failure_reason=None,
                dependencies=[],
                is_default_visible=True,
                formula_text=text,
            )
            # validate before any disk write so a bad formula leaves no file behind
            self._apply_user_formula(spec, formula_text=formula_text, name=name)
            atomic_write_text(path, spec.formula_text or "")
            spec.source_file = str(path.resolve())
            ureg = self._load_user_registry()
            ureg.register(spec)
            self._save_user_registry(ureg)
            self.load_full_registry()
        return rule_to_public(spec, include_formula=True)

    def update_rule(
        self,
        rule_id: str,
        *,
        name: Optional[str] = None,
        formula_text: Optional[str] = None,
        description: Optional[str] = None,
        category: Optional[str] = None,
    ) -> Dict[str, Any]:
        rid = _require_user_rule_id(rule_id)
        with self._lock:
            ureg = self._load_user_registry()
            spec = ureg.get(rid)
            # 别名反查兼容，但落盘路径与注册表键一律用规范 spec.id
            canonical_id = _require_user_rule_id(spec.id)
            if name is not None:
                clean_name = _sanitize_rule_text(name).strip()
                if clean_name:
                    spec.name = clean_name
                    spec.aliases = [clean_name]
            if formula_text is not None:
                # same pipeline as create_rule: validation, dependencies, min60
                # proxy flags (cleared when no longer referenced), MIN1 rejection
                self._apply_user_formula(spec, formula_text=formula_text, name=spec.name)
                path = self.user_dir / f"{canonical_id}.txt"
                atomic_write_text(path, spec.formula_text or "")
                spec.source_file = str(path.resolve())
            params = dict(spec.parameters or {})
            if description is not None:
                params["description"] = _sanitize_rule_text(description)
            if category is not None:
                cat = _sanitize_rule_text(category).strip()
                if len(cat) > self._MAX_CATEGORY_LEN:
                    raise ValueError(
                        f"category name too long (max {self._MAX_CATEGORY_LEN})"
                    )
                params["category"] = cat
            params["updated_at"] = int(time.time())
            spec.parameters = params
            ureg.register(spec)
            self._save_user_registry(ureg)
            self.load_full_registry()
        return rule_to_public(spec, include_formula=True)

    def archive_rule(self, rule_id: str) -> Dict[str, Any]:
        rid = _require_user_rule_id(rule_id)
        with self._lock:
            ureg = self._load_user_registry()
            spec = ureg.get(rid)
            _require_user_rule_id(spec.id)
            params = dict(spec.parameters or {})
            params["archived"] = True
            params["archived_at"] = int(time.time())
            spec.parameters = params
            spec.is_default_visible = False
            ureg.register(spec)
            self._save_user_registry(ureg)
            self.load_full_registry()
        return rule_to_public(spec, include_formula=False)

    def _hidden_path(self) -> Path:
        return Path(self.cfg.storage_root) / "indicators" / "hidden_rule_ids.json"

    def _load_hidden_ids(self) -> set:
        path = self._hidden_path()
        if not path.exists():
            return set()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return set(str(x) for x in data)
            if isinstance(data, dict):
                return set(str(x) for x in (data.get("ids") or []))
        except Exception:
            return set()
        return set()

    def _save_hidden_ids(self, ids: set) -> None:
        atomic_write_json(self._hidden_path(), sorted(ids))

    def delete_rule(self, rule_id: str, *, permanent: bool = True) -> Dict[str, Any]:
        """删除/隐藏规则：user_* 硬删（白名单校验后拼路径），其余仅隐藏。

        别名反查通过全量注册表完成，但隐藏集合与落盘路径一律用规范
        ``spec.id``；非 user 规则不改动源文件，只从默认列表移除（可用
        restore 恢复）。
        """
        with self._lock:
            reg = self.load_full_registry()
            try:
                spec = reg.get(str(rule_id))
            except KeyError as e:
                raise KeyError(str(rule_id)) from e
            canonical_id = str(spec.id)
            if canonical_id.startswith("user_"):
                canonical_id = _require_user_rule_id(canonical_id)
                if not permanent:
                    return self.archive_rule(canonical_id)
                ureg = self._load_user_registry()
                if canonical_id in ureg._by_id:
                    del ureg._by_id[canonical_id]
                    self._save_user_registry(ureg)
                fpath = self.user_dir / f"{canonical_id}.txt"
                if fpath.exists():
                    try:
                        fpath.unlink()
                    except OSError:
                        pass
                hidden = self._load_hidden_ids()
                if canonical_id in hidden:
                    hidden.discard(canonical_id)
                    self._save_hidden_ids(hidden)
                self.load_full_registry()
                return {
                    "id": canonical_id,
                    "deleted": True,
                    "mode": "hard",
                    "name": spec.name,
                }
            hidden = self._load_hidden_ids()
            hidden.add(canonical_id)
            self._save_hidden_ids(hidden)
            return {
                "id": canonical_id,
                "deleted": False,
                "mode": "hide",
                "name": spec.name,
                "note": "builtin/import rule hidden; source files kept",
            }

    def restore_rule(self, rule_id: str) -> Dict[str, Any]:
        with self._lock:
            reg = self.load_full_registry()
            try:
                spec = reg.get(str(rule_id))
            except KeyError as e:
                raise KeyError(str(rule_id)) from e
            canonical_id = str(spec.id)
            hidden = self._load_hidden_ids()
            if canonical_id in hidden:
                hidden.discard(canonical_id)
                self._save_hidden_ids(hidden)
            pub = rule_to_public(spec)
            pub["hidden"] = False
            return pub

    # --- import / batch validate / categories -----------------------------

    _MAX_IMPORT_BYTES = 512 * 1024

    def import_rule(self, *, filename: str, content: str) -> Dict[str, Any]:
        """导入 .txt 公式到指标目录并返回新规则 {id, name}。

        filename 取 basename 防路径穿越；后缀统一小写 .txt（Windows 上传
        ``A.TXT`` 在 Linux 上也能被 glob("*.txt") 扫到）；同名文件已存在时
        抛 FileExistsError（路由映射 400），查重忽略大小写以保证跨平台一致；
        写入 tmp + os.replace 原子，
        失败清理临时文件并转成 ValueError（路由 400）。.tn6 不做二进制直传，
        提示走 CLI 配对源文件。
        """
        raw = str(filename or "").strip()
        base = Path(raw.replace("\\", "/")).name.strip()
        if not base or base in (".", ".."):
            raise ValueError("filename is required")
        low = base.lower()
        if low.endswith(".tn6"):
            raise ValueError(
                ".tn6 为二进制包，需通过 CLI 与配对源文件一起导入"
                "（import-tn6 --source ...），不支持直接上传"
            )
        if not low.endswith(".txt"):
            raise ValueError("仅支持 .txt 公式文件")
        base = base[:-4] + ".txt"
        text = content if isinstance(content, str) else ""
        if not text.strip():
            raise ValueError("content is required")
        if len(text.encode("utf-8")) > self._MAX_IMPORT_BYTES:
            raise ValueError("content too large (max 512KB)")
        ind_dir = Path(self.cfg.indicator_dir)
        target = ind_dir / base
        with self._lock:
            existing = _find_case_insensitive_name(ind_dir, base)
            if existing is not None:
                raise FileExistsError(f"同名文件已存在: {existing}")
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                atomic_write_text(target, text)
            except (OSError, ValueError) as e:
                raise ValueError(f"写入导入文件失败: {base}: {e}") from e
            reg = self.load_full_registry()
            target_resolved = target.resolve()
            for s in reg.list():
                if s.source_file and Path(s.source_file).exists():
                    try:
                        if Path(s.source_file).resolve() == target_resolved:
                            return {"id": s.id, "name": s.name}
                    except OSError:
                        continue
        # 兜底：按 tn6_importer 的 txt_<slug> 规则命名（正常路径不应到达）
        stem = Path(base).stem
        return {"id": f"txt_{_slug(stem)}", "name": stem}

    def batch_validate(
        self, ids: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """批量编译校验：ids 缺省=全量注册表；单条异常不中断整体。"""
        reg = self.load_full_registry()
        selected: List[Tuple[str, Optional[IndicatorSpec]]] = []
        if ids:
            seen = set()
            for raw in ids:
                rid = str(raw or "").strip()
                if not rid or rid in seen:
                    continue
                seen.add(rid)
                try:
                    selected.append((rid, reg.get(rid)))
                except KeyError:
                    selected.append((rid, None))
        else:
            selected = [(s.id, s) for s in reg.list()]

        results: List[Dict[str, Any]] = []
        ok_count = 0
        for rid, spec in selected:
            name = spec.name if spec is not None else rid
            try:
                if spec is None:
                    ok, error = False, "rule not found"
                elif not (spec.formula_text or "").strip():
                    ok = False
                    error = spec.failure_reason or (
                        "no formula source (e.g. .tn6 not paired with a source file)"
                    )
                else:
                    v = self.validate_formula(spec.formula_text, name=spec.name)
                    ok = bool(v.get("ok"))
                    error = "" if ok else (v.get("error") or "compile failed")
            except Exception as e:  # noqa: BLE001
                ok, error = False, f"{type(e).__name__}: {e}"
            if ok:
                ok_count += 1
            results.append({"id": rid, "name": name, "ok": ok, "error": error})
        return {
            "summary": {
                "total": len(results),
                "ok": ok_count,
                "failed": len(results) - ok_count,
            },
            "results": results,
        }

    def _categories_path(self) -> Path:
        return Path(self.cfg.storage_root) / "indicators" / "rule_categories.json"

    def _load_categories(self) -> List[str]:
        path = self._categories_path()
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            logger.warning("规则分类 sidecar 损坏（%s），按空列表继续: %s", path, e)
            return []
        if isinstance(data, dict):
            values = data.get("categories") or []
        elif isinstance(data, list):
            values = data
        else:
            return []
        out: List[str] = []
        seen = set()
        for v in values:
            s = str(v).strip()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out

    def _save_categories(self, cats: List[str]) -> None:
        atomic_write_json(self._categories_path(), {"categories": cats})

    _MAX_CATEGORY_LEN = 20

    def list_categories(self) -> List[str]:
        return self._load_categories()

    def add_category(self, name: str) -> List[str]:
        cat = _sanitize_rule_text(name or "").strip()
        if not cat:
            raise ValueError("category name is required")
        if len(cat) > self._MAX_CATEGORY_LEN:
            raise ValueError(
                f"category name too long (max {self._MAX_CATEGORY_LEN})"
            )
        with self._lock:
            cats = self._load_categories()
            if cat not in cats:
                cats.append(cat)
                self._save_categories(cats)
            return cats
