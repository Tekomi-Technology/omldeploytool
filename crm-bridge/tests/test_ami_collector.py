import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from ami_collector import event_payload, parse_message


class AmiCollectorTests(unittest.TestCase):
    def test_outbound_uses_destination_as_customer_number(self):
        event = parse_message(
            b"Event: DialBegin\r\nChannel: PJSIP/1006-0001\r\n"
            b"DestChannel: PJSIP/trunk-0002\r\nCallerIDNum: 1006\r\n"
            b"DestCallerIDNum: 0342387314\r\nLinkedid: call-1"
        )
        payload = event_payload(event, "voice-a")
        self.assertEqual(payload["call_id"], "call-1")
        self.assertEqual(payload["extension"], "1006")
        self.assertEqual(payload["phone"], "0342387314")
        self.assertEqual(payload["direction"], "outbound")

    def test_inbound_uses_caller_as_customer_number(self):
        event = parse_message(
            b"Event: DialBegin\r\nChannel: PJSIP/trunk-0001\r\n"
            b"DestChannel: PJSIP/1006-0002\r\nCallerIDNum: 02483801899\r\n"
            b"DestCallerIDNum: 1006\r\nLinkedid: call-2"
        )
        payload = event_payload(event, "voice-b")
        self.assertEqual(payload["call_id"], "call-2")
        self.assertEqual(payload["extension"], "1006")
        self.assertEqual(payload["phone"], "02483801899")
        self.assertEqual(payload["direction"], "inbound")

    def test_ignores_non_agent_or_unrelated_event(self):
        self.assertIsNone(event_payload({"Event": "Newchannel"}, "voice-a"))
        self.assertIsNone(event_payload({"Event": "DialBegin", "Channel": "PJSIP/trunk-x"}, "voice-a"))


if __name__ == "__main__":
    unittest.main()
