"""
Build modern prefecture-level summaries for famine cannibalism records.

Pipeline:
1. Build a unique location table from the consolidated CSV.
2. Produce an LLM-style location diagnosis table. By default this uses
   deterministic rules; pass --use-llm to call an OpenAI-compatible API.
3. Geocode raw and recommended modern locations with Baidu Maps, caching
   every query. Baidu BD-09 coordinates are converted to WGS84.
4. Spatially join WGS84 points to data/administrative_boundaries/市.shp and 县.shp.
5. Write matched row-level location details, manual-review records, and prefecture
   event counts.

The script is safe to run without external credentials. Without
BAIDU_MAP_AK, it still writes the unique-location and diagnosis outputs, and
marks geocoding-dependent rows for review.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, cast

import geopandas as gpd
import pandas as pd
from dotenv import load_dotenv
from shapely.geometry import Point
from tqdm import tqdm

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / ".env"
if ENV_FILE.exists():
    load_dotenv(ENV_FILE)

DEFAULT_INPUT = ROOT / "processing" / "merged_cleaned_data" / "ming_qing_famine_cannibalism_chen_ling" / "明清时期灾荒食人年表_汇总版.csv"
DEFAULT_CITY_SHP = ROOT / "data" / "administrative_boundaries" / "市.shp"
DEFAULT_COUNTY_SHP = ROOT / "data" / "administrative_boundaries" / "县.shp"
DEFAULT_OUTPUT_DIR = ROOT / "result"
DEFAULT_CACHE_DIR = ROOT / "data" / "location_review"

UNIQUE_OUT = "地名唯一表.csv"
LLM_OUT = "地名LLM初判.csv"
GEOCODE_CACHE_OUT = "地名地理编码缓存.csv"
MANUAL_REVIEW_XLSX_OUT = "地名人工复核表.xlsx"
MANUAL_REVIEW_LEGACY_CSV = "地名人工复核表.csv"
DETAIL_OUT = "明清时期灾荒食人年表_地点匹配明细.csv"
SUMMARY_OUT = "明清时期灾荒食人事件_地级市汇总.csv"
CORRECTIONS_IN = "地名人工校订表.csv"
CORRECTIONS_XLSX_IN = "地名人工校订表.xlsx"

BLANK_VALUES = {"", "无", "nan", "None", "NONE", "null", "NULL"}
DIRECT_CONTROLLED_MUNICIPALITIES = {"北京", "天津", "上海", "重庆"}
PROVINCE_QUERY_NAME_MAP = {
    "北京": "北京市",
    "天津": "天津市",
    "上海": "上海市",
    "重庆": "重庆市",
    "河北": "河北省",
    "山西": "山西省",
    "辽宁": "辽宁省",
    "吉林": "吉林省",
    "黑龙江": "黑龙江省",
    "江苏": "江苏省",
    "浙江": "浙江省",
    "安徽": "安徽省",
    "福建": "福建省",
    "江西": "江西省",
    "山东": "山东省",
    "河南": "河南省",
    "湖北": "湖北省",
    "湖南": "湖南省",
    "广东": "广东省",
    "海南": "海南省",
    "四川": "四川省",
    "贵州": "贵州省",
    "云南": "云南省",
    "陕西": "陕西省",
    "甘肃": "甘肃省",
    "青海": "青海省",
    "台湾": "台湾省",
    "广西": "广西壮族自治区",
    "内蒙古": "内蒙古自治区",
    "西藏": "西藏自治区",
    "宁夏": "宁夏回族自治区",
    "新疆": "新疆维吾尔自治区",
    "香港": "香港特别行政区",
    "澳门": "澳门特别行政区",
}
HISTORICAL_TERMS = (
    "府",
    "卫",
    "所",
    "厅",
    "路",
    "道",
    "司",
    "京师",
    "顺天府",
    "延绥镇",
)
MODERN_SUFFIXES = (
    "特别行政区",
    "自治区",
    "省",
    "市",
    "县",
    "区",
    "自治县",
    "自治州",
    "自治旗",
    "旗",
    "盟",
    "地区",
)


def clean_text(value: Any) -> str:
    if pd.isna(value):
        return ""
    text = str(value).strip()
    return "" if text in BLANK_VALUES else text


def ensure_existing_path(path: Path, pattern: str, root: Path) -> Path:
    if path.exists():
        return path
    matches = sorted(root.rglob(pattern), key=lambda p: p.stat().st_size, reverse=True)
    if not matches:
        raise FileNotFoundError(f"Cannot find {path} or any {pattern} under {root}")
    return matches[0]


def read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")


def read_table(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in {".xlsx", ".xls"}:
        return pd.read_excel(path, dtype=str).fillna("")
    return read_csv(path)


def write_csv(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")


def write_review_xlsx(df: pd.DataFrame, path: Path) -> None:
    try:
        from openpyxl import load_workbook
        from openpyxl.worksheet.datavalidation import DataValidation
    except ImportError:
        print("openpyxl is not installed; skipped XLSX review table.", file=sys.stderr)
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_excel(path, index=False)
    wb = load_workbook(path)
    ws = wb.active
    if ws is None:
        return
    if "confirmed_location_level" in df.columns:
        col_idx = list(df.columns).index("confirmed_location_level") + 1
        cell = ws.cell(row=1, column=col_idx)
        if cell is None:
            return
        col_letter = cell.column_letter
        dv = DataValidation(
            type="list",
            formula1='"province_only,prefecture_level,county_or_specific"',
            allow_blank=True,
        )
        ws.add_data_validation(dv)
        dv.add(f"{col_letter}2:{col_letter}{max(len(df) + 1, 2)}")
    wb.save(path)


def backup_file(path: Path, keep: int = 1) -> None:
    if not path.exists():
        return
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    backup_path = path.with_name(f"{path.stem}_backup_{timestamp}{path.suffix}")
    shutil.copy2(path, backup_path)
    print(f"Backed up existing file: {backup_path.name}")
    if keep <= 0:
        return
    backups = sorted(
        path.parent.glob(f"{path.stem}_backup_*{path.suffix}"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old_backup in backups[keep:]:
        old_backup.unlink()
        print(f"Deleted old backup: {old_backup.name}")


def location_key(row: pd.Series | dict[str, Any]) -> str:
    return "|".join(clean_text(row.get(col, "")) for col in ["province", "city", "county", "ancient_name"])


def province_name_for_query(province: str) -> str:
    text = clean_text(province)
    if not text:
        return ""
    return PROVINCE_QUERY_NAME_MAP.get(normalize_admin_name(text), text)


def build_query(province: str, city: str, county: str) -> str:
    province = clean_text(province)
    city = clean_text(city)
    county = clean_text(county)
    if city and county:
        parts = [city, county]
        seen: list[str] = []
        for part in parts:
            if part and part not in seen:
                seen.append(part)
        return "".join(seen)
    if province and county and not city:
        return f"{province_name_for_query(province)}{county}"
    parts = [province, city, county]
    seen: list[str] = []
    for part in parts:
        if part and part not in seen:
            seen.append(part)
    return "".join(seen)


def strip_common_suffix(name: str) -> str:
    text = clean_text(name)
    changed = True
    while changed and len(text) > 1:
        changed = False
        for suffix in sorted(MODERN_SUFFIXES, key=len, reverse=True):
            if text.endswith(suffix) and len(text) > len(suffix):
                text = text[: -len(suffix)]
                changed = True
                break
    return text


def normalize_admin_name(name: str) -> str:
    text = clean_text(name)
    for suffix in ("特别行政区", "自治区", "省", "市"):
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[: -len(suffix)]
            break
    aliases = {
        "广西壮族": "广西",
        "宁夏回族": "宁夏",
        "新疆维吾尔": "新疆",
        "内蒙古": "内蒙古",
        "西藏": "西藏",
    }
    return aliases.get(text, text)


def is_province_only_location(province: str, city: str, county: str) -> bool:
    province = clean_text(province)
    city = clean_text(city)
    county = clean_text(county)
    if not province:
        return False
    city_is_province = city and normalize_admin_name(city) == normalize_admin_name(province)
    county_is_province = county and normalize_admin_name(county) == normalize_admin_name(province)
    if not city and (not county or county_is_province):
        return True
    if (
        city_is_province
        and normalize_admin_name(province) not in DIRECT_CONTROLLED_MUNICIPALITIES
        and (not county or county_is_province)
    ):
        return True
    return False


def normalize_place_for_match(name: str) -> str:
    return strip_common_suffix(normalize_admin_name(name))


def classify_location_level(province: str, city: str, county: str, city_base_names: set[str]) -> str:
    province = clean_text(province)
    city = clean_text(city)
    county = clean_text(county)
    if is_province_only_location(province, city, county):
        return "province_only"
    city_base = normalize_place_for_match(city)
    county_base = normalize_place_for_match(county)
    if city:
        if not county or city_base == county_base:
            return "prefecture_level"
        return "county_or_specific"
    if county and county_base in city_base_names:
        return "prefecture_level"
    return "county_or_specific"


def apply_location_levels(unique_df: pd.DataFrame, city_gdf: gpd.GeoDataFrame) -> pd.DataFrame:
    city_base_names = {normalize_place_for_match(str(name)) for name in city_gdf["市"].map(clean_text) if clean_text(name)}
    result = unique_df.copy()
    result["location_level"] = result.apply(
        lambda r: classify_location_level(r["province"], r["city"], r["county"], city_base_names),
        axis=1,
    )
    return result


def build_modern_province_map(city_gdf: gpd.GeoDataFrame) -> dict[str, str]:
    province_names = sorted({str(name) for name in city_gdf["省"].map(clean_text) if name})
    mapping = {normalize_admin_name(name): name for name in province_names if name}
    mapping.update(
        {
            "直隶": "河北省",
            "顺天": "北京市",
            "京师": "北京市",
            "奉天": "辽宁省",
            "盛京": "辽宁省",
            "辽东": "辽宁省",
            "察哈尔": "河北省",
            "热河": "河北省",
            "西康": "四川省",
        }
    )
    return mapping


def build_modern_province_code_map(city_gdf: gpd.GeoDataFrame) -> dict[str, str]:
    province_names = city_gdf["省"].map(clean_text)
    province_codes = city_gdf["省代码"].map(normalize_admin_code)
    province = pd.DataFrame({"省": province_names, "省代码": province_codes})
    province = province[province["省"].ne("")].drop_duplicates("省")
    return {normalize_admin_name(row["省"]): row["省代码"] for _, row in province.iterrows()}


def modernize_province_name(province: str, province_map: dict[str, str]) -> str:
    text = clean_text(province)
    if not text:
        return ""
    parts = re.split(r"[、,，/；;]+", text)
    modern_parts: list[str] = []
    for part in parts:
        key = normalize_admin_name(part)
        modern_parts.append(province_map.get(key, part))
    return "、".join(dict.fromkeys(modern_parts))


def normalize_admin_code(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    try:
        return str(int(float(text)))
    except ValueError:
        return text


def build_unique_locations(df: pd.DataFrame) -> pd.DataFrame:
    work = df.copy()
    for col in ["province", "city", "county", "ancient_name"]:
        work[col] = work[col].map(clean_text)
    work["location_key"] = work.apply(location_key, axis=1)
    grouped = (
        work.groupby(["location_key", "province", "city", "county", "ancient_name"], dropna=False)
        .agg(event_count=("seq", "count"))
        .reset_index()
        .sort_values(["province", "city", "county", "ancient_name", "location_key"])
    )
    grouped["raw_query"] = grouped.apply(lambda r: build_query(r["province"], r["city"], r["county"]), axis=1)
    return grouped


def diagnose_location_rule(
    row: pd.Series,
    city_names: set[str],
    city_base_names: set[str],
    province_map: dict[str, str],
) -> dict[str, Any]:
    province = clean_text(row["province"])
    city = clean_text(row["city"])
    county = clean_text(row["county"])
    ancient_name = clean_text(row["ancient_name"])
    raw_query = build_query(province, city, county)
    location_joined = " ".join([province, city, county, ancient_name])
    historical_hits = [term for term in HISTORICAL_TERMS if term in location_joined]
    city_exact = city in city_names or county in city_names
    city_base = strip_common_suffix(city) in city_base_names or strip_common_suffix(county) in city_base_names
    has_specific_place = bool(county or city)
    is_province_only = clean_text(row.get("location_level", "")) == "province_only"

    if historical_hits:
        suspected = "是"
        confidence = "medium"
        needs_review = "是"
        reason = "疑似古地名:" + "、".join(sorted(set(historical_hits)))
    elif is_province_only:
        suspected = "否"
        confidence = "high"
        needs_review = "否"
        reason = "省级地点"
        raw_query = modernize_province_name(province, province_map)
    elif city_exact or city_base:
        suspected = "否"
        confidence = "medium"
        needs_review = "否"
        reason = "现代地名"
    elif not has_specific_place:
        suspected = "不确定"
        confidence = "low"
        needs_review = "是"
        reason = "缺少具体地点"
    else:
        suspected = "不确定"
        confidence = "low"
        needs_review = "是"
        reason = "需交叉验证"

    return {
        "location_key": row["location_key"],
        "is_suspected_historical": suspected,
        "recommended_modern_name": raw_query,
        "recommended_modern_province": province,
        "recommended_modern_city": city if city else "",
        "llm_reason": reason,
        "llm_confidence": confidence,
        "llm_needs_manual_review": needs_review,
        "analysis_source": "rule_fallback",
    }


def extract_json_array(text: str) -> list[dict[str, Any]]:
    match = re.search(r"\[[\s\S]*\]", text)
    if not match:
        raise ValueError("LLM response does not contain a JSON array")
    data = json.loads(match.group(0))
    if not isinstance(data, list):
        raise ValueError("LLM response JSON is not an array")
    return [item for item in data if isinstance(item, dict)]


def call_llm_batch(rows: pd.DataFrame) -> list[dict[str, Any]]:
    import requests

    api_key = os.environ.get("LLM_API_KEY", "")
    base_url = os.environ.get("LLM_BASE_URL", "https://api.minimax.chat/v1").rstrip("/")
    model = os.environ.get("LLM_MODEL", "abab6.5s-chat")
    if not api_key:
        raise RuntimeError("LLM_API_KEY is not set")

    payload_rows = [
        {
            "location_key": r.location_key,
            "province": r.province,
            "city": r.city,
            "county": r.county,
            "ancient_name": r.ancient_name,
        }
        for r in rows.itertuples(index=False)
    ]
    prompt = (
        "请判断这些明清灾荒记录中的地点是否可能是古代地名，并给出现代地名候选。"
        "只返回 JSON 数组，不要解释。每个对象必须包含：location_key, "
        "is_suspected_historical(是/否/不确定), recommended_modern_name, "
        "recommended_modern_province, recommended_modern_city, llm_reason, "
        "llm_confidence(high/medium/low), llm_needs_manual_review(是/否)。\n\n"
        + json.dumps(payload_rows, ensure_ascii=False)
    )
    response = requests.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": "你是中国历史地理和现代行政区划校勘助手。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
            "max_tokens": 4096,
        },
        timeout=120,
    )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    parsed = extract_json_array(content)
    for item in parsed:
        item["analysis_source"] = "llm"
    return parsed


def build_location_diagnosis(unique_df: pd.DataFrame, city_gdf: gpd.GeoDataFrame, use_llm: bool, batch_size: int) -> pd.DataFrame:
    city_names = {str(name) for name in city_gdf["市"].map(clean_text) if name}
    city_base_names = {strip_common_suffix(name) for name in city_names if name}
    province_map = build_modern_province_map(city_gdf)
    rule_rows = [diagnose_location_rule(row, city_names, city_base_names, province_map) for _, row in unique_df.iterrows()]
    diagnosis = pd.DataFrame(rule_rows)

    if not use_llm:
        return diagnosis

    llm_rows: list[dict[str, Any]] = []
    for start in tqdm(range(0, len(unique_df), batch_size), desc="LLM diagnosing locations"):
        batch = unique_df.iloc[start : start + batch_size]
        try:
            llm_rows.extend(call_llm_batch(batch))
        except Exception as exc:
            print(f"LLM batch {start}-{start + len(batch)} failed, keeping rule fallback: {exc}", file=sys.stderr)
    if not llm_rows:
        return diagnosis

    llm_df = pd.DataFrame(llm_rows)
    keep_cols = [
        "location_key",
        "is_suspected_historical",
        "recommended_modern_name",
        "recommended_modern_province",
        "recommended_modern_city",
        "llm_reason",
        "llm_confidence",
        "llm_needs_manual_review",
        "analysis_source",
    ]
    for col in keep_cols:
        if col not in llm_df.columns:
            llm_df[col] = ""
    merged = diagnosis.set_index("location_key")
    llm_df = llm_df[keep_cols].drop_duplicates("location_key").set_index("location_key")
    merged.update(llm_df)
    return merged.reset_index()


def bd09_to_gcj02(bd_lng: float, bd_lat: float) -> tuple[float, float]:
    x = bd_lng - 0.0065
    y = bd_lat - 0.006
    z = math.sqrt(x * x + y * y) - 0.00002 * math.sin(y * math.pi)
    theta = math.atan2(y, x) - 0.000003 * math.cos(x * math.pi)
    return z * math.cos(theta), z * math.sin(theta)


def out_of_china(lng: float, lat: float) -> bool:
    return not (73.66 < lng < 135.05 and 3.86 < lat < 53.55)


def transform_lat(lng: float, lat: float) -> float:
    ret = -100.0 + 2.0 * lng + 3.0 * lat + 0.2 * lat * lat + 0.1 * lng * lat + 0.2 * math.sqrt(abs(lng))
    ret += (20.0 * math.sin(6.0 * lng * math.pi) + 20.0 * math.sin(2.0 * lng * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(lat * math.pi) + 40.0 * math.sin(lat / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(lat / 12.0 * math.pi) + 320 * math.sin(lat * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def transform_lng(lng: float, lat: float) -> float:
    ret = 300.0 + lng + 2.0 * lat + 0.1 * lng * lng + 0.1 * lng * lat + 0.1 * math.sqrt(abs(lng))
    ret += (20.0 * math.sin(6.0 * lng * math.pi) + 20.0 * math.sin(2.0 * lng * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(lng * math.pi) + 40.0 * math.sin(lng / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(lng / 12.0 * math.pi) + 300.0 * math.sin(lng / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def gcj02_to_wgs84(lng: float, lat: float) -> tuple[float, float]:
    if out_of_china(lng, lat):
        return lng, lat
    a = 6378245.0
    ee = 0.00669342162296594323
    dlat = transform_lat(lng - 105.0, lat - 35.0)
    dlng = transform_lng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - ee * magic * magic
    sqrt_magic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((a * (1 - ee)) / (magic * sqrt_magic) * math.pi)
    dlng = (dlng * 180.0) / (a / sqrt_magic * math.cos(radlat) * math.pi)
    return lng * 2 - (lng + dlng), lat * 2 - (lat + dlat)


def bd09_to_wgs84(lng: float, lat: float) -> tuple[float, float]:
    gcj_lng, gcj_lat = bd09_to_gcj02(lng, lat)
    return gcj02_to_wgs84(gcj_lng, gcj_lat)


def baidu_geocode(address: str, ak: str, region: str = "") -> dict[str, Any]:
    params = {"address": address, "output": "json", "ak": ak}
    if region:
        params["region"] = region
    url = "https://api.map.baidu.com/geocoding/v3/?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))
    status = data.get("status")
    if status != 0:
        return {"query": address, "region": region, "status": "failed", "baidu_status": status, "error": data.get("message", "")}
    result = data.get("result", {})
    loc = result.get("location", {})
    bd_lng = float(loc["lng"])
    bd_lat = float(loc["lat"])
    wgs_lng, wgs_lat = bd09_to_wgs84(bd_lng, bd_lat)
    return {
        "query": address,
        "region": region,
        "status": "ok",
        "baidu_status": status,
        "bd_lng": bd_lng,
        "bd_lat": bd_lat,
        "lng_wgs84": wgs_lng,
        "lat_wgs84": wgs_lat,
        "confidence": result.get("confidence", ""),
        "level": result.get("level", ""),
        "precise": result.get("precise", ""),
        "comprehension": result.get("comprehension", ""),
        "error": "",
    }


def load_geocode_cache(path: Path) -> pd.DataFrame:
    columns = [
        "geocode_key",
        "query_type",
        "query",
        "region",
        "status",
        "baidu_status",
        "bd_lng",
        "bd_lat",
        "lng_wgs84",
        "lat_wgs84",
        "confidence",
        "level",
        "precise",
        "comprehension",
        "error",
    ]
    if path.exists():
        existing = read_csv(path)
        for col in columns:
            if col not in existing.columns:
                existing[col] = ""
        existing = existing[columns].drop_duplicates("geocode_key")
        if "address" in set(existing["query_type"]):
            existing = existing[existing["query_type"].eq("address")]
        return existing
    return pd.DataFrame(columns=columns)


def is_retryable_geocode_failure(row: pd.Series) -> bool:
    if clean_text(row.get("status", "")) != "failed":
        return False
    baidu_status = clean_text(row.get("baidu_status", ""))
    error = clean_text(row.get("error", ""))
    if baidu_status in {"401", "429"}:
        return True
    retry_keywords = ("并发量已经超过", "限制访问", "quota", "rate limit", "temporarily unavailable", "timeout")
    return any(keyword in error for keyword in retry_keywords)


def is_retryable_geocode_result(result: dict[str, Any]) -> bool:
    if clean_text(result.get("status", "")) != "failed":
        return False
    baidu_status = clean_text(result.get("baidu_status", ""))
    error = clean_text(result.get("error", ""))
    if baidu_status in {"401", "429"}:
        return True
    retry_keywords = ("并发量已经超过", "限制访问", "quota", "rate limit", "temporarily unavailable", "timeout")
    return any(keyword in error for keyword in retry_keywords)


def run_geocoding(
    unique_df: pd.DataFrame,
    diagnosis_df: pd.DataFrame,
    cache_path: Path,
    sleep_seconds: float,
    max_requests: int | None = None,
) -> pd.DataFrame:
    cache = load_geocode_cache(cache_path)
    # Current cache is query-level, not raw/recommended-location-level.
    # Drop legacy rows from earlier script versions so duplicate requests and
    # sandbox failures do not pollute the working cache.
    cache = cache[cache["query_type"].eq("address")].copy()
    ak = os.environ.get("BAIDU_MAP_AK", "")
    if ak:
        # Retry rows previously skipped because no key was configured, and keep
        # transient API failures eligible for a later rerun.
        retryable_mask = cache.apply(is_retryable_geocode_failure, axis=1)
        known = set(cache.loc[cache["status"].eq("ok") & ~retryable_mask, "geocode_key"])
    else:
        known = set(cache["geocode_key"])

    requests_needed: list[dict[str, str]] = []
    merged = unique_df.merge(diagnosis_df[["location_key", "recommended_modern_name"]], on="location_key", how="left")
    corrections = load_corrections(cache_path.parent / CORRECTIONS_IN, cache_path.parent / MANUAL_REVIEW_XLSX_OUT)
    if not corrections.empty:
        merged = merged.merge(corrections[["location_key", "confirmed_modern_address", "confirmed_location_level"]], on="location_key", how="left")
    else:
        merged["confirmed_modern_address"] = ""
        merged["confirmed_location_level"] = ""
    for row in merged.itertuples(index=False):
        province = clean_text(row.province)
        raw_query = clean_text(row.raw_query)
        rec_query = clean_text(row.recommended_modern_name)
        manual_query = clean_text(getattr(row, "confirmed_modern_address", ""))
        location_level = clean_text(getattr(row, "confirmed_location_level", "")) or clean_text(getattr(row, "location_level", ""))
        queries = [manual_query] if location_level == "province_only" else [raw_query, rec_query, manual_query]
        for query in queries:
            if not query:
                continue
            key = f"{province}|{query}"
            if key not in known:
                requests_needed.append({"geocode_key": key, "query_type": "address", "query": query, "region": province})
                known.add(key)

    new_rows: list[dict[str, Any]] = []
    if not ak:
        for item in requests_needed:
            new_rows.append({**item, "status": "skipped", "baidu_status": "", "error": "BAIDU_MAP_AK is not set"})
    else:
        if max_requests is not None and max_requests >= 0:
            requests_needed = requests_needed[:max_requests]
        for item in tqdm(requests_needed, desc="Baidu geocoding"):
            try:
                result = baidu_geocode(item["query"], ak, item["region"])
                if is_retryable_geocode_result(result):
                    for retry_idx in range(3):
                        time.sleep(max(sleep_seconds, 0.5) * (retry_idx + 1))
                        result = baidu_geocode(item["query"], ak, item["region"])
                        if not is_retryable_geocode_result(result):
                            break
                new_rows.append({**item, **result})
            except Exception as exc:
                new_rows.append({**item, "status": "failed", "baidu_status": "", "error": str(exc)})
            if sleep_seconds:
                time.sleep(sleep_seconds)

    if new_rows:
        new_keys = {row["geocode_key"] for row in new_rows}
        cache = cache[~cache["geocode_key"].isin(new_keys)]
        cache = pd.concat([cache, pd.DataFrame(new_rows)], ignore_index=True)
        cache = cache.drop_duplicates("geocode_key", keep="last")
        write_csv(cache, cache_path)
    return cache


def spatial_match(cache: pd.DataFrame, city_gdf: gpd.GeoDataFrame, county_gdf: gpd.GeoDataFrame | None = None) -> pd.DataFrame:
    ok = cache[cache["status"].eq("ok")].copy()
    if ok.empty:
        return cache.assign(
            matched_province="",
            matched_province_code="",
            matched_city="",
            matched_city_code="",
            matched_city_type="",
            matched_county="",
            matched_county_code="",
            matched_county_type="",
        )
    ok["lng_wgs84"] = pd.to_numeric(ok["lng_wgs84"], errors="coerce")
    ok["lat_wgs84"] = pd.to_numeric(ok["lat_wgs84"], errors="coerce")
    ok = ok.dropna(subset=["lng_wgs84", "lat_wgs84"])
    point_gdf = gpd.GeoDataFrame(
        ok,
        geometry=[Point(xy) for xy in zip(ok["lng_wgs84"], ok["lat_wgs84"])],
        crs="EPSG:4326",
    )
    city = city_gdf[["省", "省代码", "市", "市代码", "市类型", "geometry"]].copy()
    city = city.to_crs("EPSG:4326")
    joined = gpd.sjoin(point_gdf, city, how="left", predicate="within")
    matched = pd.DataFrame(joined.drop(columns=["geometry", "index_right"], errors="ignore"))
    matched = matched.rename(
        columns={"省": "matched_province", "市": "matched_city", "市代码": "matched_city_code", "市类型": "matched_city_type"}
    )
    matched = matched.rename(columns={"省代码": "matched_province_code"})
    base = cache.merge(
        matched[
            [
                "geocode_key",
                "matched_province",
                "matched_province_code",
                "matched_city",
                "matched_city_code",
                "matched_city_type",
            ]
        ],
        on="geocode_key",
        how="left",
    )
    for col in ["matched_province", "matched_province_code", "matched_city", "matched_city_code", "matched_city_type"]:
        base[col] = base[col].fillna("")
    base["matched_province_code"] = base["matched_province_code"].map(normalize_admin_code)
    base["matched_city_code"] = base["matched_city_code"].map(normalize_admin_code)

    if county_gdf is not None:
        county = county_gdf[["县", "县代码", "县类型", "geometry"]].copy()
        county = county.to_crs("EPSG:4326")
        county_joined = gpd.sjoin(point_gdf, county, how="left", predicate="within")
        county_matched = pd.DataFrame(county_joined.drop(columns=["geometry", "index_right"], errors="ignore"))
        county_matched = county_matched.rename(
            columns={"县": "matched_county", "县代码": "matched_county_code", "县类型": "matched_county_type"}
        )
        base = base.merge(
            county_matched[["geocode_key", "matched_county", "matched_county_code", "matched_county_type"]],
            on="geocode_key",
            how="left",
        )
    for col in ["matched_county", "matched_county_code", "matched_county_type"]:
        if col not in base.columns:
            base[col] = ""
        base[col] = base[col].fillna("")
    base["matched_county_code"] = base["matched_county_code"].map(normalize_admin_code)
    return base


def load_corrections(path: Path, review_path: Path | None = None) -> pd.DataFrame:
    cols = [
        "location_key",
        "confirmed_modern_address",
        "confirmed_location_level",
        "correction_note",
    ]
    frames: list[pd.DataFrame] = []
    candidates: list[Path] = [path]
    if path.suffix.lower() == ".csv":
        candidates.append(path.with_suffix(".xlsx"))
    if review_path is not None:
        candidates.append(review_path)
    for candidate in candidates:
        if candidate.exists():
            frames.append(read_table(candidate))
    if not frames:
        return pd.DataFrame(columns=cols)
    df = pd.concat(frames, ignore_index=True)
    for col in cols:
        if col not in df.columns:
            df[col] = ""
    df = df[cols]
    filled = df[
        df["confirmed_modern_address"].map(clean_text).ne("")
        | df["confirmed_location_level"].map(clean_text).ne("")
    ]
    return filled.drop_duplicates("location_key", keep="last")


def persist_corrections(corrections: pd.DataFrame, path: Path) -> None:
    cols = ["location_key", "confirmed_modern_address", "confirmed_location_level", "correction_note"]
    if corrections.empty:
        return
    out = corrections.copy()
    for col in cols:
        if col not in out.columns:
            out[col] = ""
    out = out[cols].drop_duplicates("location_key", keep="last")
    if path.exists():
        existing = read_table(path)
        for col in cols:
            if col not in existing.columns:
                existing[col] = ""
        out = pd.concat([existing[cols], out], ignore_index=True).drop_duplicates("location_key", keep="last")
    write_review_xlsx(out, path)


def round_coord(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    try:
        return f"{float(text):.6f}"
    except ValueError:
        return text


def strip_book_title_marks(value: Any) -> str:
    return clean_text(value).replace("《", "").replace("》", "")


def slim_detail_columns(detail: pd.DataFrame) -> pd.DataFrame:
    detail = detail.copy()
    for col in ["final_lng_wgs84", "final_lat_wgs84"]:
        if col in detail.columns:
            detail[col] = detail[col].map(round_coord)
    if "source" in detail.columns:
        detail["source"] = detail["source"].map(strip_book_title_marks)
    detail["matched_level"] = detail.apply(
        lambda r: "province"
        if clean_text(r.get("location_level", "")) == "province_only"
        else (
            "prefecture"
            if clean_text(r.get("location_level", "")) == "prefecture_level"
            else ("county" if clean_text(r.get("final_county", "")) else ("prefecture" if clean_text(r.get("final_city", "")) else ""))
        ),
        axis=1,
    )
    detail["matched_province"] = detail["final_province"]
    detail["matched_province_code"] = detail["final_province_code"]
    detail["matched_prefecture"] = detail.apply(
        lambda r: "" if clean_text(r.get("location_level", "")) == "province_only" else clean_text(r.get("final_city", "")),
        axis=1,
    )
    detail["matched_prefecture_code"] = detail.apply(
        lambda r: "" if clean_text(r.get("location_level", "")) == "province_only" else clean_text(r.get("final_city_code", "")),
        axis=1,
    )
    detail["matched_county"] = detail.apply(
        lambda r: clean_text(r.get("final_county", "")) if clean_text(r.get("matched_level", "")) == "county" else "",
        axis=1,
    )
    detail["matched_county_code"] = detail.apply(
        lambda r: clean_text(r.get("final_county_code", "")) if clean_text(r.get("matched_level", "")) == "county" else "",
        axis=1,
    )
    detail["lng_wgs84"] = detail["final_lng_wgs84"]
    detail["lat_wgs84"] = detail["final_lat_wgs84"]
    detail["original_location"] = detail.apply(
        lambda r: " / ".join(
            part
            for part in [
                clean_text(r.get("province", "")),
                clean_text(r.get("city", "")),
                clean_text(r.get("county", "")),
                clean_text(r.get("ancient_name", "")),
            ]
            if part
        ),
        axis=1,
    )
    keep_cols = [
        "seq",
        "year_ce",
        "year_era",
        "province",
        "city",
        "county",
        "ancient_name",
        "source",
        "record",
        "matched_level",
        "matched_province",
        "matched_province_code",
        "matched_prefecture",
        "matched_prefecture_code",
        "matched_county",
        "matched_county_code",
        "lng_wgs84",
        "lat_wgs84",
        "note",
    ]
    for col in keep_cols:
        if col not in detail.columns:
            detail[col] = ""
    return detail[keep_cols]


def make_detail_and_review(
    source_df: pd.DataFrame,
    unique_df: pd.DataFrame,
    diagnosis_df: pd.DataFrame,
    geocoded: pd.DataFrame,
    corrections: pd.DataFrame,
    city_gdf: gpd.GeoDataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    province_map = build_modern_province_map(city_gdf)
    province_code_map = build_modern_province_code_map(city_gdf)
    address_geocoded = geocoded[geocoded["query_type"].eq("address")].copy()
    raw_geo = address_geocoded.add_prefix("raw_")
    rec_geo = address_geocoded.add_prefix("rec_")
    unique = unique_df.merge(diagnosis_df, on="location_key", how="left")
    unique["raw_geocode_key"] = unique.apply(lambda r: f"{clean_text(r['province'])}|{clean_text(r['raw_query'])}", axis=1)
    unique["rec_geocode_key"] = unique.apply(
        lambda r: f"{clean_text(r['province'])}|{clean_text(r['recommended_modern_name'])}", axis=1
    )
    unique = unique.merge(corrections, on="location_key", how="left")
    unique["effective_location_level"] = unique.apply(
        lambda r: clean_text(r.get("confirmed_location_level", "")) or clean_text(r.get("location_level", "")),
        axis=1,
    )
    unique["manual_geocode_key"] = unique.apply(
        lambda r: f"{clean_text(r['province'])}|{clean_text(r['confirmed_modern_address'])}"
        if clean_text(r.get("confirmed_modern_address", ""))
        else "",
        axis=1,
    )
    unique = unique.merge(raw_geo, left_on="raw_geocode_key", right_on="raw_geocode_key", how="left")
    unique = unique.merge(rec_geo, left_on="rec_geocode_key", right_on="rec_geocode_key", how="left")
    manual_geo = address_geocoded.add_prefix("manual_")
    unique = unique.merge(manual_geo, left_on="manual_geocode_key", right_on="manual_geocode_key", how="left")

    def decide(row: pd.Series) -> pd.Series:
        reasons: list[str] = []
        accepted_source = ""
        province = clean_text(row.get("province", ""))
        location_level = clean_text(row.get("effective_location_level", "")) or clean_text(row.get("location_level", ""))
        if location_level == "province_only":
            modern_province = modernize_province_name(province, province_map)
            return pd.Series(
                {
                    "geo_status": "accepted_province_only",
                    "final_province": modern_province,
                    "final_province_code": province_code_map.get(normalize_admin_name(modern_province), ""),
                    "final_city": "",
                    "final_city_code": "",
                    "final_county": "",
                    "final_county_code": "",
                    "final_lng_wgs84": "",
                    "final_lat_wgs84": "",
                    "accepted_source": "province_only",
                    "review_reason": "",
                }
            )
        raw_status = clean_text(row.get("raw_status", ""))
        rec_status = clean_text(row.get("rec_status", ""))
        raw_city = clean_text(row.get("raw_matched_city", ""))
        rec_city = clean_text(row.get("rec_matched_city", ""))
        raw_county = clean_text(row.get("raw_matched_county", ""))
        rec_county = clean_text(row.get("rec_matched_county", ""))
        raw_province = clean_text(row.get("raw_matched_province", ""))
        rec_province = clean_text(row.get("rec_matched_province", ""))
        llm_city = clean_text(row.get("recommended_modern_city", ""))
        correction_address = clean_text(row.get("confirmed_modern_address", ""))
        correction_city = clean_text(row.get("manual_matched_city", ""))
        correction_county = clean_text(row.get("manual_matched_county", ""))

        if correction_address and correction_city:
            return pd.Series(
                {
                    "geo_status": "accepted_manual_correction",
                    "final_province": clean_text(row.get("manual_matched_province", "")),
                    "final_province_code": clean_text(row.get("manual_matched_province_code", "")),
                    "final_city": correction_city,
                    "final_city_code": clean_text(row.get("manual_matched_city_code", "")),
                    "final_county": correction_county,
                    "final_county_code": clean_text(row.get("manual_matched_county_code", "")),
                    "final_lng_wgs84": clean_text(row.get("manual_lng_wgs84", "")),
                    "final_lat_wgs84": clean_text(row.get("manual_lat_wgs84", "")),
                    "accepted_source": "manual_correction_geocode",
                    "review_reason": "",
                }
            )

        suspected_historical = clean_text(row.get("is_suspected_historical", "")) == "是"
        llm_review_flag = clean_text(row.get("llm_needs_manual_review", "")) == "是"
        province_only = clean_text(row.get("llm_reason", "")) == "省级地点"
        if suspected_historical:
            reasons.append("疑似古代地名需复核")
        if province_only:
            reasons.append("仅省级地点，不能汇总到地级市")
        if raw_status != "ok":
            reasons.append("原始地名地理编码未成功")
        if rec_status != "ok":
            reasons.append("推荐地名地理编码未成功")
        if raw_status == "ok" and not raw_city:
            reasons.append("原始地名坐标未落入地级市边界")
        if rec_status == "ok" and not rec_city:
            reasons.append("推荐地名坐标未落入地级市边界")
        if raw_province and province and normalize_admin_name(raw_province) != normalize_admin_name(province):
            reasons.append("原始地名落点省份与记录省份不一致")
        if rec_province and province and normalize_admin_name(rec_province) != normalize_admin_name(province):
            reasons.append("推荐地名落点省份与记录省份不一致")
        if raw_city and rec_city and raw_city != rec_city:
            reasons.append("原始地名与推荐地名落入不同地级市")
        if llm_city and raw_city and strip_common_suffix(llm_city) != strip_common_suffix(raw_city):
            reasons.append("LLM文本判断城市与原始地名落点不同")

        # A low-confidence rule/LLM flag should not block auto-acceptance when
        # both geocoding paths agree and the province is compatible. Historical
        # names remain review-only.
        soft_review_only = llm_review_flag and not suspected_historical and not province_only

        if raw_city and rec_city and raw_city == rec_city and (not reasons or (soft_review_only and not suspected_historical)):
            status = "accepted_raw_and_recommended_agree"
            accepted_source = "raw_geocode"
            final_city = raw_city
            final_province = raw_province
            final_province_code = clean_text(row.get("raw_matched_province_code", ""))
            final_city_code = clean_text(row.get("raw_matched_city_code", ""))
            final_county = raw_county
            final_county_code = clean_text(row.get("raw_matched_county_code", ""))
            lng = clean_text(row.get("raw_lng_wgs84", ""))
            lat = clean_text(row.get("raw_lat_wgs84", ""))
            reasons = []
        elif raw_city and raw_province and normalize_admin_name(raw_province) == normalize_admin_name(province) and (not reasons or soft_review_only):
            status = "accepted_raw_province_match"
            accepted_source = "raw_geocode"
            final_city = raw_city
            final_province = raw_province
            final_province_code = clean_text(row.get("raw_matched_province_code", ""))
            final_city_code = clean_text(row.get("raw_matched_city_code", ""))
            final_county = raw_county
            final_county_code = clean_text(row.get("raw_matched_county_code", ""))
            lng = clean_text(row.get("raw_lng_wgs84", ""))
            lat = clean_text(row.get("raw_lat_wgs84", ""))
            reasons = []
        else:
            status = "needs_review"
            final_city = ""
            final_province = ""
            final_province_code = ""
            final_city_code = ""
            final_county = ""
            final_county_code = ""
            lng = ""
            lat = ""

        return pd.Series(
            {
                "geo_status": status,
                "final_province": final_province,
                "final_province_code": final_province_code,
                "final_city": final_city,
                "final_city_code": final_city_code,
                "final_county": final_county,
                "final_county_code": final_county_code,
                "final_lng_wgs84": lng,
                "final_lat_wgs84": lat,
                "accepted_source": accepted_source,
                "review_reason": "；".join(dict.fromkeys(reasons)),
            }
        )

    decisions = unique.apply(decide, axis=1)
    unique = pd.concat([unique, decisions], axis=1)

    source = source_df.copy()
    for col in ["province", "city", "county", "ancient_name"]:
        source[col] = source[col].map(clean_text)
    source["location_key"] = source.apply(location_key, axis=1)
    detail = source.merge(
        unique.drop(columns=["event_count"], errors="ignore"),
        on=["location_key", "province", "city", "county", "ancient_name"],
        how="left",
    )
    detail = detail.rename(columns={"raw_query_x": "raw_query"})
    detail = detail.drop(columns=["raw_query_y"], errors="ignore")
    province_only_mask = detail["location_level"].map(clean_text).eq("province_only")
    geocode_prefixes = ("raw_", "rec_", "manual_")
    for col in detail.columns:
        if col.startswith(geocode_prefixes):
            detail.loc[province_only_mask, col] = ""

    review_cols = [
        "location_key",
        "confirmed_modern_address",
        "confirmed_location_level",
        "correction_note",
        "province",
        "city",
        "county",
        "ancient_name",
        "location_level",
        "event_count",
        "recommended_modern_name",
        "llm_reason",
        "raw_status",
        "raw_level",
        "raw_matched_province",
        "raw_matched_city",
        "review_reason",
    ]
    review = unique[unique["geo_status"].eq("needs_review")].copy()
    for col in [
        "confirmed_modern_address",
        "confirmed_location_level",
        "correction_note",
    ]:
        if col not in review.columns:
            review[col] = ""
    for col in review_cols:
        if col not in review.columns:
            review[col] = ""
    review = review[review_cols].sort_values(["province", "city", "county", "ancient_name"])

    return slim_detail_columns(detail), review


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build modern prefecture-level event summaries.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--city-shp", type=Path, default=DEFAULT_CITY_SHP)
    parser.add_argument("--county-shp", type=Path, default=DEFAULT_COUNTY_SHP)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--use-llm", action="store_true", help="Call LLM_API_KEY/LLM_BASE_URL for location diagnosis.")
    parser.add_argument("--llm-batch-size", type=int, default=20)
    parser.add_argument("--skip-geocode", action="store_true", help="Do not call Baidu; only reuse existing cache.")
    parser.add_argument("--geocode-sleep", type=float, default=0.1)
    parser.add_argument("--max-locations", type=int, default=None, help="Only process the first N unique locations for testing.")
    parser.add_argument("--max-geocode-requests", type=int, default=None, help="Cap new Baidu API requests in this run.")
    parser.add_argument(
        "--clean-intermediate",
        action="store_true",
        help="Delete intermediate outputs after writing the matched detail and manual review files.",
    )
    return parser.parse_args()


def delete_files(paths: list[Path]) -> None:
    for path in paths:
        try:
            if path.exists():
                path.unlink()
                print(f"Deleted intermediate: {path.name}")
        except OSError as exc:
            print(f"Could not delete {path}: {exc}", file=sys.stderr)


def main() -> None:
    args = parse_args()
    input_path = ensure_existing_path(args.input, "明清时期灾荒食人年表_汇总版.csv", ROOT / "processing")
    city_shp = ensure_existing_path(args.city_shp, "市.shp", ROOT / "data")
    county_shp = ensure_existing_path(args.county_shp, "县.shp", ROOT / "data")
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    DEFAULT_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Input CSV: {input_path}")
    print(f"City boundary: {city_shp}")
    print(f"County boundary: {county_shp}")
    source_df = read_csv(input_path)
    city_gdf = cast(gpd.GeoDataFrame, gpd.read_file(city_shp))
    if city_gdf.crs is None:
        city_gdf = cast(gpd.GeoDataFrame, city_gdf.set_crs("EPSG:4326"))
    else:
        city_gdf = cast(gpd.GeoDataFrame, city_gdf.to_crs("EPSG:4326"))
    county_gdf = cast(gpd.GeoDataFrame, gpd.read_file(county_shp))
    if county_gdf.crs is None:
        county_gdf = cast(gpd.GeoDataFrame, county_gdf.set_crs("EPSG:4326"))
    else:
        county_gdf = cast(gpd.GeoDataFrame, county_gdf.to_crs("EPSG:4326"))

    unique_df = build_unique_locations(source_df)
    unique_df = apply_location_levels(unique_df, city_gdf)
    if args.max_locations is not None:
        unique_df = unique_df.head(args.max_locations).copy()
        source_keys = set(unique_df["location_key"])
        source_df = source_df.assign(location_key=source_df.apply(location_key, axis=1))
        source_df = source_df[source_df["location_key"].isin(source_keys)].drop(columns=["location_key"])
    write_csv(unique_df, output_dir / UNIQUE_OUT)
    print(f"Wrote {UNIQUE_OUT}: {len(unique_df)} rows")

    diagnosis_df = build_location_diagnosis(unique_df, city_gdf, args.use_llm, args.llm_batch_size)
    write_csv(diagnosis_df, output_dir / LLM_OUT)
    print(f"Wrote {LLM_OUT}: {len(diagnosis_df)} rows")

    cache_path = DEFAULT_CACHE_DIR / GEOCODE_CACHE_OUT
    if args.skip_geocode:
        geocode_cache = load_geocode_cache(cache_path)
    else:
        geocode_cache = run_geocoding(unique_df, diagnosis_df, cache_path, args.geocode_sleep, args.max_geocode_requests)
    geocoded = spatial_match(geocode_cache, city_gdf, county_gdf)
    write_csv(geocoded, cache_path)
    print(f"Wrote {GEOCODE_CACHE_OUT}: {len(geocoded)} rows")

    corrections = load_corrections(DEFAULT_CACHE_DIR / CORRECTIONS_IN, DEFAULT_CACHE_DIR / MANUAL_REVIEW_XLSX_OUT)
    persist_corrections(corrections, DEFAULT_CACHE_DIR / CORRECTIONS_XLSX_IN)
    detail, review = make_detail_and_review(source_df, unique_df, diagnosis_df, geocoded, corrections, city_gdf)
    write_csv(detail, output_dir / DETAIL_OUT)
    backup_file(DEFAULT_CACHE_DIR / MANUAL_REVIEW_XLSX_OUT)
    write_review_xlsx(review, DEFAULT_CACHE_DIR / MANUAL_REVIEW_XLSX_OUT)
    legacy_review_csv = output_dir / MANUAL_REVIEW_LEGACY_CSV
    if legacy_review_csv.exists():
        legacy_review_csv.unlink()
        print(f"Deleted legacy review CSV: {legacy_review_csv.name}")
    print(f"Wrote {DETAIL_OUT}: {len(detail)} rows")
    print(f"Wrote {MANUAL_REVIEW_XLSX_OUT}: {len(review)} rows")
    if args.clean_intermediate:
        delete_files(
            [
                output_dir / UNIQUE_OUT,
                output_dir / LLM_OUT,
            ]
        )
    print("Run summarize_prefecture_events.py to build the prefecture-level summary from the detail file.")


if __name__ == "__main__":
    main()
