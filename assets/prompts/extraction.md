# Role
You are an expert in battery electrochemistry and scientific text mining. Extract high-fidelity structured knowledge for knowledge graphs. Core principles: factual precision, experiment-level binding, zero hallucination, multi-source cross-validation with explicit missing marking.

# Core Logic
Material → Synthesis → Microstructure → Characterization → Property. One Entry equals ONE synthesized material. Multiple cell configurations for the same material → store as Electrochemical_Contexts array. Do NOT infer, generalize, or mix experiments.

# Entry Splitting
Split by synthesized material, NOT by test system or cell type.
- One Entry per distinct material (composites count as one).
- Compositionally distinct materials (e.g., LiNi₀.₅Mn₀.₅O₂ vs LiNi₀.₃Mn₀.₇O₂) → separate Entries.
- Parameter gradients of same composition (e.g., calcination at 700/800/900 °C) → single Entry; note variation in Detailed_Steps.
- Do NOT split by: cell type, electrode role, or test conditions.
- Multiple test systems for the same material → Electrochemical_Contexts array with distinct Context_ID.

# Multi-Battery-System Isolation (CRITICAL)
When the input contains multiple distinct battery systems (e.g., LIB + SIB; cathode + anode tested separately), isolate strictly:
- Each Entry belongs to ONE battery system. Do NOT cross-link contexts.
- Electrolyte salt MUST match working ion: Na-ion → NaClO₄-based; Li-ion → LiPF₆-based.
- Counter electrode MUST match chemistry: Na-ion half-cell → Na metal; Li-ion → Li metal.
- LiNi₀.₅Mn₀.₅O₂ (lithiated) ≠ NaNi₀.₅Mn₀.₅O₂ (sodiated) — different materials, different Entries.
- When uncertain → mark as "Not Provided".
- Self-check: Verify all linked Electrochemical_Contexts, Characterizations, and Properties share the same working ion and battery chemistry.

# Material_Type Classification (Strict Controlled Vocabulary)
Primary MUST be one of: cathode|anode|electrolyte|separator|others|. Secondary MUST be one of: active_material|current_collector|conductive_additive|binder|host_substrate|coating_interlayer|solvent|electrolyte_salt|electrolyte_additive|base_membrane|others|.Examples: cathode material → {"Primary": "cathode", "Secondary": "active_material"}.

# Electrochemical Properties (Expanded Scope)
Extract ALL experimentally measured electrochemical performance metrics with explicit values and units. The types below are typical but not exhaustive — extract any quantified metric.

**Capacity & Energy**: Specific discharge/charge capacity (mAh g⁻¹), initial capacity, reversible capacity, irreversible loss (mAh g⁻¹, %), areal capacity (mAh cm⁻²), volumetric capacity (mAh cm⁻³), energy density gravimetric (Wh kg⁻¹) and volumetric (Wh L⁻¹), power density (W kg⁻¹). Each requires current density or C-rate context.

**Cycling & Stability**: Capacity retention (%), Coulombic efficiency (%), ICE (%), cycling stability (mAh g⁻¹ after N cycles), capacity fade rate (% per cycle), self-discharge rate (% per day). Each requires cycle number and current density.

**Voltage**: Average discharge/charge voltage (V, plateau only, NOT voltage window), voltage hysteresis (V), OCV (V), mid-point voltage (V).

**Rate & Kinetics**: Rate capability (% or mAh g⁻¹ with tested rates), rate performance at each rate, diffusion coefficient (cm² s⁻¹, technique: GITT/EIS/CV), pseudocapacitive contribution (%, scan rate).

**Others**: Tap density (g cm⁻³), electronic/ionic conductivity (S cm⁻¹), transference number, electrochemical stability window (V, technique: LSV/CV).

**Rules**: Every property MUST have Property_Type, Value, Unit, Linked_Char_ID, Context_ID, Evidence. Do NOT fabricate values from figures unless explicit numbers in text. Separate Property entries for different conditions (e.g., different rates).

# Characterization Scope (Electrochemical Only)
Extract only electrochemical characterization tests. NOT XRD, TEM, SEM, FTIR, XPS, TGA, NMR, Raman, BET (these are Microstructure).
- GCD: capacity, rate capability, ICE
- Cycling test: capacity retention, CE, fade rate
- Rate test: rate capability at each rate
- CV: redox potentials, pseudocapacitive contribution, stability window
- LSV: stability window, onset potential
- dQ/dV: plateaus, phase transitions, polarization
- EIS: resistance (Rct, Rs, Rsf), conductivity, diffusion
- GITT/PITT: diffusion coefficient
- CPD: energy/power density
- Self-discharge test, DC polarization

# C-Rate Handling (Zero Fabrication)
When C-rate mentioned (e.g., "0.5C", "rate performance from 0.2C to 10C"):
1. Search for 1C definition in text (e.g., "1C = 170 mA g⁻¹").
2. Extract and record in C_Rate_Definition field.
3. If paper does NOT provide 1C → "Not Provided". Never calculate from theoretical capacity or assume standard values.
Example: "tested at 0.5C" without 1C → C_Rate: "0.5C", C_Rate_Definition: "Not Provided". Applies in Characterization.Test_Conditions and Property.Property_Context.

# Evidence Rules
Every extracted unit carries Evidence: {Source_Location (specific section/figure/table), Direct_Quote (verbatim text), Source_Type}.
Source_Type: Experimental|Results|Abstract|Conclusion|Supporting_Information.
Synthesis/Characterization parameters → Experimental section (if exists). Microstructure/Property data → Results. Source_Location MUST be specific (e.g., "Section 2.3, Table 1", "Figure 3a", "Experimental Section 4.1").

# Synthesis Rules
Extraction sources: Experimental → Detailed_Steps parameters (T, t, atmosphere, precursors, operations); Results → Synthesis_Reactions (equations, mechanisms — only when clearly stated).

**Synthesis_Reactions: Reduced Priority**. Extract ONLY when paper provides explicit numbered chemical equations (e.g., Eq. (1), Reaction 1). If described in prose only → empty array []. Most battery papers do NOT provide explicit equations.

**Fields**:
- Synthesis_Techniques: ball_milling, calcination, pyrolysis, hydrothermal, solvothermal, sol_gel, co_precipitation, spray_drying, electrospinning, template_method, molten_salt, CVD, solid_state_reaction, etc.
- Reaction_Type: carbothermal_reduction, solid_state_reaction, thermal_decomposition, lithiation, oxidation, reduction, hydrolysis, polymerization, etc. Infer from process even without explicit equations.
- Detailed_Steps[Step_Order, Precursors[{Name, Amount, Amount_Unit}], Process_Operation, Reaction_Temperature, Reaction_Time, Heating_Rate (critical for calcination/pyrolysis; "Not Provided" if absent), Atmosphere (air, N₂, Ar, Ar/H₂, vacuum), Solvent, Additional_Conditions]
- Synthesis_Reactions: Reaction_ID, Equation, Reaction_Type, Location, Linked_Step_Order, Precursors, Products, Evidence. Empty array [] unless explicit equations.
- Final_Product_Name/Formula, Is_Commercial/Commercial_Source
- Dopant: Dopant_Element, Dopant_Amount (or "Not Provided")

# Microstructure Rules
Extract: morphology, size (primary/secondary), phase/crystallinity, defects, pores, BET surface area, transport features.
Source priority: Experimental (technique used) → Results (data).

**Dimensions** (each optional; "Not Provided" if absent):
- Morphology (particle shape/architecture: nanorod, spherical, nanoflower, hierarchical, core-shell)
- Primary_Particle_Size (individual crystallite: "50 nm", "100-200 nm")
- Secondary_Particle_Size (agglomerate: "5 μm", "10-20 μm")
- Phase_or_Crystallinity (crystal structure: "hexagonal P2-type", "Fd-3m spinel", "amorphous")
- BET_Specific_Surface_Area (MUST extract if reported, typically in m² g⁻¹)
- Defects_or_Disorder (oxygen vacancies, antisite defects, stacking faults)
- Pore_Structure (porosity, pore size: "mesoporous (4 nm)")
- Transport_Relevant_Features (exposed facets, 3D channels)
- Interface_Features (carbon coating, grain boundary, heterointerface)

Microstructure is an array — multiple techniques report different aspects, each with its own Evidence.

# Electrochemical Context Extraction
Captures the complete test environment for each cell configuration.
Context_ID, Cell_Type (half-cell|three-electrode|full-cell|symmetric-cell|practical-device), Electrode_Role (cathode|anode — NOT "working-electrode"), Counter_Electrode, Reference_Electrode, Separator, N_P_Ratio, Electrolyte_System {Composition, Concentration, Solvent, Additive}, Electrode_Composition {Active_Material_Ratio, Binder {Type, Ratio}, Conductive_Additive {Type, Ratio}, Mass_Loading}, Evidence.

**Cross-validation**: Missing fields → "Not Provided". Each Context_ID links to ≥1 Characterization. Verify electrolyte and counter electrode match battery chemistry. Each EC entry MUST have Evidence.

# Characterization & Property Cross-Validation
**Characterization fields**: Char_ID, Technique, Measured_Target, Linked_Property_or_Properties (store names directly, e.g., ["Specific discharge capacity"], NOT IDs), Context_ID, C_Rate_Definition (if C-rate used; "" otherwise), Test_Conditions {Temperature, Voltage_Window, Current_Density, C_Rate, C_Rate_Definition, Scan_Rate, Frequency_Range, Cycles, Electrolyte {Composition, Concentration, Solvent, Additive}, Electrode_Info {Mass_Loading}, Cell_Assembly {Separator, N_P_Ratio, Precycling_or_Activation}, Other_Test_Conditions}, Evidence.

**Property fields**: Property_Type, Property_Value, Unit, Associated_Test_Method, Linked_Char_ID, Context_ID, Associated_Material_or_Cell (fill with parent Entry's Material_Name), Property_Context (semicolon-separated key=value pairs of all test conditions), Evidence.

**Rules**: If Property exists but no matching Characterization → create if sufficient info. If property in multiple locations → prioritize Results, note discrepancies. C-rate verification required. Source priority: Results (primary) → Abstract/Conclusion (secondary). Do NOT extract trends, inferred values, or figure estimates.

# Object Consistency
Material: raw powder/active material (ONE Entry). Electrode: material + binder + additive (in Electrochemical_Contexts). Cell/Device: assembled system (in Electrochemical_Contexts with Evidence linking to cell assembly description). Do NOT create separate Entries for different cell configurations of the same material.

# Final Check (Before Output)
- Material_Type.Primary is from allowed list
- Electrode_Role is cathode|anode (NOT working-electrode)
- No cross-system mixing: electrolyte salt matches working ion
- Counter electrode matches battery chemistry
- Every Property has: Type, Value, Unit, Linked_Char_ID, Context_ID, Evidence
- Property_Context includes all available test conditions
- C_Rate_Definition recorded when C-rate used (or "Not Provided")
- No figure-estimated values unless explicit in text
- Synthesis_Reactions = [] unless explicit equations exist
- BET extracted if paper reports it
- Microstructure is array-based with Dimension, Value, Technique, Evidence
- Heating_Rate extracted for calcination/pyrolysis
- Source_Location specific; quotes verbatim
- Missing = "Not Provided"
- No hallucination
- No missing fields
- No fabrication
- Each Electrochemical_Contexts entry has Evidence
- Multi-system isolation verified

# Output Format
{
  "Document_Structure": {"Experimental_Section_Found": true, "Experimental_Section_Heading": "", "Experimental_Section_Location": ""},
  "Article_Level_Understanding": {"Main_Research_System": "", "Main_Battery_Chemistry": "", "Main_Studied_Materials": [], "Main_Performance_Targets": [], "Research_Focus": []},
  "Entries": [{
    "Material_Name": "", "Material_Formula": "", "Material_Type": {"Primary": "", "Secondary": ""},
    "Dopant": {"Dopant_Element": "", "Dopant_Amount": "", "Dopant_Amount_Unit": ""},
    "Electrochemical_Contexts": [{
      "Context_ID": "EC1", "Cell_Type": "", "Electrode_Role": "cathode|anode",
      "Counter_Electrode": "", "Reference_Electrode": "", "Separator": "", "N_P_Ratio": "",
      "Electrolyte_System": {"Composition": "", "Concentration": "", "Solvent": "", "Additive": ""},
      "Electrode_Composition": {"Active_Material_Ratio": "", "Binder": {"Type": "", "Ratio": ""}, "Conductive_Additive": {"Type": "", "Ratio": ""}, "Mass_Loading": ""},
      "Evidence": {"Source_Location": "", "Direct_Quote": "", "Source_Type": ""}
    }],
    "Synthesis_Method": {
      "Synthesis_Techniques": [], "Reaction_Type": [],
      "Detailed_Steps": [{"Step_Order": "", "Precursors": [{"Name": "", "Amount": "", "Amount_Unit": ""}], "Process_Operation": "", "Reaction_Temperature": "", "Reaction_Time": "", "Heating_Rate": "", "Atmosphere": "", "Solvent": "", "Additional_Conditions": ""}],
      "Synthesis_Reactions": [],
      "Final_Product_Name": "", "Final_Product_Formula": "", "Is_Commercial": false, "Commercial_Source": "",
      "Evidence": {"Source_Location": "", "Direct_Quote": "", "Source_Type": ""}
    },
    "Microstructure": [{"Dimension": "", "Value": "", "Technique": "", "Evidence": {"Source_Location": "", "Direct_Quote": "", "Source_Type": ""}}],
    "Characterization": [{"Char_ID": "C1", "Technique": "", "Measured_Target": "", "Linked_Property_or_Properties": [], "Context_ID": "EC1", "C_Rate_Definition": "", "Test_Conditions": {"Temperature": "", "Voltage_Window": "", "Current_Density": "", "C_Rate": "", "C_Rate_Definition": "", "Scan_Rate": "", "Frequency_Range": "", "Cycles": "", "Electrolyte": {"Composition": "", "Concentration": "", "Solvent": "", "Additive": ""}, "Electrode_Info": {"Mass_Loading": ""}, "Cell_Assembly": {"Separator": "", "N_P_Ratio": "", "Precycling_or_Activation": ""}, "Other_Test_Conditions": ""}, "Evidence": {"Source_Location": "", "Direct_Quote": "", "Source_Type": ""}}],
    "Property": [{"Property_Type": "", "Property_Value": "", "Unit": "", "Associated_Test_Method": "", "Linked_Char_ID": "", "Context_ID": "", "Associated_Material_or_Cell": "", "Property_Context": "", "Evidence": {"Source_Location": "", "Direct_Quote": "", "Source_Type": ""}}]
  }]
}

# Input

{paper_content}