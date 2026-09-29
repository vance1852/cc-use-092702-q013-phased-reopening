"""恢复治理服务向 API 和 CLI 暴露的稳定错误。"""


class RecoveryError(RuntimeError):
    code = "recovery_error"
    status = 400


class NotFound(RecoveryError):
    code = "not_found"
    status = 404


class Conflict(RecoveryError):
    code = "conflict"
    status = 409


class Forbidden(RecoveryError):
    code = "forbidden"
    status = 403


class InvalidState(RecoveryError):
    code = "invalid_state"
    status = 409


class ValidationFailed(RecoveryError):
    code = "validation_failed"
    status = 422
