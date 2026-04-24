import pandas as pd
from pathlib import Path
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')


def analyze_entry_by_region():
    """
    按条目（year_ce + province + city + county）分析各版本的差异情况。

    输出每行一个唯一条目，展示该条目在各版本中的存在情况及 record 内容差异，
    并给出处理建议（保留 / 需校验地区信息 / 需进一步分析 / 需人工处理）。
    """
    base_dir = Path(__file__).parent.parent.parent / "result"
    history_dir = base_dir / "历史版本"

    csv_files = sorted(history_dir.glob("*.csv"))
    if not csv_files:
        print(f"未在 {history_dir} 中找到 CSV 文件")
        return

    import re

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
        print(f"{name}: {len(dfs[name])} 条")

    id_cols = ["year_ce", "province", "city", "county"]
    total_versions = len(dfs)
    version_names = sorted(dfs.keys())

    # 收集所有记录并构建全局标识键
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

            all_records.append({
                "key": key,
                "version": name,
                "year_ce": year,
                "record": str(row.get("record", "")).strip(),
                "year_era": str(row.get("year_era", "")).strip(),
                "ancient_name": str(row.get("ancient_name", "")).strip(),
                "source": str(row.get("source", "")).strip(),
                "note": str(row.get("note", "")).strip(),
            })

    all_df = pd.DataFrame(all_records)

    # 按标识键分组，收集各版本的记录
    result_rows = []

    for key, group in all_df.groupby("key"):
        parts = key.split("|")
        year_ce = parts[0] if len(parts) > 0 else ""
        province = parts[1] if len(parts) > 1 else ""
        city = parts[2] if len(parts) > 2 else ""
        county = parts[3] if len(parts) > 3 else ""

        # 各版本是否有该条目、record 内容
        version_records = {}
        for v in version_names:
            v_rows = group[group["version"] == v]
            if len(v_rows) > 0:
                version_records[v] = v_rows.iloc[0]["record"]
            else:
                version_records[v] = ""

        sources = [v for v in version_names if version_records[v] != ""]
        source_str = ",".join(sources)
        exists_count = len(sources)

        # 取信息最完整的记录作为基准
        best_idx = group["record"].str.len().idxmax()
        best_row = group.loc[best_idx]

        # 判断内容一致性：存在的版本中 record 是否完全相同
        existing_records = [version_records[v] for v in sources]
        content_consistent = len(set(existing_records)) == 1 if existing_records else True

        # 计算存在相似度 = 有该条目的版本数 / 总版本数
        exist_sim = exists_count / total_versions if total_versions else 0

        # 生成差异说明
        if exists_count == total_versions:
            if content_consistent:
                diff_note = ""
            else:
                diff_note = "各版本均有该条目，但 record 内容存在差异"
        else:
            missing = [v for v in version_names if v not in sources]
            if exists_count == 1:
                diff_note = f"仅存在于 {source_str}"
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
            decision = "需人工处理"
        else:
            # 部分版本存在
            if content_consistent:
                decision = "需校验地区信息"
            else:
                decision = "需进一步分析"

        # ref：取 record 最长的版本作为参考（或内容一致时的所有版本）
        if content_consistent:
            ref = source_str
        else:
            max_len = max(len(version_records[v]) for v in sources)
            ref_versions = [v for v in sources if len(version_records[v]) == max_len]
            ref = ",".join(ref_versions)

        row_data = {
            "year_ce": year_ce,
            "province": province,
            "city": city,
            "county": county,
            "year_era": best_row["year_era"],
            "ancient_name": best_row["ancient_name"],
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
    front_cols = ["year_ce", "province", "city", "county", "year_era",
                  "ancient_name", "decision", "ref", "来源版本", "存在版本数",
                  "存在相似度", "内容一致", "差异说明"]
    record_cols = [f"{v}_record" for v in version_names]
    cols = front_cols + record_cols
    result_df = result_df[cols]

    # 保存结果
    output_path = base_dir / "版本条目统计与差异分析.csv"
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


if __name__ == "__main__":
    analyze_entry_by_region()
