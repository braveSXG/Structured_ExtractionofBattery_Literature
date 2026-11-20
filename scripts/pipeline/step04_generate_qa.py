#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
04zj_batch_QA_xin.py — 基于 DeepSeek API 的 QA 链批量生产

功能：
  - 保持与 batch_xin_QArun.py 相同的输入格式：每个输入 .json 包含 final_property_chains
  - 保持与 batch_xin_QArun.py 相同的输出格式：每个输入 .json 对应一个输出 .json，内部包含 chains_qa
  - 调用 DeepSeek OpenAI 兼容 API
  - 文件级并行，默认 MAX_WORKERS=6
  - 带 tqdm 进度条、断点续跑、run.log、errors.jsonl、results.jsonl、summary.json
  - 增强输入 JSON 与模型输出 JSON 的解析容错能力

依赖：
  pip install openai python-dotenv tqdm

运行示例：
  python zj_batch_QA.py --skip_existing
  python zj_batch_QA.py --debug_input --limit 1
  nohup python -u zj_batch_QA.py --skip_existing > QA_0630_jianjie.log 2>&1 &


也可以通过命令行参数覆盖默认 Prompt、输入目录、输出目录和密钥文件。
"""

import os
import re
import ast
import json
import time
import argparse
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
# 默认路径配置
# =========================
APP_DIR = Path(__file__).resolve().parent
DEFAULT_API_KEY_ENV_FILE = str(APP_DIR / ".env")
DEFAULT_PROMPT_MD = str(APP_DIR / "propertyQA_0630_narrative.md")
DEFAULT_INPUT_DIR = str(APP_DIR / "output" / "standalone_step03")
DEFAULT_OUT_DIR = str(APP_DIR / "output" / "standalone_step04")

# =========================
# API 配置
# =========================
BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"

# =========================
# 并发、重试配置
# =========================
DEFAULT_MAX_WORKERS = 6
DEFAULT_MAX_RETRIES = 3
DEFAULT_REQUEST_TIMEOUT = 1800
DEFAULT_MAX_TOKENS = 32000
DEFAULT_TEMPERATURE = 0.0
DEFAULT_TEXT_LIMIT = 80000

# APP_CONFIG_OVERRIDE_POINT

# =========================
# 线程锁 & 全局 API key
# =========================
write_lock = threading.Lock()
count_lock = threading.Lock()

OPENAI_API_KEY = None


# =========================
# 基础工具函数
# =========================
def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def read_text(path: Path) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def save_json(path: Path, obj: Dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def append_jsonl(path: Path, item: Dict[str, Any]) -> None:
    with write_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")


def safe_output_name(text: str) -> str:
    text = str(text or "")
    return re.sub(r'[\\/:*?"<>|]+', "_", text)


def list_json_files(input_dir: Path) -> List[Path]:
    return sorted([p for p in input_dir.glob("*.json") if p.is_file()])


def filter_empty_chain_files(json_files: List[Path]) -> Tuple[List[Path], int]:
    """过滤掉 final_property_chains 为空列表的输入文件（避免无意义的 file 级 failed）"""
    logging.info("输入文件预扫中: 检查 %d 个文件的 final_property_chains 是否为空 ...", len(json_files))
    valid: List[Path] = []
    skipped = 0
    parse_failed = 0
    for jf in tqdm(json_files, desc="预扫输入文件", unit="file"):
        try:
            data = load_json_robust(jf)
            chains = data.get("final_property_chains", [])
            if isinstance(chains, list) and len(chains) > 0:
                valid.append(jf)
            else:
                skipped += 1
        except Exception:
            # 解析不出的文件保留，让后续处理报错
            parse_failed += 1
            valid.append(jf)
    logging.info("输入预扫完成: 共 %d 个文件, 有效 chain 文件 %d 个, 空 chain 文件 %d 个 (已跳过), 解析失败 %d 个",
                 len(json_files), len(valid), skipped, parse_failed)
    return valid, skipped


def prescan_output_files(json_files: List[Path], out_dir: Path, prompt_stem: str) -> Dict[str, Any]:
    """在 --retry_failed 模式下预扫输出文件，统计各状态文件数及待重试 chain 数。"""
    logging.info("输出文件预扫中: 检查已有输出文件状态 ...")
    status_counts = {"success": 0, "partial_failed": 0, "failed": 0, "missing": 0, "unreadable": 0}
    retry_files = 0
    total_chains_to_retry = 0
    total_chains_already_ok = 0

    for jf in tqdm(json_files, desc="预扫输出文件", unit="file"):
        out_path = out_dir / make_output_filename(jf, prompt_stem)
        if not out_path.exists():
            status_counts["missing"] += 1
            retry_files += 1
            continue
        try:
            existing = load_json_robust(out_path)
            existing_status = existing.get("status", "unknown")
            if existing_status == "success":
                status_counts["success"] += 1
            elif existing_status == "partial_failed":
                status_counts["partial_failed"] += 1
                retry_files += 1
                chains_qa = existing.get("chains_qa", [])
                for c in chains_qa:
                    if c.get("status") == "success":
                        total_chains_already_ok += 1
                    else:
                        total_chains_to_retry += 1
            elif existing_status == "failed":
                status_counts["failed"] += 1
                retry_files += 1
                # failed 文件需要重跑全部 chain，从输入文件获取 chain 数量
            else:
                status_counts["failed"] += 1
                retry_files += 1
        except Exception:
            status_counts["unreadable"] += 1
            retry_files += 1

    # 对 failed/missing 文件估算待重试 chain 数（因为无法从输出文件获知，需要扫输入文件）
    # 这里不做二次扫描，在日志中分开报告即可
    logging.info(
        "输出预扫完成: 已有输出文件状态分布 -> "
        "success=%d, partial_failed=%d, failed=%d, missing=%d, unreadable=%d",
        status_counts["success"], status_counts["partial_failed"],
        status_counts["failed"], status_counts["missing"], status_counts["unreadable"],
    )
    logging.info(
        "retry_failed 模式: 将跳过 %d 个 success 文件, 重新处理 %d 个文件"
        " (内含 %d 条已成功 chain 可复用, 至少 %d 条失败 chain 需重试)",
        status_counts["success"], retry_files,
        total_chains_already_ok, total_chains_to_retry,
    )

    return {
        "status_counts": status_counts,
        "retry_files": retry_files,
        "total_chains_already_ok": total_chains_already_ok,
        "total_chains_to_retry": total_chains_to_retry,
    }


def get_api_key(api_key_env_file: Path) -> str:
    """参照 zjlab_batch_ds0609.py 的 key 加载方式"""
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
# 输入 JSON 容错解析
# =========================
def strip_bom(text: str) -> str:
    return (text or "").lstrip("﻿").strip()


def remove_json_comments(text: str) -> str:
    """去掉常见 // 和 /* */ 注释；尽量不破坏字符串内容。"""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    out = []
    in_str = False
    quote = ""
    escaped = False
    i = 0
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if in_str:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                in_str = False
            i += 1
            continue
        if ch in {'"', "'"}:
            in_str = True
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "/":
            while i < len(text) and text[i] not in "\r\n":
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def remove_trailing_commas(text: str) -> str:
    """去掉 } 或 ] 前面的尾逗号。"""
    return re.sub(r",\s*([}\]])", r"\1", text)


def extract_balanced_json_text(text: str) -> Optional[str]:
    """从文本中提取第一个平衡的 JSON 对象或数组。"""
    text = strip_bom(text)
    start_candidates = [(text.find("{"), "{"), (text.find("["), "[")]
    start_candidates = [(idx, ch) for idx, ch in start_candidates if idx != -1]
    if not start_candidates:
        return None
    start, open_ch = min(start_candidates, key=lambda x: x[0])
    close_ch = "}" if open_ch == "{" else "]"

    depth = 0
    in_str = False
    quote = ""
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                in_str = False
            continue
        if ch in {'"', "'"}:
            in_str = True
            quote = ch
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def load_json_robust(path: Path) -> Dict[str, Any]:
    """
    尽量读取不完全规范的 JSON：
      1. 标准 json.loads
      2. 去注释、去尾逗号后 json.loads
      3. 从全文中提取第一个 {...} 后 json.loads
      4. ast.literal_eval 兼容 Python dict 风格的 True/False/None、单引号
    """
    raw = strip_bom(read_text(path))
    candidates = [raw, remove_trailing_commas(remove_json_comments(raw))]
    extracted = extract_balanced_json_text(raw)
    if extracted:
        candidates.append(extracted)
        candidates.append(remove_trailing_commas(remove_json_comments(extracted)))

    errors = []
    for cand in candidates:
        if not cand:
            continue
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
            raise ValueError("top-level JSON is not an object")
        except Exception as e:
            errors.append(f"json.loads: {repr(e)}")

        try:
            obj = ast.literal_eval(cand)
            if isinstance(obj, dict):
                return obj
            raise ValueError("top-level literal is not a dict")
        except Exception as e:
            errors.append(f"literal_eval: {repr(e)}")

    raise ValueError("输入 JSON 解析失败；最后错误：" + " | ".join(errors[-4:]))


# =========================
# 模型输出 JSON 提取
# =========================
def strip_code_fence(text: str) -> str:
    text = strip_bom(text)
    if text.startswith("```"):
        text = re.sub(r"^```(?:json|JSON)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def extract_json_from_model_text(text: str) -> Optional[Dict[str, Any]]:
    text = strip_code_fence(text)
    if not text:
        return None

    candidates = [text, remove_trailing_commas(remove_json_comments(text))]
    extracted = extract_balanced_json_text(text)
    if extracted:
        candidates.append(strip_code_fence(extracted))
        candidates.append(remove_trailing_commas(remove_json_comments(extracted)))

    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        try:
            obj = ast.literal_eval(cand)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return None


# =========================
# Prompt 拼装
# =========================
def build_messages(base_prompt: str, chain_obj: Dict[str, Any], text_limit: int) -> List[Dict[str, str]]:
    context_json = json.dumps(chain_obj, ensure_ascii=False, indent=2)
    if len(context_json) > text_limit:
        head = int(text_limit * 0.85)
        tail = text_limit - head
        context_json = context_json[:head] + "\n\n[CHAIN_JSON_TRUNCATED]\n\n" + context_json[-tail:]

    if "{context}" in base_prompt:
        user_prompt = base_prompt.replace("{context}", context_json)
    elif "{final_property_chain}" in base_prompt:
        user_prompt = base_prompt.replace("{final_property_chain}", context_json)
    else:
        user_prompt = (
            f"{base_prompt.strip()}\n\n"
            "# Input\n\n"
            "```json\n"
            f"{context_json}\n"
            "```\n"
        )

    system_msg = (
        "You are a scientific reasoning engine for battery electrochemistry.\n"
        "Return ONLY one valid JSON object. No markdown. No explanation. No extra text.\n"
        "Do not fabricate data. Do not use information outside the provided chain.\n"
        "The top-level JSON must contain: narrative, context_sentence, question, answer, "
        "context_level, is_capacity_type_explicit, reasoning_chain.\n"
        "context_level must be one of: high, medium, minimal.\n"
        "is_capacity_type_explicit must be a boolean.\n"
        "reasoning_chain must contain: target_property_identification, condition_binding, "
        "evidence_alignment, consistency_check."
    )

    return [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_prompt},
    ]


# =========================
# 输出规范化
# =========================
def normalize_output(obj: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(obj, dict):
        obj = {}

    narrative = obj.get("narrative", "")
    context_sentence = obj.get("context_sentence", "")
    question = obj.get("question", "")
    answer = obj.get("answer", "")
    context_level = obj.get("context_level", "minimal")
    is_capacity_type_explicit = obj.get("is_capacity_type_explicit", False)
    reasoning_chain = obj.get("reasoning_chain", {})

    def to_str(x: Any) -> str:
        if x is None:
            return ""
        if isinstance(x, str):
            return x
        return json.dumps(x, ensure_ascii=False) if isinstance(x, (dict, list)) else str(x)

    if isinstance(context_level, str):
        context_level = context_level.strip().lower()
    else:
        context_level = "minimal"
    if context_level not in {"high", "medium", "minimal"}:
        context_level = "minimal"

    if not isinstance(is_capacity_type_explicit, bool):
        if isinstance(is_capacity_type_explicit, str):
            is_capacity_type_explicit = is_capacity_type_explicit.strip().lower() in {"true", "yes", "1"}
        else:
            is_capacity_type_explicit = False

    if not isinstance(reasoning_chain, dict):
        reasoning_chain = {}

    reasoning_chain = {
        "target_property_identification": to_str(reasoning_chain.get("target_property_identification", "")),
        "condition_binding": to_str(reasoning_chain.get("condition_binding", "")),
        "evidence_alignment": to_str(reasoning_chain.get("evidence_alignment", "")),
        "consistency_check": to_str(reasoning_chain.get("consistency_check", "")),
    }

    return {
        "narrative": to_str(narrative),
        "context_sentence": to_str(context_sentence),
        "question": to_str(question),
        "answer": to_str(answer),
        "context_level": context_level,
        "is_capacity_type_explicit": is_capacity_type_explicit,
        "reasoning_chain": reasoning_chain,
    }


# =========================
# 模型调用（参照 zjlab_batch_ds0609.py）
# =========================
def call_model_json(
    model: str,
    messages: List[Dict[str, str]],
    temperature: float,
    max_tokens: int,
    max_retries: int,
    request_timeout: int,
    doi_label: str = "",
) -> Tuple[Dict[str, Any], str, Dict[str, Any]]:
    """
    参照 zjlab_batch_ds0609.py 的 call_model：
    - client 在函数内创建，max_retries=0
    - timeout 放在 create() 中
    - 指数退避 2**attempt
    - 捕获 APIConnectionError / APITimeoutError / APIError
    """
    client = OpenAI(
        base_url=BASE_URL,
        api_key=OPENAI_API_KEY,
        max_retries=0,
    )

    last_err = ""
    last_raw_text = ""

    for attempt in range(1, max_retries + 1):
        try:
            resp = create_structured_completion(
                client,
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=request_timeout,
            )
            raw_text, response_diagnostic = read_response_text(resp)
            if not raw_text.strip():
                raise ValueError(f"模型返回 content 为空（{response_diagnostic}）")
            last_raw_text = raw_text
            obj = extract_json_from_model_text(raw_text)
            if obj is not None:
                meta = {
                    "id": getattr(resp, "id", None),
                    "model": getattr(resp, "model", model),
                    "usage": getattr(resp, "usage", None).model_dump() if getattr(resp, "usage", None) else None,
                    "attempt": attempt,
                    "json_repair_used": False,
                }
                return obj, raw_text, meta

            # 模型返回不是 JSON 时，做一次纠偏调用
            fix_messages = messages + [{
                "role": "user",
                "content": (
                    "Your previous response was not valid JSON. Return ONLY one valid JSON object. "
                    "No markdown, no explanation, no extra text."
                )
            }]
            resp2 = create_structured_completion(
                client,
                model=model,
                messages=fix_messages,
                temperature=0,
                max_tokens=max_tokens,
                timeout=request_timeout,
            )
            raw_text2, response_diagnostic2 = read_response_text(resp2)
            if not raw_text2.strip():
                raise ValueError(f"模型修复返回 content 为空（{response_diagnostic2}）")
            last_raw_text = raw_text2
            obj2 = extract_json_from_model_text(raw_text2)
            if obj2 is not None:
                meta = {
                    "id": getattr(resp2, "id", None),
                    "model": getattr(resp2, "model", model),
                    "usage": getattr(resp2, "usage", None).model_dump() if getattr(resp2, "usage", None) else None,
                    "attempt": attempt,
                    "json_repair_used": True,
                }
                return obj2, raw_text2, meta

            raise ValueError(f"模型输出无法解析为 JSON。raw_head={last_raw_text[:1200]}")

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

    raise RuntimeError(f"LLM调用失败：{doi_label}。Last error={last_err}. Last raw head={last_raw_text[:1000]}")


# =========================
# 单文件处理
# =========================
def make_output_filename(json_path: Path, prompt_stem: str) -> str:
    return f"{safe_output_name(json_path.stem)}_{safe_output_name(prompt_stem)}_QA.json"


def init_chain_result(chain: Any, chain_idx: int, model: str) -> Dict[str, Any]:
    chain_id = ""
    if isinstance(chain, dict):
        chain_id = str(chain.get("chain_id", "") or "")
    if not chain_id:
        chain_id = f"CHAIN_{chain_idx:03d}"
    return {
        "chain_id": chain_id,
        "status": "success",
        "model": model,
        "narrative": "",
        "context_sentence": "",
        "question": "",
        "answer": "",
        "context_level": "minimal",
        "is_capacity_type_explicit": False,
        "reasoning_chain": {
            "target_property_identification": "",
            "condition_binding": "",
            "evidence_alignment": "",
            "consistency_check": "",
        },
        "error": "",
    }


def process_one_file(
    json_path: Path,
    prompt_path: Path,
    out_dir: Path,
    base_prompt: str,
    args: argparse.Namespace,
    result_jsonl: Path,
    error_jsonl: Path,
) -> Dict[str, Any]:
    prompt_stem = prompt_path.stem
    out_path = out_dir / make_output_filename(json_path, prompt_stem)

    existing_chains_qa: Optional[List[Dict[str, Any]]] = None
    retry_failed_chains = False

    if out_path.exists():
        if args.retry_failed:
            # retry_failed 模式：根据输出文件状态决定是跳过还是重试
            try:
                existing = load_json_robust(out_path)
                existing_status = existing.get("status", "")
                if existing_status == "success":
                    return {
                        "source_file": str(json_path),
                        "output_file": str(out_path),
                        "status": "skipped_existing",
                        "total_chains": 0,
                        "success_chains": 0,
                        "failed_chains": 0,
                    }
                elif existing_status == "partial_failed":
                    # 仅重试失败的 chain，保留已成功的 chain
                    existing_chains_qa = existing.get("chains_qa", [])
                    retry_failed_chains = True
                    logging.info("retry_failed: 文件 %s 先前 partial_failed，仅重试失败的 chain",
                                 json_path.name)
                else:
                    # failed 或其他异常状态：整个文件重新处理
                    logging.info("retry_failed: 文件 %s 先前 failed，重新处理全部 chain",
                                 json_path.name)
            except Exception:
                logging.warning("retry_failed: 无法读取已有输出 %s，将重新处理", out_path)
        elif args.skip_existing:
            return {
                "source_file": str(json_path),
                "output_file": str(out_path),
                "status": "skipped_existing",
                "total_chains": 0,
                "success_chains": 0,
                "failed_chains": 0,
            }

    file_result: Dict[str, Any] = {
        "source_file": str(json_path),
        "prompt_file": str(prompt_path),
        "source_doi": "",
        "total_entries": "",
        "total_chains": "",
        "final_total_chains": 0,
        "status": "processing",       # 初始状态，避免中途保存被误认为终态
        "model": args.model,
        "chains_qa": [],
        "error": "",
        "error_detail": {},
    }

    success_chains = 0
    failed_chains = 0

    try:
        data = load_json_robust(json_path)
        chains = data.get("final_property_chains", [])
        if not isinstance(chains, list) or not chains:
            raise ValueError("Input json has no valid non-empty 'final_property_chains' list.")

        file_result.update({
            "source_doi": data.get("source_doi", ""),
            "total_entries": data.get("total_entries", ""),
            "total_chains": data.get("total_chains", ""),
            "final_total_chains": len(chains),  # 使用实际链数，而非输入文件的历史元数据
        })

        for chain_idx, chain in enumerate(chains):
            # retry_failed 模式：如果该 chain 之前已成功，直接复用结果
            if retry_failed_chains and existing_chains_qa and chain_idx < len(existing_chains_qa):
                prev_result = existing_chains_qa[chain_idx]
                if prev_result.get("status") == "success":
                    prev_result.pop("raw_model_output_head", None)
                    file_result["chains_qa"].append(prev_result)
                    success_chains += 1
                    continue

            chain_result = init_chain_result(chain, chain_idx, args.model)
            raw_text = ""
            try:
                if not isinstance(chain, dict):
                    raise ValueError(f"chain is not a dict: type={type(chain).__name__}")

                messages = build_messages(base_prompt, chain, args.text_limit)
                doi_label = f"{json_path.stem}[{chain_idx}]"
                obj, raw_text, meta = call_model_json(
                    model=args.model,
                    messages=messages,
                    temperature=args.temperature,
                    max_tokens=args.max_tokens,
                    max_retries=args.max_retries,
                    request_timeout=args.request_timeout,
                    doi_label=doi_label,
                )
                chain_result.update(normalize_output(obj))
                chain_result["meta"] = meta
                success_chains += 1

            except Exception as e:
                failed_chains += 1
                chain_result["status"] = "failed"
                chain_result["error"] = repr(e)
                chain_result["raw_model_output_head"] = raw_text[:3000]
                append_jsonl(error_jsonl, {
                    "level": "chain",
                    "source_file": str(json_path),
                    "output_file": str(out_path),
                    "chain_id": chain_result.get("chain_id", ""),
                    "error_type": type(e).__name__,
                    "message": repr(e),
                    "raw_model_output_head": raw_text[:1200],
                })

            file_result["chains_qa"].append(chain_result)

            # 每条 chain 后保存一次，防止中途断掉完全丢失
            # 注意：中途保存时 status 用 "processing" 表示未完成，避免被误认为终态
            processed_count = success_chains + failed_chains
            file_result["status"] = "processing"
            file_result["final_total_chains"] = len(file_result["chains_qa"])
            file_result["summary"] = {
                "total_chains_in_file": len(chains),
                "processed_chains": processed_count,
                "success_chains": success_chains,
                "failed_chains": failed_chains,
            }
            save_json(out_path, file_result)

        # 全部 chain 处理完成后，更新元数据为最终值
        processed_count = success_chains + failed_chains
        file_result["status"] = "success" if failed_chains == 0 else "partial_failed"
        file_result["final_total_chains"] = len(file_result["chains_qa"])
        file_result["summary"] = {
            "total_chains_in_file": len(chains),
            "processed_chains": processed_count,
            "success_chains": success_chains,
            "failed_chains": failed_chains,
        }
        save_json(out_path, file_result)

        append_jsonl(result_jsonl, {
            "source_file": str(json_path),
            "output_file": str(out_path),
            "status": file_result["status"],
            "total_chains": len(chains),
            "success_chains": success_chains,
            "failed_chains": failed_chains,
        })

        return {
            "source_file": str(json_path),
            "output_file": str(out_path),
            "status": file_result["status"],
            "total_chains": len(chains),
            "success_chains": success_chains,
            "failed_chains": failed_chains,
        }

    except Exception as e:
        file_result["status"] = "failed"
        file_result["error"] = repr(e)
        file_result["error_detail"] = {
            "error_type": type(e).__name__,
            "message": str(e),
        }
        save_json(out_path, file_result)
        append_jsonl(error_jsonl, {
            "level": "file",
            "source_file": str(json_path),
            "output_file": str(out_path),
            "error_type": type(e).__name__,
            "message": repr(e),
        })
        return {
            "source_file": str(json_path),
            "output_file": str(out_path),
            "status": "failed",
            "total_chains": 0,
            "success_chains": 0,
            "failed_chains": 0,
            "error": repr(e),
        }


# =========================
# 日志
# =========================
def setup_logging(out_dir: Path) -> Path:
    ensure_dir(out_dir)
    run_log = out_dir / "run.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.FileHandler(run_log, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    return run_log


# =========================
# 主流程
# =========================
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api_key_env_file", type=str, default=DEFAULT_API_KEY_ENV_FILE)
    ap.add_argument("--prompt_md", type=str, default=DEFAULT_PROMPT_MD)
    ap.add_argument("--input_dir", type=str, default=DEFAULT_INPUT_DIR)
    ap.add_argument("--out_dir", type=str, default=DEFAULT_OUT_DIR)
    ap.add_argument("--model", type=str, default=DEFAULT_MODEL)
    ap.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    ap.add_argument("--max_tokens", type=int, default=DEFAULT_MAX_TOKENS)
    ap.add_argument("--text_limit", type=int, default=DEFAULT_TEXT_LIMIT)
    ap.add_argument("--max_workers", type=int, default=DEFAULT_MAX_WORKERS)
    ap.add_argument("--max_retries", type=int, default=DEFAULT_MAX_RETRIES)
    ap.add_argument("--request_timeout", type=int, default=DEFAULT_REQUEST_TIMEOUT)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--skip_existing", action="store_true")
    ap.add_argument("--retry_failed", action="store_true",
                    help="与 --skip_existing 配合使用：只跳过 status=success 的输出文件，"
                         "对于 failed/partial_failed 的输出重新处理。"
                         "对 partial_failed 文件仅重试失败的 chain，保留已成功的 chain。")
    ap.add_argument("--debug_input", action="store_true", help="调试模式：打印每个输入文件的详细信息和每条 chain 的 JSON 内容")
    args = ap.parse_args()

    prompt_path = Path(args.prompt_md)
    input_dir = Path(args.input_dir)
    out_dir = Path(args.out_dir)
    api_key_env_file = Path(args.api_key_env_file)

    ensure_dir(out_dir)
    run_log = setup_logging(out_dir)
    result_jsonl = out_dir / "results.jsonl"
    error_jsonl = out_dir / "errors.jsonl"
    summary_json = out_dir / "summary.json"

    if not prompt_path.exists():
        raise FileNotFoundError(f"prompt_md not found: {prompt_path}")
    if not input_dir.exists() or not input_dir.is_dir():
        raise NotADirectoryError(f"input_dir not found or not a directory: {input_dir}")

    # ---- 加载 API key 到全局变量 ----
    api_key = get_api_key(api_key_env_file)
    global OPENAI_API_KEY
    OPENAI_API_KEY = api_key

    base_prompt = read_text(prompt_path)
    json_files = list_json_files(input_dir)

    if args.start > 0:
        json_files = json_files[args.start:]
    if args.limit and args.limit > 0:
        json_files = json_files[:args.limit]

    # ---- 预过滤：排除 final_property_chains 为空列表的文件，避免无意义的 file 级 failed ----
    json_files, _ = filter_empty_chain_files(json_files)

    if not json_files:
        raise RuntimeError(f"No .json files found in: {input_dir}")

    # ---- retry_failed 预扫：统计已有输出文件的各状态分布 ----
    if args.retry_failed:
        prescan_output_files(json_files, out_dir, prompt_path.stem)

    # ---- 调试模式：打印输入文件信息 ----
    if args.debug_input:
        logging.info("=" * 60)
        logging.info("调试模式：输入文件概览")
        logging.info("=" * 60)
        for i, jf in enumerate(json_files):
            logging.info("[%d/%d] %s", i + 1, len(json_files), jf.name)
            try:
                data = load_json_robust(jf)
                chains = data.get("final_property_chains", [])
                if not isinstance(chains, list):
                    chains = []
                logging.info("  source_doi: %s", data.get("source_doi", "N/A"))
                logging.info("  total_entries: %s", data.get("total_entries", "N/A"))
                logging.info("  total_chains (meta): %s", data.get("total_chains", "N/A"))
                logging.info("  final_total_chains: %s", data.get("final_total_chains", len(chains)))
                logging.info("  actual chains count: %d", len(chains))
                for ci, chain in enumerate(chains):
                    if isinstance(chain, dict):
                        pid = chain.get("chain_id", "N/A")
                        prop = chain.get("property", {})
                        if isinstance(prop, dict):
                            prop_type = prop.get("property_type", prop.get("property", "N/A"))
                            prop_value = prop.get("property_value", "N/A")
                        else:
                            prop_type = str(prop)[:80]
                            prop_value = "N/A"
                        logging.info("    chain[%d] id=%s | property_type=%s | property_value=%s",
                                     ci, pid, prop_type, prop_value)
                    else:
                        logging.info("    chain[%d] type=%s (not a dict)", ci, type(chain).__name__)
                logging.info("  ---")
            except Exception as e:
                logging.warning("  [%s] 解析失败: %s", jf.name, e)
        logging.info("=" * 60)

    if args.max_workers > 6:
        logging.warning("当前 max_workers=%d，大于建议的 6；如遇并发问题，建议降回 6。", args.max_workers)

    logging.info("输入目录: %s", input_dir)
    logging.info("输出目录: %s", out_dir)
    logging.info("Prompt: %s", prompt_path)
    logging.info("模型: %s", args.model)
    logging.info("BASE_URL: %s", BASE_URL)
    logging.info("待处理文件数: %d", len(json_files))
    logging.info("max_workers=%d", args.max_workers)

    stats = {
        "total_files": len(json_files),
        "success_files": 0,
        "partial_failed_files": 0,
        "failed_files": 0,
        "skipped_existing_files": 0,
        "total_chains": 0,
        "success_chains": 0,
        "failed_chains": 0,
    }
    file_summaries: List[Dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
        futures = {
            executor.submit(
                process_one_file,
                json_path,
                prompt_path,
                out_dir,
                base_prompt,
                args,
                result_jsonl,
                error_jsonl,
            ): json_path
            for json_path in json_files
        }

        for future in tqdm(as_completed(futures), total=len(futures), desc="文件推理进度", unit="file"):
            json_path = futures[future]
            try:
                info = future.result()
            except Exception as e:
                info = {
                    "source_file": str(json_path),
                    "status": "thread_exception",
                    "total_chains": 0,
                    "success_chains": 0,
                    "failed_chains": 0,
                    "error": repr(e),
                }
                append_jsonl(error_jsonl, {
                    "level": "thread",
                    "source_file": str(json_path),
                    "error_type": type(e).__name__,
                    "message": repr(e),
                })
                logging.exception("线程级异常: %s", json_path)

            file_summaries.append(info)
            status = info.get("status", "")
            if status == "success":
                stats["success_files"] += 1
            elif status == "partial_failed":
                stats["partial_failed_files"] += 1
            elif status == "skipped_existing":
                stats["skipped_existing_files"] += 1
            else:
                stats["failed_files"] += 1

            stats["total_chains"] += int(info.get("total_chains", 0) or 0)
            stats["success_chains"] += int(info.get("success_chains", 0) or 0)
            stats["failed_chains"] += int(info.get("failed_chains", 0) or 0)

    summary = {
        "status": "done",
        "input_dir": str(input_dir),
        "out_dir": str(out_dir),
        "prompt_file": str(prompt_path),
        "model": args.model,
        "base_url": BASE_URL,
        "max_workers": args.max_workers,
        "max_retries": args.max_retries,
        "request_timeout": args.request_timeout,
        "run_log": str(run_log),
        "result_jsonl": str(result_jsonl),
        "error_jsonl": str(error_jsonl),
        "stats": stats,
        "files": file_summaries,
    }
    save_json(summary_json, summary)

    logging.info("全部完成。summary=%s", summary_json)
    logging.info("文件统计: %s", stats)


if __name__ == "__main__":
    main()
