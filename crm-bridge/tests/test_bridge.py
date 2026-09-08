import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from bridge_app import Bridge, Store
from bridge_app import CrmClient


class FakeCrm:
    def __init__(self): self.tickets = []
    def list_all(self, resource):
        if resource == 'customers': return [{'userid': 7, 'company': 'BV Test'}]
        return [{'id': 11, 'userid': 7, 'firstname': 'An', 'lastname': 'Nguyen', 'email': 'a@example.test', 'phonenumber': '+84901234567'}]
    def create_ticket(self, payload): self.tickets.append(payload); return {'data': {'ticketid': 99}}


class FakeOmni:
    def __init__(self): self.calls = []
    def sync(self, contacts): self.calls.append(contacts); return {'created': len(contacts)}


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.db = tempfile.NamedTemporaryFile(delete=False); self.db.close()
        self.crm, self.omni = FakeCrm(), FakeOmni()
        self.bridge = Bridge({'DB_PATH': self.db.name, 'CRM_BASE_URL': 'https://crm.test/rest_api/v1',
                              'CRM_API_TOKEN': 'test', 'OML_SYNC_URL': 'https://oml.test/sync',
                              'SHARED_SECRET': 'test', 'QUEUE_DEPARTMENTS': {'CSKH': 3}},
                             crm=self.crm, omni=self.omni, store=Store(self.db.name))
    def tearDown(self): os.unlink(self.db.name)
    def test_sync_is_upsert_only(self):
        self.assertEqual(self.bridge.sync_contacts(), {'seen': 1, 'changed': 1})
        self.assertEqual(self.bridge.sync_contacts(), {'seen': 1, 'changed': 0})
        self.assertEqual(len(self.omni.calls), 1)
        self.assertEqual(self.omni.calls[0][0]['external_id'], 'crm:contact:11')
    def test_ticket_is_agent_action_and_idempotent(self):
        self.bridge.sync_contacts()
        self.bridge.store.create_call({'call_id': 'c-1', 'phone': '0901234567', 'agent_id': 2})
        data = {'subject': 'Can ho tro', 'message': 'Noi dung', 'queue': 'CSKH', 'request_id': 'r-1'}
        self.assertEqual(self.bridge.create_ticket('c-1', 'r-1', data), {'ticket_id': 99, 'replayed': False})
        self.assertEqual(self.bridge.create_ticket('c-1', 'r-1', data), {'ticket_id': '99', 'replayed': True})
        self.assertEqual(len(self.crm.tickets), 1)
        self.assertEqual(self.crm.tickets[0]['userid'], 7)
        self.assertIn('OML-CALL:c-1', self.crm.tickets[0]['message'])
    def test_ticket_requires_mapped_customer(self):
        self.bridge.store.create_call({'call_id': 'c-2', 'phone': '0999999999'})
        with self.assertRaisesRegex(ValueError, 'not mapped'):
            self.bridge.create_ticket('c-2', 'r-2', {'subject': 'x', 'message': 'x', 'queue': 'CSKH'})

    def test_crm_client_spaces_requests(self):
        class Response:
            def raise_for_status(self): pass
            def json(self): return {'data': [], 'meta': {'total': 0}}
        class Http:
            def get(self, *args, **kwargs): return Response()
        clock_values = iter([0.0, 0.2, 0.2])
        sleeps = []
        client = CrmClient('https://crm.test', 'token', http=Http(), min_interval=1.5,
                           clock=lambda: next(clock_values), sleeper=sleeps.append)
        client._request('get', '/customers')
        client._request('get', '/contacts')
        self.assertEqual(sleeps, [1.3])

if __name__ == '__main__': unittest.main()
