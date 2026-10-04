# AutoBot — Telegram Giftcode Bot (Browser-Only)

Bot theo dõi 31 kênh Telegram, bắt mã quà tặng (ưu tiên mã **spoiler**), lọc mã hợp lệ rồi tự nhập vào 7 site qua trình duyệt **Edge kết nối CDP**. Bot không dùng HTTP/API để gửi mã và không giải CAPTCHA/Turnstile.

> **Bảo mật:** gói này KHÔNG chứa `.env` và file session Telegram (`*.session`). Bạn tự copy `.env` và session của mình vào thư mục dự án. Không gửi hai thứ này cho ai, không đưa vào Git.

---

## 1. Luồng xử lý

```
Telegram ──► nhận tin (sự kiện + bộ quét 1 giây) ──► lọc theo danh sách 31 kênh
        ──► hộp thư SQLite (chống trùng, phục hồi khi restart)
        ──► 6 worker: bắt mã (spoiler > caption/text; OCR chỉ kênh PHÁT CODE XX88)
        ──► kiểm tra mã (code_validator) ──► hàng đợi theo site
        ──► tab trình duyệt (Edge CDP 9222) điền form + gửi ──► đọc kết quả
```

- **Nhận tin:** bot lắng nghe sự kiện tin mới/tin sửa, đồng thời có bộ quét (`CHANNEL_POLL_INTERVAL`, mặc định 1 giây) hỏi Telegram tin trên cùng của cả 31 kênh bằng **một** yêu cầu. Bộ quét là lớp dự phòng cho trường hợp Telegram không đẩy tin kênh theo thời gian thực (từng thấy tin đến trễ ~15 phút). Tin trùng giữa hai đường được hộp thư bỏ qua.
- **Tin cũ:** tin quá `CODE_MAX_AGE_SECONDS` (mặc định 120 giây) bị bỏ. Chỉ nhận tin từ lúc bot khởi động trở đi (chừa 30 giây lệch đồng hồ).
- **Bắt mã:** nếu tin có mã spoiler hợp lệ thì dùng ngay, không quét thêm text. Mọi kênh chỉ đọc caption/text, **không tải media**. Chỉ kênh `PHÁT CODE XX88` (`-1002817093108`) được OCR ảnh/video; hằng khóa cứng `_OCR_ONLY_CHANNEL_ID` trong `main_script.py`, không phụ thuộc `.env`.
- **Gửi mã:** mỗi site có hồ sơ riêng (ô nhập, nút gửi, thời gian chờ, số tab) trong `browser_site_profiles.py`; tab được giữ sẵn ở trạng thái nóng.

---

## 2. Danh sách kênh (31)

| Nhóm | Kênh (ID) | Luật mã |
|---|---|---|
| **XX88** (xx88code.com) | PHÁT CODE XX88 `-1002817093108` (có OCR) · XX88 THỂ THAO ESPORT `-1002768264448` · XX88 DỊCH VỤ GIAI NHÂN `-1002730903277` · SOI KÈO CÙNG XX88 `-1004352437280` | 6–8 ký tự |
| **KJC → 4 site** | GÁI XINH `-1002528908352` · KJC - ĐỒNG HÀNH THỂ THAO `-1003503954906` | 6–8 ký tự; mỗi mã nhập cho cả MM88, GG88, RR88, XX88 |
| **RR88** (rr88code.com) | RR88 DỊCH VỤ GIAI NHÂN `-1002386905514` · RR88 SOI KÈO `-1004406362195` | 6–8 ký tự |
| **GG88** (gg88live.tv) | GG88 DỊCH VỤ GIAI NHÂN `-1003731231345` · KÈO BÓNG GG88 `-1004411105242` | 6–8 ký tự |
| **MM88** (livemm88.net) | MM88VIP Dịch Vụ Giai Nhân `-1003134541072` · MM88 ĐỘNG BÀN TƠ `-1002519029952` · GÁI 18+ `-1003936595246` · MM88 GIRL DANCE `-1003939163957` · SOI KÈO MM88 `-1003912975699` | 6–8 ký tự |
| **o8** (o8code.com) | 8 DỊCH VỤ GIAI NHÂN `-1003396129975` · o8 TIN HOT 24H `-1003802387209` · o8 - TROLL BÓNG ĐÁ `-1003574944644` · o8 SOI KÈO 24/7 `-1003904150684` | 6–8 ký tự |
| **QQ88** (tangquaqq88.com) | KHO GIF `-1002421765170` · TIN TỨC MỖI NGÀY `-1002377579866` · REVIEW PHIM HAY `-1002325212717` · GIẢI TRÍ `-1002324210129` · TIN HOT 24/7 `-1002278162941` | Chữ hoa + chữ thường; **chỉ nhận mã spoiler** |
| **Hi88** (hi88-freecode.pages.dev) | PHÁT CODE MIỄN PHÍ NỖ HŨ-BẮN CÁ `-1004435825431` · CƯỢC GIẢI TRÍ `-1003933844700` · KÊNH GIẢI TRÍ HOT `-1002695720902` · TIN HOT MỖI NGÀY `-1002657420328` · REVIEW PHIM HAY MỖI NGÀY `-1002662584621` · TUYỂN ĐẠI LÝ HOA HỒNG 60% `-1002625548636` · KHO GIF `-1002018121888` | Chữ hoa + chữ thường |

Danh sách này nằm trong `CHANNEL_CONFIG` ở `config.py`. Thêm/bớt kênh chỉ cần sửa ở đó.

### Bộ lọc mã (`code_validator.py`)
- Độ dài 6–8 cho nhóm XX88/RR88/GG88/MM88/o8 và hai kênh KJC.
- Chặn chữ quảng cáo và chuỗi chữ ghép hoàn toàn từ các từ quảng cáo (vd `CHUCMUNG`, `TANGLIXI`).
- Chuẩn hóa mã spoiler dạng `T*2*8*G*K*P*G*G` thành `T28GKPGG`.
- Chỉ coi mã là của site khác khi bắt đầu bằng đủ `MM88` hoặc `QQ88`; mã bắt đầu bằng `QQ` hay `M88` thường vẫn được nhận.

---

## 3. Site và tài khoản

| Site | Tab |
|---|---|
| xx88code.com, gg88live.tv, livemm88.net | 2 tab / 2 tài khoản |
| rr88code.com | 2 tab / 2 tài khoản (`kaoboy012`, `miniichan`) |
| tangquaqq88.com, hi88-freecode.pages.dev, o8code.com | 1 tab / 1 tài khoản |

Tài khoản theo từng site cấu hình ở `DOMAIN_ACCOUNT_OVERRIDES` trong `config.py`. Mỗi site có giới hạn tốc độ riêng (`REQUESTS_PER_MINUTE`, `MAX_BURST`, và biến riêng như `XX88_REQUESTS_PER_MINUTE`).

---

## 4. Cài đặt và chạy (Windows)

Yêu cầu: Python 3.11+, Microsoft Edge.

1. Giải nén vào `D:\AutoBot` (đường dẫn mặc định của `run.bat`).
2. Chạy `setup.bat` (tạo `.venv312`, cài `requirements.txt`). Không cần `playwright install`, bot chỉ kết nối vào Edge đang chạy.
3. Copy `.env.example` thành `.env`, điền tối thiểu `API_ID`, `API_HASH`, `SESSION_NAME`, `ALERT_BOT_TOKEN`, `ALERT_CHAT_ID`. Copy file session Telegram của bạn vào cùng thư mục.
4. Chạy `run.bat`: mở Edge với cổng CDP `127.0.0.1:9222` và profile cố định, rồi khởi động bot.
5. Mở từng site trong Edge, đăng nhập tài khoản và hoàn tất xác minh (Turnstile) bằng tay nếu site yêu cầu.

Chỉ chạy **một** bot tại một thời điểm. Không để hai tiến trình dùng chung session Telegram, database, profile Edge hay cổng 9222. Cổng CDP chỉ để ở `127.0.0.1`, không mở ra mạng.

### Biến `.env` hay chỉnh

| Biến | Ý nghĩa |
|---|---|
| `CHANNEL_POLL_INTERVAL` | Chu kỳ quét kênh (giây). Mặc định 1.0; `0` = tắt; tăng lên 2–3 nếu gặp FloodWait |
| `SINGLE_ROUND_PER_BATCH` | `false`: tin nhiều mã vẫn nhập đủ (mã dư xếp hàng). `true`: chỉ nhập số mã bằng số tab, mã dư bị bỏ |
| `ACCOUNTS_PER_CODE` | Số tài khoản nhập cho mỗi mã |
| `REQUESTS_PER_MINUTE`, `MAX_BURST` | Giới hạn tốc độ mỗi site. Tăng quá mức có thể bị site hạn chế tài khoản |
| `CODE_MAX_AGE_SECONDS` | Tuổi tối đa của tin (mặc định 120) |
| `TAB_POOL_MEMORY_RELOAD_IDLE_SECONDS` | Tab rảnh bao lâu thì tải lại (mặc định 1800) |
| `OCR_DEBUG_TIMING` | Bật log thời gian OCR để tinh chỉnh |
| `STEALTH_JS_ENABLED` | `false` giữ nguyên dấu vân tay của Edge thật (khuyên dùng nếu hay vướng Cloudflare) |
| `EDGE_CDP_HOST`, `EDGE_CDP_PORT` | Địa chỉ CDP, phải khớp `run.bat` |

`TELEGRAM_FILTER_AT_SOURCE` hiện được bỏ qua trong code: bot luôn nhận rộng rồi tự lọc theo 31 kênh.

---

## 5. Kiểm tra bot chạy đúng

Trong log khởi động:
- `configured_cached=31/31` và `31/31 channels valid`: tài khoản đã thấy đủ 31 kênh. Thấp hơn nghĩa là tài khoản chưa tham gia kênh đó hoặc ID đã đổi.
- `📡 [Poll] bật poller kênh | mỗi 1.00s | 31 kênh`: bộ quét đang chạy.
- `Handler ready ... OCR-only=[-1002817093108]`: chỉ một kênh OCR.

Khi có tin mới:
- `📡 [Poll] chat=… tuổi=…s` và `📨 [Telegram accepted] … ingress_to_durable_ms=…`: tuổi tin nên chỉ vài giây, vài mili-giây vào hộp thư.
- Dòng `⏭️ bỏ tin cũ Ns (> 120s)` xuất hiện liên tục nghĩa là tin đang đến trễ.
- Dòng nhịp tim `💓` cho biết `connected`, `raw_updates`, `accepted`, `loop_lag`, `q`.

Lệnh quản trị gửi qua Telegram: `/status`, `/clear`.

---

## 6. Cấu trúc file

| File | Vai trò |
|---|---|
| `main_script.py` | Điểm vào: nhận tin Telegram, bộ quét kênh, worker, bắt mã spoiler/caption/OCR, KJC chia 4 site, quản lý tắt máy |
| `config.py` | Đọc `.env`; `CHANNEL_CONFIG` (31 kênh), tài khoản theo site, bộ lọc mã, OCR, giới hạn tốc độ |
| `code_validator.py` | Kiểm tra và chuẩn hóa mã, nhận diện chữ quảng cáo và mã site khác |
| `browser_engine.py` | Tab pool, điền form, gửi mã, Cloudflare, đọc kết quả |
| `browser_adapter.py` | Lớp nối giữa luồng xử lý và trình duyệt |
| `browser_site_profiles.py` | Ô nhập, nút gửi, thời gian chờ, số tab theo từng site |
| `durable_inbox.py` | Hộp thư SQLite/WAL: chống trùng, phục hồi, retry độc lập theo site/tài khoản |
| `image_code_extractor.py` | OCR ảnh/video (RapidOCR + ONNX Runtime) |
| `media_download_manager.py`, `media_helpers.py` | Tải media (chỉ cho kênh OCR) và tiện ích media |
| `queue_manager.py` | Giới hạn hàng đợi, bỏ tin khi quá tải |
| `submission_outcomes.py` | Phân loại kết quả gửi mã |
| `database.py` | Lịch sử mã, tài khoản, lượt gửi |
| `freshness.py`, `timing.py` | Tính tuổi tin và đo thời gian từng bước |
| `dashboard.py`, `monitoring.py` | Bảng theo dõi trực tiếp và giám sát hệ thống |
| `logger_setup.py` | Ghi log (`logs/bot_activity.log`, `logs/channels/`) |
| `features.py` | Phiên bản bot và cờ tính năng |
| `run.bat`, `setup.bat` | Chạy bot kèm Edge CDP; cài môi trường |
| `requirements.txt`, `.env.example`, `.gitignore` | Thư viện, mẫu cấu hình, loại trừ file nhạy cảm |

Thư mục `data/` (database) và `logs/` được bot tự tạo khi chạy. Sao lưu `data/telegram_inbox.db` trước khi nâng cấp.

---

## 7. Giới hạn đã biết

- Telegram có thể không đẩy tin kênh theo thời gian thực; bộ quét 1 giây là lớp dự phòng. Nguyên nhân gốc chưa xác định.
- Tin được sửa sau khi đăng hơn 120 giây vẫn bị coi là cũ (tuổi tính từ lúc đăng gốc).
- Thời gian OCR video của kênh PHÁT CODE XX88 (lấy khung hình ở giây thứ 8 và 18) chưa được đo và chỉnh bằng số liệu thật.
- Nếu Turnstile chưa xác minh, bot giữ nguyên trang và chờ, không reload, không gửi lại liên tục; mã đến lúc đó có thể hết hạn.
