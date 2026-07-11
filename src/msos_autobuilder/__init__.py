"""MSOS Autobuilder public package surface."""

from .backends import GitWorkspaceError, LocalGitCloneBackend
from .contracts import ProductContract, load_product_contract
from .lanes import LaneConflictError, assert_lanes_compatible
from .leases import FileLeaseStore, LeaseConflictError, LeaseRecord, LeaseStateError
from .models import BuildLane, BuildTask, CostClass, WorkerCapabilities
from .routing import BackendRouter, BackendRoutingError
from .scheduler import ParallelScheduler, SchedulerError
from .workspace_witness import WorkspaceWitnessReport, run_workspace_witness
from .workspaces import WorkspaceIsolationError, WorkspacePolicy

__all__ = [
    "BackendRouter",
    "BackendRoutingError",
    "BuildLane",
    "BuildTask",
    "CostClass",
    "FileLeaseStore",
    "GitWorkspaceError",
    "LaneConflictError",
    "LeaseConflictError",
    "LeaseRecord",
    "LeaseStateError",
    "LocalGitCloneBackend",
    "ParallelScheduler",
    "ProductContract",
    "SchedulerError",
    "WorkerCapabilities",
    "WorkspaceIsolationError",
    "WorkspacePolicy",
    "WorkspaceWitnessReport",
    "assert_lanes_compatible",
    "load_product_contract",
    "run_workspace_witness",
]
