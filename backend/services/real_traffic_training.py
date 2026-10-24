"""Local, single-flight periodic real-data fitting while the API is running."""
from datetime import datetime, timezone
import os
import threading
from services import real_traffic_model


class RealTrafficTrainer:
    def __init__(self):
        self._stop = threading.Event()
        self._thread = None
        self._state = {'active': False, 'last_attempt_at': None, 'last_success_at': None, 'error': None}
        self._lock = threading.Lock()

    def status(self):
        with self._lock:
            return dict(self._state)

    def train_once(self, now=None):
        now = now or datetime.now(timezone.utc)
        with self._lock:
            self._state['last_attempt_at'] = now.isoformat()
        try:
            rows, available, archive = real_traffic_model.snapshot()
            payload = real_traffic_model.train(rows, available, now=now)
            payload['report']['archive'] = archive
            real_traffic_model.save_artifact(payload)
            with self._lock:
                self._state.update(last_success_at=now.isoformat(), error=None)
            return True
        except Exception:
            with self._lock:
                self._state['error'] = 'Training unavailable; previous complete model retained.'
            return False

    def _run(self):
        try:
            while not self._stop.is_set():
                self.train_once()
                if self._stop.wait(3600):
                    break
        finally:
            with self._lock:
                self._state['active'] = False

    def start(self):
        if os.environ.get('CROSSFLOW_REAL_TRAFFIC_TRAINING') != '1' or os.environ.get('VERCEL') or os.environ.get('AWS_LAMBDA_FUNCTION_NAME'):
            return False
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._state['active'] = True
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name='crossflow-observed-training', daemon=True)
            self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)


TRAINER = RealTrafficTrainer()
