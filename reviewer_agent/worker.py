import threading
class ReviewerWorker:
    def __init__(self,reviewer,poll_seconds=5):self.reviewer,self.poll_seconds=reviewer,poll_seconds;self.stop_event=threading.Event()
    def run_forever(self):
        while not self.stop_event.is_set():
            if self.reviewer.review_once() is None:self.stop_event.wait(self.poll_seconds)

