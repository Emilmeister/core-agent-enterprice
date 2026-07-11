class MemoryServiceError(Exception):
    def __init__(self, code, message=None, *, data=None):
        self.code = code
        self.message = message or code
        self.data = data or {}
        super().__init__(self.message)
