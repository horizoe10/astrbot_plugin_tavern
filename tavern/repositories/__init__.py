"""Repository mixins grouped by persistent domain."""

from .admin_overview import AdminOverviewRepositoryMixin
from .ai_turns import AiTurnsRepositoryMixin
from .ai_turns_queries import AiTurnsQueriesRepositoryMixin
from .audit import AuditRepositoryMixin
from .backup_import import BackupImportRepositoryMixin
from .character_assets import CharacterAssetsRepositoryMixin
from .character_cards import CharacterCardsRepositoryMixin
from .character_cards_mutations import CharacterCardsMutationsRepositoryMixin
from .character_cards_queries import CharacterCardsQueriesRepositoryMixin
from .character_cards_transactions import CharacterCardsTransactionsRepositoryMixin
from .character_reviews import CharacterReviewsRepositoryMixin
from .character_web_state import CharacterWebStateRepositoryMixin
from .character_runtime import CharacterRuntimeRepositoryMixin
from .character_runtime_queries import CharacterRuntimeQueriesRepositoryMixin
from .choices import ChoicesRepositoryMixin
from .choices_queries import ChoicesQueriesRepositoryMixin
from .conditions import ConditionsRepositoryMixin
from .currency_exchange import CurrencyExchangeRepositoryMixin
from .delegations import DelegationsRepositoryMixin
from .delivery_leases import DeliveryLeasesRepositoryMixin
from .delivery_queue import DeliveryQueueRepositoryMixin
from .delivery_results import DeliveryResultsRepositoryMixin
from .economy_bootstrap import EconomyBootstrapRepositoryMixin
from .economy_transactions import EconomyTransactionsRepositoryMixin
from .fate_state import FateStateRepositoryMixin
from .fate_consequence_commit import FateConsequenceCommitRepositoryMixin
from .generation_runs import GenerationRunsRepositoryMixin
from .generation_reminders import GenerationReminderRepositoryMixin
from .health_maintenance_runtime import HealthMaintenanceRuntimeRepositoryMixin
from .item_mutations import ItemMutationsRepositoryMixin
from .item_ownership import ItemOwnershipRepositoryMixin
from .item_queries import ItemQueriesRepositoryMixin
from .memories import MemoriesRepositoryMixin
from .participants import ParticipantsRepositoryMixin
from .participants_queries import ParticipantsQueriesRepositoryMixin
from .permissions import PermissionsRepositoryMixin
from .rescue_windows import RescueWindowsRepositoryMixin
from .rule_config import RuleConfigRepositoryMixin
from .rule_receipts import RuleReceiptsRepositoryMixin
from .rule_receipts_queries import RuleReceiptsQueriesRepositoryMixin
from .rule_runtime import RuleRuntimeRepositoryMixin
from .rule_runtime_queries import RuleRuntimeQueriesRepositoryMixin
from .session_lifecycle import SessionLifecycleRepositoryMixin
from .session_lifecycle_mutations import SessionLifecycleMutationsRepositoryMixin
from .session_lifecycle_queries import SessionLifecycleQueriesRepositoryMixin
from .session_permissions import SessionPermissionsRepositoryMixin
from .session_queries import SessionQueriesRepositoryMixin
from .snapshots_queries import SnapshotsQueriesRepositoryMixin
from .snapshots import SnapshotsRepositoryMixin
from .story_log import StoryLogRepositoryMixin
from .story_log_queries import StoryLogQueriesRepositoryMixin
from .supplement_actions import SupplementActionsRepositoryMixin
from .supplement_actions_queries import SupplementActionsQueriesRepositoryMixin
from .supplement_queries import SupplementQueriesRepositoryMixin
from .supplement_receipts import SupplementReceiptsRepositoryMixin
from .terminal_state import TerminalStateRepositoryMixin
from .timer_policy import TimerPolicyRepositoryMixin
from .timer_runtime import TimerRuntimeRepositoryMixin
from .timer_scheduler import TimerSchedulerRepositoryMixin
from .turn_deliveries import TurnDeliveriesRepositoryMixin
from .turn_queue import TurnQueueRepositoryMixin
from .turn_queue_queries import TurnQueueQueriesRepositoryMixin
from .wallets import WalletsRepositoryMixin
from .workflow_operations import WorkflowOperationsRepositoryMixin
from .world_authoring import WorldAuthoringRepositoryMixin
from .world_authoring_queries import WorldAuthoringQueriesRepositoryMixin
from .world_catalog import WorldCatalogRepositoryMixin
from .world_modules import WorldModulesRepositoryMixin
from .world_modules_queries import WorldModulesQueriesRepositoryMixin
from .world_packages import WorldPackagesRepositoryMixin


from .current_state import CurrentStateRepositoryMixin
from .control import ControlRepositoryMixin
from .dm import DmRepositoryMixin
from .world_commands import WorldCommandRepositoryMixin
from .atomic_purchase import AtomicPurchaseMixin
from .world_edits import WorldEditRepositoryMixin
from .pacing import PacingRepositoryMixin
from .growth import GrowthRepositoryMixin
from .turn_commits import TurnCommitRepositoryMixin
from .tendencies import TendencyRepositoryMixin
from .tendency_rebuild import TendencyRebuildRepositoryMixin
from .knowledge import KnowledgeRepositoryMixin
from .author_jobs import AuthorJobRepositoryMixin
from .author_job_receipts import AuthorJobReceiptRepositoryMixin
from .author_job_workers import AuthorJobWorkerRepositoryMixin
from .health import HealthRepositoryMixin
from .health_recovery import HealthRecoveryRepositoryMixin
from .health_diagnostics import HealthDiagnosticRepositoryMixin
from .health_summary import HealthSummaryRepositoryMixin
from .outbox import OutboxRepositoryMixin
from .principal_bindings import PrincipalBindingRepositoryMixin
from .room_invites import RoomInviteRepositoryMixin
from .ai_companions import AiCompanionRepositoryMixin
from .ai_decisions import AiDecisionRepositoryMixin
from .choice_recovery import ChoiceRecoveryRepositoryMixin
from .world_module_status import WorldModuleStatusRepositoryMixin
from .github_imports import GithubImportRepositoryMixin
from .twp_imports import TwpImportRepositoryMixin
from .gameplay_runtime import GameplayRuntimeRepositoryMixin


class RepositoryFacade(
    AdminOverviewRepositoryMixin,
    AiTurnsRepositoryMixin,
    AiTurnsQueriesRepositoryMixin,
    AuditRepositoryMixin,
    BackupImportRepositoryMixin,
    CharacterAssetsRepositoryMixin,
    CharacterWebStateRepositoryMixin,
    CharacterCardsRepositoryMixin,
    CharacterCardsMutationsRepositoryMixin,
    CharacterCardsQueriesRepositoryMixin,
    CharacterCardsTransactionsRepositoryMixin,
    CharacterReviewsRepositoryMixin,
    CharacterRuntimeRepositoryMixin,
    CharacterRuntimeQueriesRepositoryMixin,
    ChoicesRepositoryMixin,
    ChoicesQueriesRepositoryMixin,
    ConditionsRepositoryMixin,
    CurrencyExchangeRepositoryMixin,
    DelegationsRepositoryMixin,
    DeliveryLeasesRepositoryMixin,
    DeliveryQueueRepositoryMixin,
    DeliveryResultsRepositoryMixin,
    EconomyBootstrapRepositoryMixin,
    EconomyTransactionsRepositoryMixin,
    FateStateRepositoryMixin,
    FateConsequenceCommitRepositoryMixin,
    GenerationRunsRepositoryMixin,
    GenerationReminderRepositoryMixin,
    HealthMaintenanceRuntimeRepositoryMixin,
    ItemMutationsRepositoryMixin,
    ItemOwnershipRepositoryMixin,
    ItemQueriesRepositoryMixin,
    MemoriesRepositoryMixin,
    ParticipantsRepositoryMixin,
    ParticipantsQueriesRepositoryMixin,
    PermissionsRepositoryMixin,
    RescueWindowsRepositoryMixin,
    RuleConfigRepositoryMixin,
    RuleReceiptsRepositoryMixin,
    RuleReceiptsQueriesRepositoryMixin,
    RuleRuntimeRepositoryMixin,
    RuleRuntimeQueriesRepositoryMixin,
    SessionLifecycleRepositoryMixin,
    SessionLifecycleMutationsRepositoryMixin,
    SessionLifecycleQueriesRepositoryMixin,
    SessionPermissionsRepositoryMixin,
    SessionQueriesRepositoryMixin,
    SnapshotsQueriesRepositoryMixin,
    SnapshotsRepositoryMixin,
    StoryLogRepositoryMixin,
    StoryLogQueriesRepositoryMixin,
    SupplementActionsRepositoryMixin,
    SupplementActionsQueriesRepositoryMixin,
    SupplementQueriesRepositoryMixin,
    SupplementReceiptsRepositoryMixin,
    TerminalStateRepositoryMixin,
    TimerPolicyRepositoryMixin,
    TimerRuntimeRepositoryMixin,
    TimerSchedulerRepositoryMixin,
    TurnDeliveriesRepositoryMixin,
    TurnQueueRepositoryMixin,
    TurnQueueQueriesRepositoryMixin,
    WalletsRepositoryMixin,
    WorkflowOperationsRepositoryMixin,
    WorldAuthoringRepositoryMixin,
    WorldAuthoringQueriesRepositoryMixin,
    WorldCatalogRepositoryMixin,
    WorldModulesRepositoryMixin,
    WorldModulesQueriesRepositoryMixin,
    WorldPackagesRepositoryMixin,
    CurrentStateRepositoryMixin,
    ControlRepositoryMixin,
    DmRepositoryMixin,
    WorldCommandRepositoryMixin,
    AtomicPurchaseMixin,
    WorldEditRepositoryMixin,
    PacingRepositoryMixin,
    GrowthRepositoryMixin,
    TurnCommitRepositoryMixin,
    TendencyRepositoryMixin,
    TendencyRebuildRepositoryMixin,
    KnowledgeRepositoryMixin,
    AuthorJobRepositoryMixin,
    AuthorJobReceiptRepositoryMixin,
    AuthorJobWorkerRepositoryMixin,
    HealthRepositoryMixin,
    HealthRecoveryRepositoryMixin,
    HealthDiagnosticRepositoryMixin,
    HealthSummaryRepositoryMixin,
    OutboxRepositoryMixin,
    PrincipalBindingRepositoryMixin,
    RoomInviteRepositoryMixin,
    AiCompanionRepositoryMixin,
    AiDecisionRepositoryMixin,
    ChoiceRecoveryRepositoryMixin,
    WorldModuleStatusRepositoryMixin,
    GithubImportRepositoryMixin,
    TwpImportRepositoryMixin,
    GameplayRuntimeRepositoryMixin,
):
    """Compose stable repository domains without exposing retired shard names."""

__all__ = [
    "RepositoryFacade",
    "AdminOverviewRepositoryMixin",
    "AiTurnsRepositoryMixin",
    "AiTurnsQueriesRepositoryMixin",
    "AuditRepositoryMixin",
    "BackupImportRepositoryMixin",
    "CharacterAssetsRepositoryMixin",
    "CharacterCardsRepositoryMixin",
    "CharacterCardsMutationsRepositoryMixin",
    "CharacterCardsQueriesRepositoryMixin",
    "CharacterCardsTransactionsRepositoryMixin",
    "CharacterReviewsRepositoryMixin",
    "CharacterRuntimeRepositoryMixin",
    "CharacterRuntimeQueriesRepositoryMixin",
    "ChoicesRepositoryMixin",
    "ChoicesQueriesRepositoryMixin",
    "ConditionsRepositoryMixin",
    "CurrencyExchangeRepositoryMixin",
    "DelegationsRepositoryMixin",
    "DeliveryLeasesRepositoryMixin",
    "DeliveryQueueRepositoryMixin",
    "DeliveryResultsRepositoryMixin",
    "EconomyBootstrapRepositoryMixin",
    "EconomyTransactionsRepositoryMixin",
    "FateStateRepositoryMixin",
    "FateConsequenceCommitRepositoryMixin",
    "GenerationRunsRepositoryMixin",
    "GenerationReminderRepositoryMixin",
    "HealthMaintenanceRuntimeRepositoryMixin",
    "ItemMutationsRepositoryMixin",
    "ItemOwnershipRepositoryMixin",
    "ItemQueriesRepositoryMixin",
    "MemoriesRepositoryMixin",
    "ParticipantsRepositoryMixin",
    "ParticipantsQueriesRepositoryMixin",
    "PermissionsRepositoryMixin",
    "RescueWindowsRepositoryMixin",
    "RuleConfigRepositoryMixin",
    "RuleReceiptsRepositoryMixin",
    "RuleReceiptsQueriesRepositoryMixin",
    "RuleRuntimeRepositoryMixin",
    "RuleRuntimeQueriesRepositoryMixin",
    "SessionLifecycleRepositoryMixin",
    "SessionLifecycleMutationsRepositoryMixin",
    "SessionLifecycleQueriesRepositoryMixin",
    "SessionPermissionsRepositoryMixin",
    "SessionQueriesRepositoryMixin",
    "SnapshotsQueriesRepositoryMixin",
    "SnapshotsRepositoryMixin",
    "StoryLogRepositoryMixin",
    "StoryLogQueriesRepositoryMixin",
    "SupplementActionsRepositoryMixin",
    "SupplementActionsQueriesRepositoryMixin",
    "SupplementQueriesRepositoryMixin",
    "SupplementReceiptsRepositoryMixin",
    "TerminalStateRepositoryMixin",
    "TimerPolicyRepositoryMixin",
    "TimerRuntimeRepositoryMixin",
    "TimerSchedulerRepositoryMixin",
    "TurnDeliveriesRepositoryMixin",
    "TurnQueueRepositoryMixin",
    "TurnQueueQueriesRepositoryMixin",
    "WalletsRepositoryMixin",
    "WorkflowOperationsRepositoryMixin",
    "WorldAuthoringRepositoryMixin",
    "WorldAuthoringQueriesRepositoryMixin",
    "WorldCatalogRepositoryMixin",
    "WorldModulesRepositoryMixin",
    "WorldModulesQueriesRepositoryMixin",
    "WorldPackagesRepositoryMixin",
    "CurrentStateRepositoryMixin",
    "ControlRepositoryMixin",
    "DmRepositoryMixin",
    "WorldCommandRepositoryMixin",
    "AtomicPurchaseMixin",
    "WorldEditRepositoryMixin",
    "PacingRepositoryMixin",
    "GrowthRepositoryMixin",
    "TurnCommitRepositoryMixin",
    "TendencyRepositoryMixin",
    "TendencyRebuildRepositoryMixin",
    "KnowledgeRepositoryMixin",
    "AuthorJobRepositoryMixin",
    "AuthorJobReceiptRepositoryMixin",
    "AuthorJobWorkerRepositoryMixin",
    "HealthRepositoryMixin",
    "HealthRecoveryRepositoryMixin",
    "HealthDiagnosticRepositoryMixin",
    "HealthSummaryRepositoryMixin",
    "OutboxRepositoryMixin",
    "PrincipalBindingRepositoryMixin",
    "RoomInviteRepositoryMixin",
    "AiCompanionRepositoryMixin",
    "AiDecisionRepositoryMixin",
    "ChoiceRecoveryRepositoryMixin",
    "WorldModuleStatusRepositoryMixin",
    "GithubImportRepositoryMixin",
    "TwpImportRepositoryMixin",
    "GameplayRuntimeRepositoryMixin",
]
