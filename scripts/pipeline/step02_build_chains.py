#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
V3 Rule-Based Property Chain Builder (V2 Enhanced Version)

定位：
- 本地代码版 V3（V2 增强版）
- 不调用大模型
- 以 Property 为中心，通过 ID 关联和规则裁剪构建 property chains
- 作为后续 QC 和 V4 fallback 的前置步骤

V2 增强点：
1. 增加 extract_c_rate_binding 和 extract_cycles_binding 函数
2. pick_best_c_rate 和 pick_best_cycles 优先使用 direct evidence binding
3. 当 property_context 与 direct evidence 冲突时，优先使用 evidence
4. 支持 respectively 配对的精确解析
5. 增加 vague expression 检测，避免捏造 C-Rate
"""

from __future__ import annotations

import ast
import json
import os
import re
import time
import traceback
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

from tqdm import tqdm


# =========================================================
# 固定路径 - 修改为你的新路径
# =========================================================
APP_DIR = Path(__file__).resolve().parent
INPUT_DIR = APP_DIR / "output" / "standalone_step01"
OUTPUT_DIR = APP_DIR / "output" / "standalone_step02"

TEST_ONLY_ONE_JSON = False
TEST_ONLY_FIRST_ENTRY = False
SKIP_IF_OUTPUT_EXISTS = False

# 并行配置
MAX_WORKERS = min(8, os.cpu_count() or 4)

# 调试打印
DEBUG_PRINT = True

# APP_CONFIG_OVERRIDE_POINT

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# 日志/汇总路径依赖注入后的输出目录
ERROR_LOG_PATH = OUTPUT_DIR / "builder_batch_errors.jsonl"
SUMMARY_PATH = OUTPUT_DIR / "builder_batch_summary.json"


# =========================================================
# 基础 IO
# =========================================================
def load_json_file(file_path: str) -> Dict[str, Any]:
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json_file(data: Dict[str, Any], file_path: str) -> None:
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    tmp_path = file_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, file_path)


def find_test_file(input_dir: Path) -> Path:
    files = sorted([p for p in input_dir.glob("*.json") if p.is_file()])
    if not files:
        raise FileNotFoundError(f"No json files found in {input_dir}")
    return files[0]

def strip_code_fence(text: str) -> str:
    text = safe_str(text)
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return text


def clean_json_like_text(text: str) -> str:
    text = strip_code_fence(text)
    text = text.replace("\ufeff", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    text = re.sub(r",(?=\s*[}\]])", "", text)
    return text


def try_parse_json_like_text(text: str) -> Dict[str, Any]:
    text = clean_json_like_text(text)

    try:
        return json.loads(text)
    except Exception:
        pass

    cleaned = text
    cleaned = re.sub(r"\bNone\b", "null", cleaned)
    cleaned = re.sub(r"\bTrue\b", "true", cleaned)
    cleaned = re.sub(r"\bFalse\b", "false", cleaned)
    cleaned = re.sub(r"\bNaN\b", "null", cleaned)
    cleaned = re.sub(r"\bInfinity\b", "null", cleaned)
    cleaned = re.sub(r"\b-Infinity\b", "null", cleaned)

    try:
        return json.loads(cleaned)
    except Exception:
        pass

    obj = ast.literal_eval(text)
    if not isinstance(obj, dict):
        raise ValueError("Parsed response block is not a dict.")
    return obj


def build_placeholder_output(
    input_file: str,
    doi: str = "Not Provided",
    source_mode: str = "parse_failed_placeholder",
    error_type: str = "UnknownError",
    error_message: str = "",
    traceback_text: str = "",
    builder_errors: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    return {
        "source_doi": doi,
        "source_file": os.path.basename(input_file),
        "source_mode": source_mode,
        "total_entries": 0,
        "total_chains": 0,
        "processing_method": "v3_rule_based_builder_v2",
        "property_chains": [],
        "builder_errors": builder_errors or [{
            "stage": source_mode,
            "error_type": error_type,
            "error_message": error_message,
            "traceback": traceback_text,
        }],
    }


# =========================================================
# 输入解析
# =========================================================
def extract_standardized_json_from_response_field(response_text: str) -> Dict[str, Any]:
    """
    从第一阶段原始输出的 response 字段中提取标准化 JSON。
    """
    response_text = response_text or ""

    start_idx = response_text.find('{\n  "Document_Structure"')
    if start_idx == -1:
        start_idx = response_text.find('{"Document_Structure"')

    if start_idx == -1:
        raise ValueError("Cannot find standardized extraction JSON in response field.")

    brace_count = 0
    end_idx = -1

    for i, ch in enumerate(response_text[start_idx:], start_idx):
        if ch == "{":
            brace_count += 1
        elif ch == "}":
            brace_count -= 1
            if brace_count == 0:
                end_idx = i + 1
                break

    if end_idx == -1:
        raise ValueError("Cannot find matching closing brace for extraction JSON.")

    json_text = response_text[start_idx:end_idx]
    return try_parse_json_like_text(json_text)


def parse_input_to_extraction_data(data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
    """
    兼容两类输入：
    1) 原始第一阶段输出：包含 doi / response
    2) 已标准化 extraction JSON：包含 Document_Structure / Entries

    返回：
    - doi
    - extraction_data
    - source_mode
    """
    if "response" in data:
        doi = data.get("doi", "Not Provided")
        extraction_data = extract_standardized_json_from_response_field(data.get("response", ""))
        return doi, extraction_data, "from_response"

    if "Document_Structure" in data and "Entries" in data:
        doi = data.get("doi", "Not Provided")
        return doi, data, "already_standardized"

    raise ValueError("Input file format is not recognized.")


# =========================================================
# 基础查找
# =========================================================
def find_characterization_by_id(characterizations: List[Dict[str, Any]], char_id: str) -> Optional[Dict[str, Any]]:
    for char in characterizations:
        if char.get("Char_ID") == char_id:
            return char
    return None


def find_electrochemical_context_by_id(contexts: List[Dict[str, Any]], context_id: str) -> Optional[Dict[str, Any]]:
    for ctx in contexts:
        if ctx.get("Context_ID") == context_id:
            return ctx
    return None


# =========================================================
# 文本提取与规则工具（V2 增强版）
# =========================================================
def safe_str(x: Any) -> str:
    if x is None:
        return ""
    if isinstance(x, str):
        return x.strip()
    return str(x).strip()


def normalize_text(x: Any) -> str:
    return safe_str(x).lower()


def build_chain_id(entry_index: int, property_index: int) -> str:
    return f"CHAIN_{entry_index:03d}_{property_index:03d}"


def extract_c_rates(text: str) -> List[str]:
    if not text:
        return []
    hits = re.findall(r'(\d+(?:\.\d+)?)\s*C\b', text, flags=re.IGNORECASE)
    return list(dict.fromkeys([f"{h}C" for h in hits]))


def extract_cycle_markers(text: str) -> List[str]:
    """
    提取 cycle marker（增强版）：
    - 支持 first/1st, second/2nd, third/3rd
    - 支持 Nth 形式（如 1000th, 100th）
    - 支持 "after N cycles", "within N cycles"
    """
    if not text:
        return []

    text_l = text.lower()
    markers: List[str] = []

    # Nth cycle 模式（增强）
    nth_patterns = [
        (r'\b(\d+)(?:st|nd|rd|th)\s*cycles?\b', 'nth_cycle'),
        (r'\b(\d+)(?:st|nd|rd|th)\b', 'nth'),
        (r'\bafter\s+(\d+)\s*cycles?\b', 'after'),
        (r'\bwithin\s+(\d+)\s*cycles?\b', 'within'),
        (r'\b(\d+)\s*cycles?\b', 'plain'),
    ]

    for pattern, ptype in nth_patterns:
        for m in re.finditer(pattern, text_l):
            num = m.group(1)
            if ptype == 'nth_cycle':
                markers.append(f"{num}th cycle")
            elif ptype == 'nth':
                markers.append(f"{num}th cycle")
            elif ptype == 'after':
                markers.append(f"after {num} cycles")
            elif ptype == 'within':
                markers.append(f"within {num} cycles")
            else:
                markers.append(f"{num} cycles")

    # 传统关键词模式
    keyword_patterns = [
        (r'\bfirst\b', 'first'),
        (r'\b1st\b', '1st'),
        (r'\bsecond\b', 'second'),
        (r'\b2nd\b', '2nd'),
        (r'\bthird\b', 'third'),
        (r'\b3rd\b', '3rd'),
        (r'\bsei\b', 'sei'),
        (r'\bformation\b', 'formation'),
        (r'\bactivation\b', 'activation'),
    ]

    for pattern, marker in keyword_patterns:
        if re.search(pattern, text_l, flags=re.IGNORECASE):
            markers.append(marker)

    return list(dict.fromkeys(markers))


def extract_voltage_windows(text: str) -> List[str]:
    if not text:
        return []

    patterns = [
        r'\d+(?:\.\d+)?\s*(?:-|–|—|to)\s*\d+(?:\.\d+)?\s*V',
        r'\d+(?:\.\d+)?\s*(?:and)\s*\d+(?:\.\d+)?\s*V',
    ]

    hits: List[str] = []
    for p in patterns:
        hits.extend(re.findall(p, text, flags=re.IGNORECASE))

    return list(dict.fromkeys([h.strip() for h in hits]))


def extract_mass_loading_markers(text: str) -> List[str]:
    if not text:
        return []

    patterns = [
        r'\d+(?:\.\d+)?\s*(?:-|–|—|to)\s*\d+(?:\.\d+)?\s*mg\s*cm[⁻-]?\s*²',
        r'\d+(?:\.\d+)?\s*mg\s*cm[⁻-]?\s*²',
    ]

    hits: List[str] = []
    for p in patterns:
        hits.extend(re.findall(p, text, flags=re.IGNORECASE))

    return list(dict.fromkeys([h.strip() for h in hits]))


def parse_respectively_pairing(text: str) -> List[Tuple[str, str]]:
    """
    解析 respectively 配对结构：
    "A and B for X and Y respectively" → [(A, X), (B, Y)]
    "A and B at X and Y" → [(A, X), (B, Y)]

    返回 (value, condition) 对列表
    """
    if not text:
        return []

    text_l = text.lower()
    pairs = []

    # 模式 1: "X and Y for A and B respectively" 或 "X and Y at A and B respectively"
    respectively_pattern = r'([\d\.]+\s*(?:and\s+)*[\d\.]+)\s*(?:mAh\s*g[⁻-]?[¹1]?|Ah)\s*(?:for|at)\s+([\d\.]+\s*(?:and\s+)*[\d\.]+)\s*(?:C|mA\s*g[⁻-]?[¹1]?)\s*(?:respectively)?'

    for m in re.finditer(respectively_pattern, text_l, flags=re.IGNORECASE):
        values_str = m.group(1)
        conditions_str = m.group(2)

        # 提取所有数值
        values = re.findall(r'\d+(?:\.\d+)?', values_str)
        conditions = re.findall(r'\d+(?:\.\d+)?', conditions_str)

        # 判断 condition 是 C-rate 还是 current density
        if 'c\b' in conditions_str.lower() or len(conditions) == len(values):
            cond_units = ['C'] * len(conditions)
        else:
            cond_units = ['mA g⁻¹'] * len(conditions)

        if len(values) == len(conditions):
            for v, c, u in zip(values, conditions, cond_units):
                pairs.append((v, f"{c}{u}"))

    return pairs


def parse_respectively_pairing_from_evidence(evidence_quote: str) -> List[Tuple[str, str]]:
    """
    专门处理证据中的 respectively 结构：
    "A, B, C mAh g⁻¹ at X, Y, ZC respectively" 或 "A, B, C at X, Y, ZC respectively"

    示例：
    "1109, 948, 819, 706, and 520 mAh g⁻¹ at increasing rates of 0.1, 0.2, 0.5, 1, and 2C, respectively"
    → [('1109', '0.1C'), ('948', '0.2C'), ('819', '0.5C'), ('706', '1C'), ('520', '2C')]

    返回 (value, condition) 对列表
    """
    if not evidence_quote or 'respectively' not in evidence_quote.lower():
        return []

    text_l = evidence_quote.lower()
    pairs = []

    # 模式："A, B, C (mAh g⁻¹) at X, Y, ZC respectively"
    # 关键：提取 respectively 之前的两部分：容量值列表和 C-rate 列表

    # 找 "at ... rates of ..." 结构
    at_match = re.search(r'at\s+(?:increasing\s+)?rates\s+of\s+(.+?)\s*(?:,\s*)?respectively', text_l)
    if at_match:
        rates_part = at_match.group(1)
        # 容量值部分：在 "at" 之前
        at_start = at_match.start()
        values_part = text_l[:at_start].strip()

        # 清理并提取容量值（数字序列）
        # 移除可能的 "mah g⁻¹" 等单位
        values_part = re.sub(r'\s*mah\s*g[⁻-]?[¹1]?\s*$', '', values_part)
        values = re.findall(r'\d+(?:\.\d+)?', values_part)

        # 提取 C-rate 值：先按逗号/and 分割，再提取每个部分中的数字+C
        # 处理 "0.1, 0.2, 0.5, 1, and 2C" 这种格式
        rate_tokens = re.split(r',\s*|\s+and\s+', rates_part)
        rates = []
        for token in rate_tokens:
            token = token.strip()
            # 提取数字和 C
            rate_match = re.search(r'(\d+(?:\.\d+)?)\s*c', token)
            if rate_match:
                rates.append(f"{rate_match.group(1)}C")
            else:
                # 尝试只提取数字（C 在最后）
                num_match = re.match(r'(\d+(?:\.\d+)?)', token)
                if num_match:
                    rates.append(f"{num_match.group(1)}C")

        # 如果 rates 只有一个元素且包含多个数字，说明是 "0.1, 0.2, 0.5C" 这种紧凑格式
        if len(rates) == 1:
            rates = re.findall(r'(\d+(?:\.\d+)?)\s*c', rates_part)
            rates = [f"{r}C" for r in rates]

        if len(values) == len(rates) and len(values) > 0:
            for v, c in zip(values, rates):
                pairs.append((v, c))

    return pairs


# =========================================================
# V2 新增：从 evidence 中提取与 property_value 绑定的 C-Rate/Cycles
# =========================================================
def extract_c_rate_binding(evidence_quote: str, property_value: str) -> Optional[str]:
    """
    从 evidence 中提取与 property_value 明确绑定的 C-Rate

    返回单个 C-Rate 值（如果找到明确绑定），否则返回 None

    支持的绑定模式：
    - "VALUE mAh g⁻¹ at XC"
    - "at XC, VALUE mAh g⁻¹"
    - "VALUE (XC)"
    - "VALUE...at XC"
    - Respectively: "A, B, C at X, Y, ZC respectively" → 解析为配对
    """
    evidence_quote = safe_str(evidence_quote)
    property_value = safe_str(property_value)
    if not evidence_quote or not property_value:
        return None

    # 清理 property_value 用于正则匹配
    prop_val_clean = re.escape(property_value)

    # 模式 1: "VALUE mAh g⁻¹ at XC"
    pattern1 = rf'{prop_val_clean}\s*mAh\s*g[⁻-]?[¹1]?\s*at\s*(\d+(?:\.\d+)?)\s*C'
    match = re.search(pattern1, evidence_quote, flags=re.IGNORECASE)
    if match:
        return f"{match.group(1)}C"

    # 模式 2: "at XC, VALUE mAh g⁻¹"
    pattern2 = rf'at\s*(\d+(?:\.\d+)?)\s*C.*?{prop_val_clean}\s*mAh\s*g[⁻-]?[¹1]?'
    match = re.search(pattern2, evidence_quote, flags=re.IGNORECASE)
    if match:
        return f"{match.group(1)}C"

    # 模式 3: "VALUE (XC)"
    pattern3 = rf'{prop_val_clean}\s*\(\s*(\d+(?:\.\d+)?)\s*C\s*\)'
    match = re.search(pattern3, evidence_quote, flags=re.IGNORECASE)
    if match:
        return f"{match.group(1)}C"

    # 模式 4: "VALUE...at XC" (更宽松的匹配)
    pattern4 = rf'{prop_val_clean}.*?at\s*(\d+(?:\.\d+)?)\s*C'
    match = re.search(pattern4, evidence_quote, flags=re.IGNORECASE)
    if match:
        return f"{match.group(1)}C"

    # 模式 5: Respectively 结构
    # "A, B, C at X, Y, ZC respectively" 或 "A, B, C mAh g⁻¹ at X, Y, ZC respectively"
    if 'respectively' in evidence_quote.lower():
        pairs = parse_respectively_pairing_from_evidence(evidence_quote)
        prop_val_lower = property_value.lower().strip()
        for val, cond in pairs:
            # 宽松匹配：允许 property_value 包含单位，但 val 只有数字
            val_clean = val.lower().strip()
            if val_clean == prop_val_lower or val_clean == prop_val_lower.replace(',', '') or val == property_value:
                if 'C' in cond and 'cycle' not in cond.lower():
                    return cond

    return None


def extract_cycles_binding(evidence_quote: str, property_value: str) -> Optional[str]:
    """
    从 evidence 中提取与 property_value 明确绑定的 Cycles 信息

    返回标准化后的 cycle 值（如果找到明确绑定），否则返回 None

    支持的绑定模式：
    - "VALUE mAh g⁻¹ at Nth cycle"
    - "VALUE mAh g⁻¹ after N cycles"
    - "VALUE mAh g⁻¹ (Nth cycle)"
    - Respectively: "A, B, C at 1st, 2nd, 100th cycles respectively" → 解析为配对
    """
    evidence_quote = safe_str(evidence_quote)
    property_value = safe_str(property_value)
    if not evidence_quote or not property_value:
        return None

    text_l = evidence_quote.lower()
    prop_val_lower = property_value.lower()

    # 优先级 1: 如果证据包含 respectively，优先从 respectively 配对中提取
    if 'respectively' in evidence_quote.lower():
        pairs = parse_respectively_pairing_from_evidence_for_cycles(evidence_quote)
        for val, cond in pairs:
            val_clean = val.lower().strip()
            if val_clean == prop_val_lower or val_clean == prop_val_lower.replace(',', '') or val == property_value:
                return cond

    # 查找 property_value 的位置
    val_idx = text_l.find(prop_val_lower)
    if val_idx == -1:
        return None

    # 在 property_value 附近 150 字符内找 cycle 信息
    window_start = max(0, val_idx - 100)
    window_end = min(len(text_l), val_idx + 150)
    window = text_l[window_start:window_end]

    # 优先级 2: 查找 "Nth cycle" 模式（直接在 property_value 附近）
    # 需要确保 cycle 标记在 property_value 之后或附近
    nth_match = re.search(r'(\d+)(?:st|nd|rd|th)\s*(?:cycle)?', window)
    if nth_match:
        num = nth_match.group(1)
        return f"{num}th cycle"

    # 优先级 3: 查找 "after N cycles" 模式
    after_match = re.search(r'after\s+(\d+)\s+cycles?', window)
    if after_match:
        num = after_match.group(1)
        return f"{num}th cycle"

    # 优先级 4: 查找 "within N cycles" 模式
    within_match = re.search(r'within\s+(\d+)\s+cycles?', window)
    if within_match:
        num = within_match.group(1)
        return f"{num}th cycle"

    # 优先级 5: 查找 "first/second cycle" 模式
    if 'first' in window or '1st' in window:
        return "1st cycle"
    if 'second' in window or '2nd' in window:
        return "2nd cycle"
    if 'initial' in window:
        return "1st cycle"

    return None


def parse_respectively_pairing_from_evidence_for_cycles(evidence_quote: str) -> List[Tuple[str, str]]:
    """
    专门从 respectively 结构中提取 capacity-cycle 配对

    示例：
    "capacities of 0.95 Ah and 0.87 Ah for the 1st and 1000th cycles at 1C, respectively"
    → [('0.95', '1st cycle'), ('0.87', '1000th cycle')]

    "discharge capacities of 1109, 948, 819 mAh g⁻¹ at 0.1, 0.2, 0.5C for the 1st, 2nd, 3rd cycles, respectively"
    → [('1109', '1st cycle'), ('948', '2nd cycle'), ('819', '3rd cycle')]
    """
    if not evidence_quote or 'respectively' not in evidence_quote.lower():
        return []

    text_l = evidence_quote.lower()
    pairs = []

    # 模式 1: "X and Y for the Ath and Bth cycles"
    # 改进：更宽松地匹配 values 部分，支持 "0.95 Ah and 0.87 Ah" 和 "0.95 and 0.87 Ah"
    cycle_pattern = r'((?:[\d\.]+\s*(?:and\s+)?)+)\s*(?:mAh\s*g[⁻-]?[¹1]?|Ah)?\s*for\s+(?:the\s+)*((?:\d+(?:st|nd|rd|th)\s*(?:and\s+)?)+)\s*cycles?'

    for m in re.finditer(cycle_pattern, text_l, flags=re.IGNORECASE):
        values_str = m.group(1)
        cycles_str = m.group(2)

        # 提取所有 capacity 值（数字）
        values = re.findall(r'\d+(?:\.\d+)?', values_str)
        # 提取所有 cycle 数字
        cycles = re.findall(r'\d+(?:st|nd|rd|th)', cycles_str)

        # 将 cycle 统一转换为 "Nth cycle" 格式
        cycles_normalized = []
        for c in cycles:
            num = re.match(r'\d+', c)
            if num:
                cycles_normalized.append(f"{num.group()}th cycle")

        if len(values) == len(cycles_normalized) and len(values) > 0:
            for v, c in zip(values, cycles_normalized):
                pairs.append((v, c))

    # 模式 2: 处理逗号分隔的列表 "A, B, C for the 1st, 2nd, 3rd cycles, respectively"
    if not pairs:
        for_match = re.search(r'for\s+(?:the\s+)*((?:\d+(?:st|nd|rd|th)\s*,?\s*(?:and\s+)?)+)\s*cycles', text_l)
        if for_match:
            cycles_str = for_match.group(1)
            # 提取 cycle 数字并标准化
            cycle_nums = re.findall(r'\d+', cycles_str)
            cycles_normalized = [f"{n}th cycle" for n in cycle_nums]

            # 尝试找 capacity 值（在 "for" 之前）
            for_start = for_match.start()
            values_part = text_l[:for_start]
            # 提取最后的数字序列（假设是 capacity 值）
            values = re.findall(r'\b(\d+(?:\.\d+)?)\b', values_part)
            # 只取与 cycles 数量相同的最后几个值
            if len(values) >= len(cycles_normalized):
                values = values[-len(cycles_normalized):]

            if len(values) == len(cycles_normalized):
                for cap, cyc in zip(values, cycles_normalized):
                    pairs.append((cap, cyc))

    return pairs


def extract_current_density_binding(evidence_quote: str, property_value: str) -> Optional[str]:
    """
    从 evidence 中提取与 property_value 明确绑定的 Current Density

    返回单个 Current Density 值（如果找到明确绑定），否则返回 None

    支持的绑定模式：
    - "VALUE mAh g⁻¹ at X mA g⁻¹"
    - "at X mA g⁻¹, VALUE mAh g⁻¹"
    - "VALUE (X mA g⁻¹)"
    - "VALUE...at X mA g⁻¹"
    - Respectively: "A, B, C at X, Y, Z mA g⁻¹ respectively" → 解析为配对
    """
    evidence_quote = safe_str(evidence_quote)
    property_value = safe_str(property_value)
    if not evidence_quote or not property_value:
        return None

    # 清理 property_value 用于正则匹配
    prop_val_clean = re.escape(property_value)

    # 模式 1: "VALUE mAh g⁻¹ at X mA g⁻¹"
    pattern1 = rf'{prop_val_clean}\s*mAh\s*g[⁻-]?[¹1]?\s*at\s*(\d+(?:\.\d+)?)\s*mA\s*g[⁻-]?[¹1]?'
    match = re.search(pattern1, evidence_quote, flags=re.IGNORECASE)
    if match:
        return f"{match.group(1)} mA g⁻¹"

    # 模式 2: "at X mA g⁻¹, VALUE mAh g⁻¹"
    pattern2 = rf'at\s*(\d+(?:\.\d+)?)\s*mA\s*g[⁻-]?[¹1]?.*?{prop_val_clean}\s*mAh\s*g[⁻-]?[¹1]?'
    match = re.search(pattern2, evidence_quote, flags=re.IGNORECASE)
    if match:
        return f"{match.group(1)} mA g⁻¹"

    # 模式 3: "VALUE (X mA g⁻¹)"
    pattern3 = rf'{prop_val_clean}\s*\(\s*(\d+(?:\.\d+)?)\s*mA\s*g[⁻-]?[¹1]?\s*\)'
    match = re.search(pattern3, evidence_quote, flags=re.IGNORECASE)
    if match:
        return f"{match.group(1)} mA g⁻¹"

    # 模式 4: "VALUE...at X mA g⁻¹" (更宽松的匹配)
    pattern4 = rf'{prop_val_clean}.*?at\s*(\d+(?:\.\d+)?)\s*mA\s*g[⁻-]?[¹1]?'
    match = re.search(pattern4, evidence_quote, flags=re.IGNORECASE)
    if match:
        return f"{match.group(1)} mA g⁻¹"

    # 模式 5: Respectively 结构
    if 'respectively' in evidence_quote.lower():
        # 尝试提取 current density 配对
        pairs = parse_respectively_pairing_for_current_density(evidence_quote)
        prop_val_lower = property_value.lower().strip()
        for val, cond in pairs:
            val_clean = val.lower().strip()
            if val_clean == prop_val_lower or val_clean == prop_val_lower.replace(',', '') or val == property_value:
                return cond

    return None


def parse_respectively_pairing_for_current_density(evidence_quote: str) -> List[Tuple[str, str]]:
    """
    专门从 respectively 结构中提取 capacity-current density 配对

    示例：
    "capacities of 1109, 948, 819 mAh g⁻¹ at 100, 200, 300 mA g⁻¹, respectively"
    → [('1109', '100 mA g⁻¹'), ('948', '200 mA g⁻¹'), ('819', '300 mA g⁻¹')]

    "specific capacities of 1200, 950, 700 mAh g-1 at 100, 200, 500 mA g-1, respectively"
    → [('1200', '100 mA g⁻¹'), ('950', '200 mA g⁻¹'), ('700', '500 mA g⁻¹')]
    """
    if not evidence_quote or 'respectively' not in evidence_quote.lower():
        return []

    text_l = evidence_quote.lower()
    pairs = []

    # 找 "at ... mA g... respectively" 结构（使用更宽松的模式）
    at_match = re.search(r'at\s+(.+?)\s*,\s*respectively', text_l)
    if at_match:
        density_part = at_match.group(1)
        at_start = at_match.start()
        values_part = text_l[:at_start].strip()

        # 清理并提取容量值（使用更宽松的模式移除单位）
        # 匹配 "mah g" 后面的任何字符直到行尾
        values_part = re.sub(r'\s*mah\s*g.*$', '', values_part, flags=re.IGNORECASE)
        values = re.findall(r'\d+(?:\.\d+)?', values_part)

        # 从 density_part 中提取 current density 数值
        # 使用简单模式：直接提取 "mA g" 之前的数字
        ma_match = re.search(r'([\d\s,\.]+)\s*ma\s*g', density_part, flags=re.IGNORECASE)
        if ma_match:
            densities_str = ma_match.group(1)
            densities = re.findall(r'\d+(?:\.\d+)?', densities_str)
        else:
            # 退回到直接提取数字（排除末尾的 1）
            densities = re.findall(r'\d+(?:\.\d+)?', density_part)
            # 排除最后一个单独的 1（通常来自 g-1）
            if densities and densities[-1] == '1':
                densities = densities[:-1]

        densities = [f"{d} mA g⁻¹" for d in densities]

        if len(values) == len(densities) and len(values) > 0:
            for v, d in zip(values, densities):
                pairs.append((v, d))

    return pairs


def extract_cycle_from_respectively(evidence_quote: str, property_value: str) -> Optional[str]:
    """
    从 respectively 配对中提取 cycle 信息
    """
    evidence_quote = safe_str(evidence_quote)
    property_value = safe_str(property_value)
    if not evidence_quote or not property_value:
        return None

    text_l = evidence_quote.lower()

    # 查找 property_value 的位置
    val_idx = text_l.find(property_value.lower())
    if val_idx == -1:
        return None

    # 在 property_value 附近 100 字符内找 cycle 信息
    window_start = max(0, val_idx - 100)
    window_end = min(len(text_l), val_idx + 100)
    window = text_l[window_start:window_end]

    # 查找 "Nth cycle" 模式
    nth_match = re.search(r'(\d+)(?:st|nd|rd|th)\s*(?:cycle)?', window)
    if nth_match:
        return f"{nth_match.group(1)}th cycle"

    # 查找 "after N cycles" 模式
    after_match = re.search(r'after\s+(\d+)\s+cycles?', window)
    if after_match:
        return f"{after_match.group(1)}th cycle"

    # 查找 "first/second cycle" 模式
    if 'first' in window:
        return "1st cycle"
    if 'second' in window:
        return "2nd cycle"

    return None


def is_vague_rate_expression(text: str) -> bool:
    """
    检测是否为模糊的 C-Rate 表达
    如果是 vague expression，应该返回 "Not Provided" 而不是捏造
    """
    if not text:
        return False

    text_l = text.lower()

    vague_patterns = [
        r'\bat\s+elevated\s+(?:current\s+)?rates\b',
        r'\bat\s+high\s+(?:current\s+)?rates\b',
        r'\bat\s+various\s+(?:current\s+)?rates\b',
        r'\bat\s+different\s+(?:current\s+)?densit(?:y|ies)\b',
        r'\bat\s+multiple\s+c-rates?\b',
        r'\bat\s+all\s+(?:current\s+)?rates\b',
        r'\bat\s+various\s+current\s+rates?\b',
    ]

    for p in vague_patterns:
        if re.search(p, text_l):
            return True

    return False


def pick_best_c_rate(original_value: str, property_context: str, property_value: str = None, evidence_quote: str = None) -> str:
    """
    V2 增强版：支持从 evidence 中根据 property_value 做局部绑定
    Evidence-First，而非 Context-First
    """
    prop_rates = extract_c_rates(property_context)
    orig_rates = extract_c_rates(original_value)

    # V2 新增：优先从 evidence 中获取 direct binding
    if property_value and evidence_quote:
        # 1. 尝试从 evidence 中提取 direct binding
        evidence_c_rate = extract_c_rate_binding(evidence_quote, property_value)
        if evidence_c_rate:
            return evidence_c_rate

        # 2. 检查是否为 vague expression
        if is_vague_rate_expression(evidence_quote):
            return "Not Provided"

    # 3. 其次从 property_context 获取
    if prop_rates:
        for pr in prop_rates:
            if pr in orig_rates or not orig_rates:
                return pr

    if len(orig_rates) == 1:
        return orig_rates[0]

    if original_value:
        return original_value

    return "Not Provided"


def pick_best_cycles(original_value: str, property_context: str, property_value: str = None, evidence_quote: str = None) -> str:
    """
    V2 增强版 cycles narrowing：
    1. 优先从 evidence 中获取 direct binding
    2. 尝试从 respectively 配对提取
    3. 支持 "Nth cycle" 模式
    4. 支持 "after N cycles", "within N cycles"
    """
    if not original_value:
        return "Not Provided"

    ctx = property_context.lower()
    orig = original_value.lower()

    # V2 新增：优先从 evidence 中获取 direct binding
    if property_value and evidence_quote:
        # 1. 尝试从 evidence 中提取 direct binding
        evidence_cycles = extract_cycles_binding(evidence_quote, property_value)
        if evidence_cycles:
            return evidence_cycles

        # 2. 尝试从 respectively 配对中提取
        cycle_from_respectively = extract_cycle_from_respectively(evidence_quote, property_value)
        if cycle_from_respectively:
            return cycle_from_respectively

    # 3. 从 property_context 提取
    # 3.1 Nth cycle 模式（如 1st, 2nd, 1000th）
    nth_match = re.search(r'(\d+)(?:st|nd|rd|th)\s*(?:cycle)?', ctx)
    if nth_match:
        num = nth_match.group(1)
        if num in orig:
            return f"{num}th cycle"

    # 3.2 first/second cycle 模式
    if "first" in ctx or "1st" in ctx:
        if "first" in orig or "1st" in orig or "1" in orig.split():
            return "1st cycle"

    if "second" in ctx or "2nd" in ctx:
        if "second" in orig or "2nd" in orig or "2" in orig.split():
            return "2nd cycle"

    # 3.3 after N cycles 模式
    after_match = re.search(r'after\s+(\d+)\s+cycles?', ctx)
    if after_match:
        num = after_match.group(1)
        if num in orig:
            return f"after {num} cycles"

    # 3.4 within N cycles 模式
    within_match = re.search(r'within\s+(\d+)\s+cycles?', ctx)
    if within_match:
        num = within_match.group(1)
        return f"within {num} cycles"

    # 3.5 stable / after SEI 模式
    if "stable" in ctx or "after sei" in ctx or "formation" in ctx:
        if "sei" in orig or "formation" in orig:
            return "after SEI formation cycles"

    # 4. 如果 property_context 本身出现了 cycle 数字，尽量保留原字段
    if re.search(r'\b\d+\s*cycles?\b', ctx):
        return original_value

    # 5. 保守 fallback
    return original_value


def pick_best_voltage(original_value: str, property_context: str) -> str:
    ctx_voltages = extract_voltage_windows(property_context)
    orig_voltages = extract_voltage_windows(original_value)

    if ctx_voltages:
        for cv in ctx_voltages:
            if cv in orig_voltages or not orig_voltages:
                return cv

    if len(orig_voltages) == 1:
        return orig_voltages[0]

    if original_value:
        return original_value

    return "Not Provided"


def pick_best_loading(original_value: str, property_context: str) -> str:
    ctx_loadings = extract_mass_loading_markers(property_context)
    orig_loadings = extract_mass_loading_markers(original_value)

    if ctx_loadings:
        for cl in ctx_loadings:
            if cl in orig_loadings or not orig_loadings:
                return cl

    if len(orig_loadings) == 1:
        return orig_loadings[0]

    if original_value:
        return original_value

    return "Not Provided"


def maybe_narrow_text_field(original_value: Any, property_context: str, field_name: str,
                            property_value: str = None, evidence_quote: str = None) -> Any:
    """
    对高风险字符串字段做基础规则收窄（V2 增强版）。
    传入 property_value 和 evidence_quote 用于更精确的局部绑定。
    """
    if not isinstance(original_value, str):
        return original_value

    text = original_value.strip()
    if not text:
        return "Not Provided"

    field_lower = field_name.lower()

    if field_lower == "c_rate":
        return pick_best_c_rate(text, property_context, property_value, evidence_quote)

    if field_lower == "cycles":
        return pick_best_cycles(text, property_context, property_value, evidence_quote)

    if field_lower == "voltage_window":
        return pick_best_voltage(text, property_context)

    if field_lower in {"mass_loading", "other_test_conditions"}:
        return pick_best_loading(text, property_context)

    # 对其他字段采取保守策略：原样返回
    return text


# =========================================================
# test_conditions 对齐（V2 增强版）
# =========================================================
def extract_property_aligned_test_conditions(
    test_conditions: Dict[str, Any],
    property_context: str,
    property_value: str = None,
    evidence_quote: str = None
) -> Dict[str, Any]:
    """
    对 test_conditions 做 property-aware 规则收窄（V2 增强版）。
    传入 property_value 和 evidence_quote 用于更精确的局部绑定。
    """
    if not isinstance(test_conditions, dict) or not test_conditions:
        return {}

    aligned = deepcopy(test_conditions)

    # 顶层高风险字段
    top_level_fields = [
        "Temperature",
        "Voltage_Window",
        "Current_Density",
        "Scan_Rate",
        "Frequency_Range",
        "Other_Test_Conditions",
        "C_Rate",
        "Cycles",
    ]

    for field in top_level_fields:
        if field in aligned:
            aligned[field] = maybe_narrow_text_field(
                aligned[field],
                property_context,
                field,
                property_value,
                evidence_quote
            )

    # 嵌套字段：Electrode_Info
    electrode_info = aligned.get("Electrode_Info")
    if isinstance(electrode_info, dict):
        for sub_key in list(electrode_info.keys()):
            electrode_info[sub_key] = maybe_narrow_text_field(
                electrode_info[sub_key],
                property_context,
                sub_key,
                property_value,
                evidence_quote
            )

    # 嵌套字段：Cell_Assembly
    cell_assembly = aligned.get("Cell_Assembly")
    if isinstance(cell_assembly, dict):
        for sub_key in list(cell_assembly.keys()):
            cell_assembly[sub_key] = maybe_narrow_text_field(
                cell_assembly[sub_key],
                property_context,
                sub_key,
                property_value,
                evidence_quote
            )

    # 嵌套字段：Electrolyte
    electrolyte = aligned.get("Electrolyte")
    if isinstance(electrolyte, dict):
        for sub_key in list(electrolyte.keys()):
            electrolyte[sub_key] = maybe_narrow_text_field(
                electrolyte[sub_key],
                property_context,
                sub_key,
                property_value,
                evidence_quote
            )

    return aligned


# =========================================================
# electrochemical_context 对齐
# =========================================================
def extract_property_aligned_electrochemical_context(
    electrochemical_context: Dict[str, Any],
    property_context: str
) -> Dict[str, Any]:
    """
    对 electrochemical_context 做有限的 property-aware 收窄。
    目前主要对 electrode_composition 中的载量类字段做基础 narrowing。
    """
    if not isinstance(electrochemical_context, dict) or not electrochemical_context:
        return {}

    aligned = deepcopy(electrochemical_context)

    electrode_composition = aligned.get("electrode_composition")
    if isinstance(electrode_composition, dict):
        for sub_key in list(electrode_composition.keys()):
            electrode_composition[sub_key] = maybe_narrow_text_field(
                electrode_composition[sub_key],
                property_context,
                sub_key
            )

    return aligned


# =========================================================
# 构建单条 chain（V2 增强版）
# =========================================================
def build_property_chain(
    entry: Dict[str, Any],
    property_item: Dict[str, Any],
    entry_index: int,
    property_index: int
) -> Dict[str, Any]:
    linked_char_id = property_item.get("Linked_Char_ID", "")
    context_id = property_item.get("Context_ID", "")
    property_context = property_item.get("Property_Context", "Not Provided")
    property_value = property_item.get("Property_Value", "Not Provided")
    evidence = property_item.get("Evidence", {})
    evidence_quote = evidence.get("Direct_Quote", "") if isinstance(evidence, dict) else ""

    characterizations = entry.get("Characterization", [])
    electrochemical_contexts = entry.get("Electrochemical_Contexts", [])

    characterization = None
    if linked_char_id:
        characterization = find_characterization_by_id(characterizations, linked_char_id)

    electrochemical_context = None
    if context_id:
        electrochemical_context = find_electrochemical_context_by_id(
            electrochemical_contexts,
            context_id
        )

    chain_id = build_chain_id(entry_index, property_index)

    chain = {
        "chain_id": chain_id,
        "material_info": {
            "material_name": entry.get("Material_Name", "Not Provided"),
            "material_formula": entry.get("Material_Formula", "Not Provided"),
            "material_type": entry.get("Material_Type", {})
        },
        "electrochemical_context": {},
        "property": {},
        "characterization": {},
        "microstructure": {},
        "synthesis_method": {},
        "v3_builder_meta": {
            "builder_mode": "v2_rule_based_id_linking",  # V2 标识
            "entry_index": entry_index,
            "property_index": property_index,
            "used_linked_char_id": linked_char_id if linked_char_id else "Not Provided",
            "used_context_id": context_id if context_id else "Not Provided",
            "characterization_found": characterization is not None,
            "electrochemical_context_found": electrochemical_context is not None,
            "property_context": property_context,
            "property_value": property_value,
            "evidence_quote": evidence_quote,
            "narrowing_applied": True,
            "narrowing_version": "v2_evidence_first",  # V2 标识
        }
    }

    # electrochemical_context
    if electrochemical_context:
        raw_context = {
            "context_id": electrochemical_context.get("Context_ID", "Not Provided"),
            "cell_type": electrochemical_context.get("Cell_Type", "Not Provided"),
            "electrode_role": electrochemical_context.get("Electrode_Role", "Not Provided"),
            "counter_electrode": electrochemical_context.get("Counter_Electrode", "Not Provided"),
            "reference_electrode": electrochemical_context.get("Reference_Electrode", "Not Provided"),
            "electrolyte_system": electrochemical_context.get("Electrolyte_System", {}),
            "electrode_composition": electrochemical_context.get("Electrode_Composition", {}),
            "evidence": electrochemical_context.get("Evidence", {})
        }
        chain["electrochemical_context"] = extract_property_aligned_electrochemical_context(
            raw_context,
            property_context
        )
    else:
        chain["electrochemical_context"] = {
            "error": "Electrochemical Context not found",
            "context_id": context_id if context_id else "Not Provided"
        }

    # property
    chain["property"] = {
        "property_type": property_item.get("Property_Type", "Not Provided"),
        "property_value": property_value,
        "unit": property_item.get("Unit", "Not Provided"),
        "associated_test_method": property_item.get("Associated_Test_Method", "Not Provided"),
        "linked_char_id": linked_char_id if linked_char_id else "Not Provided",
        "context_id": context_id if context_id else "Not Provided",
        "property_context": property_context,
        "evidence": evidence
    }

    # characterization（V2 增强版 narrowing）
    if characterization:
        # 获取 characterization 的 evidence quote 用于 narrowing
        char_evidence = characterization.get("Evidence", {})
        char_evidence_quote = char_evidence.get("Direct_Quote", "") if isinstance(char_evidence, dict) else ""

        specific_test_conditions = extract_property_aligned_test_conditions(
            characterization.get("Test_Conditions", {}),
            property_context,
            property_value,
            evidence_quote  # 优先使用 property evidence
        )

        chain["characterization"] = {
            "char_id": characterization.get("Char_ID", "Not Provided"),
            "technique": characterization.get("Technique", "Not Provided"),
            "measured_target": characterization.get("Measured_Target", "Not Provided"),
            "linked_properties": characterization.get("Linked_Property_or_Properties", []),
            "test_conditions": specific_test_conditions,
            "evidence": characterization.get("Evidence", {})
        }
    else:
        chain["characterization"] = {
            "error": "Characterization not found",
            "linked_char_id": linked_char_id if linked_char_id else "Not Provided"
        }

    # microstructure（兼容 dict 和 list 两种格式）
    microstructure = entry.get("Microstructure", {})
    if isinstance(microstructure, list):
        # list 格式：转为 dict，用 index 做 key 保留原始信息
        micro_dict = {}
        for i, item in enumerate(microstructure):
            if isinstance(item, dict):
                for k, v in item.items():
                    micro_dict[k] = v
            else:
                micro_dict[f"item_{i}"] = str(item)
        microstructure = micro_dict
    chain["microstructure"] = {
        "standardized_type": microstructure.get("Standardized_Type", []),
        "morphology": microstructure.get("Morphology", "Not Provided"),
        "particle_size_or_dimension": microstructure.get("Particle_Size_or_Dimension", "Not Provided"),
        "structural_features": microstructure.get("Structural_Features", "Not Provided"),
        "phase_or_crystallinity": microstructure.get("Phase_or_Crystallinity", "Not Provided"),
        "interface_features": microstructure.get("Interface_Features", "Not Provided"),
        "transport_relevant_features": microstructure.get("Transport_Relevant_Features", "Not Provided"),
        "evidence": microstructure.get("Evidence", {})
    }

    # synthesis_method
    synthesis_method = entry.get("Synthesis_Method", {})
    chain["synthesis_method"] = {
        "synthesis_techniques": synthesis_method.get("Synthesis_Techniques", []),
        "reaction_type": synthesis_method.get("Reaction_Type", []),
        "detailed_steps": synthesis_method.get("Detailed_Steps", []),
        "synthesis_reactions": synthesis_method.get("Synthesis_Reactions", []),
        "final_product_name": synthesis_method.get("Final_Product_Name", "Not Provided"),
        "final_product_formula": synthesis_method.get("Final_Product_Formula", "Not Provided"),
        "is_commercial": synthesis_method.get("Is_Commercial", False),
        "commercial_source": synthesis_method.get("Commercial_Source", ""),
        "evidence": synthesis_method.get("Evidence", {})
    }

    return chain


# =========================================================
# 单文件处理
# =========================================================
def process_single_file(input_file: str, output_dir: str) -> Dict[str, Any]:
    output_path = make_output_path(input_file, output_dir)

    if SKIP_IF_OUTPUT_EXISTS and os.path.exists(output_path):
        return {
            "input_file": input_file,
            "output_file": output_path,
            "entries_processed": 0,
            "chains_built": 0,
            "status": "skipped_existing"
        }

    data = load_json_file(input_file)
    doi = data.get("doi", "Not Provided") if isinstance(data, dict) else "Not Provided"

    try:
        doi, extraction_data, source_mode = parse_input_to_extraction_data(data)
    except Exception as e:
        placeholder = build_placeholder_output(
            input_file=input_file,
            doi=doi,
            source_mode="from_response_parse_failed",
            error_type=type(e).__name__,
            error_message=str(e),
            traceback_text=traceback.format_exc(),
        )
        save_json_file(placeholder, output_path)
        return {
            "input_file": input_file,
            "output_file": output_path,
            "entries_processed": 0,
            "chains_built": 0,
            "status": "success_with_parse_fallback"
        }

    entries = extraction_data.get("Entries", [])
    if not isinstance(entries, list):
        placeholder = build_placeholder_output(
            input_file=input_file,
            doi=doi,
            source_mode=f"{source_mode}_invalid_entries",
            error_type="InvalidEntriesError",
            error_message="Extraction JSON does not contain a valid 'Entries' list.",
            traceback_text="",
        )
        save_json_file(placeholder, output_path)
        return {
            "input_file": input_file,
            "output_file": output_path,
            "entries_processed": 0,
            "chains_built": 0,
            "status": "success_with_parse_fallback"
        }

    if TEST_ONLY_FIRST_ENTRY:
        entries = entries[:1]

    all_chains: List[Dict[str, Any]] = []
    chain_count = 0
    chain_build_errors: List[Dict[str, Any]] = []

    for entry_index, entry in enumerate(entries):
        properties = entry.get("Property", [])
        if not isinstance(properties, list):
            chain_build_errors.append({
                "stage": "entry_property_list",
                "entry_index": entry_index,
                "error_type": "InvalidPropertyList",
                "error_message": "entry['Property'] is not a list",
            })
            continue

        for property_index, property_item in enumerate(properties):
            try:
                chain = build_property_chain(entry, property_item, entry_index, property_index)
                all_chains.append(chain)
                chain_count += 1
            except Exception as e:
                chain_build_errors.append({
                    "stage": "build_property_chain",
                    "entry_index": entry_index,
                    "property_index": property_index,
                    "property_type": safe_str(property_item.get("Property_Type", "Not Provided")) if isinstance(property_item, dict) else "Not Provided",
                    "property_value": safe_str(property_item.get("Property_Value", "Not Provided")) if isinstance(property_item, dict) else "Not Provided",
                    "error_type": type(e).__name__,
                    "error_message": str(e),
                    "traceback": traceback.format_exc(),
                })
                continue

    output_data = {
        "source_doi": doi,
        "source_file": os.path.basename(input_file),
        "source_mode": source_mode,
        "total_entries": len(entries),
        "total_chains": chain_count,
        "processing_method": "v3_rule_based_builder_v2",
        "property_chains": all_chains,
        "builder_errors": chain_build_errors,
    }

    save_json_file(output_data, output_path)

    status = "success_with_partial_chain_errors" if chain_build_errors else "success"
    return {
        "input_file": input_file,
        "output_file": output_path,
        "entries_processed": len(entries),
        "chains_built": chain_count,
        "chain_build_errors": len(chain_build_errors),
        "status": status
    }


# =========================================================
# 批量处理辅助工具
# =========================================================
def make_output_path(input_file: str, output_dir: str) -> str:
    base_name = os.path.basename(input_file)
    if base_name.endswith(".json"):
        output_name = base_name.replace(".json", "_property_chains_0422.json")
    else:
        output_name = base_name + "_property_chains_0422.json"
    return os.path.join(output_dir, output_name)


def append_jsonl_record(file_path: Path, record: Dict[str, Any]) -> None:
    with open(file_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def process_single_file_safe(input_file: str, output_dir: str) -> Dict[str, Any]:
    start_time = time.time()
    output_path = make_output_path(input_file, output_dir)

    try:
        result = process_single_file(input_file, output_dir)
        result = dict(result)
        result["elapsed_seconds"] = round(time.time() - start_time, 3)
        return result

    except Exception as e:
        error_record = {
            "input_file": input_file,
            "output_file": output_path,
            "status": "failed",
            "error_type": type(e).__name__,
            "error_message": str(e),
            "traceback": traceback.format_exc(),
            "elapsed_seconds": round(time.time() - start_time, 3),
        }
        append_jsonl_record(ERROR_LOG_PATH, error_record)
        return error_record


# =========================================================
# 目录处理（并行批量版）
# =========================================================
def process_directory(input_dir: str, output_dir: str) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []

    json_files = sorted(list(Path(input_dir).glob("*.json")))
    if not json_files:
        raise FileNotFoundError(f"No json files found in {input_dir}")

    if TEST_ONLY_ONE_JSON and json_files:
        json_files = json_files[:1]

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_file = {
            executor.submit(process_single_file_safe, str(json_file), output_dir): json_file
            for json_file in json_files
        }

        with tqdm(total=len(future_to_file), desc="01 Builder Files", ncols=110) as pbar:
            for future in as_completed(future_to_file):
                json_file = future_to_file[future]

                try:
                    result = future.result()
                except Exception as e:
                    result = {
                        "input_file": str(json_file),
                        "output_file": make_output_path(str(json_file), output_dir),
                        "status": "failed",
                        "error_type": type(e).__name__,
                        "error_message": str(e),
                        "traceback": traceback.format_exc(),
                    }
                    append_jsonl_record(ERROR_LOG_PATH, result)

                results.append(result)

                status = result.get("status", "unknown")
                if status == "failed":
                    tqdm.write(
                        f"[FAIL] {os.path.basename(str(json_file))} | "
                        f"{result.get('error_type', 'Error')}: {result.get('error_message', '')}"
                    )
                elif status == "skipped_existing":
                    tqdm.write(f"[SKIP] {os.path.basename(str(json_file))}")
                else:
                    tqdm.write(
                        f"[OK] {result.get('chains_built', 0)} chains from "
                        f"{os.path.basename(str(json_file))}"
                    )

                pbar.update(1)

    return results


# =========================================================
# 主程序
# =========================================================
def main() -> None:
    input_dir = str(INPUT_DIR)
    output_dir = str(OUTPUT_DIR)

    print("=" * 70)
    print("V3 Rule-Based Property Chain Builder (V2 Enhanced Version)")
    print("=" * 70)
    print(f"Input directory        : {input_dir}")
    print(f"Output directory       : {output_dir}")
    print(f"TEST_ONLY_ONE_JSON     : {TEST_ONLY_ONE_JSON}")
    print(f"TEST_ONLY_FIRST_ENTRY  : {TEST_ONLY_FIRST_ENTRY}")
    print(f"SKIP_IF_OUTPUT_EXISTS  : {SKIP_IF_OUTPUT_EXISTS}")
    print(f"MAX_WORKERS            : {MAX_WORKERS}")
    print(f"ERROR_LOG_PATH         : {ERROR_LOG_PATH}")
    print(f"SUMMARY_PATH           : {SUMMARY_PATH}")
    print()

    if os.path.isdir(input_dir):
        print("Processing directory (parallel batch mode)...")
        results = process_directory(input_dir, output_dir)

        successful = sum(1 for r in results if str(r.get("status", "")).startswith("success"))
        skipped = sum(1 for r in results if r.get("status") == "skipped_existing")
        failed = sum(1 for r in results if r.get("status") == "failed")
        total_chains = sum(r.get("chains_built", 0) for r in results if str(r.get("status", "")).startswith("success"))

        summary = {
            "input_dir": input_dir,
            "output_dir": output_dir,
            "test_only_one_json": TEST_ONLY_ONE_JSON,
            "test_only_first_entry": TEST_ONLY_FIRST_ENTRY,
            "skip_if_output_exists": SKIP_IF_OUTPUT_EXISTS,
            "max_workers": MAX_WORKERS,
            "total_files": len(results),
            "successful_files": successful,
            "skipped_existing_files": skipped,
            "failed_files": failed,
            "total_chains_built": total_chains,
            "error_log_path": str(ERROR_LOG_PATH),
            "results": results,
        }
        save_json_file(summary, str(SUMMARY_PATH))

        print()
        print("=" * 70)
        print("Summary")
        print("=" * 70)
        print(f"Files processed        : {len(results)}")
        print(f"Successful             : {successful}")
        print(f"Skipped existing       : {skipped}")
        print(f"Failed                 : {failed}")
        print(f"Total chains built     : {total_chains}")
        print(f"Summary saved to       : {SUMMARY_PATH}")
        print(f"Detailed errors saved  : {ERROR_LOG_PATH}")

    elif os.path.isfile(input_dir):
        print("Processing single file...")
        result = process_single_file_safe(input_dir, output_dir)

        print()
        print("=" * 70)
        print("Summary")
        print("=" * 70)
        print(f"Chains built           : {result.get('chains_built', 0)}")
        print(f"Output saved           : {result.get('output_file', 'Not Provided')}")
        print(f"Status                 : {result.get('status', 'unknown')}")
        if result.get("status") == "failed":
            print(f"Error type             : {result.get('error_type', 'Error')}")
            print(f"Error message          : {result.get('error_message', '')}")
            print(f"Detailed errors saved  : {ERROR_LOG_PATH}")
    else:
        raise FileNotFoundError(f"Input path does not exist: {input_dir}")


if __name__ == "__main__":
    main()
