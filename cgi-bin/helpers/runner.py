from StringIO import StringIO
from datetime import datetime
from dateutil.parser import parse as datetime_parse
from gzip import GzipFile
from jose import jwt
from time import sleep

from api_provider import GithubAPIProvider
from database import create_db
from methods import AVAILABLE_EVENTS, get_handlers, get_logger

import hashlib, hmac, itertools, json, os, time, requests

SYNC_HANDLER_SLEEP_SECS = 3600

# Implementation of digest comparison from Django
# https://github.com/django/django/blob/0ed7d155635da9f79d4dd67e4889087d3673c6da/django/utils/crypto.py#L96-L105
def compare_digest(val_1, val_2):
    result = 0
    if len(val_1) != len(val_2):
        return False

    for x, y in zip(val_1, val_2):
        result |= ord(x) ^ ord(y)

    return result == 0


class InstallationHandler(object):
    base_url = 'https://api.github.com'
    installation_url = base_url + '/installations/%s/access_tokens'
    rate_limit_url = base_url + '/rate_limit'
    headers = {
        'Content-Type': 'application/json',
        # integration-specific header
        'Accept': 'application/vnd.github.machine-man-preview+json',
        'Accept-Encoding': 'gzip, deflate'
    }

    def __init__(self, runner, inst_id):
        self.runner = runner
        self.logger = runner.logger
        self._id = inst_id
        self.installation_url = self.installation_url % inst_id
        # Github offers 5000 requests per hour (per installation) for an integration
        # (all these will be overridden when we sync the token and "/rate_limit" endpoint)
        self.remaining = self.runner.config.get('remaining', 0)
        self.reset_time = self.runner.config.get('reset', int(time.time()) - 60)
        self.next_token_sync = datetime_parse(self.runner.config.get('expires_at', '%s' % datetime.now()))
        self.token = self.runner.config.get('token')

    def sync_token(self):
        now = datetime.now(self.next_token_sync.tzinfo)     # should be timezone-aware version
        if now >= self.next_token_sync:
            self.logger.debug('Getting auth token with JWT from PEM key...')
            # https://developer.github.com/early-access/integrations/authentication/#jwt-payload
            since_epoch = int(time.time())
            auth_payload = {
                'iat': since_epoch,
                'exp': since_epoch + 600,       # 10 mins expiration for JWT
                'iss': self.runner.integration_id,
            }

            auth = 'Bearer %s' % jwt.encode(auth_payload, self.runner.pem_key, 'RS256')
            resp = self._request(auth, 'POST', self.installation_url)
            self.token = resp['token']      # installation token (expires in 1 hour)
            self.logger.debug('Token expires on %s', resp['expires_at'])
            self.next_token_sync = datetime_parse(resp['expires_at'])

    def wait_time(self):
        now = int(time.time())
        if now >= self.reset_time:
            auth = 'token %s' % self.token
            data = self._request(auth, 'GET', self.rate_limit_url)
            self.reset_time = data['rate']['reset']
            self.remaining = data['rate']['remaining']
            self.logger.debug('Current time: %s, Remaining requests: %s, Reset time: %s',
                              now, self.remaining, self.reset_time)
        return (self.reset_time - now) / float(self.remaining)          # (uniform) wait time per request

    def _request(self, auth, method, url, data=None):       # not supposed to be called by any handler
        self.headers['Authorization'] = auth
        data = json.dumps(data) if data is not None  else data
        req_method = getattr(requests, method.lower())              # hack
        self.logger.info('%s: %s (data: %s)', method, url, data)
        resp = req_method(url, data=data, headers=self.headers)
        data, code = resp.text, resp.status_code

        if code < 200 or code >= 300:
            self.logger.error('Got a %s response: %r', code, data)
            raise Exception

        if resp.headers.get('Content-Encoding') == 'gzip':
            try:
                fd = GzipFile(fileobj=StringIO(data))
                data = fd.read()
            except IOError:
                self.logger.debug('Cannot decode with Gzip, Trying to load JSON from raw response...')
                pass
        try:
            return json.loads(data)
        except (TypeError, ValueError):         # stuff like 'diff' will be a string
            self.logger.debug('Cannot decode JSON, Passing the payload as string...')
            return data

    def update_config(self):
        self.runner.config[self._id] = {
            'remaining': self.remaining,
            'expires_at': '%s' % self.next_token_sync,
            'reset': self.reset_time,
            'token': self.token,
        }

    def queue_request(self, method, url, data=None):
        self.sync_token()
        interval = self.wait_time()
        sleep(interval)
        self.remaining -= 1
        self.update_config()
        auth = 'token %s' % self.token
        return self._request(auth, method, url, data)

    def add(self, payload, event):
        api = GithubAPIProvider(self.runner.name, payload, self.queue_request)
        for _, handler in get_handlers(event):
            handler(api)


class SyncHandler(object):
    def __init__(self, inst_handler):
        self._id = inst_handler._id
        self.runner = inst_handler.runner   # FIXME: Too many indirections (refactor them someday)
        self.request = inst_handler.queue_request

    def post(self, payload):
        # This relies on InstallationHandler's request queueing
        api = GithubAPIProvider(self.runner.name, payload, self.request)
        # Since these handlers don't belong to any particular event, they're supposed to
        # exist in `issues` and `pull_request` (with 'sync' flag enabled in their config)
        for path, handler in itertools.chain(get_handlers('issues', sync=True),
                                             get_handlers('pull_request', sync=True)):
            handler(api, self.runner.db, self._id, os.path.basename(path))


class Runner(object):
    def __init__(self, config):
        self.name = config['name']
        self.logger = get_logger(__name__)
        self.dump_path = config['dump_path']
        self.enabled_events = config.get('enabled_events', [])
        self.integration_id = config['integration_id']
        self.secret = str(config.get('secret', ''))
        self.installations = {}
        self.sync_runners = {}
        self.db = create_db(config)

        if not self.enabled_events:
            self.enabled_events = AVAILABLE_EVENTS

        with open(config['pem_file'], 'r') as fd:
            self.pem_key = fd.read()

    def verify_payload(self, header_sign, payload_fd):
        try:
            payload = json.load(payload_fd)
        except Exception as err:
            self.logger.debug('Cannot decode payload JSON: %s', err)
            return 400, None

        # app "secret" key is optional, but it makes sure that you're getting payloads
        # from Github. If a third-party found your POST endpoint, then anyone can send a
        # cooked-up payload, and your bot will respond and make API requests to Github.
        #
        # In python, you can do something like this,
        # >>> import random
        # >>> print ''.join(map(chr, random.sample(range(32, 127), 32)))
        #
        # ... which will generate a 32-byte key in the ASCII range.

        if self.secret:
            hash_func, signature = (header_sign + '=').split('=')[:2]
            hash_func = getattr(hashlib, hash_func)     # for now, sha1
            msg_auth_code = hmac.new(self.secret, raw_payload, hash_func)
            hashed = msg_auth_code.hexdigest()

            if not compare_digest(signature, hashed):
                self.logger.debug('Invalid signature!')
                return 403, None

            self.logger.info("Payload's signature has been verified!")
        else:
            self.logger.info("Payload's signature can't be verified without secret key!")

        return None, payload

    def set_installation(self, inst_id):
        self.installations.setdefault(inst_id, InstallationHandler(self, inst_id))
        inst_handler = self.installations[inst_id]
        self.sync_runners.setdefault(inst_id, SyncHandler(inst_handler))

    def handle_payload(self, payload, event):
        inst_id = payload['installation']['id']
        self.set_installation(inst_id)
        if event in self.enabled_events:
            if self.name in payload.get('sender', {'login': None})['login']:
                self.logger.debug('Skipping payload for event %r sent by self', event)
            else:
                self.logger.info('Received payload for for installation %s'
                                 ' (event: %s, action: %s)', inst_id, event, payload.get('action'))
                self.installations[inst_id].add(payload, event)
        else:   # no matching events
            self.logger.info("(event %s, action: %s) doesn't match any enabled events (installation %s)."
                             " Skipping payload-dependent handlers...", event, payload.get('action'), inst_id)
        # We pass all the payloads through sync handlers regardless of the event (they're unconstrained)
        self.sync_runners[inst_id].post(payload)

    def poke_data(self):
        self.logger.debug('Poking available installation data...')
        for _id in self.db.get_installations():
            self.set_installation(_id)

        for _id, sync_runner in self.sync_runners.items():
            # poke all the sync handlers (of all installations) on hand with fake payloads
            self.logger.info('Poking runner for installation %s with empty payload', _id)
            sync_runner.post({})

    def start_sync(self):
        self.logger.info('Spawning a new thread for sync handlers...')
        for _id in self.db.get_installations():
             self.set_installation(_id)

        while True:
            start = int(time.time())
            for _id, sync_runner in self.sync_runners.items():
                # poke all the sync handlers (of all installations) on hand with fake payloads
                self.logger.info('Poking runner for installation %s with empty payload', _id)
                sync_runner.post({})

            end = int(time.time())
            interval = SYNC_HANDLER_SLEEP_SECS - (end - start)
            self.logger.debug('Going to sleep for %s seconds', interval)
            sleep(interval)