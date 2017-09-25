from HTMLParser import HTMLParser

from helpers.methods import find_reviewers

import json, re

def check_log_for_css_failures(api, build_url):
    data = api.get_screenshots_for_build(build_url)
    # Image data is the key because, there could be
    # different tests with same result.
    images = {}
    for img in data:
        images.setdefault(img['blend'], [])
        images[img['blend']].append('**%s** (test) **%s** (ref)' % \
                                    (img['test']['url'], img['ref']['url']))

    link = None
    comment = ('Hi! I was able to get the screenshots for some tests.'
               " To show the difference, I've blended the two screenshots.")
    for img in images:
        link = api.post_image_to_imgur(img)
        if not link:
            continue

        tests = ', '.join(images[img])
        comment += '\n\n - %s\n\n![](%s)' % (tests, link)

    if link is not None:
        # We've uploaded at least one image (let's post comment)
        api.post_comment(comment)


def check_failure_log(api):
    comment = api.payload['comment']['body']
    # bors's comment would be something like,
    # ":broken_heart: Test failed - [linux2](http://build.servo.org/builders/linux2/builds/2627)"
    # ... from which we get the relevant build result url
    url = re.findall(r'.*\((.*)\)', str(comment))
    if not url:
        return

    # Substitute and get the new url
    # (e.g. http://build.servo.org/json/builders/linux2/builds/2627)
    json_url = re.sub(r'(.*)(builders/.*)', r'\1json/\2', url[0])
    json_stuff = api.get_page_content(json_url)
    if not json_stuff:
        return

    build_stats = json.loads(json_stuff)
    failure_regex = r'Tests with unexpected results:\n(.*)\n</span><span'
    comments = []

    for step in build_stats['steps']:
        for name, log_url in step['logs']:
            if name != 'stdio':
                continue

            stdio = api.get_page_content(log_url)
            failures = re.findall(failure_regex, stdio, re.DOTALL)

            if not failures:
                continue

            try:
                failures = HTMLParser().unescape(failures[0])
            except UnicodeDecodeError:
                failures = HTMLParser().unescape(failures[0].decode('utf-8'))

            if 'css' in failures:
                check_log_for_css_failures(api, url)

            comment = [' ' * 4 + line for line in failures.split('\n')]
            comments.extend(comment)

    if comments:
        api.post_comment('\n'.join(comments))


def assign_reviewer(api):
    if api.payload.get('action') != 'created':
        return

    comment = api.payload['comment']['body']

    def get_approver():
        approval_regex = r'.*@bors-servo[: ]*r([\+=])([a-zA-Z0-9\-,\+]*)'
        approval = re.search(approval_regex, str(comment))

        if approval:
            if approval.group(1) == '=':    # "r=foo" or "r=foo,bar"
                reviewer = approval.group(2)
                return reviewer
            return api.sender       # fall back and assign the approver

    reviewers = get_approver()
    if reviewers:
        api.set_assignees(reviewers.split(','))
        return

    reviewers = find_reviewers(comment)
    if reviewers:
        api.set_assignees(reviewers)


def check_bors_msg(api):
    if api.sender != 'bors-servo' or api.payload.get('action') != 'created':
        return

    comment = api.payload['comment']['body']
    api.logger.debug('Checking comment by bors...')
    if 'has been approved by' in comment or 'Testing commit' in comment:
        remove_labels = ['S-awaiting-review', 'S-needs-rebase',
                         'S-tests-failed', 'S-needs-code-changes',
                         'S-needs-squash', 'S-awaiting-answer']
        api.update_labels(add=['S-awaiting-merge'], remove=remove_labels)

    elif 'Test failed' in comment:
        api.update_labels(add=['S-tests-failed'], remove=['S-awaiting-merge'])
        # Get the homu build stats url, extract the failed tests and post them!
        check_failure_log(api)

    elif 'Please resolve the merge conflicts' in comment:
        api.update_labels(add=['S-needs-rebase'], remove=['S-awaiting-merge'])


REPO_SPECIFIC_HANDLERS = {
    'servo/': [
        assign_reviewer,
    ],
    'servo/servo': [
        check_bors_msg,
    ],
}

def payload_handler(api, config):
    if api.payload.get('action') != 'created':
        return

    # Ensure that links to repos never point to master branch
    comment = str(api.payload['comment']['body'])
    matches = re.findall('github.com/(.*?)/(.*?)/(?:(blob|tree))/master', comment)

    for match in matches:
        owner, repo = match[0], match[1]
        comment_id = api.payload['comment']['id']
        head = api.get_branch_head(owner=owner, repo=repo)
        comment = re.sub(r'(github.com/%s/%s/(?:(blob|tree)))/master' % (owner, repo),
                         r'\1/%s' % head, comment)
    if matches:
        api.logger.debug('Replacing links to master branch...')
        api.edit_comment(comment_id, comment)

    other_handlers = api.get_matches_from_config(REPO_SPECIFIC_HANDLERS) or []
    for handler in other_handlers:
        handler(api)