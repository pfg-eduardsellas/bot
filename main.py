import argparse
import asyncio
import json
import os
import sys
from datetime import datetime

# Ensure the root of the bot repository is in the pythonpath
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from botpy.core.engine import Engine
from database import SessionLocal
import db_models

async def run_bot_worker(scan_id: int, target_url: str, max_urls: int = 3, max_depth: int = 5, max_actions: int = 100):
    print(f"[Worker] Starting scan {scan_id} for URL {target_url}...")
    db = SessionLocal()
    
    try:
        # Mark as running
        scan = db.query(db_models.Scan).filter(db_models.Scan.id == scan_id).first()
        if scan:
            scan.status = "running"
            db.commit()

        # Execute Engine
        raw_form_data = scan.form_data if scan and scan.form_data else {}
        if isinstance(raw_form_data, str):
            import json as _json
            raw_form_data = _json.loads(raw_form_data)
        form_data = raw_form_data
        engine = Engine(start_url=target_url, max_pages=max_urls, max_depth=max_depth, max_actions=max_actions, form_data=form_data)
        await engine.run()
        
        # Collect actions
        actions = []
        for a in engine.actions_graph.values():
            actions.append(a.to_dict())
            
        print(f"[Worker] Finished scraping. Found {len(actions)} actions. Saving to DB...")

        # Clear any existing actions just in case
        db.query(db_models.ActionRecord).filter(db_models.ActionRecord.scan_id == scan_id).delete()
        
        # Insert new actions
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
        
        # Mark scan as done
        if scan:
            scan.status = "done"
            scan.finished_at = datetime.utcnow()
        
        db.commit()
        print(f"[Worker] Scan {scan_id} completed successfully.")
        
    except Exception as exc:
        print(f"[Worker] Error during scan {scan_id}: {exc}")
        scan = db.query(db_models.Scan).filter(db_models.Scan.id == scan_id).first()
        if scan:
            scan.status = "error"
            scan.error_message = str(exc)
            scan.finished_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PFG Bot Worker")
    parser.add_argument("--scan-id", type=int, required=True, help="Database ID of the scan")
    parser.add_argument("--url", type=str, required=True, help="Target URL to crawl")
    parser.add_argument("--max-urls", type=int, default=3, help="Max unique URLs to visit")
    parser.add_argument("--max-depth", type=int, default=5, help="Max path depth to explore")
    parser.add_argument("--max-actions", type=int, default=100, help="Max total actions to gather")
    
    args = parser.parse_args()
    
    asyncio.run(run_bot_worker(args.scan_id, args.url, args.max_urls, args.max_depth, args.max_actions))
