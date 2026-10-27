#!/usr/bin/env python3
"""Read local Messages with SQLite mode=ro. No third-party Python packages."""
import argparse
import base64
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
BASE_GAPS = ["Only this Mac's currently stored rows; deleted, expired, unsynced and other-channel messages are not reconstructed.",
             "Attachment contents, rich payloads, archive attributes and prior edit revisions are not decoded; reactions are events, not task completion."]


class ReadConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def connect(path):
    c = sqlite3.connect(Path(path).expanduser().resolve().as_uri() + '?mode=ro', uri=True, timeout=5, factory=ReadConnection)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA query_only=ON')
    c.execute('BEGIN')  # coherent read snapshot, including WAL; never use immutable=1 on live DB
    return c


def cols(c, table):
    return {r['name'] for r in c.execute('PRAGMA table_info("' + table + '")')}


def schema(c):
    required = {'message': {'guid', 'text', 'date', 'is_from_me', 'handle_id'},
                'chat': {'guid', 'display_name'}, 'handle': {'id'},
                'chat_message_join': {'chat_id', 'message_id'},
                'chat_handle_join': {'chat_id', 'handle_id'}}
    tables = {t: cols(c, t) for t in required}
    for t, expected in required.items():
        if not expected <= tables[t]:
            raise ValueError('Unsupported schema: missing ' + t + ' columns ' + str(sorted(expected - tables[t])))
    return tables


def decode(items):
    if not items:
        return {}
    if not shutil.which('swift'):
        return {x['id']: {'error': 'Swift unavailable; install Apple Command Line Tools manually'} for x in items}
    def run(batch):
        try:
            p = subprocess.run(['swift', str(Path(__file__).with_name('decode.swift'))],
                               input=json.dumps(batch), text=True, capture_output=True, timeout=120)
            if p.returncode == 0:
                rows = json.loads(p.stdout)
                if {x['id'] for x in rows} == {x['id'] for x in batch}:
                    return {x['id']: x for x in rows}
        except (subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError):
            pass
        return None
    result = run(items)
    if result is not None:
        return result
    # An Objective-C archive exception can terminate Swift; isolate bad rows.
    if len(items) == 1:
        return {items[0]['id']: {'error': 'Decoder failed, crashed or timed out; text may be incomplete'}}
    result = {}
    for item in items:
        result.update(run([item]) or {item['id']: {'error': 'Decoder failed, crashed or timed out; text may be incomplete'}})
    return result


def stamp(raw):
    if raw is None:
        return None
    # Messages has used both Apple-epoch seconds and nanoseconds.
    seconds = raw / 1e9 if abs(raw) >= 100_000_000_000 else raw
    try:
        return (EPOCH + timedelta(seconds=seconds)).isoformat()
    except (OverflowError, TypeError):
        return None


def norm(value):
    value = value.strip().lower()
    if '@' in value:
        return value
    digits = ''.join(x for x in value if x.isdigit())
    return digits or value


def members(c, chat):
    return [dict(x) for x in c.execute('SELECT h.ROWID AS handle_rowid,h.id FROM handle h JOIN chat_handle_join j ON j.handle_id=h.ROWID WHERE j.chat_id=? ORDER BY h.ROWID', (chat,))]


def find(c, a):
    candidates = {}
    errors = []
    contact_matches = []
    if a.contacts:
        root = Path.home() / 'Library/Application Support/AddressBook'
        paths = [root / 'AddressBook-v22.abcddb', *sorted(root.glob('Sources/*/AddressBook-v22.abcddb'))]
        for index, path in enumerate(paths):
            if not path.exists():
                continue
            try:
                with connect(path) as cc:
                    if not {'Z_PK', 'ZFIRSTNAME', 'ZLASTNAME', 'ZORGANIZATION'} <= cols(cc, 'ZABCDRECORD'):
                        raise ValueError('unsupported Contacts schema')
                    rows = cc.execute("SELECT Z_PK,ZFIRSTNAME,ZLASTNAME,ZORGANIZATION FROM ZABCDRECORD WHERE instr(lower(coalesce(ZFIRSTNAME,'') || ' ' || coalesce(ZLASTNAME,'') || ' ' || coalesce(ZORGANIZATION,'')),lower(?))>0 LIMIT 51", (a.query,)).fetchall()
                    if len(rows) > 50:
                        errors.append('Contact candidates truncated; narrow the name query')
                    for row in rows[:50]:
                        addresses = []
                        for table, column in [('ZABCDPHONENUMBER', 'ZFULLNUMBER'), ('ZABCDEMAILADDRESS', 'ZADDRESS')]:
                            if {'ZOWNER', column} <= cols(cc, table):
                                addresses += [r[0] for r in cc.execute(f'SELECT {column} FROM {table} WHERE ZOWNER=?', (row['Z_PK'],)) if r[0]]
                            else:
                                errors.append('Contacts phone/email table unavailable')
                        contact_matches.append({'contact_source': f'addressbook-{index}:{row["Z_PK"]}', 'name': ' '.join(str(row[k]) for k in ['ZFIRSTNAME', 'ZLASTNAME', 'ZORGANIZATION'] if row[k]), 'addresses': addresses})
            except (sqlite3.Error, OSError, ValueError) as e:
                errors.append('Contacts source unavailable: ' + str(e))
        if not contact_matches:
            errors.append('No matching Contacts records; names may be absent or access unavailable. Ask for phone/email.')
    handles = c.execute('SELECT ROWID AS handle_rowid,* FROM handle').fetchall()
    for h in handles:
        if a.query.casefold() in h['id'].casefold() or (any(x.isdigit() for x in a.query) and norm(a.query) in norm(h['id'])) or any(norm(h['id']) == norm(address) for match in contact_matches for address in match['addresses']):
            candidates[h['handle_rowid']] = dict(h)
    chats = {}
    for hid in candidates:
        for chat in c.execute('SELECT c.ROWID AS chat_rowid,c.guid,c.display_name FROM chat c JOIN chat_handle_join j ON j.chat_id=c.ROWID WHERE j.handle_id=?', (hid,)):
            chats[chat['chat_rowid']] = dict(chat)
    # Display names are clues, not identity proof.
    for chat in c.execute("SELECT ROWID AS chat_rowid,guid,display_name FROM chat WHERE instr(lower(coalesce(display_name,'')),lower(?))>0", (a.query,)):
        chats[chat['chat_rowid']] = dict(chat)
    total = len(chats)
    selected = list(chats.values())[:a.limit]
    for chat in selected:
        chat['participants'] = members(c, chat['chat_rowid'])
        chat['message_rows'] = c.execute('SELECT count(DISTINCT message_id) FROM chat_message_join WHERE chat_id=?', (chat['chat_rowid'],)).fetchone()[0]
    return {'contacts': contact_matches, 'matching_handles': list(candidates.values())[:a.limit], 'handles_truncated': len(candidates) > a.limit,
            'chats': selected, 'chats_truncated': total > a.limit, 'gaps': sorted(set(errors)),
            'identity_note': 'Candidates only. Confirm the person and all relevant phone/email identities; inspect group participants. National/international phone variants are not guessed.'}


def history(c, a, tables):
    ids = sorted(set(a.chat))
    q = ','.join('?' for _ in ids)
    chats = [dict(x) for x in c.execute(f'SELECT ROWID AS chat_rowid,guid,display_name FROM chat WHERE ROWID IN ({q})', ids)]
    if len(chats) != len(ids):
        raise ValueError('Unknown chat rowid')
    for chat in chats:
        chat['participants'] = members(c, chat['chat_rowid'])
    maximum = c.execute(f'SELECT max(m.ROWID) FROM message m WHERE EXISTS(SELECT 1 FROM chat_message_join j WHERE j.message_id=m.ROWID AND j.chat_id IN ({q}))', ids).fetchone()[0] or 0
    through = maximum if a.through_rowid is None else a.through_rowid
    where = f'm.ROWID>? AND m.ROWID<=? AND EXISTS(SELECT 1 FROM chat_message_join j WHERE j.message_id=m.ROWID AND j.chat_id IN ({q}))'
    params = [a.after_rowid, through, *ids]
    total = c.execute(f'SELECT count(*) FROM message m WHERE {where}', params).fetchone()[0]
    rows = [dict(r) for r in c.execute(f'SELECT m.ROWID AS message_rowid,m.* FROM message m WHERE {where} ORDER BY m.ROWID LIMIT ?', [*params, a.limit])]
    blobs = [{'id': r['message_rowid'], 'body': base64.b64encode(r['attributedBody']).decode()} for r in rows if r.get('attributedBody')]
    decoded = decode(blobs)
    gaps = []
    observed = set()
    messages = []
    attachment_ok = {'message_id', 'attachment_id'} <= cols(c, 'message_attachment_join') and bool(cols(c, 'attachment'))
    optional = {'attributedBody', 'associated_message_type', 'associated_message_guid', 'date_edited', 'date_retracted', 'item_type', 'group_action_type', 'payload_data', 'message_summary_info', 'cache_has_attachments'}
    missing = sorted(optional - tables['message'])
    if missing:
        gaps.append('Schema lacks optional content/coverage fields: ' + ', '.join(missing))
    if not attachment_ok:
        gaps.append('Attachment metadata unavailable')
    for r in rows:
        rid = r['message_rowid']
        flags = []
        plain = r.get('text')
        d = decoded.get(rid, {})
        body = d.get('text')
        if d.get('error'):
            flags.append('attributed_body_decode_failed')
        if plain is not None and body is not None and plain != body:
            flags.append('text_body_differ')
        text = plain if plain not in (None, '', '\ufffc') else body if body is not None else plain
        if text is None or not text.replace('\ufffc', '').strip():
            flags.append('no_readable_text')
        if text and '\ufffc' in text:
            flags.append('embedded_object_placeholder')
        at = r.get('associated_message_type') or 0
        if at:
            flags.append('associated_event')
        if r.get('date_edited'):
            flags.append('edited_prior_versions_not_reconstructed')
        if r.get('date_retracted'):
            flags.append('retracted')
        if r.get('item_type') or r.get('group_action_type'):
            flags.append('system_or_group_event')
        if any(r.get(k) for k in ['payload_data', 'message_summary_info', 'balloon_bundle_id', 'expressive_send_style_id']):
            flags.append('unsupported_payload_or_summary')
        attachments = []
        if attachment_ok:
            for ar in c.execute('SELECT a.ROWID AS attachment_rowid,a.* FROM attachment a JOIN message_attachment_join j ON j.attachment_id=a.ROWID WHERE j.message_id=?', (rid,)):
                attachment = {k: ar[k] for k in ['attachment_rowid', 'guid', 'filename', 'mime_type', 'uti', 'transfer_name', 'total_bytes', 'transfer_state', 'is_sticker'] if k in ar.keys()}
                filename = attachment.get('filename')
                try:
                    attachment['local_file_exists'] = Path(filename).expanduser().is_file() if filename else False
                except OSError:
                    attachment['local_file_exists'] = None
                attachments.append(attachment)
        if attachments or r.get('cache_has_attachments'):
            flags.append('attachment_contents_not_reviewed')
        if any(x['local_file_exists'] is not True for x in attachments):
            flags.append('attachment_file_unavailable')
        if r.get('cache_has_attachments') and not attachments:
            flags.append('attachment_metadata_gap')
        sender = c.execute('SELECT id FROM handle WHERE ROWID=?', (r['handle_id'],)).fetchone()
        if not r['is_from_me'] and not sender:
            flags.append('unknown_sender')
        date = stamp(r['date'])
        if date is None:
            flags.append('invalid_or_missing_date')
        chat_refs = [dict(x) for x in c.execute('SELECT c.ROWID AS chat_rowid,c.guid FROM chat c JOIN chat_message_join j ON j.chat_id=c.ROWID WHERE j.message_id=?', (rid,))]
        item = {'source_id': 'message:' + (r['guid'] or f'local-rowid:{rid}'), 'message_rowid': rid, 'guid': r['guid'], 'chats': chat_refs,
                'date_utc': date, 'date_raw': r['date'], 'direction': 'outgoing' if r['is_from_me'] else 'incoming', 'sender': 'self' if r['is_from_me'] else sender[0] if sender else None,
                'text': text, 'text_column': plain, 'attributed_text': body, 'decode_error': d.get('error'),
                'associated_message_type': at, 'associated_message_guid': r.get('associated_message_guid'),
                'date_edited_raw': r.get('date_edited'), 'date_retracted_raw': r.get('date_retracted'),
                'item_type': r.get('item_type'), 'group_action_type': r.get('group_action_type'), 'attachments': attachments, 'flags': flags}
        observed.update(flags)
        if a.command != 'search' or a.query.casefold() in '\n'.join(x for x in [plain, body] if x).casefold():
            messages.append(item)
    more = total > len(rows)
    return {'chats': chats, 'messages': messages, 'coverage': {'scope': 'Selected chats, locally available current rows',
            'through_rowid': through, 'after_rowid': a.after_rowid, 'rows_in_requested_range': total, 'rows_scanned': len(rows), 'rows_returned': len(messages),
            'more_rows': more, 'next_after_rowid': rows[-1]['message_rowid'] if more and rows else None,
            'row_retrieval_complete': not more and a.after_rowid == 0, 'content_complete': False,
            'observed_flags': sorted(observed), 'gaps': BASE_GAPS + gaps,
            'order': 'Ingestion rowid for stable paging; sort by date_utc then message_rowid after collecting pages.',
            'paging_note': 'Reuse through_rowid and next_after_rowid. Inserts above watermark excluded; edits/deletes between invocations can change pages. Each invocation uses one read snapshot.',
            'search_note': 'Search checks ordinary and decoded text in every scanned row; collect every page even if this page has no matches. Failed decodes and unreviewed attachments can hide matches.' if a.command == 'search' else None}}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db', default=str(Path.home() / 'Library/Messages/chat.db'))
    sub = p.add_subparsers(dest='command', required=True)
    check = sub.add_parser('check', help='Schema/dependencies only; optional single-body compatibility probe without text output')
    check.add_argument('--decode-sample', action='store_true')
    f = sub.add_parser('find', help='Find identity/conversation candidates, never auto-select a person')
    f.add_argument('--query', required=True)
    f.add_argument('--contacts', action='store_true', help='Also read matching names from local Contacts databases')
    f.add_argument('--limit', type=int, default=50)
    for command in ['history', 'search']:
        s = sub.add_parser(command)
        s.add_argument('--chat', type=int, action='append', required=True)
        s.add_argument('--limit', type=int, default=200)
        s.add_argument('--after-rowid', type=int, default=0)
        s.add_argument('--through-rowid', type=int)
        if command == 'search':
            s.add_argument('--query', required=True)
    a = p.parse_args()
    if hasattr(a, 'limit') and not 1 <= a.limit <= 1000:
        p.error('--limit must be 1..1000')
    if hasattr(a, 'query') and not a.query.strip():
        p.error('--query must not be empty')
    if hasattr(a, 'after_rowid') and (a.after_rowid < 0 or (a.through_rowid is not None and a.through_rowid < a.after_rowid)):
        p.error('Invalid rowid range')
    try:
        with connect(a.db) as c:
            tables = schema(c)
            if a.command == 'check':
                result = {'read_only_access': True, 'query_only': bool(c.execute('PRAGMA query_only').fetchone()[0]), 'schema_supported': True,
                          'python': sys.version.split()[0], 'swift_available': bool(shutil.which('swift')), 'body_sample_read': False}
                if a.decode_sample:
                    if 'attributedBody' not in tables['message']:
                        result['sample_decode'] = 'attributedBody unavailable'
                    else:
                        row = c.execute('SELECT attributedBody FROM message WHERE attributedBody IS NOT NULL AND length(attributedBody)>0 LIMIT 1').fetchone()
                        result['body_sample_read'] = row is not None
                        result['sample_decode'] = ('success' if 'text' in decode([{'id': 1, 'body': base64.b64encode(row[0]).decode()}])[1] else 'failed') if row else 'no sample'
            elif a.command == 'find':
                result = find(c, a)
            else:
                result = history(c, a, tables)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (sqlite3.Error, OSError, ValueError) as e:
        print(json.dumps({'error': str(e), 'retrieval_complete': False, 'setup': 'For permission denial: manually allow the actual host app/terminal in System Settings > Privacy & Security > Full Disk Access, relaunch it, then rerun check. Do not change permissions automatically. Missing files, schema errors and corruption require separate diagnosis.'}), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
