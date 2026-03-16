# Python Database Backup Tool

สคริปต์สำหรับแบ็กอัปฐานข้อมูล **MySQL** และ **PostgreSQL** รองรับ:

- เลือกฐานข้อมูลที่จะ backup ได้
- ตั้งเวลา backup อัตโนมัติ (รายวัน หรือทุก X นาที)
- บีบอัดไฟล์เป็น `.zip`
- ส่งข้อความแจ้งเตือนผลผ่าน Telegram
- คำนวณและสรุปเวลา backup (เวลารวม + แยกแต่ละฐานข้อมูล)

## ความต้องการเบื้องต้น

ต้องมีคำสั่งต่อไปนี้ในเครื่อง:

- `mysqldump` (สำหรับ MySQL)
- `pg_dump` (สำหรับ PostgreSQL)
- Python 3.9+ (ใช้ `zoneinfo`)

## เริ่มใช้งาน

1. คัดลอกไฟล์ config ตัวอย่าง

```bash
cp backup_config.example.json backup_config.json
```

2. แก้ค่าใน `backup_config.json`

- `telegram.bot_token` และ `telegram.chat_id`
- ข้อมูลเชื่อมต่อฐานข้อมูลใน `databases`
- ค่า schedule (`mode`, `time`, `interval_minutes`, `timezone`)

## คำสั่งใช้งาน

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
