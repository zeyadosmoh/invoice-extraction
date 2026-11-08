"""The dbt vendor seed must agree with the synthetic ground truth (meta.vendor_id)."""

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "dbt" / "seeds" / "vendor_aliases.csv"
GT_DIR = ROOT / "data" / "synthetic" / "ground_truth"


def _seed() -> dict[str, dict]:
    with SEED.open(encoding="utf-8") as fh:
        return {r["printed_name"]: r for r in csv.DictReader(fh)}


def test_every_ground_truth_vendor_name_maps_to_its_vendor_id():
    seed = _seed()
    for path in sorted(GT_DIR.glob("doc_*.json")):
        gt = json.loads(path.read_text(encoding="utf-8"))
        printed = gt["invoice"]["vendor_name"]
        assert printed in seed, f"{path.name}: {printed!r} missing from vendor_aliases.csv"
        assert seed[printed]["vendor_id"] == gt["meta"]["vendor_id"], path.name


def test_canonical_name_and_vendor_id_are_one_to_one():
    pairs = {(r["vendor_id"], r["canonical_name"]) for r in _seed().values()}
    assert len({v for v, _ in pairs}) == len(pairs)
    assert len({c for _, c in pairs}) == len(pairs)
