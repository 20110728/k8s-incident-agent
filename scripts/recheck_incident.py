"""Create a fresh cluster-read-only observation, or read saved recheck history.

Creating an observation persists a new record; it never approves or resumes the
original workflow. History mode only reads existing records.
"""
import argparse
import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--incident-id', required=True)
    parser.add_argument('--api-base', default='http://127.0.0.1:8000/api/v1')
    parser.add_argument('--note', default='')
    parser.add_argument('--history', action='store_true')
    parser.add_argument('--expect', choices=['passed', 'failed', 'unknown'])
    parser.add_argument('--expect-recheck-id', help='History mode: require this saved record in the returned page.')
    parser.add_argument('--before-sequence', type=int)
    args = parser.parse_args()
    if not args.incident_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-' for c in args.incident_id):
        parser.error('invalid incident ID')
    if (args.expect_recheck_id or args.before_sequence is not None) and not args.history:
        parser.error('--expect-recheck-id/--before-sequence require --history')
    incident_url = args.api_base.rstrip('/') + '/incidents/' + args.incident_id

    def call(url, payload=None):
        req = Request(url, data=None if payload is None else json.dumps(payload).encode(),
                      headers={'Content-Type': 'application/json'})
        try:
            with urlopen(req, timeout=180) as response:
                return json.load(response)
        except HTTPError as error:
            raise SystemExit(f'HTTP {error.code}: ' + error.read().decode(errors='replace')) from error

    if args.history:
        url = incident_url + '/rechecks'
        if args.before_sequence is not None:
            url += '?before_sequence=' + str(args.before_sequence)
        result = call(url)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        rows = result['items']
        if args.expect_recheck_id and not any(r['recheck_id'] == args.expect_recheck_id for r in rows):
            raise SystemExit('Saved recheck ID not found in this page.')
        if args.expect and (not rows or rows[0]['status'] != args.expect):
            raise SystemExit('Latest recheck status did not match expectation.')
        return
    before = call(incident_url)
    result = call(incident_url + '/rechecks', {'note': args.note})
    after = call(incident_url)
    unchanged = before == after
    print(json.dumps({'original_incident_unchanged': unchanged, 'recheck': result}, ensure_ascii=False, indent=2))
    if not unchanged:
        raise SystemExit('Original incident changed during recheck; inspect concurrent activity.')
    if args.expect and result['status'] != args.expect:
        raise SystemExit('Recheck status did not match expectation.')


if __name__ == '__main__':
    main()
