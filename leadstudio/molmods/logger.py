"""Silent logger for the edit operators.

The original helper attached a FileHandler (./logs) and a console handler to the ROOT logger and the operators log
f-strings that can contain SMILES. A server must do neither (structures are never logged), so this returns a logger
that is never enabled and is not part of the logging tree.
"""

import logging


def setup_molecule_logger(log_dir=None, log_filename=None):  # arguments kept for call compatibility; ignored
    return logging.Logger("leadstudio.molmods.silent", level=logging.CRITICAL + 10)
