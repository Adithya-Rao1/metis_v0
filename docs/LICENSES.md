# Licenses (status: cleared / pending / excluded)
| Item | License | Status | Notes |
| --- | --- | --- | --- |
| RDKit | BSD-3 | cleared | per owner/memory; verify |
| 3Dmol.js | BSD-3 / MIT per owner | cleared | |
| TDC datasets (hERG, hERG_Karim, AMES, LD50_Zhu, DILI, Solubility_AqSolDB, Caco2_Wang, PAMPA_NCATS, Half_Life_Obach, Clearance_Microsome_AZ, Clearance_Hepatocyte_AZ, Lipophilicity_AstraZeneca) | "Not Specified" / CC BY 4.0 link per TDC pages; AZ sets are ChEMBL deposits (CC BY-SA 3.0, verify); AqSolDB upstream to verify | pending (G1) | machine-readable source: ml/data/datasets.yaml; training is code-gated on `cleared` + date_checked + confirmed_by; Lipophilicity is logD 7.4 |
| ADMET-AI weights | MIT code; weights pending | pending (G1) | only if wanted |
| FreeSolv | CC BY-NC-SA | excluded | non-commercial |
| Hackathon studio code (accounts, molio, geometry) | own work, no license | cleared | reuse |
| Vina / GNINA / fpocket / P2Rank | check before bundling | pending | |
| Biogen kinetic solubility | unchecked, not in TDC | pending | source not yet identified |
| PyTDC (only if --download used) | check before use | pending | lazy optional dependency, not in requirements |
| scikit-learn, NumPy, SciPy, PyYAML, pytest | BSD-3 / BSD / MIT per upstream | pending verification | confirm against pinned versions |
