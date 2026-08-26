# -*- coding: utf-8 -*-
"""从《高岛易断》全文抽取「营商类」断语，生成旁挂 sidecar：bagua_gaodao.json。

为什么要旁挂而不是写进 bagua_384.json：
    bagua_384.json 头部有 source_file/source_sha256 与权威 Excel 绑定，
    BaguaKnowledge.excel_consistency_check() 会逐行校验，
    且 service/gua.py::reimport_excel() 会从 Excel 整体重建该文件。
    任何写入 bagua_384.json 的额外字段都会破坏校验、并在下次 reimport 时丢失。
    因此高岛断语单独存一个文件，按 state_id 关联，互不干扰。

配对口径（为什么可靠）：
    《高岛易断》按周易通行本（King Wen）顺序编排，卦大标题形如 "3、水雷屯"，
    序号与 bagua_384.json 的 gua_order 一一对应（已实测 64/64 卦名完全一致）。
    爻位由爻名前缀推出（初=1、二=2、三=3、四=4、五=5、上=6），
    因此用 (gua_order, yao_order) 配对，比卦名模糊包含匹配更稳。

覆盖策略：
    营商类优先（别名：营商/商业/经商/买卖/贸易/营业/生意/财运），
    缺失时按 时运 → 功名 兜底；两者都无则留空，由现有 market_judgement 兜底。

用法::

    python -X utf8 scripts/build_gaodao_sidecar.py            # 生成 sidecar
    python -X utf8 scripts/build_gaodao_sidecar.py --dry-run  # 只看统计不写文件
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[1]
DEFAULT_TXT = Path(r"E:\风水书籍\高岛易断\高岛易断_全文.txt")
BAGUA_DIR = REPO / "wtpy" / "apps" / "astock" / "bagua"
DEFAULT_BAGUA_JSON = BAGUA_DIR / "bagua_384.json"
DEFAULT_OUT = BAGUA_DIR / "bagua_gaodao.json"

SCHEMA_VERSION = 1
EXTRACTOR_VERSION = "gaodao_sidecar_v1"

# 有效爻名（六爻）。"用九/用六" 是乾坤两卦的额外爻，384 爻体系中不存在，必须跳过。
VALID_YAO = {
    "初九": 1, "初六": 1,
    "九二": 2, "六二": 2,
    "九三": 3, "六三": 3,
    "九四": 4, "六四": 4,
    "九五": 5, "六五": 5,
    "上九": 6, "上六": 6,
}
EXTRA_YAO = ("用九", "用六")

# 卦大标题：行首 序号 + 、 + 卦名（如 "3、水雷屯"）
RE_GUA_TITLE = re.compile(r"^\s*(\d{1,2})\s*、\s*(\S+?)\s*$")
# 爻块起始：行首 爻名 + 全角冒号
RE_YAO_START = re.compile(
    r"^\s*(初九|初六|九二|六二|九三|六三|九四|六四|九五|六五|上九|上六|用九|用六)\s*："
)
# 单条占断："问XX：内容"
RE_ITEM = re.compile(r"问\s*([\u4e00-\u9fff]{1,4})\s*：\s*(.+)", re.S)

# 营商类别名（原文用词不统一）
COMMERCE_ALIASES = ("营商", "商业", "经商", "买卖", "贸易", "营业", "生意", "财运")
# 营商类缺失时的兜底顺序（越靠前越贴近投资语境）
FALLBACK_PRIORITY = ("时运", "功名")


def _clean(s: str) -> str:
    """归一化行内噪声：零宽字符去掉，全角空格转半角，首尾裁剪。"""
    return s.replace("\u200b", "").replace("\u3000", " ").strip()


def _strip_leading_symbols(name: str) -> str:
    """去掉卦名前的非汉字前缀（卦符等），只保留中文卦名。"""
    s = name.strip()
    while s and not ("\u4e00" <= s[0] <= "\u9fff"):
        s = s[1:]
    return s


def parse_gaodao(txt: str) -> Tuple[Dict[Tuple[int, int], Dict[str, str]], Dict[int, str], Counter]:
    """解析全文。

    返回三元组：
      - by_key: {(gua_order, yao_order): {类别: 断语}}，每爻每类别只取首次出现
      - titles: {gua_order: 卦名}
      - cat_counter: 全书占断类别频次（诊断用）
    """
    by_key: Dict[Tuple[int, int], Dict[str, str]] = {}
    titles: Dict[int, str] = {}
    cat_counter: Counter = Counter()

    cur_gua: Optional[int] = None
    cur_yao: Optional[int] = None
    in_block = False

    for raw in txt.splitlines():
        line = _clean(raw)
        if not line:
            continue

        m_gua = RE_GUA_TITLE.match(line)
        if m_gua:
            order = int(m_gua.group(1))
            if 1 <= order <= 64:
                cur_gua = order
                titles.setdefault(order, _strip_leading_symbols(m_gua.group(2)))
            else:
                cur_gua = None
            cur_yao = None
            in_block = False
            continue

        m_yao = RE_YAO_START.match(line)
        if m_yao:
            yao_name = m_yao.group(1)
            if yao_name in VALID_YAO:
                cur_yao = VALID_YAO[yao_name]
                in_block = True
            else:  # 用九/用六
                cur_yao = None
                in_block = False
            continue

        if not (in_block and cur_gua and cur_yao and "○" in line):
            continue

        bucket = by_key.setdefault((cur_gua, cur_yao), {})
        # 多个占断类别常挤在同一行、以 ○ 分隔，必须切片后逐条解析，
        # 否则只能拿到该行第一个类别。
        for seg in line.split("○"):
            seg = seg.strip()
            if not seg:
                continue
            m = RE_ITEM.match(seg)
            if not m:
                continue
            category = m.group(1)
            content = _clean(m.group(2))
            if not content:
                continue
            cat_counter[category] += 1
            # 同一爻同一类别只保留首次出现（引读区优先于正文重复段）
            bucket.setdefault(category, content)
    return by_key, titles, cat_counter


def pick_text(cats: Dict[str, str]) -> Tuple[str, str, str]:
    """按覆盖策略挑一条断语，返回 (text, category, kind)。

    kind: primary=营商类 / fallback=时运功名兜底 / missing=无可用断语
    """
    for alias in COMMERCE_ALIASES:
        if cats.get(alias):
            return cats[alias], alias, "primary"
    for alias in FALLBACK_PRIORITY:
        if cats.get(alias):
            return cats[alias], alias, "fallback"
    return "", "", "missing"


def build_sidecar(txt_path: Path, bagua_json: Path) -> dict:
    txt = txt_path.read_text(encoding="utf-8", errors="replace")
    sha = hashlib.sha256(txt_path.read_bytes()).hexdigest()
    by_key, titles, cat_counter = parse_gaodao(txt)

    kb = json.loads(bagua_json.read_text(encoding="utf-8"))
    entries = kb.get("entries") or []

    by_state_id: Dict[str, dict] = {}
    missing: List[str] = []
    counts = {"primary": 0, "fallback": 0, "missing": 0}
    name_mismatch: List[str] = []

    for e in entries:
        go = int(e["gua_order"])
        yo = int(e.get("yao_order") or e.get("line_index") or 0)
        sid = e.get("state_id") or f"{go:02d}-{yo}"
        gua_name = e.get("gua_name") or e.get("main_hexagram_name") or ""
        yao_name = e.get("yao_name") or e.get("line_name") or ""
        if titles.get(go) and titles[go] != gua_name:
            name_mismatch.append(f"{go}: txt={titles.get(go)} kb={gua_name}")

        text, category, kind = pick_text(by_key.get((go, yo)) or {})
        counts[kind] += 1
        if kind == "missing":
            missing.append(str(sid))
            continue
        by_state_id[str(sid)] = {
            "text": text,
            "category": category,
            "gua_name": gua_name,
            "yao_name": yao_name,
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "source_file": txt_path.name,
        "source_path": str(txt_path),
        "source_sha256": sha,
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "extractor_version": EXTRACTOR_VERSION,
        "policy": {
            "primary": list(COMMERCE_ALIASES),
            "fallback": list(FALLBACK_PRIORITY),
            "match_key": "(gua_order, yao_order)",
            "note": "营商类优先；缺失时按 时运→功名 兜底；两者皆无则留空由 market_judgement 兜底。",
        },
        "counts": {
            "total": counts["primary"] + counts["fallback"],
            "primary": counts["primary"],
            "fallback": counts["fallback"],
            "missing": counts["missing"],
            "state_total": len(entries),
        },
        "missing_state_ids": sorted(missing),
        "gua_name_mismatch": name_mismatch,
        "category_frequency": dict(cat_counter.most_common(30)),
        "by_state_id": by_state_id,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="生成《高岛易断》营商断语 sidecar")
    ap.add_argument("--txt", default=str(DEFAULT_TXT), help="高岛易断全文 txt 路径")
    ap.add_argument("--bagua-json", default=str(DEFAULT_BAGUA_JSON), help="384 爻知识库路径")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="sidecar 输出路径")
    ap.add_argument("--dry-run", action="store_true", help="只打印统计，不写文件")
    args = ap.parse_args()

    txt_path = Path(args.txt)
    if not txt_path.exists():
        raise SystemExit(f"找不到全文文件: {txt_path}")
    bagua_json = Path(args.bagua_json)
    if not bagua_json.exists():
        raise SystemExit(f"找不到知识库: {bagua_json}")

    data = build_sidecar(txt_path, bagua_json)
    c = data["counts"]
    lines = [
        f"[源文件] {data['source_path']}",
        f"[sha256] {data['source_sha256']}",
        f"[覆盖]   营商 {c['primary']} + 兜底 {c['fallback']} = {c['total']} / {c['state_total']}"
        f"（缺失 {c['missing']}）",
        f"[缺失爻] {', '.join(data['missing_state_ids']) or '无'}",
    ]
    if data["gua_name_mismatch"]:
        lines.append(f"[警告] 卦名不一致 {len(data['gua_name_mismatch'])} 处: "
                     f"{data['gua_name_mismatch'][:5]}")
    if not args.dry_run:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        # 与 bagua_384.json 一致：UTF-8 无 BOM、缩进 2、不转义中文
        out.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        lines.append(f"[写入]   {out}")
    else:
        lines.append("[dry-run] 未写文件")

    report = "\n".join(lines)
    # 中文直接打印到 Windows 控制台易乱码，同时落一份 UTF-8 文件便于查看
    (REPO / "storage" / "astock").mkdir(parents=True, exist_ok=True)
    (REPO / "storage" / "astock" / "gaodao_sidecar_build.log").write_text(
        report + "\n", encoding="utf-8"
    )
    print(report)


if __name__ == "__main__":
    main()
