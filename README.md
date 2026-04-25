# 明清时期灾荒食人现象研究

本项目研究明清时期（1368-1912年）中国灾荒期间的食人现象。原始数据来自陈岭的研究论文《明清时期灾荒食人现象研究》，通过多轮 LLM 提取生成多个数据版本，经比对、分析后合并为最终数据集。

## 项目结构

```
├── data/
│   └── 校准版/                  # 经人工校准的原始文本（Markdown 格式）
│       └── 明清时期灾荒食人现象研究_陈岭_校准版.md
├── processing/
│   └── scripts/                 # 数据处理脚本
│       ├── extract_famine_data_llm.py   # LLM 提取原始数据
│       ├── retry_failed_years.py        # 重试提取失败的年份
│       ├── compare_versions.py          # 比对多版本差异，生成处理建议
│       └── consolidate_versions.py      # 按处理建议合并为最终数据集
├── result/
│   ├── 历史版本/                # 各轮 LLM 提取结果（v1 ~ v6）
│   ├── 版本条目统计与差异分析.csv    # 条目级差异分析与处理建议
│   ├── 明清时期灾荒食人年表_汇总版.csv   # 最终合并数据集
│   └── 明清时期灾荒食人年表_待人工复核.csv # 需人工复核的条目
├── reference/                   # 参考资料
│   └── 明清时期灾荒食人现象研究_陈岭.pdf
├── LICENSE
└── README.md
```

## 数据流水线

### 1. 原始数据提取

`extract_famine_data_llm.py` 读取校准版 Markdown 文本，按年份分段调用 LLM API（Minimax），提取食人事件记录，输出为 CSV。

`retry_failed_years.py` 用于定向补录提取失败的年份，追加到主 CSV 中。

### 2. 版本差异分析

`compare_versions.py` 读取 `result/历史版本/` 下的多个版本 CSV，进行条目级比对：

- 按 `year_ce + province + city + county + source + record` 聚类
- 判断各版本内容一致性
- 生成处理建议：**保留** / **需校验地区信息** / **需进一步分析**
- 输出 `版本条目统计与差异分析.csv`
- 同时打印各版本总条数与总体相似度

### 3. 合并汇总

`consolidate_versions.py` 读取差异分析结果，按规则自动合并：

- 对 "保留" 和 "需校验地区信息" 的条目，取自然排序后的第一个存在版本
- 对 "需进一步分析" 的条目，从 ref 候选版本中取 record 最长者
- 应用多条去重规则（子集合并、同地点合并、古今地名映射、空 county 处理等）
- 输出 `明清时期灾荒食人年表_汇总版.csv`
- 将可疑条目（city/county 含顿号、source 为空等）单独输出到 `明清时期灾荒食人年表_待人工复核.csv`

### 运行流程

```bash
cd processing/scripts

# 步骤1：生成差异分析（会自动调用 compare_versions.py）
python consolidate_versions.py

# 或单独运行版本比对
python compare_versions.py
```

## 数据来源

原始数据来自陈岭的研究论文《明清时期灾荒食人现象研究》。

## 许可证

本项目采用 MIT 许可证。详见 [LICENSE](LICENSE) 文件。