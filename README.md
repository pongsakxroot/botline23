# Python Database Backup Tool

สคริปต์สำหรับแบ็กอัปฐานข้อมูล **MySQL** และ **PostgreSQL** รองรับ:

- เลือกฐานข้อมูลที่จะ backup ได้
- ติ๊กข้ามตารางประเภท log ได้ (auto และระบุชื่อ table เอง)
- ตั้งเวลา backup อัตโนมัติ (รายวัน หรือทุก X นาที)
- บีบอัดไฟล์เป็น `.zip`
- ส่งข้อความแจ้งเตือนผลผ่าน Telegram
- คำนวณและสรุปเวลา backup (เวลารวม + แยกแต่ละฐานข้อมูล)
- แสดง progress เป็นเปอร์เซ็นต์ระหว่างทำงาน

## ความต้องการเบื้องต้น

ต้องมีคำสั่งต่อไปนี้ในเครื่อง:

- `mysqldump` (สำหรับ MySQL)
- `pg_dump` (สำหรับ PostgreSQL)
- Python 3.9+ (ใช้ `zoneinfo`)
- `tkinter` (สำหรับ GUI)  
  - Ubuntu/Debian: `sudo apt-get install -y python3-tk`

## เริ่มใช้งาน

1. คัดลอกไฟล์ config ตัวอย่าง

```bash
cp backup_config.example.json backup_config.json
```

2. แก้ค่าใน `backup_config.json`

- `telegram.bot_token` และ `telegram.chat_id`
- ข้อมูลเชื่อมต่อฐานข้อมูลใน `databases`
- ค่า schedule (`mode`, `time`, `interval_minutes`, `timezone`)
- การข้ามตาราง log (`skip_log_tables`, `log_table_keywords`, `exclude_tables`)

## คำสั่งใช้งาน

### GUI (แนะนำสำหรับตั้งค่าง่าย)

```bash
python3 backup_gui.py
```

ความสามารถใน GUI:

- หน้าจอแยกแท็บชัดเจน: `General / Databases / Schedule / Telegram / Monitor`
- ตั้งค่าทุกอย่างผ่าน GUI ได้ (ไม่ต้องแก้ JSON ด้วยมือ)
- จัดการรายการฐานข้อมูลแบบ Add/Edit/Remove
- ติ๊กข้ามตาราง log ได้ ทั้งระดับระบบและต่อฐานข้อมูล
- กด `Run Backup Now` เพื่อแบ็กอัปทันที
- กด `Start Auto Backup` / `Stop Auto Backup` สำหรับงานอัตโนมัติ
- ดู log การทำงานทุกขั้นตอนในแท็บ Monitor
- มี progress bar แสดงเปอร์เซ็นต์การทำงานแบบเรียลไทม์
- บันทึก/โหลดไฟล์ config JSON ได้

### 1) รันแบ็กอัปทันที (run once)

```bash
python3 backup_tool.py --config backup_config.json
```

### 2) เลือกเฉพาะบาง target

```bash
python3 backup_tool.py --config backup_config.json --targets main-mysql,main-postgres
```

### 3) โหมดตั้งเวลาอัตโนมัติ

```bash
python3 backup_tool.py --config backup_config.json --schedule
```

## รูปแบบ Schedule

ใน `backup_config.json`:

- `mode: "daily"` ใช้เวลาจาก `time` (รูปแบบ `HH:MM`)
- `mode: "interval"` ใช้ค่า `interval_minutes`

ตัวอย่าง:

```json
"schedule": {
  "enabled": true,
  "mode": "daily",
  "time": "02:00",
  "interval_minutes": 60,
  "timezone": "Asia/Bangkok"
}
```

## การข้ามตาราง log

ตั้งค่าจาก `backup_config.json`:

- `skip_log_tables`: เปิด/ปิดการข้ามตาราง log ทั้งระบบ
- `log_table_keywords`: คำที่ใช้ตรวจจับชื่อตาราง log
- `databases[].exclude_tables`: ชื่อตารางที่ต้องการข้ามเพิ่มเติม

ตัวอย่าง:

```json
"skip_log_tables": true,
"log_table_keywords": ["log", "logs", "audit", "history"],
"databases": [
  {
    "name": "main-mysql",
    "type": "mysql",
    "exclude_tables": ["temp_log_archive"]
  }
]
```

## โครงสร้างไฟล์แบ็กอัป

ไฟล์จะถูกเก็บใน:

```text
backups/YYYY-MM-DD/
```

โดยชื่อไฟล์จะเป็น:

```text
<target_name>_YYYYMMDD_HHMMSS.sql.zip
```

## หมายเหตุด้านความปลอดภัย

- อย่าเก็บรหัสผ่านจริงใน repo สาธารณะ
- แนะนำใช้ secret manager หรือ environment variables ในงาน production
