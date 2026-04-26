"""
从校准版 Markdown 文本中提取明清时期灾荒食人事件记录。

功能概览：
- 读取 `data/calibrated_text/明清时期灾荒食人现象研究_陈岭_校准版.md`
- 按年份分段，调用 LLM API 提取食人事件
- 解析 LLM 返回的 JSON，清洗并验证字段，写入 CSV
- 支持并发请求、失败重试、超长内容分块、进度持久化
- 支持按指定年份抽样、保存调试材料、重试历史失败年份

环境变量（也可写入仓库根目录 `.env`）：
    LLM_API_KEY      API 密钥，必填
    LLM_BASE_URL     API 地址，默认 https://api.minimax.chat/v1
    LLM_MODEL        模型名称，默认 abab6.5s-chat

默认输入输出：
    输入 Markdown:
        data/calibrated_text/明清时期灾荒食人现象研究_陈岭_校准版.md
    默认输出 CSV:
        processing/record_level_cleaning/ming_qing_famine_cannibalism_chen_ling/明清时期灾荒食人年表.csv
    进度文件:
        processing/scripts/.extract_progress.json
    调试目录:
        processing/scripts/.debug/

常见用法：
    1. 全量提取
       python extract_records_llm.py

    2. 只测试前 20 个年份，不写正式进度
       python extract_records_llm.py --limit 20 --no-progress

    3. 跳过前 50 个年份，继续看后面的结果
       python extract_records_llm.py --offset 50 --limit 20 --no-progress

    4. 只跑指定年份（适合单年检查或 prompt 调试）
       python extract_records_llm.py --sample-years 1556 --no-progress
       python extract_records_llm.py --sample-years 1556,1877 --no-progress

    5. 跑指定年份并保存调试材料
       python extract_records_llm.py --sample-years 1556,1877 --debug-sample --no-progress
       这会在 `processing/scripts/.debug/` 下保存：
       - prompt
       - 原始模型输出 raw response
       - 解析后的 JSON

    6. 重试历史失败年份
       python extract_records_llm.py --retry-failed
       失败年份来自 `processing/scripts/.extract_progress.json`

    7. 从头重跑全部年份
       python extract_records_llm.py --restart

    8. 指定输出文件，避免覆盖正式结果
       python extract_records_llm.py --sample-years 1556 --output result/test_1556.csv --no-progress

    9. 调整并发数
       python extract_records_llm.py --workers 2

参数说明：
    --limit N
        只处理前 N 个待处理年份，常用于小样本测试。
    --offset N
        跳过前 N 个待处理年份。
    --output PATH
        自定义输出 CSV 路径。
    --no-progress
        不读取也不写入进度文件，适合测试。
    --restart
        删除现有进度文件，从头重新处理全部年份。
    --retry-failed
        仅处理进度文件中记录为失败的年份。
    --workers N
        控制并发请求数。
    --sample-years 1644,1877
        只处理指定公元年份，可传一个或多个，用逗号分隔。
    --debug-sample
        保存 prompt/raw/parsed 调试材料，通常与 `--sample-years` 搭配。

进度文件说明：
- 正常全量运行时，会把成功年份和失败年份写入 `.extract_progress.json`
- 使用 `--limit`、`--offset`、`--sample-years` 这类测试模式时，建议同时加 `--no-progress`
- 若某些年份失败，可先检查 `.debug/`，再用 `--retry-failed` 重跑
"""

import argparse
import asyncio
import csv
import json
import os
import re
import sys
import time

# 修复 Windows 终端中文乱码
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import threading
from dataclasses import dataclass, field
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
INPUT_FILE = Path(__file__).parent.parent.parent / "data" / "calibrated_text" / "明清时期灾荒食人现象研究_陈岭_校准版.md"
OUTPUT_CSV = Path(__file__).parent.parent.parent / "processing" / "record_level_cleaning" / "ming_qing_famine_cannibalism_chen_ling" / "明清时期灾荒食人年表.csv"
PROGRESS_FILE = Path(__file__).parent / ".extract_progress.json"
DEBUG_DIR = Path(__file__).parent / ".debug"

LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.minimax.chat/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "abab6.5s-chat")

CONCURRENT_REQUESTS = 8           # 默认并发数
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


@dataclass
class LLMDebugBundle:
    year_ce: int
    year_era: str
    prompt: str
    raw_response: str
    parsed_records: list[dict] | None


@dataclass
class RequestTiming:
    attempts: int = 0
    request_seconds: float = 0.0
    retry_sleep_seconds: float = 0.0
    parse_failures: int = 0
    forced_json_used: bool = False
    forced_json_success: bool = False
    forced_json_seconds: float = 0.0


@dataclass
class ChunkTiming:
    index: int
    content_chars: int
    elapsed_seconds: float = 0.0
    request_timing: RequestTiming = field(default_factory=RequestTiming)


@dataclass
class YearTiming:
    year_ce: int
    year_era: str
    total_seconds: float = 0.0
    chunk_timings: list[ChunkTiming] = field(default_factory=list)
    restored_records: int = 0
    valid_records: int = 0
    written_records: int = 0

    @property
    def chunk_count(self) -> int:
        return len(self.chunk_timings)

    @property
    def total_attempts(self) -> int:
        return sum(chunk.request_timing.attempts for chunk in self.chunk_timings)

    @property
    def total_retries(self) -> int:
        return sum(max(chunk.request_timing.attempts - 1, 0) for chunk in self.chunk_timings)

    @property
    def total_request_seconds(self) -> float:
        return sum(chunk.request_timing.request_seconds for chunk in self.chunk_timings)

    @property
    def total_retry_sleep_seconds(self) -> float:
        return sum(chunk.request_timing.retry_sleep_seconds for chunk in self.chunk_timings)

    @property
    def total_parse_failures(self) -> int:
        return sum(chunk.request_timing.parse_failures for chunk in self.chunk_timings)

    @property
    def forced_json_used(self) -> bool:
        return any(chunk.request_timing.forced_json_used for chunk in self.chunk_timings)


# ---------------------------------------------------------------------------
# System Prompt
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """你是历史文本结构化抽取助手。你的任务是从给定史料中抽取“明确发生食人行为”的记录，并输出为 JSON 数组。

硬性要求：
1. 只输出 JSON 数组，不输出解释、分析、思考过程、markdown 代码块。
2. 每个元素只能包含以下字段：
   province, city, county, ancient_name, year_ce, year_era, source, note, record
3. 所有字段值都必须可被 JSON 正确解析；其中 year_ce 必须是整数。
4. 无符合条件记录时，返回 []。
5. 不确定时填“无”，不要猜测。
6. 除非规则明确允许，否则不要根据常识补全缺失地名。
7. record 必须尽量保留原文整行；若含双引号，统一改成单引号。"""


# ---------------------------------------------------------------------------
# User Prompt 模板
# ---------------------------------------------------------------------------
USER_PROMPT_TEMPLATE = """请处理以下 {year_era}（{year_ce}年）史料，抽取其中所有“明确发生食人行为”的记录。

请严格按下面顺序判断：

第一步：判断是否提取
- 仅当原文明确出现食人行为时才提取，例如：`人相食`、`以人为食`、`食人肉`、`食尸`、`人食人`。
- 如果只是灾荒、饥饿、逃荒、死亡，而没有明确食人行为，不提取。

第二步：判断是否拆分
- 如果同一行中列出多个具体县名，且这些县都对应同一食人事件，应拆成多条记录，每条记录对应一个县。
- 如果只是泛指多个省、多个府、多个地区，没有落实到具体县级地点，则不提取。
- 如果是“某府，下属若干具体县发生人相食”，则只提取具体县，不提取泛指的府名。

第三步：填写字段
- province：
  只写省名简称，如“河南”“山东”。
  直辖市填“北京”“上海”“天津”“重庆”。
  若无法确定，填“无”。
- city：
  只有原文明确出现市名时才填写。
  不得根据县名反推市名。
  若原文没有明确市名，填“无”。
- county：
  原文明确出现的县、州、府或其他最具体地点名称，填入这里。
  不要自行补全。
  若无，填“无”。
- ancient_name：
  仅当原文出现现代已不用的古地名时填写该古地名本身，否则填“无”。
- source：
  只保留书名。
  去掉卷次、页码、年号、编者说明等附属信息。
  若为转引，写成“原书名转引后书名”。
- note：
  仅在以下情况填写，否则填“无”：
  1. 疑似 OCR 错误
  2. 地名存在不确定性
  3. 做了古地名识别说明
- record：
  保留该条记录对应的原文整行，尽量完整，不要省略开头地名和结尾来源。
  若含双引号，改为单引号。

关于古地名：
- ancient_name 只填原文中的古地名本身。
- 若古地名无法稳定映射到现代行政区，不要强行映射，province、city、county 可填“无”。
- 若你非常确定映射关系，也只能在不违背“不要猜测”原则时填写，并在 note 中注明“根据古地名推定”。
- 现代仍在用的地名，不记为 ancient_name。

特殊地名可按保守规则处理：
- `都下`、`京师`、`北畿`可按北京处理；若你认为仍不够确定，可只在 note 中说明并将相关字段填“无”。
- `秦`、`晋`这类单字泛称，若上下文不能唯一确定，不要强行映射。

正反例：
- `大饥，民多流亡` -> 不提取
- `人相食` -> 提取
- `某府属甲县、乙县、丙县人相食` -> 拆成 3 条
- `山东、河南、湖广饥，人相食` -> 泛指，不提取

输出要求：
- 只输出 JSON 数组，不要任何额外文字
- 不要输出 markdown 代码块
- 不要增加任何额外字段
- year_ce 固定写 {year_ce}
- year_era 固定写 {year_era}
- 无符合条件记录时返回 []

以下是 {year_era}（{year_ce}年）的记录：

{content}
"""


# ---------------------------------------------------------------------------
# 分段解析
# ---------------------------------------------------------------------------
YEAR_PATTERN = re.compile(
    r"^###\s+(?P<era>.+?)\s*[（(](?P<year>\d{1,4})年[）)]"
)


def format_seconds(seconds: float) -> str:
    return f"{seconds:.1f}s"


def summarize_year_timing(timing: YearTiming) -> str:
    summary = [
        f"t={format_seconds(timing.total_seconds)}",
        f"chunks={timing.chunk_count}",
    ]
    if timing.total_retries:
        summary.append(f"retries={timing.total_retries}")
    if timing.total_parse_failures:
        summary.append(f"parse_fail={timing.total_parse_failures}")
    if timing.forced_json_used:
        summary.append("forced_json=1")
    return " ".join(summary)


def should_log_year_timing(timing: YearTiming) -> bool:
    return (
        timing.total_seconds >= 15
        or timing.chunk_count > 1
        or timing.total_retries > 0
        or timing.total_parse_failures > 0
        or timing.forced_json_used
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
    """将内容中的各类双引号替换为单引号，避免 JSON 因未转义引号而解析失败。"""
    return (
        text.replace('"', "'")
        .replace("“", "'")
        .replace("”", "'")
        .replace("‟", "'")
    )


async def _call_llm_single(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    year_era: str,
    year_ce: int,
    content: str,
    debug_dir: Path | None = None,
    timing: RequestTiming | None = None,
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
        "temperature": 0.0,
        "max_tokens": MAX_TOKENS,
    }

    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }

    url = f"{LLM_BASE_URL}/chat/completions"

    last_raw = ""
    for attempt in range(1, MAX_RETRIES + 1):
        if timing:
            timing.attempts += 1
        async with semaphore:
            try:
                request_started = time.perf_counter()
                async with session.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    if timing:
                        timing.request_seconds += time.perf_counter() - request_started
                    raw = data["choices"][0]["message"]["content"]
                    last_raw = raw
                    result = parse_llm_response(raw, year_era, year_ce)
                    if debug_dir:
                        save_debug_bundle(
                            LLMDebugBundle(
                                year_ce=year_ce,
                                year_era=year_era,
                                prompt=prompt,
                                raw_response=raw,
                                parsed_records=result,
                            ),
                            debug_dir,
                        )
                    if result is not None:
                        return result
                    if timing:
                        timing.parse_failures += 1
                    # 解析失败时重试；空数组 [] 也是合法结果
                    continue
            except Exception as e:
                tqdm.write(f"  [Retry {attempt}/{MAX_RETRIES}] {year_era}({year_ce}) request failed: {e}")
            if attempt < MAX_RETRIES:
                sleep_seconds = 2 ** attempt
                if timing:
                    timing.retry_sleep_seconds += sleep_seconds
                await asyncio.sleep(sleep_seconds)

    # 检查是否是 think 过程耗尽 token 导致无 JSON 输出
    if _is_think_exhausted(last_raw):
        tqdm.write(f"  [Retry] {year_era}({year_ce}) think tokens exhausted, forcing JSON output...")
        if timing:
            timing.forced_json_used = True
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
                forced_started = time.perf_counter()
                async with session.post(
                    url,
                    headers=headers,
                    json=forced_payload,
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    if timing:
                        timing.forced_json_seconds += time.perf_counter() - forced_started
                    raw = data["choices"][0]["message"]["content"]
                    result = parse_llm_response(raw, year_era, year_ce)
                    if timing and result is not None:
                        timing.forced_json_success = True
                    if debug_dir:
                        save_debug_bundle(
                            LLMDebugBundle(
                                year_ce=year_ce,
                                year_era=year_era,
                                prompt=prompt,
                                raw_response=raw,
                                parsed_records=result,
                            ),
                            debug_dir,
                            suffix="_forced",
                        )
                    return result if result is not None else []
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
    debug_dir: Path | None = None,
    timing: YearTiming | None = None,
) -> list[dict]:
    """
    调用 LLM API 提取单年份的食人记录。
    若内容超长，按行拆分为多个 chunk 分别调用，最后合并结果。
    """
    if len(content) <= MAX_CONTENT_CHARS:
        chunk_timing = ChunkTiming(index=1, content_chars=len(content))
        if timing:
            timing.chunk_timings.append(chunk_timing)
        chunk_started = time.perf_counter()
        records = await _call_llm_single(
            session,
            semaphore,
            year_era,
            year_ce,
            content,
            debug_dir=debug_dir,
            timing=chunk_timing.request_timing,
        )
        chunk_timing.elapsed_seconds = time.perf_counter() - chunk_started
        return records

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
        chunk_debug_dir = None
        if debug_dir:
            chunk_debug_dir = debug_dir / f"{year_ce}_chunk_{idx}"
        chunk_timing = ChunkTiming(index=idx, content_chars=len(chunk_content))
        if timing:
            timing.chunk_timings.append(chunk_timing)
        chunk_started = time.perf_counter()
        records = await _call_llm_single(
            session,
            semaphore,
            year_era,
            year_ce,
            header + "\n" + chunk_content,
            debug_dir=chunk_debug_dir,
            timing=chunk_timing.request_timing,
        )
        chunk_timing.elapsed_seconds = time.perf_counter() - chunk_started
        all_records.extend(records)

    return all_records


# ---------------------------------------------------------------------------
# 响应解析
# ---------------------------------------------------------------------------
def _save_debug_raw(year_ce: int, raw: str) -> None:
    """保存原始响应到 debug 目录。"""
    debug_dir = DEBUG_DIR
    debug_dir.mkdir(exist_ok=True)
    (debug_dir / f"{year_ce}_raw.txt").write_text(raw, encoding="utf-8")


def save_debug_bundle(bundle: LLMDebugBundle, debug_dir: Path, suffix: str = "") -> None:
    """保存 prompt、原始响应和解析结果，便于比较不同提示词效果。"""
    debug_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{bundle.year_ce}_{bundle.year_era}{suffix}"
    (debug_dir / f"{stem}_prompt.txt").write_text(bundle.prompt, encoding="utf-8")
    (debug_dir / f"{stem}_raw.txt").write_text(bundle.raw_response, encoding="utf-8")
    parsed_text = (
        "PARSE_FAILED"
        if bundle.parsed_records is None
        else json.dumps(bundle.parsed_records, ensure_ascii=False, indent=2)
    )
    (debug_dir / f"{stem}_parsed.json").write_text(parsed_text, encoding="utf-8")


def _is_think_exhausted(raw: str) -> bool:
    """检测 LLM 的 think 过程是否耗尽了 token，导致没有输出 JSON。"""
    if "<think>" not in raw or "</think>" not in raw:
        return False
    after_think = raw[raw.find("</think>") + 8:].strip()
    return not after_think


def parse_llm_response(raw: str, year_era: str, year_ce: int) -> list[dict] | None:
    """
    从 LLM 返回的文本中解析 JSON 数组。
    返回 None 表示解析失败；返回 [] 表示模型明确返回空数组。
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
        return None

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
                return None
    elif start != -1:
        # 有 [ 但没有 ]，尝试补全
        try:
            records = json.loads(text[start:] + "]")
        except json.JSONDecodeError:
            tqdm.write(f"  [Parse failed] {year_era}({year_ce}): JSON truncated and unrecoverable")
            _save_debug_raw(year_ce, raw)
            return None
    else:
        tqdm.write(f"  [Parse failed] {year_era}({year_ce}): no JSON array found")
        _save_debug_raw(year_ce, raw)
        return None

    if not isinstance(records, list):
        tqdm.write(f"  [格式错误] {year_era}({year_ce}): 返回不是数组")
        return None

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
    source = normalize_source(str(raw.get("source", "")).strip() or "无")
    note = normalize_free_text(str(raw.get("note", "")).strip() or "无")
    record = normalize_record_text(str(raw.get("record", "")).strip() or "无")

    # province 过长可能是整段描述误入（中国省份最长约 9 字）
    if len(province) > 15:
        # province 过长可能是整段描述误入（中国省份最长约 9 字）
        return None

    # 核心字段缺失时丢弃，避免写入空壳记录
    if source == "无" or record == "无":
        return None

    return Record(
        province=province,
        city=normalize_place_field(city),
        county=normalize_place_field(county),
        ancient_name=normalize_place_field(ancient_name),
        year_ce=year_ce,
        year_era=year_era,
        source=source,
        note=note,
        record=record,
    )


def normalize_free_text(value: str) -> str:
    """压缩多余空白，保留原始信息。"""
    value = re.sub(r"\s+", " ", value).strip()
    return value or "无"


def normalize_place_field(value: str) -> str:
    """统一地点字段中的空值表达和多余空白。"""
    value = normalize_free_text(value)
    if value in {"", "无", "未知", "不详", "未详", "缺"}:
        return "无"
    return value


def normalize_source(value: str) -> str:
    """清洗来源字段中的卷次、页码和常见附属说明。"""
    value = normalize_free_text(value)
    if value == "无":
        return value

    value = re.sub(r"[，,、；;]?\s*第?\d+\s*页.*$", "", value)
    value = re.sub(r"\s*卷[一二三四五六七八九十百千0-9]+[^\s，,、；;]*", "", value)
    value = re.sub(r"\s*（[^）]*年[^）]*）", "", value)
    value = re.sub(r"\s*\([^)]*year[^)]*\)", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", "", value)

    if "转引" in value:
        parts = [p for p in re.split(r"\s*转引(?:自)?\s*", value) if p]
        cleaned_parts = [extract_book_title(p) for p in parts]
        cleaned_parts = [p for p in cleaned_parts if p != "无"]
        return "转引".join(cleaned_parts) if cleaned_parts else "无"

    return extract_book_title(value)


def extract_book_title(value: str) -> str:
    titles = re.findall(r"《[^》]+》", value)
    if titles:
        return "、".join(dict.fromkeys(titles))
    return value if value and len(value) <= 30 else "无"


def normalize_record_text(value: str) -> str:
    """统一 record 中的引号和空白，尽量保持原文。"""
    value = normalize_free_text(_sanitize_quotes(value))
    if value in {"", "无"}:
        return "无"
    return value


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
        has_data = path.exists() and path.stat().st_size > 0
        need_header = not has_data
        with open(path, "a", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if need_header:
                writer.writeheader()
            for rec in records:
                row = {
                    # Renumber seq later in sort_csv_by_year().
                    "seq": "",
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
    debug_dir: Path | None = None,
) -> tuple[int, str, int, bool, YearTiming]:
    """处理单个年份，返回 (year_ce, year_era, valid_record_count, success)。"""
    year_timing = YearTiming(year_ce=ce, year_era=era)
    year_started = time.perf_counter()
    try:
        raw_records = await call_llm_extract(
            session,
            semaphore,
            era,
            ce,
            content,
            debug_dir=debug_dir,
            timing=year_timing,
        )
    except LLMExtractError:
        year_timing.total_seconds = time.perf_counter() - year_started
        return ce, era, 0, False, year_timing

    # 从原始内容中补全可能被 LLM 省略前缀的 record
    for rec in raw_records:
        if isinstance(rec, dict):
            original = rec.get("record", "")
            restored = restore_record_from_source(original, content)
            if restored != original:
                rec["record"] = restored
                year_timing.restored_records += 1

    records = [r for r in (validate_record(r) for r in raw_records) if r is not None]
    year_timing.valid_records = len(records)

    # 去重并写入 CSV
    new_records: list[Record] = []
    for rec in records:
        key = (rec.province, rec.city, rec.county, rec.year_ce, rec.source, rec.note, rec.record)
        if key not in seen_keys:
            seen_keys.add(key)
            new_records.append(rec)

    if new_records:
        write_csv(new_records, output_csv)
    year_timing.written_records = len(new_records)
    year_timing.total_seconds = time.perf_counter() - year_started

    return ce, era, len(records), True, year_timing


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
    parser.add_argument("--sample-years", type=str, default=None, help="Comma-separated CE years to process, e.g. 1644,1877")
    parser.add_argument("--debug-sample", action="store_true", help="Save prompt/raw/parsed outputs for sampled years under processing/scripts/.debug")
    parser.add_argument("--verbose-timing", action="store_true", help="Print detailed per-year and per-chunk timing diagnostics")
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

    sample_years: set[int] | None = None
    if args.sample_years:
        try:
            sample_years = {int(part.strip()) for part in args.sample_years.split(",") if part.strip()}
        except ValueError:
            print("Error: --sample-years must be a comma-separated list of integers")
            sys.exit(1)

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

    if sample_years is not None:
        pending = [(era, ce, content) for era, ce, content in pending if ce in sample_years]
        print(f"[Sample mode] Processing years: {sorted(sample_years)}")

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
    successful_years: set[int] = set()
    newly_failed: dict[int, str] = {}
    year_timings: list[YearTiming] = []
    saved_success_checkpoint = 0

    connector = aiohttp.TCPConnector(limit=workers)
    async with aiohttp.ClientSession(connector=connector) as session:
        # 创建所有任务，用 semaphore 控制并发
        tasks = [
            _process_one_year(
                session,
                semaphore,
                era,
                ce,
                content,
                seen_keys,
                output_csv,
                debug_dir=(DEBUG_DIR / str(ce)) if args.debug_sample and (sample_years is None or ce in sample_years) else None,
            )
            for era, ce, content in pending
        ]

        # 使用 as_completed 实现流水线：完成的任务立即处理，不等待同批其他任务
        pbar = tqdm(total=len(pending), desc="Processing years", unit="year")
        year_results: list[tuple[int, str, int, bool]] = []  # 收集结果用于最终汇总
        success_count = 0
        fail_count = 0
        for coro in asyncio.as_completed(tasks):
            ce, era, count, success, year_timing = await coro
            year_timings.append(year_timing)
            year_results.append((ce, era, count, success))
            if success:
                successful_years.add(ce)
                success_count += 1
                progress.failed_years.pop(ce, None)
                # 用 postfix 实时显示最新完成的年份，不打断进度条
                pbar.set_postfix_str(
                    f"[{ce}] {era}={count} {summarize_year_timing(year_timing)} ok={success_count} fail={fail_count}"
                )
                if args.verbose_timing or should_log_year_timing(year_timing):
                    tqdm.write(
                        f"  [Timing] [{ce}] {era} ok records={count} "
                        f"written={year_timing.written_records} restored={year_timing.restored_records} "
                        f"{summarize_year_timing(year_timing)}"
                    )
                    if args.verbose_timing:
                        for chunk in year_timing.chunk_timings:
                            tqdm.write(
                                f"    chunk {chunk.index}/{year_timing.chunk_count}: chars={chunk.content_chars} "
                                f"elapsed={format_seconds(chunk.elapsed_seconds)} "
                                f"requests={chunk.request_timing.attempts} "
                                f"retry_sleep={format_seconds(chunk.request_timing.retry_sleep_seconds)} "
                                f"forced_json={'yes' if chunk.request_timing.forced_json_used else 'no'}"
                            )
            else:
                fail_count += 1
                newly_failed[ce] = era
                progress.failed_years[ce] = era
                # 失败信息仍然用 write 输出，确保用户能看到
                tqdm.write(f"  [{ce}] {era} -> FAILED")
                tqdm.write(f"  [Timing] [{ce}] {era} failed {summarize_year_timing(year_timing)}")
            pbar.update(1)

            # 每完成 10 个年份保存一次进度（减少 IO 频率）
            if progress_file and len(successful_years) - saved_success_checkpoint >= 10:
                progress.done_years |= successful_years
                save_progress(progress)
                saved_success_checkpoint = len(successful_years)
        pbar.close()

    # 最终保存进度
    if progress_file:
        final_progress_started = time.perf_counter()
        print("Saving final progress...")
        progress.done_years |= successful_years
        save_progress(progress)
        print(f"Final progress saved in {format_seconds(time.perf_counter() - final_progress_started)}")

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
        count_started = time.perf_counter()
        print("Counting total records...")
        with open(output_csv, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            next(reader)  # skip header
            total = sum(1 for _ in reader)
        print(f"\nTotal records: {total}")
        print(f"Counted total records in {format_seconds(time.perf_counter() - count_started)}")

    if newly_failed:
        print(f"Failed years this run: {sorted(newly_failed.keys())}")
        print("Run with --retry-failed to reprocess them")

    # 按年份排序并重写 CSV
    sort_started = time.perf_counter()
    print("Sorting and rewriting CSV...")
    sort_csv_by_year(output_csv)
    print(f"Sorted and rewrote CSV in {format_seconds(time.perf_counter() - sort_started)}")

    if year_timings:
        slowest_years = sorted(year_timings, key=lambda item: item.total_seconds, reverse=True)[:5]
        print("\nSlowest years:")
        for timing in slowest_years:
            print(
                f"  [{timing.year_ce}] {timing.year_era}: "
                f"{summarize_year_timing(timing)} written={timing.written_records}"
            )

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
