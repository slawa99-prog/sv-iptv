"""Synthetic queue/control tests, never evidence of real stream playback."""
import contextlib
import gzip
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sv_iptv as sv
from expansion_check import Campaign, FROZEN_RETRY_POLICY
from research_queue import archive_items


class FullIntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, value in [('ROOT', self.root), ('VALIDATION_ROOT', self.root)]:
            p = patch.object(sv, name, value)
            p.start(); self.addCleanup(p.stop)
        p = patch.object(sv, 'visual_signature', return_value='test-criteria')
        p.start(); self.addCleanup(p.stop)
        self.a = {'url': 'https://example.com/a.m3u8', 'headers': {}}
        self.foreign = {'url': 'https://example.com/foreign.m3u8', 'headers': {}}
        self.unknown = {'url': 'https://example.com/unidentified', 'headers': {}}
        self.cfg = {'sources': ['input.m3u'], 'minimum_channels': 1, 'workers': 1,
                    'require_fresh_remote_source': False, 'check_environment': 'ru-vps',
                    'publication_mode': 'reviewed-local',
                    'candidate_quality': {'enabled': True, 'require_stable': True},
                    'channel_scope': {'mode': 'reviewed-allowlist', 'allowed_ids': ['A.ru']},
                    'expansion_campaign': {'enabled': True, 'id': 'fixture', 'environments': ['ru-vps'],
                        'evidence_file': 'evidence.json', 'max_requests_per_run': 1000}}
        channel = {'id': 'A.ru', 'name': 'A', 'group': 'TV'}
        sv.write_json(self.root / 'channels.json', [channel])
        (self.root / 'input.m3u').write_text(sv.render_m3u([(channel, self.a)], {}))
        sv.write_json(self.root / 'evidence.json', {'campaign_id': 'fixture',
            'pc_success_request_keys': [sv.stream_key(self.a)]})
        (self.root / 'tv.m3u').write_text('#EXTM3U\n# reviewed main sentinel\n')

    def archive(self, items, name='archive.jsonl.gz'):
        rows = [{**i, 'key': sv.stream_key(i), 'names': ['Foreign / unknown'],
                 'scope_hint': 'foreign-metadata', 'claimed_ids': ['Foreign.xx']} for i in items]
        data = gzip.compress(('\n'.join(json.dumps(r) for r in rows) + '\n').encode(), mtime=0)
        (self.root / name).write_bytes(data)
        return {'path': name, 'request_count': len(items), 'sha256': hashlib.sha256(data).hexdigest()}

    def attach(self, items):
        self.cfg['expansion_campaign'].update(frozen_research_archives=[self.archive(items)],
            frozen_union_request_count=len({sv.stream_key(i) for i in items}))

    def run_at(self, at, result=None):
        visited = []
        def tester(item, cfg):
            visited.append(item['url'])
            return {'ok': True, 'reason': 'synthetic fixture', 'media_url': item['url'], **(result or {})}
        with contextlib.redirect_stdout(io.StringIO()):
            report = sv.build(self.cfg, tester=tester, now=at)
        return report, visited

    def test_unknown_foreign_and_header_variants_are_all_admitted_without_identity_approval(self):
        variant = {**self.a, 'headers': {'Referer': 'https://example.com/player'}}
        spec = self.archive([self.a, self.foreign, self.unknown, variant])
        items = archive_items(self.root, [spec], {sv.stream_key(self.a)}, sv.stream_key, sv.validate_url)
        self.assertEqual({sv.stream_key(i) for i in items}, {sv.stream_key(i) for i in (variant, self.foreign, self.unknown)})
        self.assertTrue(all(i['identity_review_required'] and i['research_only'] and not i['attrs'] for i in items))

    def test_archive_hash_count_key_and_safe_path_are_enforced(self):
        spec = self.archive([self.unknown])
        for mutation in ({'sha256': '0' * 64}, {'request_count': 2}, {'path': '../outside.gz'}):
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                archive_items(self.root, [{**spec, **mutation}], set(), sv.stream_key, sv.validate_url)
        data = gzip.compress((json.dumps({'url': self.a['url'], 'key': 'wrong'})+'\n').encode())
        (self.root / spec['path']).write_bytes(data)
        spec['sha256'] = hashlib.sha256(data).hexdigest()
        with self.assertRaises(ValueError):
            archive_items(self.root, [spec], set(), sv.stream_key, sv.validate_url)

    def test_archives_expand_only_ru_vps_and_cannot_change_reviewed_main(self):
        self.attach([self.a, self.foreign, self.unknown])
        before = (self.root/'tv.m3u').read_bytes()
        report, visited = self.run_at('2026-11-17T00:00:00Z')
        self.assertEqual(set(visited), {i['url'] for i in (self.a, self.foreign, self.unknown)})
        self.assertEqual(report['expansion_campaign']['verification_only_requests'], 2)
        self.assertEqual(report['candidate_quality']['held_channels'], 1)
        self.assertEqual((self.root/'tv.m3u').read_bytes(), before)
        self.cfg['check_environment'] = 'github-actions'
        report, visited = self.run_at('2026-11-17T03:00:00Z')
        self.assertEqual(visited, [self.a['url']])
        self.assertNotIn('expansion_campaign', report)
        self.assertEqual((self.root/'tv.m3u').read_bytes(), before)

    def test_old_cohort_completion_does_not_complete_added_archive(self):
        self.attach([self.a])
        self.run_at('2026-11-17T00:00:00Z')
        self.run_at('2026-11-17T03:00:00Z')
        self.attach([self.a, self.foreign, self.unknown])
        report, _ = self.run_at('2026-11-17T06:00:00Z')
        campaign = report['expansion_campaign']
        self.assertEqual(campaign['completed_cohort_passes'], 2)
        self.assertEqual(campaign['completed_frozen_queue_passes'], 1)
        self.assertEqual(campaign['request_categories']['pending'], 2)
        report, visited = self.run_at('2026-11-17T07:00:00Z')
        self.assertEqual(visited, [])
        self.assertEqual(report['expansion_campaign']['completed_frozen_queue_passes'], 1)
        report, _ = self.run_at('2026-11-17T08:00:00Z')
        self.assertEqual(report['expansion_campaign']['completed_frozen_queue_passes'], 2)
        self.assertEqual(report['stable_channels'], 0)

    def test_inconclusive_is_pending_without_failures_or_erasing_pc_evidence(self):
        self.attach([self.a, self.unknown])
        self.run_at('2026-11-17T00:00:00Z')
        report, _ = self.run_at('2026-11-17T03:00:00Z', {'ok': False, 'inconclusive': True})
        history = sv.read_json(self.root/'history.json', {})
        for stat in history['expansion_campaign']['requests'].values():
            self.assertEqual((stat['samples'], stat['successes']), (1, 1))
        self.assertTrue(history['streams'][sv.stream_key(self.a)]['last_result']['ok'])
        self.assertEqual(report['expansion_campaign']['requests_needing_format_review'], 2)
        report, visited = self.run_at('2026-11-17T06:00:00Z')
        self.assertEqual(visited, [])
        report, visited = self.run_at('2026-11-24T03:00:00Z')
        self.assertEqual(len(visited), 2)
        self.assertEqual(report['expansion_campaign']['requests_needing_format_review'], 0)

    def test_format_limitation_cannot_quarantine_but_real_weekly_checks_are_counted(self):
        key = sv.stream_key(self.unknown)
        c = Campaign({'id': 'test', 'environment': 'ru-vps'}, {}, {}, {}, {}, sv.stream_key,
                     '2026-11-17T00:00:00Z', verification_items=[self.unknown])
        for at in ('2026-11-17T00:00:00Z', '2026-11-17T03:00:00Z'):
            c.record({key: {'ok': False, 'inconclusive': True, 'checked_utc': at}})
        self.assertEqual(c.category(key), 'pending')
        self.assertEqual(c.stats[key]['samples'], 0)
        for at in ('2026-11-18T00:00:00Z', '2026-11-18T03:00:00Z'):
            c.record({key: {'ok': False, 'checked_utc': at}})
        self.assertEqual(c.category(key), 'quarantined')
        self.assertEqual(c.report()['weekly_recheck_observations'], 0)
        c.record({key: {'ok': False, 'checked_utc': '2026-11-25T03:00:00Z'}})
        self.assertEqual(c.report()['weekly_recheck_observations'], 1)
        c.record({key: {'ok': False, 'checked_utc': '2026-11-25T03:00:00Z'}})
        self.assertEqual(c.report()['weekly_recheck_observations'], 1)

    def test_direct_capture_requires_explicit_live_identity_review_even_if_stable(self):
        channel = {'id': 'A.ru', 'country': 'RU'}
        h = {'stable': True, 'last_result': {'identity_review_required': True, 'live_broadcast_review_required': True}}
        review = {'channel_id': 'A.ru', 'stream_key': sv.stream_key(self.a), 'reviewed_utc': '2026-11-16T00:00:00Z',
                  'reviewer': 'fixture', 'environment': 'pc', 'evidence': 'fixture',
                  'identity_matches': True, 'russian_audio': True, 'no_placeholder': True}
        self.cfg['candidate_quality']['identity_reviews'] = [review]
        self.assertIsNotNone(sv.candidate_review_reason(channel, self.a, h, self.cfg, '2026-11-17T00:00:00Z'))
        review['live_broadcast_confirmed'] = True
        self.assertIsNone(sv.candidate_review_reason(channel, self.a, h, self.cfg, '2026-11-17T00:00:00Z'))

    def test_one_early_adapter_retry_preserves_spacing_weekly_counts_and_pc_evidence(self):
        key = sv.stream_key(self.unknown)
        cfg = {'id': 'fixture', 'environment': 'ru-vps', 'max_requests_per_run': 1000}
        campaign = Campaign(cfg, {}, {}, {}, {'pc_success_request_keys': [key]}, sv.stream_key,
                            '2026-11-25T10:00:00Z', verification_items=[self.unknown])
        reason = 'continuous sample decode incomplete; format review required'
        result = {'ok': False, 'inconclusive': True, 'reason': reason, 'checked_utc': campaign.now}
        campaign.record({key: result})
        for when, expected in [('2026-11-25T11:59:59Z', False), ('2026-11-25T12:00:00Z', True)]:
            campaign.now = when
            self.assertEqual(key in campaign.batch({key: self.unknown}), expected)
        for restricted in ('encrypted media requires an authorized player', 'HTTP 403 requires access or rate-limit review'):
            campaign.stats[key]['inconclusive_reason'] = restricted
            self.assertEqual(campaign.batch({key: self.unknown}), {})
        campaign.stats[key]['inconclusive_reason'] = reason
        campaign.record({key: {**result, 'media_probe_version': sv.MEDIA_PROBE_VERSION, 'checked_utc': campaign.now}})
        campaign.now = '2026-11-25T16:00:00Z'
        self.assertEqual(campaign.batch({key: self.unknown}), {})
        campaign.record({key: {'ok': True, 'checked_utc': '2026-11-25T10:30:00Z'}})
        self.assertEqual((campaign.stats[key]['samples'], campaign.stats[key]['inconclusive_attempts']), (0, 2))
        self.assertIn(key, campaign.pc)
        self.assertFalse(campaign.stats[key].get('weekly_rechecks'))
        campaign.now = '2026-12-02T12:00:00Z'
        self.assertIn(key, campaign.batch({key: self.unknown}))
        campaign.record({key: {'ok': True, 'checked_utc': campaign.now}})
        self.assertEqual(campaign.stats[key]['samples'], 1)
        self.assertNotIn('inconclusive_probe_version', campaign.stats[key])
        campaign.record({key: result})
        self.assertNotIn('last_inconclusive', campaign.stats[key])

    def test_frozen_sweeps_finish_first_visits_before_repeats_then_hold_open_formats(self):
        items = [self.a, self.foreign, self.unknown]
        self.attach(items)
        before = (self.root / 'tv.m3u').read_bytes()
        ambiguous = {'ok': False, 'inconclusive': True, 'reason': 'unrecognized format'}
        self.run_at('2026-11-18T00:00:00Z', ambiguous)
        self.cfg['expansion_campaign'].update(frozen_retry_policy=FROZEN_RETRY_POLICY, max_requests_per_run=1)
        first, second = [], []
        for hour in ('10:00', '10:10', '13:00', '13:05', '13:20', '14:59', '15:00'):
            report, visited = self.run_at('2026-11-18T' + hour + ':00Z', ambiguous)
            if hour == '14:59':
                self.assertEqual(visited, [])
            elif hour <= '13:00':
                first.extend(visited)
            else:
                second.extend(visited)
        for visits in (first, second):
            self.assertCountEqual(visits, [i['url'] for i in items])
        c = report['expansion_campaign']
        sweep = c['frozen_retry_rounds']
        self.assertEqual((sweep['first_attempted'], sweep['second_attempted'], sweep['held_for_format_review']), (3, 3, 3))
        self.assertEqual((c['requests_needing_format_review'], c['completed_frozen_queue_passes']), (3, 0))
        self.assertEqual(c['request_categories']['pending'], 3)
        later, visited = self.run_at('2026-12-01T00:00:00Z')
        self.assertEqual(visited, [])
        self.assertEqual(later['expansion_campaign']['frozen_retry_rounds'], sweep)
        self.assertEqual((self.root / 'tv.m3u').read_bytes(), before)
        stats = sv.read_json(self.root / 'history.json', {})['expansion_campaign']['requests']
        for item in items:
            stat = stats[sv.stream_key(item)]
            self.assertEqual(stat['samples'], 0)
            self.assertEqual([a['outcome'] for a in stat['sweep_attempts']], ['inconclusive'] * 2)
            a, b = (sv.timestamp(a['at']) for a in stat['sweep_attempts'])
            self.assertGreaterEqual(b - a, 7200)

    def test_conclusive_repeat_gets_missing_sample_pc_protection_and_genuine_weekly_audit(self):
        self.attach([self.a, self.foreign])
        self.cfg['expansion_campaign']['frozen_retry_policy'] = FROZEN_RETRY_POLICY
        self.run_at('2026-11-18T00:00:00Z', {'ok': False, 'inconclusive': True})
        failure = {'ok': False, 'reason': 'network timeout or unavailable'}
        report, visited = self.run_at('2026-11-18T02:00:00Z', failure)
        self.assertEqual(len(visited), 2)
        c = report['expansion_campaign']
        self.assertEqual(c['frozen_retry_rounds']['awaiting_counted_sample'], 2)
        self.assertEqual(c['completed_frozen_queue_passes'], 1)
        self.assertEqual(self.run_at('2026-11-18T03:59:59Z', failure)[1], [])
        report, _ = self.run_at('2026-11-18T04:00:00Z', failure)
        c = report['expansion_campaign']
        self.assertEqual((c['completed_frozen_queue_passes'], c['weekly_rechecked_requests']), (2, 0))
        self.assertEqual((c['request_categories']['pc_only'], c['request_categories']['quarantined']), (1, 1))
        self.assertEqual(self.run_at('2026-11-25T03:59:59Z', failure)[1], [])
        report, visited = self.run_at('2026-11-25T04:00:00Z', failure)
        c = report['expansion_campaign']
        self.assertEqual((len(visited), c['weekly_rechecked_requests'], c['weekly_recheck_observations']), (2, 2, 2))

    def test_network_outage_and_stale_sweep_results_cannot_advance_a_request(self):
        at = '2026-11-18T00:00:00Z'
        controls = [{'url': f'https://example.com/{n}', 'headers': {}} for n in range(12)]
        history = {sv.stream_key(i): {'stable': True} for i in controls}
        cfg = {'id': 'fixture', 'environment': 'ru-vps', 'max_requests_per_run': 1000,
               'frozen_retry_policy': FROZEN_RETRY_POLICY}
        c = Campaign(cfg, {}, {'controls': controls}, history, {}, sv.stream_key, at, [self.unknown])
        key = sv.stream_key(self.unknown)
        self.assertEqual(set(c.batch(c.items)), {key} | history.keys())
        c.record({k: {'ok': False, 'checked_utc': at} for k in c.items})
        self.assertTrue(c.network_degraded)
        self.assertEqual(c.stats[key]['sweep_attempts'], [])
        c.now = '2026-11-18T02:00:00Z'
        self.assertIn(key, c.batch(c.items))
        c.record({key: {'ok': True, 'checked_utc': '2026-11-18T01:59:59Z'}})
        self.assertEqual(c.stats[key]['samples'], 0)
        c.record({key: {'ok': True, 'checked_utc': c.now}})
        c.record({key: {'ok': True, 'checked_utc': c.now}})
        self.assertEqual((len(c.stats[key]['sweep_attempts']), c.stats[key]['samples']), (1, 1))

    def test_old_policy_and_previous_sweep_checkpoints_cannot_supply_a_new_visit(self):
        self.attach([self.a])
        self.cfg['resume_checks'] = True
        visited = []
        key = sv.stream_key(self.a)
        def checker(item, cfg):
            visited.append(item['url'])
            return {'ok': False, 'inconclusive': True, 'reason': 'format review required'}
        def run(at):
            with patch.object(sv, 'check_stream', checker), patch.object(sv.shutil, 'which', return_value='/fake/ffmpeg'), \
                 patch.object(sv, 'utcnow', return_value=at), patch.object(sv.time, 'time', return_value=sv.timestamp(at)), \
                 contextlib.redirect_stdout(io.StringIO()):
                return sv.build(self.cfg, tester=checker, now=at)
        run('2026-11-18T00:00:00Z')
        self.cfg['expansion_campaign']['frozen_retry_policy'] = FROZEN_RETRY_POLICY
        for hour in ('03', '06'):
            at = f'2026-11-18T{hour}:00:00Z'
            checkpoint = sv.read_json(self.root / 'probe-progress.json', {})
            checkpoint.update(completed=False, results={key: {'ok': False, 'checked_utc': at, 'reason': 'old result'}})
            sv.write_json(self.root / 'probe-progress.json', checkpoint)
            visited.clear()
            report = run(at)
            self.assertEqual(visited, [self.a['url']])
        self.assertEqual(report['expansion_campaign']['completed_frozen_queue_passes'], 0)
        self.assertEqual(report['expansion_campaign']['frozen_retry_rounds']['held_for_format_review'], 1)

    def test_prior_success_and_two_samples_do_not_clear_an_open_format_review(self):
        key = sv.stream_key(self.unknown)
        c = Campaign({'id': 'fixture', 'environment': 'ru-vps'}, {}, {}, {}, {}, sv.stream_key,
                     '2026-11-25T10:00:00Z', verification_items=[self.unknown])
        for hour in ('00','03'):
            c.record({key: {'ok': True, 'checked_utc': '2026-11-25T'+hour+':00:00Z'}})
        c.record({key: {'ok': False, 'inconclusive': True, 'checked_utc': c.now, 'reason': 'access review'}})
        self.assertEqual(c.category(key), 'pending')
        self.assertEqual((c.stats[key]['samples'], c.stats[key]['successes']), (2, 2))
        self.assertTrue(c.stats[key]['ever_passed'])

    def test_old_held_html_uses_new_adapter_through_build_without_replaying_checkpoint(self):
        self.attach([self.unknown])
        self.cfg['expansion_campaign']['frozen_retry_policy'] = FROZEN_RETRY_POLICY
        self.cfg['resume_checks'] = True
        key = sv.stream_key(self.unknown)
        before = (self.root / 'tv.m3u').read_bytes()
        calls = []
        def checker(item, cfg):
            if sv.stream_key(item) != key:
                return {'ok': False, 'reason': 'fixture'}
            calls.append(bool(cfg.get('_review_held_format')))
            detail = {'body_kind': 'html', 'content_type': 'text/html'}
            if cfg.get('_review_held_format'):
                detail.update(format_review='html_no_declared_media', source_keys=[])
            return {'ok': False, 'inconclusive': True, 'reason': 'page', 'response_diagnostic': detail}
        def run(at):
            with patch.object(sv, 'check_stream', checker), patch.object(sv.shutil, 'which', return_value='/fake/ffmpeg'), \
                 patch.object(sv, 'utcnow', return_value=at), patch.object(sv.time, 'time', return_value=sv.timestamp(at)), \
                 contextlib.redirect_stdout(io.StringIO()):
                return sv.build(self.cfg, tester=checker, now=at)
        for hour in ('00', '03'):
            run('2026-11-27T' + hour + ':00:00Z')
        # Same format-probe phase, still before the minimum interval.
        run('2026-11-27T04:00:00Z')
        path = self.root / 'probe-progress.json'
        unfinished = sv.read_json(path, {})
        unfinished.update(completed=False, results={key: {'checked_utc': '2026-11-27T05:55:00Z',
                                                          'ok': False, 'reason': 'unmarked old result'}})
        sv.write_json(path, unfinished)
        report = run('2026-11-27T06:00:00Z')['expansion_campaign']
        run('2026-11-28T06:00:00Z')
        self.assertEqual(calls, [False, False, True])
        self.assertEqual(report['frozen_retry_rounds']['format_review']['html_pages'], {'html_no_declared_media': 1})
        row = sv.read_json(self.root/'history.json', {})['expansion_campaign']['requests'][key]
        self.assertEqual((row['samples'], row['sweep_attempts'][-1]['at']), (0, '2026-11-27T06:00:00Z'))
        self.assertEqual((self.root / 'tv.m3u').read_bytes(), before)

    def test_resume_after_queue_change_keeps_original_dates_and_archives_checkpoint(self):
        self.attach([self.a])
        self.cfg['resume_checks'] = True
        visited = []
        def checker(item, cfg):
            visited.append(item['url'])
            return {'ok': False, 'reason': 'synthetic fixture'}
        def run(at):
            with patch.object(sv, 'check_stream', checker), patch.object(sv.shutil, 'which', return_value='/fake/ffmpeg'), \
                 patch.object(sv, 'utcnow', return_value=at), patch.object(sv.time, 'time', return_value=sv.timestamp(at)), \
                 contextlib.redirect_stdout(io.StringIO()):
                return sv.build(self.cfg, tester=checker, now=at)
        run('2026-11-17T00:00:00Z')
        checkpoint = sv.read_json(self.root/'probe-progress.json', {})
        checkpoint.update(completed=False, results={sv.stream_key(self.a):
            {'ok': False, 'reason': 'synthetic fixture', 'checked_utc': '2026-11-17T00:00:00Z'}})
        sv.write_json(self.root/'probe-progress.json', checkpoint)
        self.attach([self.a, self.unknown])
        visited.clear()
        report = run('2026-11-17T03:00:00Z')
        self.assertEqual(visited, [self.unknown['url']])
        self.assertTrue((self.root/'checkpoints'/f"{checkpoint['signature']}.json").exists())
        history = sv.read_json(self.root/'history.json', {})
        self.assertEqual(history['expansion_campaign']['requests'][sv.stream_key(self.a)]['samples'], 1)
        self.assertEqual(report['expansion_campaign']['completed_frozen_queue_passes'], 1)
        visited.clear()
        report = run('2026-11-17T06:00:00Z')
        self.assertEqual(set(visited), {self.a['url'], self.unknown['url']})
        self.assertEqual(report['expansion_campaign']['completed_frozen_queue_passes'], 2)

    def test_worker_increase_reuses_unfinished_checkpoint_without_a_second_sample(self):
        self.attach([self.a])
        self.cfg['resume_checks'] = True
        visited = []
        def checker(item, cfg):
            visited.append(item['url'])
            return {'ok': False, 'reason': 'synthetic fixture'}
        def run(at):
            with patch.object(sv, 'check_stream', checker), patch.object(sv.shutil, 'which', return_value='/fake/ffmpeg'), \
                 patch.object(sv, 'utcnow', return_value=at), patch.object(sv.time, 'time', return_value=sv.timestamp(at)), \
                 contextlib.redirect_stdout(io.StringIO()):
                return sv.build(self.cfg, tester=checker, now=at)
        run('2026-11-18T00:00:00Z')
        checkpoint = sv.read_json(self.root/'probe-progress.json', {})
        checkpoint.update(completed=False, results={sv.stream_key(self.a):
            {'ok': False, 'reason': 'synthetic fixture', 'checked_utc': '2026-11-18T00:00:00Z'}})
        sv.write_json(self.root/'probe-progress.json', checkpoint)
        self.cfg['workers'] = 6
        visited.clear()
        report = run('2026-11-18T03:00:00Z')
        self.assertEqual(visited, [])
        history = sv.read_json(self.root/'history.json', {})
        request = history['expansion_campaign']['requests'][sv.stream_key(self.a)]
        self.assertEqual(request['samples'], 1)
        self.assertEqual(request['sample_at'], '2026-11-18T00:00:00Z')
        self.assertEqual(report['expansion_campaign']['completed_frozen_queue_passes'], 1)

