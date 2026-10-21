# Source repository rules

- Keep frontend and backend behavior aligned with `release/locked_version.json` and the pinned submodule commits. Do not silently change the production TTS profile, 25-step setting, seed, reference-emotion policy or independent background strategy.
- Use a subagent for independent review of content, media boundaries or release privacy when those tasks apply. Do not concurrently modify the same task data. Heavy GPU inference remains single-process; short tests and text review may run independently.
- Never commit actual Proxy/API configuration, credentials, private endpoints, task state, translations, subtitle output, speaker references, audio/video, model weights, caches or virtual environments. Local examples must remain empty of real service values and source-specific registrations.
- Preserve complete speech, factual qualifiers, natural punctuation and per-source speaker references. Do not apply a speaker selection or special correction from one source to another.
- MP4 output must be strictly below 50,000,000 bytes. Do not meet this limit by truncating files, speech or video duration. Keep the final byte check, full-media decode and audio identity protections.
- Run relevant source tests before changing release behavior. Engineering checks, source synchronization and user approval of an existing video do not prove a new source or machine is fully verified.
- Consult README.md and docs/DEPLOYMENT.md before modifying environment paths or setup. Keep source-specific correction registrations local and retain their strict evidence checks.
