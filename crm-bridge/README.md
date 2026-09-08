# Tekomi CRM Bridge

Service độc lập kết nối OmniLeads với CRM qua REST API. CRM là nguồn dữ liệu
Contact; Bridge chỉ insert/update Contact Database `CRM_MASTER` của OmniLeads,
không xóa hoặc inactive dữ liệu ở OmniLeads.

## API

- `GET /health`
- `POST /v1/sync` — đồng bộ Customers/Contacts từ CRM sang OmniLeads.
- `POST /v1/calls` — ghi call context, trả URL workspace.
- `GET /workspace?call_id=...&token=...` — màn hình agent tối giản.
- `POST /v1/calls/<call_id>/tickets` — agent chủ động tạo Ticket CRM.

Các endpoint ghi yêu cầu `X-Bridge-Api-Key`. Bridge gọi OmniLeads bằng HMAC
(`X-Bridge-Timestamp`, `X-Bridge-Signature`); shared secret không nằm trong URL.

## Cấu hình

`CRM_BASE_URL`, `CRM_API_TOKEN`, `OML_SYNC_URL`, `BRIDGE_API_KEY`,
`BRIDGE_SHARED_SECRET`, `BRIDGE_DB_PATH`, `BRIDGE_QUEUE_DEPARTMENTS` (JSON).

Chạy local: `python3 bridge_app.py`.
