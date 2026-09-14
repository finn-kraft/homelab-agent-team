from __future__ import annotations

import logging
import threading


class PlannerWorker:
    def __init__(self, planner, poll_seconds: float = 5):
        self.planner, self.poll_seconds = planner, poll_seconds
        self.stop_event = threading.Event()

    def run_forever(self) -> None:
        logging.info("planner worker %s started", self.planner.worker_id)
        while not self.stop_event.is_set():
            decision = self.planner.plan_once()
            if decision:
                logging.info("planner decision: %s", decision.decision)
            else:
                self.stop_event.wait(self.poll_seconds)

