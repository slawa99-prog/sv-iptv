import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sv_iptv as sv
from expansion_check import Campaign, compact_failed_history


class ExpansionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for field in ('ROOT', 'VALIDATION_ROOT'):
            p = patch.object(sv, field, self.root)
            p.start()
            self.addCleanup(p.stop)
        self.cfg = {'sources': ['input.m3u'], 'minimum_channels': 1,
                    'workers': 2, 'max_variants_per_channel': 2,
                    'require_fresh_remote_source': False, 'check_environment': 'ru-vps',
                    'channel_scope': {'mode': 'reviewed-allowlist', 'allowed_ids': ['A.ru', 'B.ru'],
                                      'excluded_ids': ['Foreign.xx']},
                    'expansion_campaign': {'enabled': True, 'id': 'wide-test',
                        'environments': ['ru-vps'], 'minimum_passes': 2,
                        'evidence_file': 'evidence.json', 'retry_rejected_hours': 168}}
        self.urls = [f'https://example.com/{i}.m3u8' for i in range(8)]
        self.items = [{'url': u, 'headers': {}} for u in self.urls]
        sv.write_json(self.root / 'channels.json', [{'id': c, 'name': c, 'group': 'ТВ'} for c in ['A.ru', 'B.ru', 'Foreign.xx']])
        text = '#EXTM3U\n' + ''.join(f'#EXTINF:-1 tvg-id="A.ru",A\n{u}\n' for u in self.urls)
        text += '#EXTINF:-1 tvg-id="B.ru",B\nhttps://example.com/pc.m3u8\n'
        text += '#EXTINF:-1 tvg-id="Foreign.xx",Foreign\nhttps://example.com/foreign.m3u8\n'
        (self.root / 'input.m3u').write_text(text)
        self.evidence = {'campaign_id': 'wide-test',
            'pc_success_request_keys': [sv.stream_key({'url':'https://example.com/pc.m3u8'})],
            'pc_success_channel_ids': ['B.ru']}
        sv.write_json(self.root / 'evidence.json', self.evidence)

    def run_at(self, at, good=()):
        visited=[]
        def checker(item, cfg):
            visited.append(item['url'])
            return {'ok': item['url'] in good, 'reason': 'fixture', 'media_url': item['url']}
        with contextlib.redirect_stdout(io.StringIO()):
            report=sv.build(self.cfg, tester=checker, now=at)
        return report, visited

    def test_full_two_passes_then_prune_with_pc_protection_and_periodic_retry(self):
        r, tested=self.run_at('2026-11-17T00:00:00Z', self.urls[:1])
        self.assertEqual(len(tested), 9)  # no per-channel cap during discovery
        self.assertEqual(r['unique_channels_passed'], 0)  # new link awaits second pass
        r, tested=self.run_at('2026-11-17T03:00:00Z', self.urls[:1])
        self.assertEqual(len(tested), 9)
        self.assertEqual(r['unique_channels_passed'], 1)
        summary=r['expansion_campaign']
        self.assertEqual(summary['completed_cohort_passes'], 2)
        self.assertEqual(summary['request_categories'], {'pending':0,'monitor':1,'pc_only':1,'quarantined':7})
        self.assertEqual(summary['channels_protected_by_pc'], ['b.ru'])
        r, tested=self.run_at('2026-11-17T06:00:00Z', self.urls[:1])
        self.assertEqual(tested, self.urls[:1])
        r, tested=self.run_at('2026-11-25T06:00:00Z', self.urls[:2])
        self.assertEqual(len(tested), 9)
        self.assertEqual(r['expansion_campaign']['request_categories']['monitor'], 2)
        self.assertNotIn('https://example.com/foreign.m3u8', tested)

    def test_bounded_batches_cover_every_key_and_do_not_claim_full_pass(self):
        self.cfg['expansion_campaign']['max_requests_per_run'] = 3
        visits = []
        for minute in (0, 10, 20):
            report, tested = self.run_at(f'2026-11-17T00:{minute:02d}:00Z')
            self.assertEqual(len(tested), 3)
            self.assertTrue(report['partial_campaign_batch'])
            self.assertEqual(report['publication_reason'], 'partial campaign batch; previous playlist retained')
            visits.extend(tested)
        self.assertEqual(len(set(visits)), 9)
        self.assertEqual(report['expansion_campaign']['completed_cohort_passes'], 1)
        report, tested = self.run_at('2026-11-17T01:00:00Z')
        self.assertEqual(tested, [])
        self.assertEqual(report['expansion_campaign']['completed_cohort_passes'], 1)
        for minute in (0, 10, 20):
            report, tested = self.run_at(f'2026-11-17T03:{minute:02d}:00Z')
            self.assertEqual(len(tested), 3)
        self.assertEqual(report['expansion_campaign']['completed_cohort_passes'], 2)
        self.assertEqual(report['expansion_campaign']['request_categories'], {'pending':0, 'monitor':0, 'pc_only':1, 'quarantined':8})

    def test_quick_retries_are_not_two_passes_and_pc_does_not_inflate_rating(self):
        self.run_at('2026-11-17T00:00:00Z')
        r,_=self.run_at('2026-11-17T01:00:00Z', ['https://example.com/pc.m3u8'])
        self.assertEqual(r['expansion_campaign']['completed_cohort_passes'], 1)
        self.assertEqual(r['stable_channels'], 0)
        self.assertEqual(r['expansion_campaign']['request_categories']['quarantined'], 0)
        h=sv.read_json(self.root/'history.json', {})
        key=self.evidence['pc_success_request_keys'][0]
        self.assertEqual(h['streams'][key]['samples'], 1)
        self.assertEqual(h['streams'][key]['success_rate'], 0)

    def test_compact_history_retains_trial_dates_and_protects_pc_and_vps_success(self):
        self.cfg['expansion_campaign']['compact_research_history'] = True
        self.run_at('2026-11-17T00:00:00Z', self.urls[:1])
        self.run_at('2026-11-17T03:00:00Z', self.urls[:1])
        h = sv.read_json(self.root/'history.json', {})
        failed = h['streams'][sv.stream_key(self.items[1])]
        self.assertEqual([c['at'] for c in failed['checks']], ['2026-11-17T00:00:00Z', '2026-11-17T03:00:00Z'])
        self.assertEqual((failed['samples'], failed['stable']), (2, False))
        self.assertIn('observation_hours', h['streams'][self.evidence['pc_success_request_keys'][0]])
        self.assertIn('observation_hours', h['streams'][sv.stream_key(self.items[0])])
        self.assertEqual(h['expansion_campaign']['requests'][sv.stream_key(self.items[1])]['samples'], 2)
        report, _ = self.run_at('2026-11-25T06:00:00Z', self.urls[:2])
        recovered = sv.read_json(self.root/'history.json', {})['streams'][sv.stream_key(self.items[1])]
        self.assertEqual(recovered['samples'], 3)
        self.assertFalse(recovered['stable'])

    def test_previously_proven_server_stream_stays_available_during_expansion(self):
        key=sv.stream_key(self.items[0])
        sv.write_json(self.root/'history.json', {'streams':{key:{'checks':[{'at':'2026-11-16T00:00:00Z','ok':True}],
            'last_checked':'2026-11-16T00:00:00Z','last_result':{'ok':True}}},'last_selected':{'a.ru':key}})
        r,_=self.run_at('2026-11-17T00:00:00Z', self.urls[:1])
        self.assertEqual(r['unique_channels_passed'],1)

    def test_campaign_does_not_expand_github_or_import_pc_observations(self):
        self.cfg['check_environment']='github-actions'
        r,tested=self.run_at('2026-11-17T00:00:00Z', self.urls)
        self.assertEqual(len(tested),3)
        self.assertNotIn('expansion_campaign',r)
        self.assertEqual(r['stable_channels'],0)

    def test_new_request_gets_two_attempts_after_original_cohort_completes(self):
        self.run_at('2026-11-17T00:00:00Z', self.urls[:1])
        self.run_at('2026-11-17T03:00:00Z', self.urls[:1])
        with (self.root/'input.m3u').open('a') as f:
            f.write('#EXTINF:-1 tvg-id="A.ru",A\nhttps://example.com/new.m3u8\n')
        r,tested=self.run_at('2026-11-17T06:00:00Z', self.urls[:1])
        self.assertIn('https://example.com/new.m3u8',tested)
        self.assertEqual(r['expansion_campaign']['request_categories']['pending'],1)
        self.assertEqual(r['expansion_campaign']['completed_cohort_passes'],2)

    def test_campaign_id_change_starts_a_new_two_pass_review(self):
        self.run_at('2026-11-17T00:00:00Z')
        self.run_at('2026-11-17T03:00:00Z')
        self.cfg['expansion_campaign']['id']='second-batch'
        self.evidence['campaign_id']='second-batch'
        sv.write_json(self.root/'evidence.json',self.evidence)
        r,tested=self.run_at('2026-11-17T06:00:00Z')
        self.assertEqual(len(tested),9)
        self.assertEqual(r['expansion_campaign']['completed_cohort_passes'],1)

    def test_widespread_failure_does_not_quarantine_new_candidates(self):
        items=[{'url':f'https://example.com/control{i}.m3u8'} for i in range(12)]
        history={sv.stream_key(i):{'stable':True,'last_result':{'ok':True}} for i in items}
        unseen={'url':'https://example.com/new.m3u8'}
        campaign=Campaign({'id':'outage','environment':'ru-vps'}, {}, {'a':items+[unseen]},
                          history,{},sv.stream_key,'2026-11-17T00:00:00Z')
        results={sv.stream_key(i):{'ok':False,'checked_utc':'2026-11-17T00:00:00Z'} for i in items+[unseen]}
        campaign.record(results)
        self.assertTrue(campaign.report()['negative_trials_deferred_for_network_outage'])
        self.assertEqual(campaign.report()['completed_cohort_passes'],0)

    def test_completed_real_checker_pass_is_not_resumed_as_another_pass(self):
        visited=[]
        def checker(item,cfg):
            visited.append(item['url'])
            return {'ok':False,'reason':'fixture','pixels':0}
        self.cfg['resume_checks']=True
        with patch.object(sv,'check_stream',checker), patch.object(sv.shutil,'which',return_value='/fake/ffmpeg'), \
             patch.object(sv,'utcnow',return_value='2026-11-17T00:00:00Z'), contextlib.redirect_stdout(io.StringIO()):
            sv.build(self.cfg,tester=checker,now='2026-11-17T00:00:00Z')
            self.assertTrue(sv.read_json(self.root/'probe-progress.json',{})['completed'])
            sv.build(self.cfg,tester=checker,now='2026-11-17T00:10:00Z')
        self.assertEqual(len(visited),18)
        state=sv.read_json(self.root/'history.json',{})['expansion_campaign']
        self.assertEqual({x['samples'] for x in state['requests'].values()},{1})


if __name__ == '__main__':
    unittest.main()
