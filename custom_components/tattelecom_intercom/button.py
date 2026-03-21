"""Button component."""


from __future__ import annotations

import asyncio
import logging

import httpx
from homeassistant.components.button import (
    ENTITY_ID_FORMAT,
    ButtonEntity,
    ButtonEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    BUTTON_ANSWER,
    BUTTON_ANSWER_NAME,
    BUTTON_DECLINE,
    BUTTON_DECLINE_NAME,
    BUTTON_HANGUP,
    BUTTON_HANGUP_NAME,
    BUTTON_OPEN,
    BUTTON_OPEN_NAME,
    SIGNAL_CALL_STATE,
    SIGNAL_NEW_INTERCOM,
)
from .entity import IntercomEntity
from .intercom_enum import CallState
from .exceptions import IntercomError
from .updater import IntercomEntityDescription, IntercomUpdater, async_get_updater

PARALLEL_UPDATES = 0

_LOGGER = logging.getLogger(__name__)

BUTTONS: tuple[ButtonEntityDescription, ...] = (
    ButtonEntityDescription(
        key=BUTTON_ANSWER,
        name=BUTTON_ANSWER_NAME,
        icon="mdi:phone-in-talk",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=True,
        translation_key="answer",
    ),
    ButtonEntityDescription(
        key=BUTTON_DECLINE,
        name=BUTTON_DECLINE_NAME,
        icon="mdi:phone-cancel",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=True,
        translation_key="decline",
    ),
    ButtonEntityDescription(
        key=BUTTON_HANGUP,
        name=BUTTON_HANGUP_NAME,
        icon="mdi:phone-hangup",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=True,
        translation_key="hangup",
    ),
    ButtonEntityDescription(
        key=BUTTON_OPEN,
        name=BUTTON_OPEN_NAME,
        icon="mdi:lock-open",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=True,
        translation_key="open_door",
    ),
)


# pylint: disable=too-many-ancestors
async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Tattelecom intercom button entry.

    :param hass: HomeAssistant: Home Assistant object
    :param config_entry: ConfigEntry: ConfigEntry object
    :param async_add_entities: AddEntitiesCallback: AddEntitiesCallback callback object
    """

    updater: IntercomUpdater = async_get_updater(hass, config_entry.entry_id)

    @callback
    def add_button(entity: IntercomEntityDescription) -> None:
        """Add button.

        :param entity: IntercomEntityDescription: Sensor object
        """

        async_add_entities(
            [
                IntercomButton(
                    f"{config_entry.entry_id}-button-{entity.id}",
                    ButtonEntityDescription(
                        key=f"open_{entity.id}",
                        name=BUTTON_OPEN_NAME,
                        icon="mdi:lock-open",
                        entity_category=EntityCategory.CONFIG,
                        entity_registry_enabled_default=True,
                        translation_key="open_door",
                    ),
                    updater,
                    entity.device_info,
                    intercom_id=entity.id,  # Передаем ID конкретного домофона
                )
            ]
        )

    # Создаем основные кнопки (глобальные)
    entities: list[IntercomButton] = [
        IntercomButton(
            f"{config_entry.entry_id}-{description.key}",
            description,
            updater,
        )
        for description in BUTTONS
    ]
    async_add_entities(entities)

    # Создаем кнопки открытия двери для каждого домофона
    for intercom in updater.intercoms.values():
        add_button(intercom)

    updater.new_intercom_callbacks.append(
        async_dispatcher_connect(hass, SIGNAL_NEW_INTERCOM, add_button)
    )


class IntercomButton(IntercomEntity, ButtonEntity):
    """Intercom button entry."""

    def __init__(
        self,
        unique_id: str,
        description: ButtonEntityDescription,
        updater: IntercomUpdater,
        device_info: DeviceInfo | None = None,
        intercom_id: str | None = None,  # Добавляем параметр для ID домофона
    ) -> None:
        """Initialize button.

        :param unique_id: str: Unique ID
        :param description: ButtonEntityDescription object
        :param updater: IntercomUpdater: Intercom updater object
        :param device_info: DeviceInfo | None: DeviceInfo object
        :param intercom_id: str | None: Intercom ID for door open buttons
        """

        IntercomEntity.__init__(self, unique_id, description, updater, ENTITY_ID_FORMAT)
        self._attr_should_poll = False
        self._intercom_id = intercom_id

        if device_info:
            self._attr_device_info = device_info

    @property
    def available(self) -> bool:
        """Return if button is available."""
        # Кнопки ответа/сброса доступны только при активном вызове
        if self.entity_description.key in [BUTTON_ANSWER, BUTTON_DECLINE, BUTTON_HANGUP]:
            return (self._updater.last_call is not None and 
                    self._updater.last_call.state in [CallState.RINGING, CallState.ANSWERED])
        
        # Кнопки открытия двери доступны всегда, если есть соединение
        return self._updater.last_update_success

    async def async_press(self) -> None:
        """Async press action."""

        try:
            # Обработка кнопок ответа/сброса
            if self.entity_description.key in [BUTTON_ANSWER, BUTTON_DECLINE, BUTTON_HANGUP]:
                if not self._updater.last_call:
                    _LOGGER.debug("No active call for %s", self.entity_description.key)
                    return

                if self.entity_description.key == BUTTON_ANSWER:
                    await self._updater.last_call.answer()
                    _LOGGER.debug("Answered call")
                elif self.entity_description.key == BUTTON_DECLINE:
                    await self._updater.last_call.decline()
                    _LOGGER.debug("Declined call")
                elif self.entity_description.key == BUTTON_HANGUP:
                    await self._updater.last_call.hangup()
                    _LOGGER.debug("Hung up call")

                async_dispatcher_send(self.hass, SIGNAL_CALL_STATE)
                return

            # Обработка кнопок открытия двери
            if self.entity_description.key == BUTTON_OPEN:
                # Если есть активный вызов, открываем дверь для звонящего
                if self._updater.last_call and self._updater.last_call.login in self._updater.code_map:
                    intercom_id = self._updater.code_map[self._updater.last_call.login]
                    await self._updater.client.open(int(intercom_id))
                    _LOGGER.debug("Opened door for active call")
                    return
                
                # Если нет активного вызова, но у нас есть ID домофона, открываем его
                if self._intercom_id:
                    await self._updater.client.open(int(self._intercom_id))
                    _LOGGER.debug("Opened door for intercom %s", self._intercom_id)
                    return
                
                # Если нет ни активного вызова, ни ID домофона
                _LOGGER.warning(
                    "Cannot open door: no active call and no intercom ID specified. "
                    "Use specific door buttons instead."
                )
                return

            # Обработка кнопок открытия для конкретных домофонов (с префиксом open_)
            if self.entity_description.key.startswith("open_") and self._intercom_id:
                await self._updater.client.open(int(self._intercom_id))
                _LOGGER.debug("Opened door for intercom %s", self._intercom_id)
                return

        except IntercomError as _err:
            _LOGGER.error(
                "An error occurred while pressing the button %r: %r",
                self.entity_description.key,
                _err,
            )
        except (httpx.HTTPError, asyncio.TimeoutError, ValueError) as err:
            _LOGGER.error(
                "Unexpected error pressing button %r: %s",
                self.entity_description.key,
                err,
                exc_info=True,
            )

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._updater.async_add_listener(self._handle_coordinator_update)
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()