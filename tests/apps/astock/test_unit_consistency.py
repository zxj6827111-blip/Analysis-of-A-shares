"""Vendor unit normalization: volume 手→股 x100, amount 千元→元 x1000; docs must agree."""
from pathlib import Path

from wtpy.apps.astock.data.providers.local_vendor import LocalVendorProvider


CSV_HEADER = (
    "code,datetime,open,high,low,close,pre_close,change,pct_chg,volume,amount,"
    "turnover,turnover_free,volume_ratio,pe,pe_ttm,pb,ps,ps_ttm,dv_yield,dv_ttm,"
    "total_share,float_share,free_share,total_mv,circ_mv"
)


def _parse(rows):
    provider = LocalVendorProvider.__new__(LocalVendorProvider)  # no fs scan needed
    raw = ("﻿" + CSV_HEADER + "\n" + "\n".join(rows)).encode("utf-8")
    return provider._parse_csv(raw, "SSE.STK.600000", None, None)


class TestUnitFactors:
    def test_volume_x100_amount_x1000(self):
        # volume=1234.5 手, amount=678.9 千元
        bars = _parse([
            "600000.SH,2024-01-05,10,11,9,10.5,10,0.5,5,1234.5,678.9,"
            "1,1,1,10,10,1,1,1,1,1,100000,90000,80000,1000000,900000"
        ])
        assert len(bars) == 1
        assert abs(bars[0].volume - 123450.0) < 1e-9   # 1234.5 * 100
        assert abs(bars[0].amount - 678900.0) < 1e-9   # 678.9 * 1000

    def test_zero_and_missing_volume_amount(self):
        bars = _parse([
            "600000.SH,2024-01-05,10,11,9,10.5,10,0.5,5,,,"
            "1,1,1,10,10,1,1,1,1,1,100000,90000,80000,1000000,900000"
        ])
        assert bars[0].volume == 0.0
        assert bars[0].amount == 0.0

    def test_vwap_plausibility_after_conversion(self):
        # amount(千元)=close*volume(手)*100/1000 => vwap back in price range
        close, lots = 12.34, 5000.0
        amount_qianyuan = close * lots * 100 / 1000.0
        bars = _parse([
            f"600000.SH,2024-01-05,12,12.5,11.9,{close},12,0.3,2,{lots},{amount_qianyuan},"
            "1,1,1,10,10,1,1,1,1,1,100000,90000,80000,1000000,900000"
        ])
        vwap = bars[0].amount / bars[0].volume
        assert bars[0].low <= vwap <= bars[0].high


class TestUnitDocsConsistent:
    def test_provider_doc_says_qianyuan_not_wanyuan(self):
        import wtpy.apps.astock.data.providers.local_vendor as m
        text = Path(m.__file__).read_text(encoding="utf-8")
        assert "Amount unit: 千元" in text
        assert "Amount unit: 万元" not in text
        # conversion comments must agree with the implementation
        assert "* 1000.0" in text
        assert "* 100.0" in text

    def test_no_conflicting_wanyuan_unit_claim_in_data_layer(self):
        import wtpy.apps.astock.data.providers.local_vendor as m
        root = Path(m.__file__).resolve().parents[1]  # .../data
        offenders = []
        for p in root.rglob("*.py"):
            text = p.read_text(encoding="utf-8")
            for i, line in enumerate(text.splitlines(), 1):
                if "万元" in line and "Do NOT" not in line and "非万元" not in line \
                        and "万元 unit hypothesis" not in line \
                        and "x10000" not in line and "×10000" not in line:
                    # x10000/×10000 marks an explicit documented conversion to
                    # 元 (e.g. tdxquant Amount) — not a conflicting unit claim.
                    offenders.append(f"{p.name}:{i}: {line.strip()[:80]}")
        assert not offenders, f"conflicting 万元 unit claims: {offenders}"
