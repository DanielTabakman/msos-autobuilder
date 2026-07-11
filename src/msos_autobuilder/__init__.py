"""MSOS Autobuilder public package surface."""

from .backends import (
    GitWorkspaceError,
    LocalGitCloneBackend,
    LocalProcessBackend,
    ProcessExecutionError,
)
from .contracts import ProductContract, load_product_contract
from .lanes import (
    LaneConflictError,
    LaneScopeError,
    assert_changed_paths_allowed,
    assert_lanes_compatible,
)
from .leases import FileLeaseStore, LeaseConflictError, LeaseRecord, LeaseStateError
from .models import BuildLane, BuildTask, CostClass, WorkerCapabilities
from .process_witness import ProcessWitnessReport, run_process_witness
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
    "LaneScopeError",
    "LeaseConflictError",
    "LeaseRecord",
    "LeaseStateError",
    "LocalGitCloneBackend",
    "LocalProcessBackend",
    "ParallelScheduler",
    "ProcessExecutionError",
    "ProcessWitnessReport",
    "ProductContract",
    "SchedulerError",
    "WorkerCapabilities",
    "WorkspaceIsolationError",
    "WorkspacePolicy",
    "WorkspaceWitnessReport",
    "assert_changed_paths_allowed",
    "assert_lanes_compatible",
    "load_product_contract",
    "run_process_witness",
    "run_workspace_witness",
]
