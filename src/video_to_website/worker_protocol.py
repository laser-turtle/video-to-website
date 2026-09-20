"""Shared constants; helpers do not need the server's database implementation."""

PROTOCOL = 1
LEASE_SECONDS = 30
HEARTBEAT_SECONDS = 5
OFFLINE_SECONDS = 30
TASK_TIMEOUT = 7200
KINDS = {"transcribe", "scenes", "frame", "frame_hash", "activity", "clip"}
