import pandas as pd
from pathlib import Path
import sys
import io

# 设置输出编码为 UTF-8，解决 Windows 终端中文乱码
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

def merge_famine_versions():
    """
    合并三个版本的 CSV 文件，按 (年份+地区) 取并集，并标注版本差异。
    同一事件（相同 year_ce+province+city+county）只保留一条记录，
    但会标注该事件存在于哪些版本，以及各版本间是否存在内容差异。
    """
    base_dir = Path(__file__).parent.parent.parent / "result"
    history_dir = base_dir / "历史版本"

    # 自动扫描 历史版本 目录下所有 csv 文件
    csv_files = sorted(history_dir.glob("*.csv"))
    if not csv_files:
        print(f"未在 {history_dir} 中找到 CSV 文件")
        return

    # 文件名 -> 版本名 映射表，未在表中的文件将尝试自动提取版本标识
    name_map = {
        # 示例："明清时期灾荒食人年表_v1": "v1",
    }

    import re

    def get_version_name(path: Path) -> str:
        stem = path.stem
        if stem in name_map:
            return name_map[stem]
        # 尝试提取 _v数字 模式
        m = re.search(r"_v\d+", stem)
        if m:
            return m.group(0)[1:]  # 去掉开头的下划线
        return stem

    dfs = {}
    for path in csv_files:
        name = get_version_name(path)
        dfs[name] = pd.read_csv(path)
        print(f"{name}: {len(dfs[name])} 条")

    # 定义标识键（用于判断是否为同一事件）
    id_cols = ["year_ce", "province", "city", "county"]

    # 为每个版本添加来源标记
    for name, df in dfs.items():
        df["_source"] = name

    # 合并所有数据
    all_df = pd.concat(dfs.values(), ignore_index=True)

    # 构建标识键（处理"无"值和空值）
    def make_key(row):
        parts = []
        for col in id_cols:
            val = row.get(col, "")
            if pd.isna(val) or str(val).strip() in ["", "无"]:
                parts.append("")
            else:
                parts.append(str(val).strip())
        return "|".join(parts)

    all_df["_key"] = all_df.apply(make_key, axis=1)

    result_rows = []

    for key, group in all_df.groupby("_key"):
        # 该标识键存在于哪些版本
        sources = sorted(set(group["_source"].tolist()))
        source_str = ",".join(sources)

        # 选择保留的记录：取记录内容最长的那条（信息最完整）
        group["_record_len"] = group["record"].astype(str).str.len()
        best_idx = group["_record_len"].idxmax()
        keep_row = group.loc[best_idx].copy()

        # 构建差异说明：只反映"哪些版本有/没有"该条目，不比较内容
        all_versions = sorted(dfs.keys())
        missing = [v for v in all_versions if v not in sources]
        if not missing:
            diff_note = ""
        else:
            diff_note = f"仅存在于：{source_str}；缺少：{','.join(missing)}"

        keep_row["来源版本"] = source_str
        keep_row["存在版本数"] = len(sources)
        keep_row["差异说明"] = diff_note
        result_rows.append(keep_row)

    # 构建结果 DataFrame
    result_df = pd.DataFrame(result_rows)

    # 清理临时列
    result_df = result_df.drop(columns=["_source", "_key", "_record_len"], errors="ignore")

    # 重新生成序号
    result_df = result_df.reset_index(drop=True)
    result_df["seq"] = range(1, len(result_df) + 1)

    # 调整列顺序
    cols = ["seq", "year_ce", "year_era", "province", "city", "county",
            "ancient_name", "source", "record", "note", "来源版本", "存在版本数", "差异说明"]
    cols = [c for c in cols if c in result_df.columns]
    result_df = result_df[cols]

    # 保存结果
    output_path = base_dir / "明清时期灾荒食人年表_合并版.csv"
    result_df.to_csv(output_path, index=False, encoding="utf-8-sig")

    # 输出统计信息
    print(f"\n=== 合并结果 ===")
    print(f"总条目数（按 year_ce+province+city+county 去重后）: {len(result_df)}")
    print(f"原始条目总数: {sum(len(df) for df in dfs.values())}")
    print(f"去重合并后减少: {sum(len(df) for df in dfs.values()) - len(result_df)} 条")

    print(f"\n=== 来源版本分布（按存在版本数排序）===")
    source_counts = result_df.groupby(["存在版本数", "来源版本"]).size().reset_index(name="count")
    source_counts = source_counts.sort_values(["存在版本数", "count"], ascending=[False, False])
    for _, row in source_counts.iterrows():
        print(f"  {row['来源版本']}（{row['存在版本数']}个版本）: {row['count']} 条")

    print(f"\n结果已保存至: {output_path}")
    return result_df


def analyze_version_statistics():
    """
    分析每个版本按年份的统计情况，以年份为索引，每个版本展示三列：
    是否有记录（0/1）、记录条数、年份平均相似度。

    相似情况定义：对于一条记录，相似情况 = 该记录出现的版本数 / 总版本数
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

    id_cols = ["year_ce", "province", "city", "county"]
    total_versions = len(dfs)
    version_names = sorted(dfs.keys())

    # 收集所有记录，构建全局标识键
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

    # 先统计每个年份有哪些版本有记录
    year_versions = {}
    for v in version_names:
        v_df = all_df[all_df["version"] == v]
        valid = v_df[v_df["year_ce"].notna()]
        for year in valid["year_ce"].unique():
            y = int(year)
            year_versions.setdefault(y, set()).add(v)

    # 计算每条记录（按标识键）出现的版本数
    key_version_count = all_df.groupby("key")["version"].nunique().to_dict()

    # 计算每条记录的相似情况：该记录出现的版本数 / 该年份有记录的版本数
    def calc_similarity(row):
        rec_versions = key_version_count.get(row["key"], 0)
        year_total = len(year_versions.get(row["year_ce"], set()))
        return rec_versions / year_total if year_total else 0

    all_df["similarity"] = all_df.apply(calc_similarity, axis=1)

    # 统计每个版本、每个年份的数据
    stats = {}
    for v in version_names:
        v_df = all_df[all_df["version"] == v]
        valid = v_df[v_df["year_ce"].notna()]
        stats[v] = {}
        for year, group in valid.groupby("year_ce"):
            stats[v][int(year)] = {
                "has": 1,
                "cnt": len(group),
                "sim": round(group["similarity"].mean(), 4),
            }
        stats[v]["__overall__"] = round(v_df["similarity"].mean(), 4)

    # 收集所有年份（取并集）
    all_years = set()
    for v in version_names:
        all_years.update(k for k in stats[v] if isinstance(k, int))
    all_years = sorted(all_years)

    # 构建结果表格：年份为行索引，各版本的三列为横向展开
    result_rows = []
    for year in all_years:
        row = {"year_ce": year}
        for v in version_names:
            if year in stats[v]:
                s = stats[v][year]
                row[f"{v}_has"] = 1
                row[f"{v}_cnt"] = s["cnt"]
                row[f"{v}_sim"] = s["sim"]
            else:
                row[f"{v}_has"] = 0
                row[f"{v}_cnt"] = 0
                row[f"{v}_sim"] = None
        result_rows.append(row)

    # 为每个年份生成处理建议
    for row in result_rows:
        if row["year_ce"] == "总计":
            row["decision"] = ""
            row["ref"] = ""
            continue

        sims = {v: row[f"{v}_sim"] for v in version_names
                if row[f"{v}_sim"] is not None and pd.notna(row[f"{v}_sim"])}
        if not sims:
            row["decision"] = ""
            row["ref"] = ""
            continue

        max_sim = max(sims.values())
        ref_versions = [v for v, s in sims.items() if s == max_sim]
        ref = ",".join(ref_versions)

        if max_sim == 1:
            row["decision"] = "保留"
        elif max_sim <= 0.5:
            row["decision"] = "需人工处理"
        else:
            row["decision"] = "需进一步分析"
        row["ref"] = ref

    # 补充：非保留样本中，各版本记录条数相同 → 需校验地区信息
    for row in result_rows:
        if row["year_ce"] == "总计" or row["decision"] == "保留":
            continue
        cnts = [row[f"{v}_cnt"] for v in version_names if row[f"{v}_has"] == 1]
        if len(cnts) > 0 and len(set(cnts)) == 1:
            row["decision"] = "需校验地区信息"

    # 添加总计行（总体平均相似度）
    total_row = {"year_ce": "总计"}
    for v in version_names:
        total_row[f"{v}_has"] = ""
        total_row[f"{v}_cnt"] = sum(1 for y in all_years if stats[v].get(y, {}).get("has", 0) == 1)
        total_row[f"{v}_sim"] = stats[v].get("__overall__", "")
    total_row["decision"] = ""
    total_row["ref"] = ""
    result_rows.append(total_row)

    result_df = pd.DataFrame(result_rows)

    # 调整列顺序
    cols = ["year_ce", "decision", "ref"]
    for v in version_names:
        cols.extend([f"{v}_has", f"{v}_cnt", f"{v}_sim"])
    result_df = result_df[cols]

    output_path = base_dir / "版本年份统计与相似度分析.csv"
    result_df.to_csv(output_path, index=False, encoding="utf-8-sig")

    print(f"\n=== 版本年份统计与相似度分析 ===")
    print(f"共 {len(all_years)} 个年份")
    for v in version_names:
        years_in_v = sum(1 for y in all_years if y in stats[v])
        print(f"  {v}: {years_in_v} 个年份有记录，总体相似度: {stats[v].get('__overall__', '')}")

    print(f"\n结果已保存至: {output_path}")
    return result_df


if __name__ == "__main__":
    merge_famine_versions()
    analyze_version_statistics()
