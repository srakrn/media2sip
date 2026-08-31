"""Constants for the PBX Page integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "pbx_page"

# -- config entry ---------------------------------------------------------

CONF_URL: Final = "url"
CONF_TOKEN: Final = "token"
CONF_TARGETS: Final = "targets"
CONF_NAME: Final = "name"
CONF_EXTENSION: Final = "extension"

# -- options --------------------------------------------------------------

CONF_LEAD_IN: Final = "lead_in"
CONF_CHIME: Final = "chime"
CONF_POLICY: Final = "policy"
CONF_GLOBAL_LOCK: Final = "global_lock"

DEFAULT_LEAD_IN: Final = 1.0
DEFAULT_POLICY: Final = "queue"

# -- concurrency ----------------------------------------------------------

POLICY_QUEUE: Final = "queue"
POLICY_PREEMPT: Final = "preempt"
POLICY_REJECT: Final = "reject"
POLICIES: Final = [POLICY_QUEUE, POLICY_PREEMPT, POLICY_REJECT]

# Bounded, and small. These are announcements of a few seconds; a deep queue just
# means playing something the listener has stopped caring about.
QUEUE_DEPTH: Final = 3

# -- sidecar events -------------------------------------------------------

EVENT_CALLING: Final = "calling"
EVENT_EARLY: Final = "early"
EVENT_CONFIRMED: Final = "confirmed"
EVENT_PLAYBACK_STARTED: Final = "playback_started"
EVENT_PLAYBACK_FINISHED: Final = "playback_finished"
EVENT_DISCONNECTED: Final = "disconnected"
EVENT_REGISTERED: Final = "registered"
EVENT_UNREGISTERED: Final = "unregistered"

# -- services -------------------------------------------------------------

SERVICE_PAGE: Final = "page"
ATTR_TEXT: Final = "text"
ATTR_SOUND: Final = "sound"
ATTR_TARGETS: Final = "targets"
ATTR_CHIME: Final = "chime"
ATTR_PRIORITY: Final = "priority"

PRIORITY_NORMAL: Final = "normal"
PRIORITY_URGENT: Final = "urgent"

SOUND_PREFIX: Final = "sound:"
