"""Serialize each chat's conversations without blocking unrelated groups."""

import asyncio

from telegram.ext import BaseUpdateProcessor


class BillingUpdateProcessor(BaseUpdateProcessor):
    def __init__(self):
        super().__init__(max_concurrent_updates=256)
        self._chat_locks = {}

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
            # ConversationHandler uses per-chat state. Keep each chat serial,
            # including its callbacks, while another group's AI request waits.
            chat = getattr(update, 'effective_chat', None)
            key = chat.id if chat else None
            entry = self._chat_locks.setdefault(key, [asyncio.Lock(), 0])
            entry[1] += 1
            started = False
            try:
                async with entry[0]:
                    started = True
                    await coroutine
            finally:
                if not started:
                    coroutine.close()
                entry[1] -= 1
                if not entry[1]:
                    del self._chat_locks[key]
