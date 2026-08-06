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

export interface ProjectedUtilization {
  projected_cpu_utilization_percent: number;
  projected_memory_utilization_percent: number;
  projected_storage_utilization_percent: number;
  projected_headroom_percent: number;
  fits_all: boolean;
}

export interface CandidateScore {
  cluster_id: number;
  cluster_code: string;
  eligibility_status: "Eligible" | "Rejected";
  rule_results: RuleResult[];
  subscores: SubScores | null;
  overall_score: number | null;
  estimated_monthly_cost: number | null;
  projected: ProjectedUtilization | null;
  rank: number | null;
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
  CandidateEntityType: string;
  CandidateEntityId: number;
  Rank: number;
  EligibilityStatus: string;
  OverallScore: number | null;
  EstimatedMonthlyCost: number | null;
  ProjectedCpuUtilization: number | null;
  ProjectedMemoryUtilization: number | null;
  ProjectedHeadroomPercent: number | null;
  Explanation: string | null;
  Status: string;
  CreatedAt: string;
}

export interface RunInvestigationResult {
  investigation_id: number;
  status: "AwaitingReview" | "Completed";
  investigation_type?: string;
  confidence?: string;
  review_payload?: {
    top_candidates: string[];
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
