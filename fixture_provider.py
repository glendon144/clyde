"""Offline fixture provider. All filesystem paths come from broker-owned policy."""
import re
import stat
import os

MAX_DOCUMENT = 2048
ID = re.compile(r'[a-zA-Z0-9_-]{1,64}\Z')

def fetch_fixture(task, policy):
    payload = task.get('payload')
    if (task.get('type') != 'fixture' or task.get('intent') != 'fetch' or
            not isinstance(payload, dict) or set(payload) != {'document_id'}):
        return {'ok': False, 'error': 'Invalid fixture task'}
    document_id = payload['document_id']
    if not isinstance(document_id, str) or not ID.fullmatch(document_id):
        return {'ok': False, 'error': 'Invalid fixture identifier'}
    path = policy.get('fixture_documents', {}).get(document_id)
    if not isinstance(path, str):
        return {'ok': False, 'error': 'Fixture not approved'}
    try:
        # No caller path, no URL, no shell; refuse special files and symlinks.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return {'ok': False, 'error': 'Fixture must be a regular file'}
            content = stream.read(MAX_DOCUMENT + 1)
        if len(content) > MAX_DOCUMENT:
            return {'ok': False, 'error': 'Fixture too large'}
        text = content.decode('utf-8')
    except (OSError, UnicodeError):
        return {'ok': False, 'error': 'Fixture unavailable'}
    return {'ok': True, 'result': text, 'metadata': {'source_id': document_id, 'offline': True}}
