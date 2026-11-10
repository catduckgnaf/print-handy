# Issue #126 timelapse delivery contract

The Moonraker listener sends completion and an eligible timelapse together through the configured Discord target. The message uses the `MEDIA:<local path>` marker, so the attachment path must be a local, materialized MP4, not the remote archive path. The listener first archives the rendered video, downloads it to its local media cache, and then applies Discord size reduction before adding the marker.

If rendering, archive retrieval, transcoding, or attachment delivery fails, the listener sends the print completion text separately. A failed attachment send is not reported as delivered, and completion acknowledgment remains governed by the completion delivery state. For a multi-plate plan, the combined video is sent after the final plate; a cancelled plan with clips labels the output partial.

Offline listener tests mock the REST boundary. They verify configured recipient, marker/path handoff, and interpretation of a failed send result. They do not contact Discord or prove end-to-end delivery to a live conversation.
