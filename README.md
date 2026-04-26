# 明清时期灾荒食人现象研究数据集

本项目围绕陈岭《明清时期灾荒食人现象研究》中的相关史料，构建一个可复现的结构化数据处理流程，用于提取、比对、合并和复核明清时期灾荒食人事件记录。

当前仓库的重点不是“只保存一份最终 CSV”，而是尽量保留从原始文本到结构化结果之间的处理中间环节，方便后续继续调 prompt、核对版本差异、补跑失败年份，以及做人工复核。

## 当前数据产物

当前推荐优先查看 `result/明清时期灾荒食人年表_汇总版.csv`。该文件由多轮历史版本比对、自动合并和去重后生成，当前包含 1856 条记录。

`result/明清时期灾荒食人年表_待人工复核.csv` 是自动流程主动拎出的疑点清单，当前包含 7 条记录。它不是错误日志，而是“自动规则不应武断决定”的条目集合，主要用于最后的人文学术校勘。

## 项目目标

- 从校准后的 Markdown 史料中提取“明确发生食人行为”的记录
- 将非结构化史料整理为统一字段的 CSV 数据
- 对不同轮次 LLM 提取结果进行差异分析
- 在多版本结果基础上生成汇总版数据集
- 保留待人工复核清单，减少黑箱式处理

## 仓库结构

```text
.
├── data/
│   └── 校准版/
│       └── 明清时期灾荒食人现象研究_陈岭_校准版.md
├── processing/
│   └── scripts/
│       ├── extract_records_llm.py
│       ├── analyze_version_differences.py
│       └── build_consolidated_dataset.py
├── reference/
│   └── 明清时期灾荒食人现象研究_陈岭.pdf
├── result/
│   ├── versions/
│   ├── 明清时期灾荒食人年表_版本差异分析.csv
│   ├── 明清时期灾荒食人年表.csv
│   ├── 明清时期灾荒食人年表_汇总版.csv
│   └── 明清时期灾荒食人年表_待人工复核.csv
├── .env
├── LICENSE
└── README.md
```

## 数据来源

- 论文：陈岭，《明清时期灾荒食人现象研究》
- 校准后的工作文本：`data/校准版/明清时期灾荒食人现象研究_陈岭_校准版.md`
- PDF 参考资料：`reference/明清时期灾荒食人现象研究_陈岭.pdf`

`data/校准版/` 下的 Markdown 是抽取流程的直接输入。后续所有按年分段、调用 LLM、写出 CSV 的步骤，都是围绕这份校准文本进行的。

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
2. 将多轮提取结果保存到 `result/versions/`
3. 使用 `analyze_version_differences.py` 比对不同版本的条目差异
4. 使用 `build_consolidated_dataset.py` 根据差异分析结果生成汇总版数据
5. 对 `明清时期灾荒食人年表_待人工复核.csv` 进行人工检查

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
```

## 环境准备

建议使用 Python 3.11 或更高版本。

### 1. 安装依赖

安装依赖：

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

在仓库根目录创建 `.env` 文件，至少包含：

```env
LLM_API_KEY=your_api_key
LLM_BASE_URL=https://api.minimax.chat/v1
LLM_MODEL=abab6.5s-chat
```

说明：

- `LLM_API_KEY` 必填
- `LLM_BASE_URL` 和 `LLM_MODEL` 如果不写，会使用脚本中的默认值

## 脚本说明

### 1. `processing/scripts/extract_records_llm.py`

这是当前主抽取脚本，也是日常最常用的脚本。

它负责：

- 读取校准版 Markdown
- 按年份分段
- 调用 LLM 提取“明确发生食人行为”的记录
- 解析 JSON
- 清洗和验证字段
- 写入 `result/明清时期灾荒食人年表.csv`
- 在需要时保存进度和调试材料

#### 默认输入输出

- 输入：`data/校准版/明清时期灾荒食人现象研究_陈岭_校准版.md`
- 默认输出：`result/明清时期灾荒食人年表.csv`
- 进度文件：`processing/scripts/.extract_progress.json`
- 调试目录：`processing/scripts/.debug/`

#### 常见用法

全量运行：

```bash
python processing/scripts/extract_records_llm.py
```

测试前 20 个年份：

```bash
python processing/scripts/extract_records_llm.py --limit 20 --no-progress
```

只跑指定年份：

```bash
python processing/scripts/extract_records_llm.py --sample-years 1556 --no-progress
python processing/scripts/extract_records_llm.py --sample-years 1556,1877 --no-progress
```

保存 prompt / raw / parsed 调试材料：

```bash
python processing/scripts/extract_records_llm.py --sample-years 1556,1877 --debug-sample --no-progress
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

#### 推荐工作流

如果你正在调 prompt 或检查某个年份，建议按下面顺序操作：

1. 先用 `--sample-years` 跑 1 到 3 个代表性年份
2. 如需定位问题，再加 `--debug-sample`
3. prompt 稳定后，再跑更大的样本或全量
4. 若全量运行中有失败年份，使用 `--retry-failed`

### 2. `processing/scripts/analyze_version_differences.py`

这个脚本用于比较 `result/versions/` 目录下多个版本 CSV 的差异。

主要功能：

- 读取多个历史版本 CSV
- 按条目聚类分析版本差异
- 生成 `result/明清时期灾荒食人年表_版本差异分析.csv`
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

- `result/明清时期灾荒食人年表_汇总版.csv`
- `result/明清时期灾荒食人年表_待人工复核.csv`

## 结果目录说明

### `result/versions/`

用于保存不同轮次、不同 prompt、不同策略下的历史抽取结果。  
推荐命名清晰一些，例如：

- `明清时期灾荒食人年表_v1.csv`
- `明清时期灾荒食人年表_v2.csv`
- `明清时期灾荒食人年表_promptA.csv`

### `result/明清时期灾荒食人年表.csv`

主抽取脚本当前直接写出的结果，通常代表“某一轮当前版本”的原始结构化抽取结果。

### `result/明清时期灾荒食人年表_版本差异分析.csv`

由 `analyze_version_differences.py` 生成，用于分析版本间一致、冲突和待处理条目。

### `result/明清时期灾荒食人年表_汇总版.csv`

由 `build_consolidated_dataset.py` 生成，是当前仓库更接近“对外使用”的汇总版本。

### `result/明清时期灾荒食人年表_待人工复核.csv`

用于集中查看自动处理仍不够稳妥的条目，例如：

- 地点字段含多个地点
- 来源异常
- 需要人工判断的版本差异

当前待复核清单主要来自两类规则：

- `county` 含顿号 `、`：通常表示一个字段中包含多个县或地点，例如“平定县、乐平县”“句容、溧水、溧阳”。脚本会将这类条目交给人工判断是否应拆分为多条记录，或保留为区域性事件。
- `county` 为单字且 `record` 含顿号 `、`：通常表示原文中出现“徐、萧、丰、沛”“淮、徐、海、沐”等并列简称。脚本不自动推定这些简称对应的现代地名，而是列入复核清单。

进入待复核清单不代表条目无效。它只表示自动流程认为该条目的地点结构存在歧义，适合人工结合原文语境、地方志名和历史行政区划再确认。

## 人工复核建议

复核 `明清时期灾荒食人年表_待人工复核.csv` 时，建议优先检查：

- 并列地名是否应拆成多条记录，还是作为同一场区域性灾荒事件保留
- 单字简称是否能确定为具体州、府、县或区域
- `province`、`city`、`county` 是否需要补全或移动到更合适的层级
- 修订后的条目是否需要回填到汇总版，或作为下一轮规则优化依据

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

- 把每一轮结果备份到 `result/versions/`
- 在文件名中写明版本号或策略名
- 不要直接反复覆盖所有历史版本
- 汇总前先跑 `analyze_version_differences.py`

一个常见做法是：

1. 用 `extract_records_llm.py` 生成新结果
2. 将结果复制或重命名到 `result/versions/`
3. 跑 `analyze_version_differences.py`
4. 跑 `build_consolidated_dataset.py`
5. 检查待人工复核清单

## 当前限制

- 抽取结果仍依赖 LLM，稳定性受模型与 prompt 影响
- 古地名、泛指地区、多地点拆分等边界情况仍可能需要人工确认
- `source`、`province`、`county` 等字段虽然做了清洗，但并不等于完全标准化
- 汇总规则能减少重复和明显冲突，但不能完全替代人工学术校对

## 许可证

本项目采用 [MIT License](LICENSE)。

## 致谢

感谢原始研究资料的整理与校准工作，为后续结构化抽取和版本比对提供了基础。
