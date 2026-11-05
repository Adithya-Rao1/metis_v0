# Licenses (status: cleared / pending / excluded)
| Item | License | Status | Notes |
| --- | --- | --- | --- |
| RDKit | BSD-3 | cleared | per owner/memory; verify |
| 3Dmol.js | BSD-3 / MIT per owner | cleared | |
| TDC datasets (hERG, hERG_Karim, AMES, LD50_Zhu, DILI, Solubility_AqSolDB, Caco2_Wang, PAMPA_NCATS, Half_Life_Obach, Clearance_Microsome_AZ, Clearance_Hepatocyte_AZ, Lipophilicity_AstraZeneca) | "Not Specified" / CC BY 4.0 link per TDC pages; AZ sets are ChEMBL deposits (CC BY-SA 3.0, verify); AqSolDB upstream to verify | pending (G1) | machine-readable source: ml/data/datasets.yaml; training is code-gated on `cleared` + date_checked + confirmed_by; Lipophilicity is logD 7.4 |
| ADMET-AI weights | MIT code; weights pending | pending (G1) | only if wanted |
| FreeSolv (MobleyLab) | CC-BY-4.0 (owner-verified 2026-11-05; earlier NC-SA note was unverified and is withdrawn) | cleared | attribute Mobley and Guthrie in model cards |
| Hackathon studio code (accounts, molio, geometry) | own work, no license | cleared | reuse |
| Vina / GNINA / fpocket / P2Rank | check before bundling | pending | |
| Biogen kinetic solubility | unchecked, not in TDC | pending | source not yet identified |
| PyTDC (only if --download used) | check before use | pending | lazy optional dependency, not in requirements |
| scikit-learn, NumPy, SciPy, PyYAML, pytest | BSD-3 / BSD / MIT per upstream | pending verification | confirm against pinned versions |
| Ketcher (ketcher-core/react/standalone 3.18.0) | Apache-2.0 per npm registry | vendored; NOTICE lists LGPL-2.1, to be reviewed | owner chose Ketcher 2026-11-04; read package LICENSE/NOTICE and bundled third-party notices before vendoring |
| deap 1.4.4 (Lead Studio GA) | **LGPL-3.0** (pip metadata says "LGPL"; its dependency moocore is LGPL-2.1-or-later) | cleared with a flag | Used unmodified as an installed library (dynamic import), never vendored or patched, so LGPL obligations are limited to keeping it replaceable and preserving the notice. Review again before shipping any frozen or single-file bundle. |
| SA score data and code (`leadstudio/sa`, from RDKit Contrib / Ertl and Schuffenhauer) | BSD-3-Clause | cleared | fragment-score file is sha256-pinned on load |
| GraphDQN code copied into `leadstudio/` | MIT (owner's own repository) | cleared | copied, not linked; `binding_module` (PLAPT) is NOT shipped |
| torch (optional, Lead Studio DQN policy) | BSD-3 | not installed | not in requirements; without it the policy is uniform |
| PLAPT (`binding_module`, Bindwell, MIT per its LICENSE.txt) with ProtBert (Rostlab) and ChemBERTa (seyonec) weights | code MIT; weight licences not yet checked | pending | used only on the GPU box (imported from the GraphDQN checkout), NOT shipped in this repo; check the Hugging Face model cards before any public launch |
| DynamicBind (code and v2 checkpoint) | check upstream licence | pending | runs on the GPU box only; not in this repo |
| ESM-2 embeddings (used by DynamicBind) | check upstream licence | pending | GPU box only |
| IBM Plex Sans / Mono fonts (web/fonts) | SIL OFL 1.1 per upstream | pending verification | confirm the OFL text ships with the fonts |
| pdb2pqr / propka / Open Babel / Meeko (dockprep) | BSD-3 / LGPL-2.1 / GPL-2.0 / LGPL-2.1 per upstream | pending | GPL/LGPL tools: used as separate programs or libraries, not modified; owner/lawyer to confirm before launch |
| axe-core 4.10.2 (tests/web/vendor/axe/) | MPL-2.0 | cleared, TEST-ONLY | Vendored unmodified (`axe.min.js`, sha256 in the folder README, npm integrity verified) with its LICENSE and third-party notices; MPL-2.0 is file-level copyleft and we do not modify the file, so vendoring is permitted. Injected by Playwright tests only; never served from `web/` and never shipped. |
