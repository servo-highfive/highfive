from ..runner.config import get_logger
from ..runner.request import request_with_requests

import random

DEFAULTS = ['is_pull', 'pull_url', 'is_open', 'creator', 'last_updated', 'number',
            'sender', 'owner', 'repo', 'current_label', 'assignee', 'comment', 'labels']

class APIProvider(object):
    '''
    The interface used by `GithubAPIProvider` object to take actions based on
    the incoming payload. API provider objects are tied to payloads.

    Once the runner receives a payload, an API provider object is created and
    sent to all handlers (through the installation and synchronization managers).
    This object is supposed to provide encapsulation for commonly used payload
    attributes and methods, so that the handlers don't have to worry about
    extracting them every time.
    '''

    imgur_post_url = 'https://api.imgur.com/3/image'

    def __init__(self, config, payload):
        self.name = config.name
        self.config = config
        self.payload = payload
        self.logger = get_logger(__name__)

        for attr in DEFAULTS:
            setattr(self, attr, None)

        if payload.get('repository'):
            self.owner = payload['repository']['owner']['login']
            self.repo = payload['repository']['name']
        else:
            self.logger.error('Error getting repository information from payload.')

        if payload.get('sender'):
            self.sender = payload['sender']['login'].lower()

        if payload.get('pull_request'):
            self.is_pull = True
            self.pull_url = payload['pull_request']['url']
            self.creator = payload['pull_request']['user']['login'].lower()
            self.is_open = payload['pull_request']['state'] == 'open'
            self.last_updated = payload['pull_request'].get('updated_at')
            self.number = payload['pull_request']['number']
        elif payload.get('issue'):
            self.is_pull = False
            self.creator = payload['issue']['user']['login'].lower()
            self.is_open = payload['issue']['state'].lower() == 'open'
            self.last_updated = payload['issue'].get('updated_at')
            self.number = payload['issue']['number']
            self.labels = payload['issue']['labels']

        if payload.get('comment'):
            self.comment = payload['comment']['body'].encode('utf-8')
            # Github API always shows comments as "issue comments" - there's no PR comments.
            # We have to find manually.
            issue = self.payload.get('issue', {})
            self.is_pull = issue.get("pull_request") is not None

    def post_image_to_imgur(self, base64_data, json_request=request_with_requests):
        '''
        If the client ID is present in configuration, then this method can be used to
        upload base64-encoded image data (anonymously) to Imgur and returns the permalink.
        '''

        if self.config.imgur_client_id is None:
            self.logger.error('Imgur client ID has not been set!')
            return

        headers = {'Authorization': 'Client-ID %s' % self.config.imgur_client_id}

        resp = json_request('POST', self.imgur_post_url,
                            data={'image': base64_data},
                            headers=headers)
        if resp.code != 200:
            self.logger.error('Error posting image to Imgur! Response: %s' % resp.data)
            return

        if not resp.is_json():
            self.logger.error('Cannot parse response from Imgur! Response: %s' % resp.data)
            return

        return resp.data['data']['link']

    # Overridable methods.

    def get_branch_head(self, branch):
        raise NotImplementedError

    def edit_comment(self, _id, comment):
        raise NotImplementedError

    def set_assignees(self, assignees):
        raise NotImplementedError

    def get_labels(self):
        raise NotImplementedError

    def replace_labels(self, labels=[]):
        raise NotImplementedError

    def post_comment(self, comment):
        raise NotImplementedError

    def get_page_content(self, url):
        raise NotImplementedError

    # Default methods depending on the overriddable methods.

    def update_labels(self, add=[], remove=[]):
        '''
        This fetches the labels corresponding to the given payload, adds/subtracts
        labels based on the method call, and finally replaces all the labels in the issue/PR.
        Since this calls `get_labels` every time this method is called, it's up to the implementor
        to ensure that proper caching is done in that method.
        '''

        to_lower = lambda label: label.lower()      # str.lower doesn't work for unicode
        current_labels = set(map(to_lower, self.get_labels()))
        current_labels.update(map(to_lower, add))
        current_labels.difference_update(map(to_lower, remove))
        self.replace_labels(list(current_labels))