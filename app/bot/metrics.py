"""Prometheus metrics of the MAX bot. Labels are enumerations only: no users, chats, texts or ids."""

from prometheus_client import Counter, Histogram

UPDATES = Counter(
    "assistant_bot_updates_total",
    "MAX updates handled by the bot",
    ["kind", "outcome"],  # kind: text | audio | start | other; outcome: see BotService.handle
)
SENDS = Counter(
    "assistant_bot_send_total",
    "Messages sent to MAX",
    ["purpose", "outcome"],  # purpose: welcome | status | transcript | reply | busy | error | edit
)
SEND_SECONDS = Histogram(
    "assistant_bot_send_seconds",
    "Duration of one send to MAX, including retries",
    ["purpose"],
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 40),
)
AUDIO_FETCH = Counter(
    "assistant_bot_audio_fetch_total",
    "Audio downloads from MAX",
    ["outcome"],  # ok | source_error | too_large | fetch_error
)
PROCESSING = Counter(
    "assistant_bot_processing_total",
    "Finished background processing of admitted requests",
    ["outcome"],  # replied | not_heard | audio_error | asr_error | timeout | failed | cancelled
)
DELIVERIES = Counter(
    "assistant_bot_notification_deliveries_total",
    "Notifications from the stream with a recorded result",
    ["status", "outcome"],  # status: delivered | retrying | dead_lettered; outcome: the contract's
)
REMINDERS_PUBLISHED = Counter(
    "assistant_bot_reminders_published_total",
    "Due reminders published to the notification stream by the temporary scheduler",
)
