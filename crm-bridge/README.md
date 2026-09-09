# Tekomi CRM Bridge

Service độc lập kết nối OmniLeads với CRM qua REST API. CRM là nguồn dữ liệu
Contact; Bridge chỉ insert/update Contact Database `CRM_MASTER` của OmniLeads,
không xóa hoặc inactive dữ liệu ở OmniLeads.

## API

- `GET /health`
- `POST /v1/sync` — đồng bộ Customers/Contacts từ CRM sang OmniLeads.
- `POST /v1/calls` — ghi/enrich call context; AMI realtime và call_logger
  dùng cùng Asterisk `Linkedid`, vì vậy chỉ tạo một call record.
- `GET /workspace?session=...` — màn hình agent, chỉ nhận session ngắn hạn do
  OmniLeads cấp cho agent đã đăng nhập.
- `POST /v1/calls/<call_id>/tickets` — agent chủ động tạo Ticket CRM.
- `POST /v1/calls/<call_id>/callbacks` — agent chủ động tạo CRM Task callback;
  request idempotent và audit được giữ ở Bridge.

Các endpoint ingest (`/v1/sync`, `/v1/calls`, `/v1/telephony-events`) yêu cầu
`X-Bridge-Api-Key`; browser agent chỉ có session 5 phút do Django phát hành và
chỉ được xem/tạo Ticket hoặc Callback của chính call mình. Bridge gọi
OmniLeads bằng HMAC (`X-Bridge-Timestamp`, `X-Bridge-Signature`); shared secret
không nằm trong URL.

## Cấu hình

`CRM_BASE_URL`, `CRM_API_TOKEN`, `OML_SYNC_URL`, `BRIDGE_API_KEY`,
`BRIDGE_SHARED_SECRET`, `BRIDGE_DB_PATH`, `BRIDGE_QUEUE_DEPARTMENTS` (JSON),
`CRM_REQUEST_INTERVAL_SECONDS` (mặc định `1.5`, giãn giữa mọi request CRM).

## HA và cuộc gọi SIP trực tiếp

`ami_collector.py` chạy **một bản trên mỗi node Asterisk**. Nó chỉ đọc AMI ở
node cục bộ và gửi `DialBegin`, `BridgeEnter`, `Hangup` tới Bridge; dữ liệu call
được ghi vào PostgreSQL dùng chung. Vì vậy extension do Kamailio quản lý vẫn có
thể gọi bằng Zoiper/softphone, và call context không bị buộc vào một Asterisk
cố định. Call đang diễn ra tại node bị hỏng vẫn là giới hạn tự nhiên của
Asterisk/OmniLeads; các node còn lại và dữ liệu đã ghi không phụ thuộc node đó.

Trong deployment HA, chạy image adapter trên mọi `omnileads_voice` host, đặt
`ASTERISK_HOSTNAME=127.0.0.1`, `PBX_NODE_ID` là định danh node ổn định và
`BRIDGE_URL` là VIP/service Bridge. Adapter dùng host network (giống
Asterisk), nên luôn đọc AMI cục bộ; AIO dùng `http://127.0.0.1:18080` để gọi
Bridge cục bộ, còn cụm HA đặt URL Bridge/VIP dùng chung.

Chạy local: `python3 bridge_app.py`.
