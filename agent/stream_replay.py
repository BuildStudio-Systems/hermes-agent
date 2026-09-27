"""Exact-prefix reconciliation only for an explicit interrupted-text retry."""


class StreamReplayPrefix:
    def __init__(self, prefix: str):
        self.prefix = prefix
        self.pending = ""
        self.decided = not prefix

    def feed(self, text: str) -> str:
        if self.decided:
            return text
        self.pending += text
        if self.prefix.startswith(self.pending) and len(self.pending) < len(self.prefix):
            return ""
        result = self.pending
        if result.startswith(self.prefix):
            result = result[len(self.prefix):]
        self.pending = ""
        self.decided = True
        return result

