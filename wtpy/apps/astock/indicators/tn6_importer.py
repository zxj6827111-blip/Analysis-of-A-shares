"""TN6 package intake: register, dedupe by SHA256, never reverse-engineer."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .models import IndicatorSpec, Tn6Package
from ..data.io_util import atomic_write_json


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def slugify(name: str) -> str:
    s = name.strip()
    s = re.sub(r"\.tn6$", "", s, flags=re.I)
    s = re.sub(r"\.txt$", "", s, flags=re.I)
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "indicator"


def scan_tn6_dir(directory: Path) -> List[Tn6Package]:
    directory = Path(directory)
    packages: List[Tn6Package] = []
    if not directory.exists():
        return packages
    for p in sorted(directory.glob("*.tn6")):
        digest = file_sha256(p)
        packages.append(
            Tn6Package(
                path=str(p.resolve()),
                name=p.stem,
                sha256=digest,
                size=p.stat().st_size,
                content_id=digest[:16],
            )
        )
    return packages


def load_source_map(path: Path) -> Dict[str, dict]:
    path = Path(path)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_source_map(path: Path, mapping: Dict[str, dict]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, mapping)


def pair_source(
    mapping: Dict[str, dict],
    package_sha256: str,
    source_file: Path,
    *,
    note: str = "",
    package_file: Optional[Path] = None,
) -> Dict[str, dict]:
    """Explicit package->source pairing. Never inferred from filename alone."""
    source_file = Path(source_file)
    if not source_file.exists():
        raise FileNotFoundError(source_file)
    src_sha = file_sha256(source_file)
    pkg_path = None
    if package_file is not None:
        package_file = Path(package_file)
        if not package_file.exists():
            raise FileNotFoundError(package_file)
        pkg_path = str(package_file.resolve())
    mapping[package_sha256] = {
        "package_sha256": package_sha256,
        "package_file": pkg_path,
        "source_file": str(source_file.resolve()),
        "source_sha256": src_sha,
        "note": note,
        "formula_provenance": "user_confirmation_required",
        "source_pair_status": "paired_unconfirmed",
        "formal_backtest_allowed": False,
        "research_backtest_allowed": True,
        "schema_version": 2,
        "confirmation": None,
    }
    return mapping


def read_formula_text(path: Path) -> str:
    raw = Path(path).read_bytes()
    # strip UTF-8 BOM so formulas do not fail at column 1
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    for enc in ("utf-8", "gbk", "gb18030", "utf-16"):
        try:
            text = raw.decode(enc)
            return text.lstrip("\ufeff")
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace").lstrip("\ufeff")


def detect_dependencies(formula: str) -> List[str]:
    deps: List[str] = []
    if re.search(r"#MIN60", formula, flags=re.I):
        deps.append("MIN60")
    if re.search(r"#MIN1\b", formula, flags=re.I):
        deps.append("MIN1")
    if re.search(r"#WEEK", formula, flags=re.I):
        deps.append("WEEK")
    if re.search(r"#MONTH", formula, flags=re.I):
        deps.append("MONTH")
    return deps


def detect_output_type(formula: str) -> str:
    if re.search(r"\bXG\s*:", formula, flags=re.I):
        return "signal"
    return "series"


def build_specs_from_indicator_dir(
    indicator_dir: Path,
    mapping: Optional[Dict[str, dict]] = None,
    *,
    min60_available: bool = False,
) -> List[IndicatorSpec]:
    """Scan 指标 directory: register all .tn6 and .txt; pair only via explicit map."""
    indicator_dir = Path(indicator_dir)
    mapping = mapping or {}
    packages = scan_tn6_dir(indicator_dir)

    # content_id -> primary package
    by_hash: Dict[str, List[Tn6Package]] = {}
    for pkg in packages:
        by_hash.setdefault(pkg.sha256, []).append(pkg)

    specs: List[IndicatorSpec] = []
    seen_content: Dict[str, str] = {}  # sha -> primary id

    for sha, group in by_hash.items():
        # Prefer a human-facing package name when duplicates share content
        # (e.g. 双增20260707测试版 over older same-hash copies).
        def _rank(pkg):
            n = pkg.name or ""
            score = 0
            if "测试" in n:
                score += 20
            if "20260707" in n:
                score += 10
            return (-score, n)

        primary = sorted(group, key=_rank)[0]
        aliases = [g.name for g in group]
        content_id = primary.content_id
        ind_id = f"tn6_{slugify(primary.name)}"
        if len(group) > 1:
            # stable id by content
            ind_id = f"tn6_content_{content_id}"
        seen_content[sha] = ind_id

        map_entry = mapping.get(sha)
        source_file = None
        source_sha = None
        formula = None
        status = "source_required"
        failure = "No explicit formula source paired for this .tn6 package."
        deps: List[str] = []
        output_type = "signal"
        periods = ("DAY", "WEEK", "MONTH")
        params: dict = {}

        if map_entry:
            source_file = map_entry.get("source_file")
            source_sha = map_entry.get("source_sha256")
            if source_file and Path(source_file).exists():
                formula = read_formula_text(Path(source_file))
                deps = detect_dependencies(formula)
                output_type = detect_output_type(formula)
                params: dict = {}
                if "MIN60" in deps and not min60_available:
                    # Research: day-line MACD proxy for #MIN60 (not true minute bars).
                    from .compiler import compile_formula
                    cr = compile_formula(formula, indicator_id=ind_id)
                    if cr.ok:
                        status = "ready"
                        failure = (
                            "MIN60 satisfied via day-line MACD research proxy "
                            "(min60_day_proxy); not true 60-minute data."
                        )
                        params = {
                            "min60_day_proxy": True,
                            "min60_proxy_note": (
                                "MACD.DIF#MIN60 / MACD.DEA#MIN60 filled from daily DIF/DEA"
                            ),
                        }
                    else:
                        status = "invalid"
                        failure = cr.error or "compile failed"
                else:
                    from .compiler import compile_formula
                    cr = compile_formula(formula, indicator_id=ind_id)
                    if cr.ok:
                        status = "ready"
                        failure = None
                    else:
                        status = "invalid"
                        failure = cr.error or "compile failed"
            else:
                status = "source_required"
                failure = f"Mapped source missing: {source_file}"
                params = {}

        specs.append(
            IndicatorSpec(
                id=ind_id,
                name=primary.name,
                version=f"content:{content_id}",
                kind="tdx_formula",
                output_type=output_type,  # type: ignore
                supported_periods=periods,
                source_file=source_file,
                source_sha256=source_sha,
                package_file=primary.path,
                package_sha256=sha,
                compile_status=status,  # type: ignore
                parameters=params if map_entry else {},
                aliases=aliases,
                content_id=content_id,
                failure_reason=failure,
                dependencies=deps,
                is_default_visible=True,
                formula_text=formula,
            )
        )

    # standalone .txt formulas (not auto-equated to tn6)
    for p in sorted(indicator_dir.glob("*.txt")):
        formula = read_formula_text(p)
        deps = detect_dependencies(formula)
        output_type = detect_output_type(formula)
        src_sha = file_sha256(p)
        ind_id = f"txt_{slugify(p.stem)}"
        params: dict = {}
        if "MIN60" in deps and not min60_available:
            from .compiler import compile_formula
            cr = compile_formula(formula, indicator_id=ind_id)
            if cr.ok:
                status = "ready"
                failure = (
                    "MIN60 satisfied via day-line MACD research proxy "
                    "(min60_day_proxy); not true 60-minute data."
                )
                params = {
                    "min60_day_proxy": True,
                    "min60_proxy_note": (
                        "MACD.DIF#MIN60 / MACD.DEA#MIN60 filled from daily DIF/DEA"
                    ),
                }
            else:
                status = "invalid"
                failure = cr.error or "compile failed"
        else:
            from .compiler import compile_formula
            cr = compile_formula(formula, indicator_id=ind_id)
            if cr.ok:
                status = "ready"
                failure = None
            else:
                status = "invalid"
                failure = cr.error or "compile failed"
        specs.append(
            IndicatorSpec(
                id=ind_id,
                name=p.stem,
                version=f"source:{src_sha[:12]}",
                kind="tdx_formula",
                output_type=output_type,  # type: ignore
                supported_periods=("DAY", "WEEK", "MONTH"),
                source_file=str(p.resolve()),
                source_sha256=src_sha,
                package_file=None,
                package_sha256=None,
                compile_status=status,  # type: ignore
                parameters=params,
                aliases=[p.stem],
                content_id=src_sha[:16],
                failure_reason=failure,
                dependencies=deps,
                is_default_visible=True,
                formula_text=formula,
            )
        )
    return specs


def import_tn6_with_source(
    tn6_path: Path,
    source_path: Path,
    mapping_path: Path,
    *,
    note: str = "explicit user pairing",
) -> Tuple[dict, IndicatorSpec]:
    tn6_path = Path(tn6_path)
    source_path = Path(source_path)
    sha = file_sha256(tn6_path)
    mapping = load_source_map(mapping_path)
    pair_source(mapping, sha, source_path, note=note, package_file=tn6_path)
    save_source_map(mapping_path, mapping)
    formula = read_formula_text(source_path)
    deps = detect_dependencies(formula)
    status = "ready"
    failure = None
    params: dict = {}
    if "MIN60" in deps:
        from .compiler import compile_formula
        cr = compile_formula(formula, indicator_id=f"tn6_{slugify(tn6_path.stem)}")
        if cr.ok:
            status = "ready"
            failure = (
                "MIN60 satisfied via day-line MACD research proxy "
                "(min60_day_proxy); not true 60-minute data."
            )
            params = {
                "min60_day_proxy": True,
                "min60_proxy_note": (
                    "MACD.DIF#MIN60 / MACD.DEA#MIN60 filled from daily DIF/DEA"
                ),
            }
        else:
            status = "invalid"
            failure = cr.error or "compile failed"
    else:
        from .compiler import compile_formula
        cr = compile_formula(formula, indicator_id=f"tn6_{slugify(tn6_path.stem)}")
        if not cr.ok:
            status = "invalid"
            failure = cr.error or "compile failed"
    spec = IndicatorSpec(
        id=f"tn6_{slugify(tn6_path.stem)}",
        name=tn6_path.stem,
        version=f"content:{sha[:16]}",
        kind="tdx_formula",
        output_type=detect_output_type(formula),  # type: ignore
        supported_periods=("DAY", "WEEK", "MONTH"),
        source_file=str(source_path.resolve()),
        source_sha256=file_sha256(source_path),
        package_file=str(tn6_path.resolve()),
        package_sha256=sha,
        compile_status=status,  # type: ignore
        parameters=params,
        aliases=[tn6_path.stem],
        content_id=sha[:16],
        failure_reason=failure,
        dependencies=deps,
        formula_text=formula,
    )
    return mapping[sha], spec


def prune_invalid_source_map(mapping: Dict[str, dict], *, keep_package_shas: Optional[set] = None) -> Dict[str, dict]:
    """Drop entries whose source_file is missing or package not in keep set."""
    out = {}
    for k, v in mapping.items():
        src = v.get("source_file")
        if not src or not Path(src).exists():
            continue
        if keep_package_shas is not None and k not in keep_package_shas:
            # allow only if package path exists when recorded
            pkg = v.get("package_file")
            if pkg and not Path(pkg).exists():
                continue
        out[k] = v
    return out


def is_confirmation_valid(
    entry: dict,
    *,
    package_sha256: str,
    source_sha256: str,
    live_package_sha256: Optional[str] = None,
    live_source_sha256: Optional[str] = None,
) -> bool:
    """True only when confirmation binds to live file hashes strictly."""
    conf = entry.get("confirmation") or {}
    if not conf or not conf.get("confirmed_by"):
        return False
    conf_pkg = conf.get("package_sha256")
    conf_src = conf.get("source_sha256")
    if not conf_pkg or not conf_src:
        return False
    if not package_sha256 or not source_sha256:
        return False
    if conf_pkg != package_sha256 or conf_src != source_sha256:
        return False
    if entry.get("package_sha256") != package_sha256:
        return False
    if entry.get("source_sha256") != source_sha256:
        return False
    if live_package_sha256 is not None and live_package_sha256 != package_sha256:
        return False
    if live_source_sha256 is not None and live_source_sha256 != source_sha256:
        return False
    return entry.get("formula_provenance") == "user_provided_human_formula"


def confirm_source_pair(
    mapping_path: Path,
    package_sha256: str,
    *,
    confirmed_by: str,
    note: str = "",
    confirmation_method: str = "cli_confirm_user_provided",
) -> dict:
    """Mark an existing pair as user-confirmed human formula. Does not invent sources."""
    from datetime import datetime, timezone

    mapping = load_source_map(mapping_path)
    if package_sha256 not in mapping:
        raise KeyError(f"no mapping for package {package_sha256}")
    entry = dict(mapping[package_sha256])
    src = entry.get("source_file")
    if not src or not Path(src).exists():
        raise FileNotFoundError(f"source missing: {src}")
    pkg_file = entry.get("package_file")
    if not pkg_file or not Path(pkg_file).exists():
        raise FileNotFoundError(f"package_file missing: {pkg_file}")
    live_pkg = file_sha256(Path(pkg_file))
    if live_pkg != package_sha256:
        raise ValueError(
            f"package hash mismatch live={live_pkg} map_key={package_sha256}"
        )
    src_sha = file_sha256(Path(src))
    entry["source_sha256"] = src_sha
    entry["package_file"] = str(Path(pkg_file).resolve())
    entry["source_file"] = str(Path(src).resolve())
    conf = {
        "package_sha256": package_sha256,
        "source_sha256": src_sha,
        "confirmed_at": datetime.now(timezone.utc).isoformat(),
        "confirmed_by": confirmed_by,
        "confirmation_method": confirmation_method,
        "note": note,
        "schema_version": 1,
    }
    entry["confirmation"] = conf
    entry["formula_provenance"] = "user_provided_human_formula"
    entry["source_pair_status"] = "paired_confirmed"
    entry["formal_backtest_allowed"] = True
    entry["research_backtest_allowed"] = True
    mapping[package_sha256] = entry
    save_source_map(mapping_path, mapping)
    return entry


def resolve_formula_audit(entry: Optional[dict], package_sha256: Optional[str] = None) -> dict:
    """Return audit fields for run_meta / gates.

    Formal backtest requires live source and package files whose hashes match
    the confirmation record. Missing files never fall through as formal-ok.
    """
    if not entry:
        return {
            "formula_provenance": "none",
            "source_pair_status": "unpaired",
            "formal_backtest_allowed": False,
            "research_backtest_allowed": False,
        }

    pkg = package_sha256 or entry.get("package_sha256")
    src_path = entry.get("source_file")
    pkg_path = entry.get("package_file")

    # txt-only self source (no package)
    if entry.get("source_pair_status") == "txt_only":
        if not src_path or not Path(src_path).exists():
            return {
                "formula_provenance": "user_confirmation_required",
                "source_pair_status": "source_missing",
                "formal_backtest_allowed": False,
                "research_backtest_allowed": True,
            }
        live_src = file_sha256(Path(src_path))
        expected = entry.get("source_sha256")
        if expected and live_src != expected:
            return {
                "formula_provenance": "user_confirmation_required",
                "source_pair_status": "stale_source_hash",
                "formal_backtest_allowed": False,
                "research_backtest_allowed": True,
            }
        return {
            "formula_provenance": "txt_self_source",
            "source_pair_status": "txt_only",
            "formal_backtest_allowed": True,
            "research_backtest_allowed": True,
            "source_file": str(Path(src_path).resolve()),
            "source_sha256": live_src,
            "package_file": None,
            "package_sha256": None,
        }

    if not src_path or not Path(src_path).exists():
        return {
            "formula_provenance": "user_confirmation_required",
            "source_pair_status": "source_missing",
            "formal_backtest_allowed": False,
            "research_backtest_allowed": True,
            "package_sha256": pkg,
            "source_file": src_path,
            "package_file": pkg_path,
        }

    live_src = file_sha256(Path(src_path))
    entry_src = entry.get("source_sha256")
    if entry_src and live_src != entry_src:
        return {
            "formula_provenance": "user_confirmation_required",
            "source_pair_status": "stale_source_hash",
            "formal_backtest_allowed": False,
            "research_backtest_allowed": True,
            "package_sha256": pkg,
            "source_sha256": live_src,
            "source_file": str(Path(src_path).resolve()),
            "package_file": pkg_path,
        }

    live_pkg = None
    if not pkg_path or not Path(pkg_path).exists():
        return {
            "formula_provenance": "user_confirmation_required",
            "source_pair_status": "package_missing",
            "formal_backtest_allowed": False,
            "research_backtest_allowed": True,
            "package_sha256": pkg,
            "source_sha256": live_src,
            "source_file": str(Path(src_path).resolve()),
            "package_file": pkg_path,
        }
    live_pkg = file_sha256(Path(pkg_path))
    if pkg and live_pkg != pkg:
        return {
            "formula_provenance": "user_confirmation_required",
            "source_pair_status": "stale_package_hash",
            "formal_backtest_allowed": False,
            "research_backtest_allowed": True,
            "package_sha256": live_pkg,
            "source_sha256": live_src,
            "source_file": str(Path(src_path).resolve()),
            "package_file": str(Path(pkg_path).resolve()),
        }

    conf = entry.get("confirmation") or {}
    if not conf or not conf.get("confirmed_by"):
        return {
            "formula_provenance": "user_confirmation_required",
            "source_pair_status": "paired_unconfirmed",
            "formal_backtest_allowed": False,
            "research_backtest_allowed": True,
            "package_sha256": live_pkg,
            "source_sha256": live_src,
            "source_file": str(Path(src_path).resolve()),
            "package_file": str(Path(pkg_path).resolve()),
            "confirmed_by": None,
        }

    conf_pkg = conf.get("package_sha256")
    conf_src = conf.get("source_sha256")
    if not conf_pkg or not conf_src:
        return {
            "formula_provenance": "user_confirmation_required",
            "source_pair_status": "paired_unconfirmed",
            "formal_backtest_allowed": False,
            "research_backtest_allowed": True,
            "package_sha256": live_pkg,
            "source_sha256": live_src,
            "source_file": str(Path(src_path).resolve()),
            "package_file": str(Path(pkg_path).resolve()),
        }
    if conf_src != live_src:
        return {
            "formula_provenance": "user_confirmation_required",
            "source_pair_status": "stale_source_hash",
            "formal_backtest_allowed": False,
            "research_backtest_allowed": True,
            "package_sha256": live_pkg,
            "source_sha256": live_src,
            "source_file": str(Path(src_path).resolve()),
            "package_file": str(Path(pkg_path).resolve()),
        }
    if conf_pkg != live_pkg:
        return {
            "formula_provenance": "user_confirmation_required",
            "source_pair_status": "stale_package_hash",
            "formal_backtest_allowed": False,
            "research_backtest_allowed": True,
            "package_sha256": live_pkg,
            "source_sha256": live_src,
            "source_file": str(Path(src_path).resolve()),
            "package_file": str(Path(pkg_path).resolve()),
        }

    return {
        "formula_provenance": "user_provided_human_formula",
        "source_pair_status": "paired_confirmed",
        "formal_backtest_allowed": True,
        "research_backtest_allowed": True,
        "package_sha256": live_pkg,
        "source_sha256": live_src,
        "source_file": str(Path(src_path).resolve()),
        "package_file": str(Path(pkg_path).resolve()),
        "confirmed_by": conf.get("confirmed_by"),
        "confirmed_at": conf.get("confirmed_at"),
        "confirmation_method": conf.get("confirmation_method"),
        "confirmation_note": conf.get("note"),
    }
