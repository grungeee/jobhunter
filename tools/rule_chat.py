#!/usr/bin/env python3
"""Preview and approve a single Jobhunter writing rule from a Hermes conversation."""

import argparse
import fcntl
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time

import generate
import review

Error = generate.GenerateError
PENDING = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local' / 'state') / 'jobhunter' / 'rule-proposals'
TTL = 600


def propose(text, *, masculine=False):
    rule = generate.MASCULINE_TITLES if masculine else review.clean(text, 1000)
    base = generate.generation_profile()
    if rule in base['application_preferences']:
        return {'status': 'already_active', 'rule': rule}
    PENDING.mkdir(parents=True, exist_ok=True, mode=0o700)
    if PENDING.is_symlink() or PENDING.stat().st_mode & 0o077:
        raise Error('The proposal directory must be private.')
    now = time.time()
    for expired in PENDING.glob('*.json'):
        if expired.stat().st_mtime < now - TTL * 2:
            expired.unlink(missing_ok=True)
    proposal_id, challenge = secrets.token_hex(12), secrets.token_hex(8)
    expires = int(now) + TTL
    ticket = {'rule': rule, 'challenge_hash': review.digest(challenge), 'base': review.digest(base), 'expires_at': expires}
    path = PENDING / (proposal_id + '.json')
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w', encoding='utf-8') as handle:
        json.dump(ticket, handle, ensure_ascii=False)
    return {'status': 'pending_approval', 'rule': rule, 'proposal_id': proposal_id,
            'challenge': challenge, 'expires_at': expires}


def apply(proposal_id, challenge, *, now=None):
    if not re.fullmatch(r'[a-f0-9]{24}', proposal_id or ''):
        raise Error('Invalid rule proposal ID.')
    path = PENDING / (proposal_id + '.json')
    try:
        handle = path.open(encoding='utf-8')
    except FileNotFoundError as exc:
        raise Error('Rule proposal is missing or has already been used.') from exc
    with handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        if not path.exists():
            raise Error('Rule proposal has already been used.')
        ticket = json.load(handle)
        if ((time.time() if now is None else now) >= ticket['expires_at']
                or not hmac.compare_digest(ticket['challenge_hash'], review.digest(challenge))):
            raise Error('Rule approval expired or did not match the proposal.')
        base = generate.generation_profile()
        if not hmac.compare_digest(ticket['base'], review.digest(base)):
            raise Error('The profile changed since preview. Request a new proposal and approval.')
        rule = review.clean(ticket['rule'], 1600)
        proposed = {**base, 'application_preferences': [*base['application_preferences'], rule]}
        review.save_profile(base, generate.validate_profile(proposed))
        path.unlink()
        if rule not in generate.generation_profile()['application_preferences']:
            raise Error('Could not verify the saved rule.')
    return {'status': 'active', 'rule': rule}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='verb', required=True)
    sub.add_parser('list', help='Show active rule text only')
    preview = sub.add_parser('propose', help='Read one rule from stdin; does not update the profile')
    preview.add_argument('--masculine-titles', action='store_true')
    approval = sub.add_parser('apply', help='Read the approval challenge from stdin')
    approval.add_argument('proposal_id')
    args = parser.parse_args(argv)
    try:
        if args.verb == 'list':
            result = {'rules': generate.generation_profile()['application_preferences']}
        elif args.verb == 'propose':
            result = propose(None if args.masculine_titles else sys.stdin.read(1602).removesuffix('\n'),
                             masculine=args.masculine_titles)
        else:
            result = apply(args.proposal_id, sys.stdin.read(64).strip())
    except (Error, OSError, ValueError, KeyError, TypeError) as exc:
        print(review.terminal_text(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
