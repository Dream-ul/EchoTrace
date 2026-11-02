"""Shared domain validation errors."""


class TraceContractError(ValueError):
    """Raised when a node or report contract is invalid."""


__all__ = ["TraceContractError"]
