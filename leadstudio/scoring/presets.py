"""Named objective bundles. Keys of each bundle are schema.Descriptors fields."""

from __future__ import annotations

from leadstudio.schema import ObjectiveSpec

Spec = ObjectiveSpec

PRESETS: dict[str, dict[str, ObjectiveSpec]] = {
    "remove toxic alerts": {
        "alerts": Spec(weight=3.0, direction="minimize", low=0, high=2),
        "qed": Spec(weight=0.5, direction="maximize", low=0.3, high=0.8),
        "mw": Spec(weight=0.5, direction="range", low=200, high=500),
    },
    "more drug-like": {
        "qed": Spec(weight=2.0, direction="maximize", low=0.3, high=0.9),
        "lipinski_violations": Spec(weight=1.5, direction="minimize", low=0, high=2),
        "mw": Spec(weight=1.0, direction="range", low=200, high=450),
        "logp": Spec(weight=1.0, direction="range", low=1, high=3),
        "tpsa": Spec(weight=1.0, direction="range", low=40, high=100),
        "alerts": Spec(weight=1.0, direction="minimize", low=0, high=2),
    },
    "more soluble": {
        "logp": Spec(weight=2.0, direction="range", low=0, high=2.5),
        "tpsa": Spec(weight=1.5, direction="range", low=60, high=120),
        "fsp3": Spec(weight=1.0, direction="maximize", low=0.2, high=0.6),
        "mw": Spec(weight=0.5, direction="range", low=150, high=450),
        "qed": Spec(weight=0.5, direction="maximize", low=0.3, high=0.8),
    },
}
