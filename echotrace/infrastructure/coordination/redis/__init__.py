"""Redis coordination plane for task dispatch and derived runtime state.

Redis is deliberately kept behind small ports.  It never owns investigation
facts, evidence, conclusions, or audit history; those remain in MySQL.
"""

from .cache import RedisResponseCache
from .client import InMemoryRedis, RedisStreamBus
from .config import RedisConfig
from .contracts import InvestigationStartMessage, NodeTaskMessage, RedisKeys
from .dispatcher import OutboxDispatcher
from .notifier import RunEventNotifier
from .permit import Permit, RedisPermitManager
from .rate_limit import RateLimitDecision, RedisRateLimiter
from .lease import Lease, RedisLeaseManager
from .request_gate import RedisCallCoordinator, RedisCallGateError
from .worker import OrchestratorRunWorker, ReportComposerWorker, StreamTaskWorker, WorkerResult

__all__ = [
    "InMemoryRedis",
    "OutboxDispatcher",
    "RateLimitDecision",
    "RedisKeys",
    "RedisConfig",
    "RedisResponseCache",
    "RedisRateLimiter",
    "RedisLeaseManager",
    "RedisCallCoordinator",
    "RedisCallGateError",
    "RedisStreamBus",
    "RunEventNotifier",
    "RedisPermitManager",
    "Permit",
    "Lease",
    "StreamTaskWorker",
    "OrchestratorRunWorker",
    "ReportComposerWorker",
    "NodeTaskMessage",
    "InvestigationStartMessage",
    "WorkerResult",
]
