"""领域错误类型。

接口边界据此把业务失败翻译成稳定的 HTTP 响应，
服务层与持久化层只抛出这里的错误，不泄漏底层异常。
"""


class DomainError(Exception):
    """所有领域错误的基类。"""


class ValidationError(DomainError):
    """输入校验失败。"""


class NotFoundError(DomainError):
    """引用的实体不存在。"""


class PermissionDeniedError(DomainError):
    """当前操作者缺少所需角色或资格。"""


class ConflictError(DomainError):
    """状态冲突：重复提交或非法状态迁移。"""
