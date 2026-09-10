from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from core.protocol import RelayError


@dataclass
class _ChunkState:
    total_parts: int
    received_parts: dict[int, str] = field(default_factory=dict)
    last_received_timestamp: float = field(default_factory=time.monotonic)
    is_final_chunk_received: bool = False

class ChunkReassembler:
    """Manages the reassembly of chunked messages."""
    def __init__(self, timeout_s: float = 60.0):
        self._chunks: dict[str, _ChunkState] = {}
        self._timeout_s = timeout_s

    def add_chunk(self, chunk_message: dict[str, Any]) -> None:
        message_id = chunk_message["message_id"]
        part = chunk_message["part"]
        parts = chunk_message["parts"]
        payload = chunk_message["payload"]
        is_final = chunk_message["final"]

        if message_id not in self._chunks:
            self._chunks[message_id] = _ChunkState(total_parts=parts)
        
        chunk_state = self._chunks[message_id]

        if parts != chunk_state.total_parts:
            # Conflicting chunk, possibly a protocol violation or error
            raise RelayError(f"Conflicting 'parts' count for message_id {message_id}: expected {chunk_state.total_parts}, got {parts}")
        
        if part in chunk_state.received_parts:
            # Duplicate chunk, ignore for now
            return

        chunk_state.received_parts[part] = payload
        chunk_state.last_received_timestamp = time.monotonic()
        if is_final:
            chunk_state.is_final_chunk_received = True

    def is_complete(self, message_id: str) -> bool:
        if message_id not in self._chunks:
            return False
        
        chunk_state = self._chunks[message_id]
        return chunk_state.is_final_chunk_received and \
               len(chunk_state.received_parts) == chunk_state.total_parts

    def get_reassembled_message(self, message_id: str) -> str:
        if not self.is_complete(message_id):
            raise RelayError(f"Message {message_id} is not complete.")
        
        chunk_state = self._chunks[message_id]
        sorted_payloads = [chunk_state.received_parts[i] for i in range(1, chunk_state.total_parts + 1)]
        return "".join(sorted_payloads)

    def clear_message(self, message_id: str) -> None:
        if message_id in self._chunks:
            del self._chunks[message_id]
    
    def cleanup_old_chunks(self) -> None:
        now = time.monotonic()
        messages_to_delete = [
            msg_id for msg_id, chunk_state in self._chunks.items()
            if now - chunk_state.last_received_timestamp > self._timeout_s
        ]
        for msg_id in messages_to_delete:
            del self._chunks[msg_id]
