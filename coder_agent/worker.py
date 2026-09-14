from __future__ import annotations

import logging
import threading

from .agent import CoderAgent


class Worker:
    def __init__(self, agent: CoderAgent, poll_seconds: float = 5.0):
        self.agent, self.poll_seconds = agent, poll_seconds
        self.stop_event = threading.Event()

    def run_forever(self) -> None:
        logging.info("worker %s started", self.agent.worker_id)
        while not self.stop_event.is_set():
            task = self.agent.store.claim(self.agent.worker_id)
            if task:
                result = self.agent.run_task(task)
                logging.info("step %s ended with %s", task.step_id, result.status)
            else:
                self.stop_event.wait(self.poll_seconds)

