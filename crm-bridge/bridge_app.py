"""Standalone, dependency-light CRM bridge for OmniLeads and Perfex-style CRM."""
import ast
import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
import base64
import threading
from datetime import datetime, timezone
from urllib.parse import parse_qs
import requests
from wsgiref.simple_server import make_server


def now():
    return datetime.now(timezone.utc).isoformat()


def department_mapping(value):
    """Read a deployment mapping written as JSON or as a legacy Python dict.

    Older compose env files in this installation contain ``{'default': 1}``.
    Environment data is deployment-owned, nevertheless ``literal_eval`` keeps
    the compatibility path data-only and never evaluates code.
    """
    if not value:
        return {}
    try:
        mapping = json.loads(value)
    except json.JSONDecodeError:
        # ``source`` strips inner double quotes in an unquoted .env value such
        # as {"default": 1}, leaving {default: 1}. Normalize only bare object
        # keys before falling back to the older single-quoted representation.
        normalized = re.sub(r'([,{]\s*)([A-Za-z_][A-Za-z0-9_-]*)\s*:', r'\1"\2":', value)
        try:
            mapping = json.loads(normalized)
        except json.JSONDecodeError:
            mapping = ast.literal_eval(value)
    if not isinstance(mapping, dict):
        raise ValueError("BRIDGE_QUEUE_DEPARTMENTS must be an object")
    return {str(key): item for key, item in mapping.items()}


class Store:
    def __init__(self, path):
        self.postgres = path.startswith('postgresql://')
        self.consumer_leader = False
        if self.postgres:
            import psycopg2
            from psycopg2.extras import RealDictCursor
            self.db = psycopg2.connect(path, cursor_factory=RealDictCursor)
            self.db.autocommit = False
            self.db.cursor().execute('CREATE SCHEMA IF NOT EXISTS tekomi_crm_bridge')
            self.db.cursor().execute('''CREATE TABLE IF NOT EXISTS tekomi_crm_bridge.crm_contacts (external_id TEXT PRIMARY KEY, crm_customer_id TEXT NOT NULL, crm_contact_id TEXT, phone TEXT NOT NULL, payload TEXT NOT NULL, payload_hash TEXT NOT NULL, updated_at TEXT NOT NULL); CREATE TABLE IF NOT EXISTS tekomi_crm_bridge.calls (call_id TEXT PRIMARY KEY, agent_id TEXT, phone TEXT, campaign_id TEXT, crm_customer_id TEXT, crm_contact_id TEXT, payload TEXT NOT NULL, created_at TEXT NOT NULL); CREATE TABLE IF NOT EXISTS tekomi_crm_bridge.ticket_requests (request_id TEXT PRIMARY KEY, call_id TEXT NOT NULL, crm_ticket_id TEXT, state TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL); CREATE TABLE IF NOT EXISTS tekomi_crm_bridge.audit_log (id BIGSERIAL PRIMARY KEY, action TEXT NOT NULL, subject TEXT NOT NULL, details TEXT NOT NULL, created_at TEXT NOT NULL); CREATE TABLE IF NOT EXISTS tekomi_crm_bridge.state (key TEXT PRIMARY KEY, value TEXT NOT NULL);''')
            self.db.commit()
            return
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

    def execute(self, query, params=()):
        if self.postgres:
            query = query.replace('?', '%s').replace('INSERT OR IGNORE', 'INSERT').replace('ON CONFLICT(request_id) DO NOTHING', 'ON CONFLICT(request_id) DO NOTHING')
            query = query.replace('crm_contacts', 'tekomi_crm_bridge.crm_contacts').replace('ticket_requests', 'tekomi_crm_bridge.ticket_requests').replace('audit_log', 'tekomi_crm_bridge.audit_log').replace(' state', ' tekomi_crm_bridge.state').replace('INTO state', 'INTO tekomi_crm_bridge.state').replace(' calls', ' tekomi_crm_bridge.calls').replace('INTO calls', 'INTO tekomi_crm_bridge.calls')
            cursor = self.db.cursor(); cursor.execute(query, params); return cursor
        return self.db.execute(query, params)

    def audit(self, action, subject, details):
        self.execute('INSERT INTO audit_log(action,subject,details,created_at) VALUES(?,?,?,?)',
                        (action, subject, json.dumps(details, sort_keys=True), now()))
        self.db.commit()

    def upsert_contacts(self, contacts):
        changed = []
        for contact in contacts:
            encoded = json.dumps(contact, sort_keys=True, separators=(',', ':'))
            digest = hashlib.sha256(encoded.encode()).hexdigest()
            old = self.execute('SELECT payload_hash FROM crm_contacts WHERE external_id=?',
                                  (contact['external_id'],)).fetchone()
            if old and old['payload_hash'] == digest:
                continue
            self.execute('''INSERT INTO crm_contacts(external_id,crm_customer_id,crm_contact_id,phone,payload,payload_hash,updated_at)
              VALUES(?,?,?,?,?,?,?) ON CONFLICT(external_id) DO UPDATE SET
              crm_customer_id=excluded.crm_customer_id,crm_contact_id=excluded.crm_contact_id,
              phone=excluded.phone,payload=excluded.payload,payload_hash=excluded.payload_hash,updated_at=excluded.updated_at''',
              (contact['external_id'], str(contact['crm_customer_id']), contact.get('crm_contact_id'), contact['phone'], encoded, digest, now()))
            changed.append(contact)
        self.db.commit()
        return changed

    def find_contact(self, phone):
        normalized = normalize_phone(phone)
        rows = self.execute('SELECT * FROM crm_contacts').fetchall()
        for row in rows:
            if normalize_phone(row['phone']) == normalized:
                return json.loads(row['payload'])
        return None

    def create_call(self, data):
        if not data.get('agent_id') and data.get('extension'):
            data['agent_id'] = self.agent_for_extension(data['extension'])
        contact = self.find_contact(data.get('phone', ''))
        self.execute('''INSERT INTO calls(call_id,agent_id,phone,campaign_id,crm_customer_id,crm_contact_id,payload,created_at)
          VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(call_id) DO UPDATE SET payload=excluded.payload''',
          (data['call_id'], str(data.get('agent_id', '')), data.get('phone', ''), str(data.get('campaign_id', '')),
           str(contact['crm_customer_id']) if contact else None, contact.get('crm_contact_id') if contact else None,
           json.dumps(data, sort_keys=True), now()))
        self.db.commit()
        self.audit('call_context_received', data['call_id'], {'matched': bool(contact)})
        return contact

    def get_call(self, call_id):
        return self.execute('SELECT * FROM calls WHERE call_id=?', (call_id,)).fetchone()

    def agent_calls(self, agent_id):
        return [dict(row) for row in self.execute('SELECT call_id,phone,campaign_id,created_at,crm_customer_id FROM calls WHERE agent_id=? ORDER BY created_at DESC LIMIT 30', (str(agent_id),)).fetchall()]

    def agent_for_extension(self, extension):
        if not self.postgres or not extension: return None
        row = self.execute('SELECT id FROM ominicontacto_app_agenteprofile WHERE sip_extension=? AND is_inactive=false AND borrado=false LIMIT 1', (str(extension),)).fetchone()
        return row['id'] if row else None

    def recent_audit(self, limit=30):
        return [dict(row) for row in self.execute('SELECT * FROM audit_log ORDER BY id DESC LIMIT ?', (limit,)).fetchall()]

    def ingest_oml_call_logs(self):
        """Consume OmniLeads' durable call-logger table through shared PostgreSQL."""
        if not self.postgres: return 0
        row = self.execute("SELECT value FROM state WHERE key='oml_llamadalog_id'").fetchone()
        after = int(row['value']) if row else 0
        rows = self.execute('SELECT id,callid,campana_id,agente_id,event,numero_marcado,duracion_llamada,bridge_wait_time,archivo_grabacion,time FROM reportes_app_llamadalog WHERE id>? ORDER BY id LIMIT 500', (after,)).fetchall()
        for row in rows:
            if row['callid']:
                self.create_call({'call_id': row['callid'], 'agent_id': row['agente_id'], 'campaign_id': row['campana_id'], 'phone': row['numero_marcado'], 'event': row['event'], 'duration': row['duracion_llamada'], 'wait_duration': row['bridge_wait_time'], 'recording_ref': row['archivo_grabacion'], 'occurred_at': row['time'].isoformat() if row['time'] else now()})
            after = row['id']
        if rows:
            self.execute("INSERT INTO state(key,value) VALUES('oml_llamadalog_id',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(after),)); self.db.commit()
        return len(rows)

    def claim_call_log_consumer(self):
        if not self.postgres: return False
        if self.consumer_leader: return True
        self.consumer_leader = bool(self.execute('SELECT pg_try_advisory_lock(90824017) AS locked').fetchone()['locked'])
        return self.consumer_leader


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
        existing = self.store.execute('SELECT * FROM ticket_requests WHERE request_id=?', (request_id,)).fetchone()
        if existing and existing['crm_ticket_id']: return {'ticket_id': existing['crm_ticket_id'], 'replayed': True}
        call = self.store.get_call(call_id)
        if not call or not call['crm_customer_id']: raise ValueError('Caller is not mapped to a CRM customer')
        department = self.config['QUEUE_DEPARTMENTS'].get(str(data.get('queue', '')), data.get('department'))
        if not department: raise ValueError('No CRM department mapping for this queue')
        payload = {'subject': data['subject'], 'message': data['message'] + '\n\nOML-CALL:' + call_id,
                   'department': department, 'userid': int(call['crm_customer_id'])}
        if call['crm_contact_id']: payload['contactid'] = int(call['crm_contact_id'])
        if data.get('priority') is not None: payload['priority'] = data['priority']
        self.store.execute('INSERT INTO ticket_requests(request_id,call_id,state,payload,created_at,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(request_id) DO NOTHING',
                              (request_id, call_id, 'pending', json.dumps(payload), now(), now())); self.store.db.commit()
        response = self.crm.create_ticket(payload); ticket = response.get('data', response).get('ticketid', response.get('data', response).get('id'))
        self.store.execute('UPDATE ticket_requests SET crm_ticket_id=?,state=?,updated_at=? WHERE request_id=?', (str(ticket), 'created', now(), request_id)); self.store.db.commit()
        self.store.audit('ticket_created', call_id, {'ticket_id': ticket, 'request_id': request_id})
        return {'ticket_id': ticket, 'replayed': False}

    def workspace(self, call_id):
        call = self.store.get_call(call_id)
        if not call: return None
        data = dict(call); data['payload'] = json.loads(data['payload'])
        return data

    def callback(self, call_id, data):
        call = self.store.get_call(call_id)
        if not call: raise ValueError('Unknown call')
        self.store.audit('callback_requested', call_id, {'when': data.get('when', ''), 'note': data.get('note', '')})
        return {'saved': True}

    def session(self, token):
        try:
            encoded, signature = token.split('.', 1); raw = base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4))
            expected = hmac.new(self.config['SHARED_SECRET'].encode(), raw, hashlib.sha256).hexdigest()
            agent_id, extension, stamp = raw.decode().split(':')
            if not hmac.compare_digest(expected, signature) or time.time() - int(stamp) > 300: return None
            return {'agent_id': agent_id, 'extension': extension}
        except Exception: return None

    def run_call_log_consumer(self):
        while True:
            try:
                if self.store.claim_call_log_consumer(): self.store.ingest_oml_call_logs()
            except Exception: pass
            time.sleep(3)

WORKSPACE_HTML = '''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Tekomi CRM</title><style>body{font:14px system-ui;margin:16px}.card{background:#f5f7fb;padding:10px;margin:8px 0;border-radius:7px}input,textarea,button,select{box-sizing:border-box;width:100%;padding:7px;margin:4px 0}button{background:#1565c0;color:#fff;border:0;border-radius:4px}.err{color:#b00020}</style><h3>CRM Bridge</h3><select id="calls"></select><div id="info" class="card">Chọn cuộc gọi</div><div class="card"><b>Quick Ticket</b><input id="subject" placeholder="Tiêu đề"><textarea id="message" placeholder="Nội dung"></textarea><button onclick="ticket()">Tạo Ticket CRM</button></div><div class="card"><b>Callback</b><input id="when" type="datetime-local"><textarea id="note" placeholder="Ghi chú"></textarea><button onclick="callback()">Lưu Callback</button></div><div id="result"></div><script>const q=new URLSearchParams(location.search),s=q.get('session');let id;const api=(p,o={})=>fetch('/crm-bridge/'+p+(p.includes('?')?'&':'?')+'session='+encodeURIComponent(s),o).then(r=>r.json());const out=x=>result.innerHTML='<p class="'+(x.error?'err':'')+'">'+(x.error||x.message||JSON.stringify(x))+'</p>';api('v1/agent/calls').then(x=>{(x.calls||[]).forEach(c=>calls.add(new Option(c.phone+' · '+c.created_at,c.call_id)));calls.onchange=load;load()});function load(){id=calls.value;if(!id)return;api('v1/calls/'+id).then(x=>info.innerHTML=x.error?x.error:`<b>${x.phone}</b><br>Campaign: ${x.campaign_id||'-'}<br>CRM Customer: ${x.crm_customer_id||'chưa map'}`)}function ticket(){api('v1/calls/'+id+'/tickets',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request_id:crypto.randomUUID(),subject:subject.value,message:message.value,department:1})}).then(x=>out(x.ticket_id?{message:'Đã tạo Ticket #'+x.ticket_id}:x))}function callback(){api('v1/calls/'+id+'/callbacks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({when:when.value,note:note.value})}).then(x=>out(x.saved?{message:'Đã lưu callback'}:x))}</script>'''


def application(config=None):
    config = config or {'DB_PATH': os.getenv('BRIDGE_DATABASE_URL', os.getenv('BRIDGE_DB_PATH', 'bridge.sqlite3')), 'CRM_BASE_URL': os.environ['CRM_BASE_URL'], 'CRM_API_TOKEN': os.environ['CRM_API_TOKEN'], 'OML_SYNC_URL': os.environ['OML_SYNC_URL'], 'BRIDGE_API_KEY': os.environ['BRIDGE_API_KEY'], 'SHARED_SECRET': os.environ['BRIDGE_SHARED_SECRET'], 'QUEUE_DEPARTMENTS': department_mapping(os.getenv('BRIDGE_QUEUE_DEPARTMENTS', '{}')), 'CRM_REQUEST_INTERVAL_SECONDS': float(os.getenv('CRM_REQUEST_INTERVAL_SECONDS', '1.5'))}
    bridge = Bridge(config)
    if bridge.store.postgres:
        threading.Thread(target=bridge.run_call_log_consumer, daemon=True).start()
    def app(environ, start_response):
        path, method = environ['PATH_INFO'], environ['REQUEST_METHOD']; query = parse_qs(environ.get('QUERY_STRING', ''))
        def respond(status, body, content='application/json'):
            raw = json.dumps(body).encode() if content == 'application/json' else body.encode(); start_response(status, [('Content-Type', content), ('Content-Length', str(len(raw)))]); return [raw]
        if path == '/health': return respond('200 OK', {'ok': True})
        session = bridge.session(query.get('session', [''])[0])
        if path == '/workspace' and method == 'GET':
            if not session: return respond('401 Unauthorized', 'Unauthorized', 'text/plain')
            return respond('200 OK', WORKSPACE_HTML, 'text/html; charset=utf-8')
        if method == 'POST' and environ.get('HTTP_X_BRIDGE_API_KEY') != config['BRIDGE_API_KEY'] and not session: return respond('401 Unauthorized', {'error': 'unauthorized'})
        length = int(environ.get('CONTENT_LENGTH') or 0); data = json.loads(environ['wsgi.input'].read(length) or b'{}') if length else {}
        try:
            if path == '/v1/sync' and method == 'POST': return respond('200 OK', bridge.sync_contacts())
            if path == '/v1/calls' and method == 'POST': bridge.store.create_call(data); return respond('201 Created', {'workspace_url': '/workspace?call_id=' + data['call_id']})
            if path == '/v1/telephony-events' and method == 'POST':
                bridge.store.create_call(data); bridge.store.audit('telephony_event', data['call_id'], {'node_id': data.get('node_id'), 'event': data.get('event')}); return respond('201 Created', {'accepted': True})
            if path == '/v1/agent/calls' and method == 'GET':
                if not session: return respond('401 Unauthorized', {'error': 'unauthorized'})
                return respond('200 OK', {'calls': bridge.store.agent_calls(session['agent_id'])})
            if path.startswith('/v1/calls/') and method == 'GET':
                if not session and environ.get('HTTP_X_BRIDGE_API_KEY') != config['BRIDGE_API_KEY']: return respond('401 Unauthorized', {'error': 'unauthorized'})
                call = bridge.workspace(path.split('/')[3])
                if session and call and str(call['agent_id']) != session['agent_id']: return respond('403 Forbidden', {'error': 'forbidden'})
                return respond('200 OK', call or {'error': 'not found'})
            if path.startswith('/v1/calls/') and path.endswith('/tickets') and method == 'POST':
                call = bridge.workspace(path.split('/')[3])
                if session and (not call or str(call['agent_id']) != session['agent_id']): return respond('403 Forbidden', {'error': 'forbidden'})
                return respond('201 Created', bridge.create_ticket(path.split('/')[3], data['request_id'], data))
            if path.startswith('/v1/calls/') and path.endswith('/callbacks') and method == 'POST':
                call = bridge.workspace(path.split('/')[3])
                if session and (not call or str(call['agent_id']) != session['agent_id']): return respond('403 Forbidden', {'error': 'forbidden'})
                return respond('201 Created', bridge.callback(path.split('/')[3], data))
            return respond('404 Not Found', {'error': 'not found'})
        except ValueError as error: return respond('422 Unprocessable Entity', {'error': str(error)})
        except requests.RequestException: return respond('502 Bad Gateway', {'error': 'CRM or OmniLeads unavailable'})
    return app

if __name__ == '__main__':
    make_server('0.0.0.0', int(os.getenv('PORT', '8080')), application()).serve_forever()
