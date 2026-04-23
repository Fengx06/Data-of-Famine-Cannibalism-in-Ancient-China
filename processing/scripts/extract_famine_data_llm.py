#!/usr/bin/env python3
"""
从校准版 Markdown 中提取明清时期灾荒食人记录到 CSV。

用法:
    export LLM_API_KEY="your-api-key"
    export LLM_BASE_URL="https://api.minimax.chat/v1"  # 或其他兼容 OpenAI 的 API
    export LLM_MODEL="abab6.5s-chat"                    # 或其他模型

    # 正式运行（全部 224 个年份）
    python extract_famine_data_llm.py

    # 先测试 20 条看看效果
    python extract_famine_data_llm.py --limit 20 --no-progress

    # 跳过前 10 条，再测试 20 条
    python extract_famine_data_llm.py --offset 10 --limit 20 --no-progress
"""

import argparse
import asyncio
import csv
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

# 加载 .env 文件（从脚本目录向上查找）
_SCRIPT_DIR = Path(__file__).parent
_env_file = _SCRIPT_DIR.parent.parent / ".env"
if _env_file.exists():
    load_dotenv(dotenv_path=_env_file)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
INPUT_FILE = Path(__file__).parent.parent.parent / "data" / "校准版" / "明清时期灾荒食人现象研究_陈岭_校准版.md"
OUTPUT_CSV = Path(__file__).parent.parent.parent / "result" / "明清时期灾荒食人年表.csv"
PROGRESS_FILE = Path(__file__).parent / ".extract_progress.json"

LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.minimax.chat/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "abab6.5s-chat")

CONCURRENT_REQUESTS = 5           # 并发数
REQUEST_TIMEOUT = 120             # 单次请求超时（秒）
MAX_RETRIES = 3                   # 失败重试次数


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
@dataclass
class Record:
    province: str
    city: str
    county: str
    ancient_name: str
    year_ce: int
    year_era: str
    source: str
    note: str


# ---------------------------------------------------------------------------
# System Prompt
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """你是一个历史数据提取专家，负责从明清时期灾荒记录中提取食人相关事件。你的输出必须严格遵循给定的 JSON 格式。"""


# ---------------------------------------------------------------------------
# User Prompt 模板
# ---------------------------------------------------------------------------
USER_PROMPT_TEMPLATE = """请从以下 {year_era}（{year_ce}年）的灾荒记录中提取所有食人事件。

**提取标准**：
- 必须包含食人行为关键词才提取：`人相食`、`相食`、`民相食`、`父子相食`、`兄弟相食`、`至相食`、`多相食`、`人有相食者`、`易子而食`、`割人肉`、`杀人而食`、`食人肉`、`自食其子`、`自食其父`、`父食其子`、`或相食`
- **不提取**：仅描述饥荒、大旱、大水但未提及食人行为的记录

**输出格式**（严格 JSON 数组）：
```json
[{
  "province": "省",
  "city": "市（如无填'无'）",
  "county": "县（如无填'无'）",
  "ancient_name": "古代地名（现代已不用的地名，如无填'无'）",
  "year_ce": 公元年份,
  "year_era": "年号",
  "source": "来源（仅书名，去除年号/卷次）",
  "note": "备注（如无填'无'）"
}]
```

**边界条件处理**：
1. **来源处理**：去除编纂者年号和卷次信息。示例：`康熙《续修陈州志》卷四灾异` → `《续修陈州志》`；转引来源保留原始来源书名，如`——《两当县新志》（道光二十二年），转引自《西北灾荒史》第1595页` → `《两当县新志》转引《西北灾荒史》`
2. **古代地名映射**：以下古代地名请映射到现代地名并填入 ancient_name 字段：
   - `泽州`→`山西省晋城市`，`解州`→`山西省运城市`，`绛州`→`山西省运城市新绛县`，`秦州`→`甘肃省天水市`，`肃州`→`甘肃省酒泉市`，`代州`→`山西省忻州市代县`，`平阳府`→`山西省临汾市`，`河州`→`甘肃省临夏县`
   - 现代仍在用的地名（如庆阳、庄浪、辽东）不记为古代地名
3. **OCR错误**：若发现明显 OCR 错误（如`个人信息`），在 note 中标注"疑似OCR错误"
4. **顿号多县拆分**：如`曲沃、洪洞、临汾`出现在同一条记录中，拆分为3条独立记录
5. **县等格式拆分**：如`静宁县等：静宁、灵台、肃州等处` → 拆分为静宁、灵台、肃州3条
6. **泛指跳过**：
   - 多省泛指（如`两京、山东、河南、湖广...`）不提取
   - 府级泛指（如`淮、扬、庐、凤等府饥`）不提取
   - 但如果是`某府：府下具体县人相食`，则提取具体县
7. **特殊地名**：`秦、晋`→陕西、山西（备注说明）；`都下`→北京；`京师`→北京；`北畿`→北京

**输出要求**：
- 只输出 JSON 数组，不要任何其他文字、解释、markdown 代码块标记
- year_ce 必须是整数数字
- 无食人记录的年份返回 `[]`

---

以下是 {year_era}（{year_ce}年）的记录：

{content}
"""


# ---------------------------------------------------------------------------
# 分段解析
# ---------------------------------------------------------------------------
YEAR_PATTERN = re.compile(
    r"^###\s+(?P<era>.+?)\s*[（(](?P<year>\d{1,4})年[）)]"
)


def extract_year_segments(md_path: Path) -> list[tuple[str, int, str]]:
    """
    按 ### 标题分段，返回 [(年号, 公元年, 内容)]
    """
    text = md_path.read_text(encoding="utf-8")
    lines = text.splitlines()

    segments = []
    current_title = None
    current_year = None
    current_lines = []

    for line in lines:
        line = line.rstrip()
        m = YEAR_PATTERN.match(line)
        if m:
            # 保存上一个分段
            if current_title is not None:
                content = "\n".join(current_lines).strip()
                segments.append((current_title, current_year, content))
            current_title = m.group("era")
            current_year = int(m.group("year"))
            current_lines = []
        elif current_title is not None:
            current_lines.append(line)

    # 最后一个分段
    if current_title is not None:
        content = "\n".join(current_lines).strip()
        segments.append((current_title, current_year, content))

    return segments


# ---------------------------------------------------------------------------
# LLM 调用
# ---------------------------------------------------------------------------
async def call_llm_extract(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    year_era: str,
    year_ce: int,
    content: str,
) -> list[dict]:
    """
    调用 LLM API 提取单年份的食人记录。
    """
    prompt = USER_PROMPT_TEMPLATE.format(
        year_era=year_era,
        year_ce=year_ce,
        content=content,
    )

    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 4096,
    }

    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }

    url = f"{LLM_BASE_URL}/chat/completions"

    for attempt in range(1, MAX_RETRIES + 1):
        async with semaphore:
            try:
                async with session.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    raw = data["choices"][0]["message"]["content"]
                    return parse_llm_response(raw, year_era, year_ce)
            except Exception as e:
                print(f"  [重试 {attempt}/{MAX_RETRIES}] {year_era}({year_ce}) 请求失败: {e}")
                if attempt == MAX_RETRIES:
                    print(f"  [跳过] {year_era}({year_ce}) 最终失败")
                    return []
                await asyncio.sleep(2 ** attempt)

    return []


# ---------------------------------------------------------------------------
# 响应解析
# ---------------------------------------------------------------------------
def parse_llm_response(raw: str, year_era: str, year_ce: int) -> list[dict]:
    """
    从 LLM 返回的文本中解析 JSON 数组。
    """
    text = raw.strip()

    # 去除 markdown 代码块标记
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    # 尝试直接解析
    try:
        records = json.loads(text)
    except json.JSONDecodeError:
        # 尝试提取方括号包裹的内容
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1 and end > start:
            try:
                records = json.loads(text[start : end + 1])
            except json.JSONDecodeError as e:
                print(f"  [解析失败] {year_era}({year_ce}): {e}")
                # 保存原始响应用于调试
                debug_dir = Path(__file__).parent / ".debug"
                debug_dir.mkdir(exist_ok=True)
                (debug_dir / f"{year_ce}_raw.txt").write_text(raw, encoding="utf-8")
                return []
        else:
            print(f"  [解析失败] {year_era}({year_ce}): 未找到 JSON 数组")
            debug_dir = Path(__file__).parent / ".debug"
            debug_dir.mkdir(exist_ok=True)
            (debug_dir / f"{year_ce}_raw.txt").write_text(raw, encoding="utf-8")
            return []

    if not isinstance(records, list):
        print(f"  [格式错误] {year_era}({year_ce}): 返回不是数组")
        return []

    # 注入年号与公元年（确保一致）
    for rec in records:
        if isinstance(rec, dict):
            rec["year_era"] = year_era
            rec["year_ce"] = year_ce

    return records


# ---------------------------------------------------------------------------
# 记录验证与清洗
# ---------------------------------------------------------------------------
def validate_record(raw: dict) -> Record | None:
    """
    验证字段并填充默认值，返回 Record 或 None（无效记录）。
    """
    if not isinstance(raw, dict):
        return None

    # year_ce 必须是整数
    try:
        year_ce = int(raw.get("year_ce", 0))
    except (ValueError, TypeError):
        return None

    province = str(raw.get("province", "")).strip() or "无"
    city = str(raw.get("city", "")).strip() or "无"
    county = str(raw.get("county", "")).strip() or "无"
    ancient_name = str(raw.get("ancient_name", "")).strip() or "无"
    year_era = str(raw.get("year_era", "")).strip() or "无"
    source = str(raw.get("source", "")).strip() or "无"
    note = str(raw.get("note", "")).strip() or "无"

    # 跳过无 province 或 province 为泛指的记录
    if province in ("无", "") or len(province) > 20:
        # province 过长可能是整段描述误入
        return None

    return Record(
        province=province,
        city=city,
        county=county,
        ancient_name=ancient_name,
        year_ce=year_ce,
        year_era=year_era,
        source=source,
        note=note,
    )


# ---------------------------------------------------------------------------
# 进度持久化
# ---------------------------------------------------------------------------
def load_progress() -> set[int]:
    if PROGRESS_FILE.exists():
        try:
            data = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
            return set(data.get("done_years", []))
        except Exception:
            pass
    return set()


def save_progress(done_years: set[int]):
    PROGRESS_FILE.write_text(
        json.dumps({"done_years": sorted(done_years)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# CSV 写入
# ---------------------------------------------------------------------------
def write_csv(records: list[Record], path: Path):
    fieldnames = ["province", "city", "county", "ancient_name", "year_ce", "year_era", "source", "note"]
    path.parent.mkdir(parents=True, exist_ok=True)

    # 根据文件是否存在且有内容来决定是否写 header
    need_header = not path.exists() or path.stat().st_size == 0
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if need_header:
            writer.writeheader()
        for rec in records:
            writer.writerow(asdict(rec))


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
async def main():
    parser = argparse.ArgumentParser(description="从校准版 Markdown 中提取明清时期灾荒食人记录到 CSV")
    parser.add_argument("--limit", type=int, default=None, help="仅处理前 N 个年份（用于测试）")
    parser.add_argument("--offset", type=int, default=0, help="跳过前 N 个年份后再开始处理")
    parser.add_argument("--output", type=str, default=None, help="自定义输出 CSV 路径")
    parser.add_argument("--no-progress", action="store_true", help="不使用进度文件（测试模式）")
    parser.add_argument("--restart", action="store_true", help="重置进度，重新处理所有年份")
    args = parser.parse_args()

    if not INPUT_FILE.exists():
        print(f"错误：输入文件不存在: {INPUT_FILE}")
        sys.exit(1)

    if not LLM_API_KEY:
        print("错误：请设置环境变量 LLM_API_KEY")
        sys.exit(1)

    # 重置进度
    if args.restart and PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()
        print("已重置进度文件")

    segments = extract_year_segments(INPUT_FILE)
    print(f"共解析到 {len(segments)} 个年份分段")

    # 确定输出路径
    if args.output:
        output_csv = Path(args.output)
    elif args.limit:
        output_csv = OUTPUT_CSV.parent / f"明清时期灾荒食人年表_测试{args.limit}条.csv"
    else:
        output_csv = OUTPUT_CSV

    # 确定进度文件（测试模式或使用了 offset/limit 时不用进度文件，避免污染正式进度）
    is_test_mode = args.limit is not None or args.offset > 0
    use_progress = not (args.no_progress or is_test_mode)
    progress_file = PROGRESS_FILE if use_progress else None

    done_years = load_progress() if progress_file else set()
    if done_years:
        print(f"已处理 {len(done_years)} 个年份，将跳过")

    pending = [(era, ce, content) for era, ce, content in segments if ce not in done_years]

    # 应用 offset 和 limit（在原始列表上切片，不受已处理年份影响）
    if args.offset:
        pending = pending[args.offset:]
        print(f"跳过前 {args.offset} 个，剩余 {len(pending)} 个")
    if args.limit:
        pending = pending[:args.limit]
        print(f"【测试模式】仅处理前 {args.limit} 个年份")
    else:
        print(f"待处理 {len(pending)} 个年份")

    if not pending:
        print("所有年份已处理完毕")
        return

    semaphore = asyncio.Semaphore(CONCURRENT_REQUESTS)
    all_records: list[Record] = []
    seen_keys: set[tuple] = set()

    connector = aiohttp.TCPConnector(limit=CONCURRENT_REQUESTS)
    async with aiohttp.ClientSession(connector=connector) as session:
        for i in range(0, len(pending), CONCURRENT_REQUESTS):
            batch = pending[i : i + CONCURRENT_REQUESTS]
            tasks = [
                call_llm_extract(session, semaphore, era, ce, content)
                for era, ce, content in batch
            ]
            results = await asyncio.gather(*tasks)

            batch_records: list[Record] = []
            batch_years: set[int] = set()
            for (era, ce, _), raw_records in zip(batch, results):
                records = [r for r in (validate_record(r) for r in raw_records) if r is not None]
                # 批次内去重
                for rec in records:
                    key = (rec.province, rec.city, rec.county, rec.year_ce, rec.source, rec.note)
                    if key not in seen_keys:
                        seen_keys.add(key)
                        batch_records.append(rec)
                batch_years.add(ce)
                print(f"  [{ce}] {era} -> {len(records)} 条记录")

            # 写入 CSV
            if batch_records:
                write_csv(batch_records, output_csv)
                all_records.extend(batch_records)

            # CSV 写入成功后再保存进度，避免进度领先于 CSV
            if progress_file:
                done_years.update(batch_years)
                save_progress(done_years)

    print(f"\n完成！输出文件: {output_csv}")

    # 最终统计
    if output_csv.exists():
        with open(output_csv, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            next(reader)  # skip header
            total = sum(1 for _ in reader)
        print(f"总记录数: {total}")

    # 全部完成后清理进度文件
    if progress_file and len(done_years) == len(segments):
        PROGRESS_FILE.unlink(missing_ok=True)
        print("已清理进度文件")


if __name__ == "__main__":
    asyncio.run(main())
