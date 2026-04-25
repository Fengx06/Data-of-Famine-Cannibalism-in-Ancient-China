"""
从校准版 Markdown 文本中提取明清时期灾荒食人事件记录。

功能：
- 读取 data/校准版/明清时期灾荒食人现象研究_陈岭_校准版.md
- 按年份分段，调用 LLM API（默认 Minimax abab6.5s-chat）提取食人事件
- 解析 LLM 返回的 JSON，验证并写入 result/明清时期灾荒食人年表.csv
- 支持并发请求、失败重试、内容分块（单年记录过长时自动拆分）

环境变量（也可写入 .env 文件）：
    LLM_API_KEY      API 密钥
    LLM_BASE_URL     API 地址（默认 https://api.minimax.chat/v1）
    LLM_MODEL        模型名称（默认 abab6.5s-chat）

用法：
    # 全量提取（约 224 个年份）
    python extract_famine_data_llm.py

    # 先测试前 20 年
    python extract_famine_data_llm.py --limit 20 --no-progress
"""

import argparse
import asyncio
import csv
import json
import os
import re
import sys

# 修复 Windows 终端中文乱码
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import threading
from dataclasses import dataclass
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
from tqdm import tqdm

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

CONCURRENT_REQUESTS = 5           # 默认并发数
REQUEST_TIMEOUT = 180             # 单次请求超时（秒）
MAX_RETRIES = 3                   # 失败重试次数
MAX_CONTENT_CHARS = 2500          # 单请求内容最大字符数，超长则拆分
MAX_TOKENS = 40000                # LLM 输出 token 上限（需预留 think 推理空间）

# CSV 写入锁（防止多进程/多实例并发写入导致行交错）
_CSV_LOCK = threading.Lock()


class LLMExtractError(Exception):
    """LLM 提取最终失败，所有重试均无效。"""

    def __init__(self, year_ce: int, year_era: str, message: str = ""):
        self.year_ce = year_ce
        self.year_era = year_era
        super().__init__(f"{year_era}({year_ce}) {message}")


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
SYSTEM_PROMPT = """你是一个历史数据提取专家，负责从明清时期灾荒记录中提取食人相关事件。你的输出必须严格遵循给定的 JSON 格式。思考过程请尽量简短，将主要输出空间留给 JSON 结果。

重要：在构造 JSON 时，若原始记录中包含双引号（" 或 " 或 "），请在 JSON 的 record 字段中将其替换为单引号（'），以确保 JSON 格式合法。"""


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
- record 字段中若原文包含双引号（" 或 " 或 "），请替换为单引号（'），确保 JSON 格式合法

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
    """单次 LLM 调用。"""
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

    last_raw = ""
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
                    last_raw = raw
                    result = parse_llm_response(raw, year_era, year_ce)
                    if result:
                        return result
                    # 解析为空但请求成功，继续重试
                    continue
            except Exception as e:
                tqdm.write(f"  [Retry {attempt}/{MAX_RETRIES}] {year_era}({year_ce}) request failed: {e}")
            if attempt < MAX_RETRIES:
                await asyncio.sleep(2 ** attempt)

    # 检查是否是 think 过程耗尽 token 导致无 JSON 输出
    if _is_think_exhausted(last_raw):
        tqdm.write(f"  [Retry] {year_era}({year_ce}) think tokens exhausted, forcing JSON output...")
        forced_payload = {
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": "你是一个历史数据提取专家。请直接输出 JSON 数组，不要输出思考过程。"},
                {"role": "user", "content": prompt + "\n\n【强制要求】请直接输出 JSON 数组，不要输出任何思考过程或解释。"},
            ],
            "temperature": 0.0,
            "max_tokens": MAX_TOKENS,
        }
        async with semaphore:
            try:
                async with session.post(
                    url,
                    headers=headers,
                    json=forced_payload,
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    raw = data["choices"][0]["message"]["content"]
                    return parse_llm_response(raw, year_era, year_ce)
            except Exception as e:
                tqdm.write(f"  [Skip] {year_era}({year_ce}) forced JSON request also failed: {e}")
                return []

    tqdm.write(f"  [Skip] {year_era}({year_ce}) final failure")
    raise LLMExtractError(year_ce, year_era)


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

    # 内容超长拆分，静默处理不打扰进度条
    all_records: list[dict] = []
    for idx, chunk_lines in enumerate(chunks, 1):
        chunk_content = "\n".join(chunk_lines)
        header = f"【{year_era}（{year_ce}年）记录共 {len(chunks)} 部分，此为第 {idx} 部分】"
        records = await _call_llm_single(session, semaphore, year_era, year_ce, header + "\n" + chunk_content)
        all_records.extend(records)

    return all_records


# ---------------------------------------------------------------------------
# 响应解析
# ---------------------------------------------------------------------------
def _save_debug_raw(year_ce: int, raw: str) -> None:
    """保存原始响应到 debug 目录。"""
    debug_dir = Path(__file__).parent / ".debug"
    debug_dir.mkdir(exist_ok=True)
    (debug_dir / f"{year_ce}_raw.txt").write_text(raw, encoding="utf-8")


def _is_think_exhausted(raw: str) -> bool:
    """检测 LLM 的 think 过程是否耗尽了 token，导致没有输出 JSON。"""
    if "<think>" not in raw or "</think>" not in raw:
        return False
    after_think = raw[raw.find("</think>") + 8:].strip()
    return not after_think


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
        tqdm.write(f"  [Format error] {year_era}({year_ce}): response is not an array")
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
                tqdm.write(f"  [Parse failed] {year_era}({year_ce}): JSON truncated and unrecoverable")
                _save_debug_raw(year_ce, raw)
                return []
    elif start != -1:
        # 有 [ 但没有 ]，尝试补全
        try:
            records = json.loads(text[start:] + "]")
        except json.JSONDecodeError:
            tqdm.write(f"  [Parse failed] {year_era}({year_ce}): JSON truncated and unrecoverable")
            _save_debug_raw(year_ce, raw)
            return []
    else:
        tqdm.write(f"  [Parse failed] {year_era}({year_ce}): no JSON array found")
        _save_debug_raw(year_ce, raw)
        return []

    if not isinstance(records, list):
        tqdm.write(f"  [格式错误] {year_era}({year_ce}): 返回不是数组")
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
def restore_record_from_source(record: str, content: str) -> str:
    """
    根据 LLM 返回的 record，在原始 content 中查找匹配的完整原文行。

    LLM 有时会省略 record 开头的地名前缀（如把"临汝县：大饥..."只返回"大饥..."），
    此函数通过核心内容匹配，从原始文本中找回完整的一行。
    """
    record = record.strip()
    if not record or record == "无":
        return record

    # 如果已经有"地名："前缀格式，认为已经完整
    if re.match(r"^[^：:\s]{1,15}[：:]", record):
        return record

    # 提取关键词：连续 2 个以上的中文字符
    keywords = re.findall(r"[一-鿿]{2,}", record)
    if not keywords:
        return record

    # 按长度排序，优先用长的
    keywords = sorted(set(keywords), key=len, reverse=True)

    lines = [l.strip() for l in content.splitlines() if l.strip()]

    # 优先用 >=3 字的关键词匹配，降低误匹配概率
    for min_len in (3, 2):
        for keyword in [k for k in keywords if len(k) >= min_len]:
            matches = [l for l in lines if keyword in l]
            if not matches:
                continue
            if len(matches) == 1:
                return matches[0]
            # 多个匹配时，尝试用更多关键词进一步过滤
            for kw2 in keywords:
                if kw2 != keyword and len(kw2) >= 2:
                    filtered = [l for l in matches if kw2 in l]
                    if len(filtered) == 1:
                        return filtered[0]
            # 仍有多个，返回最长的（通常包含完整前缀和来源）
            return max(matches, key=len)

    return record


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
class Progress:
    """进度数据，包含已完成年份和失败年份。"""

    def __init__(self, done_years: set[int] | None = None, failed_years: dict[int, str] | None = None):
        self.done_years: set[int] = done_years or set()
        self.failed_years: dict[int, str] = failed_years or {}  # year_ce -> era_name


def load_progress() -> Progress:
    if PROGRESS_FILE.exists():
        try:
            data = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
            return Progress(
                done_years=set(data.get("done_years", [])),
                failed_years={int(k): v for k, v in data.get("failed_years", {}).items()},
            )
        except Exception:
            pass
    return Progress()


def save_progress(progress: Progress):
    PROGRESS_FILE.write_text(
        json.dumps(
            {
                "done_years": sorted(progress.done_years),
                "failed_years": {str(k): v for k, v in sorted(progress.failed_years.items())},
            },
            ensure_ascii=False,
            indent=2,
        ),
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
        has_data = path.exists() and path.stat().st_size > 0
        if has_data:
            with open(path, "r", encoding="utf-8-sig") as f:
                reader = csv.reader(f)
                try:
                    next(reader)  # skip header
                except StopIteration:
                    pass
                existing_rows = sum(1 for _ in reader)

        need_header = not has_data
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
async def _process_one_year(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    era: str,
    ce: int,
    content: str,
    seen_keys: set[tuple],
    output_csv: Path,
) -> tuple[int, str, int, bool]:
    """处理单个年份，返回 (year_ce, year_era, valid_record_count, success)。"""
    try:
        raw_records = await call_llm_extract(session, semaphore, era, ce, content)
    except LLMExtractError:
        return ce, era, 0, False

    # 从原始内容中补全可能被 LLM 省略前缀的 record
    for rec in raw_records:
        if isinstance(rec, dict):
            original = rec.get("record", "")
            restored = restore_record_from_source(original, content)
            if restored != original:
                rec["record"] = restored

    records = [r for r in (validate_record(r) for r in raw_records) if r is not None]

    # 去重并写入 CSV
    new_records: list[Record] = []
    for rec in records:
        key = (rec.province, rec.city, rec.county, rec.year_ce, rec.source, rec.note, rec.record)
        if key not in seen_keys:
            seen_keys.add(key)
            new_records.append(rec)

    if new_records:
        write_csv(new_records, output_csv)

    return ce, era, len(records), True


def remove_years_from_csv(years_to_remove: set[int], path: Path):
    """从 CSV 中删除指定年份的所有记录，用于重试失败年份前去重。"""
    if not path.exists() or not years_to_remove:
        return
    temp_path = path.with_suffix(".csv.tmp")
    fieldnames = ["seq", "year_ce", "year_era", "province", "city", "county", "ancient_name", "source", "record", "note"]
    with open(path, "r", encoding="utf-8-sig") as f_in, \
         open(temp_path, "w", newline="", encoding="utf-8-sig") as f_out:
        reader = csv.DictReader(f_in)
        writer = csv.DictWriter(f_out, fieldnames=fieldnames)
        writer.writeheader()
        for row in reader:
            if int(row.get("year_ce", 0)) not in years_to_remove:
                writer.writerow(row)
    temp_path.replace(path)


async def main():
    parser = argparse.ArgumentParser(description="Extract famine cannibalism records from calibrated Markdown to CSV")
    parser.add_argument("--limit", type=int, default=None, help="Only process first N years (for testing)")
    parser.add_argument("--offset", type=int, default=0, help="Skip first N years before processing")
    parser.add_argument("--output", type=str, default=None, help="Custom output CSV path")
    parser.add_argument("--no-progress", action="store_true", help="Do not use progress file (test mode)")
    parser.add_argument("--restart", action="store_true", help="Reset progress and reprocess all years")
    parser.add_argument("--retry-failed", action="store_true", help="Only reprocess years that previously failed")
    parser.add_argument("--workers", type=int, default=CONCURRENT_REQUESTS, help=f"Concurrent API requests (default: {CONCURRENT_REQUESTS})")
    args = parser.parse_args()

    if not INPUT_FILE.exists():
        print(f"Error: input file not found: {INPUT_FILE}")
        sys.exit(1)

    if not LLM_API_KEY:
        print("Error: please set environment variable LLM_API_KEY")
        sys.exit(1)

    # 重置进度
    if args.restart and PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()
        print("Progress file reset")

    segments = extract_year_segments(INPUT_FILE)
    print(f"Parsed {len(segments)} year segments")

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

    progress = load_progress() if progress_file else Progress()
    if progress.done_years:
        print(f"{len(progress.done_years)} years already processed, will skip")
    if progress.failed_years:
        print(f"{len(progress.failed_years)} years previously failed: {sorted(progress.failed_years.keys())}")

    if args.retry_failed:
        # 只重试之前失败的年份
        if not progress.failed_years:
            print("No failed years to retry")
            return
        # 先从 CSV 中删除这些年份的旧记录，避免重复
        remove_years_from_csv(set(progress.failed_years.keys()), output_csv)
        pending = [(era, ce, content) for era, ce, content in segments if ce in progress.failed_years]
        print(f"Retrying {len(pending)} failed years")
    else:
        pending = [(era, ce, content) for era, ce, content in segments if ce not in progress.done_years]

    # 应用 offset 和 limit（在原始列表上切片，不受已处理年份影响）
    if args.offset:
        pending = pending[args.offset:]
        print(f"Skipped first {args.offset}, {len(pending)} remaining")
    if args.limit:
        pending = pending[:args.limit]
        print(f"[Test mode] Processing only first {args.limit} years")
    else:
        print(f"Pending {len(pending)} years")

    if not pending:
        print("All years processed")
        return

    workers = args.workers
    semaphore = asyncio.Semaphore(workers)
    seen_keys: set[tuple] = set()
    completed_years: set[int] = set()
    newly_failed: dict[int, str] = {}

    connector = aiohttp.TCPConnector(limit=workers)
    async with aiohttp.ClientSession(connector=connector) as session:
        # 创建所有任务，用 semaphore 控制并发
        tasks = [
            _process_one_year(session, semaphore, era, ce, content, seen_keys, output_csv)
            for era, ce, content in pending
        ]

        # 使用 as_completed 实现流水线：完成的任务立即处理，不等待同批其他任务
        pbar = tqdm(total=len(pending), desc="Processing years", unit="year")
        year_results: list[tuple[int, str, int, bool]] = []  # 收集结果用于最终汇总
        success_count = 0
        fail_count = 0
        for coro in asyncio.as_completed(tasks):
            ce, era, count, success = await coro
            completed_years.add(ce)
            year_results.append((ce, era, count, success))
            if success:
                success_count += 1
                progress.failed_years.pop(ce, None)
                # 用 postfix 实时显示最新完成的年份，不打断进度条
                pbar.set_postfix_str(f"[{ce}] {era}={count}  ok={success_count} fail={fail_count}")
            else:
                fail_count += 1
                newly_failed[ce] = era
                progress.failed_years[ce] = era
                # 失败信息仍然用 write 输出，确保用户能看到
                tqdm.write(f"  [{ce}] {era} -> FAILED")
            pbar.update(1)

            # 每完成 10 个年份保存一次进度（减少 IO 频率）
            if progress_file and len(completed_years) % 10 == 0:
                progress.done_years |= completed_years
                save_progress(progress)
        pbar.close()

    # 最终保存进度
    if progress_file:
        progress.done_years |= completed_years
        save_progress(progress)

    print(f"\nDone! Output file: {output_csv}")

    # 年份汇总（只显示有记录或失败的年份，方便快速核对）
    if year_results:
        year_results.sort(key=lambda x: x[0])
        summary_parts = []
        for ce, era, count, success in year_results:
            if not success:
                summary_parts.append(f"[{ce}] {era} -> FAILED")
            elif count > 0:
                summary_parts.append(f"[{ce}] {era} -> {count} records")
        if summary_parts:
            print("\nYear summary:")
            for part in summary_parts:
                print(f"  {part}")

    # 最终统计
    if output_csv.exists():
        with open(output_csv, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            next(reader)  # skip header
            total = sum(1 for _ in reader)
        print(f"\nTotal records: {total}")

    if newly_failed:
        print(f"Failed years this run: {sorted(newly_failed.keys())}")
        print("Run with --retry-failed to reprocess them")

    # 按年份排序并重写 CSV
    sort_csv_by_year(output_csv)

    # 全部完成后清理进度文件（只有正常全量跑且没有失败时才清理）
    all_years_done = len(progress.done_years | set(progress.failed_years.keys())) == len(segments)
    if progress_file and not progress.failed_years and all_years_done:
        PROGRESS_FILE.unlink(missing_ok=True)
        print("Progress file cleaned up")


def sort_csv_by_year(path: Path):
    """按 year_ce 排序并重写 CSV，同时重新编号 seq。"""
    if not path.exists():
        return

    fieldnames = ["seq", "year_ce", "year_era", "province", "city", "county",
                  "ancient_name", "source", "record", "note"]

    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if not rows:
        return

    # 按 year_ce 排序
    rows.sort(key=lambda r: int(r.get("year_ce", 0)))

    # 重新编号
    for i, row in enumerate(rows, 1):
        row["seq"] = i

    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"CSV sorted by year: {path}")


if __name__ == "__main__":
    asyncio.run(main())
