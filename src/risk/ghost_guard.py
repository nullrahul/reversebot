"""
Ghost Guard — Polygon Nonce Invalidation Monitor.

MISTAKE 13 GUARD: Ghost Fills occur when an off-chain order is matched 
against an on-chain nonce that has already been incremented (invalidated).
This component monitors the Polygon blockchain for the incrementNonce() 
signature (0x627cdcb9) from the bot's own address.

If detected, it triggers the kill switch immediately to prevent the 
matching engine from attempting to fill now-invalid orders.
"""

import asyncio
from typing import Optional
from web3 import Web3
from src.logs.log_config import get_logger

logger = get_logger("ghost_guard")

# CTF Exchange incrementNonce() signature: keccak256("incrementNonce()")[:10]
INCREMENT_NONCE_SIG = "0x627cdcb9"


class GhostGuard:
    """
    Asynchronous monitor for Polygon on-chain nonce resets.
    """

    def __init__(self, rpc_url: str, wallet_address: str, kill_switch):
        self._rpc_url = rpc_url
        self._address = Web3.to_checksum_address(wallet_address)
        self._kill_switch = kill_switch
        self._w3: Optional[Web3] = None
        
        self._should_run = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Start the monitoring task."""
        try:
            self._w3 = Web3(Web3.HTTPProvider(self._rpc_url))
            
            # Non-blocking connection check
            if not await asyncio.get_event_loop().run_in_executor(None, self._w3.is_connected):
                logger.error("ghost_guard_rpc_failed", url=self._rpc_url)
                return

            self._should_run = True
            self._task = asyncio.create_task(self._monitor_loop())
            
            logger.info(
                "ghost_guard_started", 
                address=self._address, 
                rpc=self._rpc_url,
                message="Ghost Guard active — monitoring Polygon for nonce resets"
            )
        except Exception as e:
            logger.error("ghost_guard_init_failed", error=str(e))

    async def stop(self) -> None:
        """Stop the monitoring task."""
        self._should_run = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _monitor_loop(self) -> None:
        """Main loop polling for new blocks and checking for nonce increments."""
        # Start from the current block to avoid historical noise
        try:
            last_block = await asyncio.get_event_loop().run_in_executor(None, lambda: self._w3.eth.block_number)
        except Exception as e:
            logger.error("ghost_guard_block_fetch_failed", error=str(e))
            return

        while self._should_run:
            try:
                current_block = await asyncio.get_event_loop().run_in_executor(None, lambda: self._w3.eth.block_number)
                
                if current_block > last_block:
                    # Limit scan to avoid overwhelming the RPC if we fall behind
                    start_bn = max(last_block + 1, current_block - 10)
                    
                    for bn in range(start_bn, current_block + 1):
                        block = await asyncio.get_event_loop().run_in_executor(
                            None, 
                            lambda b=bn: self._w3.eth.get_block(b, full_transactions=True)
                        )
                        
                        for tx in block.transactions:
                            # Only care about transactions FROM our address
                            if tx.get('from') and tx['from'].lower() == self._address.lower():
                                input_data = str(tx.get('input', ""))
                                
                                # Check for incrementNonce() signature
                                if input_data.startswith(INCREMENT_NONCE_SIG):
                                    logger.critical(
                                        "ghost_fill_reset_detected",
                                        tx_hash=tx.hash.hex(),
                                        block=bn,
                                        message="CRITICAL: incrementNonce() detected on-chain! All off-chain orders invalidated."
                                    )
                                    
                                    # Trigger kill switch immediately
                                    await self._kill_switch.trigger(
                                        "GHOST_FILL_GUARD", 
                                        {"tx_hash": tx.hash.hex(), "block": bn}
                                    )
                                    return # Stop monitoring once triggered

                    last_block = current_block

            except Exception as e:
                logger.warning("ghost_guard_cycle_error", error=str(e))
            
            # Polygon block time is ~2s
            await asyncio.sleep(2.0)
