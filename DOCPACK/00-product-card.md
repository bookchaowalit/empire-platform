---
title: Empire Platform — Product Card
doc_id: CARD-empire-platform
doc_type: product-card
verdict: invest
version: 0.1
date: 2026-10-09
status: ร่างเพื่อพิจารณา
owner: Product owner (Book Dev)
---

# Empire Platform — Product Card

## 1. สรุปคุณค่าในหนึ่งประโยค

ฐานกลางของ Solo Empire แบบออนไลน์ที่ให้ทุก domain cell ใช้ identity, workspace, สิทธิ์, delegation ของ agent และ audit ชุดเดียวกัน จึงเปิดให้ AI employees และผู้ใช้ภายนอกใช้งานได้อย่างปลอดภัย

## 2. ปัญหาและผู้ใช้เป้าหมาย

- ปัญหา: ข้อมูลของ Solo Empire อยู่ใน SQLite บนเครื่องเดียว ไม่มีแนวคิด workspace หรือ actor และ agent ยังไม่มีขอบเขตสิทธิ์
- ผู้ใช้: เจ้าของเป็น workspace แรก ส่วน agent และผู้ใช้ภายนอกตามมาในเฟส 2 และ 4

## 3. สิ่งที่ทำได้จริงตอนนี้

- schema `platform` บน PostgreSQL ในเครื่อง: identity, workspace, membership, role, policy, delegation, execution, approval และ audit
- RLS แบบ forced ทุกตาราง, role ของแอปที่ bypass ไม่ได้ และ delegation ที่ไม่เกินผู้มอบ
- เทสต์ 55 ข้อ และ mutation check ยืนยันว่าเทสต์จับการทำ RLS หลวมและการตัด grantor check ได้

## 4. หลักฐานสถานะ

| รายการ | ค่า |
|---|---|
| Stack | Python 3.12, PostgreSQL 16 (pgserver) |
| เทสต์ | 55 ผ่าน |
| Deploy config | ไม่มี (ยังไม่อนุมัติ Neon และ deploy) |
| Live URL | ไม่มี |

## 5. คุณค่าและจุดต่าง

- เป็น reference ของ cell contract ที่ cell อื่นจะทำตาม
- แยกสิทธิ์ของ agent ออกจาก auth provider จึงเปลี่ยน provider ได้โดยข้อมูลสิทธิ์ไม่หาย

## 6. ช่องว่างสำคัญ

- ยังไม่มี tasks cell, API, MCP, outbox และ graph (P1.3 เป็นต้นไป)
- ยังไม่ได้ทดสอบกับ Neon และ Clerk

## 7. คำตัดสิน

**`invest`** — เป็นฐานของ target architecture ที่เจ้าของเห็นชอบในหลักการ

## 8. ขั้นต่อไป

1. P1.3 `packages/authz` ที่ API และ MCP ใช้ร่วมกัน
2. P1.4–P1.5 tasks schema, ตัวย้ายข้อมูล และ tasks API

## การอนุมัติ

| บทบาท | สถานะ | วันที่ | ลงนาม |
|---|---|---|---|
| Product owner (Book Dev) | รอพิจารณา | — | ________ |

## แหล่งข้อมูล

- `README.md`
- `services/platform/migrations/`
- `tests/`
