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

export interface HostingRecommendationResponse {
  application: Record<string, unknown>;
  requirement: Record<string, unknown>;
  candidates: CandidateScore[];
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

export interface ClusterForecast {
  cluster_id: number;
  cluster_code: string;
  horizon_days: number;
  cpu: ResourceForecast;
  memory: ResourceForecast;
  storage: ResourceForecast;
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
