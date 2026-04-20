from database import SessionLocal
import db_models


class BotLogger:
    """Writes log messages to the `logs` table in real time and to stdout."""

    def __init__(self, scan_id: int):
        self.scan_id = scan_id

    def log(self, message: str):
        print(message)
        db = SessionLocal()
        try:
            db.add(db_models.ScanLog(scan_id=self.scan_id, message=message))
            db.commit()
        except Exception as e:
            print(f"[BotLogger] Failed to write log to DB: {e}")
            db.rollback()
        finally:
            db.close()
