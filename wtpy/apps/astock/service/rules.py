"""Rule (indicator) library: list, validate, create user formulas."""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config import AStockConfig, get_default_config
from ..indicators.compiler import compile_formula
from ..indicators.models import IndicatorSpec
from ..indicators.registry import BAGUA_SPEC, IndicatorRegistry
from ..indicators.tn6_importer import file_sha256


def _slug(name: str) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "_", name.strip(), flags=re.UNICODE)
    s = s.strip("_")[:40] or "rule"
    return s


def rule_to_public(spec: IndicatorSpec, *, include_formula: bool = False) -> Dict[str, Any]:
    d = {
        "id": spec.id,
        "name": spec.name,
        "kind": spec.kind,
        "output_type": spec.output_type,
        "compile_status": spec.compile_status,
        "backtestable": spec.backtestable,
        "supported_periods": list(spec.supported_periods or ()),
        "aliases": list(spec.aliases or []),
        "dependencies": list(spec.dependencies or []),
        "failure_reason": spec.failure_reason,
        "min60_day_proxy": bool((spec.parameters or {}).get("min60_day_proxy")),
        "is_default_visible": spec.is_default_visible,
        "source": "user" if str(spec.id).startswith("user_") else (
            "system" if spec.kind == "native" or not str(spec.id).startswith("user_") else "system"
        ),
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

    def _load_user_registry(self) -> IndicatorRegistry:
        if self.user_registry_path.exists():
            return IndicatorRegistry.load(self.user_registry_path)
        return IndicatorRegistry([])

    def _save_user_registry(self, reg: IndicatorRegistry) -> None:
        reg.save(self.user_registry_path)

    def load_full_registry(self) -> IndicatorRegistry:
        """Bootstrap system indicators + merge user rules."""
        try:
            reg = IndicatorRegistry.bootstrap(self.cfg.indicator_dir, self.cfg.mapping_path)
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

    def create_rule(
        self,
        *,
        name: str,
        formula_text: str,
        description: str = "",
        periods: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise ValueError("name is required")
        v = self.validate_formula(formula_text, name=name)
        if not v.get("ok"):
            raise ValueError(v.get("error") or "invalid formula")
        text = formula_text.strip()
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        rid = f"user_{_slug(name)}_{sha[:8]}"
        path = self.user_dir / f"{rid}.txt"
        path.write_text(text, encoding="utf-8")
        periods_t = tuple(periods or ["DAY", "WEEK", "MONTH"])
        deps = list(v.get("dependencies") or [])
        params = {
            "description": description or "",
            "created_at": int(time.time()),
            "source": "user",
            "archived": False,
        }
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
        if "MIN1" in deps:
            raise ValueError("MIN1 dependency is not supported without minute bars")
        spec = IndicatorSpec(
            id=rid,
            name=name,
            version=f"user:{sha[:12]}",
            kind="tdx_formula",
            output_type="signal",
            supported_periods=periods_t,
            source_file=str(path.resolve()),
            source_sha256=sha,
            package_file=None,
            package_sha256=None,
            compile_status="ready",
            parameters=params,
            aliases=[name],
            content_id=sha[:16],
            failure_reason=failure,
            dependencies=deps,
            is_default_visible=True,
            formula_text=text,
        )
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
    ) -> Dict[str, Any]:
        if not str(rule_id).startswith("user_"):
            raise ValueError("only user rules can be updated")
        ureg = self._load_user_registry()
        spec = ureg.get(rule_id)
        text = formula_text if formula_text is not None else (spec.formula_text or "")
        if formula_text is not None:
            v = self.validate_formula(text, name=name or spec.name)
            if not v.get("ok"):
                raise ValueError(v.get("error") or "invalid formula")
            sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
            path = self.user_dir / f"{rule_id}.txt"
            path.write_text(text.strip(), encoding="utf-8")
            spec.formula_text = text.strip()
            spec.source_file = str(path.resolve())
            spec.source_sha256 = sha
            spec.version = f"user:{sha[:12]}"
            spec.content_id = sha[:16]
            spec.compile_status = "ready"
        if name is not None:
            spec.name = name.strip() or spec.name
            spec.aliases = [spec.name]
        params = dict(spec.parameters or {})
        if description is not None:
            params["description"] = description
        params["updated_at"] = int(time.time())
        spec.parameters = params
        ureg.register(spec)
        self._save_user_registry(ureg)
        self.load_full_registry()
        return rule_to_public(spec, include_formula=True)

    def archive_rule(self, rule_id: str) -> Dict[str, Any]:
        if not str(rule_id).startswith("user_"):
            raise ValueError("only user rules can be archived")
        ureg = self._load_user_registry()
        spec = ureg.get(rule_id)
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
        path = self._hidden_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(sorted(ids), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def delete_rule(self, rule_id: str, *, permanent: bool = True) -> Dict[str, Any]:
        # user_*: hard-delete; builtin/import: hide from list only
        rule_id = str(rule_id)
        reg = self.load_full_registry()
        try:
            spec = reg.get(rule_id)
        except KeyError as e:
            raise KeyError(rule_id) from e

        if rule_id.startswith("user_"):
            if not permanent:
                return self.archive_rule(rule_id)
            ureg = self._load_user_registry()
            if rule_id in ureg._by_id:
                del ureg._by_id[rule_id]
                self._save_user_registry(ureg)
            fpath = self.user_dir / f"{rule_id}.txt"
            if fpath.exists():
                try:
                    fpath.unlink()
                except Exception:
                    pass
            hidden = self._load_hidden_ids()
            if rule_id in hidden:
                hidden.discard(rule_id)
                self._save_hidden_ids(hidden)
            self.load_full_registry()
            return {
                "id": rule_id,
                "deleted": True,
                "mode": "hard",
                "name": spec.name,
            }

        hidden = self._load_hidden_ids()
        hidden.add(rule_id)
        self._save_hidden_ids(hidden)
        return {
            "id": rule_id,
            "deleted": True,
            "mode": "hide",
            "name": spec.name,
            "note": "builtin/import rule hidden; source files kept",
        }

    def restore_rule(self, rule_id: str) -> Dict[str, Any]:
        hidden = self._load_hidden_ids()
        if rule_id in hidden:
            hidden.discard(rule_id)
            self._save_hidden_ids(hidden)
        reg = self.load_full_registry()
        spec = reg.get(rule_id)
        pub = rule_to_public(spec)
        pub["hidden"] = False
        return pub
