#!/bin/bash
set -eu
umask 077
python3 - <<'PY'
import json, os
from datetime import datetime, timezone
from pathlib import Path
destination = Path('/opt/athena/runs/job-outcome.json')
temporary = destination.with_suffix('.tmp')
with temporary.open('w') as stream:
    stream.write(json.dumps({
    'finished_at': datetime.now(timezone.utc).isoformat(),
    'service_result': os.environ.get('SERVICE_RESULT', 'UNKNOWN'),
    'exit_code': os.environ.get('EXIT_CODE', 'UNKNOWN'),
    'exit_status': os.environ.get('EXIT_STATUS', 'UNKNOWN'),
    'export_state': 'Receipt and distinct-copy acknowledgement required before deleting evidence',
    'limit': 'Supervisor process outcome; benchmark assertions and authenticity require separate inspection'
}, indent=2) + '\n')
    stream.flush()
    os.fsync(stream.fileno())
os.replace(temporary, destination)
descriptor = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
sync
