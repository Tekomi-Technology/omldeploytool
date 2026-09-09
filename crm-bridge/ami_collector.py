"""Run one adapter per OmniLeads voice node; it writes through the Bridge HA endpoint."""
import os,re,socket,time,requests
HOST=os.getenv('ASTERISK_HOSTNAME','127.0.0.1'); PORT=int(os.getenv('AMI_PORT','5038')); NODE=os.getenv('PBX_NODE_ID',HOST)
URL=os.environ['BRIDGE_URL'].rstrip('/')+'/v1/telephony-events'; KEY=os.environ['BRIDGE_API_KEY']
def ext(s):
    m=re.search(r'(?:PJSIP|SIP)/([0-9]+)',s or ''); return m.group(1) if m else ''
def main():
  while True:
   try:
    with socket.create_connection((HOST,PORT),10) as s:
     s.sendall(('Action: Login\r\nUsername: %s\r\nSecret: %s\r\nEvents: on\r\n\r\n'%(os.environ['AMI_USER'],os.environ['AMI_PASSWORD'])).encode()); buf=b''
     while True:
      buf+=s.recv(4096)
      while b'\r\n\r\n' in buf:
       raw,buf=buf.split(b'\r\n\r\n',1); d=dict(x.split(': ',1) for x in raw.decode(errors='ignore').split('\r\n') if ': ' in x)
       if d.get('Event') in ('DialBegin','BridgeEnter','Hangup'):
        phone=d.get('DestCallerIDNum') or d.get('CallerIDNum') or ''; linked=d.get('Linkedid') or d.get('Uniqueid')
        if phone and linked: requests.post(URL,json={'call_id':NODE+':'+linked,'node_id':NODE,'extension':ext(d.get('Channel')) or ext(d.get('DestChannel')),'phone':phone,'event':d['Event'],'occurred_at':str(time.time())},headers={'X-Bridge-Api-Key':KEY},timeout=3)
   except Exception: time.sleep(3)
if __name__=='__main__': main()
