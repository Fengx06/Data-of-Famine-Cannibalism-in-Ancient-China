"""
读取版本差异分析结果，按处理建议规则合并为最终数据集。

功能：
- 调用 compare_versions.py 生成最新的差异分析
- 按处理建议（保留 / 需校验地区信息 / 需进一步分析）选择目标版本
- 应用十余条去重规则：
  子集合并、同地点合并、古今地名映射、空 county 处理、多地点拆分等
- 输出 result/明清时期灾荒食人年表_汇总版.csv
- 将可疑条目（含顿号、source 为空等）输出到待人工复核.csv

用法：
    python consolidate_versions.py
"""

import io
import re
import subprocess
import sys
from pathlib import Path

import pandas as pd

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")


BASE_COLUMNS = [
    "seq",
    "year_ce",
    "year_era",
    "province",
    "city",
    "county",
    "ancient_name",
    "source",
    "record",
    "note",
    "来源版本",
    "处理标记",
]

ADMIN_SUFFIXES = ("省", "府", "州", "市", "县", "区")


def is_blank(value):
    if pd.isna(value):
        return True
    return str(value).strip() == ""


def normalize_record(text):
    text = "" if pd.isna(text) else str(text).strip()
    for q in ["“", "”", "＂"]:
        text = text.replace(q, '"')
    for q in ["‘", "’", "＇"]:
        text = text.replace(q, "'")
    return text


def normalize_record_for_compare(text):
    """Build a compact record key for subset-style duplicate checks."""
    text = strip_leading_record_label(text)
    text = re.sub(r"^[0-9一二三四五六七八九十〇零]{2,4}年[，,]?", "", text)
    text = re.sub(r"^[^，,：:]+[，,：:]", "", text)
    text = re.sub(r"[（(][^）)]*[）)]", "", text)
    text = re.sub(r"[0-9一二三四五六七八九十〇零]{2,4}年", "", text)
    return re.sub(r"[\s。．.，,；;：:、\"'“”‘’＂＇]+", "", text)


def normalize_source_for_compare(text):
    text = "" if pd.isna(text) else str(text).strip()
    if not text or text == "无":
        return ""
    return re.sub(r"[\s《》〈〉\"'“”‘’＂＇，,。．.；;：:、]+", "", text)


def sources_are_compatible(source1, source2):
    src1 = normalize_source_for_compare(source1)
    src2 = normalize_source_for_compare(source2)
    if not src1 or not src2:
        return False
    return src1 in src2 or src2 in src1


def normalize_region_name(name):
    """Normalize only common administrative suffixes, avoiding loose substring matching."""
    text = "" if pd.isna(name) else str(name).strip()
    if text == "无":
        return ""
    while len(text) > 1 and text.endswith(ADMIN_SUFFIXES):
        text = text[:-1]
    return text


def natural_sort_key(value):
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", str(value))]


def split_versions(value):
    if is_blank(value):
        return []
    return [item.strip() for item in str(value).split(",") if item.strip()]


def merge_sources(values):
    sources = set()
    for value in values:
        sources.update(split_versions(value))
    return ",".join(sorted(sources, key=natural_sort_key))


def version_count(value):
    return len(split_versions(value))


def load_analysis_result(path):
    if not path.exists():
        print(f"未找到分析结果文件: {path}")
        print("请先运行 analyze_entry_versions.py 生成差异分析结果。")
        return None
    return pd.read_csv(path)


def choose_target_version(row, record_cols):
    decision = row["decision"]
    ref_versions = split_versions(row["ref"] if pd.notna(row["ref"]) else "")
    source_versions = split_versions(row["来源版本"] if pd.notna(row["来源版本"]) else "")

    if decision in ("保留", "需校验地区信息"):
        candidates = source_versions
    elif decision == "需进一步分析":
        candidates = ref_versions
    else:
        candidates = ref_versions or source_versions

    existing = []
    for version in candidates:
        record_col = f"{version}_record"
        if record_col in record_cols and not is_blank(row[record_col]):
            existing.append(version)

    if not existing:
        return ""

    if decision == "需进一步分析":
        return sorted(
            existing,
            key=lambda version: (
                -len(str(row[f"{version}_record"]).strip()),
                natural_sort_key(version),
            ),
        )[0]

    return sorted(existing, key=natural_sort_key)[0]


def build_initial_result_rows(df):
    record_cols = [c for c in df.columns if c.endswith("_record")]
    result_rows = []

    for _, row in df.iterrows():
        target_version = choose_target_version(row, record_cols)
        record_col = f"{target_version}_record"
        record = ""
        if record_col in df.columns and pd.notna(row[record_col]):
            record = str(row[record_col]).strip()

        result_rows.append(
            {
                "year_ce": row["year_ce"],
                "year_era": row["year_era"],
                "province": row["province"],
                "city": row["city"],
                "county": row["county"],
                "ancient_name": row["ancient_name"],
                "source": row["source"] if pd.notna(row["source"]) else "",
                "record": record,
                "note": row["note"] if pd.notna(row["note"]) else "",
                "来源版本": row["来源版本"] if pd.notna(row["来源版本"]) else "",
                "处理标记": row["decision"],
            }
        )

    result_df = pd.DataFrame(result_rows)
    return reset_seq(result_df)


def reset_seq(df):
    df = df.reset_index(drop=True).copy()
    df["seq"] = range(1, len(df) + 1)
    cols = [c for c in BASE_COLUMNS if c in df.columns]
    other_cols = [c for c in df.columns if c not in cols]
    return df[cols + other_cols]


def record_prefix_location(record):
    match = re.match(r"^([^：:]+)[：:]", str(record).strip())
    return match.group(1).strip() if match else ""


def strip_leading_record_label(record):
    text = normalize_record(record)
    match = re.match(r"^[^：:]{1,20}[：:](.+)$", text)
    return match.group(1).strip() if match else text


def get_record_core(record):
    text = strip_leading_record_label(record)
    text = re.sub(r"^[0-9一二三四五六七八九十〇零]{2,4}年[，,]?", "", text)
    text = re.sub(r"^[^，,：:]+[，,：:]", "", text)
    return text.strip().strip("。．.，,；; ")


def record_mentions_location(record, location):
    text = strip_leading_record_label(record)
    loc = normalize_region_name(location)
    return bool(loc) and loc in normalize_region_name(text)


def get_min_location(row):
    county = "" if is_blank(row["county"]) else str(row["county"]).strip()
    city = "" if is_blank(row["city"]) else str(row["city"]).strip()
    return normalize_region_name(county or city)


def is_multi_location_record_group(group):
    record = normalize_record(group.iloc[0]["record"])
    if "、" not in record:
        return False

    locations = []
    for _, row in group.iterrows():
        loc = get_min_location(row)
        if loc and loc not in locations:
            locations.append(loc)

    if len(locations) <= 1:
        return False

    # If the shared record explicitly enumerates multiple distinct locations
    # from this group, keep them as separate rows instead of merging by record.
    matched = [loc for loc in locations if loc in record]
    return len(matched) > 1


def records_are_related(record1, record2):
    core1 = get_record_core(record1)
    core2 = get_record_core(record2)
    if not core1 or not core2:
        return False
    if core1 == core2:
        return True
    if core1 in core2 or core2 in core1:
        return True

    compact1 = normalize_record_for_compare(record1)
    compact2 = normalize_record_for_compare(record2)
    if not compact1 or not compact2:
        return False
    return compact1 in compact2 or compact2 in compact1


def is_multi_location_cluster(group):
    locations = []
    for _, row in group.iterrows():
        loc = get_min_location(row)
        if loc and loc not in locations:
            locations.append(loc)

    if len(locations) <= 1:
        return False

    for _, row in group.iterrows():
        record = normalize_record(row["record"])
        if "、" not in record:
            continue
        matched = [loc for loc in locations if loc in record]
        if len(matched) > 1:
            return True
    return False


def row_location_supported_by_record(row):
    record = row["record"]
    tokens = []

    loc = get_min_location(row)
    if loc:
        tokens.append(loc)

    ancient_name = "" if is_blank(row.get("ancient_name", "")) else str(row.get("ancient_name", "")).strip()
    ancient_norm = normalize_region_name(ancient_name)
    if ancient_norm and ancient_norm not in tokens:
        tokens.append(ancient_norm)

    if not tokens:
        return False

    record_prefix = normalize_region_name(record_prefix_location(record))
    return any(
        (record_prefix == token) or record_mentions_location(record, token)
        for token in tokens
    )


def row_supported_tokens(row):
    record = row["record"]
    tokens = []

    county = "" if is_blank(row.get("county", "")) else str(row.get("county", "")).strip()
    county_norm = normalize_region_name(county)
    if county_norm and len(county_norm) >= 2 and record_mentions_location(record, county_norm):
        tokens.append(county_norm)

    ancient_name = "" if is_blank(row.get("ancient_name", "")) else str(row.get("ancient_name", "")).strip()
    ancient_norm = normalize_region_name(ancient_name)
    if ancient_norm and record_mentions_location(record, ancient_norm) and ancient_norm not in tokens:
        tokens.append(ancient_norm)

    return tokens


def choose_best_keep_idx(df, indices, preferred_location=None):
    preferred_location = normalize_region_name(preferred_location)

    def prefix_matches_location(idx):
        if not preferred_location:
            return False
        record_prefix = normalize_region_name(record_prefix_location(df.loc[idx, "record"]))
        return bool(record_prefix) and record_prefix == preferred_location

    return sorted(
        indices,
        key=lambda idx: (
            not prefix_matches_location(idx),
            is_blank(df.loc[idx, "city"]),
            -version_count(df.loc[idx, "来源版本"]),
            idx,
        ),
    )[0]


def merge_group_into_keep(df, group, keep_idx):
    merged_sources = merge_sources(df.loc[group.index, "来源版本"])
    df.loc[keep_idx, "来源版本"] = merged_sources
    df.loc[keep_idx, "处理标记"] = "保留"
    return [idx for idx in group.index if idx != keep_idx]


def mark_as_processed(df, keep_idx, rule_summary):
    df.loc[keep_idx, "处理标记"] = f"已处理：{rule_summary}"


def choose_record_group_keep_indices(group):
    candidates = set(group.index)

    counties = group["county"].fillna("").astype(str).str.strip()
    base_counties = counties.apply(normalize_region_name)
    if base_counties.nunique(dropna=False) == 1 and counties.nunique(dropna=False) > 1:
        prefix = record_prefix_location(group.iloc[0]["record"])
        if prefix:
            exact = set(group[counties == prefix].index)
            if exact:
                candidates &= exact

    city_empty = group["city"].apply(is_blank)
    if city_empty.any() and (~city_empty).any():
        candidates &= set(group[~city_empty].index)

    counties_after_filter = group.loc[list(candidates), "county"].fillna("").astype(str).str.strip()
    if len(candidates) > 1 and counties_after_filter.nunique(dropna=False) == 1 and counties_after_filter.iloc[0]:
        cities = group.loc[list(candidates), "city"].fillna("").astype(str).str.strip()
        if cities.ne("").all() and cities.nunique(dropna=False) > 1:
            norm_county = normalize_region_name(counties_after_filter.iloc[0])
            non_redundant = {
                idx
                for idx in candidates
                if normalize_region_name(group.loc[idx, "city"]) != norm_county
            }
            if non_redundant:
                candidates &= non_redundant

    return candidates


def deduplicate_by_record_group(result_df):
    result_df = result_df.copy()
    result_df["_record_norm"] = result_df["record"].apply(normalize_record)
    drop_indices = []

    grouped = result_df.groupby(["year_ce", "province", "_record_norm"], dropna=False)
    for _, group in grouped:
        if len(group) <= 1 or is_blank(group.iloc[0]["_record_norm"]):
            continue
        if is_multi_location_record_group(group):
            continue

        keep_candidates = choose_record_group_keep_indices(group)
        if not keep_candidates or len(keep_candidates) == len(group):
            continue

        keep_idx = choose_best_keep_idx(result_df, keep_candidates)
        drop_indices.extend(merge_group_into_keep(result_df, group, keep_idx))

    if drop_indices:
        result_df = result_df.drop(sorted(set(drop_indices)))
    return result_df.drop(columns=["_record_norm"], errors="ignore")


def record_subset_merge_location(row):
    return get_min_location(row) or normalize_region_name(row.get("ancient_name", ""))


def deduplicate_related_record_subsets(result_df):
    result_df = result_df.copy()
    drop_indices = []

    grouped = result_df.groupby(["year_ce", "province"], dropna=False)
    for _, group in grouped:
        if len(group) <= 1:
            continue

        indices = group.index.tolist()
        parent = {idx: idx for idx in indices}

        def find(idx):
            root = idx
            while parent[root] != root:
                root = parent[root]
            while parent[idx] != idx:
                next_idx = parent[idx]
                parent[idx] = root
                idx = next_idx
            return root

        def union(a, b):
            ra = find(a)
            rb = find(b)
            if ra != rb:
                parent[ra] = rb

        for i in range(len(indices)):
            for j in range(i + 1, len(indices)):
                idx_i = indices[i]
                idx_j = indices[j]
                if not sources_are_compatible(result_df.loc[idx_i, "source"], result_df.loc[idx_j, "source"]):
                    continue
                if records_are_related(result_df.loc[idx_i, "record"], result_df.loc[idx_j, "record"]):
                    union(idx_i, idx_j)

        clusters = {}
        for idx in indices:
            clusters.setdefault(find(idx), []).append(idx)

        for cluster_indices in clusters.values():
            if len(cluster_indices) <= 1:
                continue

            merge_groups = [cluster_indices]
            cluster = result_df.loc[cluster_indices]
            if is_multi_location_cluster(cluster):
                by_location = {}
                for idx in cluster_indices:
                    loc = record_subset_merge_location(result_df.loc[idx])
                    if not loc:
                        continue
                    by_location.setdefault(loc, []).append(idx)
                merge_groups = list(by_location.values())

            for merge_indices in merge_groups:
                if len(merge_indices) <= 1:
                    continue

                keep_idx = choose_best_keep_idx(result_df, merge_indices)
                merged_sources = merge_sources(result_df.loc[merge_indices, "来源版本"])
                result_df.loc[keep_idx, "来源版本"] = merged_sources
                mark_as_processed(result_df, keep_idx, "record子集合并")
                drop_indices.extend(idx for idx in merge_indices if idx != keep_idx)

    if drop_indices:
        result_df = result_df.drop(sorted(set(drop_indices)))
    return result_df


def deduplicate_empty_city_by_source(result_df):
    result_df = result_df.copy()
    drop_indices = []

    grouped = result_df.groupby(["year_ce", "province", "source"], dropna=False)
    for _, group in grouped:
        if len(group) <= 1:
            continue

        source = str(group.iloc[0]["source"]).strip()
        if not source:
            continue

        city_empty = group["city"].apply(is_blank)
        if not (city_empty.any() and (~city_empty).any()):
            continue

        record_norms = group["record"].apply(normalize_record)
        if record_norms.nunique(dropna=False) != 1:
            continue

        keep_idx = choose_best_keep_idx(result_df, group[~city_empty].index)
        drop_indices.extend(merge_group_into_keep(result_df, group, keep_idx))

    if drop_indices:
        result_df = result_df.drop(sorted(set(drop_indices)))
    return result_df


def deduplicate_exact_location_by_source(result_df):
    result_df = result_df.copy()
    drop_indices = []

    grouped = result_df.groupby(["year_ce", "province", "source"], dropna=False)
    for _, group in grouped:
        if len(group) <= 1:
            continue

        source = str(group.iloc[0]["source"]).strip()
        if not source:
            continue

        loc_to_indices = {}
        for idx, row in group.iterrows():
            loc = get_min_location(row)
            if not loc:
                continue
            record_prefix = normalize_region_name(record_prefix_location(row["record"]))
            if record_prefix and record_prefix != loc and not record_mentions_location(row["record"], loc):
                continue
            loc_to_indices.setdefault(loc, []).append(idx)

        for indices in loc_to_indices.values():
            if len(indices) <= 1:
                continue
            keep_idx = choose_best_keep_idx(result_df, indices, preferred_location=loc)
            merged_sources = merge_sources(result_df.loc[indices, "来源版本"])
            result_df.loc[keep_idx, "来源版本"] = merged_sources
            mark_as_processed(result_df, keep_idx, "同地点合并")
            drop_indices.extend(idx for idx in indices if idx != keep_idx)

    if drop_indices:
        result_df = result_df.drop(sorted(set(drop_indices)))
    return result_df


def deduplicate_same_county_city_variants(result_df):
    result_df = result_df.copy()
    drop_indices = []

    grouped = result_df.groupby(["year_ce", "province", "source"], dropna=False)
    for _, group in grouped:
        if len(group) <= 1:
            continue

        county_groups = {}
        for idx, row in group.iterrows():
            county = "" if is_blank(row["county"]) else normalize_region_name(row["county"])
            if not county:
                continue
            county_groups.setdefault(county, []).append(idx)

        for indices in county_groups.values():
            if len(indices) <= 1:
                continue

            candidate_indices = []
            for idx in indices:
                row = result_df.loc[idx]
                county = normalize_region_name(row["county"])
                record_prefix = normalize_region_name(record_prefix_location(row["record"]))
                if record_prefix and record_prefix not in ("", county):
                    continue
                candidate_indices.append(idx)

            if len(candidate_indices) <= 1:
                continue

            related = True
            for i in range(len(candidate_indices)):
                for j in range(i + 1, len(candidate_indices)):
                    idx_i = candidate_indices[i]
                    idx_j = candidate_indices[j]
                    if not records_are_related(result_df.loc[idx_i, "record"], result_df.loc[idx_j, "record"]):
                        related = False
                        break
                if not related:
                    break

            if not related:
                continue

            keep_idx = choose_best_keep_idx(result_df, candidate_indices, preferred_location=normalize_region_name(result_df.loc[candidate_indices[0], "county"]))
            merged_sources = merge_sources(result_df.loc[candidate_indices, "来源版本"])
            result_df.loc[keep_idx, "来源版本"] = merged_sources
            mark_as_processed(result_df, keep_idx, "同county异city合并")
            drop_indices.extend(idx for idx in candidate_indices if idx != keep_idx)

    if drop_indices:
        result_df = result_df.drop(sorted(set(drop_indices)))
    return result_df


def deduplicate_empty_county_variants(result_df):
    result_df = result_df.copy()
    drop_indices = []

    grouped = result_df.groupby(["year_ce", "province", "source"], dropna=False)
    for _, group in grouped:
        if len(group) <= 1:
            continue

        indices = group.index.tolist()
        parent = {idx: idx for idx in indices}

        def find(idx):
            root = idx
            while parent[root] != root:
                root = parent[root]
            while parent[idx] != idx:
                next_idx = parent[idx]
                parent[idx] = root
                idx = next_idx
            return root

        def union(a, b):
            ra = find(a)
            rb = find(b)
            if ra != rb:
                parent[ra] = rb

        for i in range(len(indices)):
            for j in range(i + 1, len(indices)):
                idx_i = indices[i]
                idx_j = indices[j]
                if records_are_related(result_df.loc[idx_i, "record"], result_df.loc[idx_j, "record"]):
                    union(idx_i, idx_j)

        clusters = {}
        for idx in indices:
            clusters.setdefault(find(idx), []).append(idx)

        for cluster_indices in clusters.values():
            if len(cluster_indices) <= 1:
                continue

            county_empty = [
                idx for idx in cluster_indices
                if is_blank(result_df.loc[idx, "county"]) and not row_supported_tokens(result_df.loc[idx])
            ]
            county_non_empty = [
                idx for idx in cluster_indices
                if not is_blank(result_df.loc[idx, "county"])
            ]

            if not county_empty or not county_non_empty:
                continue

            # Narrow rule: only remove the entry with empty county when the
            # competing entry in the same related cluster already has county info.
            keep_idx = choose_best_keep_idx(result_df, county_non_empty)
            merged_sources = merge_sources(result_df.loc[cluster_indices, "来源版本"])
            result_df.loc[keep_idx, "来源版本"] = merged_sources
            mark_as_processed(result_df, keep_idx, "删除空county条目")
            drop_indices.extend(county_empty)

    if drop_indices:
        result_df = result_df.drop(sorted(set(drop_indices)))
    return result_df


def deduplicate_related_records_by_record_location(result_df):
    result_df = result_df.copy()
    drop_indices = []

    grouped = result_df.groupby(["year_ce", "province", "source"], dropna=False)
    for _, group in grouped:
        if len(group) <= 1:
            continue

        indices = group.index.tolist()
        parent = {idx: idx for idx in indices}

        def find(idx):
            root = idx
            while parent[root] != root:
                root = parent[root]
            while parent[idx] != idx:
                next_idx = parent[idx]
                parent[idx] = root
                idx = next_idx
            return root

        def union(a, b):
            ra = find(a)
            rb = find(b)
            if ra != rb:
                parent[ra] = rb

        for i in range(len(indices)):
            for j in range(i + 1, len(indices)):
                idx_i = indices[i]
                idx_j = indices[j]
                if records_are_related(result_df.loc[idx_i, "record"], result_df.loc[idx_j, "record"]):
                    union(idx_i, idx_j)

        clusters = {}
        for idx in indices:
            clusters.setdefault(find(idx), []).append(idx)

        for cluster_indices in clusters.values():
            if len(cluster_indices) <= 1:
                continue

            cluster = result_df.loc[cluster_indices]
            if is_multi_location_cluster(cluster):
                continue
            if not cluster["record"].astype(str).str.contains("、", regex=False).any():
                continue

            supported = [idx for idx in cluster_indices if row_location_supported_by_record(result_df.loc[idx])]
            if supported and len(supported) < len(cluster_indices):
                keep_idx = choose_best_keep_idx(result_df, supported)
                merged_sources = merge_sources(result_df.loc[cluster_indices, "来源版本"])
                result_df.loc[keep_idx, "来源版本"] = merged_sources
                mark_as_processed(result_df, keep_idx, "按record提及地点保留")
                drop_indices.extend(idx for idx in cluster_indices if idx != keep_idx)
                continue

            non_empty = [idx for idx in cluster_indices if get_min_location(result_df.loc[idx])]
            if not supported and non_empty and len(non_empty) < len(cluster_indices):
                keep_idx = choose_best_keep_idx(result_df, non_empty)
                merged_sources = merge_sources(result_df.loc[cluster_indices, "来源版本"])
                result_df.loc[keep_idx, "来源版本"] = merged_sources
                mark_as_processed(result_df, keep_idx, "优先保留非空地区")
                drop_indices.extend(idx for idx in cluster_indices if idx != keep_idx)

    if drop_indices:
        result_df = result_df.drop(sorted(set(drop_indices)))
    return result_df


def deduplicate_multi_location_supported_tokens(result_df):
    result_df = result_df.copy()
    drop_indices = []

    grouped = result_df.groupby(["year_ce", "province", "source"], dropna=False)
    for _, group in grouped:
        if len(group) <= 1:
            continue
        if not group["record"].astype(str).str.contains("、", regex=False).any():
            continue

        token_to_indices = {}
        for idx, row in group.iterrows():
            for token in row_supported_tokens(row):
                token_to_indices.setdefault(token, []).append(idx)

        for indices in token_to_indices.values():
            unique_indices = sorted(set(indices))
            if len(unique_indices) <= 1:
                continue

            empty_loc_indices = [
                idx for idx in unique_indices
                if is_blank(result_df.loc[idx, "city"]) and is_blank(result_df.loc[idx, "county"])
            ]
            detailed = [idx for idx in unique_indices if idx not in empty_loc_indices]
            if detailed and empty_loc_indices:
                keep_idx = choose_best_keep_idx(result_df, detailed)
                merged_sources = merge_sources(result_df.loc[unique_indices, "来源版本"])
                result_df.loc[keep_idx, "来源版本"] = merged_sources
                mark_as_processed(result_df, keep_idx, "多地点记录删除空地区条目")
                drop_indices.extend(empty_loc_indices)

    if drop_indices:
        result_df = result_df.drop(sorted(set(drop_indices)))
    return result_df


def deduplicate_ancient_modern_mapping_pairs(result_df):
    result_df = result_df.copy()
    drop_indices = []

    grouped = result_df.groupby(["year_ce", "province", "source"], dropna=False)
    for _, group in grouped:
        if len(group) <= 1:
            continue

        indices = group.index.tolist()
        for keep_idx in indices:
            keep_row = result_df.loc[keep_idx]
            keep_city = "" if is_blank(keep_row["city"]) else str(keep_row["city"]).strip()
            keep_county = "" if is_blank(keep_row["county"]) else normalize_region_name(keep_row["county"])
            keep_ancient = "" if is_blank(keep_row["ancient_name"]) else normalize_region_name(keep_row["ancient_name"])
            if not keep_city or not keep_ancient:
                continue

            for drop_idx in indices:
                if drop_idx == keep_idx or drop_idx in drop_indices:
                    continue

                drop_row = result_df.loc[drop_idx]
                drop_city = "" if is_blank(drop_row["city"]) else str(drop_row["city"]).strip()
                drop_county = "" if is_blank(drop_row["county"]) else normalize_region_name(drop_row["county"])
                drop_ancient = "" if is_blank(drop_row["ancient_name"]) else normalize_region_name(drop_row["ancient_name"])

                if drop_city:
                    continue

                same_ancient_only = (not drop_county) and drop_ancient == keep_ancient
                county_matches_ancient = drop_county == keep_ancient and not drop_ancient
                if not (same_ancient_only or county_matches_ancient):
                    continue

                keep_record = normalize_record(keep_row["record"])
                drop_record = normalize_record(drop_row["record"])
                if not records_are_related(keep_record, drop_record):
                    continue

                if county_matches_ancient:
                    if not record_mentions_location(keep_row["record"], keep_ancient):
                        continue
                    if not record_mentions_location(drop_row["record"], drop_county):
                        continue

                merge_indices = [keep_idx, drop_idx]
                merged_sources = merge_sources(result_df.loc[merge_indices, "来源版本"])
                result_df.loc[keep_idx, "来源版本"] = merged_sources
                mark_as_processed(result_df, keep_idx, "古今地名映射保留现代定位")
                drop_indices.append(drop_idx)

    if drop_indices:
        result_df = result_df.drop(sorted(set(drop_indices)))
    return result_df


def deduplicate_prefer_modern_county(result_df):
    result_df = result_df.copy()
    drop_indices = []

    grouped = result_df.groupby(["year_ce", "province", "source"], dropna=False)
    for _, group in grouped:
        if len(group) <= 1:
            continue

        indices = group.index.tolist()
        for keep_idx in indices:
            keep_row = result_df.loc[keep_idx]
            keep_city = "" if is_blank(keep_row["city"]) else normalize_region_name(keep_row["city"])
            keep_county = "" if is_blank(keep_row["county"]) else normalize_region_name(keep_row["county"])
            keep_ancient = "" if is_blank(keep_row["ancient_name"]) else normalize_region_name(keep_row["ancient_name"])
            if not keep_city or not keep_county or not keep_ancient:
                continue
            if keep_county == keep_ancient:
                continue

            for drop_idx in indices:
                if drop_idx == keep_idx or drop_idx in drop_indices:
                    continue

                drop_row = result_df.loc[drop_idx]
                drop_city = "" if is_blank(drop_row["city"]) else normalize_region_name(drop_row["city"])
                drop_county = "" if is_blank(drop_row["county"]) else normalize_region_name(drop_row["county"])
                drop_ancient = "" if is_blank(drop_row["ancient_name"]) else normalize_region_name(drop_row["ancient_name"])

                if not drop_city or not drop_county or not drop_ancient:
                    continue
                if drop_city != keep_city:
                    continue
                if drop_ancient != keep_ancient:
                    continue
                if drop_county != drop_ancient:
                    continue

                keep_record = normalize_record(keep_row["record"])
                drop_record = normalize_record(drop_row["record"])
                if not records_are_related(keep_record, drop_record):
                    continue

                merged_sources = merge_sources(result_df.loc[[keep_idx, drop_idx], "来源版本"])
                result_df.loc[keep_idx, "来源版本"] = merged_sources
                mark_as_processed(result_df, keep_idx, "古今地名并存时保留现代定位")
                drop_indices.append(drop_idx)

    if drop_indices:
        result_df = result_df.drop(sorted(set(drop_indices)))
    return result_df


def deduplicate_modern_city_county_over_ancient(result_df):
    result_df = result_df.copy()
    drop_indices = []

    grouped = result_df.groupby(["year_ce", "province", "source"], dropna=False)
    for _, group in grouped:
        if len(group) <= 1:
            continue

        indices = group.index.tolist()
        for keep_idx in indices:
            keep_row = result_df.loc[keep_idx]
            keep_city = "" if is_blank(keep_row["city"]) else normalize_region_name(keep_row["city"])
            keep_county = "" if is_blank(keep_row["county"]) else normalize_region_name(keep_row["county"])
            keep_ancient = "" if is_blank(keep_row["ancient_name"]) else normalize_region_name(keep_row["ancient_name"])

            # Modern-style target: city is present, county is present, and ancient_name is blank.
            if not keep_city or not keep_county or keep_ancient:
                continue

            for drop_idx in indices:
                if drop_idx == keep_idx or drop_idx in drop_indices:
                    continue

                drop_row = result_df.loc[drop_idx]
                drop_city = "" if is_blank(drop_row["city"]) else normalize_region_name(drop_row["city"])
                drop_county = "" if is_blank(drop_row["county"]) else normalize_region_name(drop_row["county"])
                drop_ancient = "" if is_blank(drop_row["ancient_name"]) else normalize_region_name(drop_row["ancient_name"])

                if drop_city != keep_city:
                    continue
                if not drop_county or not drop_ancient:
                    continue
                if drop_county != drop_ancient:
                    continue

                keep_record = normalize_record(keep_row["record"])
                drop_record = normalize_record(drop_row["record"])
                if not records_are_related(keep_record, drop_record):
                    continue

                merged_sources = merge_sources(result_df.loc[[keep_idx, drop_idx], "来源版本"])
                result_df.loc[keep_idx, "来源版本"] = merged_sources
                mark_as_processed(result_df, keep_idx, "同city下保留现代county")
                drop_indices.append(drop_idx)

    if drop_indices:
        result_df = result_df.drop(sorted(set(drop_indices)))
    return result_df


def deduplicate_related_records_by_version_count(result_df):
    result_df = result_df.copy()
    drop_indices = []

    grouped = result_df.groupby(["year_ce", "province", "source"], dropna=False)
    for _, group in grouped:
        if len(group) <= 1:
            continue

        indices = group.index.tolist()
        parent = {idx: idx for idx in indices}

        def find(idx):
            root = idx
            while parent[root] != root:
                root = parent[root]
            while parent[idx] != idx:
                next_idx = parent[idx]
                parent[idx] = root
                idx = next_idx
            return root

        def union(a, b):
            ra = find(a)
            rb = find(b)
            if ra != rb:
                parent[ra] = rb

        for i in range(len(indices)):
            for j in range(i + 1, len(indices)):
                idx_i = indices[i]
                idx_j = indices[j]
                if records_are_related(result_df.loc[idx_i, "record"], result_df.loc[idx_j, "record"]):
                    union(idx_i, idx_j)

        clusters = {}
        for idx in indices:
            clusters.setdefault(find(idx), []).append(idx)

        for cluster_indices in clusters.values():
            if len(cluster_indices) <= 1:
                continue

            cluster = result_df.loc[cluster_indices]
            if is_multi_location_cluster(cluster):
                continue

             # Weak fallback rule:
             # only apply after other rules have failed to resolve a cluster,
             # and only when the remaining rows are still non-final.
            decisions = set(cluster["处理标记"].fillna("").astype(str).str.strip())
            if "保留" in decisions:
                continue

            counts = {idx: version_count(result_df.loc[idx, "来源版本"]) for idx in cluster_indices}
            max_count = max(counts.values())
            max_indices = [idx for idx, cnt in counts.items() if cnt == max_count]
            if len(max_indices) != 1:
                continue

            keep_idx = choose_best_keep_idx(result_df, max_indices)
            merged_sources = merge_sources(result_df.loc[cluster_indices, "来源版本"])
            result_df.loc[keep_idx, "来源版本"] = merged_sources
            drop_indices.extend(idx for idx in cluster_indices if idx != keep_idx)

    if drop_indices:
        result_df = result_df.drop(sorted(set(drop_indices)))
    return result_df


def fix_county_name_from_record(result_df):
    result_df = result_df.copy()
    for idx, row in result_df.iterrows():
        county = "" if is_blank(row["county"]) else str(row["county"]).strip()
        record_county = record_prefix_location(row["record"])
        if not county or not record_county:
            continue
        if normalize_region_name(county) == normalize_region_name(record_county) and county != record_county:
            result_df.loc[idx, "county"] = record_county
    return result_df


def collect_suspicious_rows(result_df):
    review_rows = []
    exclude_indices = []

    for idx, row in result_df.iterrows():
        reasons = []
        city = "" if is_blank(row["city"]) else str(row["city"]).strip()
        county = "" if is_blank(row["county"]) else str(row["county"]).strip()
        source = "" if is_blank(row["source"]) else str(row["source"]).strip()
        record = "" if is_blank(row["record"]) else str(row["record"]).strip()

        if "、" in city:
            reasons.append("city 含顿号，疑似多地并列")
        if "、" in county:
            reasons.append("county 含顿号，疑似多县并列")
        if len(county) == 1 and "、" in record:
            reasons.append("单字 county 且 record 含顿号，疑似拆分错误")
        if not source:
            reasons.append("source 为空，已跳过按 source 自动合并")

        if reasons:
            if "、" in city or "、" in county:
                exclude_indices.append(idx)
            review_row = row.to_dict()
            review_row["manual_review_reason"] = "；".join(reasons)
            review_rows.append(review_row)

    if exclude_indices:
        result_df = result_df.drop(sorted(set(exclude_indices)))
        result_df = reset_seq(result_df)

    review_df = pd.DataFrame(review_rows)
    if not review_df.empty:
        review_cols = [c for c in BASE_COLUMNS if c in review_df.columns]
        other_cols = [c for c in review_df.columns if c not in review_cols]
        review_df = review_df[review_cols + other_cols]

    return result_df, review_df


def save_outputs(result_df, review_df, base_dir):
    output_path = base_dir / "明清时期灾荒食人年表_汇总版.csv"
    review_path = base_dir / "明清时期灾荒食人年表_待人工复核.csv"

    result_df.to_csv(output_path, index=False, encoding="utf-8-sig")
    review_df.to_csv(review_path, index=False, encoding="utf-8-sig")
    return output_path, review_path


def print_summary(result_df, review_df, output_path, review_path):
    print("=== 汇总结果 ===")
    print(f"总条目数: {len(result_df)}")
    print(f"待人工复核条目数: {len(review_df)}")

    print("\n=== 处理标记分布 ===")
    for decision, cnt in result_df["处理标记"].value_counts().items():
        pct = cnt / len(result_df) * 100 if len(result_df) else 0
        print(f"  {decision}: {cnt} ({pct:.1f}%)")

    if not review_df.empty:
        print("\n=== 待人工复核原因分布 ===")
        reason_counts = review_df["manual_review_reason"].str.get_dummies(sep="；").sum().sort_values(ascending=False)
        for reason, cnt in reason_counts.items():
            print(f"  {reason}: {cnt}")

    print(f"\n结果已保存至: {output_path}")
    print(f"待人工复核清单已保存至: {review_path}")


def run_analysis_script():
    script_path = Path(__file__).with_name("analyze_entry_versions.py")
    result = subprocess.run(
        [sys.executable, str(script_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        raise RuntimeError(f"分析脚本运行失败，返回码: {result.returncode}")
    print(result.stdout, end="")


def consolidate_versions():
    """
    读取版本条目统计与差异分析.csv，按处理建议规则生成最终汇总表。

    规则：
    - 保留：所有版本一致，取自然排序后的第一个存在版本
    - 需校验地区信息：record 一致，取自然排序后的第一个存在版本
    - 需进一步分析：从 ref 候选版本中取 record 最长者，并用版本自然排序打破并列
    - 需人工处理：优先取 ref 指定版本，否则取来源版本中的第一个存在版本
    """
    base_dir = Path(__file__).parent.parent.parent / "result"
    analysis_path = base_dir / "版本条目统计与差异分析.csv"

    # 自动运行前置分析脚本，确保分析结果是最新的
    run_analysis_script()

    df = load_analysis_result(analysis_path)
    if df is None:
        return None

    result_df = build_initial_result_rows(df)
    result_df = deduplicate_by_record_group(result_df)
    result_df = deduplicate_related_record_subsets(result_df)
    result_df = deduplicate_empty_city_by_source(result_df)
    result_df = deduplicate_exact_location_by_source(result_df)
    result_df = deduplicate_same_county_city_variants(result_df)
    result_df = deduplicate_empty_county_variants(result_df)
    result_df = fix_county_name_from_record(result_df)
    result_df = deduplicate_related_records_by_record_location(result_df)
    result_df = deduplicate_multi_location_supported_tokens(result_df)
    result_df = deduplicate_ancient_modern_mapping_pairs(result_df)
    result_df = deduplicate_prefer_modern_county(result_df)
    result_df = deduplicate_modern_city_county_over_ancient(result_df)
    result_df = deduplicate_related_records_by_version_count(result_df)
    result_df = reset_seq(result_df)
    result_df, review_df = collect_suspicious_rows(result_df)

    output_path, review_path = save_outputs(result_df, review_df, base_dir)
    print_summary(result_df, review_df, output_path, review_path)
    return result_df


if __name__ == "__main__":
    consolidate_versions()
