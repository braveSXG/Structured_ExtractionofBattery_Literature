"""Batch process multiple JSON files, clean fields in-place, output as JSON.

Output: for each input JSON, write a cleaned copy with '_cleaned' suffix.
Only cleaned fields differ from the original; all other fields remain untouched.
"""

import json
import sys
import os
import copy
import re
from pathlib import Path
from typing import Dict, Any, List, Optional

# Handle Windows console encoding
if sys.platform == 'win32':
    os.system('chcp 65001 > nul')
    sys.stdout.reconfigure(encoding='utf-8') if hasattr(sys.stdout, 'reconfigure') else None

APP_DIR = Path(__file__).resolve().parent
INPUT_DIR = APP_DIR / "output" / "standalone_step02"
OUTPUT_DIR = APP_DIR / "output" / "standalone_step03"

# APP_CONFIG_OVERRIDE_POINT


# ===========================================================================
# 自研清洗工具：公式标准化、单位标准化、术语映射
# 全部为自主编写，无外部依赖，可用于软著申请
# ===========================================================================

def split_voltage_window(value: str) -> Dict[str, Optional[float]]:
    """从电压窗字符串中提取最小/最大值，如 '2.5-4.3 V' -> {'voltage_min': 2.5, 'voltage_max': 4.3}"""
    if not value or not isinstance(value, str):
        return {"voltage_min": None, "voltage_max": None}
    cleaned = re.sub(r'\s*V\b', '', value, flags=re.IGNORECASE)
    matches = re.findall(r'(\d+(?:\.\d+)?)', cleaned)
    if len(matches) >= 2:
        return {"voltage_min": float(matches[0]), "voltage_max": float(matches[1])}
    elif len(matches) == 1:
        return {"voltage_min": float(matches[0]), "voltage_max": None}
    return {"voltage_min": None, "voltage_max": None}


def clean_string(value: str) -> str:
    """清理字符串：去除多余空白。"""
    if not value or not isinstance(value, str):
        return value
    return ' '.join(value.split()).strip()


class UnitNormalizer:
    """单位标准化器 —— 纯正则 + 字典映射，无外部依赖。

    覆盖：温度、时间、电压、电流密度等电池领域常见单位。
    """

    TIME_MAP = {
        "h": "h", "hour": "h", "hours": "h", "hr": "h", "hrs": "h",
        "min": "min", "minute": "min", "minutes": "min",
        "s": "s", "sec": "s", "second": "s", "seconds": "s",
    }
    TEMP_MAP = {
        "°c": "°C", "celsius": "°C",
        "°f": "°C", "fahrenheit": "°C",
        "k": "K", "kelvin": "K",
    }

    def normalize(self, value: str, field_name: str) -> str:
        """标准化单位字符串。"""
        if not value or not isinstance(value, str):
            return value
        s = value.strip()
        field = field_name.lower()

        # Temperature
        if "temp" in field:
            s_lower = s.lower()
            for alias, std in self.TEMP_MAP.items():
                if alias in s_lower:
                    s = s.replace(alias, std)
                    break
            if "room" in s_lower:
                return "25 °C"
            return s

        # Time
        if "time" in field:
            m = re.search(r"([\d.]+)\s*(\w+)", s)
            if m:
                num, unit = m.groups()
                unit = self.TIME_MAP.get(unit.lower(), unit)
                return f"{num} {unit}"
            return s

        # Voltage
        if "voltage" in field:
            s_clean = re.sub(r"\s*V\b", " V", s, flags=re.IGNORECASE).strip()
            return s_clean

        # Current Density (e.g. "5 mA cm⁻²")
        if "current" in field or "density" in field:
            s_clean = s.replace("mAcm⁻²", "mA cm⁻²").replace("mAg⁻¹", "mA g⁻¹")
            s_clean = s_clean.replace("Ag⁻¹", "A g⁻¹").replace("mAg-1", "mA g⁻¹")
            return s_clean

        # Mass Loading / C-Rate
        if "mass" in field or "loading" in field or "rate" in field:
            m = re.search(r"([\d.]+)\s*(mg\s*cm⁻²|mg\s*cm-2|mg\s*cm⁻2|mAh\s*g⁻¹|C)", s, re.IGNORECASE)
            if m:
                num, unit = m.groups()
                unit = unit.replace("cm-2", "cm⁻²").replace("cm⁻2", "cm⁻²")
                return f"{num} {unit}"
            return s

        return s


class FormulaNormalizer:
    """化学式标准化器 —— 处理下标字符、分数、空格等。"""

    SUBSCRIPT_MAP = {
        "₀": "0", "₁": "1", "₂": "2", "₃": "3", "₄": "4",
        "₅": "5", "₆": "6", "₇": "7", "₈": "8", "₉": "9",
        "⁻": "-", "⁺": "+",
    }
    FRAC_MAP = {"1/2": "0.5", "1/3": "0.33", "2/3": "0.67", "1/4": "0.25", "3/4": "0.75"}

    def normalize(self, formula: str) -> str:
        """标准化化学式：去空格、统一下标、处理分数。"""
        if not formula or not isinstance(formula, str):
            return formula
        s = formula.strip()
        s = s.replace(" ", "")
        for uni, num in self.SUBSCRIPT_MAP.items():
            s = s.replace(uni, num)
        for frac, dec in self.FRAC_MAP.items():
            s = s.replace(frac, dec)
        return s


class TerminologyNormalizer:
    """术语标准化器 —— 字典映射，统一电池领域常见术语表述。"""

    SYNTHESIS_MAP = {
        "sol-gel": "sol-gel", "sol gel": "sol-gel", "solgel": "sol-gel",
        "hydrothermal": "hydrothermal", "hydrothermal method": "hydrothermal",
        "solid-state": "solid-state", "solid state": "solid-state",
        "co-precipitation": "co-precipitation", "coprecipitation": "co-precipitation",
        "ball milling": "ball milling", "ball-milling": "ball milling",
        "electrospinning": "electrospinning",
        "spray pyrolysis": "spray pyrolysis",
        "combustion": "combustion synthesis", "combustion method": "combustion synthesis",
    }
    ATMOSPHERE_MAP = {
        "air": "air", "oxygen": "O₂", "o2": "O₂",
        "nitrogen": "N₂", "n2": "N₂",
        "argon": "Ar", "ar": "Ar",
        "vacuum": "vacuum",
        "hydrogen": "H₂", "h2": "H₂", "forming gas": "forming gas",
    }
    SOLVENT_MAP = {
        "water": "H₂O", "h2o": "H₂O", "deionized water": "H₂O", "di water": "H₂O",
        "ethanol": "ethanol", "ethyl alcohol": "ethanol",
        "methanol": "methanol",
        "nmp": "NMP", "n-methyl-2-pyrrolidone": "NMP", "n-methylpyrrolidone": "NMP",
        "dmf": "DMF", "dimethylformamide": "DMF",
        "dmso": "DMSO", "dimethyl sulfoxide": "DMSO",
    }
    MORPHOLOGY_MAP = {
        "nanoparticle": "nanoparticle", "nanoparticles": "nanoparticle", "np": "nanoparticle",
        "nanowire": "nanowire", "nanowires": "nanowire",
        "nanosheet": "nanosheet", "nanosheets": "nanosheet",
        "nanorod": "nanorod", "nanorods": "nanorod",
        "nanosphere": "nanosphere", "nanospheres": "nanosphere",
        "porous": "porous", "hollow": "hollow",
        "spherical": "spherical", "cubic": "cubic",
        "core-shell": "core-shell", "core–shell": "core-shell",
        "flake": "flake", "flakes": "flake",
    }
    ELECTRODE_MAP = {
        "cathode": "cathode", "positive": "cathode", "positive electrode": "cathode",
        "anode": "anode", "negative": "anode", "negative electrode": "anode",
        "working electrode": "working-electrode", "we": "working-electrode",
        "counter electrode": "counter-electrode", "ce": "counter-electrode",
        "reference electrode": "reference-electrode", "re": "reference-electrode",
    }
    CELL_MAP = {
        "coin cell": "coin cell", "coin": "coin cell", "coin-type": "coin cell",
        "pouch cell": "pouch cell", "pouch": "pouch cell", "pouch-type": "pouch cell",
        "half cell": "half cell", "half-cell": "half cell",
        "full cell": "full cell", "full-cell": "full cell",
        "swagelok": "Swagelok", "swagelok cell": "Swagelok",
        "three-electrode": "three-electrode", "three electrode": "three-electrode",
    }

    def normalize(self, value: str, field_name: str) -> str:
        """标准化术语字符串。"""
        if not value or not isinstance(value, str):
            return value
        v = value.strip()
        v_lower = v.lower()
        field = field_name.lower()

        if "synthesis" in field or "technique" in field:
            return self.SYNTHESIS_MAP.get(v_lower, v)
        if "atmosphere" in field:
            return self.ATMOSPHERE_MAP.get(v_lower, v)
        if "solvent" in field:
            return self.SOLVENT_MAP.get(v_lower, v)
        if "morphology" in field or "particle" in field:
            return self.MORPHOLOGY_MAP.get(v_lower, v)
        if "electrode" in field or "role" in field:
            return self.ELECTRODE_MAP.get(v_lower, v)
        if "cell_type" in field or "cell" in field:
            return self.CELL_MAP.get(v_lower, v)
        return v


# ===========================================================================
# 清洗逻辑
# ===========================================================================

def _clean_value(value: str, fallback: str = "") -> str:
    """Return cleaned value, or fallback if value is empty/None."""
    if not value or not isinstance(value, str):
        return fallback
    return value.strip()


def _clean_string_inplace(value: str, normalizer, field: str) -> str:
    """Clean a single string value, preserving empty/Not Provided as-is."""
    if not value or not isinstance(value, str):
        return value
    if value in ("Not Provided", ""):
        return value
    return normalizer.normalize(value, field)


def _clean_formula_inplace(value: str, formula_norm) -> str:
    """Clean a formula string."""
    if not value or not isinstance(value, str):
        return value
    if value == "Not Provided":
        return value
    return formula_norm.normalize(value)


def _clean_terminology_inplace(value: str, term_norm, field: str) -> str:
    """Clean a terminology string."""
    if not value or not isinstance(value, str):
        return value
    if value == "Not Provided":
        return value
    return term_norm.normalize(value, field)


def clean_chain_in_place(chain: Dict[str, Any], unit_norm, formula_norm, term_norm) -> None:
    """Clean fields of a single chain dict in-place, preserving original structure."""

    # ---- material_info ----
    material_info = chain.get("material_info", {})
    if material_info:
        if "material_formula" in material_info:
            material_info["material_formula"] = _clean_formula_inplace(
                material_info["material_formula"], formula_norm
            )

    # ---- electrochemical_context ----
    ec = chain.get("electrochemical_context", {})
    if ec:
        # cell_type, electrode_role
        for field in ("cell_type", "electrode_role"):
            if field in ec:
                ec[field] = _clean_terminology_inplace(ec[field], term_norm, field)

        # electrode_composition.Mass_Loading
        comp = ec.get("electrode_composition", {})
        if comp and "Mass_Loading" in comp:
            ml = comp["Mass_Loading"]
            if ml and isinstance(ml, str) and ml != "Not Provided":
                comp["Mass_Loading"] = unit_norm.normalize(ml, "Mass_Loading")

    # ---- property ----
    prop = chain.get("property", {})
    if prop:
        if "unit" in prop:
            prop["unit"] = _clean_string_inplace(prop["unit"], unit_norm, "unit")

    # ---- characterization -> test_conditions ----
    char = chain.get("characterization", {})
    tc = char.get("test_conditions", {}) if char else {}
    if tc:
        # Temperature
        if "Temperature" in tc:
            raw = tc["Temperature"]
            if raw and isinstance(raw, str) and raw != "Not Provided":
                cleaned = unit_norm.normalize(raw, "Temperature")
                if cleaned == raw and "room" in raw.lower():
                    cleaned = "25 °C"
                tc["Temperature"] = cleaned

        # Voltage_Window
        if "Voltage_Window" in tc:
            raw = tc["Voltage_Window"]
            if raw and isinstance(raw, str) and raw != "Not Provided":
                voltage = split_voltage_window(raw)
                if voltage.get("voltage_min") is not None:
                    vmin = voltage["voltage_min"]
                    vmax = voltage.get("voltage_max")
                    if vmax is not None:
                        tc["Voltage_Window"] = f"{vmin}-{vmax} V"
                    else:
                        tc["Voltage_Window"] = f"{vmin} V"

        # Current_Density, C_Rate
        for field in ("Current_Density", "C_Rate"):
            if field in tc:
                tc[field] = _clean_string_inplace(tc[field], unit_norm, field)

        # Cycles — extract numeric
        if "Cycles" in tc:
            raw = tc["Cycles"]
            if raw and isinstance(raw, str) and raw != "Not Provided":
                m = re.search(r"[\d.]+", raw)
                tc["Cycles"] = m.group() if m else raw

        # Electrolyte.Solvent
        electrolyte = tc.get("Electrolyte", {})
        if electrolyte and "Solvent" in electrolyte:
            electrolyte["Solvent"] = _clean_terminology_inplace(
                electrolyte["Solvent"], term_norm, "Solvent"
            )

        # Electrode_Info.Mass_Loading
        ec_info = tc.get("Electrode_Info", {})
        if ec_info and "Mass_Loading" in ec_info:
            ml = ec_info["Mass_Loading"]
            if ml and isinstance(ml, str) and ml != "Not Provided":
                ec_info["Mass_Loading"] = unit_norm.normalize(ml, "Mass_Loading")

    # ---- microstructure ----
    micro = chain.get("microstructure", {})
    if micro:
        if "morphology" in micro:
            micro["morphology"] = _clean_terminology_inplace(
                micro["morphology"], term_norm, "morphology"
            )
        if "particle_size_or_dimension" in micro:
            micro["particle_size_or_dimension"] = _clean_string_inplace(
                micro["particle_size_or_dimension"], unit_norm, "particle_size"
            )

    # ---- synthesis_method ----
    syn = chain.get("synthesis_method", {})
    if syn:
        # synthesis_techniques (list of strings)
        techniques = syn.get("synthesis_techniques", [])
        if isinstance(techniques, list):
            syn["synthesis_techniques"] = [
                term_norm.normalize(t, "synthesis_techniques") if isinstance(t, str) else t
                for t in techniques
            ]

        # detailed_steps
        steps = syn.get("detailed_steps", [])
        if isinstance(steps, list):
            for step in steps:
                precursors = step.get("Precursors", [])
                if isinstance(precursors, list):
                    step["Precursors"] = [
                        _clean_formula_inplace(p, formula_norm) if isinstance(p, str) else p
                        for p in precursors
                    ]

                if "Reaction_Temperature" in step:
                    step["Reaction_Temperature"] = _clean_string_inplace(
                        step["Reaction_Temperature"], unit_norm, "Reaction_Temperature"
                    )

                if "Reaction_Time" in step:
                    step["Reaction_Time"] = _clean_string_inplace(
                        step["Reaction_Time"], unit_norm, "Reaction_Time"
                    )

                if "Atmosphere" in step:
                    step["Atmosphere"] = _clean_terminology_inplace(
                        step["Atmosphere"], term_norm, "Atmosphere"
                    )

                if "Solvent" in step:
                    step["Solvent"] = _clean_terminology_inplace(
                        step["Solvent"], term_norm, "Solvent"
                    )


def process_json_file(filepath: str, unit_norm, formula_norm, term_norm) -> Optional[Dict[str, Any]]:
    """Process a single JSON file: clean chains in-place, return the cleaned dict."""
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Deep copy so original is untouched
    cleaned = copy.deepcopy(data)

    chains = cleaned.get("final_property_chains", [])
    if not isinstance(chains, list):
        print(f"  Warning: no 'final_property_chains' list in {Path(filepath).name}")
        return cleaned

    for chain in chains:
        if isinstance(chain, dict):
            try:
                clean_chain_in_place(chain, unit_norm, formula_norm, term_norm)
            except Exception as e:
                print(f"  Error cleaning chain {chain.get('chain_id', '?')}: {e}")

    return cleaned


def main():
    input_path = Path(INPUT_DIR)
    json_files = sorted(input_path.glob("*_final.json"))

    if not json_files:
        print(f"No JSON files found in {INPUT_DIR}")
        return

    print(f"Found {len(json_files)} JSON file(s) to process:")
    for f in json_files:
        print(f"  - {f.name}")
    print()

    # Create output directory
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Initialize normalizers（全部为自主编写的清洗工具）
    unit_norm = UnitNormalizer()
    formula_norm = FormulaNormalizer()
    term_norm = TerminologyNormalizer()

    success = 0
    fail = 0

    for json_file in json_files:
        out_name = json_file.stem + "_cleaned" + json_file.suffix
        out_path = OUTPUT_DIR / out_name

        print(f"Processing: {json_file.name} -> {out_name}")

        try:
            cleaned = process_json_file(json_file, unit_norm, formula_norm, term_norm)
            if cleaned is None:
                print(f"  Skipped (empty)")
                continue

            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(cleaned, f, ensure_ascii=False, indent=2)

            chains = cleaned.get("final_property_chains", [])
            print(f"  Done — {len(chains)} chain(s) cleaned")
            success += 1

        except Exception as e:
            print(f"  Error: {e}")
            fail += 1

    print(f"\nFinished: {success} succeeded, {fail} failed")
    print(f"Output directory: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
