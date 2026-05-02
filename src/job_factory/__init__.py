"""Job_Factory — profile discovery, matching, and dynamic creation.

Sits between :class:`Refiner` and :class:`Orchestrator`. Given a raw
user request, the Factory:

  1. Scans ``profiles/`` for available profiles (cached with TTL).
  2. Scores each against the request using keyword/semantic hints.
  3. If a confident match exists → returns that profile_id.
  4. Otherwise → synthesizes a new profile from a template and registers
     it on disk, using the standard directory layout.

LangChain analogy:
  A ``Runnable`` that maps ``user_message → profile_id`` with a fallback
  branch (profile creation) when no mapping exists.
"""
from src.job_factory.classifier import JobClassification, JobClassifier
from src.job_factory.dispatcher import (
    ApprovalRequest,
    DispatchOutcome,
    DispatchResult,
    JobFactoryDispatcher,
    StepRecord,
    ValidatorFn,
)
from src.job_factory.factory import (
    JobFactory,
    JobFactoryError,
    ProfileMatch,
)
from src.job_factory.policy import (
    CloudPolicy,
    CloudPolicyConfig,
    PolicyOutcome,
    PolicyVerdict,
)
from src.job_factory.profile_template import render_new_profile
from src.job_factory.registry import (
    ClassifierConfig,
    DiscoveryConfig,
    JobType,
    JobTypeRegistry,
    ModelEntry,
    ModelRegistry,
    RegistryConfigError,
)
from src.job_factory.runner import (
    ALWAYS_ALLOWED_TOOLS,
    ActionRunner,
    ToolCall,
    ToolRegistry,
    ToolResult,
    parse_llm_output,
)
from src.job_factory.score_matrix import (
    ScoreMatrix,
    ScoreStats,
)
from src.job_factory.selector import (
    EpsilonGreedySelector,
    Selection,
    SelectionReason,
)
from src.job_factory.validator import (
    CompositeValidator,
    LLMJudgeValidator,
    LengthValidator,
    ResponseValidator,
    StructuralValidator,
    ValidationResult,
    default_rubric,
    make_dispatcher_validator,
)

__all__ = [
    # Legacy v1 (profile matching)
    "JobFactory",
    "JobFactoryError",
    "ProfileMatch",
    "render_new_profile",
    # v2 Phase 1: bandit primitives
    "ScoreMatrix",
    "ScoreStats",
    "EpsilonGreedySelector",
    "Selection",
    "SelectionReason",
    # v2 Phase 5: registries
    "JobType",
    "JobTypeRegistry",
    "ClassifierConfig",
    "ModelEntry",
    "ModelRegistry",
    "DiscoveryConfig",
    "RegistryConfigError",
    # v2 Phase 5: classifier
    "JobClassifier",
    "JobClassification",
    # v2 Phase 5: runner / tool execution
    "ActionRunner",
    "ToolRegistry",
    "ToolCall",
    "ToolResult",
    "parse_llm_output",
    "ALWAYS_ALLOWED_TOOLS",
    # v2 Phase 5: dispatcher
    "JobFactoryDispatcher",
    "DispatchResult",
    "DispatchOutcome",
    "StepRecord",
    "ApprovalRequest",
    "ValidatorFn",
    # v2 Phase 6: validator
    "ResponseValidator",
    "LengthValidator",
    "StructuralValidator",
    "LLMJudgeValidator",
    "CompositeValidator",
    "ValidationResult",
    "default_rubric",
    "make_dispatcher_validator",
    # v2 Phase 6: cloud policy
    "CloudPolicy",
    "CloudPolicyConfig",
    "PolicyVerdict",
    "PolicyOutcome",
]
