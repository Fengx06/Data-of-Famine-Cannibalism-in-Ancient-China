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
import threading
from dataclasses import dataclass
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
MAX_CONTENT_CHARS = 4000          # 单请求内容最大字符数，超长则拆分

# CSV 写入锁（防止多进程/多实例并发写入导致行交错）
_CSV_LOCK = threading.Lock()


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
    record: str


# ---------------------------------------------------------------------------
# System Prompt
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """你是一个历史数据提取专家，负责从明清时期灾荒记录中提取食人相关事件。你的输出必须严格遵循给定的 JSON 格式。"""


# ---------------------------------------------------------------------------
# User Prompt 模板
# ---------------------------------------------------------------------------
USER_PROMPT_TEMPLATE = """请从以下 {year_era}（{year_ce}年）的灾荒记录中提取所有食人事件。

提取标准：
- 必须包含食人行为关键词才提取，如"人相食"、"以人为食"、"食人肉"、"食尸体"、"人吃人"等。
- 不提取：仅描述饥荒、大旱、大水但未提及食人行为的记录

输出格式（严格 JSON 数组）：
```json
[{
  "province": "省（简写，如河南、山东；直辖市则填北京、上海等）",
  "city": "市（原文是什么就写什么，不要根据推理自动补全，如无填'无'；直辖市与省份相同）",
  "county": "县（原文是什么就写什么，不要简写，不要根据推理自动补全，如无填'无'）",
  "ancient_name": "古代地名（现代已不用的地名，如无填'无'）",
  "year_ce": 公元年份,
  "year_era": "年号",
  "source": "来源（仅书名，去除年号/卷次）",
  "note": "备注（如无填'无'）",
  "record": "原始记录原文（完整摘抄这一行的原文，包含开头的地名和来源，不要省略）"
}]
```

边界条件处理：
1. 来源处理：去除编纂者年号和卷次信息。示例：`康熙《续修陈州志》卷四灾异` → `《续修陈州志》`；转引来源保留原始来源书名，如`——《两当县新志》（道光二十二年），转引自《西北灾荒史》第1595页` → `《两当县新志》转引《西北灾荒史》`
2. 古代地名映射参考：以下古代地名可映射到现代地名用于填写 city/county，但 ancient_name 必须填古代地名本身：
   - `代州`→`山西省忻州市代县`，`泽州`→`山西省晋城市`，`秦州`→`甘肃省天水市`
   - 现代仍在用的地名（如庆阳、庄浪、辽东）不记为古代地名
3. OCR错误：若发现明显 OCR 错误（如`个人信息`），在 note 中标注"疑似OCR错误"
4. 顿号多县拆分：如`曲沃、洪洞、临汾`出现在同一条记录中，拆分为3条独立记录
5. 县等格式拆分：如`静宁县等：静宁、灵台、肃州等处` → 拆分为静宁、灵台、肃州3条
6. 泛指跳过：
   - 多省泛指（如`两京、山东、河南、湖广...`）不提取
   - 府级泛指（如`淮、扬、庐、凤等府饥`）不提取
   - 但如果是`某府：府下具体县人相食`，则提取具体县
7. 特殊地名：`秦、晋`→陕西、山西（备注说明）；`都下`→北京；`京师`→北京；`北畿`→北京
8. 地名提取优先级：若原文中已经包含省/市/县名称，直接按原文提取，不要根据来源书名推断补充
9. city 字段严格限制：只能填写原文中明确出现的市名（如`潍坊市`、`济南市`），不得根据县名推断所属市，不得将古代地名的现代映射填入 city
10. 古代地名处理：若原文中出现古代地名（如`代州`、`泽州`、`秦州`等），ancient_name 填该古代地名本身；city 和 county 可根据映射规则推测现代地名填入，但必须在 note 中标注"xxx为推测，原文为古代地名xxx"。若原文中已是现代地名，则 city/county 严格按原文填写，不得推断
11. 省份简写：province 字段只写省名，不要加"省"字（如`河南`、`山东`）；直辖市（北京、上海、天津、重庆）province 和 city 都填该直辖市名称

输出要求：
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
async def _call_llm_single(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    year_era: str,
    year_ce: int,
    content: str,
) -> list[dict]:
    """单次 LLM 调用。"""
    prompt = USER_PROMPT_TEMPLATE.replace("{year_era}", year_era).replace("{year_ce}", str(year_ce)).replace("{content}", content)

    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 8192,
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


async def call_llm_extract(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    year_era: str,
    year_ce: int,
    content: str,
) -> list[dict]:
    """
    调用 LLM API 提取单年份的食人记录。
    若内容超长，按行拆分为多个 chunk 分别调用，最后合并结果。
    """
    if len(content) <= MAX_CONTENT_CHARS:
        return await _call_llm_single(session, semaphore, year_era, year_ce, content)

    # 按行拆分，保持行完整性
    lines = content.splitlines()
    chunks: list[list[str]] = []
    current_chunk: list[str] = []
    current_len = 0

    for line in lines:
        line_len = len(line) + 1  # +1 为换行符
        if current_len + line_len > MAX_CONTENT_CHARS and current_chunk:
            chunks.append(current_chunk)
            current_chunk = [line]
            current_len = line_len
        else:
            current_chunk.append(line)
            current_len += line_len

    if current_chunk:
        chunks.append(current_chunk)

    print(f"  [{year_ce}] {year_era} 内容超长（{len(content)} 字符），拆分为 {len(chunks)} 次请求")

    all_records: list[dict] = []
    for idx, chunk_lines in enumerate(chunks, 1):
        chunk_content = "\n".join(chunk_lines)
        # 标注这是第几部分，帮助 LLM 理解上下文边界
        header = f"【{year_era}（{year_ce}年）记录共 {len(chunks)} 部分，此为第 {idx} 部分】"
        records = await _call_llm_single(session, semaphore, year_era, year_ce, header + "\n" + chunk_content)
        all_records.extend(records)
        print(f"    - 第 {idx}/{len(chunks)} 次请求 -> {len(records)} 条记录")

    return all_records


# ---------------------------------------------------------------------------
# 响应解析
# ---------------------------------------------------------------------------
def _save_debug_raw(year_ce: int, raw: str) -> None:
    """保存原始响应到 debug 目录。"""
    debug_dir = Path(__file__).parent / ".debug"
    debug_dir.mkdir(exist_ok=True)
    (debug_dir / f"{year_ce}_raw.txt").write_text(raw, encoding="utf-8")


def parse_llm_response(raw: str, year_era: str, year_ce: int) -> list[dict]:
    """
    从 LLM 返回的文本中解析 JSON 数组。
    """
    text = raw.strip()

    # 去掉 <think>...</think> 推理块
    think_start = text.find("<think>")
    think_end = text.find("</think>")
    if think_start != -1 and think_end != -1 and think_end > think_start:
        text = text[:think_start] + text[think_end + 8:]
    text = text.strip()

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
        pass
    else:
        if isinstance(records, list):
            for rec in records:
                if isinstance(rec, dict):
                    rec["year_era"] = year_era
                    rec["year_ce"] = year_ce
            return records
        print(f"  [格式错误] {year_era}({year_ce}): 返回不是数组")
        return []

    # 尝试提取方括号包裹的内容
    start = text.find("[")
    end = text.rfind("]")

    if start != -1 and end != -1 and end > start:
        # 完整的 [ ... ]
        try:
            records = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            # 尝试补全截断的 JSON
            try:
                records = json.loads(text[start:] + "]")
            except json.JSONDecodeError:
                print(f"  [解析失败] {year_era}({year_ce}): JSON 截断且无法修复")
                _save_debug_raw(year_ce, raw)
                return []
    elif start != -1:
        # 有 [ 但没有 ]，尝试补全
        try:
            records = json.loads(text[start:] + "]")
        except json.JSONDecodeError:
            print(f"  [解析失败] {year_era}({year_ce}): JSON 截断且无法修复")
            _save_debug_raw(year_ce, raw)
            return []
    else:
        print(f"  [解析失败] {year_era}({year_ce}): 未找到 JSON 数组")
        _save_debug_raw(year_ce, raw)
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
    record = str(raw.get("record", "")).strip() or "无"

    # 跳过无 province 或 province 为泛指的记录
    if province in ("无", "") or len(province) > 15:
        # province 过长可能是整段描述误入（中国省份最长约 9 字）
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
        record=record,
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
    fieldnames = ["seq", "year_ce", "year_era", "province", "city", "county", "ancient_name", "source", "record", "note"]
    path.parent.mkdir(parents=True, exist_ok=True)

    with _CSV_LOCK:
        # 计算已有记录数，用于序号续编
        existing_rows = 0
        if path.exists() and path.stat().st_size > 0:
            with open(path, "r", encoding="utf-8-sig") as f:
                reader = csv.reader(f)
                try:
                    next(reader)  # skip header
                except StopIteration:
                    pass
                existing_rows = sum(1 for _ in reader)

        need_header = not path.exists() or path.stat().st_size == 0
        with open(path, "a", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if need_header:
                writer.writeheader()
            for i, rec in enumerate(records, existing_rows + 1):
                row = {
                    "seq": i,
                    "year_ce": rec.year_ce,
                    "year_era": rec.year_era,
                    "province": rec.province,
                    "city": rec.city,
                    "county": rec.county,
                    "ancient_name": rec.ancient_name,
                    "source": rec.source,
                    "record": rec.record,
                    "note": rec.note,
                }
                writer.writerow(row)


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
                    key = (rec.province, rec.city, rec.county, rec.year_ce, rec.source, rec.note, rec.record)
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
