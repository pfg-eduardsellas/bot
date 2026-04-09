import asyncio
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from botpy.core.engine import Engine
from database import SessionLocal
import db_models

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "5"))  # seconds between polls


async def run_scan(scan_id: int):
    """Process a single scan: run the engine and save results to DB."""
    db = SessionLocal()
    try:
        scan = db.query(db_models.Scan).filter(db_models.Scan.id == scan_id).first()
        if not scan:
            return

        scan.status = "running"
        db.commit()

        raw_form_data = scan.form_data or {}
        if isinstance(raw_form_data, str):
            raw_form_data = json.loads(raw_form_data)

        engine = Engine(
            start_url=scan.target_url,
            max_pages=scan.max_pages,
            max_depth=scan.max_depth,
            max_actions=scan.max_actions,
            form_data=raw_form_data,
            in_domain=scan.in_domain,
        )
        await engine.run()

        actions = [a.to_dict() for a in engine.actions_graph.values()]
        print(f"[Worker] Scan {scan_id} finished. {len(actions)} actions found. Saving...")

        db.query(db_models.ActionRecord).filter(db_models.ActionRecord.scan_id == scan_id).delete()
        for a in actions:
            db.add(db_models.ActionRecord(
                scan_id=scan_id,
                action_id=a["id"],
                custom_id=a.get("custom_id", ""),
                type=a["type"],
                selector=a.get("selector", ""),
                value=a.get("value", ""),
                depth=a.get("depth"),
                predecessors=json.dumps(a.get("predecessors", [])),
                successors=json.dumps(a.get("successors", [])),
                errors=json.dumps(a.get("errors", [])),
            ))

        scan.status = "done"
        scan.finished_at = datetime.utcnow()
        db.commit()
        print(f"[Worker] Scan {scan_id} saved successfully.")

    except Exception as exc:
        print(f"[Worker] Error on scan {scan_id}: {exc}")
        db.rollback()
        scan = db.query(db_models.Scan).filter(db_models.Scan.id == scan_id).first()
        if scan:
            scan.status = "error"
            scan.error_message = str(exc)
            scan.finished_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()


async def poll_loop():
    """Continuously poll for pending scans and process them one by one."""
    print(f"[Worker] Starting polling loop (interval={POLL_INTERVAL}s)...")
    while True:
        db = SessionLocal()
        try:
            scan = (
                db.query(db_models.Scan)
                .filter(db_models.Scan.status == "pending")
                .order_by(db_models.Scan.created_at.asc())
                .first()
            )
            scan_id = scan.id if scan else None
        finally:
            db.close()

        if scan_id:
            print(f"[Worker] Found pending scan {scan_id}. Processing...")
            await run_scan(scan_id)
        else:
            await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(poll_loop())
