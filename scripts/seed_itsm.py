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
#
# WHY THESE ARE PARAGRAPHS AND NOT SENTENCES
# ------------------------------------------
# The first version was twelve one-line templates shared by every incident
# regardless of cause. Measured against the generated corpus that produced work
# notes averaging 71 characters, maximum 112 - and the contextual prefix that
# documents.py prepends to every chunk is about 60 characters. Half of each
# embedded vector was header.
#
# seekanddestroy-2d measured the consequence on real chunks (47% and 52% prefix)
# and found something worse than the ratio: the prefix carries the incident
# number, so the golden set's exact-identifier case matched every chunk of a
# ticket through BM25 no matter what the note said. That case was testing that we
# print the id into the text, not that retrieval works. Sparse won it by
# construction and hybrid inherited the win.
#
# Three rules follow, each load-bearing:
#
#   1. A note NEVER contains its own incident number. Real notes do not - you are
#      already inside the ticket. Leaving it out is what makes the identifier case
#      measure retrieval instead of string-printing. The prefix still carries it.
#   2. Notes DO reference other tickets. That is realistic, and it is legitimate
#      work for BM25 that dense retrieval cannot do - the honest version of the
#      win the prefix was faking.
#   3. Notes are keyed by root cause. The risk is not length, it is homogeneity:
#      if a Network note and a Capacity note are structurally identical with
#      different numbers, dense retrieval cannot separate them and recall@k
#      measures nothing. Each pool uses the symptoms, tools and subsystems of its
#      own cause, and introduces vocabulary that does not appear in the ticket
#      header - otherwise the note chunk adds nothing over the header chunk and
#      multi-chunk retrieval is pure cost.
_NOTES_BY_CAUSE = {
    "Capacity": [
        "Pulled the last 30 days from the capacity series for {cluster}. CPU has been above 85% for {days} consecutive days and the p99 scheduling delay on {node} is now {ms}ms, up from single digits in January. This is not a spike, it is the trend finally arriving. There is no headroom left to absorb the nightly batch window.",
        "Ran the numbers on {node}: {mem}% memory committed, {sto}% filesystem, load average {load} against 16 vCPU. The balloon driver is disabled so the hypervisor is not reclaiming. Every guest on this host is sized for peak and running at peak simultaneously, which is the actual problem.",
        "Checked whether this is real growth or a leak. RSS for the {app} workers climbs across the day and returns to baseline on restart, but the floor has risen {growth}% month over month, which is growth rather than a leak. Recommend sizing the next review against the floor, not the average.",
        "Compared {cluster} against {other}, which runs the same {app} release at roughly half the allocation and shows none of this. The difference is density, not version. Same signature as {other_inc} on the peer cluster last quarter.",
        "Filesystem on {node} hit {sto}% during the retention job. Reclaimed {pct}GB by expiring old archives, which buys about {days} days. Filing this as a workaround, not a fix - the growth rate has not changed and it will return.",
        "Batch window overran by {mins} minutes again. The window is sized for a cluster at 60% and {cluster} is running well above that, so every job queues behind the one before it. This is capacity presenting as a scheduling problem.",
    ],
    "Network": [
        "Packet capture on {node} shows retransmits climbing to {pct}% on the storage VLAN during the incident window. The application timeouts are downstream of that, not the cause. Handing to the network team with the capture attached.",
        "Traced the path from {node} to the gateway: {ms}ms added at the second hop, consistently, in both directions. The switch on that hop was reloaded under {other_chg} nine days ago. Asking for the config diff before and after.",
        "Confirmed this is not DNS. Resolution is answering in under 3ms from cache and the failures continue with hosts files pinned. Ruling it out here so nobody spends another bridge call on it.",
        "Ingress is seeing {gbps}Gbps against a normal baseline of a tenth of that, sourced from a wide spread of addresses with no single offender. Rate limiting is holding but the connection table on the edge is close to full.",
        "MTU mismatch on the {app} path. Large frames fragment between the host and the load balancer, which is why only the bulk transfers fail and the health checks stay green. Same shape as {other_inc}.",
        "Failover to the standby completed in {mins} minutes, inside the objective, but the return path did not converge for another {hrs} hours and clients held stale connections through it. The failover worked, the failback is the gap.",
    ],
    "Software": [
        "Thread dump from {node} shows {count} threads blocked on the same connection pool monitor. The pool is sized at 50 and the workload asks for more than that under load, so requests queue and the queue looks like slowness. Not a database problem.",
        "Correlated the error rate against the rollout at {time}: it rises within four minutes of the deployment reaching the third node and does not recover. Rolled back {app} on one node as a test and that node stopped erroring immediately.",
        "Heap dump taken. The retained set is dominated by cached response objects with no eviction policy, which explains why the collector runs more often but reclaims less. This is a regression from the change tracked under {other_chg}.",
        "The stack points at a null dereference in the retry handler when the downstream returns a 503 with an empty body. Reproduced it in staging by returning an empty 503 deliberately, so this is not environmental.",
        "Query plan changed after statistics were refreshed - the optimiser has switched from a seek to a scan on the largest table and the {ms}ms latency follows exactly. Plan hash attached. Same regression as {other_inc}.",
        "Log shows the same exception {count} times in {mins} minutes, all from one worker. The other workers are clean, which points at state on that instance rather than the release itself.",
    ],
    "Configuration": [
        "Diffed the running configuration on {node} against the two peers in {cluster}. The connection timeout is 30 seconds here and 5 everywhere else, so this host holds sockets long enough to exhaust the pool under load that the others absorb.",
        "The change under {other_chg} updated the template but the running hosts were never restarted, so half of {cluster} is on the new configuration and half is on the old. That is why it reproduces on some requests and not others.",
        "Found the ballooning driver enabled on this host and disabled on the rest of the cluster. That alone accounts for the memory the guest thinks it has lost. Correcting it and monitoring rather than closing.",
        "TLS on the {app} listener is still pinned to the retired cipher suite after the platform upgrade. Modern clients negotiate down and older ones fail outright, which matches the pattern of who is complaining.",
        "The {pct}% figure in the alert is measured at the ingress, not in the application - the application's own metrics show materially less. The threshold was set against the wrong series when the alert was written.",
        "Feature flag for the new path is enabled in this environment and disabled in the peer, which was not intentional. Same divergence as {other_inc}, which suggests the promotion step does not carry flags.",
    ],
    "Hardware": [
        "Controller log on {node} shows media errors on the disk in slot {slot}, {count} of them since the host was last booted, with no corresponding filesystem errors yet. Predictive failure rather than failure. Vendor case {vendor} opened.",
        "Memory test flagged correctable ECC errors on one DIMM at a rate of roughly {count} an hour. The host is stable but this is the shape that precedes an uncorrectable event. Requesting a window to replace it.",
        "The host lost a power supply at {time}. It is running on the remaining unit and is not redundant until that is replaced, so {cluster} is one fault away from losing this node entirely.",
        "Firmware on the storage controller is two releases behind the fleet baseline. The release notes name exactly this timeout signature as fixed. Vendor case {vendor} opened with the controller log attached.",
        "Disk latency on {node} is {ms}ms at the device layer, not the filesystem, which rules out the application and the volume manager. Same signature as {other_inc} on a host of the same generation.",
        "Replaced the failed unit and the array is rebuilding. Rebuild is degrading throughput by roughly {pct}% and will take about {hrs} hours, so expect the latency complaints to continue until it completes.",
    ],
    "Dependency": [
        "The upstream {app} service is returning 503 for roughly {pct}% of calls and our retry policy turns each one into three, which is why our error rate is a multiple of theirs. We are amplifying their incident, not having our own.",
        "Confirmed the dependency chain: our failures follow theirs by about {mins} minutes, which is our circuit breaker window. Their incident is tracked under {other_inc}. Holding here rather than investigating our own stack.",
        "The shared authentication service is slow rather than down, at {ms}ms against a normal baseline in the tens. Every request in our path waits on it, so we present as slow across every endpoint including the ones that do nothing.",
        "Message queue depth on the shared broker is {count} and climbing. Consumers are healthy, the producers are simply outpacing them since the change under {other_chg} raised the publish rate.",
        "Certificate for the downstream expired at {time}. Our calls fail closed, which is correct, but the error surfaces to users as our outage. Raising with their team.",
        "Database connections are being refused because the shared instance is at its connection limit, and we are one of six consumers. Our pool is behaving; the ceiling is shared and someone else took it.",
    ],
    "Unknown": [
        "Nothing conclusive yet. Metrics on {node} are unremarkable through the window, logs show no errors, and the only correlation is timing. Leaving it open rather than closing it as no-fault-found.",
        "Could not reproduce. The symptoms stopped before we attached and have not returned in {hrs} hours. Documenting what we checked so the next occurrence starts further along than this one did.",
        "Three plausible explanations and no evidence separating them: the deployment at {time}, the network maintenance under {other_chg}, or genuine load. Recording all three rather than picking one.",
        "This is the {nth} occurrence this quarter on {cluster} with the same shape and no root cause identified. Raising a problem record rather than closing again.",
    ],
}

#: Every substantive note, cause-agnostic. Changes have no RootCauseCategory, so
#: they draw from the union rather than from one cause's pool.
_ALL_CAUSE_NOTES = [n for pool in _NOTES_BY_CAUSE.values() for n in pool]

# One note in roughly twenty-five is a pasted log excerpt, deliberately long
# enough to exceed _MAX_CHUNK_CHARS in documents.py. Before this the longest text
# anywhere in the corpus was 344 characters, so the chunk splitter and its
# overlap handling had never executed once - untested code sitting in the
# retrieval path. These make it run.
_LONG_NOTES = [
    "Pasting the relevant section of the log from {node} for the record. The pattern repeats every few seconds through the whole window. The sequence begins with the pool reporting saturation, then the health check timing out, then the supervisor restarting the worker, then the cycle beginning again on the new process. Note that the restart resets the counter, which is why the dashboards show a sawtooth rather than a climb, and why this looked like recovery for the first hour. Timings are from the host clock, which is in sync with the fleet to within a few milliseconds. connection pool exhausted, active {count}, idle 0, waiting {count}. health probe exceeded {ms}ms budget, marking instance unhealthy. supervisor received unhealthy verdict, initiating restart, grace period {mins}s. worker terminated after grace period expired, in-flight requests dropped. new worker starting, warm cache empty, first request latency will be elevated. connection pool exhausted, active {count}, idle 0, waiting {count}. The same three lines then repeat until the load subsides at the end of the window. Filing the full log with the vendor under case {vendor}.",
    "Full output from the diagnostic run on {node}, attached here so it survives the ticket. Filesystem utilisation {sto}%, inodes at {pct}%, memory committed {mem}% with the balloon driver confirmed disabled, load average {load} sustained across all three intervals rather than spiking. Device queue depth is consistently above the point where latency stops being linear, and the service time at the device layer is {ms}ms, which is where the application latency is coming from. Nothing in dmesg, no ECC events, no media errors in the controller log, and the array is not rebuilding. Ruling out hardware on that basis. The workload profile changed when {app} was scaled out under {other_chg} and the storage was never resized to match, so this reads as capacity presenting as a storage fault. Recommend treating the resize as the fix and this ticket as the evidence, and cross-referencing {other_inc}, which showed the same shape on the peer cluster before it was resized.",
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
def _fill(template, rng, *, cluster="", node="", app="", other="", group="", exclude_inc=None):
    """Substitute the placeholder vocabulary in a template.

    Every value comes from the seeded rng, so one seed produces one corpus. That
    matters because the golden set asserts against specific retrieved documents
    and would otherwise drift on every regeneration.

    ``exclude_inc`` is the number of the incident this note belongs to. Cross
    references are drawn to point at OTHER tickets and never at the ticket the
    note sits in - a note repeating its own number puts that number in the
    embedded text of every chunk, which is the artefact that made the golden
    set's identifier case unfalsifiable. The prefix carries the own-number; the
    body carries references to elsewhere.
    """
    other_inc = INCIDENT_NUMBER_BASE + rng.randint(1, TARGET_INCIDENTS)
    if exclude_inc is not None and other_inc == exclude_inc:
        # Re-draw once. Collision is a 1-in-10,000 event, so a single retry is
        # enough and a loop would only add a way to hang.
        other_inc = INCIDENT_NUMBER_BASE + ((other_inc - INCIDENT_NUMBER_BASE) % TARGET_INCIDENTS) + 1
    return template.format(
        other_inc="INC%07d" % other_inc,
        other_chg="CHG%07d" % (CHANGE_NUMBER_BASE + rng.randint(1, TARGET_CHANGES)),
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


def _comments_for(opened, cluster, node, app, rng, root_cause="Unknown", own_number=None):
    """Six to twelve notes, roughly a third of them boilerplate.

    The boilerplate is deliberate. The chunking strategy filters short
    reassignment and acknowledgement notes rather than embedding them, and a
    filter that has never seen its input is untested. It is also honest: most of
    a real ticket's comment history says nothing.

    The substantive notes are drawn from the pool for this incident's root cause,
    not from one shared list. A Network note and a Capacity note that are
    structurally identical with different numbers give dense retrieval nothing to
    separate them by, and recall@k over such a corpus measures the template, not
    the retriever.
    """
    notes = []
    at = opened
    pool = _NOTES_BY_CAUSE.get(root_cause) or _NOTES_BY_CAUSE["Unknown"]
    for seq in range(1, rng.randint(6, 12) + 1):
        at = at + timedelta(minutes=rng.randint(3, 240))
        if rng.random() < 0.35:
            template = rng.choice(_ROUTINE_NOTES)
        elif rng.random() < 0.04:
            template = rng.choice(_LONG_NOTES)
        else:
            template = rng.choice(pool)
            # Two observations in one note, most of the time.
            #
            # One templated sentence averages ~245 characters, which against the
            # ~60-character contextual prefix leaves the prefix at 23% of the
            # embedded chunk - better than the 50% it was, still well above the
            # 15% that makes the prefix contextual rather than dominant.
            #
            # Pairing rather than writing longer templates because it is also
            # more truthful: a real work note records an observation and then what
            # the author concluded from it, and pairing two entries from the same
            # cause pool produces exactly that shape without doubling the number
            # of templates to maintain.
            if rng.random() < 0.6 and len(pool) > 1:
                second = rng.choice([t for t in pool if t != template])
                template = template + " " + second
        text = _fill(template, rng, cluster=cluster, node=node, app=app,
                     exclude_inc=own_number)
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
            _fill(rng.choice(_ALL_CAUSE_NOTES), rng, cluster=cluster.code, app=app.code),
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
        # ProblemId and CausedByChangeId are appended as None here and filled in
        # by link_incidents() once the problems exist. They cannot be set now:
        # a problem is defined by the incidents it explains, so the incidents
        # have to exist first, and the identity of a problem row is its position
        # in the insert order.
        incidents.append([
            app.idx if app else None, cluster.idx, None, severity, opened, closed, status,
            root_cause, "INC%07d" % (INCIDENT_NUMBER_BASE + idx), short, desc,
            close if closed else None, group, impact, urgency, None, None,
        ])
        for c in _comments_for(opened, cluster.code, node_name(cluster.idx),
                               app.code if app else "", rng, root_cause,
                               INCIDENT_NUMBER_BASE + idx):
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


def link_incidents(incidents, problem_rows, event_incident_ids, changes, rng):
    """Fill ProblemId and CausedByChangeId, turning the pile into a chain.

    Without this the schema has the columns and the data has none of the
    relationships, so "has this happened before" and "what caused this" cannot
    be answered however good retrieval is - which is the whole reason
    retrieval-design.md calls the links the point.

    Three kinds of link, and each is defensible rather than decorative:

    * Every incident belonging to a major event points at that event's problem
      record. That is what makes 100 tickets in three days one story.
    * Incidents on a cluster with a recurring-capacity problem point at it, but
      only the capacity-caused ones - a disk failure on a busy cluster is not
      evidence of over-commitment, and linking it would be the kind of tidy
      falsehood that makes an explanation untrustworthy.
    * A minority of incidents point at a change that FAILED on the same cluster
      shortly before they opened. Most incidents are not caused by a change and
      saying so with NULL is more honest than inventing a culprit.
    """
    problem_by_event = {}
    for i, row in enumerate(problem_rows, start=1):
        for event in MAJOR_EVENTS:
            if row[1] == event["title"]:
                problem_by_event[event["key"]] = i
    # Recurring-capacity problems carry their ClusterId in position 9.
    problem_by_cluster = {row[9]: i for i, row in enumerate(problem_rows, start=1) if row[9]}

    linked_problem = linked_change = 0

    for key, ids in event_incident_ids.items():
        pid = problem_by_event.get(key)
        if not pid:
            continue
        for incident_idx in ids:
            incidents[incident_idx - 1][15] = pid
            linked_problem += 1

    # Failed changes, indexed by cluster and completion time, so an incident can
    # be attributed to one that actually preceded it on the same infrastructure.
    failures_by_cluster = {}
    for i, c in enumerate(changes, start=1):
        if c[12] in ("Failed", "BackedOut") and c[11] is not None:
            failures_by_cluster.setdefault(c[5], []).append((c[11], i))

    for pos, row in enumerate(incidents, start=1):
        cluster_idx, opened, root_cause = row[1], row[4], row[7]
        if row[15] is None and root_cause == "Capacity" and cluster_idx in problem_by_cluster:
            row[15] = problem_by_cluster[cluster_idx]
            linked_problem += 1
        if row[16] is None:
            # A change that ended in the 10 days before the incident opened.
            # 72 hours was the first attempt and produced 45 links out of
            # 10,000 - 0.45%, against the 10-30% a real estate runs at. A bad
            # change frequently surfaces days later: a memory leak needs to
            # accumulate, a config error waits for the next batch window.
            # Ten days is still short enough that the attribution is plausible
            # rather than speculation dressed as data.
            candidates = [
                cid for ended, cid in failures_by_cluster.get(cluster_idx, [])
                if ended <= opened and (opened - ended).total_seconds() <= 10 * 86400
            ]
            if candidates and rng.random() < 0.85:
                row[16] = candidates[-1]
                linked_change += 1

    return linked_problem, linked_change


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
