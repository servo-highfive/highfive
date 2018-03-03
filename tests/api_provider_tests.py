from highfive.runner import Configuration, Response
from highfive.api_provider.interface import APIProvider, DEFAULTS

from unittest import TestCase


def create_config():
    config = Configuration()
    config.name = 'test_app'
    config.imgur_client_id = None
    return config


class APIProviderTests(TestCase):
    def test_api_init(self):
        '''The default interface will only initialize the app name and payload.'''

        config = Configuration()
        config.name = 'test_app'
        api = APIProvider(config=config, payload={})
        self.assertEqual(api.name, 'test_app')
        self.assertEqual(api.payload, {})
        self.assertEqual(api.config, config)

        for attr in DEFAULTS:
            self.assertTrue(getattr(api, attr) is None)

    def test_api_issue_payload(self):
        '''
        If the payload is related to an issue (or an issue comment in an issue/PR),
        then this should've initialized the commonly used issue-related stuff.
        '''

        payload = {
            'issue': {
                'labels': [],
                'user': {
                    'login': 'foobar'
                },
                'state': 'open',
                'number': 200,
                'updated_at': '1970-01-01T00:00:00Z'
            }
        }

        api = APIProvider(config=create_config(), payload=payload)
        self.assertEqual(api.payload, payload)
        self.assertFalse(api.is_pull)
        self.assertTrue(api.is_open)
        self.assertEqual(api.creator, 'foobar')
        self.assertEqual(api.last_updated, payload['issue']['updated_at'])
        self.assertEqual(api.number, 200)
        self.assertTrue(api.pull_url is None)

    def test_api_pr_payload(self):
        '''
        If the payload is related to a PR, then the commonly used PR attributes
        should've been initialized.
        '''

        payload = {
            'pull_request': {
                'user': {
                    'login': 'foobar'
                },
                'state': 'open',
                'number': 50,
                'url': 'some url',
                'updated_at': '1970-01-01T00:00:00Z'
            }
        }

        api = APIProvider(config=create_config(), payload=payload)
        self.assertEqual(api.payload, payload)
        self.assertTrue(api.is_open)
        self.assertTrue(api.is_pull)
        self.assertEqual(api.creator, 'foobar')
        self.assertEqual(api.last_updated, payload['pull_request']['updated_at'])
        self.assertEqual(api.number, 50)
        self.assertEqual(api.pull_url, 'some url')

    def test_api_imgur_upload(self):
        '''Test Imgur API upload'''

        config = create_config()
        api = APIProvider(config=config, payload={})
        resp = api.post_image_to_imgur('some data')
        self.assertTrue(resp is None)       # No client ID - returns None

        config.imgur_client_id = 'foobar'

        def test_valid_request(method, url, data, headers):
            self.assertEqual(headers['Authorization'], 'Client-ID foobar')
            self.assertEqual(method, 'POST')
            self.assertEqual(url, 'https://api.imgur.com/3/image')
            self.assertEqual(data, {'image': 'some data'})
            return Response(data={'data': {'link': 'hello'}})

        tests = [
            (test_valid_request, 'hello'),
            (lambda method, url, data, headers: Response(data='', code=400), None),
            (lambda method, url, data, headers: Response(data=''), None)
        ]

        for func, expected in tests:
            resp = api.post_image_to_imgur('some data', json_request=func)
            self.assertEqual(resp, expected)