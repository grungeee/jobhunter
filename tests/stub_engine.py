#!/usr/bin/env python3
"""A stand-in for the model: prints one JSON reply and exits.

Tests point GENERATE_ENGINE_CMD at this file so no provider is ever called.
"""

import json
import os
import sys

reply = os.environ.get("STUB_REPLY")
if not reply or not os.path.exists(reply):
    sys.stderr.write("stub engine: STUB_REPLY is not a file\n")
    raise SystemExit(2)
with open(reply, encoding="utf-8") as handle:
    payload = json.load(handle)
sys.stdout.write("thinking...\n" + json.dumps(payload, ensure_ascii=False) + "\n")
