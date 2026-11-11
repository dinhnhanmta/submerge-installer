# submerge

Gộp nhiều subscription bên thứ 3 thành một subscription riêng, chọn lọc máy chủ, để dán vào tab **Links** của client trong 3x-ui.

Kho này công khai. Cấu hình (nguồn subscription, máy chủ đã chọn, token) nằm trong `config.enc`, đã mã hóa AES-256 bằng mật khẩu sao lưu; không có mật khẩu thì không đọc được.

## Khôi phục trên VPS mới (1 dòng)

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/dinhnhanmta/submerge-installer/main/install.sh)
```

Lệnh hỏi mật khẩu sao lưu rồi cài dịch vụ, khôi phục nguồn, lựa chọn, token và khóa đẩy lên GitHub.

Link dán vào 3x-ui dùng tên miền (`SUBMERGE_PUBLIC_HOST` trong `/etc/submerge/env`) và token được khôi phục nguyên vẹn, nên link không đổi khi chuyển VPS: chỉ cần trỏ bản ghi DNS của tên miền về IP mới. Xem link bằng `submerge url`.

Nếu 3x-ui chạy trong Docker và chưa chạy lúc cài, chạy lại `bash /opt/submerge/install.sh` sau khi 3x-ui đã lên để lấy Xray cho lệnh `submerge test`.

## Lưu thay đổi lên GitHub (1 dòng)

```bash
submerge-backup
```

Chạy sau khi `submerge pick` / `unpick` hoặc sửa `/etc/submerge/sources.txt`.

## Quản lý máy chủ

```bash
submerge list            # danh mục kèm số thứ tự, dấu * = đang phát
submerge test 3 8 20-25  # test kết nối thật qua Xray (bỏ số = các máy chủ đang chọn)
submerge pick 3 8        # thêm vào subscription
submerge unpick 3        # bỏ khỏi subscription
submerge url             # in link để dán vào tab Links của 3x-ui
```

## Vị trí file trên VPS

| Đường dẫn | Nội dung |
|---|---|
| `/opt/submerge` | kho git này |
| `/etc/submerge/sources.txt` | các subscription nguồn, dạng `Tên \| URL` |
| `/etc/submerge/selected.txt` | máy chủ được phát, theo thứ tự |
| `/etc/submerge/env` | token, cổng, các tùy chọn |
| `/etc/submerge/backup.pass` | mật khẩu sao lưu (chỉ root đọc) |
| `/var/lib/submerge/state.json` | bản tải gần nhất của các nguồn (tự tạo lại) |
