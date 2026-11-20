#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
批量版 V4 Single-Chain Repair Controller（带详细错误日志）
模型调用方式参照 01_ZJ_lowerLetter_batch.py（DeepSeek API + dotenv + 指数退避重试 + RateLimiter）

功能：
1. 批量处理 V3 输出目录中的 *.json
2. 支持文件级并行（可配置 FILE_PARALLELISM）
3. 输出已存在则跳过
4. QC 并行
5. V4 API 并行，但严格限流，避免并发过高导致 429
6. 支持重试 + 指数退避
7. 文件级总进度条（并行时无链级进度条，避免混乱）
8. 原子写入，避免半截文件
9. 详细错误落盘：
   - repair_batch_errors.jsonl（全局）
   - 每个输出文件内保留 file_level_errors / chain_error_records
"""

from __future__ import annotations

import os
import json
import re
import time
import random
import threading
import traceback
from pathlib import Path
from copy import deepcopy
from typing import Any, Dict, Optional, Tuple, List
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv
from openai import OpenAI, APIError, APITimeoutError, APIConnectionError
from tqdm import tqdm
from model_response import read_response_text
from ssl_trust import enable_system_trust_store
from structured_model import create_structured_completion

enable_system_trust_store()

from step02_qc import judge_chain_quality


# =========================================================
# 路径配置
# =========================================================
APP_DIR = Path(__file__).resolve().parent
ENV_PATH = str(APP_DIR / ".env")
V3_DIR = APP_DIR / "output" / "standalone_step02"
PROMPT_PATH = APP_DIR / "PropertyChain_0420_Enhanced_Lite.md"
OUTPUT_DIR = APP_DIR / "output" / "standalone_step02"


# =========================================================
# 模型配置
# =========================================================
BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
MODEL_NAME = DEFAULT_MODEL
MODEL_TEMPERATURE = 0.0

# 并发配置
QC_MAX_WORKERS = min(8, os.cpu_count() or 4)

# API 并发不要太高，避免 429
API_MAX_WORKERS = 6

# 限流配置（参照 01_ZJ_lowerLetter_batch.py：每分钟最大请求数）
MAX_REQUESTS_PER_MINUTE = 20
RATE_LIMIT_WINDOW = 60.0

# 重试配置
API_MAX_RETRIES = 6
REQUEST_TIMEOUT = 1800
MAX_OUTPUT_TOKENS = 32000

# 运行控制
SKIP_IF_OUTPUT_EXISTS = True
DEBUG_PRINT = False    #True

# 输出文件命名
OUTPUT_SUFFIX = "_final.json"

# =========================================================
# 新增：文件级并行配置
# =========================================================
FILE_PARALLELISM = 3          # 同时处理的文件数（设为1则退化为串行模式，可保留链级进度条）
SHOW_CHAIN_PROGRESS = False   # 并行模式下强制关闭链级进度条（避免输出混乱）

# 全局详细错误日志（app.py 可覆盖）
GLOBAL_ERROR_LOG_PATH = OUTPUT_DIR / "10_repair_batch_errors.jsonl"
SUMMARY_PATH = OUTPUT_DIR / "10_repair_batch_summary.json"

# app.py 在此处注入覆盖值，必须早于目录、密钥、Prompt 和限流器初始化。
# APP_CONFIG_OVERRIDE_POINT

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# =========================================================
# 限流器（参照 01_ZJ_lowerLetter_batch.py）
# =========================================================
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


# =========================================================
# 加载 API Key（参照 01_ZJ_lowerLetter_batch.py）
# =========================================================
def get_api_key(env_path: str) -> str:
    api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
    if api_key:
        return api_key
    if not os.path.exists(env_path):
        raise FileNotFoundError(f"API_KEY_ENV_FILE 不存在: {env_path}")
    load_dotenv(dotenv_path=env_path, override=True, encoding="utf-8")
    api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        raise ValueError(f"未读取到 DASHSCOPE_API_KEY，请检查环境变量文件：{env_path}")
    return api_key

OPENAI_API_KEY = get_api_key(ENV_PATH)


# =========================================================
# Prompt
# =========================================================
def load_prompt() -> str:
    if not PROMPT_PATH.exists():
        raise FileNotFoundError(f"Prompt 文件不存在：{PROMPT_PATH}")
    with open(PROMPT_PATH, "r", encoding="utf-8") as f:
        return f.read()

PROMPT_TEMPLATE = load_prompt()


# =========================================================
# 错误日志工具
# =========================================================
ERROR_LOG_LOCK = threading.Lock()


def append_global_error_log(record: Dict[str, Any]) -> None:
    """
    全局错误日志，jsonl 格式，一行一条。
    """
    with ERROR_LOG_LOCK:
        with open(GLOBAL_ERROR_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def build_error_record(
    stage: str,
    source_file: str,
    chain_id: Optional[str] = None,
    property_type: Optional[str] = None,
    property_value: Optional[str] = None,
    error_type: Optional[str] = None,
    error_message: Optional[str] = None,
    traceback_text: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    record = {
        "stage": stage,
        "source_file": source_file,
        "chain_id": chain_id,
        "property_type": property_type,
        "property_value": property_value,
        "error_type": error_type,
        "error_message": error_message,
        "traceback": traceback_text,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if extra:
        record["extra"] = extra
    return record


# =========================================================
# 基础工具
# =========================================================
def safe_str(x: Any) -> str:
    if x is None:
        return ""
    if isinstance(x, str):
        return x.strip()
    return str(x).strip()


def parse_chain_id(chain_id: str) -> Tuple[int, int]:
    if not chain_id:
        raise ValueError("chain_id 为空")
    parts = chain_id.split("_")
    if len(parts) != 3:
        raise ValueError(f"非法 chain_id 格式：{chain_id}")
    return int(parts[1]), int(parts[2])


def extract_property_info(chain: Dict[str, Any]) -> Tuple[str, str]:
    prop = chain.get("property", {})
    if not isinstance(prop, dict):
        return "Not Provided", "Not Provided"
    return (
        safe_str(prop.get("property_type", "Not Provided")) or "Not Provided",
        safe_str(prop.get("property_value", "Not Provided")) or "Not Provided",
    )


def extract_json_from_response_text(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None

    text = text.strip()

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)

    try:
        return json.loads(text)
    except Exception:
        pass

    left = text.find("{")
    right = text.rfind("}")
    if left != -1 and right != -1 and right > left:
        sub = text[left:right + 1]
        try:
            return json.loads(sub)
        except Exception:
            return None
    return None


def atomic_write_json(data: Dict[str, Any], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, out_path)


def make_output_path(v3_file: Path) -> Path:
    base = v3_file.stem
    return OUTPUT_DIR / f"{base}{OUTPUT_SUFFIX}"


def is_rate_limit_error_message(msg: str) -> bool:
    msg_l = (msg or "").lower()
    keywords = [
        "429",
        "rate limit",
        "ratelimit",
        "too many requests",
        "quota",
        "throttl",
        "setlimitexceeded",
        "resource exhausted",
    ]
    return any(k in msg_l for k in keywords)


# =========================================================
# V4 调用输入构建
# =========================================================
def build_single_chain_repair_input(chain: Dict[str, Any]) -> Dict[str, Any]:
    prop = chain.get("property", {}) if isinstance(chain.get("property"), dict) else {}
    char = chain.get("characterization", {}) if isinstance(chain.get("characterization"), dict) else {}
    ectx = chain.get("electrochemical_context", {}) if isinstance(chain.get("electrochemical_context"), dict) else {}

    property_context = safe_str(prop.get("property_context"))
    property_value = safe_str(prop.get("property_value"))
    property_evidence = safe_str((prop.get("evidence") or {}).get("Direct_Quote"))

    context_conflict_warning = ""

    c_rate_in_context = re.findall(r"(\d+(?:\.\d+)?)\s*C\b", property_context, flags=re.IGNORECASE)
    if c_rate_in_context:
        c_rate_in_context = [f"{c}C" for c in c_rate_in_context]

    c_rate_in_evidence = None
    if property_value and property_evidence:
        pattern = rf"{re.escape(property_value)}\s*mAh\s*g[⁻-]?[¹1]?\s*at\s*(\d+(?:\.\d+)?)\s*C"
        match = re.search(pattern, property_evidence, flags=re.IGNORECASE)
        if match:
            c_rate_in_evidence = f"{match.group(1)}C"

    if c_rate_in_context and c_rate_in_evidence and c_rate_in_evidence not in c_rate_in_context:
        context_conflict_warning = (
            f"WARNING: property_context suggests C-Rate={c_rate_in_context}, "
            f"but property_evidence binds property_value={property_value} to {c_rate_in_evidence}. "
            f"Prioritize direct evidence binding."
        )

    return {
        "Task": "Repair one existing property chain without changing identity anchors.",
        "Target_Property": deepcopy(prop),
        "Current_Chain": deepcopy(chain),
        "Anchor_Constraints": {
            "chain_id": chain.get("chain_id", "Not Provided"),
            "property_context_id": prop.get("context_id", "Not Provided"),
            "property_linked_char_id": prop.get("linked_char_id", "Not Provided"),
            "current_context_id": ectx.get("context_id", "Not Provided"),
            "current_char_id": char.get("char_id", "Not Provided"),
        },
        "Context_Reliability_Warning": context_conflict_warning,
    }


# =========================================================
# API 调用（参照 01_ZJ_lowerLetter_batch.py 的调用方式）
#  - client 在函数内创建，max_retries=0
#  - 限流器 RateLimiter.acquire()
#  - 捕获 APIConnectionError / APITimeoutError / APIError
#  - 指数退避 2**attempt
# =========================================================
def call_v4_model_single_chain(
    chain: Dict[str, Any],
    source_file: str,
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    返回：
    - parsed_result
    - error_records（该 chain 在 API 阶段产生的详细错误）
    """
    user_input = build_single_chain_repair_input(chain)
    messages = [
        {"role": "system", "content": PROMPT_TEMPLATE},
        {"role": "user", "content": json.dumps(user_input, ensure_ascii=False)},
    ]

    chain_id = safe_str(chain.get("chain_id"))
    property_type, property_value = extract_property_info(chain)

    last_err = None
    local_error_records: List[Dict[str, Any]] = []

    for attempt in range(1, API_MAX_RETRIES + 1):
        # 限流器在每次请求前 acquire
        rate_limiter.acquire()

        # 每个请求创建独立 client（参照 01_ZJ_lowerLetter_batch.py）
        client = OpenAI(
            base_url=BASE_URL,
            api_key=OPENAI_API_KEY,
            max_retries=0,
        )

        try:
            response = create_structured_completion(
                client,
                model=MODEL_NAME,
                messages=messages,
                temperature=MODEL_TEMPERATURE,
                max_tokens=MAX_OUTPUT_TOKENS,
                timeout=REQUEST_TIMEOUT,
            )
            content, response_diagnostic = read_response_text(response)
            parsed = extract_json_from_response_text(content)

            if parsed is not None:
                return parsed, local_error_records

            err_msg = (
                f"模型返回 content 为空（{response_diagnostic}）"
                if not content.strip()
                else f"模型返回内容无法解析为 JSON（{response_diagnostic}）"
            )
            record = build_error_record(
                stage="api_parse",
                source_file=source_file,
                chain_id=chain_id,
                property_type=property_type,
                property_value=property_value,
                error_type="JSONParseError",
                error_message=err_msg,
                traceback_text=None,
                extra={
                    "attempt": attempt,
                    "response_diagnostic": response_diagnostic,
                    "raw_response_preview": content[:2000],
                },
            )
            local_error_records.append(record)
            last_err = ValueError(err_msg)

        except (APIConnectionError, APITimeoutError, APIError) as e:
            tb = traceback.format_exc()
            record = build_error_record(
                stage="api_call",
                source_file=source_file,
                chain_id=chain_id,
                property_type=property_type,
                property_value=property_value,
                error_type=type(e).__name__,
                error_message=str(e),
                traceback_text=tb,
                extra={
                    "attempt": attempt,
                    "is_rate_limit": is_rate_limit_error_message(str(e)),
                },
            )
            local_error_records.append(record)
            last_err = e

            if DEBUG_PRINT:
                tqdm.write(f"[API ERROR] attempt={attempt}/{API_MAX_RETRIES} | {e}")

        except Exception as e:
            tb = traceback.format_exc()
            record = build_error_record(
                stage="api_call",
                source_file=source_file,
                chain_id=chain_id,
                property_type=property_type,
                property_value=property_value,
                error_type=type(e).__name__,
                error_message=str(e),
                traceback_text=tb,
                extra={"attempt": attempt},
            )
            local_error_records.append(record)
            last_err = e

            if DEBUG_PRINT:
                tqdm.write(f"[API UNKNOWN ERROR] attempt={attempt}/{API_MAX_RETRIES} | {e}")

        if attempt < API_MAX_RETRIES:
            sleep_time = 2 ** attempt
            time.sleep(sleep_time)

    # 全部失败
    final_record = build_error_record(
        stage="api_final_failed",
        source_file=source_file,
        chain_id=chain_id,
        property_type=property_type,
        property_value=property_value,
        error_type=type(last_err).__name__ if last_err else "UnknownError",
        error_message=str(last_err) if last_err else "API 调用失败但未捕获到异常详情",
        traceback_text=None,
        extra={
            "max_retries": API_MAX_RETRIES,
        },
    )
    local_error_records.append(final_record)

    if DEBUG_PRINT and last_err is not None:
        tqdm.write(f"[API FAILED] {chain.get('chain_id')} | {last_err}")

    return None, local_error_records


# =========================================================
# 返回标准化
# =========================================================
def normalize_v4_result(result: Any) -> Optional[Dict[str, Any]]:
    if result is None:
        return None

    if isinstance(result, dict):
        if "repaired_chain" in result and isinstance(result["repaired_chain"], dict):
            return result["repaired_chain"]
        if "chain" in result and isinstance(result["chain"], dict):
            return result["chain"]
        if "property_chains" in result and isinstance(result["property_chains"], list):
            if result["property_chains"] and isinstance(result["property_chains"][0], dict):
                return result["property_chains"][0]
        if any(k in result for k in ["property", "characterization", "electrochemical_context"]):
            return result

    return None


# =========================================================
# 锚点校验
# =========================================================
def extract_anchor_bundle(chain: Dict[str, Any]) -> Dict[str, str]:
    prop = chain.get("property", {}) if isinstance(chain.get("property"), dict) else {}
    char = chain.get("characterization", {}) if isinstance(chain.get("characterization"), dict) else {}
    ectx = chain.get("electrochemical_context", {}) if isinstance(chain.get("electrochemical_context"), dict) else {}

    return {
        "chain_id": safe_str(chain.get("chain_id", "Not Provided")) or "Not Provided",
        "property_context_id": safe_str(prop.get("context_id", "Not Provided")) or "Not Provided",
        "property_linked_char_id": safe_str(prop.get("linked_char_id", "Not Provided")) or "Not Provided",
        "context_id": safe_str(ectx.get("context_id", "Not Provided")) or "Not Provided",
        "char_id": safe_str(char.get("char_id", "Not Provided")) or "Not Provided",
    }


def validate_anchor_consistency(original_chain: Dict[str, Any], repaired_chain: Dict[str, Any]) -> Tuple[bool, List[str]]:
    reasons: List[str] = []
    orig = extract_anchor_bundle(original_chain)
    new = extract_anchor_bundle(repaired_chain)

    required_same_keys = [
        "chain_id",
        "property_context_id",
        "property_linked_char_id",
    ]
    for key in required_same_keys:
        if orig[key] != "Not Provided" and new[key] != orig[key]:
            reasons.append(f"anchor changed: {key} ({orig[key]} -> {new[key]})")

    p_ctx = new["property_context_id"]
    e_ctx = new["context_id"]
    if p_ctx != "Not Provided" and e_ctx != "Not Provided" and p_ctx != e_ctx:
        reasons.append(f"context mismatch: property.context_id={p_ctx} vs electrochemical_context.context_id={e_ctx}")

    p_char = new["property_linked_char_id"]
    c_char = new["char_id"]
    if p_char != "Not Provided" and c_char != "Not Provided" and p_char != c_char:
        reasons.append(f"char mismatch: property.linked_char_id={p_char} vs characterization.char_id={c_char}")

    return len(reasons) == 0, reasons


# =========================================================
# 安全合并
# =========================================================
def merge_dict_by_schema(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(base)
    for k, v in patch.items():
        if k not in result:
            result[k] = deepcopy(v)
            continue
        if isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = merge_dict_by_schema(result[k], v)
        else:
            result[k] = deepcopy(v)
    return result


def safe_overlay_module(base_chain: Dict[str, Any], repaired_chain: Dict[str, Any], module_name: str):
    base_module = base_chain.get(module_name)
    repaired_module = repaired_chain.get(module_name)
    if repaired_module is None:
        return deepcopy(base_module)
    if isinstance(base_module, dict) and isinstance(repaired_module, dict):
        return merge_dict_by_schema(base_module, repaired_module)
    return deepcopy(base_module)


# =========================================================
# 轻量格式化
# =========================================================
def _ensure_test_conditions_dict(chain: Dict[str, Any]) -> Dict[str, Any]:
    chain.setdefault("characterization", {})
    char = chain["characterization"]
    if not isinstance(char, dict):
        chain["characterization"] = {"test_conditions": {}}
        return chain["characterization"]["test_conditions"]

    char.setdefault("test_conditions", {})
    if not isinstance(char["test_conditions"], dict):
        char["test_conditions"] = {}
    return char["test_conditions"]


def enforce_atomic_field_single_value(chain: Dict[str, Any]) -> Dict[str, Any]:
    chain = deepcopy(chain)
    tc = _ensure_test_conditions_dict(chain)

    atomic_fields = [
        "Temperature",
        "Current_Density",
        "C_Rate",
        "Cycles",
        "Scan_Rate",
        "Frequency_Range",
    ]

    for field in atomic_fields:
        if field not in tc:
            continue
        value = tc[field]
        if not isinstance(value, str):
            continue

        if not value.strip():
            tc[field] = "Not Provided"
            continue

        if ";" in value:
            first_value = value.split(";")[0].strip()
            tc[field] = first_value if first_value else "Not Provided"
        elif "," in value and " to " not in value.lower():
            first_value = value.split(",")[0].strip()
            tc[field] = first_value if first_value else "Not Provided"

    chain["characterization"]["test_conditions"] = tc
    return chain


def canonicalize_any_final_chain(chain: Dict[str, Any], add_meta: bool = True) -> Dict[str, Any]:
    fixed = enforce_atomic_field_single_value(chain)

    if add_meta:
        meta = fixed.get("local_post_canonicalization_meta", {})
        if not isinstance(meta, dict):
            meta = {}
        meta.update({
            "property_level_post_canonicalization": True,
            "applied_to_all_final_chains": True,
            "formatting_applied": "atomic_fields_single_value_only",
            "no_semantic_correction": True,
        })
        fixed["local_post_canonicalization_meta"] = meta

    return fixed


# =========================================================
# 构建最终链
# =========================================================
def build_final_chain(
    v3_chain: Dict[str, Any],
    repaired_result: Dict[str, Any],
    source_file: str,
) -> Tuple[Dict[str, Any], bool, List[str], List[Dict[str, Any]]]:
    """
    返回：
    - final_chain
    - accepted
    - reject_reasons
    - error_records
    """
    error_records: List[Dict[str, Any]] = []
    chain_id = safe_str(v3_chain.get("chain_id"))
    property_type, property_value = extract_property_info(v3_chain)

    normalized = normalize_v4_result(repaired_result)
    if normalized is None:
        reasons = ["normalized repaired result is None"]
        error_records.append(
            build_error_record(
                stage="normalize_v4_result",
                source_file=source_file,
                chain_id=chain_id,
                property_type=property_type,
                property_value=property_value,
                error_type="NormalizeError",
                error_message="normalize_v4_result 返回 None",
                traceback_text=None,
                extra={"repaired_result_type": str(type(repaired_result))},
            )
        )
        return deepcopy(v3_chain), False, reasons, error_records

    ok, reasons = validate_anchor_consistency(v3_chain, normalized)
    if not ok:
        error_records.append(
            build_error_record(
                stage="anchor_validation",
                source_file=source_file,
                chain_id=chain_id,
                property_type=property_type,
                property_value=property_value,
                error_type="AnchorValidationError",
                error_message="锚点校验失败",
                traceback_text=None,
                extra={"reject_reasons": reasons},
            )
        )
        return deepcopy(v3_chain), False, reasons, error_records

    final_chain = deepcopy(v3_chain)
    final_chain["chain_id"] = v3_chain.get("chain_id")

    for module_name in [
        "property",
        "characterization",
        "electrochemical_context",
        "microstructure",
        "synthesis_method",
        "material_info",
    ]:
        if module_name in v3_chain or module_name in normalized:
            final_chain[module_name] = safe_overlay_module(v3_chain, normalized, module_name)

    final_chain = canonicalize_any_final_chain(final_chain)
    return final_chain, True, [], error_records


# =========================================================
# QC worker
# =========================================================
def qc_one_chain(chain: Dict[str, Any], source_file: str) -> Dict[str, Any]:
    chain_id = chain.get("chain_id")
    property_type, property_value = extract_property_info(chain)

    try:
        if not chain_id:
            return {
                "chain": chain,
                "chain_id": None,
                "property_type": property_type,
                "property_value": property_value,
                "status": "failed",
                "reasons": ["missing chain_id"],
                "parse_ok": False,
                "error_records": [
                    build_error_record(
                        stage="qc_precheck",
                        source_file=source_file,
                        chain_id=None,
                        property_type=property_type,
                        property_value=property_value,
                        error_type="MissingChainID",
                        error_message="chain_id 缺失",
                        traceback_text=None,
                    )
                ],
            }

        parse_chain_id(chain_id)

        status, reasons = judge_chain_quality(chain)
        return {
            "chain": chain,
            "chain_id": chain_id,
            "property_type": property_type,
            "property_value": property_value,
            "status": status,
            "reasons": reasons,
            "parse_ok": True,
            "error_records": [],
        }

    except Exception as e:
        tb = traceback.format_exc()
        return {
            "chain": chain,
            "chain_id": chain_id,
            "property_type": property_type,
            "property_value": property_value,
            "status": "failed",
            "reasons": [f"QC exception: {e}"],
            "parse_ok": False,
            "error_records": [
                build_error_record(
                    stage="qc_exception",
                    source_file=source_file,
                    chain_id=safe_str(chain_id) if chain_id is not None else None,
                    property_type=property_type,
                    property_value=property_value,
                    error_type=type(e).__name__,
                    error_message=str(e),
                    traceback_text=tb,
                )
            ],
        }


# =========================================================
# Repair worker
# =========================================================
def repair_one_target(chain: Dict[str, Any], source_file: str) -> Dict[str, Any]:
    chain_id = safe_str(chain.get("chain_id"))
    property_type, property_value = extract_property_info(chain)
    local_error_records: List[Dict[str, Any]] = []

    try:
        repaired, api_error_records = call_v4_model_single_chain(chain, source_file)
        local_error_records.extend(api_error_records)

        final_chain, accepted, reject_reasons, build_error_records = build_final_chain(chain, repaired, source_file)
        local_error_records.extend(build_error_records)

        return {
            "chain_id": chain_id,
            "property_type": property_type,
            "property_value": property_value,
            "api_ok": repaired is not None,
            "accepted": accepted,
            "final_chain": final_chain,
            "reject_reasons": reject_reasons,
            "raw_repaired": repaired,
            "error_records": local_error_records,
        }

    except Exception as e:
        tb = traceback.format_exc()
        local_error_records.append(
            build_error_record(
                stage="repair_worker_exception",
                source_file=source_file,
                chain_id=chain_id,
                property_type=property_type,
                property_value=property_value,
                error_type=type(e).__name__,
                error_message=str(e),
                traceback_text=tb,
            )
        )

        return {
            "chain_id": chain_id,
            "property_type": property_type,
            "property_value": property_value,
            "api_ok": False,
            "accepted": False,
            "final_chain": canonicalize_any_final_chain(chain),
            "reject_reasons": [f"repair worker exception: {e}"],
            "raw_repaired": None,
            "error_records": local_error_records,
        }


# =========================================================
# 单文件处理（支持显示/隐藏链级进度条）
# =========================================================
def process_one_file(v3_file: Path, show_progress: bool = True) -> Dict[str, Any]:
    """
    处理单个输入文件，返回处理结果日志。
    :param show_progress: 是否显示该文件内部的 QC/V4 进度条（并行模式下建议关闭）
    """
    print(f"[DEBUG] Starting {v3_file.name} at {time.strftime('%H:%M:%S')}")
    out_path = make_output_path(v3_file)
    file_level_errors: List[Dict[str, Any]] = []
    chain_error_records: List[Dict[str, Any]] = []

    if SKIP_IF_OUTPUT_EXISTS and out_path.exists():
        return {
            "file": v3_file.name,
            "status": "skipped_existing",
            "output_path": str(out_path),
            "total_chains": 0,
            "final_total_chains": 0,
            "error_count": 0,
        }

    # -------------------------
    # 读文件
    # -------------------------
    try:
        with open(v3_file, "r", encoding="utf-8") as f:
            v3_data = json.load(f)
    except Exception as e:
        tb = traceback.format_exc()
        record = build_error_record(
            stage="file_read",
            source_file=v3_file.name,
            error_type=type(e).__name__,
            error_message=str(e),
            traceback_text=tb,
        )
        append_global_error_log(record)
        raise

    source_file = v3_data.get("source_file", v3_file.name)
    source_doi = v3_data.get("source_doi", "Not Provided")
    total_entries = v3_data.get("total_entries", 0)

    v3_chains = v3_data.get("property_chains", [])
    v3_total_chains = v3_data.get("total_chains", len(v3_chains))

    final_chains: List[Dict[str, Any]] = []
    qc_results: List[Dict[str, Any]] = []
    fallback_results: List[Dict[str, Any]] = []
    raw_outputs_by_property: List[Dict[str, Any]] = []
    fallback_targets: List[Dict[str, Any]] = []

    kept_v3_count = 0
    fallback_success_count = 0
    fallback_failed_count = 0

    # -------------------------
    # 并行 QC
    # -------------------------
    if show_progress:
        chain_bar = tqdm(
            total=len(v3_chains),
            desc=f"[{v3_file.name}] QC",
            position=0,          # 并行模式下会被覆盖，但我们已经关闭了 show_progress
            leave=False,
        )
    else:
        chain_bar = None

    with ThreadPoolExecutor(max_workers=QC_MAX_WORKERS) as executor:
        futures = [executor.submit(qc_one_chain, chain, source_file) for chain in v3_chains]
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as e:
                tb = traceback.format_exc()
                record = build_error_record(
                    stage="qc_future_exception",
                    source_file=source_file,
                    error_type=type(e).__name__,
                    error_message=str(e),
                    traceback_text=tb,
                )
                chain_error_records.append(record)
                append_global_error_log(record)
                if chain_bar:
                    chain_bar.update(1)
                continue

            chain = result["chain"]
            chain_id = result["chain_id"]
            property_type = result["property_type"]
            property_value = result["property_value"]
            status = result["status"]
            reasons = result["reasons"]

            for r in result.get("error_records", []):
                chain_error_records.append(r)
                append_global_error_log(r)

            qc_results.append({
                "chain_id": chain_id,
                "property_type": property_type,
                "property_value": property_value,
                "status": status,
                "reasons": reasons,
            })

            if status == "pass":
                final_chains.append(canonicalize_any_final_chain(chain))
                kept_v3_count += 1
            else:
                fallback_targets.append(chain)

            if chain_bar:
                chain_bar.update(1)

    if chain_bar:
        chain_bar.close()

    # -------------------------
    # 并行 Repair（限流）
    # -------------------------
    if fallback_targets:
        if show_progress:
            repair_bar = tqdm(
                total=len(fallback_targets),
                desc=f"[{v3_file.name}] V4",
                position=0,
                leave=False,
            )
        else:
            repair_bar = None

        with ThreadPoolExecutor(max_workers=API_MAX_WORKERS) as executor:
            future_map = {
                executor.submit(repair_one_target, chain, source_file): chain
                for chain in fallback_targets
            }

            for future in as_completed(future_map):
                original_chain = future_map[future]
                chain_id = safe_str(original_chain.get("chain_id"))
                property_type, property_value = extract_property_info(original_chain)

                try:
                    result = future.result()

                    for r in result.get("error_records", []):
                        chain_error_records.append(r)
                        append_global_error_log(r)

                    accepted = result["accepted"]
                    final_chain = result["final_chain"]
                    raw_repaired = result["raw_repaired"]
                    reject_reasons = result["reject_reasons"]

                    if accepted:
                        final_chains.append(final_chain)
                        fallback_results.append({
                            "chain_id": chain_id,
                            "property_type": property_type,
                            "property_value": property_value,
                            "status": "fallback_success"
                        })
                        raw_outputs_by_property.append({
                            "chain_id": chain_id,
                            "raw_repaired": raw_repaired
                        })
                        fallback_success_count += 1
                    else:
                        original_chain = canonicalize_any_final_chain(original_chain)
                        final_chains.append(original_chain)
                        fallback_results.append({
                            "chain_id": chain_id,
                            "property_type": property_type,
                            "property_value": property_value,
                            "status": "fallback_failed_keep_v3_post_canonicalized",
                            "reject_reasons": reject_reasons,
                        })
                        raw_outputs_by_property.append({
                            "chain_id": chain_id,
                            "raw_repaired": raw_repaired,
                            "reject_reasons": reject_reasons,
                        })
                        fallback_failed_count += 1

                except Exception as e:
                    tb = traceback.format_exc()
                    original_chain = canonicalize_any_final_chain(original_chain)
                    final_chains.append(original_chain)

                    fallback_results.append({
                        "chain_id": chain_id,
                        "property_type": property_type,
                        "property_value": property_value,
                        "status": "fallback_failed_keep_v3_post_canonicalized",
                        "reject_reasons": [f"repair future exception: {e}"],
                    })

                    raw_outputs_by_property.append({
                        "chain_id": chain_id,
                        "error": f"V4 failed: {e}"
                    })

                    record = build_error_record(
                        stage="repair_future_exception",
                        source_file=source_file,
                        chain_id=chain_id,
                        property_type=property_type,
                        property_value=property_value,
                        error_type=type(e).__name__,
                        error_message=str(e),
                        traceback_text=tb,
                    )
                    chain_error_records.append(record)
                    append_global_error_log(record)

                    fallback_failed_count += 1
                    if DEBUG_PRINT:
                        tqdm.write(f"[REPAIR ERROR] {chain_id} | {e}")

                if repair_bar:
                    repair_bar.update(1)

        if repair_bar:
            repair_bar.close()

    final_chains = sorted(final_chains, key=lambda x: str(x.get("chain_id", "")))
    qc_results = sorted(qc_results, key=lambda x: str(x.get("chain_id", "")))
    fallback_results = sorted(fallback_results, key=lambda x: str(x.get("chain_id", "")))
    raw_outputs_by_property = sorted(raw_outputs_by_property, key=lambda x: str(x.get("chain_id", "")))
    chain_error_records = sorted(chain_error_records, key=lambda x: str(x.get("chain_id") or ""))

    output_data = {
        "source_file": source_file,
        "source_doi": source_doi,
        "total_entries": total_entries,
        "total_chains": v3_total_chains,
        "final_total_chains": len(final_chains),
        "final_property_chains": final_chains,
        "qc_results": qc_results,
        "fallback_results": fallback_results,
        "raw_outputs_by_property": raw_outputs_by_property,
        "file_level_errors": file_level_errors,
        "chain_error_records": chain_error_records,
        "repair_controller_meta": {
            "repair_input_scope": "Target_Property + Current_Chain",
            "anchor_validation_enabled": True,
            "unsafe_deep_merge_removed": True,
            "llm_semantic_correction": True,
            "local_formatting_only": True,
            "applied_to_all_final_chains": True,
            "api_only_for_suspicious_failed": True,
            "batch_mode": True,
            "skip_existing_enabled": SKIP_IF_OUTPUT_EXISTS,
            "qc_max_workers": QC_MAX_WORKERS,
            "api_max_workers": API_MAX_WORKERS,
            "api_concurrency_limit": MAX_REQUESTS_PER_MINUTE,
            "api_max_retries": API_MAX_RETRIES,
            "model_name": MODEL_NAME,
            "base_url": BASE_URL,
            "global_error_log_path": str(GLOBAL_ERROR_LOG_PATH),
            "summary_path": str(SUMMARY_PATH),
        }
    }

    # -------------------------
    # 写文件
    # -------------------------
    try:
        atomic_write_json(output_data, out_path)
    except Exception as e:
        tb = traceback.format_exc()
        record = build_error_record(
            stage="file_write",
            source_file=source_file,
            error_type=type(e).__name__,
            error_message=str(e),
            traceback_text=tb,
            extra={"output_path": str(out_path)},
        )
        append_global_error_log(record)
        raise

    return {
        "file": v3_file.name,
        "status": "success",
        "output_path": str(out_path),
        "total_chains": v3_total_chains,
        "final_total_chains": len(final_chains),
        "kept_v3": kept_v3_count,
        "fallback_success": fallback_success_count,
        "fallback_failed": fallback_failed_count,
        "error_count": len(file_level_errors) + len(chain_error_records),
    }


# =========================================================
# 主程序（支持文件级并行）
# =========================================================
def main() -> None:
    if not V3_DIR.exists():
        raise FileNotFoundError(f"V3 输出目录不存在：{V3_DIR}")

    v3_files = sorted(V3_DIR.glob("*_property_chains_0422.json"))
    if not v3_files:
        raise FileNotFoundError(f"目录中没有找到 json 文件：{V3_DIR}")

    print("=" * 80)
    print("Batch V4 Repair Controller (Parallel File Version) [ZJ API]")
    print("=" * 80)
    print(f"V3_DIR                : {V3_DIR}")
    print(f"OUTPUT_DIR            : {OUTPUT_DIR}")
    print(f"PROMPT_PATH           : {PROMPT_PATH}")
    print(f"BASE_URL              : {BASE_URL}")
    print(f"MODEL_NAME            : {MODEL_NAME}")
    print(f"FILE_PARALLELISM      : {FILE_PARALLELISM}")
    print(f"QC_MAX_WORKERS        : {QC_MAX_WORKERS}")
    print(f"API_MAX_WORKERS       : {API_MAX_WORKERS}")
    print(f"MAX_REQUESTS_PER_MINUTE : {MAX_REQUESTS_PER_MINUTE}")
    print(f"API_MAX_RETRIES       : {API_MAX_RETRIES}")
    print(f"SKIP_IF_OUTPUT_EXISTS : {SKIP_IF_OUTPUT_EXISTS}")
    print(f"GLOBAL_ERROR_LOG_PATH : {GLOBAL_ERROR_LOG_PATH}")
    print("=" * 80)

    # 决定是否显示链级进度条
    show_progress = (FILE_PARALLELISM == 1) and not SHOW_CHAIN_PROGRESS
    # 如果 FILE_PARALLELISM == 1 且 SHOW_CHAIN_PROGRESS 为 False，仍可显示进度条（但为了清晰，只要并行度>1就强制关闭）
    if FILE_PARALLELISM > 1:
        show_progress = False
        print("Note: File parallelism > 1, chain-level progress bars disabled.\n")
    else:
        print("Note: Running in serial mode, chain-level progress bars enabled.\n")

    results: List[Dict[str, Any]] = []
    results_lock = threading.Lock()

    # 文件级并行处理
    with ThreadPoolExecutor(max_workers=FILE_PARALLELISM) as executor:
        future_to_file = {
            executor.submit(process_one_file, v3_file, show_progress): v3_file
            for v3_file in v3_files
        }

        with tqdm(total=len(future_to_file), desc="Files", unit="file") as pbar:
            for future in as_completed(future_to_file):
                v3_file = future_to_file[future]
                try:
                    result = future.result()
                    with results_lock:
                        results.append(result)

                    # 实时打印结果
                    if result["status"] == "skipped_existing":
                        tqdm.write(f"[SKIP] {result['file']} -> 已存在，跳过")
                    elif result["status"] == "success":
                        tqdm.write(
                            f"[OK] {result['file']} | chains={result['total_chains']} "
                            f"| kept_v3={result['kept_v3']} "
                            f"| fallback_success={result['fallback_success']} "
                            f"| fallback_failed={result['fallback_failed']} "
                            f"| errors={result['error_count']}"
                        )
                    else:
                        tqdm.write(f"[FAIL] {result.get('file', v3_file.name)} | {result.get('error', 'Unknown error')}")

                except Exception as e:
                    tb = traceback.format_exc()
                    record = build_error_record(
                        stage="process_one_file",
                        source_file=v3_file.name,
                        error_type=type(e).__name__,
                        error_message=str(e),
                        traceback_text=tb,
                    )
                    append_global_error_log(record)

                    with results_lock:
                        results.append({
                            "file": v3_file.name,
                            "status": "failed",
                            "error": str(e),
                        })
                    tqdm.write(f"[FAIL] {v3_file.name} | {e}")

                pbar.update(1)

    # 统计汇总
    success_count = sum(1 for x in results if x.get("status") == "success")
    skip_count = sum(1 for x in results if x.get("status") == "skipped_existing")
    fail_count = sum(1 for x in results if x.get("status") == "failed")

    summary = {
        "v3_dir": str(V3_DIR),
        "output_dir": str(OUTPUT_DIR),
        "prompt_path": str(PROMPT_PATH),
        "base_url": BASE_URL,
        "model_name": MODEL_NAME,
        "file_parallelism": FILE_PARALLELISM,
        "qc_max_workers": QC_MAX_WORKERS,
        "api_max_workers": API_MAX_WORKERS,
        "max_requests_per_minute": MAX_REQUESTS_PER_MINUTE,
        "api_max_retries": API_MAX_RETRIES,
        "skip_if_output_exists": SKIP_IF_OUTPUT_EXISTS,
        "global_error_log_path": str(GLOBAL_ERROR_LOG_PATH),
        "total_files": len(results),
        "success_files": success_count,
        "skipped_existing_files": skip_count,
        "failed_files": fail_count,
        "results": results,
    }

    with open(SUMMARY_PATH, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 80)
    print("Summary")
    print("=" * 80)
    print(f"Total files        : {len(results)}")
    print(f"Success            : {success_count}")
    print(f"Skipped existing   : {skip_count}")
    print(f"Failed             : {fail_count}")
    print(f"Summary file       : {SUMMARY_PATH}")
    print(f"Global error log   : {GLOBAL_ERROR_LOG_PATH}")
    print("=" * 80)


if __name__ == "__main__":
    main()
