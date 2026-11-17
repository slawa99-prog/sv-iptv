"""Publication safety is separate from decoder success and campaign coverage."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sv_iptv as sv


class CandidateQualityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for field in ('ROOT', 'VALIDATION_ROOT'):
            p = patch.object(sv, field, self.root)
            p.start(); self.addCleanup(p.stop)
        p = patch.object(sv, 'visual_signature', return_value='quality-test-visual-criteria')
        p.start(); self.addCleanup(p.stop)
        self.channels = [{'id': 'A.ru', 'name': 'New A', 'group': 'ТВ', 'identity_review_required': True},
                         {'id': 'B.ru', 'name': 'Old B', 'group': 'ТВ'}]
        sv.write_json(self.root/'channels.json', self.channels)
        self.items = [{'url': 'https://example.com/a.m3u8', 'headers': {}},
                      {'url': 'https://example.com/b.m3u8', 'headers': {}}]
        (self.root/'input.m3u').write_text(sv.render_m3u(list(zip(self.channels, self.items)), {}))
        self.cfg = {'sources': ['input.m3u'], 'minimum_channels': 1, 'workers': 1,
                    'require_fresh_remote_source': False, 'check_environment': 'ru-vps',
                    'candidate_quality': {'enabled': True, 'require_stable': True, 'identity_reviews': []}}

    def run_at(self, at='2026-11-17T00:00:00Z', ok=True):
        visited = []
        def tester(item, cfg):
            visited.append(item['url'])
            return {'ok': ok, 'reason': 'fixture', 'media_url': item['url'],
                    'visual_criteria': sv.visual_signature(),
                    'frame_inspection': {'state': 'no_known_placeholder'}}
        with contextlib.redirect_stdout(io.StringIO()):
            report = sv.build(self.cfg, tester=tester, now=at)
        return report, visited

    def review(self, channel='A.ru', item=None):
        return {'channel_id': channel, 'stream_key': sv.stream_key(item or self.items[0]),
                'reviewed_utc': '2026-11-16T00:00:00Z', 'reviewer': 'fixture reviewer',
                'environment': 'pc', 'evidence': 'fixture frame and audio review',
                'identity_matches': True, 'russian_audio': True, 'no_placeholder': True}

    def test_decoder_success_is_recorded_but_new_identity_is_held(self):
        report, _ = self.run_at()
        self.assertEqual([c['id'] for c in report['selected']], ['B.ru'])
        self.assertEqual(report['candidate_quality']['held_channels'], 1)
        self.assertEqual(report['candidate_quality']['held'][0]['technical_streams_passed'], 1)
        history = sv.read_json(self.root/'history.json', {})
        self.assertTrue(history['streams'][sv.stream_key(self.items[0])]['last_result']['ok'])
        self.assertNotIn('A.ru', (self.root/'tv.m3u').read_text())

    def test_identity_review_requires_independent_stability_too(self):
        self.cfg['candidate_quality']['identity_reviews'] = [self.review()]
        self.run_at('2026-11-15T00:00:00Z')
        report, _ = self.run_at('2026-11-15T06:00:00Z')
        self.assertEqual(report['unique_channels_passed'], 1)
        for hour in (12,18): self.run_at(f'2026-11-15T{hour:02d}:00:00Z')
        for hour in (0,6,12): self.run_at(f'2026-11-16T{hour:02d}:00:00Z')
        report, _ = self.run_at('2026-11-16T18:00:00Z')
        a = next(c for c in report['selected'] if c['id']=='A.ru')
        self.assertEqual((a['samples'], a['observation_hours'], a['rating']), (8,42,'observed_stable'))

    def test_review_is_bound_to_channel_request_headers_and_evidence(self):
        valid = self.review()
        now='2026-11-17T00:00:00Z'
        for change in [{'channel_id':'B.ru'}, {'stream_key':sv.stream_key({**self.items[0], 'headers':{'Referer':'https://other.example/'}})},
                       {'russian_audio':False}, {'identity_matches':False}, {'no_placeholder':False},
                       {'reviewer':''}, {'evidence':''}, {'reviewed_utc':'2026-11-18T00:00:00Z'},
                       {'reviewed_utc':'bad'}, {'environment':'github-actions'}]:
            with self.subTest(change=change):
                self.cfg['candidate_quality']['identity_reviews']=[{**valid, **change}]
                self.assertIsNotNone(sv.candidate_review_reason(self.channels[0],self.items[0],{'stable':True},self.cfg,now))
        self.cfg['candidate_quality']['identity_reviews']=[valid]
        self.assertIsNone(sv.candidate_review_reason(self.channels[0],self.items[0],{'stable':True},self.cfg,now))
        self.assertIsNotNone(sv.candidate_review_reason(self.channels[0],{'url':'https://example.com/replacement'}, {'stable':True},self.cfg,now))

    def test_conflicting_key_cannot_win_by_catalog_order(self):
        for c in self.channels:c.pop('identity_review_required',None)
        sv.write_json(self.root/'channels.json',self.channels)
        (self.root/'input.m3u').write_text(sv.render_m3u([(c,self.items[0]) for c in self.channels],{}))
        report, _ = self.run_at()
        self.assertEqual(report['unique_channels_passed'],0)
        self.assertEqual(report['candidate_quality']['identity_conflict_requests'],1)
        self.cfg['candidate_quality']['identity_reviews']=[self.review('B.ru')]
        report, _ = self.run_at('2026-11-17T03:00:00Z')
        self.assertEqual([c['id'] for c in report['selected']],['B.ru'])

    def test_domestic_national_language_requires_explicit_identity_and_audio_review(self):
        channel = {**self.channels[0], 'country': 'RU'}
        quality = self.cfg['candidate_quality']
        quality['allow_domestic_languages'] = True
        review = {**self.review(), 'russian_audio': False,
                  'domestic_channel_confirmed': True, 'domestic_language_audio': True,
                  'audio_language': 'bua'}
        quality['identity_reviews'] = [review]
        now = '2026-11-17T00:00:00Z'
        self.assertIsNone(sv.candidate_review_reason(channel, self.items[0], {'stable': True}, self.cfg, now))
        for changes in ({'domestic_channel_confirmed': False}, {'domestic_language_audio': False},
                        {'audio_language': ''}, {'audio_language': None}, {'identity_matches': False},
                        {'evidence': ''}, {'no_placeholder': False}, {'environment': 'github-actions'}):
            with self.subTest(changes=changes):
                quality['identity_reviews'] = [{**review, **changes}]
                self.assertIsNotNone(sv.candidate_review_reason(channel, self.items[0], {'stable': True}, self.cfg, now))
        quality['identity_reviews'] = [review]
        for country in ('MN', None):
            self.assertIsNotNone(sv.candidate_review_reason({**channel, 'country': country}, self.items[0], {'stable': True}, self.cfg, now))
        self.assertEqual(sv.candidate_review_reason(channel, self.items[0], {'stable': False}, self.cfg, now),
                         'independent_stability_history_required')

    def test_merged_variant_of_existing_channel_still_requires_review(self):
        q=self.cfg['candidate_quality'];q['review_required_request_keys']=[sv.stream_key(self.items[1])]
        self.assertIsNotNone(sv.candidate_review_reason(self.channels[1], self.items[1], {'stable':True}, self.cfg, '2026-11-17T00:00:00Z'))
        q['identity_reviews']=[self.review('B.ru', self.items[1])]
        self.assertIsNotNone(sv.candidate_review_reason(self.channels[1], self.items[1], {'stable':False}, self.cfg, '2026-11-17T00:00:00Z'))

    def test_retention_guard_and_backups_cannot_keep_unreviewed_candidate(self):
        self.cfg['publication_mode']='reviewed-local'
        original=b'#EXTM3U\n# manually reviewed home playlist\n'
        (self.root/'tv.m3u').write_bytes(original)
        (self.root/'cloud.m3u').write_text((self.root/'input.m3u').read_text())
        sv.write_json(self.root/'status.json',{'last_published_count':2})
        sv.write_json(self.root/'history.json',{'streams':{},'last_selected':{'a.ru':sv.stream_key(self.items[0]),'b.ru':sv.stream_key(self.items[1])}})
        sv.write_json(self.root/'backups.json',{'channels':{'A.ru':[self.items[0]],'B.ru':[self.items[1]]}})
        report,_=self.run_at(ok=False)
        self.assertEqual(report['publication'],'blocked')
        self.assertEqual(report['candidate_quality']['retained_playlist_entries_removed'],1)
        self.assertEqual(report['last_published_count'],1)
        self.assertEqual((self.root/'tv.m3u').read_bytes(),original)
        self.assertEqual([i['attrs']['tvg-id'] for i in sv.parse_m3u((self.root/'cloud.m3u').read_text())],['B.ru'])
        self.assertEqual(set(sv.read_json(self.root/'history.json',{})['last_selected']),{'b.ru'})
        self.assertEqual(sv.read_json(self.root/'backups.json',{})['channels']['A.ru'],[])

    def configure_frozen(self):
        self.cfg['channel_scope']={'mode':'reviewed-allowlist','allowed_ids':['A.ru','B.ru']}
        self.cfg['check_exclusions']={'channel_ids':['Excluded.ru']}
        q=self.cfg['candidate_quality'];q['id_aliases']={'Alias.ru':'B.ru','Reintroduced.ru':'Excluded.ru'}
        q['rejected_urls']={'https://example.com/blocked/index.m3u8':'slate'}
        with (self.root/'input.m3u').open('a') as f:
            for cid,name,url in [('Alias.ru','Alias','https://example.com/alias'),('Reintroduced.ru','Excluded','https://example.com/excluded'),('A.ru','New A','https://example.com/blocked/index.m3u8')]:
                f.write(f'#EXTINF:-1 tvg-id="{cid}",{name}\n{url}\n')
        data=(self.root/'input.m3u').read_bytes()
        self.cfg['expansion_campaign']={'enabled':True,'id':'quality-test','environments':['ru-vps'], 'evidence_file':'evidence.json',
             'frozen_queue':{'path':'input.m3u','sha256':hashlib.sha256(data).hexdigest(),'request_count':5}}
        sv.write_json(self.root/'evidence.json',{'campaign_id':'quality-test','pc_success_request_keys':[sv.stream_key(self.items[0])]})

    def test_retired_candidates_remain_in_frozen_two_pass_audit(self):
        self.configure_frozen()
        r,tested=self.run_at(ok=False)
        self.assertEqual(len(tested),5)
        self.assertEqual(r['expansion_campaign']['verification_only_requests'],2)
        r,_=self.run_at('2026-11-17T01:00:00Z',ok=False)
        self.assertEqual(r['expansion_campaign']['completed_current_passes'],1)
        r,_=self.run_at('2026-11-17T03:00:00Z',ok=False)
        self.assertEqual(r['expansion_campaign']['completed_cohort_passes'],2)
        self.assertEqual(r['expansion_campaign']['completed_current_passes'],2)
        self.assertEqual(r['expansion_campaign']['request_categories'],{'pending':0,'monitor':0,'pc_only':1,'quarantined':4})
        self.assertEqual(r['expansion_campaign']['weekly_rechecked_requests'],0)
        r,tested=self.run_at('2026-11-17T06:00:00Z',ok=False)
        self.assertEqual(tested,[])
        r,tested=self.run_at('2026-11-25T06:00:00Z',ok=False)
        self.assertEqual(len(tested),5)
        self.assertEqual(r['expansion_campaign']['completed_current_passes'],2)
        self.assertEqual(r['unique_channels_passed'],0)
        self.assertEqual(r['expansion_campaign']['weekly_rechecked_requests'],5)
        self.assertEqual(r['expansion_campaign']['weekly_recheck_observations'],5)

    def test_aliases_preserve_variants_without_reviving_excluded_identity(self):
        self.configure_frozen()
        found,excluded=sv.group_candidates(sv.parse_m3u((self.root/'input.m3u').read_text()),self.channels,self.cfg)
        self.assertEqual(len(found['b.ru']),2)
        self.assertEqual(excluded['excluded_canonical_identity'],1)
        self.assertEqual(excluded['rejected_placeholder_endpoint'],1)

    def test_frozen_queue_change_fails_before_measurement(self):
        self.configure_frozen()
        with (self.root/'input.m3u').open('a') as f:f.write('\n')
        with self.assertRaisesRegex(ValueError,'hash mismatch'):self.run_at()

    def add_extra_queue(self):
        extra={'url':'https://example.com/new-extra.m3u8','headers':{}}
        data=sv.render_m3u([(self.channels[0],extra)],{}).encode()
        (self.root/'extra.m3u').write_bytes(data)
        campaign=self.cfg['expansion_campaign']
        campaign['additional_frozen_queues']=[{'path':'extra.m3u','sha256':hashlib.sha256(data).hexdigest(),'request_count':1,'batch_id':'extra-test'}]
        campaign['frozen_union_request_count']=6
        self.cfg['sources'].append('extra.m3u')
        return extra

    def test_completed_old_cohort_does_not_complete_additional_queue(self):
        self.configure_frozen()
        self.run_at('2026-11-17T00:00:00Z',ok=False)
        r,_=self.run_at('2026-11-17T03:00:00Z',ok=False)
        self.assertEqual(r['expansion_campaign']['completed_frozen_queue_passes'],2)
        extra=self.add_extra_queue()
        r,tested=self.run_at('2026-11-17T06:00:00Z',ok=False)
        e=r['expansion_campaign']
        self.assertEqual(tested,[extra['url']])
        self.assertEqual((e['completed_cohort_passes'],e['completed_frozen_queue_passes']),(2,1))
        self.assertEqual((e['cohort_requests'],e['frozen_queue_requests']),(5,6))
        self.assertEqual(len(e['frozen_queues']),2)
        r,_=self.run_at('2026-11-17T07:00:00Z',ok=False)
        self.assertEqual(r['expansion_campaign']['completed_frozen_queue_passes'],1)
        r,_=self.run_at('2026-11-17T09:00:00Z',ok=False)
        self.assertEqual(r['expansion_campaign']['completed_frozen_queue_passes'],2)
        history=sv.read_json(self.root/'history.json',{})['expansion_campaign']
        self.assertTrue(all(v['samples']==2 for v in history['requests'].values()))
        self.assertEqual(r['expansion_campaign']['request_categories']['pc_only'],1)

    def test_additional_queue_hash_and_union_count_are_validated(self):
        self.configure_frozen();self.add_extra_queue()
        campaign=self.cfg['expansion_campaign']
        campaign['frozen_union_request_count']=7
        with self.assertRaisesRegex(ValueError,'union count mismatch'):self.run_at()
        campaign['frozen_union_request_count']=6
        with (self.root/'extra.m3u').open('a') as f:f.write('\n')
        with self.assertRaisesRegex(ValueError,'hash mismatch'):self.run_at()

    def test_overlapping_queue_does_not_create_duplicate_trials(self):
        self.configure_frozen()
        campaign=self.cfg['expansion_campaign']
        campaign['additional_frozen_queues']=[dict(campaign['frozen_queue'])]
        campaign['frozen_union_request_count']=5
        r,tested=self.run_at(ok=False)
        self.assertEqual(len(tested),5)
        self.assertEqual(r['expansion_campaign']['frozen_queue_requests'],5)


if __name__=='__main__':unittest.main()
