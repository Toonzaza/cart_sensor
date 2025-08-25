#!/usr/bin/env python3
# -*- coding: utf-8 -*-

def ascii_to_hex(s: str, uppercase: bool = False) -> str:
    """แปลงสตริง ASCII เป็น hex ต่อเนื่อง เช่น 'MXK22-1049' -> '4d584b32322d31303439'"""
    try:
        h = s.encode("ascii").hex()
    except UnicodeEncodeError:
        raise ValueError("สตริงต้องเป็น ASCII เท่านั้น (ห้ามมีอักขระไทย/ยูนิโค้ดอื่น)")
    return h.upper() if uppercase else h

def ascii_to_hex_grouped(s: str, word_bytes: int = 2, leading_colon: bool = True, uppercase: bool = False) -> str:
    """
    แปลงเป็น hex แล้วแบ่งกลุ่มตามขนาด word (หน่วย=ไบต์) เช่น word_bytes=2 -> กลุ่มละ 4 ตัวอักษร hex
    ตัวอย่าง: 'MXK22-1049' -> ':4d58:4b32:322d:3130:3439'
    """
    h = ascii_to_hex(s, uppercase=uppercase)
    step = word_bytes * 2  # ตัวอักษร hex ต่อหนึ่งกลุ่ม
    groups = [h[i:i+step] for i in range(0, len(h), step)]
    joined = ":".join(groups)
    return (":" + joined) if leading_colon else joined

def main():
    print("=== ASCII → HEX Converter (พิมพ์ q เพื่อออก) ===")
    print("โหมด: จะแสดงทั้งแบบ HEX ต่อเนื่อง และแบบแบ่ง WORD=2 ไบต์ (คั่นด้วย ':')\n")

    while True:
        try:
            s = input("กรอกข้อความ ASCII แล้วกด Enter: ")
        except (EOFError, KeyboardInterrupt):
            print("\nออกจากโปรแกรม")
            break

        if s.strip().lower() in {"q", "quit", "exit"}:
            print("ออกจากโปรแกรม")
            break

        if not s:
            print("** ว่างเปล่า ลองใหม่อีกครั้ง **\n")
            continue

        try:
            h = ascii_to_hex(s)  # ต่อเนื่อง
            g = ascii_to_hex_grouped(s, word_bytes=2, leading_colon=True)  # แบ่งกลุ่ม 2 ไบต์
        except ValueError as e:
            print(f"Error: {e}\n")
            continue

        print(f"HEX ต่อเนื่อง : {h}")
        print(f"HEX แบ่ง WORD  : {g}\n")

if __name__ == "__main__":
    main()
