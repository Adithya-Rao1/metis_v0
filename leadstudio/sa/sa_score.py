"""Ertl & Schuffenhauer synthetic accessibility score (BSD-3-Clause, see LICENSE.txt).

Copied from the GraphDQN repo (originally the RDKit Contrib SA_Score). The fragment-score table is
`data/fpscores.pkl.gz` (identical to RDKit's Contrib copy). It is a pickle, so it is only ever loaded after its
SHA-256 matches the pinned value below (a tampered file is refused, never unpickled).
"""

import gzip
import hashlib
import io
import math
import pickle  # nosec B403 - only the SHA-256-pinned data file below is ever unpickled
from pathlib import Path

from rdkit import Chem
from rdkit.Chem import rdFingerprintGenerator, rdMolDescriptors

_fscores = None
_DATA_FILE = Path(__file__).resolve().parent / "data" / "fpscores.pkl.gz"
_DATA_SHA256 = "10dcef9340c873e7b987924461b0af5365eb8dd96be607203debe8ddf80c1e73"


class SyntheticAccessibility:
    @staticmethod
    def readFragmentScores(name="fpscores"):
        global _fscores
        raw = _DATA_FILE.read_bytes()
        if hashlib.sha256(raw).hexdigest() != _DATA_SHA256:
            raise RuntimeError("SA fragment score file failed its integrity check")
        with gzip.open(io.BytesIO(raw)) as fh:
            data = pickle.load(fh)  # nosec B301 # noqa: S301 - integrity-checked above
        outDict = {}
        for i in data:
            for j in range(1, len(i)):
                outDict[i[j]] = float(i[0])
        _fscores = outDict

    @staticmethod
    def numBridgeheadsAndSpiro(mol, ri=None):
        nSpiro = rdMolDescriptors.CalcNumSpiroAtoms(mol)
        nBridgehead = rdMolDescriptors.CalcNumBridgeheadAtoms(mol)
        return nBridgehead, nSpiro

    @staticmethod
    def calculateScore(m):
        if _fscores is None:
            SyntheticAccessibility.readFragmentScores()

        morgan_gen = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)
        fp = morgan_gen.GetCountFingerprint(m)
        fps = fp.GetNonzeroElements()

        score1 = 0.0
        nf = 0
        fscores = _fscores if _fscores is not None else {}  # loaded above
        for bitId, v in fps.items():
            nf += v
            sfp = bitId
            score1 += fscores.get(sfp, -4) * v
        score1 /= nf

        # features score
        nAtoms = m.GetNumAtoms()
        nChiralCenters = len(Chem.FindMolChiralCenters(m, includeUnassigned=True))
        ri = m.GetRingInfo()
        nBridgeheads, nSpiro = SyntheticAccessibility.numBridgeheadsAndSpiro(m, ri)
        nMacrocycles = 0
        for x in ri.AtomRings():
            if len(x) > 8:
                nMacrocycles += 1

        sizePenalty = nAtoms**1.005 - nAtoms
        stereoPenalty = math.log10(nChiralCenters + 1)
        spiroPenalty = math.log10(nSpiro + 1)
        bridgePenalty = math.log10(nBridgeheads + 1)
        macrocyclePenalty = 0.0
        # ---------------------------------------
        # This differs from the paper, which defines:
        #  macrocyclePenalty = math.log10(nMacrocycles+1)
        # This form generates better results when 2 or more macrocycles are present
        if nMacrocycles > 0:
            macrocyclePenalty = math.log10(2)

        score2 = 0.0 - sizePenalty - stereoPenalty - spiroPenalty - bridgePenalty - macrocyclePenalty

        # correction for the fingerprint density
        # not in the original publication, added in version 1.1
        # to make highly symmetrical molecules easier to synthetise
        score3 = 0.0
        if nAtoms > len(fps):
            score3 = math.log(float(nAtoms) / len(fps)) * 0.5

        sascore = score1 + score2 + score3

        # need to transform "raw" value into scale between 1 and 10
        min = -4.0
        max = 2.5
        sascore = 11.0 - (sascore - min + 1) / (max - min) * 9.0
        # smooth the 10-end
        if sascore > 8.0:
            sascore = 8.0 + math.log(sascore + 1.0 - 9.0)
        if sascore > 10.0:
            sascore = 10.0
        elif sascore < 1.0:
            sascore = 1.0

        return sascore

    @staticmethod
    def processSMILES(smiles):
        # print('smiles\tsa_score')
        sa_scores = []
        for smile in smiles:
            mol = Chem.MolFromSmiles(smile)
            if mol is None:
                raise ValueError("Invalid SMILES")
            s = SyntheticAccessibility.calculateScore(mol)
            sa_scores.append(s)

        return sa_scores


calculateScore = SyntheticAccessibility.calculateScore
