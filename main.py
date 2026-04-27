import asyncio
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from botpy.core.engine import Engine
from botpy.core.test_runner import execute_test_path
from database import SessionLocal, engine, Base
import db_models
from bot_logger import BotLogger

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "5"))  # seconds between polls

Base.metadata.create_all(bind=engine)


# region Scheduler


def fire_due_schedules(current_hour: datetime):
    """
    Called once per hour when the hour changes.
    Finds all enabled TestPaths whose day+hour match right now and enqueues a run.
    """
    dow = str(current_hour.isoweekday())  # "1"=mon … "7"=sun
    hour = str(current_hour.hour)

    db = SessionLocal()
    try:
        test_paths = (
            db.query(db_models.TestPath)
            .filter(db_models.TestPath.enabled == True)
            .all()
        )

        due = [
            tp
            for tp in test_paths
            if tp.days_of_week
            and tp.hours
            and dow in tp.days_of_week.split(",")
            and hour in tp.hours.split(",")
        ]

        if due:
            print(
                f"[Scheduler] Firing {len(due)} test(s) for {current_hour.strftime('%A %H:00')}"
            )
            for tp in due:
                db.add(db_models.TestPathRun(test_path_id=tp.id, status="pending"))
            db.commit()
        else:
            print(
                f"[Scheduler] No tests scheduled for {current_hour.strftime('%A %H:00')}"
            )
    except Exception as e:
        print(f"[Scheduler] Error firing schedules: {e}")
        db.rollback()
    finally:
        db.close()


# endregion


# region Scan


async def run_scan(scan_id: int):
    """Process a single scan: run the engine and save results to DB."""
    logger = BotLogger(scan_id)
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
            accessibility=scan.accessibility if scan.accessibility is not None else True,
            owner_token=scan.owner.api_token if scan.owner else None,
            log_fn=logger.log,
        )
        await engine.run()

        actions = [a.to_dict() for a in engine.actions_graph.values()]
        logger.log(
            f"[Worker] Scan {scan_id} finished. {len(actions)} actions found. Saving..."
        )

        db.query(db_models.AccessibilityViolation).filter(
            db_models.AccessibilityViolation.scan_id == scan_id
        ).delete()
        db.query(db_models.ActionRecord).filter(
            db_models.ActionRecord.scan_id == scan_id
        ).delete()

        action_records: dict[int, db_models.ActionRecord] = {}
        for a in actions:
            record = db_models.ActionRecord(
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
            )
            db.add(record)
            action_records[a["id"]] = record

        db.flush()

        total_violations = 0
        for a in actions:
            record = action_records[a["id"]]
            for v in a.get("accessibility_violations", []):
                db.add(
                    db_models.AccessibilityViolation(
                        scan_id=scan_id,
                        action_record_id=record.id,
                        action_id=a["id"],
                        rule_id=v.get("rule_id", ""),
                        impact=v.get("impact"),
                        description=v.get("description"),
                        help_url=v.get("help_url"),
                        nodes=json.dumps(v.get("nodes", [])),
                    )
                )
                total_violations += 1

        if total_violations:
            logger.log(f"[Worker] Saved {total_violations} accessibility violation(s).")

        scan.status = "done"
        scan.finished_at = datetime.now(timezone.utc)
        db.commit()
        logger.log(f"[Worker] Scan {scan_id} saved successfully.")

    except Exception as exc:
        logger.log(f"[Worker] Error on scan {scan_id}: {exc}")
        db.rollback()
        scan = db.query(db_models.Scan).filter(db_models.Scan.id == scan_id).first()
        if scan:
            scan.status = "error"
            scan.error_message = str(exc)
            scan.finished_at = datetime.now(timezone.utc)
            db.commit()
    finally:
        db.close()


# endregion


# region Test Path


async def run_test_path_run(run_id: int):
    db = SessionLocal()
    try:
        run = (
            db.query(db_models.TestPathRun)
            .filter(db_models.TestPathRun.id == run_id)
            .first()
        )
        if not run:
            return

        test_path = (
            db.query(db_models.TestPath)
            .filter(db_models.TestPath.id == run.test_path_id)
            .first()
        )
        scan = (
            db.query(db_models.Scan)
            .filter(db_models.Scan.id == test_path.scan_id)
            .first()
        )

        action_records = (
            db.query(db_models.ActionRecord)
            .filter(db_models.ActionRecord.scan_id == scan.id)
            .all()
        )
        action_map = {r.action_id: r for r in action_records}

        node_ids = [int(x) for x in test_path.path.split(",")]

        form_data = scan.form_data or {}
        if isinstance(form_data, str):
            form_data = json.loads(form_data)

        run.status = "running"
        run.started_at = datetime.now(timezone.utc)
        db.commit()

        path_name = test_path.name or f"path#{test_path.id}"
        print(
            f"[Worker] Running test '{path_name}' (run #{run_id}, {len(node_ids)} steps)"
        )

    except Exception as e:
        print(f"[Worker] Error loading test run {run_id}: {e}")
        db.rollback()
        db.close()
        return

    result = await execute_test_path(node_ids, action_map, form_data, scan.id)

    try:
        run.status = "pass" if result["passed"] else "fail"
        run.finished_at = datetime.now(timezone.utc)
        run.result = result
        db.commit()
        print(
            f"[Worker] Test run #{run_id} finished: {'PASS' if result['passed'] else 'FAIL'}"
        )
    except Exception as e:
        print(f"[Worker] Error saving test run {run_id} results: {e}")
        db.rollback()
    finally:
        db.close()


# endregion


# region Loop
async def poll_loop():
    """Continuously poll for pending scans and test runs, firing schedules hourly."""
    print(f"[Worker] Starting polling loop (interval={POLL_INTERVAL}s)...")
    last_fired_hour = None

    while True:
        now = datetime.now(timezone.utc)
        current_hour = now.replace(minute=0, second=0, microsecond=0)

        if last_fired_hour != current_hour:
            fire_due_schedules(current_hour)
            last_fired_hour = current_hour

        db = SessionLocal()
        try:
            scan = (
                db.query(db_models.Scan)
                .filter(db_models.Scan.status == "pending")
                .order_by(db_models.Scan.created_at.asc())
                .first()
            )
            scan_id = scan.id if scan else None

            run = (
                db.query(db_models.TestPathRun)
                .filter(db_models.TestPathRun.status == "pending")
                .order_by(db_models.TestPathRun.triggered_at.asc())
                .first()
            )
            run_id = run.id if run else None
        finally:
            db.close()

        if scan_id:
            print(f"[Worker] Found pending scan {scan_id}. Processing...")
            await run_scan(scan_id)
        elif run_id:
            print(f"[Worker] Found pending test run {run_id}. Processing...")
            await run_test_path_run(run_id)
        else:
            await asyncio.sleep(POLL_INTERVAL)


# endregion


if __name__ == "__main__":
    asyncio.run(poll_loop())
