# BTC-LRA-002 replay audit

```json
{
  "mode": "replay",
  "bars": 13810,
  "events": 5452655,
  "human_lines_emitted": 0,
  "human_eligible_event_count": 216164,
  "human_event_counts": {
    "OPPOSITE_CONTROL_CANDIDATE": 36863,
    "BATTLE_STARTED": 43252,
    "BATTLE_RESOLUTION_HOLDING": 39552,
    "BATTLE_RESOLUTION_CANDIDATE": 59634,
    "PASSIVE_REJECTION_EXIT_WARNING": 36863
  },
  "human_timeline_sample": [
    {
      "time": "2026-09-20T08:01:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T08:06:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T08:06:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T08:11:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T08:16:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T08:16:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T08:16:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T08:20:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T08:20:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T08:21:00Z",
      "event": "BATTLE_RESOLUTION_HOLDING"
    },
    {
      "time": "2026-09-20T08:21:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T08:25:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T08:25:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T08:25:00Z",
      "event": "PASSIVE_REJECTION_EXIT_WARNING"
    },
    {
      "time": "2026-09-20T08:26:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T08:27:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T08:27:00Z",
      "event": "OPPOSITE_CONTROL_CANDIDATE"
    },
    {
      "time": "2026-09-20T08:28:00Z",
      "event": "BATTLE_RESOLUTION_HOLDING"
    },
    {
      "time": "2026-09-20T08:31:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T08:31:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T08:35:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T08:35:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T08:36:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T08:41:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T08:49:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T08:49:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T08:49:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T08:49:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T08:49:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T08:49:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T08:49:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T08:49:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T08:49:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T08:56:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T08:57:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T08:57:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T08:57:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T08:57:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T08:57:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T08:57:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T08:57:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T08:57:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T08:57:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T08:58:00Z",
      "event": "BATTLE_RESOLUTION_HOLDING"
    },
    {
      "time": "2026-09-20T08:58:00Z",
      "event": "BATTLE_RESOLUTION_HOLDING"
    },
    {
      "time": "2026-09-20T08:58:00Z",
      "event": "BATTLE_RESOLUTION_HOLDING"
    },
    {
      "time": "2026-09-20T08:58:00Z",
      "event": "BATTLE_RESOLUTION_HOLDING"
    },
    {
      "time": "2026-09-20T08:58:00Z",
      "event": "BATTLE_RESOLUTION_HOLDING"
    },
    {
      "time": "2026-09-20T08:58:00Z",
      "event": "BATTLE_RESOLUTION_HOLDING"
    },
    {
      "time": "2026-09-20T08:58:00Z",
      "event": "BATTLE_RESOLUTION_HOLDING"
    },
    {
      "time": "2026-09-20T08:58:00Z",
      "event": "BATTLE_RESOLUTION_HOLDING"
    },
    {
      "time": "2026-09-20T08:58:00Z",
      "event": "BATTLE_RESOLUTION_HOLDING"
    },
    {
      "time": "2026-09-20T08:59:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T08:59:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T08:59:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T08:59:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T08:59:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T08:59:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T08:59:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T08:59:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T08:59:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T08:59:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T09:00:00Z",
      "event": "BATTLE_RESOLUTION_HOLDING"
    },
    {
      "time": "2026-09-20T09:01:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T09:02:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:02:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:02:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:02:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:02:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:02:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:02:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:02:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:02:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:03:00Z",
      "event": "BATTLE_RESOLUTION_HOLDING"
    },
    {
      "time": "2026-09-20T09:03:00Z",
      "event": "BATTLE_RESOLUTION_HOLDING"
    },
    {
      "time": "2026-09-20T09:03:00Z",
      "event": "BATTLE_RESOLUTION_HOLDING"
    },
    {
      "time": "2026-09-20T09:03:00Z",
      "event": "BATTLE_RESOLUTION_HOLDING"
    },
    {
      "time": "2026-09-20T09:03:00Z",
      "event": "BATTLE_RESOLUTION_HOLDING"
    },
    {
      "time": "2026-09-20T09:03:00Z",
      "event": "BATTLE_RESOLUTION_HOLDING"
    },
    {
      "time": "2026-09-20T09:03:00Z",
      "event": "BATTLE_RESOLUTION_HOLDING"
    },
    {
      "time": "2026-09-20T09:03:00Z",
      "event": "BATTLE_RESOLUTION_HOLDING"
    },
    {
      "time": "2026-09-20T09:03:00Z",
      "event": "BATTLE_RESOLUTION_HOLDING"
    },
    {
      "time": "2026-09-20T09:04:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T09:04:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T09:04:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T09:04:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T09:04:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T09:04:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T09:04:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T09:04:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T09:04:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T09:06:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T09:11:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T09:14:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:15:00Z",
      "event": "BATTLE_RESOLUTION_HOLDING"
    },
    {
      "time": "2026-09-20T09:15:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:16:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T09:18:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:19:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:19:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:19:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:19:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:19:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:19:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:19:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:19:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:19:00Z",
      "event": "BATTLE_RESOLUTION_HOLDING"
    },
    {
      "time": "2026-09-20T09:19:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:19:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:20:00Z",
      "event": "BATTLE_RESOLUTION_HOLDING"
    },
    {
      "time": "2026-09-20T09:20:00Z",
      "event": "BATTLE_RESOLUTION_HOLDING"
    },
    {
      "time": "2026-09-20T09:20:00Z",
      "event": "BATTLE_RESOLUTION_HOLDING"
    },
    {
      "time": "2026-09-20T09:20:00Z",
      "event": "BATTLE_RESOLUTION_HOLDING"
    },
    {
      "time": "2026-09-20T09:20:00Z",
      "event": "BATTLE_RESOLUTION_HOLDING"
    },
    {
      "time": "2026-09-20T09:20:00Z",
      "event": "BATTLE_RESOLUTION_HOLDING"
    },
    {
      "time": "2026-09-20T09:20:00Z",
      "event": "BATTLE_RESOLUTION_HOLDING"
    },
    {
      "time": "2026-09-20T09:20:00Z",
      "event": "BATTLE_RESOLUTION_HOLDING"
    },
    {
      "time": "2026-09-20T09:20:00Z",
      "event": "BATTLE_RESOLUTION_HOLDING"
    },
    {
      "time": "2026-09-20T09:20:00Z",
      "event": "BATTLE_RESOLUTION_HOLDING"
    },
    {
      "time": "2026-09-20T09:21:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T09:21:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T09:21:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T09:21:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T09:21:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T09:21:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T09:21:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:21:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T09:21:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T09:21:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T09:21:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T09:22:00Z",
      "event": "BATTLE_RESOLUTION_HOLDING"
    },
    {
      "time": "2026-09-20T09:24:00Z",
      "event": "PASSIVE_REJECTION_EXIT_WARNING"
    },
    {
      "time": "2026-09-20T09:24:00Z",
      "event": "PASSIVE_REJECTION_EXIT_WARNING"
    },
    {
      "time": "2026-09-20T09:24:00Z",
      "event": "PASSIVE_REJECTION_EXIT_WARNING"
    },
    {
      "time": "2026-09-20T09:24:00Z",
      "event": "PASSIVE_REJECTION_EXIT_WARNING"
    },
    {
      "time": "2026-09-20T09:24:00Z",
      "event": "PASSIVE_REJECTION_EXIT_WARNING"
    },
    {
      "time": "2026-09-20T09:24:00Z",
      "event": "PASSIVE_REJECTION_EXIT_WARNING"
    },
    {
      "time": "2026-09-20T09:24:00Z",
      "event": "PASSIVE_REJECTION_EXIT_WARNING"
    },
    {
      "time": "2026-09-20T09:24:00Z",
      "event": "PASSIVE_REJECTION_EXIT_WARNING"
    },
    {
      "time": "2026-09-20T09:24:00Z",
      "event": "PASSIVE_REJECTION_EXIT_WARNING"
    },
    {
      "time": "2026-09-20T09:24:00Z",
      "event": "PASSIVE_REJECTION_EXIT_WARNING"
    },
    {
      "time": "2026-09-20T09:24:00Z",
      "event": "PASSIVE_REJECTION_EXIT_WARNING"
    },
    {
      "time": "2026-09-20T09:24:00Z",
      "event": "PASSIVE_REJECTION_EXIT_WARNING"
    },
    {
      "time": "2026-09-20T09:24:00Z",
      "event": "PASSIVE_REJECTION_EXIT_WARNING"
    },
    {
      "time": "2026-09-20T09:24:00Z",
      "event": "PASSIVE_REJECTION_EXIT_WARNING"
    },
    {
      "time": "2026-09-20T09:24:00Z",
      "event": "PASSIVE_REJECTION_EXIT_WARNING"
    },
    {
      "time": "2026-09-20T09:24:00Z",
      "event": "PASSIVE_REJECTION_EXIT_WARNING"
    },
    {
      "time": "2026-09-20T09:24:00Z",
      "event": "PASSIVE_REJECTION_EXIT_WARNING"
    },
    {
      "time": "2026-09-20T09:24:00Z",
      "event": "PASSIVE_REJECTION_EXIT_WARNING"
    },
    {
      "time": "2026-09-20T09:24:00Z",
      "event": "PASSIVE_REJECTION_EXIT_WARNING"
    },
    {
      "time": "2026-09-20T09:24:00Z",
      "event": "PASSIVE_REJECTION_EXIT_WARNING"
    },
    {
      "time": "2026-09-20T09:24:00Z",
      "event": "PASSIVE_REJECTION_EXIT_WARNING"
    },
    {
      "time": "2026-09-20T09:25:00Z",
      "event": "OPPOSITE_CONTROL_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:25:00Z",
      "event": "OPPOSITE_CONTROL_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:25:00Z",
      "event": "OPPOSITE_CONTROL_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:25:00Z",
      "event": "OPPOSITE_CONTROL_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:25:00Z",
      "event": "OPPOSITE_CONTROL_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:25:00Z",
      "event": "OPPOSITE_CONTROL_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:25:00Z",
      "event": "OPPOSITE_CONTROL_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:25:00Z",
      "event": "OPPOSITE_CONTROL_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:25:00Z",
      "event": "OPPOSITE_CONTROL_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:25:00Z",
      "event": "OPPOSITE_CONTROL_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:25:00Z",
      "event": "OPPOSITE_CONTROL_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:25:00Z",
      "event": "OPPOSITE_CONTROL_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:25:00Z",
      "event": "OPPOSITE_CONTROL_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:25:00Z",
      "event": "OPPOSITE_CONTROL_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:25:00Z",
      "event": "OPPOSITE_CONTROL_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:25:00Z",
      "event": "OPPOSITE_CONTROL_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:25:00Z",
      "event": "OPPOSITE_CONTROL_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:25:00Z",
      "event": "OPPOSITE_CONTROL_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:25:00Z",
      "event": "OPPOSITE_CONTROL_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:25:00Z",
      "event": "OPPOSITE_CONTROL_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:25:00Z",
      "event": "OPPOSITE_CONTROL_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:26:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T09:31:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T09:32:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:33:00Z",
      "event": "BATTLE_RESOLUTION_HOLDING"
    },
    {
      "time": "2026-09-20T09:35:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T09:36:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T09:40:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T09:43:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T09:46:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T09:46:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T09:50:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T09:51:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T09:56:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T09:56:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T10:01:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T10:01:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T10:01:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T10:01:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T10:01:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T10:02:00Z",
      "event": "BATTLE_RESOLUTION_HOLDING"
    },
    {
      "time": "2026-09-20T10:02:00Z",
      "event": "BATTLE_RESOLUTION_HOLDING"
    },
    {
      "time": "2026-09-20T10:04:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T10:04:00Z",
      "event": "BATTLE_RESOLUTION_CANDIDATE"
    },
    {
      "time": "2026-09-20T10:05:00Z",
      "event": "BATTLE_RESOLUTION_HOLDING"
    },
    {
      "time": "2026-09-20T10:05:00Z",
      "event": "BATTLE_RESOLUTION_HOLDING"
    },
    {
      "time": "2026-09-20T10:06:00Z",
      "event": "BATTLE_STARTED"
    },
    {
      "time": "2026-09-20T10:06:00Z",
      "event": "BATTLE_STARTED"
    }
  ],
  "future_leakage_errors": 0,
  "oi_resolution_note": "historical OI remains 5m/source resolution; nearest prior metadata is preserved",
  "existing_reference_benchmarks": [
    {
      "time": "2026-09-22 19:10:00",
      "state": "BATTLE_RESOLUTION_CANDIDATE",
      "zone_id": "1H-1790046000000",
      "source": "existing research reference only"
    },
    {
      "time": "2026-09-22 19:10:00",
      "state": "BATTLE_RESOLUTION_CANDIDATE",
      "zone_id": "5M-1790101800000",
      "source": "existing research reference only"
    },
    {
      "time": "2026-09-22 19:10:00",
      "state": "BATTLE_RESOLUTION_CANDIDATE",
      "zone_id": "15M-1790092800000",
      "source": "existing research reference only"
    },
    {
      "time": "2026-09-22 19:11:00",
      "state": "BATTLE_RESOLUTION_HOLDING",
      "zone_id": "1H-1790046000000",
      "source": "existing research reference only"
    },
    {
      "time": "2026-09-22 19:11:00",
      "state": "BATTLE_RESOLUTION_HOLDING",
      "zone_id": "5M-1790101800000",
      "source": "existing research reference only"
    },
    {
      "time": "2026-09-22 19:11:00",
      "state": "BATTLE_RESOLUTION_HOLDING",
      "zone_id": "15M-1790092800000",
      "source": "existing research reference only"
    },
    {
      "time": "2026-09-22 20:56:00",
      "state": "TRANSFER_CHALLENGED",
      "zone_id": "1H-1790046000000",
      "source": "existing research reference only"
    },
    {
      "time": "2026-09-22 20:58:00",
      "state": "BATTLE_RESOLUTION_HOLDING",
      "zone_id": "1H-1790046000000",
      "source": "existing research reference only"
    },
    {
      "time": "2026-09-22 20:58:00",
      "state": "BATTLE_RESOLUTION_HOLDING",
      "zone_id": "5M-1790101800000",
      "source": "existing research reference only"
    },
    {
      "time": "2026-09-22 20:58:00",
      "state": "BATTLE_RESOLUTION_HOLDING",
      "zone_id": "15M-1790092800000",
      "source": "existing research reference only"
    },
    {
      "time": "2026-09-28 06:58:00",
      "state": "BATTLE_RESOLUTION_HOLDING",
      "zone_id": "15M-1790574300000",
      "source": "existing research reference only"
    },
    {
      "time": "2026-09-28 07:04:00",
      "state": "TRANSFER_CHALLENGED",
      "zone_id": "5M-1790566800000",
      "source": "existing research reference only"
    },
    {
      "time": "2026-09-28 07:06:00",
      "state": "BATTLE_RESOLUTION_HOLDING",
      "zone_id": "5M-1790566800000",
      "source": "existing research reference only"
    },
    {
      "time": "2026-09-29 00:48:00",
      "state": "TRANSFER_CHALLENGED",
      "zone_id": "15M-1790639100000",
      "source": "existing research reference only"
    },
    {
      "time": "2026-09-29 00:50:00",
      "state": "BATTLE_RESOLUTION_HOLDING",
      "zone_id": "5M-1790645700000",
      "source": "existing research reference only"
    },
    {
      "time": "2026-09-29 00:50:00",
      "state": "BATTLE_RESOLUTION_HOLDING",
      "zone_id": "15M-1790639100000",
      "source": "existing research reference only"
    }
  ],
  "engine_digest_sha256": "c700b8269801745a78936bc3e41f9ffd6e8644fe5d3fc9e498491734a55f9963",
  "profile": {
    "telemetry": "events",
    "time_seconds": {
      "update_zones": 2245.969085105171,
      "active_zones_at": 24.74051590001909,
      "battle_step": 703.968099907448,
      "release_step": 554.5731099559343,
      "emit": 2885.4046902412665,
      "jsonl_writes": 2103.0094624605263
    },
    "counts": {
      "zones_created": 3121,
      "peak_active_zones": 1394,
      "battles_created": 43252,
      "peak_active_battles": 2952,
      "battle_bar_rows_written": 0,
      "releases_created": 39552,
      "peak_active_releases": 5317,
      "release_step_calls": 22743924,
      "total_machine_events": 5452655,
      "simultaneously_active_zones": 41,
      "simultaneously_active_battles": 2950,
      "simultaneously_active_releases": 2689,
      "active_releases_not_closed": 2689,
      "closed_releases": 0,
      "orphan_active_releases": 0,
      "average_active_zones": 58.24105720492397,
      "average_active_battles": 1365.1654598117307,
      "average_active_releases": 1647.1117306299782
    },
    "bars": 13810
  },
  "human_suppression_pass": true
}
```
