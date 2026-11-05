"""Stand-in run children for the process-isolation tests. A RunManager is given `child_module="runproc_fakes"`; the
child then runs the class named by STUDIO_TEST_FAKE (STUDIO_* variables are the only ones a child inherits besides
a few harmless ones) in place of the real engine."""
from __future__ import annotations

import os
import time

from leadstudio.server import runchild


class Hung:
    def run(self, run_id, config):
        yield {"type": "run_start", "payload": {}}
        time.sleep(120)                      # a native call that never comes back
        yield {"type": "run_end", "payload": {}}


class Quick:
    def run(self, run_id, config):
        yield {"type": "run_start", "payload": {}}
        yield {"type": "run_end", "payload": {}}


class Crash:
    def run(self, run_id, config):
        yield {"type": "run_start", "payload": {}}
        os._exit(139)                        # what a native crash looks like from outside


class Env:
    """Reports the child's environment keys and its pid in the run_start payload."""

    def run(self, run_id, config):
        yield {"type": "run_start", "payload": {"env": sorted(os.environ), "pid": os.getpid()}}
        yield {"type": "run_end", "payload": {}}


class Chatty:
    def run(self, run_id, config):
        yield {"type": "run_start", "payload": {}}
        for i in range(100000):
            yield {"type": "candidate", "payload": {"i": i}}


if __name__ == "__main__":
    runchild.serve(globals()[os.environ.get("STUDIO_TEST_FAKE", "Quick")])
