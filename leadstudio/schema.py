"""Wire schema for Metis Lead Studio.

Single source of truth for every JSON shape that crosses a process boundary
(HTTP request/response bodies and SSE event payloads). Agents code against these
models; do not change a field without updating docs/event_contract.md and telling
the integrator.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Public-service size limits (Metis hosting): enforced by the models themselves so every route inherits them.
MAX_SMILES = 2000
MAX_MOLFILE = 150_000
MAX_ID = 64

SCHEMA_VERSION = "0.1"

EditCategory = Literal["atom", "bond", "bioisostere", "functional_group", "crossover", "reaction"]
ChosenBy = Literal["policy", "random", "crossover", "reaction", "user"]
EventType = Literal["run_start", "generation", "candidate", "policy_metrics", "run_end", "error"]
Stage = Literal["binding", "pharmacology", "manual"]  # pipeline stage a run belongs to


# ---------------------------------------------------------------- descriptors
class Descriptors(BaseModel):
    qed: float
    sa: float | None = None
    logp: float
    tpsa: float
    mw: float
    hbd: int
    hba: int
    rotb: int
    fsp3: float
    heavy_atoms: int
    lipinski_violations: int
    alerts: int


class AlertHit(BaseModel):
    name: str
    family: str  # PAINS | BRENK | NIH | ...
    atom_indices: list[int]


class ScoreResult(BaseModel):
    descriptors: Descriptors
    components: dict[str, float]  # per-descriptor desirability in [0, 1]
    composite: float  # weighted aggregate in [0, 1]
    alerts: list[AlertHit] = Field(default_factory=list)
    extras: dict[str, float] = Field(default_factory=dict)  # model outputs, e.g. {"plapt_pkd": 7.1}


# ------------------------------------------------------------------ objectives
class ObjectiveSpec(BaseModel):
    """One user-controllable objective. Desirability is 1 inside [low, high]."""

    model_config = ConfigDict(extra="forbid")
    weight: float = Field(default=1.0, ge=0.0, le=10.0, allow_inf_nan=False)
    low: float | None = Field(default=None, ge=-1e6, le=1e6, allow_inf_nan=False)
    high: float | None = Field(default=None, ge=-1e6, le=1e6, allow_inf_nan=False)
    direction: Literal["maximize", "minimize", "range"] = "range"


class RunConfig(BaseModel):
    seed_smiles: str = Field(max_length=MAX_SMILES)
    objectives: dict[str, ObjectiveSpec] = Field(
        default_factory=dict, max_length=20
    )  # keys = Descriptors fields
    population_size: int = Field(default=20, ge=1, le=10_000)  # the server clamps to its own, lower caps
    generations: int = Field(default=10, ge=1, le=10_000)
    rng_seed: int = Field(default=0, ge=0, le=2**31 - 1)
    policy_guidance: float = Field(
        default=0.5, ge=0.0, le=1.0, allow_inf_nan=False
    )  # share of mutations chosen by the policy
    use_crossover: bool = True
    use_reactions: bool = False  # SynGA-style reaction operator (stretch)
    locked_atoms: list[int] = Field(
        default_factory=list, max_length=120
    )  # seed atom indices that must not change
    preset_id: str | None = Field(default=None, max_length=MAX_ID)
    stage: Stage = "pharmacology"
    target_id: str | None = Field(
        default=None, max_length=MAX_ID
    )  # key into leadstudio/data/targets (binding stage needs it)
    min_similarity_to_seed: float = Field(
        default=0.4, ge=0.0, le=1.0, allow_inf_nan=False
    )  # Tanimoto guard (stage 1)
    max_mw: float | None = Field(default=None, ge=0.0, le=1e5, allow_inf_nan=False)
    profile_id: str | None = Field(
        default=None, max_length=MAX_ID
    )  # id of a saved scoring profile (/lead/profiles)
    adapter_id: str | None = Field(
        default=None, max_length=MAX_ID
    )  # per-user policy adapter: NOT available in Metis
    seed_molblock: str | None = Field(
        default=None, max_length=MAX_MOLFILE
    )  # accepted but ignored by the engine


# ------------------------------------------------------------------- explaining
class AtomContributions(BaseModel):
    """Per-atom decompositions of the CHILD molecule, index-aligned with its RDKit atoms."""

    crippen_logp: list[float]
    tpsa: list[float]
    gasteiger: list[float]


class Explanation(BaseModel):
    descriptor_deltas: dict[str, float]  # child - parent
    alerts_added: list[AlertHit] = Field(default_factory=list)
    alerts_removed: list[AlertHit] = Field(default_factory=list)  # indices refer to the PARENT
    atom_contributions: AtomContributions | None = None
    rationale: str
    tags: list[str] = Field(default_factory=list)  # e.g. ["lowers_logp", "removes_alert"]


class EditRecord(BaseModel):
    edit_id: str  # registry id, or "brics_crossover" / "reaction:<id>"
    name: str
    category: EditCategory
    site: list[int] = Field(default_factory=list)  # parent atom indices the edit touched
    chosen_by: ChosenBy
    parents: list[str] = Field(default_factory=list)  # parent SMILES (2 for crossover)


# ---------------------------------------------------------------- event payloads
class Candidate(BaseModel):
    id: str
    generation: int
    smiles: str
    parent_smiles: str | None = None
    molblock: str  # 3D mol block of the child, aligned to the parent when possible
    parent_molblock: str | None = None
    atom_map: list[tuple[int, int]] = Field(default_factory=list)  # (parent_idx, child_idx)
    edit: EditRecord | None = None
    score: ScoreResult
    explanation: Explanation | None = None
    accepted: bool = False  # survived selection into the next generation


class PopulationMember(BaseModel):
    smiles: str
    composite: float
    candidate_id: str | None = None


class GenerationPayload(BaseModel):
    generation: int
    best: PopulationMember
    mean_composite: float
    population: list[PopulationMember]
    map_xy: list[tuple[float, float]] | None = None  # 2D chemical-space map, index-aligned with population
    map_method: Literal["morgan_pca", "chemberta_pca"] | None = None


class PolicyMetrics(BaseModel):
    generation: int
    policy_choice_rate: float
    mean_reward_policy_edits: float | None = None
    mean_reward_random_edits: float | None = None
    loss: float | None = None
    note: str | None = None


class StoryCard(BaseModel):
    seed_smiles: str
    final_smiles: str
    seed_score: ScoreResult
    final_score: ScoreResult
    edit_path: list[str]  # candidate ids from seed to final
    headline: str  # e.g. "Removed 2 alerts, QED 0.41 -> 0.72 in 7 edits"
    descriptor_table: dict[str, tuple[float, float]]  # name -> (seed, final)


class RunStartPayload(BaseModel):
    config: RunConfig
    seed_molblock: str
    seed_score: ScoreResult
    source: Literal["live", "replay", "mock"] = "live"
    schema_version: str = SCHEMA_VERSION


class ErrorPayload(BaseModel):
    message: str
    recoverable: bool = False


PAYLOAD_MODELS = {
    "run_start": RunStartPayload,
    "generation": GenerationPayload,
    "candidate": Candidate,
    "policy_metrics": PolicyMetrics,
    "run_end": StoryCard,
    "error": ErrorPayload,
}


class Event(BaseModel):
    type: EventType
    run_id: str
    seq: int
    payload: dict

    def typed_payload(self) -> BaseModel:
        return PAYLOAD_MODELS[self.type].model_validate(self.payload)


# ------------------------------------------------------------------- HTTP bodies
class ValidateRequest(BaseModel):
    smiles: str | None = Field(default=None, max_length=MAX_SMILES)
    molfile: str | None = Field(default=None, max_length=MAX_MOLFILE)  # MOL / SDF text


class ValidateResponse(BaseModel):
    ok: bool
    canonical_smiles: str | None = None
    heavy_atoms: int | None = None
    molblock: str | None = None
    score: ScoreResult | None = None
    error: str | None = None
    source: Literal["live", "mock"] = (
        "live"  # "mock": the service runs in scripted-replay mode (no real scoring)
    )


class RunCreated(BaseModel):
    run_id: str
    source: Literal["live", "mock"] = "live"  # what will produce this run's events; always present


# ------------------------------------------------------------ targets and poses
class TargetDrug(BaseModel):
    name: str
    smiles: str
    chembl_id: str | None = None
    source: str = ""  # where the SMILES came from


class TargetInfo(BaseModel):
    id: str
    name: str
    uniprot: str | None = None
    pdb_id: str
    chain: str = "A"
    sequence: str
    drugs: list[TargetDrug]
    structure_url: str  # path under /api/data/ serving the cleaned protein PDB
    pocket_residues: list[int] = Field(default_factory=list)
    note: str = ""


class PoseRequest(BaseModel):
    target_id: str = Field(max_length=MAX_ID)
    smiles: str = Field(max_length=MAX_SMILES)


class PoseResult(BaseModel):
    job_id: str
    status: Literal["queued", "running", "done", "failed"]
    target_id: str
    smiles: str
    progress: float = 0.0
    ligand_sdf: str | None = None  # best pose
    protein_pdb: str | None = None  # protein as predicted with that pose
    apo_pdb: str | None = None  # input structure, for the apo-vs-predicted overlay
    pose_confidence: float | None = None  # DynamicBind lDDT-style pose score
    affinity: float | None = None  # DynamicBind predicted affinity (independent of PLAPT)
    source: Literal["dynamicbind", "precomputed", "fallback"] = "dynamicbind"
    note: str = ""


# ------------------------------------------------------------------ manual mode
class ManualPreviewRequest(BaseModel):
    smiles: str = Field(max_length=MAX_SMILES)
    edit_id: str = Field(max_length=80)
    site: list[int] = Field(max_length=64)
    target_id: str | None = Field(default=None, max_length=MAX_ID)


class ManualPreview(BaseModel):
    ok: bool
    child_smiles: str | None = None
    molblock: str | None = None
    atom_map: list[tuple[int, int]] = Field(default_factory=list)
    score: ScoreResult | None = None
    explanation: Explanation | None = None
    sane: bool = True
    reason: str | None = None


class ManualStep(BaseModel):
    parent_smiles: str = Field(max_length=MAX_SMILES)
    child_smiles: str = Field(max_length=MAX_SMILES)
    edit_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_:.-]+$")
    site: list[int] = Field(max_length=64)
    reward_delta: float | None = Field(default=None, ge=-1e6, le=1e6, allow_inf_nan=False)
    t: float = Field(default=0.0, ge=0.0, le=1e9, allow_inf_nan=False)  # seconds since session start


class ManualSession(BaseModel):
    session_id: str
    seed_smiles: str
    target_id: str | None = None
    steps: list[ManualStep] = Field(default_factory=list)
