"""Incidents, changes and problems - with words in them.

WHY THIS EXISTS
---------------
sad.Incident had nine columns and no free text. The indexed document for an
incident read:

    "Sev2 incident on cluster atl-03, opened 2026-04-11, status Resolved,
     root cause category Capacity."

That is a sentence generated *about* a record, not the record. Hybrid retrieval
was built on top of it: the BM25 tokeniser has a dedicated pattern for ITSM
record numbers, and the corpus contained zero such tokens. The sparse half had
nothing to match because the estate had nothing to say.

Everything here exists to give retrieval something real to work on - and to make
the ranking engine's historical and change-risk dimensions carry evidence rather
than noise.

THREE RULES THE GENERATED DATA FOLLOWS
--------------------------------------
1. **Incident density follows cluster stress.** A cluster packed to 90% gets
   many incidents, mostly capacity-shaped; one at 20% gets almost none. Random
   incidents would make the historical sub-score decoration - a reviewer asking
   "why does this cluster look worse" would find no answer in the data.

2. **Change failures follow the same stress.** Changes fail where the
   infrastructure is already under pressure. That makes change risk a second,
   independent-looking signal that in fact points at the same clusters, which is
   what makes a recommendation defensible rather than merely computed.

3. **Major events deliberately break both rules.** Five to six groupings of
   roughly a hundred incidents inside a few days, spanning clusters regardless
   of how loaded they are - an attack, a breach, a firmware defect, an expiry.
   These are the retrieval test that matters: they can only be found by *time
   and language*, not by which cluster happens to be busy, so they are the case
   where a working index and a broken one give visibly different answers.

NOISE IS GENERATED ON PURPOSE
-----------------------------
Roughly a third of work notes are routine boilerplate - "Assigned to Network
team", "Monitoring", "No further updates". They are here because real ticket
systems are full of them and the chunking strategy exists to filter them: a
noise filter that has never seen noise is untested. Embedding "Assigned to
Network team" four thousand times would poison the vector neighbourhood around
every query, which is precisely the failure the filter prevents.
"""

from __future__ import annotations

from datetime import datetime, timedelta

# =============================================================================
# Record numbering - the tokens BM25 exists to find
# =============================================================================
INCIDENT_NUMBER_BASE = 1_000_000   # INC1000001
CHANGE_NUMBER_BASE = 30_000        # CHG0030001
PROBLEM_NUMBER_BASE = 40_000       # PRB0040001

TARGET_INCIDENTS = 10_000
TARGET_CHANGES = 1_000
HISTORY_DAYS = 540                 # incidents reach back further than utilisation

ASSIGNMENT_GROUPS = [
    "L2 Platform Operations", "L3 Infrastructure Engineering", "Network Operations",
    "Storage Engineering", "Database Administration", "Security Operations",
    "Application Support", "Middleware Support", "Cloud Platform Team", "Service Desk",
]

# =============================================================================
# Incident text, keyed by root cause. Each entry supplies a short description
# template, a longer body, and the resolution - the three sections the chunking
# strategy splits a ticket into.
# =============================================================================
_INCIDENT_TEMPLATES = {
    "Capacity": [
        {
            "short": "Memory exhaustion on {node}",
            "desc": ("Sustained memory pressure on {node} in cluster {cluster}. Available memory fell below "
                     "4 GB and the kernel OOM killer terminated two container workloads. {app} reported "
                     "failed health checks for {mins} minutes. Cluster memory utilisation was {mem}% at the "
                     "time of the alert, against a configured threshold of 80%."),
            "close": ("Reclaimed memory by evicting non-critical workloads and raising the memory limit on "
                      "{app}. Longer term this cluster needs additional capacity - it has been above 85% "
                      "memory for {days} consecutive days."),
        },
        {
            "short": "CPU saturation on cluster {cluster}",
            "desc": ("Cluster {cluster} sustained CPU utilisation above 95% for {mins} minutes. Scheduler "
                     "latency increased and pod start times on {node} exceeded 90 seconds. {app} response "
                     "times degraded from a 120ms baseline to over 2 seconds at the 95th percentile."),
            "close": ("Load subsided after the batch window closed. No configuration change made. This is "
                      "the {nth} occurrence on {cluster} in 90 days and should be treated as a capacity "
                      "problem rather than an incident."),
        },
        {
            "short": "Storage threshold breached on {node}",
            "desc": ("Filesystem on {node} reached {sto}% of capacity. Write latency on the backing volume "
                     "increased and {app} began queuing writes. No data loss. The volume has grown "
                     "{growth}% in the last 30 days."),
            "close": "Expanded the volume and archived logs older than 30 days. Growth trend unchanged.",
        },
    ],
    "Hardware": [
        {
            "short": "Disk failure on {node}",
            "desc": ("Physical disk in slot {slot} on {node} reported a SMART predictive failure and was "
                     "taken offline by the RAID controller. The array is running degraded. No workload "
                     "impact observed, but a second failure in this array would cause data loss."),
            "close": "Disk replaced under vendor warranty. Array rebuild completed in {hrs} hours.",
        },
        {
            "short": "Correctable memory errors on {node}",
            "desc": ("{node} logged {count} correctable ECC errors on DIMM slot {slot} over {hrs} hours. "
                     "The rate is increasing and vendor guidance treats this as a precursor to "
                     "uncorrectable failure."),
            "close": "DIMM replaced during the scheduled maintenance window. Errors have not recurred.",
        },
        {
            "short": "Network interface flapping on {node}",
            "desc": ("The primary NIC on {node} flapped {count} times in {mins} minutes, each time failing "
                     "over to the secondary. {app} saw brief connection resets on each transition."),
            "close": "Replaced the transceiver and reseated the cable. Link has been stable since.",
        },
    ],
    "Network": [
        {
            "short": "Packet loss between {cluster} and {other}",
            "desc": ("Sustained packet loss of {pct}% observed on the inter-cluster link between {cluster} "
                     "and {other}. {app} depends on this path and reported timeouts on {count} requests. "
                     "Round-trip latency rose from 1.2ms to {ms}ms."),
            "close": "Traced to a failing line card in the aggregation switch. Traffic rerouted, card replaced.",
        },
        {
            "short": "DNS resolution failures affecting {app}",
            "desc": ("Internal DNS resolution intermittently failed for service names in the {cluster} "
                     "namespace. Approximately {pct}% of lookups timed out over {mins} minutes."),
            "close": "One of the three DNS replicas was serving stale records after a failed sync. Replica restarted.",
        },
    ],
    "Software": [
        {
            "short": "Memory leak in {app} on {cluster}",
            "desc": ("{app} memory usage grew steadily from 40% to {mem}% of its limit over {hrs} hours "
                     "with no corresponding increase in request volume. Heap analysis shows retained "
                     "objects in the connection pool."),
            "close": ("Rolled back to the previous release. The leak was introduced in the connection pool "
                      "change and is tracked separately as a problem record."),
        },
        {
            "short": "Thread pool exhaustion in {app}",
            "desc": ("{app} on {cluster} exhausted its worker thread pool during the {mins}-minute peak "
                     "window. Requests queued and {count} were rejected with 503."),
            "close": "Increased pool size and added a queue depth alert. Underlying slow query also tuned.",
        },
    ],
    "Configuration": [
        {
            "short": "Certificate expiry on {app}",
            "desc": ("The TLS certificate presented by {app} on {cluster} expired at {time}. Clients "
                     "rejected connections for {mins} minutes until the certificate was renewed."),
            "close": "Certificate renewed and automated rotation enabled. Expiry monitoring extended to 30 days.",
        },
        {
            "short": "Resource limit misconfiguration on {cluster}",
            "desc": ("A deployment to {cluster} set a CPU limit an order of magnitude below the request, "
                     "causing immediate throttling of {app}. The error was present in the manifest and "
                     "passed review."),
            "close": "Corrected the manifest and added an admission policy rejecting limit below request.",
        },
    ],
    "Dependency": [
        {
            "short": "{app} failing on upstream timeout",
            "desc": ("{app} on {cluster} reported {count} upstream timeouts calling its dependency. The "
                     "dependency is hosted in a different data centre and round-trip latency is {ms}ms, "
                     "above the {pct}ms threshold this call was designed for."),
            "close": ("Increased the client timeout as a stopgap. The real fix is locality - this dependency "
                      "should not cross data centres for a latency-sensitive path."),
        },
        {
            "short": "Database connection pool saturation for {app}",
            "desc": ("{app} exhausted its database connection pool. {count} requests waited longer than "
                     "5 seconds for a connection and {mins} minutes of elevated errors followed."),
            "close": "Pool size raised and a long-running report query moved to a read replica.",
        },
    ],
    "Unknown": [
        {
            "short": "Intermittent errors on {app}",
            "desc": ("{app} on {cluster} reported an elevated error rate of {pct}% for {mins} minutes. No "
                     "correlating deployment, infrastructure event or dependency alert was found."),
            "close": "Errors subsided without intervention. No root cause identified. Monitoring left in place.",
        },
    ],
}

# Work notes that carry real detail. These are what retrieval should find.
_SUBSTANTIVE_NOTES = [
    "Checked {node}: load average {load}, {mem}% memory used, {sto}% filesystem. Nothing unusual in dmesg.",
    "Confirmed the ballooning driver is disabled on this host. That rules out the hypervisor reclaiming memory.",
    "Correlated with the deployment at {time} - the error rate rises within four minutes of the rollout.",
    "Thread dump shows {count} threads blocked on the same lock in the connection pool.",
    "Compared against {other}, which runs the same version at half the load and shows none of this.",
    "The {pct}% figure is measured at the ingress, not the application - the application's own metrics show less.",
    "Rolled back {app} to the previous release on one node as a test. That node stopped erroring.",
    "Vendor case {vendor} opened. They have asked for a core dump and the controller log.",
    "This is the {nth} time this quarter on {cluster}. Raising a problem record rather than closing again.",
    "Capacity is the underlying issue: {cluster} has been above 85% for {days} days and has no headroom left.",
    "Failover to the standby completed in {mins} minutes, inside the {pct}-minute objective.",
    "Query plan changed after statistics were updated - the optimiser is now choosing a scan.",
]

# Boilerplate. Generated deliberately so the noise filter has something to filter.
_ROUTINE_NOTES = [
    "Assigned to {group}.",
    "Acknowledged.",
    "Monitoring.",
    "No further updates.",
    "Reassigned to {group} for investigation.",
    "Awaiting vendor response.",
    "Bridge call started.",
    "Bridge call ended.",
    "Updated the stakeholders.",
    "Closing - no recurrence in 24 hours.",
]

# =============================================================================
# Major events - the groupings that ignore the stress distribution entirely
# =============================================================================
MAJOR_EVENTS = [
    {
        "key": "ddos-2026-05",
        "title": "Coordinated volumetric attack on internet-facing services",
        "days_ago": 96, "duration_days": 3, "incident_count": 118, "severity_bias": "Sev1",
        "root_cause": "Network", "group": "Security Operations",
        "short": "Volumetric attack traffic saturating {cluster} ingress",
        "desc": ("Sustained inbound traffic of {gbps} Gbps against internet-facing services on {cluster}, "
                 "roughly {mult}x normal peak. Ingress saturated and legitimate requests to {app} were "
                 "dropped. Traffic originated from a distributed set of sources across multiple regions "
                 "and matched the pattern seen across the estate during this window."),
        "close": ("Upstream scrubbing engaged and rate limiting applied at the edge. Traffic returned to "
                  "baseline after {hrs} hours. Part of the coordinated attack tracked under this event."),
    },
    {
        "key": "cred-breach-2026-06",
        "title": "Credential compromise and forced rotation",
        "days_ago": 74, "duration_days": 5, "incident_count": 104, "severity_bias": "Sev2",
        "root_cause": "Configuration", "group": "Security Operations",
        "short": "Forced credential rotation on {app} following compromise",
        "desc": ("A service account credential was found exposed in an external repository. All accounts "
                 "sharing that pattern were rotated as a precaution, including the account {app} uses on "
                 "{cluster}. Applications that had cached the old credential failed authentication until "
                 "restarted. No evidence of unauthorised access to {cluster} itself."),
        "close": ("Credentials rotated and applications restarted. Secret scanning added to the pipeline. "
                  "Part of the estate-wide rotation tracked under this event."),
    },
    {
        "key": "storage-firmware-2026-04",
        "title": "Storage array firmware defect causing latency spikes",
        "days_ago": 128, "duration_days": 6, "incident_count": 96, "severity_bias": "Sev2",
        "root_cause": "Hardware", "group": "Storage Engineering",
        "short": "Storage latency spikes on {cluster} attributed to array firmware",
        "desc": ("Write latency on volumes backing {cluster} spiked from 2ms to over {ms}ms in bursts of "
                 "{mins} minutes, with no change in IO volume. {app} saw request timeouts during each "
                 "burst. The vendor has confirmed a firmware defect affecting this array model under "
                 "mixed read/write load."),
        "close": ("Firmware upgraded across affected arrays during emergency change windows. Latency "
                  "returned to baseline. Part of the fleet-wide firmware defect tracked under this event."),
    },
    {
        "key": "ca-expiry-2026-07",
        "title": "Intermediate certificate authority expiry",
        "days_ago": 41, "duration_days": 2, "incident_count": 112, "severity_bias": "Sev1",
        "root_cause": "Configuration", "group": "L3 Infrastructure Engineering",
        "short": "TLS failures on {app} after intermediate CA expiry",
        "desc": ("The intermediate certificate authority used to sign internal service certificates "
                 "expired at {time}. Every service presenting a certificate in that chain was rejected by "
                 "clients validating the full path, including {app} on {cluster}. Impact was immediate "
                 "and estate-wide rather than specific to this cluster."),
        "close": ("New intermediate issued and distributed, services restarted to pick up the chain. "
                  "Expiry monitoring extended to cover intermediates, not only leaf certificates. Part of "
                  "the estate-wide expiry tracked under this event."),
    },
    {
        "key": "hypervisor-cve-2026-03",
        "title": "Emergency hypervisor patching for a critical CVE",
        "days_ago": 158, "duration_days": 8, "incident_count": 88, "severity_bias": "Sev3",
        "root_cause": "Software", "group": "L3 Infrastructure Engineering",
        "short": "Workload disruption during emergency hypervisor patching of {cluster}",
        "desc": ("A critical hypervisor vulnerability required patching inside {hrs} hours across the "
                 "estate. Rolling reboots of hosts in {cluster} evacuated workloads node by node. {app} "
                 "experienced {count} brief interruptions as pods were rescheduled. Patching was "
                 "unavoidable and the disruption was accepted."),
        "close": ("All hosts patched. Workloads rebalanced afterwards. Part of the emergency patching "
                  "campaign tracked under this event."),
    },
    {
        "key": "core-network-2026-08",
        "title": "Core network fabric failure in one data centre",
        "days_ago": 19, "duration_days": 2, "incident_count": 101, "severity_bias": "Sev1",
        "root_cause": "Network", "group": "Network Operations",
        "short": "Loss of inter-cluster connectivity from {cluster}",
        "desc": ("A failure in the core network fabric removed inter-cluster connectivity for hosts in "
                 "this data centre for {mins} minutes. {app} on {cluster} lost access to its dependencies "
                 "and to shared storage. Workloads that could fail over to another data centre did so; "
                 "those pinned by data residency could not."),
        "close": ("Fabric restored after the failed spine was isolated. Reviewing why the redundant path "
                  "did not carry the traffic. Part of the data centre outage tracked under this event."),
    },
]


# =============================================================================
# Generation
# =============================================================================
def _fill(template, rng, *, cluster="", node="", app="", other="", group=""):
    """Substitute the placeholder vocabulary in a template.

    Every value comes from the seeded rng, so one seed produces one corpus. That
    matters because the golden set asserts against specific retrieved documents
    and would otherwise drift on every regeneration.
    """
    return template.format(
        cluster=cluster or "the cluster", node=node or "the host", app=app or "the application",
        other=other or "the peer cluster", group=group or rng.choice(ASSIGNMENT_GROUPS),
        mins=rng.choice([4, 7, 12, 18, 25, 40, 55, 90]),
        hrs=rng.choice([2, 3, 4, 6, 9, 14, 22]),
        days=rng.choice([9, 14, 21, 33, 47, 62]),
        mem=rng.choice([86, 89, 91, 93, 95, 97]),
        sto=rng.choice([82, 87, 90, 94, 96]),
        pct=rng.choice([3, 7, 12, 18, 24, 35, 48]),
        ms=rng.choice([28, 45, 76, 120, 185, 240]),
        count=rng.choice([12, 47, 133, 402, 1180, 3400]),
        slot=rng.choice(["A2", "B1", "B4", "C3", "D2"]),
        load=rng.choice(["4.2", "7.8", "12.1", "19.6"]),
        growth=rng.choice([8, 14, 22, 31]),
        nth=rng.choice(["second", "third", "fourth", "fifth"]),
        time="%02d:%02d" % (rng.randint(0, 23), rng.randint(0, 59)),
        vendor="CS%d" % rng.randint(100000, 999999),
        gbps=rng.choice([18, 34, 62, 110, 240]),
        mult=rng.choice([6, 11, 19, 30]),
    )


def _severity(rng, bias=None):
    if bias and rng.random() < 0.55:
        return bias
    return rng.choices(["Sev1", "Sev2", "Sev3", "Sev4"], weights=[6, 20, 46, 28])[0]


_IMPACT_URGENCY = {
    "Sev1": ("High", "High"), "Sev2": ("High", "Medium"),
    "Sev3": ("Medium", "Low"), "Sev4": ("Low", "Low"),
}


def _comments_for(opened, cluster, node, app, rng):
    """Six to twelve notes, roughly a third of them boilerplate.

    The boilerplate is deliberate. The chunking strategy filters short
    reassignment and acknowledgement notes rather than embedding them, and a
    filter that has never seen its input is untested. It is also honest: most of
    a real ticket's comment history says nothing.
    """
    notes = []
    at = opened
    for seq in range(1, rng.randint(6, 12) + 1):
        at = at + timedelta(minutes=rng.randint(3, 240))
        pool = _ROUTINE_NOTES if rng.random() < 0.35 else _SUBSTANTIVE_NOTES
        text = _fill(rng.choice(pool), rng, cluster=cluster, node=node, app=app)
        notes.append((seq, at, rng.choice(ASSIGNMENT_GROUPS),
                      "work_note" if rng.random() < 0.8 else "additional_comment", text))
    return notes


def build_changes(clusters, applications, load_plans, rng, anchor):
    """1,000 changes. Failure probability rises with cluster stress.

    That correlation is the point: change risk and incident history end up
    pointing at the same clusters, from independent-looking evidence, which is
    what makes "do not place this here" defensible rather than merely computed.
    """
    active = [c for c in clusters if c.lifecycle == "Active" and c.idx in load_plans]
    weights = [0.25 + load_plans[c.idx].stress for c in active]
    rows, comments, freezes = [], [], {}

    for i in range(1, TARGET_CHANGES + 1):
        cluster = rng.choices(active, weights=weights)[0]
        plan = load_plans[cluster.idx]
        s = plan.stress
        app = rng.choice(applications)
        ctype = rng.choices(["Normal", "Standard", "Emergency"], weights=[62, 28, 10])[0]

        # Roughly a quarter are still ahead of us - the upcoming changes that
        # make a freeze a placement consideration rather than a footnote.
        future = rng.random() < 0.28
        if future:
            start = anchor + timedelta(days=rng.randint(1, 45), hours=rng.randint(0, 23))
            state, close_code, a_start, a_end, close_notes = "Scheduled", None, None, None, None
        else:
            start = anchor - timedelta(days=rng.randint(1, 400), hours=rng.randint(0, 23))
            failed = rng.random() < (0.04 + 0.26 * s)
            close_code = (rng.choice(["Failed", "BackedOut"]) if failed
                          else rng.choices(["Successful", "SuccessfulWithIssues"], weights=[86, 14])[0])
            state, a_start = "Closed", start
            a_end = start + timedelta(hours=rng.randint(1, 6))
            close_notes = (
                "Rollback executed after validation failed on %s. Root cause under investigation."
                % cluster.code if close_code in ("Failed", "BackedOut")
                else "Validation passed on %s. No user impact observed." % cluster.code)

        summary = rng.choice([
            "Patch hypervisor hosts in %s" % cluster.code,
            "Expand storage volume for %s" % app.code,
            "Upgrade %s to the current release on %s" % (app.code, cluster.code),
            "Replace failing node hardware in %s" % cluster.code,
            "Apply network ACL change affecting %s" % cluster.code,
            "Increase memory limits for %s" % app.code,
            "Rotate certificates for %s" % app.code,
            "Rebalance workloads across %s" % cluster.code,
        ])
        risk = "High" if s > 0.6 else ("Medium" if s > 0.3 else "Low")
        # A freeze on a few heavily loaded clusters - what makes RULE-011 bite.
        freeze = None
        if future and s > 0.65 and rng.random() < 0.35:
            freeze = anchor + timedelta(days=rng.randint(3, 30))
            freezes[cluster.idx] = freeze

        rows.append((
            "CHG%07d" % (CHANGE_NUMBER_BASE + i), summary,
            ("%s. Target cluster %s in %s, currently at %.0f%% CPU allocation across %d hosted "
             "workloads. Affects %s." % (summary, cluster.code, cluster.datacenter,
                                         plan.achieved_cpu_pct, plan.app_count, app.code)),
            ctype, state, cluster.idx, None, app.idx, start,
            start + timedelta(hours=rng.randint(1, 8)), a_start, a_end, close_code, close_notes,
            ("1. Snapshot current state. 2. Apply to one node and validate. 3. Roll forward to the "
             "remaining nodes. 4. Confirm %s health checks pass." % app.code),
            ("Restore the previous configuration from snapshot and restart affected workloads on %s. "
             "Estimated rollback time 30 minutes." % cluster.code),
            ("%s risk. %s is at %.0f%% allocation with %s headroom for workload movement during the "
             "change." % (risk, cluster.code, plan.achieved_cpu_pct,
                          "little" if s > 0.6 else "adequate")),
            rng.choice(ASSIGNMENT_GROUPS), freeze,
        ))
        for seq, text in enumerate([
            "Change raised for %s." % cluster.code,
            _fill(rng.choice(_SUBSTANTIVE_NOTES), rng, cluster=cluster.code, app=app.code),
            "Backout tested in the lower environment." if rng.random() < 0.5 else "CAB approved.",
        ], start=1):
            comments.append((i, seq, start - timedelta(days=rng.randint(1, 10)),
                             rng.choice(ASSIGNMENT_GROUPS), "work_note", text))
    return rows, comments, freezes


def build_incidents(clusters, applications, nodes, load_plans, change_count, rng, anchor):
    """10,000 incidents: most weighted by cluster stress, the rest in events.

    The two populations are generated differently on purpose. Routine incidents
    follow the stress distribution, so a loaded cluster looks worse than an idle
    one and the historical sub-score carries evidence. Major-event incidents
    ignore that entirely - they span clusters by time, not by load, which is the
    only way to test whether retrieval can find something by *when it happened
    and what it says* rather than by which cluster is busy.
    """
    active = [c for c in clusters if c.lifecycle == "Active" and c.idx in load_plans]
    nodes_by_cluster = {}
    for n in nodes:
        nodes_by_cluster.setdefault(n.cluster.idx, []).append(n)
    apps = list(applications)

    incidents, comments = [], []
    event_incident_ids = {}

    def node_name(cluster_idx):
        pool = nodes_by_cluster.get(cluster_idx)
        return rng.choice(pool).host_name if pool else ""

    def emit(cluster, app, severity, opened, root_cause, short, desc, close, group, event_key=None):
        idx = len(incidents) + 1
        impact, urgency = _IMPACT_URGENCY[severity]
        closed = opened + timedelta(hours=rng.randint(1, 40)) if rng.random() < 0.93 else None
        status = "Closed" if closed else rng.choice(["Open", "InProgress"])
        incidents.append((
            app.idx if app else None, cluster.idx, None, severity, opened, closed, status,
            root_cause, "INC%07d" % (INCIDENT_NUMBER_BASE + idx), short, desc,
            close if closed else None, group, impact, urgency,
        ))
        for c in _comments_for(opened, cluster.code, node_name(cluster.idx),
                               app.code if app else "", rng):
            comments.append((idx,) + c)
        if event_key:
            event_incident_ids.setdefault(event_key, []).append(idx)
        return idx

    # ---- major events, first so their identifiers are low and memorable ----
    for event in MAJOR_EVENTS:
        start = anchor - timedelta(days=event["days_ago"])
        # Deliberately spread across clusters regardless of stress.
        for _ in range(event["incident_count"]):
            cluster = rng.choice(active)
            app = rng.choice(apps)
            opened = start + timedelta(
                days=rng.randint(0, event["duration_days"] - 1),
                hours=rng.randint(0, 23), minutes=rng.randint(0, 59))
            ctx = dict(cluster=cluster.code, node=node_name(cluster.idx), app=app.code)
            emit(cluster, app, _severity(rng, event["severity_bias"]), opened, event["root_cause"],
                 _fill(event["short"], rng, **ctx), _fill(event["desc"], rng, **ctx),
                 _fill(event["close"], rng, **ctx), event["group"], event["key"])

    # ---- routine incidents, weighted by stress ------------------------------
    weights = [0.15 + 2.2 * load_plans[c.idx].stress for c in active]
    while len(incidents) < TARGET_INCIDENTS:
        cluster = rng.choices(active, weights=weights)[0]
        s = load_plans[cluster.idx].stress
        app = rng.choice(apps)
        # A stressed cluster does not just have more incidents - it has
        # different ones. Capacity is the cause when there is no headroom.
        root_cause = (rng.choices(["Capacity", "Software", "Configuration", "Dependency", "Hardware", "Network"],
                                  weights=[45, 15, 14, 12, 8, 6])[0] if s > 0.55
                      else rng.choices(["Hardware", "Network", "Software", "Configuration", "Dependency", "Capacity", "Unknown"],
                                       weights=[20, 20, 20, 18, 12, 5, 5])[0])
        tpl = rng.choice(_INCIDENT_TEMPLATES[root_cause])
        opened = anchor - timedelta(days=rng.randint(1, HISTORY_DAYS),
                                    hours=rng.randint(0, 23), minutes=rng.randint(0, 59))
        ctx = dict(cluster=cluster.code, node=node_name(cluster.idx), app=app.code,
                   other=rng.choice(active).code)
        emit(cluster, app, _severity(rng), opened, root_cause,
             _fill(tpl["short"], rng, **ctx), _fill(tpl["desc"], rng, **ctx),
             _fill(tpl["close"], rng, **ctx), rng.choice(ASSIGNMENT_GROUPS))

    return incidents, comments, event_incident_ids


def build_problems(clusters, load_plans, event_incident_ids, change_count, rng, anchor):
    """One problem record per major event, plus recurring-capacity problems.

    A problem is what turns a pile of incidents into an explanation, and
    RootCause / Workaround / FixNotes are the highest-value text in the schema -
    they are written to answer "why did this keep happening", which is the
    question a capacity planner is actually asking.
    """
    rows = []
    for event in MAJOR_EVENTS:
        n = len(event_incident_ids.get(event["key"], []))
        opened = anchor - timedelta(days=event["days_ago"])
        rows.append((
            "PRB%07d" % (PROBLEM_NUMBER_BASE + len(rows) + 1),
            event["title"],
            ("%d incidents were raised across the estate between %s and %s and share a single cause. "
             "They are grouped under this problem record rather than investigated individually."
             % (n, opened.date(), (opened + timedelta(days=event["duration_days"])).date())),
            event["title"] + " - confirmed as the common cause across every linked incident.",
            "Mitigations applied per incident; see the linked records for the specific action taken.",
            "Permanent fix tracked as a change. Estate-wide detection added so a recurrence is caught earlier.",
            1 if rng.random() < 0.4 else 0, "Closed",
            rng.randint(1, change_count) if rng.random() < 0.6 else None,
            None, None, opened, opened + timedelta(days=event["duration_days"] + rng.randint(2, 20)),
        ))

    # Recurring capacity problems on the clusters that keep generating them.
    stressed = sorted((c for c in clusters if c.idx in load_plans),
                      key=lambda c: -load_plans[c.idx].stress)[:24]
    for cluster in stressed:
        plan = load_plans[cluster.idx]
        opened = anchor - timedelta(days=rng.randint(60, 400))
        rows.append((
            "PRB%07d" % (PROBLEM_NUMBER_BASE + len(rows) + 1),
            "Recurring capacity incidents on %s" % cluster.code,
            ("%s has generated repeated capacity incidents. It is allocated to %.0f%% CPU and %.0f%% "
             "memory across %d hosted workloads, leaving no headroom to absorb normal variation."
             % (cluster.code, plan.achieved_cpu_pct, plan.achieved_mem_pct, plan.app_count)),
            ("The cluster is over-committed. Each individual incident has a proximate trigger, but the "
             "reason they keep happening is that there is no spare capacity to absorb any of them."),
            "Non-critical workloads shed during peak windows. This is mitigation, not a fix.",
            "Permanent fix is additional capacity or migrating workloads to a cluster with headroom.",
            1, rng.choice(["RootCauseAnalysis", "FixInProgress", "Assess"]),
            None, cluster.idx, None, opened, None,
        ))
    return rows
