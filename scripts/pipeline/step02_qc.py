#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
qc_utils_aggressive_enhanced_v2.py

V2 增强版 QC 工具（版本 2）：

V2 增强点：
1. 增加 Evidence-Context Conflict Detection（证据 - 上下文冲突检测）
2. 支持从 evidence 中提取与 property_value 绑定的 C-Rate/Cycles
3. 当 property_context 与 direct evidence 冲突时，触发 suspicious
4. 支持 respectively 配对的冲突检测

设计原则：
1. "Not Provided" / "" 是合法缺失，不是错误
2. 缺失本身不触发 suspicious
3. 只有"显式错误 / 显式冲突 / 显式多值残留"才触发 suspicious / failed
4. QC 的目标是抓错，不是抓不全
5. Evidence-First：direct evidence binding 优先级高于 property_context
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple, Optional


# =========================================================
# 基础工具
# =========================================================
EMPTY_LIKE_VALUES = {
    "",
    "not provided",
    "not_provided",
    "none",
    "null",
    "n/a",
    "na",
    "unknown",
    "not reported",
    "not specified",
    "not available",
    "-",
    "--",
}


def safe_str(x: Any) -> str:
    if x is None:
        return ""
    if isinstance(x, str):
        return x.strip()
    return str(x).strip()


def normalize_text(x: Any) -> str:
    return safe_str(x).lower()


def is_empty_like(x: Any) -> bool:
    """
    判断一个值是否是合法缺失表达。
    注意：缺失 != 错误
    """
    if x is None:
        return True
    if isinstance(x, str):
        return normalize_text(x) in EMPTY_LIKE_VALUES
    return False


def is_nonempty_dict(x: Any) -> bool:
    return isinstance(x, dict) and len(x) > 0


def dedup_keep_order(items: List[str]) -> List[str]:
    seen = set()
    result = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


# =========================================================
# 文本模式提取（增强版）
# =========================================================
def extract_c_rates(text: str) -> List[str]:
    """
    提取 C-rate，例如：
    0.1C, 1C, 0.05 C
    """
    if not text:
        return []
    hits = re.findall(r'(\d+(?:\.\d+)?)\s*C\b', text, flags=re.IGNORECASE)
    return dedup_keep_order([f"{h}C" for h in hits])


def extract_cycle_markers(text: str) -> List[str]:
    """
    提取 cycle / stage 关键词（增强版）。
    """
    if not text:
        return []

    text_l = text.lower()
    markers: List[str] = []

    # Nth cycle 模式
    nth_patterns = [
        (r'\b(\d+)(?:st|nd|rd|th)\s*cycles?\b', 'nth_cycle'),
        (r'\b(\d+)(?:st|nd|rd|th)\b', 'nth'),
        (r'\bafter\s+(\d+)\s*cycles?\b', 'after'),
        (r'\bwithin\s+(\d+)\s*cycles?\b', 'within'),
    ]

    for pattern, ptype in nth_patterns:
        for m in re.finditer(pattern, text_l, flags=re.IGNORECASE):
            num = m.group(1)
            if ptype == 'nth_cycle':
                markers.append(f"{num}th cycle")
            elif ptype == 'nth':
                markers.append(f"{num}th cycle")
            elif ptype == 'after':
                markers.append(f"after {num} cycles")
            elif ptype == 'within':
                markers.append(f"within {num} cycles")

    # 传统关键词
    keyword_patterns = [
        r'\bfirst\b',
        r'\b1st\b',
        r'\bsecond\b',
        r'\b2nd\b',
        r'\bthird\b',
        r'\b3rd\b',
        r'\b10th\b',
        r'\b100th\b',
        r'\b1000th\b',
        r'\b\d+\s*cycles?\b',
        r'\bsei\b',
        r'\bformation\b',
        r'\bactivation\b',
        r'\bdischarge\b',
        r'\bcharge\b',
    ]

    for p in keyword_patterns:
        for m in re.findall(p, text_l, flags=re.IGNORECASE):
            markers.append(m.lower())

    return dedup_keep_order(markers)


def extract_mass_loading_markers(text: str) -> List[str]:
    """
    提取载量信息，例如：
    3 mg cm⁻²
    4.5-5.5 mg cm⁻²
    """
    if not text:
        return []

    patterns = [
        r'\d+(?:\.\d+)?\s*(?:-|–|—|to)\s*\d+(?:\.\d+)?\s*mg\s*cm[⁻-]?\s*²',
        r'\d+(?:\.\d+)?\s*mg\s*cm[⁻-]?\s*²',
    ]

    hits: List[str] = []
    for p in patterns:
        hits.extend(re.findall(p, text, flags=re.IGNORECASE))

    return dedup_keep_order([h.strip() for h in hits])


def extract_voltage_windows(text: str) -> List[str]:
    """
    提取电压窗口，例如：
    2.1-4.2 V
    2 and 4 V
    2.1 to 4.2 V
    """
    if not text:
        return []

    patterns = [
        r'\d+(?:\.\d+)?\s*(?:-|–|—|to)\s*\d+(?:\.\d+)?\s*V',
        r'\d+(?:\.\d+)?\s*(?:and)\s*\d+(?:\.\d+)?\s*V',
    ]

    hits: List[str] = []
    for p in patterns:
        hits.extend(re.findall(p, text, flags=re.IGNORECASE))

    return dedup_keep_order([h.strip() for h in hits])


# =========================================================
# respectively 配对解析（新增）
# =========================================================
def parse_respectively_pairing(text: str) -> List[Tuple[str, str]]:
    """
    解析 respectively 配对结构：
    "A and B for X and Y respectively" → [(A, X), (B, Y)]
    "A and B at X and Y" → [(A, X), (B, Y)]
    "0.95 Ah and 0.87 Ah for the 1st and the 1000th cycles at 1C respectively" → [(0.95, 1st), (0.87, 1000th)]

    返回 (value, condition) 对列表
    """
    if not text:
        return []

    text_l = text.lower()
    pairs = []

    # 模式 1: "X and Y for A and B respectively" 或 "X and Y at A and B respectively"
    # 支持容量值和 C-rate/current density 条件
    respectively_pattern = r'([\d\.]+\s*(?:and\s+)*[\d\.]+)\s*(?:mAh\s*g[⁻-]?[¹1]?|Ah)\s*(?:for|at)\s+(?:the\s+)*([\d\.]+\s*(?:and\s+)*[\d\.]+)\s*(?:C|mA\s*g[⁻-]?[¹1]?)'

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

    # 模式 2: "X and Y for the Ath and Bth cycles"（专门用于 cycles）
    cycle_pattern = r'([\d\.]+\s*(?:and\s+)*[\d\.]+)\s*(?:mAh\s*g[⁻-]?[¹1]?|Ah)\s*for\s+(?:the\s+)*(\d+)(?:st|nd|rd|th)\s*(?:and\s+(?:the\s+)*(\d+)(?:st|nd|rd|th))?\s*cycles?'

    for m in re.finditer(cycle_pattern, text_l, flags=re.IGNORECASE):
        values_str = m.group(1)
        cycle1 = m.group(2)
        cycle2 = m.group(4) if m.group(4) else None

        values = re.findall(r'\d+(?:\.\d+)?', values_str)

        if cycle2 and len(values) == 2:
            pairs.append((values[0], f"{cycle1}th cycle"))
            pairs.append((values[1], f"{cycle2}th cycle"))
        elif len(values) == 1:
            pairs.append((values[0], f"{cycle1}th cycle"))

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


def extract_cycle_from_respectively(evidence_quote: str, property_value: str) -> Optional[str]:
    """
    从 respectively 配对中提取 cycle 信息（增强版）
    专门处理 "0.95 Ah and 0.87 Ah for the 1st and the 1000th cycles at 1C respectively" 这种结构
    """
    if not evidence_quote or not property_value:
        return None

    text_l = evidence_quote.lower()
    prop_val_lower = property_value.lower()

    # 模式 1: 直接在 property_value 附近找 "Nth cycle"
    val_idx = text_l.find(prop_val_lower)
    if val_idx != -1:
        # 在 property_value 附近 100 字符内找 cycle 信息
        window_start = max(0, val_idx - 100)
        window_end = min(len(text_l), val_idx + 150)
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

    # 模式 2: 解析 respectively 配对
    pairs = parse_respectively_pairing(evidence_quote)
    for val, cond in pairs:
        if val == prop_val_lower or val == prop_val_lower.replace(',', ''):
            if 'cycle' in cond.lower() or any(x in cond.lower() for x in ['st', 'nd', 'rd', 'th']):
                return cond

    return None


# =========================================================
# V2 新增：从 evidence 中提取与 property_value 绑定的 C-Rate
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
    if not evidence_quote or not property_value:
        return None

    # 清理 property_value 用于正则匹配
    prop_val_clean = re.escape(property_value.strip())

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
    - Respectively: "A, B, C at X, Y, Z mA g⁻¹ respectively" → 解析为配对
    """
    if not evidence_quote or not property_value:
        return None

    # 清理 property_value 用于正则匹配
    prop_val_clean = re.escape(property_value.strip())

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

    # 模式 4: Respectively 结构
    # "A, B, C at X, Y, Z mA g⁻¹ respectively" 或 "A, B, C mAh g⁻¹ at X, Y, Z mA g⁻¹ respectively"
    if 'respectively' in evidence_quote.lower():
        pairs = parse_respectively_pairing_for_current_density(evidence_quote)
        prop_val_lower = property_value.lower().strip()
        for val, cond in pairs:
            # 宽松匹配：允许 property_value 包含单位，但 val 只有数字
            val_clean = val.lower().strip()
            if val_clean == prop_val_lower or val_clean == prop_val_lower.replace(',', '') or val == property_value:
                return cond

    return None


def parse_respectively_pairing_for_current_density(evidence_quote: str) -> List[Tuple[str, str]]:
    """
    专门从 respectively 结构中提取 capacity-current density 配对

    示例：
    "capacities of 1200, 950, 700 mAh g⁻¹ at 100, 200, 500 mA g⁻¹, respectively"
    → [('1200', '100 mA g⁻¹'), ('950', '200 mA g⁻¹'), ('700', '500 mA g⁻¹')]

    "specific capacities of 1109, 948, 819 at 0.1, 0.2, 0.5C (100, 200, 500 mA g⁻¹), respectively"
    → [('1109', '100 mA g⁻¹'), ('948', '200 mA g⁻¹'), ('819', '500 mA g⁻¹')]

    返回 (value, condition) 对列表
    """
    if not evidence_quote or 'respectively' not in evidence_quote.lower():
        return []

    text_l = evidence_quote.lower()
    pairs = []

    # 模式："...at X, Y, Z mA g⁻¹, respectively"
    # 关键：找 "at" + 数字序列 + "mA g" + "respectively"
    # 使用更宽松的模式匹配 mA g 的各种变体：ma g-1, ma g1, ma/g, 等
    at_match = re.search(r'at\s+(.+?)\s*,\s*respectively', text_l)
    if at_match:
        rates_part = at_match.group(1)
        at_start = at_match.start()
        values_part = text_l[:at_start].strip()

        # 清理并提取容量值（数字序列）
        # 使用更宽松的模式移除单位
        values_part = re.sub(r'\s*mah\s*g.*$', '', values_part, flags=re.IGNORECASE)
        values = re.findall(r'\d+(?:\.\d+)?', values_part)

        # 从 rates_part 中提取 current density 数值
        # 使用简单模式：直接提取 "mA g" 之前的数字
        ma_match = re.search(r'([\d\s,\.]+)\s*ma\s*g', rates_part, flags=re.IGNORECASE)
        if ma_match:
            rates_str = ma_match.group(1)
            rates = re.findall(r'\d+(?:\.\d+)?', rates_str)
        else:
            # 退回到直接提取数字（排除末尾的 1）
            rates = re.findall(r'\d+(?:\.\d+)?', rates_part)
            # 排除最后一个单独的 1（通常来自 g-1）
            if rates and rates[-1] == '1':
                rates = rates[:-1]

        rates = [f"{r} mA g⁻¹" for r in rates]

        if len(values) == len(rates) and len(values) > 0:
            for v, c in zip(values, rates):
                pairs.append((v, c))

    return pairs


# =========================================================
# 多值 / 模糊表达检查
# =========================================================
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


def is_valid_voltage_window_expression(text: str) -> bool:
    """
    合法的单一电压窗口表达：
    - 2-5 V
    - 2 to 5 V
    - 2 and 5 V
    """
    if not text:
        return False

    t = safe_str(text)

    patterns = [
        r'^\d+(?:\.\d+)?\s*-\s*\d+(?:\.\d+)?\s*V$',
        r'^\d+(?:\.\d+)?\s*–\s*\d+(?:\.\d+)?\s*V$',
        r'^\d+(?:\.\d+)?\s*—\s*\d+(?:\.\d+)?\s*V$',
        r'^\d+(?:\.\d+)?\s*to\s*\d+(?:\.\d+)?\s*V$',
        r'^\d+(?:\.\d+)?\s*and\s*\d+(?:\.\d+)?\s*V$',
    ]

    for p in patterns:
        if re.match(p, t, flags=re.IGNORECASE):
            return True
    return False


def contains_obvious_atomic_multivalue(text: str) -> bool:
    """
    针对原子字段检查明显多值。
    只用于 Atomic fields，如 C_Rate / Cycles / Current_Density。
    """
    text_l = normalize_text(text)
    if not text_l:
        return False

    suspicious_patterns = [
        r',',
        r';',
        r'\band\b',
        r'\bor\b',
        r'followed by',
        r'first .* second',
        r'second .* tenth',
        r'\b\d+(?:\.\d+)?c\s*,\s*\d+(?:\.\d+)?c\b',
    ]

    for p in suspicious_patterns:
        if re.search(p, text_l):
            return True
    return False


def contains_weak_atomic_multivalue(text: str) -> bool:
    """
    弱多值 / 泛化表达。
    只用于 Atomic fields。
    """
    text_l = normalize_text(text)
    if not text_l:
        return False

    weak_patterns = [
        r'\bvarious\b',
        r'\bdifferent\b',
        r'\bmultiple\b',
        r'\bseveral\b',
        r'\bmixed\b',
        r'\bdepending on\b',
        r'\ball rates\b',
        r'\bfollowing cycles\b',
        r'\bsubsequent cycles\b',
    ]

    for p in weak_patterns:
        if re.search(p, text_l):
            return True
    return False


# =========================================================
# Evidence / Context 汇总
# =========================================================
def gather_context_texts(chain: Dict[str, Any]) -> Dict[str, str]:
    prop = chain.get("property", {})
    char = chain.get("characterization", {})
    ectx = chain.get("electrochemical_context", {})

    property_context = safe_str(prop.get("property_context"))
    property_evidence = safe_str((prop.get("evidence") or {}).get("Direct_Quote"))
    char_evidence = safe_str((char.get("evidence") or {}).get("Direct_Quote"))
    ctx_evidence = safe_str((ectx.get("evidence") or {}).get("Direct_Quote"))

    return {
        "property_context": property_context,
        "property_evidence": property_evidence,
        "char_evidence": char_evidence,
        "ctx_evidence": ctx_evidence,
    }


# =========================================================
# 各模块检查
# =========================================================
def check_required_modules(chain: Dict[str, Any]) -> List[str]:
    """
    结构完整性：顶层关键模块缺失 -> failed
    """
    reasons: List[str] = []

    required_top_keys = [
        "chain_id",
        "property",
        "characterization",
        "electrochemical_context",
        "microstructure",
        "synthesis_method",
        "material_info",
    ]

    for key in required_top_keys:
        if key not in chain:
            reasons.append(f"missing top-level key: {key}")

    prop = chain.get("property", {})
    if not isinstance(prop, dict):
        reasons.append("property is not a dict")

    return reasons


def check_critical_property_fields(chain: Dict[str, Any]) -> List[str]:
    """
    只对 property 的关键字段做强约束。
    """
    reasons: List[str] = []
    prop = chain.get("property", {})

    critical_fields = [
        "property_type",
        "property_value",
    ]

    for field in critical_fields:
        if is_empty_like(prop.get(field)):
            reasons.append(f"missing critical property field: property.{field}")

    return reasons


def check_id_consistency(chain: Dict[str, Any]) -> List[str]:
    """
    只有显式冲突才算 failed。
    缺失不算冲突。
    """
    reasons: List[str] = []

    prop = chain.get("property", {})
    char = chain.get("characterization", {})
    ctx = chain.get("electrochemical_context", {})

    linked_char_id = safe_str(prop.get("linked_char_id"))
    char_id = safe_str(char.get("char_id"))
    context_id_prop = safe_str(prop.get("context_id"))
    context_id_ctx = safe_str(ctx.get("context_id"))

    if linked_char_id and not is_empty_like(linked_char_id) and char_id and not is_empty_like(char_id):
        if linked_char_id != char_id:
            reasons.append(
                f"linked_char_id mismatch: property.linked_char_id={linked_char_id}, characterization.char_id={char_id}"
            )

    if context_id_prop and not is_empty_like(context_id_prop) and context_id_ctx and not is_empty_like(context_id_ctx):
        if context_id_prop != context_id_ctx:
            reasons.append(
                f"context_id mismatch: property.context_id={context_id_prop}, electrochemical_context.context_id={context_id_ctx}"
            )

    return reasons


def check_invalid_module_structure(chain: Dict[str, Any]) -> List[str]:
    """
    只抓模块结构明显非法的情况，不抓"内容偏少"。
    """
    reasons: List[str] = []

    char = chain.get("characterization", {})
    ectx = chain.get("electrochemical_context", {})
    micro = chain.get("microstructure", {})
    synth = chain.get("synthesis_method", {})

    module_checks = [
        ("characterization", char),
        ("electrochemical_context", ectx),
        ("microstructure", micro),
        ("synthesis_method", synth),
    ]

    for name, value in module_checks:
        if value is not None and not isinstance(value, dict):
            reasons.append(f"{name} has invalid structure")

    return reasons


def check_high_risk_multivalue_fields(chain: Dict[str, Any]) -> List[str]:
    """
    只抓显式多值残留。
    缺失不报错。
    Composite fields 宽松处理。
    """
    reasons: List[str] = []

    char = chain.get("characterization", {})
    tc = char.get("test_conditions", {})
    if not isinstance(tc, dict):
        return reasons

    # Atomic fields：严格
    atomic_fields = [
        "Temperature",
        "Current_Density",
        "Scan_Rate",
        "Frequency_Range",
        "Other_Test_Conditions",
        "C_Rate",
        "Cycles",
    ]

    for field in atomic_fields:
        value = tc.get(field)

        if is_empty_like(value):
            continue
        if not isinstance(value, str):
            continue

        if contains_obvious_atomic_multivalue(value):
            reasons.append(f"multi-value residue in test_conditions.{field}: {value}")
        elif contains_weak_atomic_multivalue(value):
            reasons.append(f"weak multi-value expression in test_conditions.{field}: {value}")

    # Voltage_Window：允许范围，但不允许多窗口混合
    voltage_value = tc.get("Voltage_Window")
    if not is_empty_like(voltage_value) and isinstance(voltage_value, str):
        if is_valid_voltage_window_expression(voltage_value):
            pass
        else:
            extracted = extract_voltage_windows(voltage_value)
            if len(extracted) > 1:
                reasons.append(f"multiple voltage windows in test_conditions.Voltage_Window: {voltage_value}")
            elif ";" in voltage_value or "," in voltage_value:
                # 这里只抓多窗口/多实验，不抓单个合法范围
                if len(extracted) >= 1:
                    reasons.append(f"suspicious Voltage_Window expression: {voltage_value}")

    # 嵌套字段：
    # Electrolyte / Solvent / Composition 属于 composite，宽松，不因为多个组分报错
    # 这里只抓明显的"多实验混合"
    nested_fields = ["Electrode_Info", "Cell_Assembly"]
    for field in nested_fields:
        sub = tc.get(field)
        if isinstance(sub, dict):
            for sub_k, sub_v in sub.items():
                if is_empty_like(sub_v):
                    continue
                if not isinstance(sub_v, str):
                    continue
                if contains_obvious_atomic_multivalue(sub_v):
                    reasons.append(f"multi-value residue in test_conditions.{field}.{sub_k}: {sub_v}")
                elif contains_weak_atomic_multivalue(sub_v):
                    reasons.append(f"weak multi-value expression in test_conditions.{field}.{sub_k}: {sub_v}")

    # electrochemical_context 中仅抓原子字段
    ectx = chain.get("electrochemical_context", {})
    risky_ctx_fields = [
        "cell_type",
        "electrode_role",
        "counter_electrode",
        "reference_electrode",
    ]
    for field in risky_ctx_fields:
        value = ectx.get(field)
        if is_empty_like(value):
            continue
        if not isinstance(value, str):
            continue
        if contains_obvious_atomic_multivalue(value):
            reasons.append(f"multi-value residue in electrochemical_context.{field}: {value}")
        elif contains_weak_atomic_multivalue(value):
            reasons.append(f"weak multi-value expression in electrochemical_context.{field}: {value}")

    return reasons


def check_property_context_alignment(chain: Dict[str, Any]) -> List[str]:
    """
    V2 增强版：
    增加 Evidence-Context Conflict Detection（证据 - 上下文冲突检测）

    只有"明确冲突"才 suspicious。
    "缺失"不再 suspicious。
    支持 respectively 配对检查。
    """
    reasons: List[str] = []

    texts = gather_context_texts(chain)
    anchor_text = " ".join([texts["property_context"], texts["property_evidence"]]).strip()

    if not anchor_text:
        return reasons

    context_c_rates = extract_c_rates(anchor_text)
    context_cycles = extract_cycle_markers(anchor_text)
    context_loading = extract_mass_loading_markers(anchor_text)
    context_voltages = extract_voltage_windows(anchor_text)

    char = chain.get("characterization", {})
    tc = char.get("test_conditions", {})
    if not isinstance(tc, dict):
        return reasons

    chain_c_rate = safe_str(tc.get("C_Rate"))
    chain_cycles = safe_str(tc.get("Cycles"))
    chain_voltage = safe_str(tc.get("Voltage_Window"))

    chain_c_rates = extract_c_rates(chain_c_rate)
    chain_cycle_markers = extract_cycle_markers(chain_cycles)
    chain_voltages = extract_voltage_windows(chain_voltage)

    # loading 在 test_conditions 或 electrochemical_context 中找
    loading_candidates: List[str] = []
    ectx = chain.get("electrochemical_context", {})
    ectx_comp = ectx.get("electrode_composition", {})
    if isinstance(ectx_comp, dict):
        for v in ectx_comp.values():
            if isinstance(v, str):
                loading_candidates.extend(extract_mass_loading_markers(v))

    electrode_info = tc.get("Electrode_Info", {})
    if isinstance(electrode_info, dict):
        for v in electrode_info.values():
            if isinstance(v, str):
                loading_candidates.extend(extract_mass_loading_markers(v))

    loading_candidates = dedup_keep_order(loading_candidates)

    # =====================
    # V2 新增：Evidence-Context Conflict Detection
    # =====================
    prop = chain.get("property", {})
    property_value = safe_str(prop.get("property_value"))
    property_context = texts["property_context"]
    property_evidence = texts["property_evidence"]
    char_evidence = texts["char_evidence"]

    # 从 property evidence 中提取与 property_value 绑定的 C-Rate
    evidence_c_rate = extract_c_rate_binding(property_evidence, property_value)

    # 从 characterization evidence 中提取与 property_value 绑定的 C-Rate
    char_evidence_c_rate = extract_c_rate_binding(char_evidence, property_value)

    # 优先使用 property evidence 的绑定
    direct_evidence_c_rate = evidence_c_rate or char_evidence_c_rate

    # 从 property evidence 中提取与 property_value 绑定的 Cycles
    evidence_cycles = extract_cycles_binding(property_evidence, property_value)
    char_evidence_cycles = extract_cycles_binding(char_evidence, property_value)
    direct_evidence_cycles = evidence_cycles or char_evidence_cycles

    # 从 property evidence 中提取与 property_value 绑定的 Current Density
    evidence_current_density = extract_current_density_binding(property_evidence, property_value)
    char_evidence_current_density = extract_current_density_binding(char_evidence, property_value)
    direct_evidence_current_density = evidence_current_density or char_evidence_current_density

    # 1) C-rate：Evidence-First 冲突检测
    if direct_evidence_c_rate:
        # direct evidence binding 存在，检查是否与 chain 中的值冲突
        if chain_c_rate and chain_c_rate != direct_evidence_c_rate:
            reasons.append(
                f"C-rate conflict: direct evidence binds {property_value} to {direct_evidence_c_rate}, "
                f"but chain.C_Rate={chain_c_rate}"
            )
        # 检查是否与 property_context 冲突（property_context 可能是 V3 Builder 错误匹配）
        prop_context_c_rates = extract_c_rates(property_context)
        if prop_context_c_rates and direct_evidence_c_rate not in prop_context_c_rates:
            # evidence binding 与 property_context 冲突，evidence 优先
            reasons.append(
                f"C-rate conflict: property_context={prop_context_c_rates}, "
                f"but direct evidence binds {property_value} to {direct_evidence_c_rate}"
            )
    elif context_c_rates and chain_c_rates:
        # 没有 direct evidence binding，用原有的 property_context vs chain 比较
        if not any(cr in chain_c_rates for cr in context_c_rates):
            reasons.append(f"C-rate mismatch: property_context={context_c_rates}, chain={chain_c_rates}")

    # 2) cycle：Evidence-First 冲突检测
    if direct_evidence_cycles:
        # direct evidence binding 存在，检查是否与 chain 中的值冲突
        if chain_cycles and direct_evidence_cycles not in chain_cycles:
            reasons.append(
                f"cycle conflict: direct evidence binds {property_value} to {direct_evidence_cycles}, "
                f"but chain.Cycles={chain_cycles}"
            )
    elif context_cycles and chain_cycle_markers:
        overlap = any(m in chain_cycle_markers for m in context_cycles)
        if not overlap:
            # 尝试 from respectively 配对
            cycle_from_respectively = extract_cycle_from_respectively(property_evidence, property_value)

            if cycle_from_respectively and cycle_from_respectively in chain_cycle_markers:
                pass  # respectively 配对已正确处理
            else:
                reasons.append(f"cycle mismatch: property_context={context_cycles}, chain={chain_cycle_markers}")

    # 2.5) Current Density：Evidence-First 冲突检测
    chain_current_density = safe_str(tc.get("Current_Density"))
    if direct_evidence_current_density:
        # direct evidence binding 存在，检查是否与 chain 中的值冲突
        if chain_current_density and chain_current_density != direct_evidence_current_density:
            reasons.append(
                f"Current Density conflict: direct evidence binds {property_value} to {direct_evidence_current_density}, "
                f"but chain.Current_Density={chain_current_density}"
            )

    # 3) loading：只有显式冲突才报
    if context_loading and loading_candidates:
        overlap = any(ld in loading_candidates for ld in context_loading)
        if not overlap:
            reasons.append(f"loading mismatch: property_context={context_loading}, chain={loading_candidates}")

    # 4) voltage：只有显式冲突才报
    if context_voltages and chain_voltages:
        overlap = any(v in chain_voltages for v in context_voltages)
        if not overlap:
            reasons.append(f"voltage mismatch: property_context={context_voltages}, chain={chain_voltages}")

    return reasons


# =========================================================
# 总判定
# =========================================================
def judge_chain_quality(chain: Dict[str, Any]) -> Tuple[str, List[str]]:
    """
    返回：
    - status: pass / suspicious / failed
    - reasons: 原因列表

    aggressive 策略：
    - failed：只抓硬错误
    - suspicious：只抓显式冲突 / 显式多值 / 显式非法结构
    - pass：包括大量合法缺失
    """
    failed_reasons: List[str] = []
    suspicious_reasons: List[str] = []

    # 1) 硬结构
    r1 = check_required_modules(chain)
    if r1:
        failed_reasons.extend(r1)

    # 2) property 核心字段
    r2 = check_critical_property_fields(chain)
    if r2:
        failed_reasons.extend(r2)

    # 3) ID 显式冲突
    r3 = check_id_consistency(chain)
    if r3:
        failed_reasons.extend(r3)

    # 4) 模块非法结构
    r4 = check_invalid_module_structure(chain)
    if r4:
        suspicious_reasons.extend(r4)

    # 5) 显式多值残留
    r5 = check_high_risk_multivalue_fields(chain)
    if r5:
        suspicious_reasons.extend(r5)

    # 6) 显式 anchor 冲突（V2 增强版：包括 Evidence-Context Conflict）
    r6 = check_property_context_alignment(chain)
    if r6:
        suspicious_reasons.extend(r6)

    if failed_reasons:
        return "failed", failed_reasons + suspicious_reasons

    if suspicious_reasons:
        return "suspicious", suspicious_reasons

    return "pass", []


def qc_chain_record(chain: Dict[str, Any]) -> Dict[str, Any]:
    """
    返回适合写入 JSON 的质检记录。
    """
    status, reasons = judge_chain_quality(chain)

    return {
        "chain_id": chain.get("chain_id"),
        "property_type": safe_str((chain.get("property") or {}).get("property_type")),
        "property_value": safe_str((chain.get("property") or {}).get("property_value")),
        "status": status,
        "reasons": reasons,
    }
