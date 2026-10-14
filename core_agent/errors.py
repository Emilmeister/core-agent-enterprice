from dataclasses import dataclass, fields


@dataclass(frozen=True)
class SandboxDiagnostics:
    """Finite private operator metadata; never part of public error data."""
    stage: str
    reason: str
    elapsed_ms: int
    cleanup: str
    errno: int | None = None
    returncode: int | None = None
    slots_active: int | None = None
    slots_limit: int | None = None
    memory_current_bytes: int | None = None
    memory_limit_bytes: int | None = None
    pids_current: int | None = None
    pids_limit: int | None = None
    oom_kill_count: int | None = None

    def __post_init__(self):
        if (any(type(value) is not str for value in (self.stage, self.reason, self.cleanup))
                or self.stage not in {'native_check', 'queue', 'supervisor', 'namespace', 'network', 'target_exec', 'cleanup'}
                or self.reason not in {'timeout', 'child_exit', 'protocol_invalid', 'os_error', 'unavailable',
                    'unsupported', 'dependency_missing', 'policy_invalid', 'validation_failed', 'cancelled'}
                or self.cleanup not in {'not_started', 'confirmed', 'unconfirmed'}):
            raise ValueError('Invalid sandbox diagnostics')
        for field in fields(self):
            if field.name in {'stage', 'reason', 'cleanup'}:
                continue
            value = getattr(self, field.name)
            if value is None and field.name != 'elapsed_ms':
                continue
            lower, upper = (0, 2**63 - 1)
            if field.name == 'elapsed_ms':
                upper = 2**31 - 1
            elif field.name == 'errno':
                lower, upper = 1, 4095
            elif field.name == 'returncode':
                lower, upper = -255, 255
            elif field.name.endswith('_limit') or field.name == 'memory_limit_bytes':
                lower = 1
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError('Invalid sandbox diagnostics')

    def as_dict(self):
        return {field.name: getattr(self, field.name) for field in fields(self)
                if getattr(self, field.name) is not None}


class CoreError(Exception):
    def __init__(self, code, message=None, *, retryable=False, data=None, diagnostics=None):
        self.code = code
        self.message = message or code
        self.retryable = retryable
        self.data = data or {}
        self.diagnostics = diagnostics if type(diagnostics) is SandboxDiagnostics else None
        super().__init__(self.message)


class ExecutionNotStarted(CoreError):
    """Trusted local evidence that the requested command was never released."""
