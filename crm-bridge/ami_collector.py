"""Forward Asterisk call events from *this* voice node to CRM Bridge.

The process deliberately has no local database. In an OmniLeads HA topology it
is installed once on every ``omnileads_voice`` node, beside Asterisk. The
Bridge endpoint persists normalized events into shared PostgreSQL state.
"""
import logging
import os
import re
import socket
import time

import requests


LOG = logging.getLogger("tekomi.crm_bridge.ami")
EXTENSION_RE = re.compile(r"(?:PJSIP|SIP)/([0-9]+)(?:[-/]|$)")
SUPPORTED_EVENTS = {"DialBegin", "BridgeEnter", "Hangup"}


def extension(channel):
    """Return an OmniLeads numeric SIP extension from an AMI channel."""
    match = EXTENSION_RE.search(channel or "")
    return match.group(1) if match else ""


def parse_message(raw):
    """Parse one AMI frame without interpreting its event type."""
    return dict(
        line.split(": ", 1)
        for line in raw.decode(errors="replace").split("\r\n")
        if ": " in line
    )


def event_payload(message, node_id):
    """Normalize a relevant AMI frame, or return ``None`` for irrelevant legs.

    A dial whose source is the agent is outbound, hence the called number is
    ``DestCallerIDNum``. For inbound dial, the agent is the destination and
    the caller is ``CallerIDNum``. This prevents showing an extension as the
    customer number in the global CRM panel.
    """
    if message.get("Event") not in SUPPORTED_EVENTS:
        return None
    source_extension = extension(message.get("Channel"))
    destination_extension = extension(message.get("DestChannel"))
    agent_extension = source_extension or destination_extension
    if not agent_extension:
        return None

    if source_extension:
        phone = message.get("DestCallerIDNum") or message.get("CallerIDNum")
    else:
        phone = message.get("CallerIDNum") or message.get("DestCallerIDNum")
    linked_id = message.get("Linkedid") or message.get("Uniqueid")
    if not phone or not linked_id:
        return None

    return {
        "call_id": f"{node_id}:{linked_id}",
        "node_id": node_id,
        "extension": agent_extension,
        "phone": phone,
        "event": message["Event"],
        "occurred_at": str(time.time()),
    }


def post_event(url, api_key, payload, http=requests):
    response = http.post(
        url, json=payload, headers={"X-Bridge-Api-Key": api_key}, timeout=3
    )
    response.raise_for_status()


def run(host, port, username, password, bridge_url, api_key, node_id):
    endpoint = bridge_url.rstrip("/") + "/v1/telephony-events"
    while True:
        try:
            with socket.create_connection((host, port), 10) as ami:
                # create_connection leaves its connect timeout on the socket.
                # AMI can be quiet for much longer than ten seconds, so switch
                # back to blocking mode after a successful connection.
                ami.settimeout(None)
                login = (
                    "Action: Login\r\nUsername: %s\r\nSecret: %s\r\n"
                    "Events: on\r\n\r\n"
                ) % (username, password)
                ami.sendall(login.encode())
                buffered = b""
                while True:
                    chunk = ami.recv(4096)
                    if not chunk:
                        raise ConnectionError("AMI connection closed")
                    buffered += chunk
                    while b"\r\n\r\n" in buffered:
                        raw, buffered = buffered.split(b"\r\n\r\n", 1)
                        payload = event_payload(parse_message(raw), node_id)
                        if payload:
                            post_event(endpoint, api_key, payload)
        except Exception as error:  # reconnect: Asterisk/container may restart
            LOG.warning("AMI collector reconnecting after error: %s", error)
            time.sleep(3)


def main():
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    run(
        host=os.getenv("ASTERISK_HOSTNAME", "127.0.0.1"),
        port=int(os.getenv("AMI_PORT", "5038")),
        username=os.environ["AMI_USER"],
        password=os.environ["AMI_PASSWORD"],
        bridge_url=os.environ["BRIDGE_URL"],
        api_key=os.environ["BRIDGE_API_KEY"],
        node_id=os.getenv("PBX_NODE_ID", os.getenv("ASTERISK_HOSTNAME", "unknown")),
    )


if __name__ == "__main__":
    main()
