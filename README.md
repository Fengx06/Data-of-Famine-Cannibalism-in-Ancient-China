# 明清时期灾荒食人现象研究数据集

本项目围绕陈岭《明清时期灾荒食人现象研究》中的相关史料，构建一个可复现的结构化数据处理流程，用于提取、比对、合并和复核明清时期灾荒食人事件记录。

当前仓库的重点不是“只保存一份最终 CSV”，而是尽量保留从原始文本到结构化结果之间的处理中间环节，方便后续继续调 prompt、核对版本差异、补跑失败年份，以及做人工复核。

## 当前数据产物

当前推荐优先查看 `processing/merged_cleaned_data/ming_qing_famine_cannibalism_chen_ling/明清时期灾荒食人年表_汇总版.csv`。该文件由多轮历史版本比对、自动合并和去重后生成，当前包含 1856 条记录。

`processing/merged_cleaned_data/ming_qing_famine_cannibalism_chen_ling/明清时期灾荒食人年表_待人工复核.csv` 是自动流程主动拎出的疑点清单，当前包含 7 条记录。它不是错误日志，而是“自动规则不应武断决定”的条目集合，主要用于最后的人文学术校勘。

地名空间匹配后的结果建议查看：

- `result/明清时期灾荒食人年表_地点匹配明细.csv`：逐条记录的现代行政区划匹配结果。
- `result/明清时期灾荒食人事件_地级市汇总.csv`：按现代地级市汇总后的统计结果。
- `data/location_review/地名人工复核表.xlsx`：地名自动匹配仍不确定、需要人工确认的地点清单。

## 项目目标

- 从校准后的 Markdown 史料中提取“明确发生食人行为”的记录
- 将非结构化史料整理为统一字段的 CSV 数据
- 对不同轮次 LLM 提取结果进行差异分析
- 在多版本结果基础上生成汇总版数据集
- 将原始地点匹配到现代省、市、县层级，并按现代地级市汇总
- 保留待人工复核清单，减少黑箱式处理

## 仓库结构

```text
.
├── data/
│   ├── administrative_boundaries/
│   │   ├── 市.shp
│   │   └── 县.shp
│   ├── calibrated_text/
│   │   └── 明清时期灾荒食人现象研究_陈岭_校准版.md
│   ├── location_review/
│   │   ├── 地名地理编码缓存.csv
│   │   ├── 地名人工复核表.xlsx
│   │   └── 地名人工校订表.xlsx
│   └── raw/
│       └── 明清时期灾荒食人现象研究_陈岭.md
├── processing/
│   ├── calibration_rules/
│   │   └── 明清时期灾荒食人现象研究_陈岭_校准规则.md
│   ├── merged_cleaned_data/
│   │   └── ming_qing_famine_cannibalism_chen_ling/
│   │       ├── 明清时期灾荒食人年表_版本差异分析.csv
│   │       ├── 明清时期灾荒食人年表_汇总版.csv
│   │       └── 明清时期灾荒食人年表_待人工复核.csv
│   ├── record_level_cleaning/
│   │   └── ming_qing_famine_cannibalism_chen_ling/
│   │       └── versions/
│   └── scripts/
│       ├── extract_records_llm.py
│       ├── analyze_version_differences.py
│       ├── build_consolidated_dataset.py
│       ├── build_prefecture_event_summary.py
│       └── summarize_prefecture_events.py
├── reference/
│   └── 明清时期灾荒食人现象研究_陈岭.pdf
├── result/
│   ├── 明清时期灾荒食人年表_地点匹配明细.csv
│   └── 明清时期灾荒食人事件_地级市汇总.csv
├── .env.example                    # 环境变量占位符模板
├── .gitignore
├── LICENSE
├── README.md
└── requirements.txt
```

目录树列出仓库中的主要文件；本地 `.env`、调试材料、进度文件和临时测试输出不纳入版本控制。

## 数据来源

- 论文：陈岭，《明清时期灾荒食人现象研究》
- 原始 Markdown（未经校准）：`data/raw/明清时期灾荒食人现象研究_陈岭.md`
- 校准后的工作文本：`data/calibrated_text/明清时期灾荒食人现象研究_陈岭_校准版.md`
- PDF 参考资料：`reference/明清时期灾荒食人现象研究_陈岭.pdf`

`data/calibrated_text/` 下的 Markdown 是抽取流程的直接输入。后续所有按年分段、调用 LLM、写出 CSV 的步骤，都是围绕这份校准文本进行的。

## 数据字段

主抽取结果与汇总结果使用以下字段：

| 字段 | 含义 |
| --- | --- |
| `seq` | 顺序编号，通常在最终写出 CSV 时重排 |
| `year_ce` | 公元年份 |
| `year_era` | 年号纪年 |
| `province` | 省级地点，尽量使用简称，如“河南”“山东” |
| `city` | 市级地点，仅在原文明确出现时填写 |
| `county` | 县、州、府或最具体地点名称 |
| `ancient_name` | 原文中的古地名 |
| `source` | 来源书名，尽量清洗为书名级别 |
| `record` | 原文记录，尽量保留整行 |
| `note` | 备注，如 OCR 疑点、古地名推定说明等 |

## 处理流程

项目的推荐处理顺序如下：

1. 使用 `extract_records_llm.py` 从校准版 Markdown 中提取结构化记录
2. 将多轮提取结果保存到 `processing/record_level_cleaning/ming_qing_famine_cannibalism_chen_ling/versions/`
3. 使用 `analyze_version_differences.py` 比对不同版本的条目差异
4. 使用 `build_consolidated_dataset.py` 根据差异分析结果生成汇总版数据
5. 对 `processing/merged_cleaned_data/ming_qing_famine_cannibalism_chen_ling/明清时期灾荒食人年表_待人工复核.csv` 进行人工检查
6. 使用 `build_prefecture_event_summary.py` 生成地点匹配明细和地名人工复核表
7. 人工补充 `data/location_review/地名人工复核表.xlsx` 后，重新运行地点匹配脚本
8. 使用 `summarize_prefecture_events.py` 生成地级市汇总结果

### 流程示意

```text
校准版 Markdown
    ↓
extract_records_llm.py
    ↓
多轮历史版本 CSV
    ↓
analyze_version_differences.py
    ↓
明清时期灾荒食人年表_版本差异分析.csv
    ↓
build_consolidated_dataset.py
    ↓
汇总版 CSV + 待人工复核 CSV
    ↓
build_prefecture_event_summary.py
    ↓
地点匹配明细 CSV + 地名人工复核表 XLSX
    ↓
summarize_prefecture_events.py
    ↓
地级市汇总 CSV
```

## 环境准备

建议使用 Python 3.11 或更高版本。

### 1. 安装依赖

安装依赖：

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

先在仓库根目录复制占位符模板：

```bash
cp .env.example .env
```

将 `.env` 中的占位符替换为自己的配置：`LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL` 和 `BAIDU_MAP_AK`。模板不包含真实凭据；其中的示例地址和模型名必须按实际服务修改。

`.env` 已被 `.gitignore` 忽略，不要提交或使用 `git add -f` 强行加入。只有不含真实凭据的 `.env.example` 可以提交。若凭据曾进入 Git 历史，应立即在对应服务撤销并轮换；仅删除文件或增加忽略规则不会移除历史中的凭据。

说明：

- `LLM_API_KEY` 在调用 LLM 抽取或地名初判时需要
- `LLM_BASE_URL` 和 `LLM_MODEL` 如果不写，会使用脚本中的默认值
- `BAIDU_MAP_AK` 在需要补充地理编码缓存时需要；如果只复用已有缓存，可以使用 `--skip-geocode`

## 脚本说明

### 1. `processing/scripts/extract_records_llm.py`

这是当前主抽取脚本，也是日常最常用的脚本。

它负责：

- 读取校准版 Markdown
- 按年份分段
- 调用 LLM 提取“明确发生食人行为”的记录
- 解析 JSON
- 清洗和验证字段
- 写入 `processing/record_level_cleaning/ming_qing_famine_cannibalism_chen_ling/明清时期灾荒食人年表.csv`
- 在需要时保存进度和调试材料

#### 默认输入输出

- 输入：`data/calibrated_text/明清时期灾荒食人现象研究_陈岭_校准版.md`
- 默认输出：`processing/record_level_cleaning/ming_qing_famine_cannibalism_chen_ling/明清时期灾荒食人年表.csv`
- 进度文件：`processing/scripts/.extract_progress.json`
- 调试目录：`processing/scripts/.debug/`

#### 常见用法

全量运行：

```bash
python processing/scripts/extract_records_llm.py
```

测试前 20 个年份：

```bash
python processing/scripts/extract_records_llm.py --limit 20 --output result/test_first_20.csv --no-progress
```

只跑指定年份：

```bash
python processing/scripts/extract_records_llm.py --sample-years 1556 --output result/test_1556.csv --no-progress
python processing/scripts/extract_records_llm.py --sample-years 1556,1877 --output result/test_1556_1877.csv --no-progress
```

保存 prompt / raw / parsed 调试材料：

```bash
python processing/scripts/extract_records_llm.py --sample-years 1556,1877 --output result/test_1556_1877.csv --debug-sample --no-progress
```

重试历史失败年份：

```bash
python processing/scripts/extract_records_llm.py --retry-failed
```

从头重跑：

```bash
python processing/scripts/extract_records_llm.py --restart
```

指定输出文件：

```bash
python processing/scripts/extract_records_llm.py --sample-years 1556 --output result/test_1556.csv --no-progress
```

测试输出请使用 `result/test_*.csv` 或 `result/tmp/`（目录输出参数可指向后者），这些路径已被忽略。`--no-progress` 仅控制进度保存，不改变输出路径；指定测试输出可避免覆盖正式数据。正式数据、历史版本和人工复核文件仍保留在版本控制中。

#### 推荐工作流

如果你正在调 prompt 或检查某个年份，建议按下面顺序操作：

1. 先用 `--sample-years` 跑 1 到 3 个代表性年份
2. 如需定位问题，再加 `--debug-sample`
3. prompt 稳定后，再跑更大的样本或全量
4. 若全量运行中有失败年份，使用 `--retry-failed`

### 2. `processing/scripts/analyze_version_differences.py`

这个脚本用于比较 `processing/record_level_cleaning/ming_qing_famine_cannibalism_chen_ling/versions/` 目录下多个版本 CSV 的差异。

主要功能：

- 读取多个历史版本 CSV
- 按条目聚类分析版本差异
- 生成 `processing/merged_cleaned_data/ming_qing_famine_cannibalism_chen_ling/明清时期灾荒食人年表_版本差异分析.csv`
- 输出各版本总条数和整体相似度概览

运行方式：

```bash
python processing/scripts/analyze_version_differences.py
```

适用场景：

- 已经积累了多轮抽取结果
- 想知道哪些条目在不同版本之间一致
- 想找出需要人工复核或进一步分析的部分

### 3. `processing/scripts/build_consolidated_dataset.py`

这个脚本用于读取差异分析结果，并生成汇总版数据。

主要功能：

- 自动调用 `analyze_version_differences.py`
- 按处理建议选择候选版本
- 应用去重、合并、古今地名处理等规则
- 输出汇总版和待人工复核清单

运行方式：

```bash
python processing/scripts/build_consolidated_dataset.py
```

典型输出：

- `processing/merged_cleaned_data/ming_qing_famine_cannibalism_chen_ling/明清时期灾荒食人年表_汇总版.csv`
- `processing/merged_cleaned_data/ming_qing_famine_cannibalism_chen_ling/明清时期灾荒食人年表_待人工复核.csv`

### 4. `processing/scripts/build_prefecture_event_summary.py`

这个脚本用于把汇总版年表中的地点匹配到现代行政区划，并生成逐条地点匹配明细。

主要功能：

- 从 `processing/merged_cleaned_data/ming_qing_famine_cannibalism_chen_ling/明清时期灾荒食人年表_汇总版.csv` 读取原始记录
- 先生成唯一地点表，减少重复地理编码
- 对地点做规则或 LLM 初判，识别疑似古地名和需要人工复核的地名
- 优先读取 `data/location_review/地名地理编码缓存.csv`，只对缓存缺失的地点调用百度地图 API
- 使用 `data/administrative_boundaries/市.shp` 和 `data/administrative_boundaries/县.shp` 做点落面匹配
- 输出逐条地点匹配明细和 `地名人工复核表.xlsx`

常见用法：

```bash
python processing/scripts/build_prefecture_event_summary.py
```

小样本测试，且不调用百度 API：

```bash
python processing/scripts/build_prefecture_event_summary.py --max-locations 50 --output-dir result/test_prefecture_summary --skip-geocode
```

运行后删除中间文件：

```bash
python processing/scripts/build_prefecture_event_summary.py --clean-intermediate
```

典型输出：

- `result/明清时期灾荒食人年表_地点匹配明细.csv`
- `result/地名唯一表.csv`
- `result/地名LLM初判.csv`
- `data/location_review/地名地理编码缓存.csv`
- `data/location_review/地名人工复核表.xlsx`

复核表只保留 XLSX 版本，包含 `confirmed_modern_address` 和 `confirmed_location_level` 等列。人工复核后，重新运行本脚本即可优先使用复核结果。

### 5. `processing/scripts/summarize_prefecture_events.py`

这个脚本只读取地点匹配明细，不做地理编码，也不做空间匹配。

运行方式：

```bash
python processing/scripts/summarize_prefecture_events.py
```

输入：

- `result/明清时期灾荒食人年表_地点匹配明细.csv`

输出：

- `result/明清时期灾荒食人事件_地级市汇总.csv`

汇总表包含两个核心指标：

- `year_event_count`：按“年份-地级市”计数。同一年同一地级市及其下辖区县有多条记录，也只记 1 次。
- `year_location_count`：按“年份-地点数量”计数。同一年同一地级市下有多个区县有记录，则按区县数量计；若同一年地级市和下辖区县同时有记录，只统计区县数量；若只有地级市级记录，则记 1 次。

省级记录不参与地级市汇总。

## 结果目录说明

### `processing/record_level_cleaning/ming_qing_famine_cannibalism_chen_ling/versions/`

用于保存不同轮次、不同 prompt、不同策略下的历史抽取结果。  
推荐命名清晰一些，例如：

- `明清时期灾荒食人年表_v1.csv`
- `明清时期灾荒食人年表_v2.csv`
- `明清时期灾荒食人年表_promptA.csv`

### `processing/record_level_cleaning/ming_qing_famine_cannibalism_chen_ling/明清时期灾荒食人年表.csv`

`extract_records_llm.py` 的默认输出。该文件通常代表”某一轮当前版本”的原始结构化抽取结果；如果只使用已经整理好的汇总版数据，可以不重新生成它。

### `processing/merged_cleaned_data/ming_qing_famine_cannibalism_chen_ling/明清时期灾荒食人年表_版本差异分析.csv`

由 `analyze_version_differences.py` 生成，用于分析版本间一致、冲突和待处理条目。

### `processing/merged_cleaned_data/ming_qing_famine_cannibalism_chen_ling/明清时期灾荒食人年表_汇总版.csv`

由 `build_consolidated_dataset.py` 生成，是当前仓库更接近”对外使用”的汇总版本。

### `processing/merged_cleaned_data/ming_qing_famine_cannibalism_chen_ling/明清时期灾荒食人年表_待人工复核.csv`

用于集中查看自动处理仍不够稳妥的条目，例如：

- 地点字段含多个地点
- 来源异常
- 需要人工判断的版本差异

当前待复核清单主要来自两类规则：

- `county` 含顿号 `、`：通常表示一个字段中包含多个县或地点，例如“平定县、乐平县”“句容、溧水、溧阳”。脚本会将这类条目交给人工判断是否应拆分为多条记录，或保留为区域性事件。
- `county` 为单字且 `record` 含顿号 `、`：通常表示原文中出现“徐、萧、丰、沛”“淮、徐、海、沐”等并列简称。脚本不自动推定这些简称对应的现代地名，而是列入复核清单。

进入待复核清单不代表条目无效。它只表示自动流程认为该条目的地点结构存在歧义，适合人工结合原文语境、地方志名和历史行政区划再确认。

### `result/明清时期灾荒食人年表_地点匹配明细.csv`

由 `build_prefecture_event_summary.py` 生成，是逐条记录的现代行政区划匹配结果。它保留原始灾害记录信息，并新增：

- `matched_level`
- `matched_province`
- `matched_province_code`
- `matched_prefecture`
- `matched_prefecture_code`
- `matched_county`
- `matched_county_code`
- `lng_wgs84`
- `lat_wgs84`

其中 `matched_level=province` 的记录只保留到省级，不参与地级市汇总。

### `data/location_review/地名人工复核表.xlsx`

由 `build_prefecture_event_summary.py` 生成，只列出需要人工复核的唯一地点。人工主要填写：

- `confirmed_modern_address`：确认后的现代地名
- `confirmed_location_level`：确认后的地点层级，可选 `province_only`、`prefecture_level`、`county_or_specific`
- `correction_note`：可选备注

经纬度不需要人工填写，程序会用确认后的现代地名重新查询或读取地理编码缓存。

### `result/明清时期灾荒食人事件_地级市汇总.csv`

由 `summarize_prefecture_events.py` 生成，按现代地级市汇总地点匹配明细。当前字段包括：

- `matched_province`
- `matched_city`
- `matched_city_code`
- `year_event_count`
- `year_location_count`

## 人工复核建议

复核 `processing/merged_cleaned_data/ming_qing_famine_cannibalism_chen_ling/明清时期灾荒食人年表_待人工复核.csv` 时，建议优先检查：

- 并列地名是否应拆成多条记录，还是作为同一场区域性灾荒事件保留
- 单字简称是否能确定为具体州、府、县或区域
- `province`、`city`、`county` 是否需要补全或移动到更合适的层级
- 修订后的条目是否需要回填到汇总版，或作为下一轮规则优化依据

复核 `data/location_review/地名人工复核表.xlsx` 时，只需要确认现代地名和地点层级。程序会根据确认后的现代地名继续地理编码和空间匹配，不需要手动填写经纬度。

## 调试与排错

### 1. 模型输出无法解析为 JSON

可以先用：

```bash
python processing/scripts/extract_records_llm.py --sample-years 1556 --debug-sample --no-progress
```

然后查看：

- `processing/scripts/.debug/<year>/..._prompt.txt`
- `processing/scripts/.debug/<year>/..._raw.txt`
- `processing/scripts/.debug/<year>/..._parsed.json`

### 2. 某些年份失败

先看是否在进度文件中记录为失败，再使用：

```bash
python processing/scripts/extract_records_llm.py --retry-failed
```

### 3. 想避免污染正式进度

测试时建议始终加：

```bash
--no-progress
```

### 4. 想避免覆盖正式 CSV

测试时建议加：

```bash
--output result/your_test_file.csv
```

## 版本管理建议

如果你准备持续迭代 prompt，建议这样管理结果：

- 把每一轮结果备份到 `processing/record_level_cleaning/ming_qing_famine_cannibalism_chen_ling/versions/`
- 在文件名中写明版本号或策略名
- 不要直接反复覆盖所有历史版本
- 汇总前先跑 `analyze_version_differences.py`

一个常见做法是：

1. 用 `extract_records_llm.py` 生成新结果
2. 将结果复制或重命名到 `processing/record_level_cleaning/ming_qing_famine_cannibalism_chen_ling/versions/`
3. 跑 `analyze_version_differences.py`
4. 跑 `build_consolidated_dataset.py`
5. 检查待人工复核清单

## 当前限制

- 抽取结果仍依赖 LLM，稳定性受模型与 prompt 影响
- 古地名、泛指地区、多地点拆分等边界情况仍可能需要人工确认
- `source`、`province`、`county` 等字段虽然做了清洗，但并不等于完全标准化
- 汇总规则能减少重复和明显冲突，但不能完全替代人工学术校对
- 现代行政区划匹配依赖百度地理编码结果和当前使用的行政区划边界，古今地名变迁仍需要人工复核兜底

## 许可证

本项目采用 [MIT License](LICENSE)。

## 致谢

感谢原始研究资料的整理与校准工作，为后续结构化抽取和版本比对提供了基础。
