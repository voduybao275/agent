---
name: html-tu-anh
description: Dựng lại giao diện trong ảnh thành HTML/CSS một file, giữ đúng chữ, bố cục và màu sắc.
when_to_use: Khi người dùng đính kèm ảnh giao diện/website/mockup và yêu cầu viết HTML, CSS, dựng lại trang hoặc chuyển ảnh thành code.
---

# Dựng HTML từ ảnh giao diện

Ảnh đã được đọc và mô tả (đặc tả UI) trước khi skill này được gọi. Dùng đặc tả đó làm nguồn sự thật, không tự đoán lại những gì đã có trong đặc tả.

## Quy trình

1. **Chốt cấu trúc** từ phần BỐ CỤC: liệt kê các khối theo thứ tự (header, nav, main, aside, footer, card...). Mỗi khối là một thẻ semantic, không lạm dụng `<div>`.
2. **Gom token thiết kế** từ phần MÀU SẮC và KIỂU CHỮ: đặt biến CSS cho nền, chữ, màu nhấn, viền, bo góc, font, cỡ chữ. Dùng đúng mã hex trong đặc tả; chỗ ghi "ước lượng" thì chọn giá trị gần nhất.
3. **Dựng từng khối** theo tỉ lệ và khoảng cách trong đặc tả; dùng flex/grid để tái tạo bố cục thay vì toạ độ tuyệt đối.
4. **Chép chữ nguyên văn** từ phần TOÀN BỘ CHỮ — không viết lại, không dịch, không bỏ sót nhãn/placeholder.
5. **Tự kiểm tra** trước khi trả lời:
   - Đủ mọi khối và mọi chữ nhìn thấy trong ảnh.
   - Màu nền/chữ/nút khớp đặc tả; độ tương phản đọc được.
   - Không có tài nguyên ngoài (không link CDN, font Google, ảnh mạng).
   - Thu gọn được trên màn hình hẹp mà không vỡ bố cục.

## Định dạng trả lời

- Nếu người dùng yêu cầu lưu file: gọi `write_file` với toàn bộ nội dung (mặc định `index.html`), rồi trả lời ngắn gọn đường dẫn và những điểm đã ước lượng.
- Nếu không: trả một khối ```html duy nhất chứa cả `<style>`, sau đó 2-3 gạch đầu dòng nêu phần ước lượng (màu, font, khoảng cách).
- Không mô tả lại ảnh, không giải thích từng dòng CSS.
