"""多 Agent 编排器（P5）：Planner → board → researcher/verifier/synthesizer → 报告。"""

from .errors import (
    BoardInvariantError,
    ContextBoundaryError,
    OrchestrationError,
    OrchestratorPlanError,
    SynthesisError,
    UnknownWorkerError,
    VerifierRejectedError,
)
from .orchestrator import (
    RUN_ID_SCHEMA,
    MultiAgentOrchestrator,
    OrchestrationResult,
    OrchestratorConfig,
    run_orchestration,
)
from .research_adapter import (
    REPORT_PAYLOAD_SCHEMA,
    RuntimeResearchWorker,
    ScriptedReportWorker,
)
from .roles import P5_ROLES, context_with_candidates, declared_candidates, validate_role_graph
from .synthesizer import SynthesizerWorker
from .verifier import VERDICT_PASSED, VERDICT_REJECTED, VerifierWorker

__all__ = [
    "P5_ROLES",
    "REPORT_PAYLOAD_SCHEMA",
    "RUN_ID_SCHEMA",
    "VERDICT_PASSED",
    "VERDICT_REJECTED",
    "BoardInvariantError",
    "ContextBoundaryError",
    "MultiAgentOrchestrator",
    "OrchestrationError",
    "OrchestrationResult",
    "OrchestratorConfig",
    "OrchestratorPlanError",
    "RuntimeResearchWorker",
    "ScriptedReportWorker",
    "SynthesisError",
    "SynthesizerWorker",
    "UnknownWorkerError",
    "VerifierRejectedError",
    "VerifierWorker",
    "context_with_candidates",
    "declared_candidates",
    "run_orchestration",
    "validate_role_graph",
]
