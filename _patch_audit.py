from pathlib import Path

p = Path("wtpy/apps/astock/indicators/tn6_importer.py")
t = p.read_text(encoding="utf-8")

# Fix pair_source - still old
old_pair = '''    mapping[package_sha256] = {
        "package_sha256": package_sha256,
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
'''
new_pair = '''    pkg_path = None
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
'''
if old_pair not in t:
    raise SystemExit("pair body missing")
t = t.replace(old_pair, new_pair, 1)

# Add package_file param to signature if missing
if "package_file: Optional[Path] = None" not in t[t.find("def pair_source"):t.find("def pair_source")+400]:
    t = t.replace(
        '''def pair_source(
    mapping: Dict[str, dict],
    package_sha256: str,
    source_file: Path,
    *,
    note: str = "",
) -> Dict[str, dict]:
''',
        '''def pair_source(
    mapping: Dict[str, dict],
    package_sha256: str,
    source_file: Path,
    *,
    note: str = "",
    package_file: Optional[Path] = None,
) -> Dict[str, dict]:
'''
    )

if "package_file=tn6_path" not in t:
    t = t.replace(
        "pair_source(mapping, sha, source_path, note=note)",
        "pair_source(mapping, sha, source_path, note=note, package_file=tn6_path)",
    )

# Cut from is_confirmation_valid to EOF and replace
i = t.find("def is_confirmation_valid")
if i < 0:
    raise SystemExit("no is_confirmation_valid")
# Keep confirm_source_pair which is BETWEEN is_confirmation and resolve - need full rewrite of tail
# Structure currently: is_confirmation, confirm_source_pair, resolve_formula_audit
# We'll rewrite from is_confirmation_valid to end with all three functions properly ordered

confirm_i = t.find("def confirm_source_pair")
# keep file before is_confirmation_valid
head = t[:i]

new_tail = r'''
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
'''

p.write_text(head + new_tail if False else (t[:t.find("def is_confirmation_valid")] + new_tail.lstrip("\n")), encoding="utf-8")
# wait I used wrong variable - fix write
head = t[:t.find("def is_confirmation_valid")]
p.write_text(head + new_tail.lstrip("\n") if not new_tail.startswith("\n") else head + new_tail[1:], encoding="utf-8")
# The assignment above is messy. rewrite cleanly:
content = head + new_tail.lstrip("\n")
p.write_text(content, encoding="utf-8")
compile(content, "tn6_importer.py", "exec")
print("ok lines", len(content.splitlines()))
