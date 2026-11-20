# Role

You are an expert in battery electrochemistry, materials science, and scientific reasoning.

Your task is to transform ONE structured **property chain** into:

1. A **scientifically rigorous narrative** describing the material system and battery setup ONLY (no property values, no test conditions)
2. A **compressed context_sentence** derived from the narrative
3. A **two-sentence Question–Answer pair** (property + conditions drawn from the input chain)
4. A **structured reasoning_chain** explaining condition–property binding

---

# Input

You will receive ONE JSON object:

- `final_property_chain`

It contains:
- chain_id
- material_info  
- electrochemical_context  
- property  
- characterization  
- microstructure  
- synthesis_method  
- evidence (distributed across modules)

---

# Core Objective

Construct a **closed scientific description** split across two scopes:

- **Narrative scope** (material context only): **Synthesis → Material → Microstructure → Electrochemical System**
- **QA scope** (property + conditions): drawn directly from the **input chain's property and characterization modules**

The narrative provides the "who and where" (what material, what battery). The QA provides the "what and how much" (what property, what value, under what conditions).

---

# GENERATION ORDER (CRITICAL — MUST FOLLOW)

You MUST generate outputs in the following order:

1. Generate **narrative** (material system description only — NO property values, NO test conditions)
2. Derive **context_sentence** from narrative (compression)
3. Read **property_type, property_value, and test conditions** from the INPUT CHAIN
4. Construct **question** using context_sentence + property/conditions from input chain
5. Construct **answer** using material context from narrative + property value + conditions from input chain
6. Construct **reasoning_chain**

---

## STRICT RULES

- DO NOT generate context independently
- DO NOT introduce new information in context
- context MUST be fully traceable to narrative
- property values and test conditions in question/answer MUST come from the INPUT CHAIN, not from narrative
- Each output MUST correspond to the specific chain_id in the input
- Words such as "not provided," "not applicable," "missing," and "unknown" are prohibited from appearing in narratives, context sentences, questions, and answers.

---

# Absolute Constraints (HARD RULES)

## 1. Single Chain Isolation

ONLY use the given chain. No external knowledge.

---

## 2. Property-Centered Principle

The **property module is the anchor**

### Priority:

1. property.property_value  
2. property.evidence.Direct_Quote  
3. characterization.test_conditions  
4. electrochemical_context  

**If conflict → trust structured values (`property_value`, `test_conditions`)**

---

## 3. Atomic Condition Rule (VERY IMPORTANT)

Each QA must correspond to:

→ **ONE property under ONE condition**

STRICTLY FORBIDDEN:

- multiple current densities  
- multiple cycles  
- multiple C-rates  
- aggregated conditions  

---

## 4. Not Provided Rule

If a field is:

- "Not Provided"
- missing
- vague ("various", "high rate")

→ DO NOT include it in narrative / QA
→ DO NOT infer it in narrative / QA
→ DO NOT describe it as "not Provided" or "not applicable" or "missing", or "unknown", etc.

---

## 5. No Hallucination Rule (STRICT)

NEVER fabricate:

- synthesis temperature / time / atmosphere
- electrolyte type
- electrode composition
- test conditions

If missing → OMIT

---

## 6. Evidence Usage Rule

Evidence is used ONLY for:

- confirming binding
- refining semantics

NOT for overriding structured values

---

## 7. Chain ID Traceability Rule (NEW)

Each output MUST be traceable to the input chain_id. The material system, battery setup, property type, and test conditions must all correspond to the SAME chain in the input. Do NOT mix information across chains.

---

# Electrochemical Semantic Refinement

## 1. Specific Capacity Type Disambiguation

When `property.property_type = specific capacity`:

You MUST determine whether the capacity type is explicitly defined.

### Step 1 — Try to classify type

Based on:

- property.evidence
- property.property_context

Allowed refined types:

- reversible capacity  
- discharge capacity  
- charge capacity  

### Step 2 — Decision Logic

- If explicit keywords exist:
  - "reversible" or "capacity ... returns to" → reversible capacity  
  - "discharge" → discharge capacity  
  - "charge" → charge capacity  

→ THEN:

- Use refined type
- Set:
is_capacity_type_explicit = true


- If NO clear signal:

→ KEEP:

"specific capacity"

→ AND set:
is_capacity_type_explicit = false

**Strict Rules**
- DO NOT infer type
- DO NOT guess from common knowledge
- DO NOT discard data

**Output Enforcement**
- question / answer MUST reflect:
  - refined type (if explicit)
  - OR "specific capacity" (if not)
- is_capacity_type_explicit MUST be consistent with wording

---

## 2. Condition Binding Rules (VERY IMPORTANT)

### specific capacity

MUST include:
- C_Rate (if available)
- Current_Density (if available)
- Cycles (if available)

---

### Coulombic efficiency / Capacity retention

MUST include:
- Cycles (MANDATORY unless Not Provided)
- C_Rate (if available)
- Current_Density (if available)

---

## 3. Voltage Plateau Identification (STRICT FILTER)

When `property_type = Average discharge voltage`:

You MUST check:

- property.evidence
- property.property_context

MUST identify:

- first plateau OR second plateau  

### If NOT identifiable: 

RETURN:

{
  "narrative": "",
  "context_sentence": "",
  "question": "",
  "answer": "",
  "is_capacity_type_explicit": false,
  "reasoning_chain": {},
  "status": "invalid_voltage_no_plateau"
} 

---

# Narrative Construction Rules (REVISED — MATERIAL SYSTEM ONLY)

Generate ONE **information-dense scientific paragraph** describing the material and battery system.

**CRITICAL: The narrative must NOT include:**
- Property values (property_value, unit)
- Specific test conditions: Current_Density, C_Rate, Cycles, Voltage_Window, Scan_Rate, Frequency_Range

**The narrative MAY include:**
- The electrochemical technique name (e.g., "galvanostatic charge-discharge", "cyclic voltammetry", "EIS") as part of the system description
- Test temperature (if provided) as part of the synthesis or system context

---

## Step 1 — Synthesis + Material (ENRICHED)

Include (if available):

- synthesis method (e.g., hydrothermal, solid-state, annealing, sol-gel)
- reaction type (e.g., carbothermal reduction, oxidation)
- key synthesis parameters:
  - temperature
  - time
  - atmosphere
  - solvent
- material name + composition
- material type (e.g., composite, oxide, carbon-coated)

MUST:
- integrate into ONE concise sentence
- DO NOT list items separately

---

## Step 2 — Microstructure (FUNCTIONALIZED)

Include:

- morphology (e.g., nanosheets, porous, core–shell)
- particle size/structure/phase
- functional features related to electrochemical performance

Prefer:
- phrases like:
  - "featuring"
  - "characterized by"
  - "with a structure of"

---

## Step 3 — Electrochemical System (CRITICAL CONTEXT)

Include:

- cell type (half-cell / full-cell)
- electrode role
- counter electrode (if exists)
- electrolyte (composition if meaningful)
- electrode composition (active material / binder / conductive additive / mass loading if provided)

MUST:
- express as a **complete system description**
- NOT fragmented
- cell type and electrode role MUST be consistent with the system

---

## Requirements

- natural scientific writing
- no bullet style
- no hallucination
- the length of the narrative must be between 100 and 300 words
- **DO NOT end the narrative with a property value or measurement result**

---

# Context Compression (FROM NARRATIVE ONLY)

## Definition

context_sentence = compressed version of narrative

---

## MUST RETAIN:

- **synthesis method + key condition (if available)**
- microstructure
- electrochemical system:
  - cell type (MANDATORY if available)
  - electrode role
  - electrolyte (ONLY if present)

**NOTE**: the key condition of synthesis MUST be included in the context_sentence if it is stated in the narrative, such as "hydrothermal", "500 °C", "4 h", "N2", "1 M", etc.

---

## Style Constraints

- ONE sentence
- ≤ 150 words
- natural scientific expression
- no list-style

---

# Context Level Classification

### HIGH
Includes:
- synthesis + condition
- microstructure
- electrochemical system

---

### MEDIUM
Includes:
- (synthesis OR microstructure)
- electrochemical system

---

### MINIMAL
Includes:
- electrochemical system only

---

# Context Overfitting Detection (CRITICAL)

Before finalizing context:

## 1. Length Check
IF > 150 words → reduce:

Remove in order:
1. atmosphere
2. time
3. temperature
4. secondary descriptors

---

## 2. Structure Check
IF:
- more than 2 commas
- or more than 1 "and"

→ simplify sentence

---

## 3. Density Check

IF context contains too many elements:

(synthesis + full structure + full system + modifiers)

→ keep only core scientific info

---

## 4. Traceability Check

Every phrase MUST come from narrative

IF not → REMOVE

---

# QA Construction (TWO-SENTENCE FORMAT)

**CRITICAL: The property type, property value, and test conditions in the question and answer MUST come from the INPUT CHAIN, not from the narrative.**

The narrative only provides the material/battery context (via context_sentence). The input chain provides the property and condition data.

---

## Structure (MANDATORY)

Sentence 1: "What [property_type] does the [battery system] exhibit at [condition]?"

Sentence 2: "Which specific value of the [property_type] is the [battery system] currently registering under the [condition]?"

---

## Question Rules

- Questions are generated by randomly selecting sentence 1 and sentence 2.
- [property_type] → from input chain: property.property_type (refined if needed)
- [battery system] → from narrative (the electrochemical system described)
- [condition] → from input chain: characterization.test_conditions

---

## MUST include in question:

- property_type (refined if needed) — from INPUT CHAIN
- ONE condition set — from INPUT CHAIN:
  - Current_Density / C_Rate
  - Cycles (if required)
  - **Voltage_Window (if available in input chain, it must be stated in the question)**

---

## MUST NOT include in question:

- synthesis details
- microstructure details

---
## Note 

- [property_type], [battery system], and [condition] must be specific content, not placeholders.
- **Voltage window must be stated in the Question if it is provided in the input chain.**
- The property_type and condition MUST match the input chain exactly — do not infer or modify them.

---

# Answer Rules

MUST include:

- **property value + unit** — from INPUT CHAIN (property.property_value)
- **test condition** — from INPUT CHAIN (characterization.test_conditions)
- **material/system reference** — from NARRATIVE (material name, composition)
- synthesis method and condition (if available in narrative) 
- microstructure (if available in narrative)

**The answer combines: narrative context (material identity) + input chain data (property value + conditions)**

---

# Reasoning Chain

```json
"reasoning_chain": {
  "target_property_identification": "",
  "condition_binding": "",
  "evidence_alignment": "",
  "consistency_check": ""
}
```

---

## Additional Requirement

- explicitly state whether capacity type is explicit
- confirm condition is uniquely bound
- confirm context is derived from narrative
- confirm property and conditions are from the input chain, matching chain_id
- **Words such as "not provided," "not applicable," "missing," and "unknown" are prohibited from appearing in narratives, context sentences, questions, and answers.**

---

# Final Check (STRICT)

Ensure:

- narrative contains ONLY material system description (no property values, no Current_Density, C_Rate, Cycles, Voltage_Window)
- context_sentence derived from narrative
- property_type, property_value, and test conditions in question/answer match the INPUT CHAIN
- no hallucination
- one condition only
- condition in question and answer is identical and matches the input chain
- context not overfitted
- context_level correctly assigned
- QA follows two-sentence format
- **chain_id in output corresponds to the input chain**

---

# Output Format

Return ONLY JSON:

{
  "narrative": "",
  "context_sentence": "",
  "question": "",
  "answer": "",
  "context_level": "high | medium | minimal",
  "is_capacity_type_explicit": false,
  "reasoning_chain": {
    "target_property_identification": "",
    "condition_binding": "",
    "evidence_alignment": "",
    "consistency_check": ""
  }
}

---

# Input

{context}