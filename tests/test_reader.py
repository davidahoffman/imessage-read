#!/usr/bin/env python3
"""Synthetic-only behavioral tests; never opens the user's Messages or Contacts."""
import argparse
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('reader', ROOT / 'scripts/reader.py')
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)


class ReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.blobs = {}
        if shutil.which('swift'):
            code = '''import Foundation
let s = NSAttributedString(string: "Please send the café invoice 🧾\\nTomorrow")
let empty = NSAttributedString(string: "")
let values = [NSArchiver.archivedData(withRootObject: s), try NSKeyedArchiver.archivedData(withRootObject: s, requiringSecureCoding: true), NSArchiver.archivedData(withRootObject: empty)]
print(try String(data: JSONSerialization.data(withJSONObject: values.map {$0.base64EncodedString()}), encoding: .utf8)!)
'''
            path = cls.root / 'generate.swift'
            path.write_text(code)
            p = subprocess.run(['swift', str(path)], text=True, capture_output=True, check=True, timeout=120)
            cls.blobs = dict(zip(['legacy', 'keyed', 'empty'], map(base64.b64decode, json.loads(p.stdout))))

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.db = self.root / (self.id().split('.')[-1] + '.db')
        self.writer = sqlite3.connect(self.db)
        self.writer.executescript('''
CREATE TABLE message(guid TEXT,text TEXT,attributedBody BLOB,date INTEGER,is_from_me INTEGER,handle_id INTEGER,associated_message_type INTEGER,associated_message_guid TEXT,date_edited INTEGER,date_retracted INTEGER,cache_has_attachments INTEGER,item_type INTEGER,group_action_type INTEGER,payload_data BLOB,message_summary_info BLOB);
CREATE TABLE handle(id TEXT,service TEXT);
CREATE TABLE chat(guid TEXT,display_name TEXT);
CREATE TABLE chat_message_join(chat_id INTEGER,message_id INTEGER);
CREATE TABLE chat_handle_join(chat_id INTEGER,handle_id INTEGER);
CREATE TABLE attachment(guid TEXT,filename TEXT,mime_type TEXT,transfer_name TEXT,total_bytes INTEGER,transfer_state INTEGER);
CREATE TABLE message_attachment_join(message_id INTEGER,attachment_id INTEGER);
INSERT INTO handle VALUES('+15550100001','iMessage'),('+15550100002','SMS');
INSERT INTO chat VALUES('chat-a',''),('chat-group','Building'),('chat-other','');
INSERT INTO chat_handle_join VALUES(1,1),(2,1),(2,2),(3,2);
''')
        entries = [
            ('m-1', 'Please send the keys', None, 700000000000000000, 0, 1, 0, None, 0, 0, 0, 0, 0, None, None),
            ('m-2', 'I will tomorrow', None, 700000001000000000, 1, 0, 0, None, 0, 0, 0, 0, 0, None, None),
            ('m-3', None, self.blobs.get('legacy'), 699999999000000000, 0, 1, 0, None, 1, 0, 0, 0, 0, None, None),
            ('m-4', 'Liked a message', None, 700000003000000000, 1, 0, 2001, 'p:0/m-1', 0, 0, 0, 0, 0, None, None),
            ('m-5', '\ufffc', None, 700000004000000000, 0, 1, 0, None, 0, 0, 1, 0, 0, None, None),
            ('m-6', 'fallback', b'not an archive', 700000005000000000, 0, 1, 9999, 'm-missing', 0, 1, 1, 0, 0, b'opaque', None),
            ('m-7', 'Only elsewhere', None, 700000006000000000, 0, 2, 0, None, 0, 0, 0, 0, 0, None, None),
        ]
        self.writer.executemany('INSERT INTO message VALUES(' + ','.join('?' * 15) + ')', entries)
        self.writer.executescript('''
INSERT INTO chat_message_join VALUES(1,1),(1,2),(1,3),(1,4),(1,5),(1,6),(2,3),(3,7);
INSERT INTO attachment VALUES('attach-1','/nonexistent/synthetic-only/photo.jpg','image/jpeg','photo.jpg',123,0);
INSERT INTO message_attachment_join VALUES(5,1);
''')
        self.writer.commit()

    def tearDown(self):
        self.writer.close()

    def run_history(self, **kwargs):
        args = dict(chat=[1], through_rowid=None, after_rowid=0, limit=100, command='history', query='')
        args.update(kwargs)
        with reader.connect(self.db) as c:
            return reader.history(c, argparse.Namespace(**args), reader.schema(c))

    def test_readonly_and_missing_db(self):
        before = hashlib.sha256(self.db.read_bytes()).digest()
        with reader.connect(self.db) as c:
            self.assertEqual(c.execute('PRAGMA query_only').fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                c.execute("INSERT INTO handle VALUES('blocked','SMS')")
        self.assertEqual(before, hashlib.sha256(self.db.read_bytes()).digest())
        absent = self.root / 'must-not-be-created.db'
        with self.assertRaises(sqlite3.Error):
            reader.connect(absent)
        self.assertFalse(absent.exists())

    def test_paging_watermark_dedup_and_wal(self):
        self.writer.execute('PRAGMA journal_mode=WAL')
        first = self.run_history(limit=2, chat=[1, 2])
        self.assertTrue(first['coverage']['more_rows'])
        self.assertFalse(first['coverage']['row_retrieval_complete'])
        watermark = first['coverage']['through_rowid']
        self.writer.execute("INSERT INTO message(guid,text,date,is_from_me,handle_id) VALUES('new','new WAL message',700000010000000000,0,1)")
        self.writer.execute('INSERT INTO chat_message_join VALUES(1,8)')
        self.writer.commit()
        rest = self.run_history(chat=[1, 2], after_rowid=2, through_rowid=watermark)
        self.assertEqual([m['guid'] for m in first['messages'] + rest['messages']], ['m-1', 'm-2', 'm-3', 'm-4', 'm-5', 'm-6'])
        self.assertEqual(len(rest['messages'][0]['chats']), 2)
        self.assertFalse(rest['coverage']['more_rows'])
        self.assertEqual(self.run_history()['messages'][-1]['guid'], 'new')

    @unittest.skipUnless(shutil.which('swift'), 'Swift toolchain required')
    def test_archives_unicode_keyed_empty_and_failure(self):
        result = reader.decode([{'id': i, 'body': base64.b64encode(self.blobs[k]).decode()} for i, k in enumerate(['legacy', 'keyed', 'empty'])])
        for i in [0, 1]:
            self.assertEqual(result[i]['text'], 'Please send the café invoice 🧾\nTomorrow')
        self.assertEqual(result[2]['text'], '')
        broken = reader.decode([{'id': 1, 'body': base64.b64encode(self.blobs['legacy']).decode()}, {'id': 2, 'body': base64.b64encode(b'bad').decode()}])
        self.assertIn('café', broken[1]['text'])
        self.assertIn('error', broken[2])

    @unittest.skipUnless(shutil.which('swift'), 'Swift toolchain required')
    def test_search_attributed_body_and_empty_match_pages(self):
        first = self.run_history(command='search', query='CAFÉ', limit=2)
        self.assertEqual(first['messages'], [])
        self.assertTrue(first['coverage']['more_rows'])
        rest = self.run_history(command='search', query='CAFÉ', after_rowid=2, through_rowid=6)
        self.assertEqual([r['guid'] for r in rest['messages']], ['m-3'])
        self.assertIn('attributed_body_decode_failed', rest['coverage']['observed_flags'])

    @unittest.skipUnless(shutil.which('swift'), 'Swift toolchain required')
    def test_conflicting_text_fields_preserved_and_searchable(self):
        self.writer.execute("UPDATE message SET text='ordinary-only request' WHERE guid='m-3'")
        self.writer.commit()
        for query in ['ordinary-only', 'café']:
            out = self.run_history(command='search', query=query)
            self.assertEqual(len(out['messages']), 1)
            item = out['messages'][0]
            self.assertEqual(item['text_column'], 'ordinary-only request')
            self.assertIn('café', item['attributed_text'])
            self.assertIn('text_body_differ', item['flags'])

    def test_flags_provenance_dates_scope(self):
        output = self.run_history()
        rows = {x['guid']: x for x in output['messages']}
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows['m-1']['source_id'], 'message:m-1')
        self.assertEqual(rows['m-1']['direction'], 'incoming')
        self.assertEqual(rows['m-2']['sender'], 'self')
        self.assertEqual(rows['m-4']['associated_message_guid'], 'p:0/m-1')
        self.assertIn('associated_event', rows['m-4']['flags'])
        self.assertIn('edited_prior_versions_not_reconstructed', rows['m-3']['flags'])
        self.assertFalse(rows['m-5']['attachments'][0]['local_file_exists'])
        self.assertIn('embedded_object_placeholder', rows['m-5']['flags'])
        self.assertIn('attachment_metadata_gap', rows['m-6']['flags'])
        self.assertIn('unsupported_payload_or_summary', rows['m-6']['flags'])
        self.assertIn('retracted', rows['m-6']['flags'])
        self.assertTrue(output['coverage']['row_retrieval_complete'])
        self.assertFalse(output['coverage']['content_complete'])
        self.assertEqual(reader.stamp(700000000), reader.stamp(700000000000000000))
        self.assertEqual(reader.stamp(0), '2001-01-01T00:00:00+00:00')
        with self.assertRaises(ValueError):
            self.run_history(chat=[999])

    def test_decoder_missing_and_missing_attachment_schema(self):
        with patch.object(reader.shutil, 'which', return_value=None):
            self.assertIn('error', reader.decode([{'id': 1, 'body': 'YQ=='}])[1])
        self.writer.execute('DROP TABLE message_attachment_join')
        self.writer.commit()
        self.assertIn('Attachment metadata unavailable', self.run_history()['coverage']['gaps'])

    def test_contacts_ambiguity_and_all_group_members(self):
        home = self.root / 'synthetic-home'
        folder = home / 'Library/Application Support/AddressBook'
        folder.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(folder / 'AddressBook-v22.abcddb')
        db.executescript('''CREATE TABLE ZABCDRECORD(Z_PK INTEGER,ZFIRSTNAME TEXT,ZLASTNAME TEXT,ZORGANIZATION TEXT);
CREATE TABLE ZABCDPHONENUMBER(ZOWNER INTEGER,ZFULLNUMBER TEXT);
CREATE TABLE ZABCDEMAILADDRESS(ZOWNER INTEGER,ZADDRESS TEXT);
INSERT INTO ZABCDRECORD VALUES(1,'Alex','Morgan',NULL),(2,'Alex','Morgan',NULL);
INSERT INTO ZABCDPHONENUMBER VALUES(1,'+1 (555) 010-0001'),(2,'+1 (555) 010-0002');''')
        db.close()
        with patch.object(reader.Path, 'home', return_value=home), reader.connect(self.db) as c:
            out = reader.find(c, argparse.Namespace(query='Alex Morgan', contacts=True, limit=50))
        self.assertEqual(len(out['contacts']), 2)
        self.assertEqual(len(out['matching_handles']), 2)
        self.assertEqual(len(out['chats']), 3)
        group = next(x for x in out['chats'] if x['chat_rowid'] == 2)
        self.assertEqual(len(group['participants']), 2)

    def test_schema_failure_is_explicit(self):
        self.writer.execute('ALTER TABLE message RENAME COLUMN text TO unknown_text')
        self.writer.commit()
        with reader.connect(self.db) as c, self.assertRaisesRegex(ValueError, 'Unsupported schema'):
            reader.schema(c)


if __name__ == '__main__':
    unittest.main(verbosity=2)
