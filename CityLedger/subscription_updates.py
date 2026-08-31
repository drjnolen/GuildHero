"""Keep payment validation responsive without racing wallet/calendar state."""

import asyncio

from telegram.ext import BaseUpdateProcessor


class BillingUpdateProcessor(BaseUpdateProcessor):
    def __init__(self):
        super().__init__(max_concurrent_updates=256)
        self._ordinary_updates = asyncio.Lock()

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_process_update(self, update, coroutine):
        message = getattr(update, 'effective_message', None)
        billing = getattr(update, 'pre_checkout_query', None) or (
            message and (message.successful_payment or message.refunded_payment)
        )
        if billing:
            await coroutine
        else:
            # ConversationHandler state and existing money-transfer handlers
            # keep the same serial behavior as before. Only billing can bypass.
            async with self._ordinary_updates:
                await coroutine
