/**
 * Wire types for the SeekAndDestroy UI.
 *
 * Casing is NOT uniform across the gateway on purpose, and this file mirrors
 * reality rather than papering over it:
 *   - /api/cmdb/*                  -> camelCase (ASP.NET Core's default JSON
 *                                     serialization of the Dapper-mapped C# records)
 *   - /api/recommendations/*, /api/investigations/* -> passed through verbatim
 *     from the Python AI service: CMDB entities are PascalCase (they mirror
 *     SQL column names), while scoring/capacity/forecast objects are
 *     snake_case (they mirror the Python service's own Pydantic field names).
 */

export interface CmdbApplication {
  applicationId: number;
  applicationCode: string;
  applicationName: string;
  description: string | null;
  businessCriticality: string;
  environment: string;
  lifecycleStatus: string;
  technologyPlatform: string;
  operatingSystemRequirement: string;
  cpuRequirement: number;
  memoryRequirementGb: number;
  storageRequirementGb: number;
  expectedAnnualGrowthPercent: number;
  availabilityTier: string;
  dataClassification: string;
  preferredLocation: string | null;
}

export interface InfrastructureCluster {
  clusterId: number;
  clusterCode: string;
  clusterName: string;
  clusterType: string;
  platform: string;
  environment: string;
  dataCenter: string;
  region: string;
  lifecycleStatus: string;
  nodeCount: number;
  totalCpuCores: number;
  totalMemoryGb: number;
  totalStorageGb: number;
  monthlyCost: number;
  availabilityTier: string;
  complianceClassification: string;
}

export interface RuleResult {
  rule_id: string;
  name: string;
  passed: boolean;
  reason: string;
  evidence: Record<string, unknown>;
}

export interface SubScores {
  capacity: number;
  compatibility: number;
  resiliency: number;
  cost: number;
  dependency: number;
  historical: number;
  risk: number;
}

/** Subset of ClusterCapacitySnapshot / NodeCapacitySnapshot the UI reads. */
export interface CapacitySnapshot {
  effective_cpu_cores: number;
  effective_memory_gb: number;
  effective_storage_gb: number;
  consumed_cpu_cores: number;
  consumed_memory_gb: number;
  consumed_storage_gb: number;
  available_cpu_cores: number;
  available_memory_gb: number;
  available_storage_gb: number;
  current_cpu_utilization_percent: number;
  current_memory_utilization_percent: number;
  current_storage_utilization_percent: number;
}

export interface ProjectedUtilization {
  projected_cpu_utilization_percent: number;
  projected_memory_utilization_percent: number;
  projected_storage_utilization_percent: number;
  projected_headroom_percent: number;
  fits_all: boolean;
}

/** Node sub-scores are a smaller set than cluster sub-scores: compatibility,
 *  resiliency and dependency locality are cluster properties shared by every
 *  host inside it, so they cannot order hosts within one cluster. */
export interface NodeSubScores {
  capacity: number;
  cost: number;
  reliability: number;
  risk: number;
}

export interface NodeCandidateScore {
  node_id: number;
  host_name: string;
  cluster_id: number;
  cluster_code: string;
  lifecycle_status: string;
  eligibility_status: "Eligible" | "Rejected";
  rule_results: RuleResult[];
  subscores: NodeSubScores | null;
  overall_score: number | null;
  estimated_monthly_cost: number | null;
  snapshot: CapacitySnapshot | null;
  projected: ProjectedUtilization | null;
  rank: number | null;
  evidence: Record<string, unknown>;
}

export interface CandidateScore {
  cluster_id: number;
  cluster_code: string;
  /** Which data centre this cluster sits in. Set on every CandidateScore by
   *  placement, and until now rendered only on the review screen - so the
   *  ranked results table compared clusters across eight sites while showing
   *  nothing about site at all. `atl-03` and `den-03` are different buildings
   *  and the table said only the names. */
  data_center: string | null;
  eligibility_status: "Eligible" | "Rejected";
  rule_results: RuleResult[];
  subscores: SubScores | null;
  overall_score: number | null;
  estimated_monthly_cost: number | null;
  snapshot: CapacitySnapshot | null;
  projected: ProjectedUtilization | null;
  rank: number | null;
  /** Best hosts inside this cluster, populated only for the leading clusters
   *  (see SAD_POLICY__TOP_CLUSTERS / TOP_NODES_PER_CLUSTER). Empty otherwise -
   *  not a signal that the cluster has no usable hosts. */
  top_nodes: NodeCandidateScore[];
}

/** Narration is requested explicitly and may legitimately be absent: it is
 *  best-effort, so a quota refusal or a number-drift rejection yields null
 *  while every number on the page stays exactly as computed. */
/** Mirrors app/models/agent_contracts.py::TradeOffSummary exactly. Every field
 *  is required there, so none is optional here - inventing optional fields the
 *  contract does not have is how this rendered an empty panel on its first
 *  outing. */
export interface TradeOffSummary {
  title: string;
  comparison_points: string[];
  recommendation: string;
}

export interface HostingRecommendationResponse {
  application: Record<string, unknown>;
  requirement: Record<string, unknown>;
  candidates: CandidateScore[];
  tradeoffs?: TradeOffSummary | null;
}

export interface ClusterRightSizingResult {
  cluster_id: number;
  cluster_code: string;
  classification: "Overprovisioned" | "Underprovisioned" | "Healthy";
  current_node_count: number;
  recommended_node_count: number;
  node_delta: number;
  estimated_monthly_savings: number;
  estimated_annual_savings: number;
  risks: string[];
  rationale: string;
  snapshot: {
    current_cpu_utilization_percent: number;
    current_memory_utilization_percent: number;
    current_storage_utilization_percent: number;
  };
}

export interface ConsolidationCandidate {
  application_id: number;
  application_code: string;
  current_cluster_code: string;
  target_cluster_code: string;
  reason: string;
  estimated_monthly_savings: number;
  blocking_constraints: string[];
  feasible: boolean;
}

export interface ResourceForecast {
  resource: string;
  horizon_days: number;
  current_percent: number;
  predicted_percent: number;
  confidence_low_percent: number;
  confidence_high_percent: number;
  exhaustion_date: string | null;
  breaches_threshold_within_horizon: boolean;
  recommended_action: string;
}

export interface ForecastExplanation {
  entity_code: string;
  summary?: string;
  recommended_action?: string;
}

export interface ClusterForecast {
  cluster_id: number;
  cluster_code: string;
  horizon_days: number;
  cpu: ResourceForecast;
  memory: ResourceForecast;
  storage: ResourceForecast;
  /** Which resource was narrated - the one that breaches soonest, or is
   *  closest to it. Only the binding resource is explained; narrating all
   *  three costs three model calls to say two things nobody asked about. */
  explained_resource?: "cpu" | "memory" | "storage";
  explanation?: ForecastExplanation | null;
}

export interface Investigation {
  InvestigationId: number;
  Query: string;
  InvestigationType: string;
  Status: string;
  CreatedBy: number;
  StartedAt: string;
  CompletedAt: string | null;
}

export interface InfrastructureRecommendation {
  RecommendationId: number;
  InvestigationId: number;
  ApplicationId: number | null;
  RecommendationType: string;
  /** "Cluster" or "Node". The API returns rows already ordered for display -
   *  each cluster immediately followed by the hosts recommended inside it. */
  CandidateEntityType: string;
  CandidateEntityId: number;
  /** Scoped to its own level: a Node's Rank is its position within its parent
   *  cluster, not within the investigation. */
  Rank: number;
  EligibilityStatus: string;
  OverallScore: number | null;
  EstimatedMonthlyCost: number | null;
  ProjectedCpuUtilization: number | null;
  ProjectedMemoryUtilization: number | null;
  ProjectedHeadroomPercent: number | null;
  Explanation: string | null;
  /** JSON blob of the full candidate. For Node rows it also carries
   *  parent_cluster_code / parent_cluster_rank / host_name. */
  EvidenceJson: string | null;
  Status: string;
  CreatedAt: string;
}

/** Fields the UI reads out of InfrastructureRecommendation.EvidenceJson. */
export interface RecommendationEvidence {
  cluster_code?: string;
  host_name?: string;
  parent_cluster_code?: string;
  parent_cluster_rank?: number;
  reliability_score?: number;
}

/** total / used / free for one resource, as computed by the capacity engine.
 *  `free` is the engine's own `available_*` figure, not `total - used`
 *  recomputed for display - re-deriving it here would diverge the moment
 *  reservation handling changed. */
export interface ResourceCapacity {
  total: number | null;
  used: number | null;
  free: number | null;
  used_percent: number | null;
}

export interface CapacityView {
  cpu_cores: ResourceCapacity;
  memory_gb: ResourceCapacity;
  storage_gb: ResourceCapacity;
}

export interface ReviewHost {
  host_name: string;
  node_id: number;
  overall_score: number | null;
  projected_headroom_percent: number | null;
  capacity: CapacityView | null;
}

export interface ReviewOption {
  cluster_code: string;
  cluster_id: number;
  /** Which site. A shortlist of three clusters is a choice about DATA CENTRE as
   *  much as capacity, and the payload used to carry only the cluster code -
   *  so the reviewer was picking a site from a name. Null when the candidate
   *  predates the field rather than when the site is unknown. */
  data_center: string | null;
  eligibility_status: string;
  overall_score: number | null;
  projected_headroom_percent: number | null;
  capacity: CapacityView | null;
  hosts: ReviewHost[];
}

export interface RunInvestigationResult {
  investigation_id: number;
  status: "AwaitingReview" | "Completed";
  investigation_type?: string;
  confidence?: string;
  /** The chat this answer belongs to. The server generates it on the first
   *  message and every later message sends it back, which is what gives a
   *  follow-up like "give me the options again" something to refer to. */
  conversation_id?: string | null;
  /** Set when this answer is an earlier investigation shown again rather than
   *  a new one - there is no second Investigation row behind it. */
  recall_of_investigation_id?: number;
  /** Set instead of final_report when the reviewer REJECTED a placement.
   *  Rejecting used to produce an executive summary of the thing just declined;
   *  it now asks what was wrong and offers constraints derived from that
   *  candidate's own figures. */
  rejection_prompt?: {
    rejected_cluster?: string | null;
    question: string;
    options: { id: string; label: string; constraint: Record<string, unknown> }[];
  } | null;
  review_payload?: {
    /** The richer form: one entry per shortlisted cluster, each with its
     *  capacity figures and the hosts ranked inside it. */
    options?: ReviewOption[];
    top_candidates: string[];
    top_hosts_by_cluster?: Record<string, string[]>;
    /** Per-cluster eligibility, so an empty host list can be explained as
     *  "the cluster was rejected" rather than "the cluster has no usable
     *  hosts" - hosts are only ranked inside eligible clusters. */
    cluster_eligibility?: Record<string, string>;
    message: string;
    /** What to offer when the shortlist is not good enough - see
     *  app/services/refinement.py. `sufficient` is true on a normal result and
     *  the UI shows none of this: the reader picks one and leaves. */
    next_steps?: {
      eligible_total: number;
      shown: number;
      more_available: number;
      sufficient: boolean;
      choices: {
        action: "show_more" | "refine_requirement" | "change_constraints";
        label: string;
        detail: string;
      }[];
    };
    /** Present only when this run excluded a data center - an ordinary
     *  first ask has nothing to report here. See
     *  app/services/refinement.py::data_center_choice: grouped from this
     *  run's own eligible candidates, never a fresh query, so it reflects
     *  exactly what the shortlist above already shows. On this estate a
     *  Tier-1 workload typically spans two data centers, so
     *  has_genuine_alternative:false after one exclusion is the common
     *  case, not a rare one - render that as a plain statement, not an
     *  empty picker. */
    data_center_choice?: {
      excluded_data_centers: string[];
      available_data_centers: { data_center: string; eligible_count: number }[];
      has_genuine_alternative: boolean;
    } | null;
  };
  final_report?: {
    title: string;
    executive_summary: string;
    top_recommendation: string | null;
    risks: string[];
    next_steps: string[];
    human_action_required: string;
  };
}

/** One model role and the model currently serving it.
 *  `source` is "config" (from the deployed settings) or "override" (chosen on
 *  the admin screen). The distinction is what the Reset control acts on. */
/** What answers for a role when its primary provider fails. Sent alongside
 *  the role rather than as a separate list - see app/api/routes_admin.py's
 *  `_fallback_for`. `configured: false` means nobody has chosen one; the
 *  provider/model are then null rather than a guessed default, because a
 *  fallback nobody selected is a model nobody evaluated. */
export interface ModelRoleFallback {
  /** The role name this fallback is stored under - "<role>.fallback" -
   *  which is also what PUT/DELETE /admin/model-roles/{role} expects. */
  role: string;
  provider: string | null;
  model: string | null;
  configured: boolean;
}

export interface ModelRole {
  name: string;
  title: string;
  description: string;
  chains: string[];
  provider: string;
  model: string;
  source: "config" | "override";
  updated_by: string | null;
  updated_at: string | null;
  fallback: ModelRoleFallback;
}

/** What a provider will serve right now. `available: false` carries the reason
 *  rather than showing an empty dropdown the reader cannot explain. */
export interface ModelProvider {
  provider: string;
  available: boolean;
  models: string[];
  error: string | null;
}

// ---- CMDB Insighter --------------------------------------------------------

/** A generic result grid - column headers are dimension names or "count",
 *  rows are plain values in the same order. Deliberately untyped beyond that:
 *  the columns depend on what the question asked to group by, which is open
 *  ended by design (see app.insights.whitelist). */
export interface InsightTable {
  title: string | null;
  columns: string[];
  rows: (string | number | null)[][];
}

/** One answer from the CMDB Insighter. `intent` says which path answered it -
 *  "health", "impact" and "business_service_leader" are Python-composed and
 *  need no model call to read; "aggregate" is the narrated SQL-backed path
 *  and carries `total_count`, which the other three do not. */
export interface InsightAnswer {
  intent: "health" | "impact" | "business_service_leader" | "aggregate";
  headline: string;
  narrative: string;
  insight?: string;
  /** What this answer does NOT cover - a filter that narrowed scope, or a
   *  count that is a floor rather than an exact number. Always state these
   *  next to the headline, never let a reader assume completeness. */
  caveats: string[];
  table: InsightTable | null;
  /** Findings that are real but should not compete with the headline for
   *  attention - shown behind a disclosure, same pattern as
   *  RecommendationComparison's "other options considered". */
  details?: Record<string, unknown>;
  filters_applied?: Record<string, unknown>;
  row_count?: number;
  total_count?: number;
}

/** One model's behaviour, graded from calls it already made.
 *
 *  Rates always travel with their denominator. "100% entity fidelity" over three
 *  mentions is not the same claim as over four hundred, and a scorecard that
 *  hides which one it is invites the wrong conclusion. */
export interface EvaluationProperty {
  rate: number | null;
  observations: number;
}

export interface EvaluationModel {
  model: string;
  calls: number;
  generated: number;
  /** Counted but never graded - a cached answer is the same text served again,
   *  and grading it each time turns one success into twenty. */
  cached: number;
  failures: number;
  /** Prompt was capped, so fidelity is not measurable. Reported rather than
   *  dropped: a rate over an unstated subset is worse than none. */
  ungradeable: number;
  latency_p50_ms: number | null;
  latency_p95_ms: number | null;
  properties: Record<string, EvaluationProperty>;
  by_schema: Record<string, Record<string, EvaluationProperty>>;
  flagged_count: number;
}

export interface EvaluationResult {
  calls_seen: number;
  models: EvaluationModel[];
  flagged: { audit_id: number; schema: string; property: string; ungrounded: string[] }[];
}

/** Answer quality, at the three levels it is measured. None is derived from the
 *  level below's RATE - each sums grounded and total over its own calls, because
 *  averaging rates lets a one-line reply weigh as much as a full report. */
export interface GraderScore {
  grader: string;
  grounded: number;
  total: number;
  calls?: number;
  /** null when nothing was measurable. Zero is a score; "nothing to score" is not. */
  rate: number | null;
  mixed_grader_versions?: boolean;
}

export interface ConversationTurnScore {
  turn_id: number;
  asked: string | null;
  answered: string | null;
  investigation_id: number | null;
  at: string | null;
  scores: GraderScore[];
}

export interface ConversationDetail {
  conversation_id: string;
  session: GraderScore[];
  turns: ConversationTurnScore[];
  note: string;
}

export interface ConversationSummary {
  conversation_id: string;
  started_at: string | null;
  last_activity_at: string | null;
  turns: number;
  number_fidelity: number | null;
  figures_checked: number;
}

export interface TranscriptGrade {
  grader: string;
  grounded: number;
  total: number;
  rate: number | null;
  ungrounded: string | null;
  grader_version: string;
}

export interface TranscriptCall {
  audit_id: number;
  graph_node: string | null;
  schema: string;
  model: string | null;
  provider: string | null;
  started_at: string | null;
  completed_at: string | null;
  success: boolean | null;
  prompt: string | null;
  output: string | null;
  grades: TranscriptGrade[];
}

export interface InvestigationTranscript {
  investigation_id: number;
  calls: TranscriptCall[];
  note: string;
}
