---
name: code-review
description: Review mã nguồn theo checklist, chỉ ra bug, rủi ro bảo mật và đề xuất sửa cụ thể.
when_to_use: Khi người dùng yêu cầu review, đánh giá chất lượng, hoặc tìm lỗi trong code (Python, PHP, JS...).
---

# Quy trình code review

1. Xác định phạm vi cần review (file, thư mục, hay diff chưa commit). Nếu là thay đổi chưa commit, dùng tool `git_diff`; nếu là file cụ thể, đọc trực tiếp bằng `read_file`.
2. Đọc toàn bộ đoạn code liên quan trước khi nhận xét — không review dựa trên tên hàm.
3. Kiểm tra theo checklist:
   - **Đúng đắn**: input rỗng/None, off-by-one, race condition, xử lý lỗi, nhánh không bao giờ chạy.
   - **Bảo mật**: SQL injection, XSS, command injection, path traversal, lộ secret, thiếu kiểm tra quyền.
   - **Hiệu năng**: query N+1, vòng lặp lồng không cần thiết, đọc dữ liệu thừa.
   - **Dễ đọc**: tên biến/hàm, hàm quá dài, code chết, trùng lặp.
   - **Nhất quán**: theo convention sẵn có của dự án thay vì áp style bên ngoài.
4. Chỉ nêu vấn đề có bằng chứng cụ thể (file:line). Không bịa lỗi khi chưa đọc code.

# Định dạng trả lời

## Tóm tắt
1-2 câu về chất lượng tổng thể.

## Vấn đề
- Mức độ (cao/trung bình/thấp) — mô tả ngắn — vị trí `file:line`.

## Đề xuất sửa
Diff hoặc code block trước/sau, chỉ cho vấn đề mức cao và trung bình.

## Ghi chú
Điểm tốt hoặc câu hỏi cần làm rõ (nếu có).

Không tự sửa file trừ khi người dùng yêu cầu rõ ràng.

<!-- auto-learned:start -->
## Bài học tự động
- Cần nêu cụ thể các vấn đề và đề xuất sửa chữa trong mỗi phản hồi.
- Sử dụng ví dụ cụ thể từ đoạn code để minh họa vấn đề.
- Đề xuất một số sửa chữa cụ thể thay vì chỉ nói rằng không có vấn đề.
- Cung cấp thông tin về các tiêu chuẩn hoặc chuẩn mực dự án.
- Đảm bảo rằng mỗi phản hồi đều liên quan đến một vấn đề cụ thể.
- Sử dụng ngôn ngữ rõ ràng và dễ hiểu khi mô tả vấn đề.
<!-- auto-learned:end -->
