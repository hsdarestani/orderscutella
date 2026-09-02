import os
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

_DATA = tempfile.TemporaryDirectory()
os.environ.setdefault('DATA_DIR', _DATA.name)
os.environ.setdefault('BOTTOKEN', '123456:test-token')

from telegram import Chat, Message, PhotoSize, Update, User
from app import product_flow as pf
from app.db import clear_state, set_state


class ProductRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clear_state(10)
        self.bot = AsyncMock()
        self.chat = Chat(10, 'private')
        self.user = User(10, 'Tester', False)
        self.reply = Message(99, datetime.now(timezone.utc), self.chat)
        self.reply.set_bot(self.bot)
        self.bot.send_message.return_value = self.reply
        self.context = SimpleNamespace(bot=self.bot, user_data={})
        self.application = SimpleNamespace(bot=self.bot)

    def make_update(self, *, photo=True, group=None, text=None):
        msg = Message(1, datetime.now(timezone.utc), self.chat, from_user=self.user,
                      photo=[PhotoSize('photo-1', 'unique-1', 100, 100)] if photo else None,
                      media_group_id=group, text=text)
        msg.set_bot(self.bot)
        if photo:
            msg.photo[0].set_bot(self.bot)
        return Update(1, message=msg)

    async def dispatch(self, handler, update):
        check = handler.check_update(update)
        self.assertIsNotNone(check)
        await handler.handle_update(update, self.application, check, self.context)
        self.assertEqual(handler._conversations[(10, 10)], pf.ACTIVE)

    async def test_cover_success_keeps_gallery_routed_after_reply(self):
        pf._save(10, 'cover', {'name': 'Product', 'type': 'simple'})
        handler = pf.handler()  # No in-memory conversation: simulate a restart.
        with patch.object(pf.main, 'guard', AsyncMock(return_value=True)), \
             patch.object(pf, '_fast_upload_media', return_value={'id': 50, 'source_url': 'image'}):
            await self.dispatch(handler, self.make_update())
        self.assertEqual(pf._state(10)['step'], 'gallery')
        self.assertEqual(pf._state(10)['payload']['cover']['id'], 50)
        # The next gallery photo still reaches the product handler.
        self.assertIsNotNone(handler.check_update(self.make_update(group='album')))

    async def test_cover_album_rejection_keeps_conversation_active(self):
        pf._save(10, 'cover', {})
        with patch.object(pf.main, 'guard', AsyncMock(return_value=True)):
            await self.dispatch(pf.handler(), self.make_update(group='album'))
        self.assertEqual(pf._state(10)['step'], 'cover')

    async def test_failed_cover_keeps_conversation_active(self):
        pf._save(10, 'cover', {})
        with patch.object(pf.main, 'guard', AsyncMock(return_value=True)), \
             patch.object(pf, '_fast_upload_media', side_effect=RuntimeError('Bridge HTTP 503')):
            await self.dispatch(pf.handler(), self.make_update())
        self.assertEqual(pf._state(10)['step'], 'cover')

    async def test_restart_resumes_text_step(self):
        pf._save(10, 'stock', {'type': 'simple'})
        with patch.object(pf.main, 'guard', AsyncMock(return_value=True)):
            await self.dispatch(pf.handler(), self.make_update(photo=False, text='7'))
        self.assertEqual(pf._state(10)['payload']['stock'], 7)

    async def test_unrelated_flow_is_not_claimed(self):
        set_state(10, 'shopino', 'phone', {})
        self.assertIsNone(pf.handler().check_update(self.make_update()))
        self.assertIsNone(pf.handler().check_update(self.make_update(photo=False, text='123')))
