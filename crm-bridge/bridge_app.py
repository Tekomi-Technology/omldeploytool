"""Standalone, dependency-light CRM bridge for OmniLeads and Perfex-style CRM."""
import hashlib
import hmac
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs
import requests
from wsgiref.simple_server import make_server


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS crm_contacts (
          external_id TEXT PRIMARY KEY, crm_customer_id TEXT NOT NULL,
          crm_contact_id TEXT, phone TEXT NOT NULL, payload TEXT NOT NULL,
          payload_hash TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS calls (
          call_id TEXT PRIMARY KEY, agent_id TEXT, phone TEXT, campaign_id TEXT,
          crm_customer_id TEXT, crm_contact_id TEXT, payload TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ticket_requests (
          request_id TEXT PRIMARY KEY, call_id TEXT NOT NULL, crm_ticket_id TEXT,
          state TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS audit_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, subject TEXT NOT NULL,
          details TEXT NOT NULL, created_at TEXT NOT NULL);
        ''')
        self.db.commit()

    def audit(self, action, subject, details):
        self.db.execute('INSERT INTO audit_log(action,subject,details,created_at) VALUES(?,?,?,?)',
                        (action, subject, json.dumps(details, sort_keys=True), now()))
        self.db.commit()

    def upsert_contacts(self, contacts):
        changed = []
        for contact in contacts:
            encoded = json.dumps(contact, sort_keys=True, separators=(',', ':'))
            digest = hashlib.sha256(encoded.encode()).hexdigest()
            old = self.db.execute('SELECT payload_hash FROM crm_contacts WHERE external_id=?',
                                  (contact['external_id'],)).fetchone()
            if old and old['payload_hash'] == digest:
                continue
            self.db.execute('''INSERT INTO crm_contacts(external_id,crm_customer_id,crm_contact_id,phone,payload,payload_hash,updated_at)
              VALUES(?,?,?,?,?,?,?) ON CONFLICT(external_id) DO UPDATE SET
              crm_customer_id=excluded.crm_customer_id,crm_contact_id=excluded.crm_contact_id,
              phone=excluded.phone,payload=excluded.payload,payload_hash=excluded.payload_hash,updated_at=excluded.updated_at''',
              (contact['external_id'], str(contact['crm_customer_id']), contact.get('crm_contact_id'), contact['phone'], encoded, digest, now()))
            changed.append(contact)
        self.db.commit()
        return changed

    def find_contact(self, phone):
        normalized = normalize_phone(phone)
        rows = self.db.execute('SELECT * FROM crm_contacts').fetchall()
        for row in rows:
            if normalize_phone(row['phone']) == normalized:
                return json.loads(row['payload'])
        return None

    def create_call(self, data):
        contact = self.find_contact(data.get('phone', ''))
        self.db.execute('''INSERT INTO calls(call_id,agent_id,phone,campaign_id,crm_customer_id,crm_contact_id,payload,created_at)
          VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(call_id) DO UPDATE SET payload=excluded.payload''',
          (data['call_id'], str(data.get('agent_id', '')), data.get('phone', ''), str(data.get('campaign_id', '')),
           str(contact['crm_customer_id']) if contact else None, contact.get('crm_contact_id') if contact else None,
           json.dumps(data, sort_keys=True), now()))
        self.db.commit()
        self.audit('call_context_received', data['call_id'], {'matched': bool(contact)})
        return contact

    def get_call(self, call_id):
        return self.db.execute('SELECT * FROM calls WHERE call_id=?', (call_id,)).fetchone()


def normalize_phone(value):
    value = ''.join(char for char in str(value) if char.isdigit() or char == '+')
    if value.startswith('+84'):
        return '0' + value[3:]
    if value.startswith('84') and len(value) >= 10:
        return '0' + value[2:]
    return value


class CrmClient:
    """CRM client with a small global interval to avoid bursty pagination traffic."""
    def __init__(self, base_url, token, http=requests, min_interval=1.5, sleeper=time.sleep, clock=time.monotonic):
        self.base_url, self.token, self.http = base_url.rstrip('/'), token, http
        self.min_interval, self.sleeper, self.clock, self.last_request = float(min_interval), sleeper, clock, None
    @property
    def headers(self): return {'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json', 'User-Agent': 'Tekomi-CRM-Bridge/1.0'}
    def _request(self, method, path, **kwargs):
        if self.last_request is not None:
            wait = self.min_interval - (self.clock() - self.last_request)
            if wait > 0:
                self.sleeper(wait)
        response = getattr(self.http, method)(self.base_url + path, headers=self.headers, timeout=15, **kwargs)
        self.last_request = self.clock()
        return response
    def list_all(self, resource):
        page, result = 1, []
        while True:
            response = self._request('get', '/' + resource, params={'page': page, 'per_page': 100})
            response.raise_for_status(); body = response.json(); data = body.get('data', body)
            result.extend(data if isinstance(data, list) else [])
            meta = body.get('meta', {})
            if not data or page * 100 >= int(meta.get('total', len(result))): break
            page += 1
        return result
    def create_ticket(self, payload):
        response = self._request('post', '/tickets', json=payload)
        response.raise_for_status(); return response.json()


class OmniClient:
    def __init__(self, url, secret, http=requests): self.url, self.secret, self.http = url, secret.encode(), http
    def sync(self, contacts):
        body = json.dumps({'contacts': contacts}, separators=(',', ':')).encode(); stamp = str(int(time.time()))
        signature = hmac.new(self.secret, (stamp + '.').encode() + body, hashlib.sha256).hexdigest()
        response = self.http.post(self.url, data=body, headers={'Content-Type': 'application/json',
            'X-Bridge-Timestamp': stamp, 'X-Bridge-Signature': signature}, timeout=20)
        response.raise_for_status(); return response.json()


class Bridge:
    def __init__(self, config, crm=None, omni=None, store=None):
        self.config = config; self.store = store or Store(config['DB_PATH'])
        self.crm = crm or CrmClient(config['CRM_BASE_URL'], config['CRM_API_TOKEN'],
                                    min_interval=config.get('CRM_REQUEST_INTERVAL_SECONDS', 1.5))
        self.omni = omni or OmniClient(config['OML_SYNC_URL'], config['SHARED_SECRET'])
    def sync_contacts(self):
        customers = {str(x.get('userid', x.get('id'))): x for x in self.crm.list_all('customers')}
        contacts = []
        for item in self.crm.list_all('contacts'):
            customer_id = str(item.get('userid', ''))
            phone = item.get('phonenumber', '')
            if not customer_id or not phone: continue
            customer = customers.get(customer_id, {})
            contacts.append({'external_id': 'crm:contact:' + str(item.get('id', item.get('contactid'))),
                'crm_customer_id': customer_id, 'crm_contact_id': str(item.get('id', item.get('contactid'))),
                'phone': normalize_phone(phone), 'company': customer.get('company', ''),
                'first_name': item.get('firstname', ''), 'last_name': item.get('lastname', ''), 'email': item.get('email', '')})
        changed = self.store.upsert_contacts(contacts)
        # The OmniLeads endpoint is idempotent.  Forward the current CRM
        # snapshot even when the bridge's local hash has not changed: a prior
        # attempt may have persisted the hash locally but failed before the
        # request reached OmniLeads.  This makes the next scheduled/manual run
        # a safe delivery retry, without ever deleting or inactivating data.
        if contacts:
            self.omni.sync(contacts)
        self.store.audit('crm_sync', 'contacts', {'seen': len(contacts), 'changed': len(changed)})
        return {'seen': len(contacts), 'changed': len(changed)}
    def create_ticket(self, call_id, request_id, data):
        existing = self.store.db.execute('SELECT * FROM ticket_requests WHERE request_id=?', (request_id,)).fetchone()
        if existing and existing['crm_ticket_id']: return {'ticket_id': existing['crm_ticket_id'], 'replayed': True}
        call = self.store.get_call(call_id)
        if not call or not call['crm_customer_id']: raise ValueError('Caller is not mapped to a CRM customer')
        department = self.config['QUEUE_DEPARTMENTS'].get(str(data.get('queue', '')), data.get('department'))
        if not department: raise ValueError('No CRM department mapping for this queue')
        payload = {'subject': data['subject'], 'message': data['message'] + '\n\nOML-CALL:' + call_id,
                   'department': department, 'userid': int(call['crm_customer_id'])}
        if call['crm_contact_id']: payload['contactid'] = int(call['crm_contact_id'])
        if data.get('priority') is not None: payload['priority'] = data['priority']
        self.store.db.execute('INSERT OR IGNORE INTO ticket_requests(request_id,call_id,state,payload,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                              (request_id, call_id, 'pending', json.dumps(payload), now(), now())); self.store.db.commit()
        response = self.crm.create_ticket(payload); ticket = response.get('data', response).get('ticketid', response.get('data', response).get('id'))
        self.store.db.execute('UPDATE ticket_requests SET crm_ticket_id=?,state=?,updated_at=? WHERE request_id=?', (str(ticket), 'created', now(), request_id)); self.store.db.commit()
        self.store.audit('ticket_created', call_id, {'ticket_id': ticket, 'request_id': request_id})
        return {'ticket_id': ticket, 'replayed': False}


def application(config=None):
    config = config or {'DB_PATH': os.getenv('BRIDGE_DB_PATH', 'bridge.sqlite3'), 'CRM_BASE_URL': os.environ['CRM_BASE_URL'], 'CRM_API_TOKEN': os.environ['CRM_API_TOKEN'], 'OML_SYNC_URL': os.environ['OML_SYNC_URL'], 'BRIDGE_API_KEY': os.environ['BRIDGE_API_KEY'], 'SHARED_SECRET': os.environ['BRIDGE_SHARED_SECRET'], 'QUEUE_DEPARTMENTS': json.loads(os.getenv('BRIDGE_QUEUE_DEPARTMENTS', '{}')), 'CRM_REQUEST_INTERVAL_SECONDS': float(os.getenv('CRM_REQUEST_INTERVAL_SECONDS', '1.5'))}
    bridge = Bridge(config)
    def app(environ, start_response):
        path, method = environ['PATH_INFO'], environ['REQUEST_METHOD']; query = parse_qs(environ.get('QUERY_STRING', ''))
        def respond(status, body, content='application/json'):
            raw = json.dumps(body).encode() if content == 'application/json' else body.encode(); start_response(status, [('Content-Type', content), ('Content-Length', str(len(raw)))]); return [raw]
        if path == '/health': return respond('200 OK', {'ok': True})
        if method == 'POST' and environ.get('HTTP_X_BRIDGE_API_KEY') != config['BRIDGE_API_KEY']: return respond('401 Unauthorized', {'error': 'unauthorized'})
        length = int(environ.get('CONTENT_LENGTH') or 0); data = json.loads(environ['wsgi.input'].read(length) or b'{}') if length else {}
        try:
            if path == '/v1/sync' and method == 'POST': return respond('200 OK', bridge.sync_contacts())
            if path == '/v1/calls' and method == 'POST': bridge.store.create_call(data); return respond('201 Created', {'workspace_url': '/workspace?call_id=' + data['call_id']})
            if path.startswith('/v1/calls/') and path.endswith('/tickets') and method == 'POST': return respond('201 Created', bridge.create_ticket(path.split('/')[3], data['request_id'], data))
            if path == '/workspace': return respond('200 OK', '<h1>CRM Bridge</h1><p>Call: ' + query.get('call_id', [''])[0] + '</p>', 'text/html')
            return respond('404 Not Found', {'error': 'not found'})
        except ValueError as error: return respond('422 Unprocessable Entity', {'error': str(error)})
        except requests.RequestException: return respond('502 Bad Gateway', {'error': 'CRM or OmniLeads unavailable'})
    return app

if __name__ == '__main__':
    make_server('0.0.0.0', int(os.getenv('PORT', '8080')), application()).serve_forever()
