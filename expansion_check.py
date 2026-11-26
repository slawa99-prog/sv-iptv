"""Two independent broad passes, then bounded monitoring of useful variants.

Campaign counters belong to one network and are not stability observations.
PC evidence protects known working requests but never raises a VPS rating.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime


MEDIA_PROBE_VERSION = "2026-11-25.3"
FROZEN_RETRY_POLICY = "two-sweeps-v1"
UNCLASSIFIED_RESPONSES = {
    "format requires a separate adapter or access review",
    "unrecognized continuous transport",
}
# Histories before the first adapter release did not record a probe version.
# The diagnostic release only adds a reason to revisit previously unclassified
# responses. A known access restriction or a current diagnostic waits as usual.
EARLY_RETRY_REASONS = {None: UNCLASSIFIED_RESPONSES | {
    "continuous sample decode incomplete; format review required",
    "byte-range HLS: unsupported", "byte-range initialization: unsupported",
}, "2026-11-25.1": UNCLASSIFIED_RESPONSES,
   "2026-11-25.2": {"HTTP response (unknown_binary) requires format or access review",
                    "HTTP response (id3) requires format or access review"}}


def epoch(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def passed_before(history):
    return bool(history.get('last_result', {}).get('ok') or any(
        isinstance(c, dict) and c.get('ok') for c in history.get('checks', [])))


def compact_failed_history(history, campaign):
    """Keep all dated failures, while removing duplicated derived/debug fields.

    A former VPS success or any protected PC success prevents this compaction.
    Normal observation() reconstructs the derived fields on the next trial.
    """
    for key, entry in list(history.items()):
        if (campaign.category(key) != 'quarantined' or key in campaign.pc
                or key in campaign.baseline or passed_before(entry)):
            continue
        history[key] = {'checks': [{k: v for k, v in c.items() if v is not None}
                                  for c in entry.get('checks', []) if isinstance(c, dict)],
                        'last_checked': entry.get('last_checked'),
                        'last_result': {'ok': False, 'reason': entry.get('last_result', {}).get('reason', '')},
                        'samples': entry.get('samples', 0), 'success_rate': 0, 'stable': False}
    return history


class Campaign:
    def __init__(self, config, saved, found, history, evidence, key, now, verification_items=()):
        self.config, self.key, self.now = config, key, now
        self.required = max(2, int(config.get('minimum_passes', 2)))
        self.spacing = max(2, float(config.get('minimum_spacing_hours', 2))) * 3600
        self.retry = max(24, float(config.get('retry_rejected_hours', 168))) * 3600
        self.items = {key(i): i for items in found.values() for i in items}
        self.eligible_keys = set(self.items)
        self.verification_keys = {key(i) for i in verification_items}
        self.items.update((key(i), i) for i in verification_items)
        self.found = found
        self.pc = set(evidence.get('pc_success_request_keys', []))
        self.pc_channels = {c.casefold() for c in evidence.get('pc_success_channel_ids', [])}
        if saved.get('id') == config['id'] and saved.get('environment') == config['environment']:
            self.state = deepcopy(saved)
        else:
            self.state = {'id': config['id'], 'environment': config['environment'],
                          'started_utc': now, 'cohort_keys': sorted(self.items), 'requests': {}}
        self.stats = self.state['requests']
        self.baseline = set(self.state.get('previously_working_keys', []))
        self.baseline.update(k for k in self.items if passed_before(history.get(k, {})))
        self.state['previously_working_keys'] = sorted(self.baseline)
        self.proven = set(self.state.get('publication_protected_keys', []))
        self.proven.update(k for k in self.items if k not in self.stats and passed_before(history.get(k, {})))
        self.state['publication_protected_keys'] = sorted(self.proven)
        self.controls = {k for k in self.items if history.get(k, {}).get('stable')}
        self.network_degraded = False
        self.sweep_enabled = (config.get('frozen_retry_policy') == FROZEN_RETRY_POLICY
                              and config['environment'] == 'ru-vps')
        self.sweep_keys = set()
        self.scheduled_sweep = set()
        self.batch_phase = None
        if self.sweep_enabled:
            if self.state.get('frozen_retry_policy') != FROZEN_RETRY_POLICY:
                # Enrol the unfinished frozen requests once. Store each audit trail
                # inside requests so that large histories are partitioned losslessly.
                for k in self.verification_keys:
                    if self.stats.get(k, {}).get('samples', 0) < self.required:
                        self.stats.setdefault(k, {'samples': 0, 'successes': 0})['sweep_attempts'] = []
                self.state['frozen_retry_policy'] = FROZEN_RETRY_POLICY
            self.sweep_keys = {k for k in self.verification_keys if 'sweep_attempts' in self.stats.get(k, {})}

    def sweep_stage(self, key):
        stat = self.stats[key]
        if stat.get('samples', 0) >= self.required:
            return 'covered'
        attempts = stat['sweep_attempts']
        if not attempts:
            return 'first'
        if len(attempts) == 1:
            return 'second'
        if attempts[-1]['outcome'] == 'inconclusive' and stat.get('last_inconclusive'):
            return 'format_review'
        # A conclusive repeat after an inconclusive first visit can leave one
        # genuine sample missing. Give that request its independent observation.
        return 'missing_sample'

    @staticmethod
    def sweep_phase(stages):
        for stage in ('first', 'second', 'missing_sample'):
            if stage in stages:
                return stage
        return 'awaiting_format_review' if 'format_review' in stages else 'no_sweep_due'

    def sweep_due(self, key, at):
        stat = self.stats[key]
        return all(not stat.get(field) or at - epoch(stat[field]) >= self.spacing
                   for field in ('last_checked', 'last_inconclusive', 'sweep_deferred_utc'))

    def sweep_report(self):
        stages = Counter(self.sweep_stage(k) for k in self.sweep_keys)
        attempts = [len(self.stats[k]['sweep_attempts']) for k in self.sweep_keys]
        return {'policy': FROZEN_RETRY_POLICY, 'phase': self.sweep_phase(stages),
                'target_requests': len(self.sweep_keys), 'first_attempted': sum(n >= 1 for n in attempts),
                'second_attempted': sum(n >= 2 for n in attempts), 'first_remaining': stages['first'],
                'awaiting_second': stages['second'], 'awaiting_counted_sample': stages['missing_sample'],
                'held_for_format_review': stages['format_review'], 'with_required_samples': stages['covered']}

    def category(self, key):
        stat = self.stats.get(key, {})
        if stat.get('last_inconclusive'):
            return 'pending'
        if stat.get('samples', 0) < self.required:
            return 'pending'
        if stat.get('successes', 0) or key in self.baseline:
            return 'monitor'
        return 'pc_only' if key in self.pc else 'quarantined'

    def choose(self, items, history, previous, cfg, selector):
        pending, active, retry = [], [], []
        for item in items:
            key = self.key(item)
            category = self.category(key)
            if category == 'pending':
                pending.append(item)
            elif category == 'monitor':
                active.append(item)
            elif epoch(self.now) - epoch(self.stats[key]['last_checked']) >= self.retry:
                retry.append(item)
        # Every pending variant is examined. The six-variant limit applies only
        # after its two independent attempts, not to the discovery cohort.
        monitored = selector(active, history, previous, cfg, self.now)
        return pending + monitored + retry

    def record(self, results):
        controls = {k for k in self.controls & results.keys() if not results[k].get('inconclusive')}
        self.network_degraded = len(controls) >= 10 and sum(bool(results[k]['ok']) for k in controls) < len(controls) * .5
        for key, result in results.items():
            at = result['checked_utc']
            previous = self.stats.get(key, {})
            if any(previous.get(name) and epoch(at) <= epoch(previous[name])
                   for name in ('last_checked', 'last_inconclusive')):
                continue
            if key in self.scheduled_sweep:
                if not self.sweep_due(key, epoch(at)):
                    continue
                if self.network_degraded and not result['ok']:
                    # Rotate past a failed network control batch, without using
                    # it as a first visit, repeat visit or negative sample.
                    previous['sweep_deferred_utc'] = at
                else:
                    outcome = 'inconclusive' if result.get('inconclusive') else ('success' if result['ok'] else 'failure')
                    previous['sweep_attempts'].append({'at': at, 'outcome': outcome})
                    previous.pop('sweep_deferred_utc', None)
            if result.get('inconclusive'):
                stat = self.stats.setdefault(key, {'samples': 0, 'successes': 0})
                stat['last_inconclusive'] = at
                stat['inconclusive_reason'] = result.get('reason', 'format review required')
                stat['inconclusive_attempts'] = stat.get('inconclusive_attempts', 0) + 1
                if result.get('media_probe_version'):
                    stat['inconclusive_probe_version'] = result['media_probe_version']
                else:
                    stat.pop('inconclusive_probe_version', None)
                if result.get('response_diagnostic'):
                    stat['response_diagnostic'] = result['response_diagnostic']
                else:
                    stat.pop('response_diagnostic', None)
                continue
            if self.network_degraded and not result['ok']:
                # Keep the failed VPS observation in normal history, but do not
                # discard discovery candidates during a broad network outage.
                continue
            stat = self.stats.setdefault(key, {'samples': 0, 'successes': 0})
            last = stat.get('last_checked')
            if (last and self.category(key) in ('pc_only', 'quarantined')
                    and epoch(at) - epoch(last) >= self.retry):
                stat['weekly_rechecks'] = stat.get('weekly_rechecks', 0) + 1
                stat['last_weekly_recheck'] = at
            anchor = stat.get('sample_at')
            independent = not anchor or epoch(at) - epoch(anchor) >= self.spacing
            if independent:
                stat['samples'] += 1
                stat['successes'] += bool(result['ok'])
                stat['sample_at'] = at
                stat['sample_ok'] = bool(result['ok'])
                stat.setdefault('first_checked', at)
            elif stat.get('sample_ok') and not result['ok']:
                # A rapid retry cannot erase a failure or manufacture a pass.
                stat['sample_ok'] = False
                stat['successes'] -= 1
            stat['last_checked'] = at
            stat.pop('last_inconclusive', None)
            stat.pop('inconclusive_reason', None)
            stat.pop('inconclusive_probe_version', None)
            stat.pop('response_diagnostic', None)
            if result['ok']:
                stat['ever_passed'] = True
                self.baseline.add(key)
        self.state['previously_working_keys'] = sorted(self.baseline)

    def verification_only_due(self):
        """Keep every frozen request auditable even if its channel is retired.

        These requests cannot enter a playlist. Probe them twice independently,
        then only on the weekly audit; never fabricate observations for a slate.
        """
        return [self.items[k] for k in sorted(self.verification_keys - self.eligible_keys)
                if self.category(k) == 'pending' or
                epoch(self.now) - epoch(self.stats[k]['last_checked']) >= self.retry]

    def batch(self, items):
        """Bound a very large intake without treating a batch as a full pass.

        Pending keys with fewer observations come first. A short timer cannot
        manufacture the second independent attempt. Existing samples stay in
        state even when their keys are outside this batch.
        """
        limit = int(self.config.get('max_requests_per_run', 0))
        if limit <= 0 and not self.sweep_enabled:
            return items
        if limit <= 0:
            limit = len(items)
        now = epoch(self.now)
        normal_retry = max(2, float(self.config.get('inconclusive_retry_hours', 168))) * 3600
        due = {}
        stages = {k: self.sweep_stage(k) for k in self.sweep_keys}
        self.batch_phase = self.sweep_phase(set(stages.values()))
        focus = set()
        for key, item in items.items():
            stage = stages.get(key)
            if stage and stage != 'covered':
                if stage == self.batch_phase and self.sweep_due(key, now):
                    due[key] = item
                    focus.add(key)
                continue
            stat = self.stats.get(key, {})
            if stat.get('last_checked') and now - epoch(stat['last_checked']) < self.spacing:
                continue
            if stat.get('last_inconclusive'):
                retry = normal_retry
                early_reasons = EARLY_RETRY_REASONS.get(stat.get('inconclusive_probe_version'), ())
                if (self.config['environment'] == 'ru-vps' and stat.get('samples', 0) < self.required
                        and stat.get('inconclusive_reason') in early_reasons):
                    retry = self.spacing
                if now - epoch(stat['last_inconclusive']) < retry:
                    continue
            due[key] = item
        controls = sorted(self.controls & due.keys(), key=lambda k: (self.stats.get(k, {}).get('last_checked', ''), k))[:min(16, limit // 10)]
        ordered = sorted((focus or due.keys()) - set(controls), key=lambda k: (
            self.category(k) != 'pending', self.stats.get(k, {}).get('samples', 0),
            k not in self.eligible_keys, self.stats.get(k, {}).get('last_checked', ''), k))
        selected = controls + ordered[:limit-len(controls)]
        self.scheduled_sweep = focus.intersection(selected)
        return {k: due[k] for k in selected}

    def publishable(self, key):
        # Old proven sources retain service during the broad pass. A genuinely
        # new source waits for the second independent attempt.
        stat = self.stats.get(key, {})
        return key in self.pc or key in self.proven or stat.get('samples', 0) >= self.required

    def report(self):
        counts = {c: 0 for c in ('pending', 'monitor', 'pc_only', 'quarantined')}
        bodies, mimes = Counter(), Counter()
        below_required = 0
        for key in self.items:
            counts[self.category(key)] += 1
            stat = self.stats.get(key, {})
            detail = stat.get('response_diagnostic')
            if detail and stat.get('last_inconclusive') and key in self.verification_keys:
                bodies[detail['body_kind']] += 1
                mimes[detail['content_type']] += 1
                below_required += stat.get('samples', 0) < self.required
        cohort = self.state['cohort_keys']
        passes = min((min(self.required, self.stats.get(k, {}).get('samples', 0)) for k in cohort), default=0)
        zero_both, pc_only_channels, pending_channels = [], [], []
        for cid, items in self.found.items():
            keys = {self.key(i) for i in items}
            if not keys:
                continue
            if any(self.category(k) == 'pending' for k in keys):
                pending_channels.append(cid)
            elif not any(self.category(k) == 'monitor' for k in keys):
                (pc_only_channels if cid in self.pc_channels or keys & self.pc else zero_both).append(cid)
        return {'id': self.state['id'], 'environment': self.state['environment'],
                'phase': 'broad_checks' if counts['pending'] else 'monitoring',
                'started_utc': self.state['started_utc'],
                'required_independent_passes': self.required, 'completed_cohort_passes': passes,
                'cohort_requests': len(cohort), 'current_requests': len(self.items),
                'completed_current_passes': min((min(self.required, self.stats.get(k, {}).get('samples', 0))
                                                 for k in self.items), default=0),
                'original_cohort_requests_not_current': len(set(cohort) - self.items.keys()),
                'publication_candidate_requests': len(self.eligible_keys),
                'frozen_queue_requests': len(self.verification_keys),
                **({'frozen_retry_rounds': self.sweep_report()} if self.sweep_enabled else {}),
                'verification_only_requests': len(self.verification_keys - self.eligible_keys),
                'frozen_queue_sha256': self.config.get('frozen_queue', {}).get('sha256'),
                'frozen_queues': ([self.config['frozen_queue']] if self.config.get('frozen_queue') else []) +
                                 self.config.get('additional_frozen_queues', []),
                'frozen_research_archives': self.config.get('frozen_research_archives', []),
                'completed_frozen_queue_passes': min((min(self.required, self.stats.get(k, {}).get('samples', 0))
                                                     for k in self.verification_keys), default=0),
                'request_categories': counts, 'minimum_spacing_hours': self.spacing / 3600,
                'requests_needing_format_review': sum(bool(s.get('last_inconclusive')) for s in self.stats.values()),
                'frozen_response_diagnostics': {'requests': sum(bodies.values()), 'below_required_samples': below_required,
                                                'by_body_kind': dict(bodies), 'by_content_type': dict(mimes)},
                'inconclusive_attempts': sum(s.get('inconclusive_attempts', 0) for s in self.stats.values()),
                'inconclusive_is_playback_failure': False,
                'retry_rejected_hours': self.retry / 3600,
                'weekly_rechecked_requests': sum(bool(s.get('weekly_rechecks')) for s in self.stats.values()),
                'weekly_recheck_observations': sum(s.get('weekly_rechecks', 0) for s in self.stats.values()),
                'channels_pending': sorted(pending_channels),
                'channels_zero_success_both': sorted(zero_both),
                'channels_protected_by_pc': sorted(pc_only_channels),
                'negative_trials_deferred_for_network_outage': self.network_degraded,
                'pc_evidence_is_stability_history': False}
