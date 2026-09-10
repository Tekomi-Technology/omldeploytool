# HANDOFF — Tekomi OmniLeads + CRM Bridge

> Cập nhật: 2026-09-09 (Asia/Ho_Chi_Minh).  
> Mục đích: bàn giao kỹ thuật để tiếp tục phát triển và vận hành dự án.  
> Bảo mật: không ghi mật khẩu, token, API key hoặc private key vào file này.

## 1. Trạng thái tổng quan

OmniLeads đang chạy production-test tại https://ivr.tekomi.vn trên một VPS AIO. CRM Bridge, Webphone/WebRTC và SIP device đã được triển khai.

| Hạng mục | Trạng thái | Ghi chú |
|---|---|---|
| OmniLeads Agent Console | Đang chạy | Django overlay omlapp:tekomi |
| CRM Bridge | Đã deploy | UI agent qua /crm-bridge/, state PostgreSQL chung |
| CRM sang Omni contacts | Đã làm | Một chiều, chỉ insert/update CRM_MASTER |
| Xóa hoặc inactive contact Omni | Không làm | Đây là quyết định nghiệp vụ bắt buộc |
| Quick Ticket CRM | Đã làm | Chỉ tạo khi agent chủ động bấm |
| Callback CRM Task | Đã làm | Chủ động bấm, idempotent và audit được |
| Call context / recording | Đã làm | Hợp nhất AMI realtime và call logger PostgreSQL |
| HA design | Đã làm | AMI adapter một bản trên mỗi Asterisk node |
| Zoiper / SIP device | Signaling pass | REGISTER Digest trả 200 OK; audio thật chưa test |
| Webphone / WebRTC | Signaling pass | WSS REGISTER trả Digest rồi 200 OK |
| Viettel trunk | Signaling đã xác minh | Cần giữ PAI và port provider đúng |

Không phát cuộc gọi PSTN chỉ để kiểm thử vì làm phiền người nhận và phát sinh cước. Test RTP/audio cần người vận hành mở Zoiper hoặc Webphone với số test được phép.

## 2. Repository, branch và deployment

### Deploy repository

- Source root local/VPS: omldeploytool/
- GitHub deploy remote: https://github.com/Tekomi-Technology/omldeploytool.git
- Upstream remote: GitLab OmniLeads (origin)
- Branch triển khai: feature/crm-bridge
- Commit deploy hiện tại: 6144266 — public SIP alias cho Zoiper

| Commit | Nội dung |
|---|---|
| 6144266 | Kamailio chấp nhận REGISTER tới public IP, hết 403 Not relaying |
| 2877780 | Kamailio host-network dùng Redis loopback AIO, sửa Registration Failed |
| 8807ecc | Tài liệu security/lifecycle Ticket, Callback, call context |
| bd356b0 | CRM callback tạo Task có audit/idempotency |
| 974d4e9 | Chặn browser agent gọi ingest endpoint nội bộ Bridge |
| 14f94d1 | Hợp nhất AMI realtime và CDR durable |
| f41065a | AMI collector giữ socket blocking khi idle |
| d42c0e2 | AMI collector chạy host network, đọc AMI node-local |
| 996e1b3 | Nhận call event SIP trực tiếp từ AMI adapter |
| ed79638 | PostgreSQL leader election cho durable call logger |

Push deploy repository:

    git push github feature/crm-bridge

Không push .env, secret, backup .env, hoặc tài liệu có credential lên GitHub.

### OmniLeads application fork và overlay

Source app dùng cho CRM adapter/softphone: ominicontacto_app/

- Fork: https://github.com/Tekomi-Technology/ominicontacto.git
- Branch: feature/crm-bridge
- Commit: 86e4f02d7 — durable PostgreSQL call consumer

Các thay đổi app:

- tekomi_crm_bridge_adapter/: HMAC contact sync và session ngắn hạn iframe.
- templates/agente/base_agente.html: panel CRM toàn cục Agent Console.
- Hook call logger gửi dữ liệu durable sang Bridge.
- Password SIP cố định mapping qua Redis OML:SIPDEV:<extension>.
- Endpoint agent hỗ trợ codec opus, alaw, ulaw.
- SIP agent device gọi trực tiếp được; call không qua campaign là manual call.

Trên VPS source Django nằm ở ~/omldeploytool/components-git-repo/django. Sau thay đổi app fork, pull code rồi chạy build-tekomi.sh. Không dùng full rebuild upstream vì frontend đang lỗi ERR_PNPM_PATCH_FAILED.

## 3. Kiến trúc

    Browser Agent                  Zoiper / IP Phone
         | WSS / HTTPS                    | SIP UDP :10060
         v                                v
    Nginx :443 ------------------> Kamailio (registrar, Redis location)
         |                                |
         v                                v
    OmniLeads Django                 Asterisk/ACD node ---> Viettel trunk / PSTN
         |                                |
         +--> signed agent session --> CRM Bridge <-- AMI adapter (mỗi voice node)
                                           |
                                           +--> PostgreSQL OmniLeads dùng chung
                                           +--> CRM REST API

Nguyên tắc kiến trúc:

1. CRM Bridge là service độc lập. Không sửa Perfex CRM và không rải logic vào OmniLeads core.
2. CRM là nguồn contact. Sync chỉ CRM sang OmniLeads CRM_MASTER, insert/update only; không delete/disable.
3. Không tự tạo Ticket cho mọi call. Agent phải bấm Quick Ticket hoặc Callback.
4. Adapter AMI chạy theo Asterisk node; state của Bridge ở PostgreSQL chung, không bám một Asterisk cố định.
5. Node Asterisk chết giữa cuộc gọi vẫn làm mất call đó theo giới hạn tự nhiên Asterisk. Node khác và data đã persist không phụ thuộc node chết.

## 4. CRM Bridge

| Thành phần | Vị trí | Vai trò |
|---|---|---|
| Bridge service | crm-bridge/bridge_app.py | REST API, sync, workspace, ticket/callback, audit |
| AMI adapter | crm-bridge/ami_collector.py | DialBegin, BridgeEnter, Hangup node-local |
| Django adapter | ominicontacto_app/tekomi_crm_bridge_adapter/ | HMAC sync và agent iframe session |
| Global UI | base_agente.html | Panel CRM trong Agent Console |
| Reverse proxy | docker-compose/prod-env/crm-bridge-proxy.conf | HTTPS /crm-bridge/ tới loopback port 18080 |

API:

- GET /crm-bridge/health: healthcheck công khai.
- POST /v1/sync: sync CRM contact; chỉ server key.
- POST /v1/calls và POST /v1/telephony-events: ingest nội bộ; chỉ server key.
- GET /workspace?session=...: UI iframe; session Django ký và TTL ngắn.
- POST /v1/calls/<call_id>/tickets: agent chủ động tạo Ticket.
- POST /v1/calls/<call_id>/callbacks: agent chủ động tạo CRM Task callback.

Browser session không có quyền gọi endpoint ingest. Bridge gọi Django bằng HMAC timestamp/signature. Call, ticket request, callback request và audit log được lưu PostgreSQL OmniLeads. SQLite chỉ là fallback test local.

CRM requests có interval mặc định 1.5 giây qua CRM_REQUEST_INTERVAL_SECONDS để tránh CRM/provider chặn 403 do request dồn dập.

Luồng UI:

1. Agent đăng nhập OmniLeads và mở panel CRM.
2. Bridge chỉ hiển thị call mà agent có quyền xem.
3. Agent chọn call, xem phone/contact/campaign/recording.
4. Agent bấm Quick Ticket hoặc Callback.
5. Không có action agent: chỉ call log/audit, không tạo Ticket hoặc Task.

### HA deployment

AIO hiện tại:

- crm-bridge bind 127.0.0.1:18080.
- crm-bridge-ami dùng network_mode host, AMI 127.0.0.1, node id aio-1.

Khi lên HA:

1. Deploy crm-bridge-ami lên mỗi omnileads_voice host.
2. ASTERISK_HOSTNAME=127.0.0.1 và PBX_NODE_ID riêng, ổn định theo node.
3. BRIDGE_URL phải là Bridge service/VIP chung, không dùng loopback AIO.
4. BRIDGE_DATABASE_URL phải trỏ PostgreSQL RW VIP/shared service.
5. Giữ PostgreSQL leader election cho durable call_logger.

Tài liệu ngắn nằm ở crm-bridge/README.md.

## 5. Telephony và softphone

### Dữ liệu production hiện biết

| Mục | Giá trị |
|---|---|
| Domain | ivr.tekomi.vn |
| VPS public IP | 103.72.57.182 |
| Campaign test | OutTest, manual, Active |
| Agent/extension | ag1/1002, ag2/1003, ag4/1004, ag5/1005, ag6/1006 |
| Device test | ag6, extension 1006 |
| Inbound DID | 02483801899 |
| Inbound route | DID_HN |
| Outbound route | out_02483801899, pattern 0X. |

OutTest hiện có đủ các agent trên, bao gồm ag6/1006. Không suy đoán ag1 là extension 1001: hiện tại ag1 là 1002.

### Quy tắc campaign và trunk

- SIP registration không phụ thuộc campaign. Không cần bỏ DID/trunk khỏi campaign để Zoiper/Webphone đăng ký.
- DID có thể vừa route inbound IVR/campaign vừa là caller identity outbound nếu trunk/provider cho phép.
- Agent phải thuộc campaign để nhận/thực hiện đúng workflow OmniLeads của campaign đó.
- Zoiper direct call có thể vào manual flow với campana_id=0; vẫn có CDR và Bridge context nhưng không là KPI riêng OutTest.
- Cần KPI/form/callback workflow campaign thì originate từ UI/lifecycle campaign, không quay số SIP direct.

### Fixes Registration Failed đã deploy

1. Kamailio host-network AIO dùng Redis 127.0.0.1, không dùng public IP. Sai host làm usrloc không lưu contact.
2. Kamailio khai báo SIP alias internal và public IP. Thiếu public alias làm Zoiper REGISTER bị 403 Not relaying.
3. Registrar Kamailio đặt max_contacts=3. Một extension tối đa ba contact; inbound có thể rung nhiều thiết bị, thiết bị trả lời đầu tiên nhận call.
4. WebRTC credential là ephemeral. Sau recreate hoặc TTL hết hạn phải hard reload browser. Zoiper phải unregister/register lại sau recreate.

### Trunk Viettel

- Inbound provider tới 103.72.57.182:52000, iptables redirect vào Asterisk :5060.
- Outbound provider: 125.212.225.105:33333.
- Bắt buộc P-Asserted-Identity chứa DID; thiếu PAI SBC có thể im lặng.
- Trunk cần remote_hosts=125.212.225.105:33333, identify match IP provider, endpoint/send_pai=yes.
- Public Asterisk SIP ports không mở; chỉ Kamailio UDP :10060 cho device có password.

## 6. Kiểm thử đã thực hiện

| Test | Kết quả |
|---|---|
| Unit test Bridge | 11/11 pass: python3 -m unittest discover -s crm-bridge/tests -v |
| Contact sync simulation | Pass: upsert CRM contact sang CRM_MASTER, không delete/inactive |
| Durable call enrichment | Pass: AMI/CDR cùng call id hợp nhất agent/contact/campaign/recording |
| Agent Quick Ticket | Pass: session agent tạo CRM Ticket thật |
| Agent Callback | Pass: CRM Task thật, idempotent/audited |
| HTTPS bridge health | Pass: /crm-bridge/health trả {"ok":true} |
| Fixed-password SIP REGISTER | Pass: Digest 200 OK qua Kamailio public SIP |
| WebRTC WSS REGISTER | Pass: WSS nhận 401 Digest, sau đó 200 OK |
| Softphone/PSTN audio hai chiều | Chưa hoàn tất | Cần thiết bị Zoiper/Linphone thật |
| HA node failover | Chưa thực hiện | Chưa có nhiều Asterisk voice node |

## 7. Deploy và vận hành

### Cập nhật VPS

    ssh tekomi@103.72.57.182
    cd ~/omldeploytool
    git switch feature/crm-bridge
    git pull --ff-only github feature/crm-bridge
    cd docker-compose/prod-env

docker-compose/env có dòng logger không an toàn để source nguyên file. Khi chạy Compose có biến CRM, chỉ source assignment hợp lệ hoặc dùng environment quản trị sẵn. Không chạy Compose khi CRM variables bị mất, vì config có thể render rỗng.

### Recreate đúng phạm vi

    # Sau thay đổi Bridge/compose
    docker compose up -d --build crm-bridge crm-bridge-ami nginx

    # Sau thay đổi Kamailio
    docker compose up -d --build kamailio-webrtc

    # Sau thay đổi app fork/overlay
    ./build-tekomi.sh
    docker compose up -d django-app django-commands

Không dùng oml_manage.sh rebuild full stack vì frontend build đang lỗi ERR_PNPM_PATCH_FAILED.

### Healthcheck và chẩn đoán nhanh

    cd ~/omldeploytool/docker-compose/prod-env
    docker compose ps
    curl -kfsS https://ivr.tekomi.vn/crm-bridge/health
    docker logs prod-env-crm-bridge-1 --since 15m
    docker logs prod-env-crm-bridge-ami-1 --since 15m
    docker logs prod-env-kamailio-webrtc-1 --since 15m
    docker exec prod-env-acd-1 asterisk -rx 'pjsip show endpoints'
    docker exec prod-env-acd-1 asterisk -rx 'pjsip show endpoint 1006'
    docker exec prod-env-django-app-1 python3 manage.py verificar_families_redis

| Dấu hiệu | Hướng xử lý |
|---|---|
| db_redis_connect hoặc usrloc updating contact failed | Kiểm tra REDIS_HOSTNAME Kamailio |
| 403 Not relaying khi Zoiper REGISTER | Kiểm tra public IP alias custom Kamailio config |
| auth_ephemeral expired | Webphone credential cũ; reload Agent Console |
| PJSIP endpoint Unavailable | Device chưa đăng ký/contact hết hạn; không kết luận trunk/campaign lỗi |

### Cron guard host

- Mỗi 10 phút: oml_redis_guard.sh chạy verificar_families_redis.
- Mỗi 30 phút: oml_ws_guard.sh restart acd-config-generator nếu mất WebSocket session.
- Redis delete trap ghi OML:* delete/unlink vào /var/log/oml_redis_del.log.

## 8. Việc còn lại theo ưu tiên

1. Test Zoiper audio thật: đăng ký 1006, gọi inbound/outbound, xác nhận chuông, thoại hai chiều và hangup; lưu call id/thời gian nếu lỗi.
2. Kiểm tra report nghiệp vụ: chốt KPI có bao gồm manual SIP direct (campana_id=0) hay chỉ campaign call.
3. HA infrastructure test: dựng tối thiểu hai voice node và Bridge endpoint/VIP chung, test node failure.
4. Hoàn thiện CRM UI theo nghiệp vụ: ticket fields, callback template, mapping contact/campaign/department, quyền agent/supervisor.
5. Bảo mật: revoke token từng lộ qua chat; dùng secret manager/Ansible Vault; thay self-signed certificate bằng public certificate nếu agent truy cập Internet.
6. Capacity planning: baseline 4 WebRTC calls từng đo Asterisk khoảng 50% CPU và rtpengine khoảng 11%; cần load test kiểm soát trước khi tăng CCU.

## 9. Nguyên tắc khi phát triển tiếp

- Luôn bắt đầu bằng git status; worktree có thể có thay đổi local. Không reset/checkout phá hủy.
- Chỉ commit file thuộc thay đổi đang làm. Không add .env, HANDOFF-SECRETS.md, backup hoặc file local.
- CRM Bridge phải giữ là module độc lập: thêm API/UI ở Bridge và adapter mỏng OmniLeads, không patch rải core.
- Giữ semantics contact sync: CRM sang Omni, insert/update only, không delete/inactive Omni contact.
- Không auto-create Ticket cho tất cả call; chỉ action rõ ràng agent mới tạo Ticket/Callback CRM.
- Với SIP/Kamailio, test cả WebRTC và fixed-password SIP REGISTER trước, sau đó test audio thật.
- Với HA, mọi state ở PostgreSQL/Redis/shared service; process theo voice node chỉ xử lý event node-local.
