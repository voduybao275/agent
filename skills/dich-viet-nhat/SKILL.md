---
name: dich-viet-nhat
description: Dịch Việt ↔ Nhật tự nhiên, đúng kính ngữ và ngữ cảnh; giữ nguyên thuật ngữ, số liệu và định dạng.
when_to_use: Khi người dùng yêu cầu dịch văn bản, email, tài liệu hoặc hội thoại giữa tiếng Việt và tiếng Nhật (cả hai chiều).
---

# Dịch Việt ↔ Nhật

## 1. Xác định yêu cầu

- Chiều dịch: Việt → Nhật hay Nhật → Việt. Nếu người dùng không nói, suy ra từ ngôn ngữ nguồn.
- Ngữ cảnh và mức lịch sự: email khách hàng, tài liệu kỹ thuật, hợp đồng, chat, quảng cáo...
- Nếu người dùng đưa đường dẫn file, đọc bằng `read_file` rồi dịch toàn bộ; nếu văn bản đã dán sẵn thì dịch ngay.
- Chỉ hỏi lại khi thiếu thông tin làm thay đổi nghĩa (đối tượng đọc, mức kính ngữ). Trường hợp khác chọn mặc định hợp lý và ghi chú ngắn.

## 2. Nguyên tắc dịch

- Dịch ý, không dịch từng từ; câu đích phải tự nhiên với người bản ngữ.
- Không thêm, bớt hay bình luận nội dung. Không tự sửa lỗi nguồn; nếu nguồn sai rõ ràng, dịch đúng ý định và ghi chú.
- Giữ nguyên tên riêng, số liệu, đơn vị, mã code, URL, email, tên sản phẩm. Chuyển định dạng ngày theo thông lệ nước đích (dd/mm/yyyy ↔ yyyy年m月d日).
- Giữ cấu trúc: tiêu đề, danh sách, bảng, xuống dòng, thụt lề.
- Nhất quán thuật ngữ trong toàn văn. Thuật ngữ kỹ thuật lần đầu có thể ghi "bản dịch (nguyên ngữ)".

## 3. Khi dịch sang tiếng Nhật

- Chọn thể phù hợp: です・ます cho email/công việc thông thường; 敬語 (尊敬語/謙譲語) khi gửi khách hàng hoặc cấp trên; 常体 cho ghi chú nội bộ, chat thân mật.
- Email: giữ cấu trúc 件名 → 宛名 → 挨拶 → 本文 → 結び. Dùng 「お世話になっております」 khi phù hợp; không bịa tên công ty/người nhận — dùng 〇〇 nếu nguồn không có.
- Câu dài tiếng Việt: tách thành câu ngắn, rõ chủ ngữ; tránh dịch máy móc các từ "của/các/những".
- Tên người Việt: phiên âm katakana theo cách đọc; nếu không chắc cách đọc thì giữ chữ Latin và ghi chú.
- Chỗ không đọc được (chữ mờ, ký tự lạ): giữ nguyên và đánh dấu ※要確認.

## 4. Khi dịch sang tiếng Việt

- Chỉ dùng chữ Quốc ngữ; không để lẫn chữ Hán/Trung Quốc trong bản dịch.
- Dịch tự nhiên, không bê nguyên trật tự câu Nhật; bỏ trợ từ/đuôi câu không mang nghĩa.
- Chuyển 敬語 thành lời lẽ lịch sự tương đương (ạ/vâng/kính gửi...), tránh giữ mức trang trọng gây gượng.
- Dịch sát hàm ý của 婉曲表現 (cách nói giảm, nói tránh); không biến thành phủ định mơ hồ.
- Lượng từ và cách nói ước lượng (枚, 台, 本...): chuyển sang cách diễn đạt tiếng Việt tự nhiên.

## 5. Định dạng trả lời

- Mặc định: chỉ đưa bản dịch, không kèm giải thích.
- Nếu có lựa chọn đáng chú ý (kính ngữ, thuật ngữ, chỗ mơ hồ): thêm 1-3 dòng "Ghi chú:" sau bản dịch.
- Khi được yêu cầu: đưa thêm bản dịch thay thế hoặc bảng thuật ngữ.
- Văn bản dài: dịch từng phần theo đúng thứ tự; không tóm tắt trừ khi được yêu cầu.
