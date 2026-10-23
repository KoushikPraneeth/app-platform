"""Session backstop: remove only approved temporary resources before the time limit."""
import datetime
import json
import time

from audit import OUT, now, save
from environment import cleanup

while True:
    state = json.loads((OUT / 'capacity-session.json').read_text())
    if state.get('status') == 'removed':
        print(now(), 'Temporary capacity already removed; guardian exiting', flush=True)
        break
    deadline = datetime.datetime.fromisoformat(state['cleanup_start_deadline_utc'])
    if datetime.datetime.now(datetime.timezone.utc) >= deadline:
        print(now(), 'Approved session deadline reached; removing temporary resources', flush=True)
        try:
            cleanup()
            save('guardian-result.json', {'utc': now(), 'status': 'removed_at_session_deadline'})
            break
        except Exception as error:
            save('guardian-result.json', {'utc': now(), 'status': 'cleanup_retry_required', 'error': str(error)})
    time.sleep(15)
