"""
Summarize prefecture-level event counts from the row-level matched detail CSV.

This script intentionally does not geocode or spatially match anything. It
only reads the detail output produced by build_prefecture_event_summary.py and
aggregates accepted, non-province rows to modern prefecture-level cities.

Two indicators are written:
1. year_event_count: one count for a prefecture city in a year, no matter how
   many county/prefecture rows exist in that city-year.
2. year_location_count: for a city-year, count distinct matched counties if
   county rows exist; otherwise count one prefecture-level occurrence.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = ROOT / "result" / "明清时期灾荒食人年表_地点匹配明细.csv"
DEFAULT_OUTPUT = ROOT / "result" / "明清时期灾荒食人事件_地级市汇总.csv"


def clean_text(value: Any) -> str:
    if pd.isna(value):
        return ""
    text = str(value).strip()
    return "" if text in {"", "无", "nan", "None", "NONE", "null", "NULL"} else text


def read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")


def write_csv(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")


def summarize(detail: pd.DataFrame) -> pd.DataFrame:
    if "matched_prefecture" not in detail.columns and "matched_city" in detail.columns:
        detail["matched_prefecture"] = detail["matched_city"]
    if "matched_prefecture_code" not in detail.columns and "matched_city_code" in detail.columns:
        detail["matched_prefecture_code"] = detail["matched_city_code"]
    if "matched_province" not in detail.columns and "final_province" in detail.columns:
        detail["matched_province"] = detail["final_province"]
    if "matched_prefecture" not in detail.columns and "final_city" in detail.columns:
        detail["matched_prefecture"] = detail["final_city"]
    if "matched_prefecture_code" not in detail.columns and "final_city_code" in detail.columns:
        detail["matched_prefecture_code"] = detail["final_city_code"]

    if "matched_level" not in detail.columns and "location_level" in detail.columns:
        detail["matched_level"] = detail["location_level"].map(
            {
                "province_only": "province",
                "prefecture_level": "prefecture",
                "county_or_specific": "county",
            }
        )
    if "matched_county" not in detail.columns:
        detail["matched_county"] = ""
    if "matched_county_code" not in detail.columns:
        detail["matched_county_code"] = ""

    required = [
        "year_ce",
        "matched_level",
        "matched_province",
        "matched_prefecture",
        "matched_prefecture_code",
        "matched_county",
        "matched_county_code",
    ]
    for col in required:
        if col not in detail.columns:
            detail[col] = ""

    accepted = detail[
        detail["matched_level"].map(clean_text).ne("province")
        & detail["year_ce"].map(clean_text).ne("")
        & detail["matched_prefecture_code"].map(clean_text).ne("")
    ].copy()
    accepted["county_unit"] = accepted.apply(
        lambda r: clean_text(r.get("matched_county_code", "")) or clean_text(r.get("matched_county", "")),
        axis=1,
    )

    city_cols = ["matched_province", "matched_prefecture", "matched_prefecture_code"]
    city_year_cols = city_cols + ["year_ce"]
    year_event = accepted[city_year_cols].drop_duplicates()

    def count_city_year_locations(group: pd.DataFrame) -> int:
        county_units = sorted({unit for unit in group["county_unit"].map(clean_text) if unit})
        if county_units:
            return len(county_units)
        return 1

    city_year_location = (
        accepted.groupby(city_year_cols, dropna=False)
        .apply(count_city_year_locations)
        .reset_index(name="city_year_location_count")
    )

    summary = (
        year_event.groupby(city_cols, dropna=False)
        .size()
        .reset_index(name="year_event_count")
        .merge(
            city_year_location.groupby(city_cols, dropna=False)["city_year_location_count"]
            .sum()
            .reset_index(name="year_location_count"),
            on=city_cols,
            how="left",
        )
        .sort_values(["year_event_count", "year_location_count", "matched_province", "matched_prefecture"], ascending=[False, False, True, True])
    )
    summary = summary.rename(
        columns={"matched_prefecture": "matched_city", "matched_prefecture_code": "matched_city_code"}
    )
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize prefecture-level event counts from matched detail CSV.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--clean-intermediate",
        action="store_true",
        help="Compatibility flag; the matched detail CSV is a final output and will not be deleted.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    detail = read_csv(args.input)
    summary = summarize(detail)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8-sig") as f:
        f.write("# year_event_count: 该地级市有多少个不同年份有灾荒食人记录（按省份+城市+年份去重）\n")
        f.write("# year_location_count: 该地级市所有年份累计涉及的县级地点数。每个城市-年份内如有多个县级地点则按地点数计，无县级地点则计为1\n")
    summary.to_csv(args.output, index=False, encoding="utf-8-sig", mode="a")
    year_event_total = int(summary["year_event_count"].sum()) if not summary.empty else 0
    year_location_total = int(summary["year_location_count"].sum()) if not summary.empty else 0
    print(f"Input detail rows: {len(detail)}")
    print(
        f"Wrote summary rows: {len(summary)}, "
        f"year_event_count={year_event_total}, "
        f"year_location_count={year_location_total}"
    )
    print(f"Output: {args.output}")
    if args.clean_intermediate:
        print("No files deleted: matched detail is a final output, not an intermediate file.")


if __name__ == "__main__":
    main()
