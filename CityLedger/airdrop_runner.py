"""Persisted batch outcomes. Unknown submissions are never automatically resent."""
import asyncio


async def execute_batches(service, store, key, run, private_key, gas_budget):
    for batch in run["batches"]:
        if batch["status"] != "not_sent":
            continue

        async def before_submit(digest):
            batch.update(status="submitted", digest=digest)
            await asyncio.to_thread(store.__setitem__, key, run)

        try:
            result = await service.transfer_batch(
                batch["recipients"], run["coin_type"], private_key, gas_budget, before_submit,
            )
        except Exception as exc:
            batch["status"] = "unknown" if batch.get("digest") else "not_sent"
            batch["error"] = str(exc)[:500]
            await asyncio.to_thread(store.__setitem__, key, run)
            break
        batch.update(status="sent" if result["success"] else "failed", digest=result["digest"])
        if result.get("error"):
            batch["error"] = result["error"][:500]
        await asyncio.to_thread(store.__setitem__, key, run)
        if not result["success"]:
            break


async def reconcile_batches(service, store, key, run):
    for batch in run["batches"]:
        if batch["status"] not in ("submitted", "unknown"):
            continue
        try:
            result = await service.transaction_status(batch["digest"])
        except Exception:
            # Not found/timeouts do not prove that a signed transaction failed.
            continue
        batch.update(status="sent" if result["success"] else "failed")
        batch.pop("error", None)
        if result.get("error"):
            batch["error"] = result["error"][:500]
        await asyncio.to_thread(store.__setitem__, key, run)
