"""
重试提取失败的年份数据，追加到主 CSV 中。
"""

import asyncio
import csv
import json
import os
import re
import sys

# 修复 Windows 终端中文乱码
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from dataclasses import dataclass
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
from tqdm import tqdm

# 加载 .env 文件
_SCRIPT_DIR = Path(__file__).parent
_env_file = _SCRIPT_DIR.parent.parent / ".env"
if _env_file.exists():
    load_dotenv(dotenv_path=_env_file)

# ---------------------------------------------------------------------------
# 配置（与原脚本保持一致）
# ---------------------------------------------------------------------------
INPUT_FILE = Path(__file__).parent.parent.parent / "data" / "校准版" / "明清时期灾荒食人现象研究_陈岭_校准版.md"
OUTPUT_CSV = Path(__file__).parent.parent.parent / "result" / "明清时期灾荒食人年表.csv"

LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.minimax.chat/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "abab6.5s-chat")

CONCURRENT_REQUESTS = 3
REQUEST_TIMEOUT = 120
MAX_RETRIES = 5
MAX_CONTENT_CHARS = 4000
MAX_TOKENS = 16384

# 需要重试的年份
FAILED_YEARS = {1556, 1634, 1664, 1756, 1849, 1900, 1901}

YEAR_PATTERN = re.compile(
    r"^###\s+(?P<era>.+?)\s*[（(](?P<year>\d{1,4})年[）)]"
)


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


SYSTEM_PROMPT = """你是一个历史数据提取专家，负责从明清时期灾荒记录中提取食人相关事件。你的输出必须严格遵循给定的 JSON 格式。思考过程请尽量简短，将主要输出空间留给 JSON 结果。

重要：在构造 JSON 时，若原始记录中包含双引号（" 或 " 或 "），请在 JSON 的 record 字段中将其替换为单引号（'），以确保 JSON 格式合法。"""

USER_PROMPT_TEMPLATE = """请从以下 {year_era}（{year_ce}年）的灾荒记录中提取所有食人事件。

提取标准：
- 必须包含食人行为关键词才提取，如"人相食"、"以人为食"、"食人肉"、"食尸体"、"人吃人"等。
- 不提取：仅描述饥荒、大旱、大水但未提及食人行为的记录

输出格式（严格 JSON 数组）：
```json
[{{
  "province": "省（简写，如河南、山东；直辖市则填北京、上海等）",
  "city": "市（原文是什么就写什么，不要根据推理自动补全，如无填'无'；直辖市与省份相同）",
  "county": "县（原文是什么就写什么，不要简写，不要根据推理自动补全，如无填'无'）",
  "ancient_name": "古代地名（现代已不用的地名，如无填'无'）",
  "year_ce": 公元年份,
  "year_era": "年号",
  "source": "来源（仅书名，去除年号/卷次）",
  "note": "备注（如无填'无'）",
  "record": "原始记录原文（完整摘抄这一行的原文，包含开头的地名和来源，不要省略）"
}}]
```

边界条件处理：
1. 来源处理：去除编纂者年号和卷次信息。示例：`康熙《续修陈州志》卷四灾异` → `《续修陈州志》`
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
9. city 字段严格限制：只能填写原文中明确出现的市名（如`潍坊市`、`济南市`），不得根据县名推断所属市
10. 古代地名处理：若原文中出现古代地名（如`代州`、`泽州`、`秦州`等），ancient_name 填该古代地名本身
city 和 county 可根据映射规则推测现代地名填入，但必须在 note 中标注"xxx为推测，原文为古代地名xxx"
11. 省份简写：province 字段只写省名，不要加"省"字

输出要求：
- 只输出 JSON 数组，不要任何其他文字、解释、markdown 代码块标记
- year_ce 必须是整数数字
- 无食人记录的年份返回 `[]`
- record 字段中若原文包含双引号（" 或 " 或 "），请替换为单引号（'），确保 JSON 格式合法

---

以下是 {year_era}（{year_ce}年）的记录：

{content}
"""


def extract_year_segments(md_path: Path) -> list[tuple[str, int, str]]:
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
            if current_title is not None:
                content = "\n".join(current_lines).strip()
                segments.append((current_title, current_year, content))
            current_title = m.group("era")
            current_year = int(m.group("year"))
            current_lines = []
        elif current_title is not None:
            current_lines.append(line)

    if current_title is not None:
        content = "\n".join(current_lines).strip()
        segments.append((current_title, current_year, content))

    return segments


def parse_llm_response(raw: str, year_era: str, year_ce: int) -> list[dict]:
    text = raw.strip()

    think_start = text.find("<think>")
    think_end = text.find("</think>")
    if think_start != -1 and think_end != -1 and think_end > think_start:
        text = text[:think_start] + text[think_end + 8:]
    text = text.strip()

    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

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
        return []

    start = text.find("[")
    end = text.rfind("]")

    if start != -1 and end != -1 and end > start:
        try:
            records = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            try:
                records = json.loads(text[start:] + "]")
            except json.JSONDecodeError:
                print(f"  [Parse failed] {year_era}({year_ce}): JSON truncated and unrecoverable")
                return []
    elif start != -1:
        try:
            records = json.loads(text[start:] + "]")
        except json.JSONDecodeError:
            print(f"  [Parse failed] {year_era}({year_ce}): JSON truncated and unrecoverable")
            return []
    else:
        print(f"  [Parse failed] {year_era}({year_ce}): no JSON array found")
        return []

    if not isinstance(records, list):
        return []

    for rec in records:
        if isinstance(rec, dict):
            rec["year_era"] = year_era
            rec["year_ce"] = year_ce

    return records


def validate_record(raw: dict) -> Record | None:
    if not isinstance(raw, dict):
        return None

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

    if province in ("无", "") or len(province) > 15:
        return None

    return Record(
        province=province, city=city, county=county, ancient_name=ancient_name,
        year_ce=year_ce, year_era=year_era, source=source, note=note, record=record,
    )


def _sanitize_quotes(text: str) -> str:
    """将内容中的各类双引号替换为单引号，避免 LLM 生成的 JSON 中出现未转义引号。
    同时处理 ASCII 双引号 (U+0022) 和中文双引号 (U+201C/U+201D)。
    """
    return text.replace('"', "'").replace('"', "'").replace('"', "'")


async def _call_llm_single(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    year_era: str,
    year_ce: int,
    content: str,
) -> list[dict]:
    safe_content = _sanitize_quotes(content)
    prompt = USER_PROMPT_TEMPLATE.replace("{year_era}", year_era).replace("{year_ce}", str(year_ce)).replace("{content}", safe_content)

    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "max_tokens": MAX_TOKENS,
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
                    url, headers=headers, json=payload,
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    raw = data["choices"][0]["message"]["content"]
                    result = parse_llm_response(raw, year_era, year_ce)
                    if result is not None:
                        return result
            except Exception as e:
                print(f"  [Retry {attempt}/{MAX_RETRIES}] {year_era}({year_ce}) request failed: {e}")
            if attempt < MAX_RETRIES:
                await asyncio.sleep(2 ** attempt)

    print(f"  [Skip] {year_era}({year_ce}) final failure")
    return []


async def call_llm_extract(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    year_era: str,
    year_ce: int,
    content: str,
) -> list[dict]:
    if len(content) <= MAX_CONTENT_CHARS:
        return await _call_llm_single(session, semaphore, year_era, year_ce, content)

    lines = content.splitlines()
    chunks = []
    current_chunk = []
    current_len = 0

    for line in lines:
        line_len = len(line) + 1
        if current_len + line_len > MAX_CONTENT_CHARS and current_chunk:
            chunks.append(current_chunk)
            current_chunk = [line]
            current_len = line_len
        else:
            current_chunk.append(line)
            current_len += line_len

    if current_chunk:
        chunks.append(current_chunk)

    print(f"  [{year_ce}] {year_era} content too long ({len(content)} chars), split into {len(chunks)} requests")

    all_records = []
    for idx, chunk_lines in enumerate(chunks, 1):
        chunk_content = "\n".join(chunk_lines)
        header = f"【{year_era}（{year_ce}年）记录共 {len(chunks)} 部分，此为第 {idx} 部分】"
        records = await _call_llm_single(session, semaphore, year_era, year_ce, header + "\n" + chunk_content)
        all_records.extend(records)
        print(f"    - Part {idx}/{len(chunks)} -> {len(records)} records")

    return all_records


def get_next_seq(csv_path: Path) -> int:
    if not csv_path.exists() or csv_path.stat().st_size == 0:
        return 1
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        try:
            next(reader)
        except StopIteration:
            return 1
        rows = list(reader)
    return len(rows) + 1


def write_csv(records: list[Record], path: Path, start_seq: int):
    fieldnames = ["seq", "year_ce", "year_era", "province", "city", "county", "ancient_name", "source", "record", "note"]
    path.parent.mkdir(parents=True, exist_ok=True)

    need_header = not path.exists() or path.stat().st_size == 0
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if need_header:
            writer.writeheader()
        for i, rec in enumerate(records, start_seq):
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


async def main():
    if not INPUT_FILE.exists():
        print(f"Error: input file not found: {INPUT_FILE}")
        sys.exit(1)

    if not LLM_API_KEY:
        print("Error: please set environment variable LLM_API_KEY")
        sys.exit(1)

    segments = extract_year_segments(INPUT_FILE)
    # 只保留失败年份
    pending = [(era, ce, content) for era, ce, content in segments if ce in FAILED_YEARS]

    print(f"Retrying {len(pending)} failed years: {[ce for _, ce, _ in pending]}")

    if not pending:
        print("No failed years to retry")
        return

    semaphore = asyncio.Semaphore(CONCURRENT_REQUESTS)
    seen_keys = set()
    all_new_records = []

    connector = aiohttp.TCPConnector(limit=CONCURRENT_REQUESTS)
    async with aiohttp.ClientSession(connector=connector) as session:
        for era, ce, content in pending:
            print(f"\n[{ce}] {era} -> processing...")
            raw_records = await call_llm_extract(session, semaphore, era, ce, content)
            records = [r for r in (validate_record(r) for r in raw_records) if r is not None]
            print(f"  [{ce}] {era} -> {len(records)} valid records")
            for rec in records:
                key = (rec.province, rec.city, rec.county, rec.year_ce, rec.source, rec.note, rec.record)
                if key not in seen_keys:
                    seen_keys.add(key)
                    all_new_records.append(rec)

    if all_new_records:
        start_seq = get_next_seq(OUTPUT_CSV)
        write_csv(all_new_records, OUTPUT_CSV, start_seq)
        print(f"\nAdded {len(all_new_records)} new records to {OUTPUT_CSV}")
    else:
        print("\nNo new records extracted")

    # 重新排序 seq 号
    if OUTPUT_CSV.exists():
        with open(OUTPUT_CSV, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            all_rows = list(reader)

        all_rows.sort(key=lambda r: (int(r["year_ce"]), int(r["seq"])))

        fieldnames = reader.fieldnames or []
        with open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for i, row in enumerate(all_rows, 1):
                row["seq"] = i
                writer.writerow(row)

        print(f"Re-sequenced {len(all_rows)} total records")


if __name__ == "__main__":
    asyncio.run(main())
