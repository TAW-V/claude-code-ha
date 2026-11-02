"""Conversation entity that forwards Assist requests to Claude Code."""

import json
from collections.abc import AsyncGenerator
from typing import Literal

import aiohttp

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import slugify

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
        followup = self._followup_targets(user_input)

        payload = {
            "text": user_input.text,
            "conversation_id": chat_log.conversation_id,
            "language": user_input.language,
            "user": user_name,
            "device": device_name,
            # Where results go that arrive after the reply is closed. A voice satellite cannot keep a reply open,
            # so it gets the answer right away and hears the rest as an announcement.
            "followup": followup,
            "follow": not followup["announce"],
        }
        async for _content in chat_log.async_add_delta_content_stream(self.entity_id, self._stream(payload)):
            pass
        return conversation.async_get_result_from_chat_log(user_input, chat_log)

    def _followup_targets(self, user_input: conversation.ConversationInput) -> dict[str, list[str]]:
        """The device that asked: its satellite, its mobile app, or else the asking user's mobile apps."""
        announce: list[str] = []
        notify: list[str] = []
        if user_input.device_id:
            device = dr.async_get(self.hass).async_get(user_input.device_id)
            announce = [
                ent.entity_id
                for ent in er.async_entries_for_device(er.async_get(self.hass), user_input.device_id)
                if ent.domain == "assist_satellite"
            ]
            if device:
                notify = [
                    self._mobile_app_service(entry)
                    for entry in self.hass.config_entries.async_entries("mobile_app")
                    if entry.entry_id in device.config_entries
                ]
        if not announce and not notify and user_input.context.user_id:
            notify = [
                self._mobile_app_service(entry)
                for entry in self.hass.config_entries.async_entries("mobile_app")
                if entry.data.get("user_id") == user_input.context.user_id
            ]
        return {"announce": announce, "notify": [s for s in notify if s]}

    @staticmethod
    def _mobile_app_service(entry) -> str:
        name = entry.data.get("device_name")
        return f"notify.mobile_app_{slugify(name)}" if name else ""

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
