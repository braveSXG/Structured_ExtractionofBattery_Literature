#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
01_ZJ_lowerLetter_batch.py — 基于 DeepSeek API 的 DOI 批量推理

功能：
  - 读取 CSV 中 DOI 列表 → 查找对应 .md 论文全文 → 调用 ZJ DeepSeek API → 输出结果
  - 保持与 01_lowerLetter_batch_bailian_ds.py 相同的输入输出格式
  - 使用 DeepSeek OpenAI 兼容接口、dotenv 与指数退避重试

特性：
  - DOI 大小写不敏感匹配
  - 单文件断点续算（output_file 存在即跳过）
  - done_doi.txt 记录已完成 DOI
  - RateLimiter 限流（每分钟最大请求数）
  - 指数退避重试（APIError / 超时 / 连接错误）

运行：
  nohup python -u 01_ZJ_lowerLetter_batch_bailian.py > ZJ_threeProperty.log 2>&1 &
"""

import os
import re
import csv
import json
import time
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

from tqdm import tqdm
from openai import OpenAI, APIError, APITimeoutError, APIConnectionError
from dotenv import load_dotenv
from model_response import read_response_text
from ssl_trust import enable_system_trust_store
from structured_model import create_structured_completion

enable_system_trust_store()


# =========================
# 路径配置
# =========================
APP_DIR = Path(__file__).resolve().parent
MD_ROOT = APP_DIR / "input" / "papers"
CSV_PATH = APP_DIR / "input" / "doi.csv"
PROMPT_PATH = APP_DIR / "0623prompt_jianjie.md"
API_KEY_ENV_FILE = APP_DIR / ".env"
OUTPUT_DIR = APP_DIR / "output" / "standalone_step01"

# =========================
# API 配置
# =========================
BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"

# =========================
# 并发、限流、重试配置
# =========================
MAX_WORKERS = 10
MAX_RETRIES = 3
REQUEST_TIMEOUT = 1800
MAX_OUTPUT_TOKENS = 32000

MAX_REQUESTS_PER_MINUTE = 20
RATE_LIMIT_WINDOW = 60.0

MAX_MD_CHARS = 150000

# app.py 会在此标记后注入本次任务的路径、密钥、模型和并发配置。
# APP_CONFIG_OVERRIDE_POINT

# =========================
# 输出文件
# =========================
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
RESULT_JSONL = OUTPUT_DIR / "results.jsonl"
ERROR_JSONL = OUTPUT_DIR / "errors.jsonl"
DONE_DOI_TXT = OUTPUT_DIR / "done_doi.txt"
MD_INDEX_JSON = OUTPUT_DIR / "md_index.json"
RUN_LOG = OUTPUT_DIR / "run.log"
SUMMARY_JSON = OUTPUT_DIR / "summary.json"

# =========================
# 全局锁与计数器
# =========================
write_lock = threading.Lock()
done_lock = threading.Lock()
count_lock = threading.Lock()

success_count = 0
fail_count = 0

OPENAI_API_KEY = None


# =========================
# 日志
# =========================
def setup_logging() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.FileHandler(RUN_LOG, encoding="utf-8"),
            logging.StreamHandler()
        ]
    )


# =========================
# 限流器
# =========================
class RateLimiter:
    """简单线程安全限流器"""

    def __init__(self, max_requests: int, window_seconds: float):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.request_times = []
        self.lock = threading.Lock()

    def acquire(self):
        while True:
            with self.lock:
                now = time.time()
                self.request_times = [
                    t for t in self.request_times
                    if now - t < self.window_seconds
                ]
                if len(self.request_times) < self.max_requests:
                    self.request_times.append(now)
                    return
                oldest = min(self.request_times)
                sleep_time = self.window_seconds - (now - oldest) + 0.05
            if sleep_time > 0:
                time.sleep(sleep_time)


rate_limiter = RateLimiter(
    max_requests=MAX_REQUESTS_PER_MINUTE,
    window_seconds=RATE_LIMIT_WINDOW
)


# =========================
# 基础工具函数
# =========================
def normalize_doi(text: str) -> str:
    if text is None:
        return ""
    s = str(text).strip()
    s = s.replace("\\", "/")
    s = re.sub(r"^https?://(dx\.)?doi\.org/", "", s, flags=re.I)
    s = s.strip().strip('"').strip("'")
    return s.lower()


def safe_filename_variants(doi: str) -> List[str]:
    doi = normalize_doi(doi)
    variants = set()
    variants.add(doi)
    variants.add(doi.replace("/", "_"))
    variants.add(doi.replace("/", "@"))
    variants.add(doi.replace("/", "#"))
    variants.add(doi.replace("/", "／"))
    variants.add(doi.replace("/", "-"))
    cleaned = re.sub(r'[<>:"|?*]', "_", doi)
    variants.add(cleaned)
    variants.add(cleaned.replace("/", "_"))
    variants.add(cleaned.replace("/", "@"))
    variants.add(cleaned.replace("/", "#"))
    variants.add(cleaned.replace("/", "-"))
    return [x for x in variants if x]


def safe_output_name(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", text)


def make_output_filename(doi: str) -> str:
    prompt_name = PROMPT_PATH.stem
    return f"{safe_output_name(normalize_doi(doi))}__{safe_output_name(prompt_name)}.json"


def read_text(path: Path) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def read_prompt(path: Path) -> str:
    return read_text(path)


def read_doi_csv(path: Path) -> List[str]:
    dois = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if "doi" not in reader.fieldnames:
            raise ValueError(f"{path} 中未找到 doi 列，当前列为: {reader.fieldnames}")
        for row in reader:
            doi = normalize_doi(row.get("doi", ""))
            if doi:
                dois.append(doi)
    seen = set()
    unique = []
    for d in dois:
        if d not in seen:
            seen.add(d)
            unique.append(d)
    return unique


def load_done_dois(path: Path) -> set:
    if not path.exists():
        return set()
    done = set()
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            d = normalize_doi(line.strip())
            if d:
                done.add(d)
    return done


def append_done_doi(path: Path, doi: str) -> None:
    with done_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(normalize_doi(doi) + "\n")


def append_jsonl(path: Path, item: dict) -> None:
    with write_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")


def truncate_text(text: str, max_chars: int = MAX_MD_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    head = int(max_chars * 0.85)
    tail = max_chars - head
    return text[:head] + "\n\n[TEXT_TRUNCATED]\n\n" + text[-tail:]


# =========================
# md 索引
# =========================
def build_md_index(md_root: Path, cache_json: Path) -> Dict[str, str]:
    if cache_json.exists():
        with open(cache_json, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {normalize_doi(k): v for k, v in data.items()}

    logging.info("开始扫描 md 文件并建立索引...")
    md_files = list(md_root.rglob("*.md"))
    index: Dict[str, str] = {}

    for fp in tqdm(md_files, desc="索引 md 文件", unit="file"):
        stem = fp.stem.strip()
        norm_stem = normalize_doi(stem)
        if norm_stem and norm_stem not in index:
            index[norm_stem] = str(fp)
        candidates = {
            stem.replace("_", "/"),
            stem.replace("@", "/"),
            stem.replace("#", "/"),
            stem.replace("／", "/"),
            stem.replace("-", "/"),
        }
        for c in candidates:
            nc = normalize_doi(c)
            if nc and nc not in index:
                index[nc] = str(fp)

    with open(cache_json, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    logging.info("md 索引完成，共 %d 条", len(index))
    return index


def find_md_path_for_doi(doi: str, md_root: Path, md_index: Dict[str, str]) -> Optional[Path]:
    doi = normalize_doi(doi)
    if doi in md_index:
        p = Path(md_index[doi])
        if p.exists():
            return p
    for name in safe_filename_variants(doi):
        p = md_root / f"{name}.md"
        if p.exists():
            return p
    variants_lower = {name.lower() for name in safe_filename_variants(doi)}
    for p in md_root.rglob("*.md"):
        if p.stem.lower() in variants_lower:
            return p
    return None


def read_md_content(md_path: Path) -> str:
    with open(md_path, "r", encoding="utf-8") as f:
        text = f.read()
    return truncate_text(text, MAX_MD_CHARS)


# =========================
# API Key 加载（参照 zj_batch_QA_xin.py）
# =========================
def get_api_key(api_key_env_file: Path) -> str:
    api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
    if api_key:
        return api_key
    if not api_key_env_file.exists():
        raise FileNotFoundError(f"API_KEY_ENV_FILE 不存在: {api_key_env_file}")
    load_dotenv(dotenv_path=api_key_env_file, override=True, encoding="utf-8")
    api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        raise ValueError(
            f"未找到 DASHSCOPE_API_KEY，请检查 env 文件内容: {api_key_env_file}"
        )
    return api_key


# =========================
# API 调用（参照 zj_batch_QA_xin.py 的 call_model_json）
# =========================
def make_messages(prompt_text: str, md_text: str) -> List[dict]:
    return [
        {
            "role": "user",
            "content": f"{prompt_text}\n\n# paper content\n{md_text}"
        }
    ]


def call_model(
    model: str,
    messages: List[Dict[str, str]],
    request_timeout: int,
    max_retries: int,
    doi_label: str = "",
) -> Tuple[str, Dict[str, Any]]:
    """
    参照 zj_batch_QA_xin.py 的调用方式：
    - client 在函数内创建，max_retries=0
    - 指数退避 2**attempt
    - 捕获 APIConnectionError / APITimeoutError / APIError
    """
    client = OpenAI(
        base_url=BASE_URL,
        api_key=OPENAI_API_KEY,
        max_retries=0,
    )

    last_err = ""

    for attempt in range(1, max_retries + 1):
        try:
            # 限流器在每次请求前 acquire
            rate_limiter.acquire()

            resp = create_structured_completion(
                client,
                model=model,
                messages=messages,
                temperature=0,
                max_tokens=MAX_OUTPUT_TOKENS,
                timeout=request_timeout,
            )
            raw_text, response_diagnostic = read_response_text(resp)
            # 验证响应不为空
            if not raw_text.strip():
                raise ValueError(f"模型返回 content 为空（{response_diagnostic}），准备重试")

            meta = {
                "id": getattr(resp, "id", None),
                "model": getattr(resp, "model", model),
                "usage": getattr(resp, "usage", None).model_dump() if getattr(resp, "usage", None) else None,
                "attempt": attempt,
                "response_diagnostic": response_diagnostic,
            }
            return raw_text, meta

        except (APIConnectionError, APITimeoutError, APIError) as e:
            last_err = repr(e)
            logging.warning("%s 第 %d/%d 次调用失败: %s", doi_label, attempt, max_retries, e)
            if attempt < max_retries:
                time.sleep(2 ** attempt)

        except Exception as e:
            last_err = repr(e)
            logging.warning("%s 第 %d/%d 次未知错误: %s", doi_label, attempt, max_retries, repr(e))
            if attempt < max_retries:
                time.sleep(2 ** attempt)

    raise RuntimeError(f"API调用失败：{doi_label}。Last error={last_err}")


# =========================
# 单 DOI 处理
# =========================
def process_one_doi(
    doi: str,
    prompt_text: str,
    md_root: Path,
    md_index: Dict[str, str],
    model: str,
) -> None:
    global success_count, fail_count

    doi = normalize_doi(doi)
    output_file = OUTPUT_DIR / make_output_filename(doi)

    # 单文件断点续算
    if output_file.exists():
        with count_lock:
            success_count += 1
        return

    md_path = find_md_path_for_doi(doi, md_root, md_index)
    if md_path is None:
        append_jsonl(ERROR_JSONL, {
            "doi": doi,
            "error_type": "md_not_found",
            "message": "未找到对应 md 文件"
        })
        with count_lock:
            fail_count += 1
        return

    try:
        md_text = read_md_content(md_path)
    except Exception as e:
        append_jsonl(ERROR_JSONL, {
            "doi": doi,
            "md_path": str(md_path),
            "error_type": "md_read_error",
            "message": str(e)
        })
        with count_lock:
            fail_count += 1
        return

    messages = make_messages(prompt_text, md_text)

    try:
        output_text, meta = call_model(
            model=model,
            messages=messages,
            request_timeout=REQUEST_TIMEOUT,
            max_retries=MAX_RETRIES,
            doi_label=f"doi={doi}",
        )

        result = {
            "doi": doi,
            "md_path": str(md_path),
            "prompt_file": str(PROMPT_PATH),
            "prompt_name": PROMPT_PATH.stem,
            "model": model,
            "response": output_text,
            "meta": meta,
        }

        with write_lock:
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)

        append_jsonl(RESULT_JSONL, {
            "doi": doi,
            "output_file": str(output_file),
            "status": "success"
        })
        append_done_doi(DONE_DOI_TXT, doi)

        with count_lock:
            success_count += 1

    except Exception as e:
        last_err = repr(e)
        append_jsonl(ERROR_JSONL, {
            "doi": doi,
            "md_path": str(md_path),
            "error_type": "api_failed",
            "message": last_err
        })
        with count_lock:
            fail_count += 1


# =========================
# 主流程
# =========================
def main():
    global success_count, fail_count, OPENAI_API_KEY

    setup_logging()

    if not MD_ROOT.exists():
        raise FileNotFoundError(f"MD_ROOT 不存在: {MD_ROOT}")
    if not CSV_PATH.exists():
        raise FileNotFoundError(f"CSV_PATH 不存在: {CSV_PATH}")
    if not PROMPT_PATH.exists():
        raise FileNotFoundError(f"PROMPT_PATH 不存在: {PROMPT_PATH}")
    if not API_KEY_ENV_FILE.exists():
        raise FileNotFoundError(f"API_KEY_ENV_FILE 不存在: {API_KEY_ENV_FILE}")

    # ---- 加载 API key 到全局变量（参照 zj_batch_QA_xin.py） ----
    OPENAI_API_KEY = get_api_key(API_KEY_ENV_FILE)

    prompt_text = read_prompt(PROMPT_PATH)
    doi_list = read_doi_csv(CSV_PATH)
    done_dois = load_done_dois(DONE_DOI_TXT)
    md_index = build_md_index(MD_ROOT, MD_INDEX_JSON)

    # 断点续跑
    pending = []
    for d in doi_list:
        d = normalize_doi(d)
        output_file = OUTPUT_DIR / make_output_filename(d)
        if d in done_dois or output_file.exists():
            continue
        pending.append(d)

    logging.info("CSV DOI 总数: %d", len(doi_list))
    logging.info("已完成 DOI 数: %d", len(doi_list) - len(pending))
    logging.info("待处理 DOI 数: %d", len(pending))
    logging.info("Model: %s", DEFAULT_MODEL)
    logging.info("BASE_URL: %s", BASE_URL)
    logging.info("MAX_WORKERS=%d, MAX_REQUESTS_PER_MINUTE=%d", MAX_WORKERS, MAX_REQUESTS_PER_MINUTE)
    logging.info("输出目录: %s", OUTPUT_DIR)

    if not pending:
        logging.info("没有待处理 DOI，任务结束。")
        summary = {
            "api_type": "zjlab",
            "url": BASE_URL,
            "total_csv_doi": len(doi_list),
            "pending": 0,
            "success_count": 0,
            "fail_count": 0,
            "output_dir": str(OUTPUT_DIR),
            "prompt_name": PROMPT_PATH.stem,
            "model": DEFAULT_MODEL,
        }
        with open(SUMMARY_JSON, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        return

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(
                process_one_doi,
                doi,
                prompt_text,
                MD_ROOT,
                md_index,
                DEFAULT_MODEL,
            ): doi
            for doi in pending
        }

        for future in tqdm(as_completed(futures), total=len(futures), desc="推理进度", unit="doi"):
            doi = futures[future]
            try:
                future.result()
            except Exception as e:
                logging.exception("doi=%s 线程级异常: %s", doi, e)
                append_jsonl(ERROR_JSONL, {
                    "doi": doi,
                    "error_type": "thread_exception",
                    "message": str(e)
                })
                with count_lock:
                    fail_count += 1

    summary = {
        "api_type": "zjlab",
        "url": BASE_URL,
        "total_csv_doi": len(doi_list),
        "processed_this_run": len(pending),
        "success_count": success_count,
        "fail_count": fail_count,
        "output_dir": str(OUTPUT_DIR),
        "prompt_name": PROMPT_PATH.stem,
        "model": DEFAULT_MODEL,
        "max_workers": MAX_WORKERS,
        "max_retries": MAX_RETRIES,
        "request_timeout": REQUEST_TIMEOUT,
        "max_requests_per_minute": MAX_REQUESTS_PER_MINUTE,
    }

    with open(SUMMARY_JSON, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    logging.info("全部完成。")
    logging.info("成功数量: %d", success_count)
    logging.info("失败数量: %d", fail_count)
    logging.info("汇总文件: %s", SUMMARY_JSON)


if __name__ == "__main__":
    main()
