    async def _slot_self_check(self):
        """Check for a slot mismatch after ZMod activates the job's tool.

        Wait for both active-tool channel sources to report the job slot;
        a timeout is deliberately inconclusive, never a warning.
        Best-effort and read-only.
        """
        try:
            job_slot = (await asyncio.to_thread(self._slot_sources))[0]
            if job_slot is None:
                return
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                await asyncio.sleep(1)
                sources = await asyncio.to_thread(self._slot_sources)
                # FFMInfo and zmod_ifs are both populated by ZMod's active
                # tool transition. Require both before treating the transition
                # as complete; a partial update is not evidence.
                if sources[1] == job_slot and sources[2] == job_slot:
                    break
            else:
                log(f"slot self-check inconclusive: active channel did not report job slot {job_slot}")
                return
            warning = await asyncio.to_thread(self._slot_agreement)
            if warning:
                await asyncio.to_thread(post, warning)
        except Exception as exc:
            warn(f"slot self-check failed: {exc!r}")
