"""RuleService custom rules: validate, save, list via shipped service."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import get_default_config
from wtpy.apps.astock.service.rules import RuleService


def _svc(tmp_path: Path):
    storage = tmp_path / "storage"
    (storage / "indicators").mkdir(parents=True)
    ind = tmp_path / "ind"
    ind.mkdir(exist_ok=True)
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    return cfg, RuleService(cfg)


def test_create_user_rule_and_list(tmp_path: Path):
    storage = tmp_path / "storage"
    # minimal storage layout
    (storage / "indicators").mkdir(parents=True)
    cfg = get_default_config(storage_root=storage, indicator_dir=tmp_path / "empty_ind")
    (tmp_path / "empty_ind").mkdir(exist_ok=True)
    svc = RuleService(cfg)

    formula = "MA5:=MA(C,5);\nXG:C>MA5;\n"
    bad = svc.validate_formula("MA5:=MA(C,5);\n")
    assert bad["ok"] is False

    ok = svc.validate_formula(formula)
    assert ok["ok"] is True
    assert ok["has_xg"] is True

    created = svc.create_rule(name="测试金叉", formula_text=formula, description="unit")
    assert created["id"].startswith("user_")
    assert created["source"] == "user"
    assert created["backtestable"] is True
    assert created["formula_text"].strip().startswith("MA5")

    # persisted
    ureg = storage / "indicators" / "user_registry.json"
    assert ureg.exists()
    data = json.loads(ureg.read_text(encoding="utf-8"))
    assert any(x["id"] == created["id"] for x in data["indicators"])

    rows = svc.list_rules()
    ids = {r["id"] for r in rows}
    assert created["id"] in ids

    got = svc.get_rule(created["id"])
    assert got["name"] == "测试金叉"

    archived = svc.archive_rule(created["id"])
    assert archived["archived"] is True
    rows2 = svc.list_rules(include_archived=False)
    assert created["id"] not in {r["id"] for r in rows2}


MIN60_FORMULA = 'DIF60:="MACD.DIF#MIN60";\nXG:C>0 AND DIF60>0;'
MIN1_FORMULA = 'DIF1:="MACD.DIF#MIN1";\nXG:C>0 AND DIF1>0;'


def test_create_and_update_rule_recompute_formula_fields(tmp_path: Path):
    """create/update 同一公式落地管道：dependencies / min60 代理标记随公式
    切换刷新（不再引用时清除），description/category 更新保留。"""
    _, svc = _svc(tmp_path)
    created = svc.create_rule(
        name="m60规则",
        formula_text=MIN60_FORMULA,
        description="d1",
        category="趋势",
    )
    assert created["dependencies"] == ["MIN60"]
    assert created["min60_day_proxy"] is True
    assert created["failure_reason"]
    assert created["description"] == "d1"
    assert created["category"] == "趋势"

    updated = svc.update_rule(
        created["id"],
        name="m60规则改",
        formula_text="XG:C>0;",
        description="d2",
        category="低吸",
    )
    assert updated["name"] == "m60规则改"
    assert updated["dependencies"] == []
    assert updated["min60_day_proxy"] is False
    assert not updated["failure_reason"]
    assert updated["description"] == "d2"
    assert updated["category"] == "低吸"

    again = svc.update_rule(created["id"], formula_text=MIN60_FORMULA)
    assert again["dependencies"] == ["MIN60"]
    assert again["min60_day_proxy"] is True
    assert again["failure_reason"]

    with pytest.raises(ValueError):
        svc.update_rule(created["id"], formula_text=MIN1_FORMULA)
    # 失败请求不得污染已注册 spec
    assert svc.get_rule(created["id"])["dependencies"] == ["MIN60"]


def test_import_rule_txt_guards_and_basename(tmp_path: Path):
    cfg, svc = _svc(tmp_path)
    content = "XG:C>0;\n"
    got = svc.import_rule(filename="我的规则.txt", content=content)
    assert got["id"].startswith("txt_")
    assert got["name"] == "我的规则"
    target = Path(cfg.indicator_dir) / "我的规则.txt"
    assert target.read_text(encoding="utf-8") == content

    with pytest.raises(FileExistsError):
        svc.import_rule(filename="我的规则.txt", content="XG:C>1;")

    # 路径穿越：取 basename，落点限制在指标目录内
    svc.import_rule(filename="..\\evil.txt", content="XG:C>0;")
    assert (Path(cfg.indicator_dir) / "evil.txt").exists()
    assert not (tmp_path / "evil.txt").exists()

    with pytest.raises(ValueError):
        svc.import_rule(filename="pkg.tn6", content="x")
    with pytest.raises(ValueError):
        svc.import_rule(filename="note.md", content="x")
    with pytest.raises(ValueError):
        svc.import_rule(filename="empty.txt", content="   ")
    with pytest.raises(ValueError):
        svc.import_rule(filename="big.txt", content="x" * (512 * 1024 + 1))


def test_batch_validate_summary_single_failure_no_abort(tmp_path: Path):
    cfg, svc = _svc(tmp_path)
    good = svc.create_rule(name="good", formula_text="XG:C>0;")
    (Path(cfg.indicator_dir) / "broken.txt").write_text(
        "MA5:=MA(C,5);\n", encoding="utf-8"
    )
    (Path(cfg.indicator_dir) / "pkg.tn6").write_bytes(b"tn6-package")

    out = svc.batch_validate([good["id"], "txt_broken", "tn6_pkg", "nope"])
    assert out["summary"] == {"total": 4, "ok": 1, "failed": 3}
    by_id = {r["id"]: r for r in out["results"]}
    assert by_id[good["id"]]["ok"] is True
    assert by_id["txt_broken"]["ok"] is False
    assert by_id["tn6_pkg"]["ok"] is False and by_id["tn6_pkg"]["error"]
    assert by_id["nope"]["ok"] is False
    assert by_id["nope"]["error"] == "rule not found"

    all_out = svc.batch_validate()
    assert all_out["summary"]["total"] >= 4
    assert all_out["summary"]["ok"] >= 1
    assert all_out["summary"]["failed"] == (
        all_out["summary"]["total"] - all_out["summary"]["ok"]
    )


def test_rule_categories_sidecar_roundtrip(tmp_path: Path):
    cfg, svc = _svc(tmp_path)
    assert svc.list_categories() == []
    assert svc.add_category("趋势") == ["趋势"]
    assert svc.add_category("趋势") == ["趋势"]  # 重复幂等
    assert svc.add_category("低吸") == ["趋势", "低吸"]

    path = Path(cfg.storage_root) / "indicators" / "rule_categories.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "categories": ["趋势", "低吸"]
    }
    assert RuleService(cfg).list_categories() == ["趋势", "低吸"]

    with pytest.raises(ValueError):
        svc.add_category("   ")
    with pytest.raises(ValueError):
        svc.add_category("x" * 21)


# --- 安全回归：路径穿越 / 别名规范路径 / 导入后缀 ---------------------------


def test_update_rule_alias_uses_canonical_id_path(tmp_path: Path):
    """别名（name 形如 user_*）允许反查，但写盘路径与注册表键必须用
    规范 spec.id，不能按请求参数拼 user_dir。"""
    _, svc = _svc(tmp_path)
    created = svc.create_rule(name="user_别名目标", formula_text="XG:C>0;")
    canonical = created["id"]
    assert canonical.startswith("user_")
    assert canonical != "user_别名目标"

    updated = svc.update_rule("user_别名目标", formula_text="XG:C>1;")
    assert updated["id"] == canonical

    canonical_path = svc.user_dir / f"{canonical}.txt"
    assert canonical_path.read_text(encoding="utf-8").strip() == "XG:C>1;"
    row = svc.get_rule(canonical, include_formula=True)
    assert Path(row["source_file"]).name == f"{canonical}.txt"

    # 越界字符一律拒绝；非 user 规则改回「仅隐藏」语义（源文件保留）
    with pytest.raises(ValueError):
        svc.update_rule("user_..\\..\\..\\escape", formula_text="XG:C>1;")
    with pytest.raises(ValueError):
        svc.archive_rule("txt_任意")
    hidden = svc.delete_rule("bagua_ohlc")
    assert hidden["id"] == "bagua_ohlc"
    assert hidden["deleted"] is False and hidden["mode"] == "hide"
    assert "bagua_ohlc" not in {r["id"] for r in svc.list_rules()}
    assert "bagua_ohlc" in {
        r["id"] for r in svc.list_rules(include_hidden=True)
    }
    restored = svc.restore_rule("bagua_ohlc")
    assert restored["id"] == "bagua_ohlc" and restored["hidden"] is False
    assert "bagua_ohlc" in {r["id"] for r in svc.list_rules()}


def test_delete_hide_restore_semantics_for_imported_rule(tmp_path: Path):
    """txt_ 导入规则 DELETE 仅隐藏：默认列表消失、include_hidden 可见、
    restore 后恢复；隐藏不落 user_dir 侧硬删路径。"""
    cfg, svc = _svc(tmp_path)
    imp = svc.import_rule(filename="隐藏目标.txt", content="XG:C>0;")
    rid = imp["id"]
    assert rid.startswith("txt_")
    target = Path(cfg.indicator_dir) / "隐藏目标.txt"

    out = svc.delete_rule(rid)
    assert out["id"] == rid and out["deleted"] is False and out["mode"] == "hide"
    assert target.exists(), "隐藏不得删除源文件"
    assert rid not in {r["id"] for r in svc.list_rules()}
    full = {r["id"]: r for r in svc.list_rules(include_hidden=True)}
    assert full[rid]["hidden"] is True

    restored = svc.restore_rule(rid)
    assert restored["hidden"] is False
    assert rid in {r["id"] for r in svc.list_rules()}

    # 别名反查（显示名）也按规范 spec.id 写入隐藏集合
    svc.delete_rule("隐藏目标")
    hidden = cfg.storage_root / "indicators" / "hidden_rule_ids.json"
    assert json.loads(hidden.read_text(encoding="utf-8")) == [rid]
    svc.restore_rule(rid)


def test_user_rule_unicode_ids_edit_delete_and_traversal_rejected(tmp_path: Path):
    """_slug 基于 Unicode \\w，白名单必须与之对齐：Café/テスト/Кирилл/한국규칙
    创建后可 PATCH（update）/DELETE；穿越变体仍被拒。"""
    _, svc = _svc(tmp_path)
    for name in ("Café趋势", "テスト規則", "Кирилл", "한국규칙"):
        created = svc.create_rule(name=name, formula_text="XG:C>0;")
        rid = created["id"]
        assert rid.startswith("user_")
        updated = svc.update_rule(rid, description="unicode-ok")
        assert updated["id"] == rid
        assert updated["description"] == "unicode-ok"
        deleted = svc.delete_rule(rid)
        assert deleted["id"] == rid and deleted["deleted"] is True
        with pytest.raises(KeyError):
            svc.get_rule(rid)

    for evil in (
        "user_..\\..\\..\\escape",
        "user_..%5C..%5Csentinel",
        "user_a/b",
        "user_a.b",
        "user_a b",
    ):
        with pytest.raises(ValueError):
            svc.update_rule(evil, formula_text="XG:C>1;")
        with pytest.raises(ValueError):
            svc.archive_rule(evil)


def test_create_rule_category_length_guard(tmp_path: Path):
    """create 服务层与 update/add_category 一致：category > 20 拒绝。"""
    _, svc = _svc(tmp_path)
    svc.create_rule(name="分类边界", formula_text="XG:C>0;", category="x" * 20)
    with pytest.raises(ValueError):
        svc.create_rule(name="分类超限", formula_text="XG:C>0;", category="x" * 21)


def test_rule_text_control_chars_sanitized_not_crash(tmp_path: Path):
    """规则名/描述里的 C0/C1/U+FFFF 入口清洗为 `_`，不落非法字符。"""
    _, svc = _svc(tmp_path)
    created = svc.create_rule(
        name="x\uffffy",
        formula_text="XG:C>0;",
        description="d\x01e",
        category="c\x7fd",
    )
    assert "\uffff" not in created["name"]
    assert created["name"].startswith("x") and created["name"].endswith("y")
    assert created["description"] == "d_e"
    assert created["category"] == "c_d"
    assert "\ud800" not in svc.create_rule(
        name="sur\ud800rogate", formula_text="XG:C>0;"
    )["name"]


def test_delete_rule_alias_deletes_canonical_file(tmp_path: Path):
    _, svc = _svc(tmp_path)
    created = svc.create_rule(name="user_删除别名", formula_text="XG:C>0;")
    canonical = created["id"]
    path = svc.user_dir / f"{canonical}.txt"
    assert path.exists()

    out = svc.delete_rule("user_删除别名")
    assert out["id"] == canonical and out["deleted"] is True
    assert not path.exists()
    with pytest.raises(KeyError):
        svc.get_rule(canonical)


def test_import_uppercase_txt_normalizes_suffix_and_lists(tmp_path: Path):
    cfg, svc = _svc(tmp_path)
    content = "XG:C>0;\n"
    got = svc.import_rule(filename="A.TXT", content=content)
    target = Path(cfg.indicator_dir) / "A.txt"
    assert target.read_text(encoding="utf-8") == content
    assert got["id"] == "txt_A" and got["name"] == "A"
    assert got["id"] in {r["id"] for r in svc.list_rules()}
    assert svc.get_rule(got["id"])["name"] == "A"


def test_update_rule_category_length_guard(tmp_path: Path):
    _, svc = _svc(tmp_path)
    created = svc.create_rule(name="分类上限", formula_text="XG:C>0;")
    assert svc.update_rule(created["id"], category="y" * 20)["category"] == "y" * 20
    with pytest.raises(ValueError):
        svc.update_rule(created["id"], category="x" * 21)
    # 失败请求不改动已注册状态
    assert svc.get_rule(created["id"])["category"] == "y" * 20


def test_user_rule_writes_leave_no_temp_files(tmp_path: Path):
    _, svc = _svc(tmp_path)
    created = svc.create_rule(name="原子写入", formula_text="XG:C>0;")
    svc.update_rule(created["id"], formula_text="XG:C>1;")
    indicators_dir = svc.user_registry_path.parent
    for directory in (svc.user_dir, indicators_dir):
        leftovers = list(directory.glob(".tmp-*")) + list(directory.glob(".*.part"))
        assert leftovers == [], leftovers


# --- sidecar 并发/容错 ------------------------------------------------------


def test_concurrent_create_rules_all_registered(tmp_path: Path):
    cfg, _svc0 = _svc(tmp_path)
    names = [f"并发规则{i}" for i in range(10)]

    # 每线程独立 RuleService 实例：进程级锁仍保证读-改-写串行
    with ThreadPoolExecutor(max_workers=10) as pool:
        created = list(
            pool.map(
                lambda n: RuleService(cfg).create_rule(
                    name=n, formula_text="XG:C>0;"
                ),
                names,
            )
        )

    ids = {r["id"] for r in created}
    assert len(ids) == len(names)
    data = json.loads(RuleService(cfg).user_registry_path.read_text(encoding="utf-8"))
    assert {x["id"] for x in data["indicators"]} == ids
    assert ids <= {r["id"] for r in RuleService(cfg).list_rules()}


def test_concurrent_add_category_no_lost_update(tmp_path: Path):
    cfg, _svc0 = _svc(tmp_path)
    names = [f"分类{i}" for i in range(8)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda n: RuleService(cfg).add_category(n), names))

    assert sorted(RuleService(cfg).list_categories()) == sorted(names)


def test_corrupt_sidecars_degrade_without_crash(tmp_path: Path):
    cfg, svc = _svc(tmp_path)
    svc.create_rule(name="健康规则", formula_text="XG:C>0;")
    svc.add_category("趋势")
    svc.user_registry_path.write_text("{broken json", encoding="utf-8")
    (Path(cfg.storage_root) / "indicators" / "rule_categories.json").write_text(
        "not-json", encoding="utf-8"
    )

    svc2 = RuleService(cfg)
    assert isinstance(svc2.list_rules(), list)  # 不得 500
    summary = svc2.batch_validate()  # 不得 500
    assert "summary" in summary
    assert svc2.list_categories() == []
