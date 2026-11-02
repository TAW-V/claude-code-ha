"""Conversation entity that forwards Assist requests to Claude Code."""

import json
from collections.abc import AsyncGenerator
from typing import Literal

import aiohttp

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_TOKEN, CONF_URL, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([ClaudeCodeConversationEntity(entry)])


class ClaudeCodeConversationEntity(conversation.ConversationEntity, conversation.AbstractConversationAgent):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = conversation.ConversationEntityFeature.CONTROL
    _attr_supports_streaming = True

    def __init__(self, entry: ConfigEntry) -> None:
        self.entry = entry
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Claude Code",
            manufacturer="Anthropic",
            model="Claude Code (headless)",
            entry_type=dr.DeviceEntryType.SERVICE,
        )

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        return MATCH_ALL

    async def _async_handle_message(
        self, user_input: conversation.ConversationInput, chat_log: conversation.ChatLog
    ) -> conversation.ConversationResult:
        user_name = None
        if user_input.context.user_id:
            user = await self.hass.auth.async_get_user(user_input.context.user_id)
            user_name = user.name if user else None
        device_name = None
        if user_input.device_id:
            device = dr.async_get(self.hass).async_get(user_input.device_id)
            device_name = (device.name_by_user or device.name) if device else None

        payload = {
            "text": user_input.text,
            "conversation_id": chat_log.conversation_id,
            "language": user_input.language,
            "user": user_name,
            "device": device_name,
        }
        async for _content in chat_log.async_add_delta_content_stream(self.entity_id, self._stream(payload)):
            pass
        return conversation.async_get_result_from_chat_log(user_input, chat_log)

    async def _stream(self, payload: dict) -> AsyncGenerator[conversation.AssistantContentDeltaDict]:
        """Answer first, then follow-up results while Claude Code keeps working, all in one message."""
        yield {"role": "assistant"}
        sent = False
        try:
            session = async_get_clientsession(self.hass)
            async with session.post(
                f"{self.entry.data[CONF_URL]}/conversation/stream",
                json=payload,
                headers={"Authorization": f"Bearer {self.entry.data[CONF_TOKEN]}"},
                # the bridge sends a line at least every 15 s while background work runs
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=660),
            ) as resp:
                resp.raise_for_status()
                async for raw in resp.content:
                    if not raw.strip():
                        continue
                    msg = json.loads(raw)
                    if msg.get("type") == "text" and msg.get("text"):
                        yield {"content": ("\n\n" if sent else "") + msg["text"]}
                        sent = True
                    elif msg.get("type") == "done":
                        break
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            if not sent:
                yield {"content": f"Claude Code is not reachable ({type(err).__name__})."}
