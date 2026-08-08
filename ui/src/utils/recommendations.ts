import type { InfrastructureRecommendation, RecommendationEvidence } from "@/types";

/** Node rows carry their host name and parent cluster in EvidenceJson - the
 *  recommendation table itself only stores an entity id, so without this a
 *  reviewer would be looking at "Node #151" with no idea which host that is. */
export function evidenceOf(r: InfrastructureRecommendation): RecommendationEvidence {
  if (!r.EvidenceJson) return {};
  try {
    return JSON.parse(r.EvidenceJson) as RecommendationEvidence;
  } catch {
    return {};
  }
}

/** Human-readable label for one recommendation row: a cluster code, or a host
 *  name qualified by the cluster it sits in. */
export function describeCandidate(r: InfrastructureRecommendation): string {
  const evidence = evidenceOf(r);
  if (r.CandidateEntityType === "Node") {
    const host = evidence.host_name ?? `Node #${r.CandidateEntityId}`;
    return evidence.parent_cluster_code ? `${host} (in ${evidence.parent_cluster_code})` : host;
  }
  return evidence.cluster_code ?? `${r.CandidateEntityType} #${r.CandidateEntityId}`;
}

export function isNodeRow(r: InfrastructureRecommendation): boolean {
  return r.CandidateEntityType === "Node";
}
