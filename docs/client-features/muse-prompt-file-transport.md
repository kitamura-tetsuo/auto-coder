# Muse prompt transport

Muse tasks are rendered once and carried as the single text part of one MSP
`turn/start` command. Prompt text is not placed in process arguments,
environment values, temporary prompt files, or diagnostics. Configured CLI
prompt-source and session-selection options are rejected because Auto-Coder
owns both the MSP session and prompt transport.
