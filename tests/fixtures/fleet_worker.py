from __future__ import annotations

import json
import sys

request = json.loads(sys.stdin.readline())
step = request["step"]
result = {
    "status": "completed",
    "summary": f"local process completed {step['id']}",
    "evidence": [f"process:{request['worker_id']}"],
    "artifact_handles": [],
    "token_usage": 7,
    "approved": True if step["profile"] == "reviewer" else None,
    "receipt": {
        "attempt": request["attempt"],
        "model": step["model"],
        "reasoning": step["reasoning"],
        "route": step["route"],
    },
}
sys.stdout.write(json.dumps(result, sort_keys=True))
