import io
import json
import logging

import pytest

from backend.core.logging import configure_logging, scrub_text

SMILES = [
    "CC(=O)Oc1ccccc1C(=O)O",
    "c1ccc2c(c1)cc[nH]2",
    "CN1CCC[C@H]1c1cccnc1",
    "CCCCCCO",
    "C[C@@H](N)C(=O)O",
]
MOLBLOCK = "\n  Mrv2014\n\n  1  0  0  0  0  0            999 V2000\n    0.0    0.0    0.0 C   0  0\nM  END\n"
EMAILS = ["alice@example.org", "bob.smith+tag@lab.example.co.uk"]


@pytest.fixture
def capture():
    buf = io.StringIO()
    configure_logging("DEBUG", stream=buf)
    yield buf
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_metis", False):
            root.removeHandler(h)


def output(buf):
    return buf.getvalue()


@pytest.mark.parametrize("smi", SMILES)
def test_smiles_never_logged(capture, smi):
    log = logging.getLogger("t")
    log.info("predicting %s now", smi)
    log.info(f"fstring {smi}")
    log.info("with extra", extra={"smiles": smi, "payload": {"smiles": smi, "n": 3}, "other": smi})
    log.info("arg dict %s", {"smiles": smi})
    assert smi not in output(capture)


def test_molblock_never_logged(capture):
    logging.getLogger("t").warning("upload failed: %s", MOLBLOCK)
    logging.getLogger("t").warning("x", extra={"molblock": MOLBLOCK, "blob": MOLBLOCK})
    out = output(capture)
    assert "V2000" not in out and "M  END" not in out and "Mrv2014" not in out


@pytest.mark.parametrize("email", EMAILS)
def test_email_never_logged(capture, email):
    log = logging.getLogger("t")
    log.info("login for %s", email)
    log.info("user", extra={"email": email, "detail": f"contact {email}"})
    assert email not in output(capture)


def test_secret_keys_blanked(capture):
    logging.getLogger("t").info(
        "evt",
        extra={
            "password": "hunter2",
            "token": "abcdef",
            "note": "my private note",
            "body": "raw",
            "Authorization": "Bearer zzz",
        },
    )
    out = output(capture)
    for leaked in ("hunter2", "abcdef", "my private note", "zzz"):
        assert leaked not in out
    assert json.loads(out.strip().splitlines()[-1])["password"] == "[REDACTED]"


def test_key_value_in_message(capture):
    logging.getLogger("t").info("got password=hunter2 token: abc123 smiles=CCO")
    out = output(capture)
    assert "hunter2" not in out and "abc123" not in out


def test_exception_text_not_logged(capture):
    try:
        raise ValueError("bad row CC(=O)Oc1ccccc1C(=O)O for alice@example.org")
    except ValueError:
        logging.getLogger("t").exception("failed")
    out = output(capture)
    assert "alice@example.org" not in out and "CC(=O)Oc1ccccc1C(=O)O" not in out
    assert "ValueError" in out


def test_output_is_json_and_keeps_benign_fields(capture):
    logging.getLogger("api").info("request done", extra={"status": 200, "route": "/health", "ms": 12.5})
    rec = json.loads(output(capture).strip().splitlines()[-1])
    assert rec["status"] == 200 and rec["route"] == "/health" and rec["msg"] == "request done"
    assert rec["level"] == "INFO"


@pytest.mark.parametrize(
    "benign",
    [
        "request completed in 12 ms",
        "version 1.2.3 started",
        "GET /projects/1234",
        "user created",
        "Colonies counted",
    ],
)
def test_benign_text_is_kept(benign):
    assert scrub_text(benign) == benign
