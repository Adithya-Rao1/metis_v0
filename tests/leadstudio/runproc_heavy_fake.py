"""Stand-in heavy worker (same line protocol as leadstudio.server.heavychild) with a behaviour chosen by
STUDIO_TEST_FAKE: echo (answers with its arguments), slow (answers after 2 s), crash (dies), default: never answers."""
from __future__ import annotations

import json
import os
import time

from leadstudio.server import procutil

if __name__ == "__main__":
    mode = os.environ.get("STUDIO_TEST_FAKE", "hang")
    procutil.write_event_line(b'{"ready":true}')
    while (line := procutil.read_stdin_line()) is not None:
        req = json.loads(line)
        if mode == "crash":
            os._exit(139)
        if mode == "slow":
            time.sleep(2)
        if mode in ("echo", "slow"):
            procutil.write_event_line(json.dumps({"ok": {"args": req["args"], "pid": os.getpid()}}).encode())
        else:
            time.sleep(300)
