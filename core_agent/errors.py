class CoreError(Exception):
    def __init__(self, code, message=None, *, retryable=False, data=None):
        self.code = code
        self.message = message or code
        self.retryable = retryable
        self.data = data or {}
        super().__init__(self.message)
