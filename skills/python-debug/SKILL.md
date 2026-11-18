---
name: python-debug
description: Khoanh vùng và sửa lỗi Python theo traceback, sửa tối thiểu rồi xác nhận lại bằng lệnh chạy.
when_to_use: Khi có traceback, lỗi import, test fail, lỗi encoding, hoặc hành vi sai trong code Python.
---

# Quy trình gỡ lỗi Python

1. **Đọc traceback từ dưới lên**: lỗi thật nằm ở dòng cuối; các frame phía trên cho biết đường đi tới đó.
2. **Tái hiện bằng lệnh nhỏ nhất**: chạy đúng file/test với `py -X faulthandler ...` hoặc `py -m pytest -x -q` để lấy lỗi sớm nhất. Dùng `run_shell` và đọc `exit_code`/`stderr`.
3. **Kiểm tra giả thuyết từng bước**: thêm print/log ở biên (input, kiểu dữ liệu, giá trị None); không sửa nhiều chỗ cùng lúc.
4. **Sửa tối thiểu** đúng nguyên nhân gốc; không bọc `try/except` chỉ để che lỗi.
5. **Chạy lại** test/lệnh ban đầu để xác nhận; nếu có test hồi quy thì thêm test đó.
6. **Lỗi môi trường**: kiểm tra phiên bản Python (`py --version`), venv đang active (`sys.executable`), package thiếu so với `requirements.txt`.

# Lưu ý trên Windows

- `python` có thể là Microsoft Store stub → dùng `py`.
- Console mặc định dùng code page cũ → in tiếng Việt bị lỗi; đặt `PYTHONIOENCODING=utf-8` hoặc `sys.stdout.reconfigure(encoding="utf-8")`.
- Đường dẫn có dấu cách phải quote; ưu tiên đường dẫn ngắn để tránh giới hạn MAX_PATH.

# Định dạng trả lời

- Nguyên nhân gốc: 1-2 câu kèm `file:line`.
- Cách sửa: diff hoặc code block.
- Xác nhận: lệnh đã chạy + kết quả.
