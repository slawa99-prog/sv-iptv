"""Two independent broad passes, then bounded monitoring of useful variants.

Campaign counters belong to one network and are not stability observations.
PC evidence protects known working requests but never raises a VPS rating.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime


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

    def category(self, key):
        stat = self.stats.get(key, {})
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
        controls = self.controls & results.keys()
        self.network_degraded = len(controls) >= 10 and sum(bool(results[k]['ok']) for k in controls) < len(controls) * .5
        for key, result in results.items():
            at = result['checked_utc']
            if self.network_degraded and not result['ok']:
                # Keep the failed VPS observation in normal history, but do not
                # discard discovery candidates during a broad network outage.
                continue
            stat = self.stats.setdefault(key, {'samples': 0, 'successes': 0})
            last = stat.get('last_checked')
            # Resuming an interrupted pass must not count the same result twice.
            if last and epoch(at) <= epoch(last):
                continue
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
        if limit <= 0:
            return items
        due = {k: v for k, v in items.items() if
               not self.stats.get(k, {}).get('last_checked') or
               epoch(self.now) - epoch(self.stats[k]['last_checked']) >= self.spacing}
        controls = sorted(self.controls & due.keys(), key=lambda k: (self.stats.get(k, {}).get('last_checked', ''), k))[:min(16, limit // 10)]
        ordered = sorted(due.keys() - set(controls), key=lambda k: (
            self.category(k) != 'pending', self.stats.get(k, {}).get('samples', 0),
            k not in self.eligible_keys, self.stats.get(k, {}).get('last_checked', ''), k))
        return {k: due[k] for k in controls + ordered[:limit-len(controls)]}

    def publishable(self, key):
        # Old proven sources retain service during the broad pass. A genuinely
        # new source waits for the second independent attempt.
        stat = self.stats.get(key, {})
        return key in self.pc or key in self.proven or stat.get('samples', 0) >= self.required

    def report(self):
        counts = {c: 0 for c in ('pending', 'monitor', 'pc_only', 'quarantined')}
        for key in self.items:
            counts[self.category(key)] += 1
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
                'verification_only_requests': len(self.verification_keys - self.eligible_keys),
                'frozen_queue_sha256': self.config.get('frozen_queue', {}).get('sha256'),
                'frozen_queues': ([self.config['frozen_queue']] if self.config.get('frozen_queue') else []) +
                                 self.config.get('additional_frozen_queues', []),
                'completed_frozen_queue_passes': min((min(self.required, self.stats.get(k, {}).get('samples', 0))
                                                     for k in self.verification_keys), default=0),
                'request_categories': counts, 'minimum_spacing_hours': self.spacing / 3600,
                'retry_rejected_hours': self.retry / 3600,
                'weekly_rechecked_requests': sum(bool(s.get('weekly_rechecks')) for s in self.stats.values()),
                'weekly_recheck_observations': sum(s.get('weekly_rechecks', 0) for s in self.stats.values()),
                'channels_pending': sorted(pending_channels),
                'channels_zero_success_both': sorted(zero_both),
                'channels_protected_by_pc': sorted(pc_only_channels),
                'negative_trials_deferred_for_network_outage': self.network_degraded,
                'pc_evidence_is_stability_history': False}
