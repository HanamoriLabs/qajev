"""A fresh Chrome's chain in a thread of this process, so a test's patches reach it: the real _fresh_chain and the
real message protocol, with only the process boundary gone. A test swaps runner._start_chain for start_chain."""

import queue
import threading

from qajev import runner


class Chain:
    def __init__(self, payload):
        self.messages = queue.Queue()
        self.exitcode = None
        self.thread = threading.Thread(target=self._run, args=(payload,), daemon=True)

    def _run(self, payload):
        try:
            runner._fresh_chain(payload, self.messages)
            self.exitcode = 0
        except BaseException:
            self.exitcode = 1
            raise

    def is_alive(self):
        return self.thread.is_alive()

    def join(self, timeout=None):
        self.thread.join(timeout)

    def terminate(self):  # a thread cannot be stopped: these tests never hang a chain
        pass

    kill = terminate


def start_chain(_ctx, payload):
    chain = Chain(payload)
    chain.thread.start()
    return chain, chain.messages
