import unittest
from harvest_catalog import disposition, scope_hint, obvious_rejection


class WideIntakeTests(unittest.TestCase):
    def item(self, name='Буряад ТВ', **values):
        return {'url': 'https://media.example.tv/live/national/index.m3u8',
                'name': name, 'attrs': {}, 'headers': {}, **values}

    def test_national_and_unidentified_names_are_retained(self):
        for name in ('Буряад ТВ', 'Тамыр', 'Шаян ТВ', 'НВК Саха', 'Туган Тел', 'Unknown TV', 'Канал +2'):
            self.assertEqual(disposition(self.item(name)), 'tv_candidate')
        self.assertEqual(scope_hint(self.item(), {}, set(), {}, set()), ('unidentified_requires_review', None))

    def test_domestic_metadata_does_not_require_russian_spelling(self):
        item = self.item('National TV', attrs={'tvg-id': 'National.ru'})
        self.assertEqual(scope_hint(item, {'national.ru': {'country': 'RU'}}, set(), {}, set()),
                         ('russian_domestic_metadata', 'national.ru'))
        self.assertEqual(scope_hint(item, {'national.ru': {'country': 'RU'}}, set(), {}, {'national.ru'})[0],
                         'previously_excluded_identity')

    def test_credentials_and_test_data_are_not_counted_as_tv(self):
        for url in ('https://media.example.tv/get.php?username=account&password=secret',
                    'https://media.example.tv/live/account/secret/123.ts'):
            self.assertEqual(disposition(self.item(url=url)), 'account_credential_redacted')
        self.assertEqual(disposition(self.item(), 'https://github.com/a/b/tests/fixtures/large.m3u'), 'synthetic_fixture')
        self.assertEqual(disposition(self.item(), 'https://github.com/a/b/scan_channels_named.m3u'), 'unverified_numeric_enumeration')

    def test_real_tv_on_camera_domain_and_expired_public_url_are_not_lost(self):
        self.assertEqual(disposition(self.item(url='https://media.camera/abaza/index.m3u8')), 'tv_candidate')
        self.assertEqual(disposition(self.item(url='https://media.example.tv/live/index.m3u8?expires=1500000000')), 'tv_candidate')

    def test_format_buckets_do_not_claim_playback_failure(self):
        self.assertEqual(disposition(self.item(unsupported=True)), 'drm_requires_authorized_player')
        self.assertEqual(disposition(self.item(url='https://media.example.tv/movie.mp4')), 'on_demand_video')
        self.assertEqual(disposition(self.item(attrs={'group-title': 'New Radio Stations'})), 'audio_or_radio')
        self.assertEqual(disposition(self.item(name='', url='https://cdn.example.tv/channel/tracks-v1a1/2026/10/30/09/16/17-06000.ts')),
                         'hls_media_segments')

    def test_unresolved_templates_and_invalid_authorities_are_rejected(self):
        for url in ('https://{{your_udpxy_address}}/udp/233.50.202.1:5140',
                    'http://provider-url/live.m3u8', 'https://cityeden..tv/live.m3u8',
                    'http://203.0.113.1/live.m3u8', 'https://media.example.tv:70000/live.m3u8',
                    'https://media.example.tv:bad/live.m3u8', 'https://router.lan/tv.m3u8'):
            self.assertIsNotNone(obvious_rejection(self.item(url=url)))

    def test_public_ipv6_idn_dynamic_urls_and_national_languages_survive(self):
        for url in ('https://[2606:4700:4700::1111]/stream.php',
                    'https://пример.рф/live.m3u8', 'https://media.example.tv/live/404.m3u8',
                    'https://media.example.tv/stream.php?expires=1500000000',
                    'https://media.example.tv/channel.m3u8.jpg'):
            self.assertIsNone(obvious_rejection(self.item('Буряад ТВ', url=url)))

    def test_known_slates_are_removed_by_exact_endpoint_not_arbitrary_name(self):
        self.assertEqual(obvious_rejection(self.item(url='http://s7.iptv-24.net/blocked/index.m3u8')),
                         'known_blocked_slate')
        self.assertEqual(obvious_rejection(self.item(url='https://proxy.example.tv/stream8.cinerama.uz/blocked/index.m3u8')),
                         'known_blocked_slate_proxy')
        self.assertIsNone(obvious_rejection(self.item(url='https://media.example.tv/blocked/index.m3u8')))


if __name__ == '__main__':
    unittest.main()
