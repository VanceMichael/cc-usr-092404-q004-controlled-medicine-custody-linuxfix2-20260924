"""领域错误：HTTP 层据此映射状态码，不泄露内部堆栈。"""


class DomainError(Exception):
    status_code = 400

    def __init__(self, code: str, message: str, status_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        if status_code is not None:
            self.status_code = status_code


class NotFound(DomainError):
    def __init__(self, message: str = "资源不存在"):
        super().__init__("NOT_FOUND", message, 404)


class Conflict(DomainError):
    def __init__(self, code: str = "CONFLICT", message: str = "状态冲突"):
        super().__init__(code, message, 409)


class ValidationFailed(DomainError):
    def __init__(self, message: str):
        super().__init__("VALIDATION_FAILED", message, 422)


class ManualReviewRequired(DomainError):
    """越节点扫码、链路顺序异常等：不拒绝事实，但冻结自动流转。"""

    def __init__(self, message: str, review_id: int | None = None):
        super().__init__("MANUAL_REVIEW_REQUIRED", message, 202)
        self.review_id = review_id
