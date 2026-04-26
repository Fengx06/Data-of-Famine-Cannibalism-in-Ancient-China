"""
版本差异分析与合并预处理脚本。

功能：
1. 读取 processing/record_level_cleaning/ming_qing_famine_cannibalism_chen_ling/versions/ 目录下所有 CSV（v1, v2, ...），按条目比对差异。
2. analyze_entry_by_region()：按 (year_ce + province + city + county + source + record)
   聚类分析各版本条目的存在情况和内容一致性，输出处理建议：
   - 保留：所有版本一致或内容兼容
   - 需校验地区信息：同年同省同来源，地区层级存在泛指与具体并存，或地名异写
   - 需进一步分析：record 内容存在差异，需人工确认
   结果保存至 processing/merged_cleaned_data/ming_qing_famine_cannibalism_chen_ling/明清时期灾荒食人年表_版本差异分析.csv。
3. analyze_version_statistics()：统计每个版本的总条目数和总体相似度，
   直接打印到屏幕，不生成文件。

调用关系：
- 本脚本由 build_consolidated_dataset.py 通过 subprocess 自动调用，作为合并流程的前置步骤。
- 也可独立运行：python analyze_version_differences.py
"""

import pandas as pd
from pathlib import Path
import sys
import io
import re

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')


def analyze_entry_by_region():
    """
    按条目（year_ce + province + city + county）分析各版本的差异情况。

    输出每行一个唯一条目，展示该条目在各版本中的存在情况及 record 内容差异，
    并给出处理建议（保留 / 需校验地区信息 / 需进一步分析 / 需人工处理）。
    """
    base_dir = Path(__file__).parent.parent.parent / "processing" / "merged_cleaned_data" / "ming_qing_famine_cannibalism_chen_ling"
    history_dir = Path(__file__).parent.parent.parent / "processing" / "record_level_cleaning" / "ming_qing_famine_cannibalism_chen_ling" / "versions"

    csv_files = sorted(history_dir.glob("*.csv"))
    if not csv_files:
        print(f"未在 {history_dir} 中找到 CSV 文件")
        return

    def get_version_name(path: Path) -> str:
        stem = path.stem
        m = re.search(r"_v\d+", stem)
        if m:
            return m.group(0)[1:]
        return stem

    def normalize_source_text(text):
        if pd.isna(text):
            return ""
        text = str(text).strip()
        if not text or text == "无":
            return ""
        return text.replace(" ", "")

    def normalize_record_text(text):
        if pd.isna(text):
            return ""
        text = str(text).strip()
        for q in ['"', "“", "”", "＂", "‘", "’", "＇", "'"]:
            text = text.replace(q, "")
        return text

    def strip_leading_label(text):
        text = normalize_record_text(text)
        match = re.match(r"^[^：:]{1,20}[：:](.+)$", text)
        return match.group(1).strip() if match else text

    def normalize_record_for_compare(text):
        text = strip_leading_label(text)
        text = re.sub(r"^[0-9一二三四五六七八九十〇零]{2,4}年[，,]?", "", text)
        text = re.sub(r"^[^，,：:]+[，,：:]", "", text)
        return re.sub(r"[\s。．.，,；;：:、\"'“”‘’＂＇]+", "", text)

    def record_has_multi_location(text):
        text = normalize_record_text(text)
        return "、" in text

    def record_core_key(text):
        text = strip_leading_label(text)
        text = re.sub(r"^[0-9一二三四五六七八九十〇零]{2,4}年[，,]?", "", text)
        text = re.sub(r"^[^，,：:]+[，,：:]", "", text)
        return text.strip().strip("。．.，,；; ")

    def sources_are_compatible(source_texts):
        normalized = [normalize_source_text(text) for text in source_texts if normalize_source_text(text)]
        if not normalized:
            return True
        if len(set(normalized)) == 1:
            return True
        longest = max(normalized, key=len)
        return all(text in longest for text in normalized)

    dfs = {}
    for path in csv_files:
        name = get_version_name(path)
        dfs[name] = pd.read_csv(path)

    id_cols = ["year_ce", "province", "city", "county"]
    total_versions = len(dfs)
    version_names = sorted(dfs.keys())

    def normalize_region_name(text):
        """去除地名末尾的行政级别后缀，用于统一匹配。"""
        if pd.isna(text):
            return ""
        s = str(text).strip()
        if s in ["", "无"]:
            return ""
        # 依次去除末尾的行政后缀
        for suffix in ["省", "市", "县", "区", "州"]:
            if s.endswith(suffix):
                s = s[:-1]
        return s

    def info_priority(info, ancient_name):
        city = "" if pd.isna(info.get("city", "")) else str(info.get("city", "")).strip()
        county = "" if pd.isna(info.get("county", "")) else str(info.get("county", "")).strip()
        ancient = "" if pd.isna(ancient_name) else str(ancient_name).strip()

        county_norm = normalize_region_name(county)
        city_norm = normalize_region_name(city)
        ancient_norm = normalize_region_name(ancient)

        # Prefer modern-style localization:
        # 1) county exists and differs from ancient_name
        # 2) county equals city (typical modern city-level fallback)
        # 3) city exists
        # 4) any county exists
        modern_county = bool(county_norm) and county_norm != ancient_norm
        county_same_as_city = bool(county_norm) and county_norm == city_norm
        return (
            modern_county,
            county_same_as_city,
            bool(city_norm),
            bool(county_norm),
        )

    def multi_location_token(row):
        county = normalize_region_name(row.get("county", ""))
        ancient = normalize_region_name(row.get("ancient_name", ""))
        city = normalize_region_name(row.get("city", ""))
        return county or ancient or city

    # 收集所有记录并构建全局标识键。
    # 区域字段来自 LLM，波动较大；主分组使用 year + source + record 核心。
    # 若 record 明确是多地点材料，则追加地点 token，避免多个地点被压成一个代表项。
    all_records = []
    for name, df in dfs.items():
        for _, row in df.iterrows():
            # 原始值（用于输出展示）
            raw_parts = []
            for col in id_cols:
                val = row.get(col, "")
                if pd.isna(val) or str(val).strip() in ["", "无"]:
                    raw_parts.append("")
                else:
                    raw_parts.append(str(val).strip())

            year_val = row.get("year_ce", "")
            try:
                year = int(float(year_val))
            except (ValueError, TypeError):
                year = None

            source_norm = normalize_source_text(row.get("source", ""))
            record_text = str(row.get("record", "")).strip()
            record_core = record_core_key(record_text)
            multi_location = record_has_multi_location(record_text)

            key_parts = [str(year) if year is not None else "", source_norm, record_core]
            if multi_location:
                key_parts.append(multi_location_token(row))
            key = "|".join(key_parts)

            all_records.append({
                "key": key,
                "version": name,
                "year_ce": year,
                "record": str(row.get("record", "")).strip(),
                "year_era": str(row.get("year_era", "")).strip(),
                "ancient_name": str(row.get("ancient_name", "")).strip(),
                "source": str(row.get("source", "")).strip(),
                "note": str(row.get("note", "")).strip(),
                "raw_province": raw_parts[1],
                "raw_city": raw_parts[2],
                "raw_county": raw_parts[3],
                "record_core": record_core,
                "is_multi_location": multi_location,
            })

    all_df = pd.DataFrame(all_records)

    # 按标识键分组，收集各版本的记录
    # 注意：一个版本可能对同一个规范化键有多条记录，取 record 最长的那条
    result_rows = []

    for key, group in all_df.groupby("key"):
        parts = str(key).split("|")
        year_ce = parts[0] if len(parts) > 0 else ""
        province_norm = ""
        city_norm = ""
        county_norm = ""

        # 各版本是否有该条目、record 内容（一个版本多条记录时取最长的）
        version_records = {}
        version_raw_info = {}  # 记录每个版本使用的原始地区字段
        version_sources = {}  # 记录每个版本的 source
        for v in version_names:
            v_rows = group[group["version"] == v]
            if len(v_rows) > 0:
                # 取 record 最长的那条
                best_idx = v_rows["record"].str.len().idxmax()
                best = v_rows.loc[best_idx]
                version_records[v] = best["record"]
                version_sources[v] = best["source"]
                version_raw_info[v] = {
                    "province": best["raw_province"],
                    "city": best["raw_city"],
                    "county": best["raw_county"],
                }
            else:
                version_records[v] = ""
                version_sources[v] = ""
                version_raw_info[v] = {"province": "", "city": "", "county": ""}

        sources = [v for v in version_names if version_records[v] != ""]
        source_str = ",".join(sources)
        exists_count = len(sources)

        # 取信息最完整的记录作为基准（展示用的原始地区字段）
        best_idx = group["record"].str.len().idxmax()
        best_row = group.loc[best_idx]

        # 合并展示字段时，不再简单取第一个非空值；
        # 优先采用更像“现代定位”的地区信息。
        merged_province = ""
        for v in sources:
            info = version_raw_info[v]
            if info["province"]:
                merged_province = info["province"]
                break

        best_info_version = None
        best_info_priority = None
        for v in sources:
            info = version_raw_info[v]
            priority = info_priority(info, best_row["ancient_name"])
            if best_info_priority is None or priority > best_info_priority:
                best_info_priority = priority
                best_info_version = v

        merged_city = ""
        merged_county = ""
        if best_info_version is not None:
            merged_city = version_raw_info[best_info_version]["city"]
            merged_county = version_raw_info[best_info_version]["county"]
        province_norm = normalize_region_name(merged_province)
        city_norm = normalize_region_name(merged_city)
        county_norm = normalize_region_name(merged_county)

        # 计算存在相似度（提前计算，避免循环中残留上一个 group 的值）
        exist_sim = exists_count / total_versions if total_versions else 0

        # 判断内容一致性：存在的版本中 record 是否完全相同，或 source 来源链是否兼容
        existing_records = [version_records[v] for v in sources]
        existing_sources = [version_sources[v] for v in sources]
        content_consistent = len(set(existing_records)) == 1 if existing_records else True
        # 如果 record 不完全相同，但 source 彼此相同或都可视为某一更长 source 的子集，也认为一致
        if not content_consistent and sources_are_compatible(existing_sources):
            content_consistent = True

        # 如果绝大部分版本都有但 record 不完全一致，尝试去掉地区前缀后再比较
        if not content_consistent and exist_sim >= 0.8:
            prefixes = set()
            if province_norm:
                prefixes.add(province_norm)
                prefixes.add(province_norm + "省")
            # 从原始地区字段提取前缀（避免 normalize 后层级错位）
            for v in sources:
                info = version_raw_info[v]
                for field, suffix in [("province", "省"), ("city", "市"), ("county", "县")]:
                    val = info.get(field, "")
                    if val and val not in ["", "无"]:
                        prefixes.add(val)
                        if not val.endswith(suffix):
                            prefixes.add(val + suffix)

            # 增加 province+city / province+county 复合前缀（用原始值避免 normalize 后丢失）
            if province_norm:
                raw_cities = set(info["city"] for info in version_raw_info.values() if info.get("city"))
                raw_counties = set(info["county"] for info in version_raw_info.values() if info.get("county"))
                for rc in raw_cities:
                    if rc and rc not in ["", "无"]:
                        prefixes.add(province_norm + rc)
                for rc in raw_counties:
                    if rc and rc not in ["", "无"]:
                        prefixes.add(province_norm + rc)

            def strip_region_prefix(rec):
                changed = True
                while changed:
                    changed = False
                    for p in sorted(prefixes, key=len, reverse=True):
                        for sep in ["：", ":"]:
                            if rec.startswith(p + sep):
                                rec = rec[len(p + sep):]
                                changed = True
                                break
                        if changed:
                            break
                return rec

            normalized_records = [strip_region_prefix(r) for r in existing_records]
            if len(set(normalized_records)) == 1:
                content_consistent = True

        # 对于2个及以上版本存在的记录，检查是否为截断子串关系
        if not content_consistent and exists_count >= 2:
            for candidate in existing_records:
                if all(r in candidate for r in existing_records):
                    content_consistent = True
                    break

        # 生成差异说明
        if exists_count == total_versions:
            if content_consistent:
                diff_note = ""
            else:
                diff_note = "各版本均有该条目，但 record 内容存在差异"
        else:
            missing = [v for v in version_names if v not in sources]
            if exists_count == 1:
                diff_note = ""
            elif content_consistent:
                diff_note = ""
            else:
                diff_note = f"存在于：{source_str}；缺少：{','.join(missing)}"
            if not content_consistent:
                diff_note += "；且 record 内容不一致"

        # 生成处理建议
        if exists_count == total_versions:
            if content_consistent:
                decision = "保留"
            else:
                decision = "需进一步分析"
        elif exists_count == 1:
            decision = "保留"
            diff_note = ""
        else:
            # 部分版本存在：只要存在的版本中内容一致即保留
            if content_consistent:
                decision = "保留"
            else:
                decision = "需进一步分析"

        # ref：取 record 最长的版本作为参考（或内容一致时的所有版本）
        if content_consistent:
            ref = source_str
        else:
            max_len = max(len(version_records[v]) for v in sources)
            ref_versions = [v for v in sources if len(version_records[v]) == max_len]
            ref = ",".join(ref_versions)

        # 构建匹配键（展示用）
        match_key = "|".join(filter(None, [year_ce, province_norm, city_norm, county_norm]))

        row_data = {
            "year_ce": year_ce,
            "province": merged_province,
            "city": merged_city,
            "county": merged_county,
            "match_key": match_key,
            "year_era": best_row["year_era"],
            "ancient_name": best_row["ancient_name"],
            "source": best_row["source"],
            "note": best_row["note"],
            "decision": decision,
            "ref": ref,
            "来源版本": source_str,
            "存在版本数": exists_count,
            "存在相似度": round(exist_sim, 4),
            "内容一致": "是" if content_consistent else "否",
            "差异说明": diff_note,
        }

        # 追加各版本的 record 列
        for v in version_names:
            row_data[f"{v}_record"] = version_records[v]

        result_rows.append(row_data)

    result_df = pd.DataFrame(result_rows)

    # 调整列顺序
    front_cols = ["year_ce", "province", "city", "county", "match_key", "year_era",
                  "ancient_name", "source", "note", "decision", "ref", "来源版本",
                  "存在版本数", "存在相似度", "内容一致", "差异说明"]
    record_cols = [f"{v}_record" for v in version_names]
    cols = front_cols + record_cols
    result_df = result_df[cols]

    # ========================================================================
    # Phase 2: 按 year_ce + province + record_similarity 重新聚类
    # 分母 = 有该 record 的版本数（不是 total_versions）
    # ========================================================================
    def get_best_record(row):
        records = []
        for v in version_names:
            col = f"{v}_record"
            if col in row.index and pd.notna(row[col]):
                val = str(row[col]).strip()
                if val and val.lower() != "nan":
                    records.append(val)
        return max(records, key=len) if records else ""

    def records_are_related(rec1, rec2):
        if not rec1 or not rec2:
            return rec1 == rec2
        if rec1 == rec2:
            return True
        if rec1 in rec2 or rec2 in rec1:
            return True

        compact1 = normalize_record_for_compare(rec1)
        compact2 = normalize_record_for_compare(rec2)
        if not compact1 or not compact2:
            return False
        return compact1 in compact2 or compact2 in compact1

    def strip_record_prefix(record, row):
        record = "" if pd.isna(record) else str(record).strip()
        if not record:
            return ""

        prefixes = set()
        for field in ["province", "city", "county"]:
            value = row.get(field, "")
            if pd.isna(value):
                continue
            value = str(value).strip()
            if value and value not in ["无"]:
                prefixes.add(value)

        changed = True
        while changed:
            changed = False
            for prefix in sorted(prefixes, key=len, reverse=True):
                for sep in ["：", ":"]:
                    token = prefix + sep
                    if record.startswith(token):
                        record = record[len(token):].strip()
                        changed = True
                        break
                if changed:
                    break
        return record

    def _strip_leading_label(record):
        record = "" if pd.isna(record) else str(record).strip()
        if not record:
            return ""
        match = re.match(r"^[^：:]{1,12}[：:](.+)$", record)
        if match:
            return match.group(1).strip()
        return record

    def get_record_core(row):
        best_record = get_best_record(row)
        stripped = strip_record_prefix(best_record, row)
        if stripped != best_record:
            return stripped
        return _strip_leading_label(best_record)

    def min_location_parts(row):
        parts = []
        for field in ["city", "county"]:
            value = row.get(field, "")
            if pd.isna(value):
                continue
            value = str(value).strip()
            if value and value not in ["无"]:
                parts.append(value)
        return tuple(parts)

    def normalize_location_token(text):
        if pd.isna(text):
            return ""
        text = str(text).strip()
        if not text or text == "无":
            return ""
        for suffix in ["省", "府", "州", "市", "县", "区"]:
            if text.endswith(suffix) and len(text) > 1:
                text = text[:-1]
        return text

    def location_variants(row):
        variants = []
        for field in ["city", "county"]:
            value = normalize_location_token(row.get(field, ""))
            if value and value not in variants:
                variants.append(value)
        return variants

    def char_distance_one(a, b):
        if not a or not b or len(a) != len(b):
            return False
        return sum(ch1 != ch2 for ch1, ch2 in zip(a, b)) == 1

    def are_similar_location_names(name1, name2):
        if not name1 or not name2:
            return False
        if name1 == name2:
            return True
        if name1 in name2 or name2 in name1:
            return True
        return char_distance_one(name1, name2)

    def has_location_variant_ambiguity(group):
        if len(group) <= 1:
            return False

        normalized_records = []
        all_locations = []
        sources = set()

        for _, row in group.iterrows():
            normalized_record = get_record_core(row)
            if not normalized_record:
                return False
            normalized_records.append(normalized_record)

            variants = location_variants(row)
            if not variants:
                return False
            all_locations.append(variants)

            source = str(row.get("source", "")).strip()
            if source:
                sources.add(source)

        if len(sources) != 1 or len(set(normalized_records)) != 1:
            return False

        flat_locations = []
        for variants in all_locations:
            for name in variants:
                if name not in flat_locations:
                    flat_locations.append(name)

        if len(flat_locations) <= 1:
            return False

        for i in range(len(flat_locations)):
            for j in range(i + 1, len(flat_locations)):
                if are_similar_location_names(flat_locations[i], flat_locations[j]):
                    return True

        return False

    def is_region_ambiguity_group(group):
        if len(group) <= 1:
            return False

        specificity_counts = []
        normalized_records = []
        sources = set()

        for _, row in group.iterrows():
            parts = min_location_parts(row)
            specificity_counts.append(len(parts))
            normalized_records.append(strip_record_prefix(get_best_record(row), row))
            source = str(row.get("source", "")).strip()
            if source:
                sources.add(source)

        if not sources or len(sources) != 1:
            return False

        if not (min(specificity_counts) == 0 and max(specificity_counts) > 0):
            return False

        non_empty_records = [rec for rec in normalized_records if rec]
        if len(non_empty_records) < 2:
            return False

        longest = max(non_empty_records, key=len)
        return all(rec in longest for rec in non_empty_records)

    # 按 year_ce + province 分组聚类
    for (year_val, prov_val), prov_group in result_df.groupby(["year_ce", "province"]):
        if len(prov_group) <= 1:
            continue

        indices = prov_group.index.tolist()
        n = len(indices)
        parent = list(range(n))

        def find(x):
            if parent[x] != x:
                parent[x] = find(parent[x])
            return parent[x]

        def union(x, y):
            px, py = find(x), find(y)
            if px != py:
                parent[px] = py

        # 获取每个条目的最佳 record
        best_records = {idx: get_best_record(prov_group.loc[idx]) for idx in indices}

        # 比较每对 record，子集关系则合并
        for i in range(n):
            for j in range(i + 1, n):
                if records_are_related(best_records[indices[i]], best_records[indices[j]]):
                    union(i, j)

        # 按连通分量分组
        clusters = {}
        for i in range(n):
            root = find(i)
            clusters.setdefault(root, []).append(indices[i])

        # 对每个聚类计算版本占比
        for cluster_indices in clusters.values():
            # 收集该聚类下所有条目的来源版本（并集）
            all_sources = set()
            for idx in cluster_indices:
                src = str(result_df.loc[idx, "来源版本"])
                all_sources.update(s.strip() for s in src.split(",") if s.strip())

            total_relevant = len(all_sources)
            if total_relevant == 0:
                continue

            # 如果该条目所有来源版本都有相同的 source，直接标记为保留，不做占比判断
            source_handled = set()  # 跟踪已由 source check 处理的索引
            for idx in cluster_indices:
                item_source = str(result_df.loc[idx, "source"])
                # 检查该条目所有来源版本是否都有相同的 source
                src = str(result_df.loc[idx, "来源版本"])
                item_versions = [s.strip() for s in src.split(",") if s.strip()]
                all_same_source = True
                for v in item_versions:
                    col = f"{v}_record"
                    if col in result_df.columns and pd.notna(result_df.loc[idx, col]):
                        rec = str(result_df.loc[idx, col])
                        matches = re.findall(r'《([^》]+)》', rec)
                        # 去掉《》后比较
                        normalized_item = item_source.replace("《", "").replace("》", "")
                        if normalized_item not in matches:
                            all_same_source = False
                            break
                if all_same_source:
                    result_df.loc[idx, "decision"] = "保留"
                    result_df.loc[idx, "差异说明"] = ""
                    result_df.loc[idx, "内容一致"] = "是"
                    source_handled.add(idx)

            # 单条目聚类跳过比例判断
            if len(cluster_indices) <= 1:
                continue

            if total_relevant == 0:
                continue

            # 更新每个条目的 decision（跳过已由 source check 处理的）
            for idx in cluster_indices:
                if idx in source_handled:
                    continue
                src = str(result_df.loc[idx, "来源版本"])
                item_sources = set(s.strip() for s in src.split(",") if s.strip())
                ratio = len(item_sources) / total_relevant

                result_df.loc[idx, "存在版本数"] = len(item_sources)
                result_df.loc[idx, "存在相似度"] = round(ratio, 4)

                if ratio >= 0.8:
                    result_df.loc[idx, "decision"] = "保留"
                    result_df.loc[idx, "差异说明"] = ""
                    result_df.loc[idx, "内容一致"] = "是"
                else:
                    result_df.loc[idx, "decision"] = "需进一步分析"
                    missing = sorted(all_sources - item_sources)
                    result_df.loc[idx, "差异说明"] = (
                        f"同类 record 存在于 {','.join(sorted(all_sources))}；"
                        f"本条仅存在于 {','.join(sorted(item_sources))}；占比 {ratio:.1%}"
                    )
                    result_df.loc[idx, "内容一致"] = "否"

    # 对“同年同省同来源、正文同一事件，但地区层级一个泛指一个具体”的并存条目，
    # 保留两条记录，但将处理标记改为“需校验地区信息”。
    for _, source_group in result_df.groupby(["year_ce", "province", "source"], dropna=False):
        if is_region_ambiguity_group(source_group):
            indices = source_group.index.tolist()
            result_df.loc[indices, "decision"] = "需校验地区信息"
            result_df.loc[indices, "内容一致"] = "是"
            result_df.loc[indices, "差异说明"] = "同年同省同来源记录可对应同一事件，但地区层级存在泛指与具体定位并存，需校验地区信息"

    # 对“同年同省同来源同 record，但地区名仅有近似异写/明显异体”的情况，
    # 保留相关记录，但将处理标记改为“需校验地区信息”。
    result_df["_record_core"] = result_df.apply(
        get_record_core,
        axis=1
    )
    for _, variant_group in result_df.groupby(
        ["year_ce", "province", "source", "_record_core"],
        dropna=False
    ):
        if has_location_variant_ambiguity(variant_group):
            indices = variant_group.index.tolist()
            result_df.loc[indices, "decision"] = "需校验地区信息"
            result_df.loc[indices, "内容一致"] = "是"
            result_df.loc[indices, "差异说明"] = "同年同省同来源同 record，但地区名存在近似异写或明显异体，需校验地区信息"
    result_df = result_df.drop(columns=["_record_core"], errors="ignore")

    # 保存结果
    output_path = base_dir / "明清时期灾荒食人年表_版本差异分析.csv"
    result_df.to_csv(output_path, index=False, encoding="utf-8-sig")

    # 输出统计信息
    print(f"\n=== 条目级别差异分析结果 ===")
    print(f"总条目数（按 year_ce+province+city+county 去重后）: {len(result_df)}")
    print(f"原始条目总数: {len(all_df)}")

    print(f"\n=== 处理建议分布 ===")
    decision_counts = result_df["decision"].value_counts()
    for decision, cnt in decision_counts.items():
        pct = cnt / len(result_df) * 100
        print(f"  {decision}: {cnt} ({pct:.1f}%)")

    print(f"\n=== 存在版本数分布 ===")
    exist_counts = result_df["存在版本数"].value_counts().sort_index(ascending=False)
    for exist_cnt, row_cnt in exist_counts.items():
        pct = row_cnt / len(result_df) * 100
        print(f"  {exist_cnt} 个版本存在: {row_cnt} ({pct:.1f}%)")

    print(f"\n结果已保存至: {output_path}")
    return result_df


def analyze_version_statistics():
    """
    分析每个版本的总体统计情况，只打印版本级汇总信息，不输出年份级明细。

    相似情况定义：对于一条记录，相似情况 = 该记录出现的版本数 / 该年份有记录的版本数
    """
    base_dir = Path(__file__).parent.parent.parent / "processing" / "merged_cleaned_data" / "ming_qing_famine_cannibalism_chen_ling"
    history_dir = Path(__file__).parent.parent.parent / "processing" / "record_level_cleaning" / "ming_qing_famine_cannibalism_chen_ling" / "versions"

    csv_files = sorted(history_dir.glob("*.csv"))
    if not csv_files:
        print(f"未在 {history_dir} 中找到 CSV 文件")
        return

    def get_version_name(path: Path) -> str:
        stem = path.stem
        m = re.search(r"_v\d+", stem)
        if m:
            return m.group(0)[1:]
        return stem

    dfs = {}
    for path in csv_files:
        name = get_version_name(path)
        dfs[name] = pd.read_csv(path)

    id_cols = ["year_ce", "province", "city", "county"]
    version_names = sorted(dfs.keys())

    all_records = []
    for name, df in dfs.items():
        for _, row in df.iterrows():
            parts = []
            for col in id_cols:
                val = row.get(col, "")
                if pd.isna(val) or str(val).strip() in ["", "无"]:
                    parts.append("")
                else:
                    parts.append(str(val).strip())
            key = "|".join(parts)
            year_val = row.get("year_ce", "")
            try:
                year = int(float(year_val))
            except (ValueError, TypeError):
                year = None
            all_records.append({"key": key, "version": name, "year_ce": year})

    all_df = pd.DataFrame(all_records)

    year_versions = {}
    for v in version_names:
        v_df = all_df[all_df["version"] == v]
        valid = v_df[v_df["year_ce"].notna()]
        for year in valid["year_ce"].unique():
            y = int(year)
            year_versions.setdefault(y, set()).add(v)

    key_version_count = all_df.groupby("key")["version"].nunique().to_dict()

    def calc_similarity(row):
        rec_versions = key_version_count.get(row["key"], 0)
        year_total = len(year_versions.get(row["year_ce"], set()))
        return rec_versions / year_total if year_total else 0

    all_df["similarity"] = all_df.apply(calc_similarity, axis=1)

    stats = {}
    for v in version_names:
        v_df = all_df[all_df["version"] == v]
        valid = v_df[v_df["year_ce"].notna()]
        stats[v] = {}
        for year, group in valid.groupby("year_ce"):
            stats[v][int(year)] = {
                "cnt": len(group),
                "sim": round(group["similarity"].mean(), 4),
            }
        stats[v]["__overall__"] = round(v_df["similarity"].mean(), 4)

    all_years = set()
    for v in version_names:
        all_years.update(k for k in stats[v] if isinstance(k, int))
    all_years = sorted(all_years)

    print(f"\n=== 版本统计 ===")
    for v in version_names:
        total_records = sum(stats[v][y]["cnt"] for y in all_years if y in stats[v])
        overall_sim = stats[v].get("__overall__", "")
        print(f"{v}: {total_records} 条, 相似度 {overall_sim}")


if __name__ == "__main__":
    analyze_entry_by_region()
    analyze_version_statistics()
